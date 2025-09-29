# app.py
import os, time, threading, sys, hashlib
from datetime import datetime, timezone
from flask import Flask, Response, request

from prometheus_client import (
    CollectorRegistry, Counter, Histogram, Gauge,
    generate_latest, CONTENT_TYPE_LATEST,
    ProcessCollector, PlatformCollector, GCCollector,
)

# ---------- Optional Kubernetes client (app still runs without it)
HAS_K8S = True
try:
    from kubernetes import client, config, watch
except Exception as e:
    HAS_K8S = False
    print(f"[autoscale-probe] k8s client unavailable: {e}", file=sys.stderr)

# ---------- Config (env vars)
MAX_INFLIGHT       = int(os.getenv("MAX_INFLIGHT", "200"))      # concurrent requests before 503
ACQUIRE_TIMEOUT_MS = int(os.getenv("ACQUIRE_TIMEOUT_MS", "0"))  # 0=immediate 503; >0=wait then 503
CPU_MS             = int(os.getenv("CPU_MS", "20"))             # default per-request burn (ms)
PBKDF2_ITERS       = int(os.getenv("PBKDF2_ITERS", "200000"))   # PBKDF2 iterations (CPU intensity)
WATCH_NS           = os.getenv("WATCH_NAMESPACE", "default")
WATCH_SEL          = os.getenv("WATCH_SELECTOR",  "app=stress-app")
ENABLE_K8S         = os.getenv("ENABLE_K8S_WATCHERS", "1") == "1"

# ---------- Prometheus metrics (single custom registry)
REG = CollectorRegistry()

# Standard process/platform/python metrics so you get CPU & memory for THIS pod
ProcessCollector(registry=REG)    # exposes: process_cpu_seconds_total, process_resident_memory_bytes, ...
PlatformCollector(registry=REG)   # exposes: python_info
GCCollector(registry=REG)         # exposes: python_gc_* totals

# App/request-centric metrics
REQ_COUNTER = Counter(
    "loadgen_requests_total",
    "Total /hot requests by status",
    ["status"],
    registry=REG,
)
REQ_HIST = Histogram(
    "loadgen_request_duration_seconds",
    "Duration of /hot requests (seconds)",
    registry=REG,
)
INFLIGHT = Gauge(
    "loadgen_inflight",
    "In-flight /hot requests (per-process)",
    registry=REG,
)

# Request parameter visibility (helps dashboards)
CPU_MS_USED = Histogram(
    "loadgen_cpu_ms",
    "Requested CPU burn time (ms) per request",
    buckets=(1, 5, 10, 20, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 20000, 60000),
    registry=REG,
)
PARALLEL_USED = Histogram(
    "loadgen_parallel",
    "Requested worker threads per /hot",
    buckets=(1, 2, 4, 8, 16, 32, 64, 128, 256),
    registry=REG,
)
THREADS_CURRENT = Gauge(
    "loadgen_threads_current",
    "Python threading.active_count()",
    registry=REG,
)

# Config snapshot (easy to graph current limits)
CFG_GAUGE = Gauge(
    "loadgen_config",
    "Runtime config values (labels carry the keys)",
    ["key"],
    registry=REG,
)
CFG_GAUGE.labels(key="max_inflight").set(MAX_INFLIGHT)
CFG_GAUGE.labels(key="acquire_timeout_ms").set(ACQUIRE_TIMEOUT_MS)
CFG_GAUGE.labels(key="default_cpu_ms").set(CPU_MS)
CFG_GAUGE.labels(key="pbkdf2_iters").set(PBKDF2_ITERS)

# Memory workout (so you can demo memory pressure too)
ALLOC_INUSE_BYTES = Gauge(
    "loadgen_memory_inuse_bytes",
    "Bytes currently held via /mem allocations",
    registry=REG,
)
ALLOC_TOTAL_BYTES = Counter(
    "loadgen_memory_allocated_bytes_total",
    "Total bytes ever allocated via /mem",
    registry=REG,
)

# K8s-oriented gauges for readiness and scheduling (high-level timing)
NODE_READY_SECONDS = Gauge(
    "k8s_node_ready_seconds",
    "Seconds from Node creationTimestamp to first Ready=True",
    ["node"],
    registry=REG,
)
NODE_READY_GAUGE = Gauge(
    "k8s_node_ready_gauge",
    "Node Ready flag (1=Ready, 0=NotReady)",
    ["node"],
    registry=REG,
)
NODE_UNSCHEDULABLE = Gauge(
    "k8s_node_unschedulable",
    "Node spec.unschedulable flag",
    ["node"],
    registry=REG,
)
NODE_CAP_CPU_CORES = Gauge(
    "k8s_node_capacity_cpu_cores",
    "Node capacity.cpu (cores)",
    ["node"],
    registry=REG,
)
NODE_ALLOC_CPU_CORES = Gauge(
    "k8s_node_allocatable_cpu_cores",
    "Node allocatable.cpu (cores)",
    ["node"],
    registry=REG,
)
NODE_CAP_MEM_BYTES = Gauge(
    "k8s_node_capacity_memory_bytes",
    "Node capacity.memory (bytes)",
    ["node"],
    registry=REG,
)
NODE_ALLOC_MEM_BYTES = Gauge(
    "k8s_node_allocatable_memory_bytes",
    "Node allocatable.memory (bytes)",
    ["node"],
    registry=REG,
)
POD_SCHEDULE_SECONDS = Gauge(
    "k8s_pod_schedule_seconds",
    "Seconds from Pod Pending to first Running (filtered by WATCH_SELECTOR)",
    ["pod", "node"],
    registry=REG,
)
PODS_BY_PHASE = Gauge(
    "k8s_pods_phase",
    "Count of pods by phase (filtered by WATCH_SELECTOR)",
    ["namespace", "phase"],
    registry=REG,
)

# ---------- Concurrency gate and CPU burn
_gate = threading.BoundedSemaphore(MAX_INFLIGHT)
_payload = b"x" * 64
_salt = b"salt"  # fixed salt; we only care about CPU work, not cryptographic properties

def _busy_ms(ms: int):
    """Burn CPU for approximately `ms` using PBKDF2 (C code releases the GIL)."""
    end = time.time() + (ms / 1000.0)
    while time.time() < end:
        hashlib.pbkdf2_hmac("sha256", _payload, _salt, PBKDF2_ITERS)

def _burn_parallel(ms: int, parallel: int):
    """Run PBKDF2 in parallel threads. PBKDF2 releases GIL -> scales across cores."""
    threads = []
    for _ in range(parallel):
        t = threading.Thread(target=_busy_ms, args=(ms,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

# ---------- Memory burner (optional)
_LEAKS = []
_LEAKS_LOCK = threading.Lock()

def _release_after(buf, nbytes, seconds):
    try:
        time.sleep(max(0, seconds))
    finally:
        with _LEAKS_LOCK:
            try:
                _LEAKS.remove(buf)
            except ValueError:
                pass
            ALLOC_INUSE_BYTES.dec(nbytes)

# ---------- Flask app
app = Flask(__name__)
application = app  # mod_wsgi compatibility

@app.get("/hot")
def hot():
    start = time.time()
    # burn time (ms) per thread
    try:
        ms = max(0, int(request.args.get("cpu_ms", CPU_MS)))
    except Exception:
        ms = CPU_MS
    CPU_MS_USED.observe(ms)

    # fan-out threads (default 1). For “use the whole node”, pass parallel≈#cores (or higher).
    try:
        parallel = int(request.args.get("parallel", "1"))
    except Exception:
        parallel = 1
    parallel = max(1, min(parallel, (os.cpu_count() or 1) * 8))  # sane cap
    PARALLEL_USED.observe(parallel)

    # admission control: generate 503s when saturated
    if ACQUIRE_TIMEOUT_MS <= 0:
        acquired = _gate.acquire(blocking=False)  # instant 503 if busy
    else:
        acquired = _gate.acquire(timeout=ACQUIRE_TIMEOUT_MS / 1000.0)

    if not acquired:
        REQ_COUNTER.labels(status="503").inc()
        return ("busy\n", 503)

    INFLIGHT.inc()
    THREADS_CURRENT.set(threading.active_count())
    try:
        if parallel == 1:
            _busy_ms(ms)
        else:
            _burn_parallel(ms, parallel)

        REQ_HIST.observe(time.time() - start)
        REQ_COUNTER.labels(status="200").inc()
        return "ok\n", 200
    finally:
        INFLIGHT.dec()
        THREADS_CURRENT.set(threading.active_count())
        _gate.release()

@app.get("/mem")
def mem_allocate():
    """
    Allocate memory and hold it for `seconds` (default 60).
    Examples:
      /mem?mb=512&seconds=300
      /mem?mb=128           (holds for 60s)
    """
    try:
        mb = max(1, int(request.args.get("mb", "100")))
    except Exception:
        mb = 100
    try:
        seconds = int(request.args.get("seconds", "60"))
    except Exception:
        seconds = 60

    nbytes = mb * 1024 * 1024
    buf = bytearray(nbytes)  # touch pages
    for i in range(0, len(buf), 4096):
        buf[i] = 1

    with _LEAKS_LOCK:
        _LEAKS.append(buf)
        ALLOC_INUSE_BYTES.inc(nbytes)
        ALLOC_TOTAL_BYTES.inc(nbytes)

    t = threading.Thread(target=_release_after, args=(buf, nbytes, seconds), daemon=True)
    t.start()
    return f"allocated {mb}MiB for {seconds}s\n", 200

@app.get("/mem/release")
def mem_release_all():
    with _LEAKS_LOCK:
        released = 0
        for buf in _LEAKS:
            released += len(buf)
        _LEAKS.clear()
        # Best-effort: set gauge to zero (background timers will no-op on remove)
        ALLOC_INUSE_BYTES.set(0)
    return f"released {released} bytes\n", 200

@app.get("/metrics")
def metrics():
    return Response(generate_latest(REG), mimetype=CONTENT_TYPE_LATEST)

@app.get("/healthz")
def healthz():
    return "ok\n", 200

@app.get("/")
def root():
    return (
        "autoscale-probe endpoints:\n"
        "  /hot?cpu_ms=NN&parallel=M     -> CPU burn (PBKDF2)\n"
        "  /mem?mb=X&seconds=Y            -> hold X MiB for Y seconds (default 100MiB/60s)\n"
        "  /mem/release                   -> release all held memory\n"
        "  /metrics                       -> Prometheus exposition\n"
        "  /healthz                       -> liveness/readiness\n",
        200,
    )

# ---------- Kubernetes watchers (optional)
def _node_is_ready(n):
    for cond in (n.status.conditions or []):
        if cond.type == "Ready":
            return cond.status == "True"
    return False

def _node_ready_transition(n):
    for cond in (n.status.conditions or []):
        if cond.type == "Ready" and cond.status == "True" and cond.last_transition_time:
            return cond.last_transition_time
    return None

def _parse_quantity_cpu(q: str) -> float:
    """Convert '250m' or '2' into cores (float)."""
    if q is None:
        return 0.0
    s = str(q)
    if s.endswith("m"):
        return float(s[:-1]) / 1000.0
    return float(s)

def _parse_quantity_bytes(q: str) -> int:
    """Rough parser for K8s memory quantities like '128974848', '129Mi', '2Gi'."""
    if q is None:
        return 0
    s = str(q).strip()
    units = {
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "Pi": 1024**5,
        "Ei": 1024**6,
        "K":  1000,
        "M":  1000**2,
        "G":  1000**3,
        "T":  1000**4,
        "P":  1000**5,
        "E":  1000**6,
    }
    for u, mult in units.items():
        if s.endswith(u):
            return int(float(s[:-len(u)]) * mult)
    return int(float(s))

def watch_nodes():
    while True:
        try:
            try:
                config.load_incluster_config()
                print("[autoscale-probe] using in-cluster config", file=sys.stderr)
            except Exception:
                config.load_kube_config()
                print("[autoscale-probe] using local kubeconfig", file=sys.stderr)

            v1 = client.CoreV1Api()
            w = watch.Watch()

            # prime existing nodes
            for n in v1.list_node().items:
                name = n.metadata.name
                NODE_READY_GAUGE.labels(node=name).set(1 if _node_is_ready(n) else 0)
                NODE_UNSCHEDULABLE.labels(node=name).set(1 if bool(getattr(n.spec, "unschedulable", False)) else 0)

                # capacity/allocatable snapshots
                cap = n.status.capacity or {}
                alloc = n.status.allocatable or {}
                NODE_CAP_CPU_CORES.labels(node=name).set(_parse_quantity_cpu(cap.get("cpu")))
                NODE_ALLOC_CPU_CORES.labels(node=name).set(_parse_quantity_cpu(alloc.get("cpu")))
                NODE_CAP_MEM_BYTES.labels(node=name).set(_parse_quantity_bytes(cap.get("memory")))
                NODE_ALLOC_MEM_BYTES.labels(node=name).set(_parse_quantity_bytes(alloc.get("memory")))

                rt = _node_ready_transition(n)
                if rt:
                    d = (rt - n.metadata.creation_timestamp).total_seconds()
                    if d > 0:
                        NODE_READY_SECONDS.labels(node=name).set(d)

            # stream updates
            for ev in w.stream(v1.list_node, _request_timeout=300):
                n = ev["object"]
                name = n.metadata.name
                if _node_is_ready(n):
                    NODE_READY_GAUGE.labels(node=name).set(1)
                    rt = _node_ready_transition(n)
                    if rt:
                        d = (rt - n.metadata.creation_timestamp).total_seconds()
                        if d > 0:
                            NODE_READY_SECONDS.labels(node=name).set(d)
                else:
                    NODE_READY_GAUGE.labels(node=name).set(0)

                NODE_UNSCHEDULABLE.labels(node=name).set(1 if bool(getattr(n.spec, "unschedulable", False)) else 0)

                cap = n.status.capacity or {}
                alloc = n.status.allocatable or {}
                NODE_CAP_CPU_CORES.labels(node=name).set(_parse_quantity_cpu(cap.get("cpu")))
                NODE_ALLOC_CPU_CORES.labels(node=name).set(_parse_quantity_cpu(alloc.get("cpu")))
                NODE_CAP_MEM_BYTES.labels(node=name).set(_parse_quantity_bytes(cap.get("memory")))
                NODE_ALLOC_MEM_BYTES.labels(node=name).set(_parse_quantity_bytes(alloc.get("memory")))

        except Exception as e:
            print(f"[autoscale-probe] watch_nodes error: {e}", file=sys.stderr)
            time.sleep(2)

def watch_pods():
    pending_seen = {}
    while True:
        try:
            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()

            v1 = client.CoreV1Api()
            w = watch.Watch()
            selector = WATCH_SEL

            def _recount():
                # reset and recount (namespace + phase) among selected pods
                counts = {}
                pods = v1.list_namespaced_pod(WATCH_NS, label_selector=selector).items
                for p in pods:
                    ns = p.metadata.namespace or WATCH_NS
                    phase = p.status.phase or "Unknown"
                    counts[(ns, phase)] = counts.get((ns, phase), 0) + 1
                # export
                # First, zero everything we have previously set to avoid stale series
                # (Prometheus will handle series disappearance, but we keep it simple.)
                # We can't enumerate label sets from the client, so just set current ones.
                for (ns, phase), c in counts.items():
                    PODS_BY_PHASE.labels(namespace=ns, phase=phase).set(c)

            # prime
            now = datetime.now(timezone.utc)
            for p in v1.list_namespaced_pod(WATCH_NS, label_selector=selector).items:
                if p.status.phase == "Pending":
                    pending_seen[p.metadata.uid] = now
                if p.status.phase == "Running":
                    start = pending_seen.get(p.metadata.uid) or (p.status.start_time or now)
                    d = (now - start).total_seconds() if isinstance(start, datetime) else 0
                    POD_SCHEDULE_SECONDS.labels(pod=p.metadata.name, node=(p.spec.node_name or "unknown")).set(max(0, d))
            _recount()

            # stream
            for ev in w.stream(v1.list_namespaced_pod, WATCH_NS, label_selector=selector, _request_timeout=300):
                p = ev["object"]
                uid = p.metadata.uid
                phase = p.status.phase
                now = datetime.now(timezone.utc)
                if phase == "Pending" and uid not in pending_seen:
                    pending_seen[uid] = now
                if phase == "Running":
                    start = pending_seen.get(uid) or (p.status.start_time or now)
                    d = (now - start).total_seconds() if isinstance(start, datetime) else 0
                    POD_SCHEDULE_SECONDS.labels(pod=p.metadata.name, node=(p.spec.node_name or "unknown")).set(max(0, d))
                _recount()

        except Exception as e:
            print(f"[autoscale-probe] watch_pods error: {e}", file=sys.stderr)
            time.sleep(2)

def _start_watchers_once():
    if not HAS_K8S:
        print("[autoscale-probe] k8s watchers disabled: kubernetes client not importable", file=sys.stderr)
        return
    t1 = threading.Thread(target=watch_nodes, daemon=True)
    t2 = threading.Thread(target=watch_pods, daemon=True)
    t1.start(); t2.start()
    print("[autoscale-probe] k8s watchers started", file=sys.stderr)

if ENABLE_K8S and HAS_K8S:
    _start_watchers_once()
else:
    print(f"[autoscale-probe] watchers not started (ENABLE_K8S_WATCHERS={ENABLE_K8S}, HAS_K8S={HAS_K8S})", file=sys.stderr)

if __name__ == "__main__":
    # Flask dev server: threaded=True allows concurrent handlers;
    # PBKDF2 releases the GIL so threads will use multiple cores.
    app.run(host="0.0.0.0", port=8080, threaded=True)