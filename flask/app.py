# app.py
import os, time, threading, sys, hashlib
from flask import Flask, Response, request
from prometheus_client import (
    CollectorRegistry, Counter, Histogram, Gauge,
    generate_latest, CONTENT_TYPE_LATEST,
    ProcessCollector, PlatformCollector, GCCollector,
)

# ---------- Config (env vars)
MAX_INFLIGHT = int(os.getenv("MAX_INFLIGHT", "200"))     # max concurrent /hot handlers
CPU_MS       = int(os.getenv("CPU_MS", "20"))            # default burn per request (ms)
PBKDF2_ITERS = int(os.getenv("PBKDF2_ITERS", "200000"))  # CPU intensity
WATCH_NS     = os.getenv("WATCH_NAMESPACE", "default")
ENABLE_K8S   = os.getenv("ENABLE_K8S_WATCHERS", "1") == "1"

# ---------- Optional Kubernetes client
HAS_K8S = True
try:
    from kubernetes import client, config, watch
except Exception as e:
    HAS_K8S = False
    print(f"[autoscale-probe] k8s client unavailable: {e}", file=sys.stderr)

# ---------- Metrics (custom registry)
REG = CollectorRegistry()
ProcessCollector(registry=REG)   # process_cpu_seconds_total, process_resident_memory_bytes, ...
PlatformCollector(registry=REG)  # python_info
GCCollector(registry=REG)        # python_gc_* totals

# Only “what was actually served”
REQS_SERVED = Counter(
    "loadgen_requests_served_total",
    "Number of /hot requests completed successfully (HTTP 200).",
    registry=REG,
)
REQ_HIST = Histogram(
    "loadgen_request_duration_seconds",
    "Duration of /hot requests (seconds).",
    registry=REG,
)
INFLIGHT = Gauge(
    "loadgen_inflight",
    "In-flight /hot requests (per-process).",
    registry=REG,
)
CPU_MS_USED = Histogram(
    "loadgen_cpu_ms",
    "Requested CPU burn time (ms) per request.",
    buckets=(1, 5, 10, 20, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 20000, 60000),
    registry=REG,
)
PARALLEL_USED = Histogram(
    "loadgen_parallel",
    "Requested worker threads per /hot.",
    buckets=(1, 2, 4, 8, 16, 32, 64, 128, 256),
    registry=REG,
)
THREADS_CURRENT = Gauge(
    "loadgen_threads_current",
    "Python threading.active_count().",
    registry=REG,
)
CFG_GAUGE = Gauge("loadgen_config", "Runtime config values.", ["key"], registry=REG)
CFG_GAUGE.labels(key="max_inflight").set(MAX_INFLIGHT)
CFG_GAUGE.labels(key="default_cpu_ms").set(CPU_MS)
CFG_GAUGE.labels(key="pbkdf2_iters").set(PBKDF2_ITERS)

# Node capacity snapshots (handy for dashboards)
NODE_READY_SECONDS = Gauge("k8s_node_ready_seconds", "Node create→Ready seconds", ["node"], registry=REG)
NODE_READY_GAUGE   = Gauge("k8s_node_ready_gauge", "Node Ready (1/0)", ["node"], registry=REG)
NODE_UNSCHEDULABLE = Gauge("k8s_node_unschedulable", "Node spec.unschedulable (1/0)", ["node"], registry=REG)
NODE_CAP_CPU_CORES   = Gauge("k8s_node_capacity_cpu_cores", "Node capacity.cpu (cores)", ["node"], registry=REG)
NODE_ALLOC_CPU_CORES = Gauge("k8s_node_allocatable_cpu_cores", "Node allocatable.cpu (cores)", ["node"], registry=REG)
NODE_CAP_MEM_BYTES   = Gauge("k8s_node_capacity_memory_bytes", "Node capacity.memory (bytes)", ["node"], registry=REG)
NODE_ALLOC_MEM_BYTES = Gauge("k8s_node_allocatable_memory_bytes", "Node allocatable.memory (bytes)", ["node"], registry=REG)

# ---------- CPU burner
_gate = threading.BoundedSemaphore(MAX_INFLIGHT)
_payload = b"x" * 64
_salt = b"salt"

def _busy_ms(ms: int):
    end = time.time() + (ms / 1000.0)
    while time.time() < end:
        hashlib.pbkdf2_hmac("sha256", _payload, _salt, PBKDF2_ITERS)

def _burn_parallel(ms: int, parallel: int):
    threads = []
    for _ in range(parallel):
        t = threading.Thread(target=_busy_ms, args=(ms,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

# ---------- Flask app
app = Flask(__name__)
application = app  # mod_wsgi compatibility

@app.get("/hot")
def hot():
    start = time.time()

    # per-request burn
    try:
        ms = max(0, int(request.args.get("cpu_ms", CPU_MS)))
    except Exception:
        ms = CPU_MS
    CPU_MS_USED.observe(ms)

    # fan out across threads
    try:
        parallel = int(request.args.get("parallel", "1"))
    except Exception:
        parallel = 1
    parallel = max(1, min(parallel, (os.cpu_count() or 1) * 8))
    PARALLEL_USED.observe(parallel)

    # Always block until a slot is free (no 503 path at all)
    _gate.acquire()
    INFLIGHT.inc()
    THREADS_CURRENT.set(threading.active_count())
    try:
        if parallel == 1:
            _busy_ms(ms)
        else:
            _burn_parallel(ms, parallel)
        REQ_HIST.observe(time.time() - start)
        REQS_SERVED.inc()
        return "ok\n", 200
    finally:
        INFLIGHT.dec()
        THREADS_CURRENT.set(threading.active_count())
        _gate.release()

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
        "  /hot?cpu_ms=NN&parallel=M   -> CPU burn (PBKDF2), blocks when saturated (no 503)\n"
        "  /metrics                    -> Prometheus exposition\n"
        "  /healthz                    -> liveness/readiness\n",
        200,
    )

# ---------- K8s node watcher (optional, with delete & reconcile to avoid ghost nodes)
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
    if q is None: return 0.0
    s = str(q)
    return float(s[:-1]) / 1000.0 if s.endswith("m") else float(s)

def _parse_quantity_bytes(q: str) -> int:
    if q is None: return 0
    s = str(q).strip()
    units = {"Ki":1024,"Mi":1024**2,"Gi":1024**3,"Ti":1024**4,"Pi":1024**5,"Ei":1024**6,
             "K":1000,"M":1000**2,"G":1000**3,"T":1000**4,"P":1000**5,"E":1000**6}
    for u, m in units.items():
        if s.endswith(u):
            return int(float(s[:-len(u)]) * m)
    return int(float(s))

_NODES_LOCK = threading.Lock()
KNOWN_NODES = set()

def _remove_node_series(name: str):
    """Delete all node-labelled time series for a node."""
    for g in (
        NODE_READY_SECONDS, NODE_READY_GAUGE, NODE_UNSCHEDULABLE,
        NODE_CAP_CPU_CORES, NODE_ALLOC_CPU_CORES,
        NODE_CAP_MEM_BYTES, NODE_ALLOC_MEM_BYTES,
    ):
        try:
            g.remove(name)  # labels=["node"]
        except KeyError:
            pass
    with _NODES_LOCK:
        KNOWN_NODES.discard(name)
    print(f"[autoscale-probe] removed metrics for node {name}", file=sys.stderr)

def _export_node(n):
    """Set gauges for a node object and mark it as known."""
    name = n.metadata.name
    NODE_READY_GAUGE.labels(node=name).set(1 if _node_is_ready(n) else 0)
    NODE_UNSCHEDULABLE.labels(node=name).set(1 if bool(getattr(n.spec, "unschedulable", False)) else 0)
    rt = _node_ready_transition(n)
    if rt:
        d = (rt - n.metadata.creation_timestamp).total_seconds()
        if d > 0:
            NODE_READY_SECONDS.labels(node=name).set(d)
    cap = n.status.capacity or {}
    alloc = n.status.allocatable or {}
    NODE_CAP_CPU_CORES.labels(node=name).set(_parse_quantity_cpu(cap.get("cpu")))
    NODE_ALLOC_CPU_CORES.labels(node=name).set(_parse_quantity_cpu(alloc.get("cpu")))
    NODE_CAP_MEM_BYTES.labels(node=name).set(_parse_quantity_bytes(cap.get("memory")))
    NODE_ALLOC_MEM_BYTES.labels(node=name).set(_parse_quantity_bytes(alloc.get("memory")))
    with _NODES_LOCK:
        KNOWN_NODES.add(name)

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

            # Prime from current list
            current = {n.metadata.name: n for n in v1.list_node().items}
            for n in current.values():
                _export_node(n)

            # Remove any stale label sets from previous runs
            with _NODES_LOCK:
                stale = KNOWN_NODES.difference(current.keys())
            for name in list(stale):
                _remove_node_series(name)

            last_reconcile = time.time()

            # Stream updates
            for ev in w.stream(v1.list_node, _request_timeout=300):
                et = ev.get("type")
                n = ev.get("object")
                if not n or not getattr(n, "metadata", None):
                    continue
                name = n.metadata.name

                if et == "DELETED":
                    _remove_node_series(name)
                    continue

                # ADDED / MODIFIED / BOOKMARK
                _export_node(n)

                # Periodic reconcile in case we missed deletes
                if time.time() - last_reconcile > 60:
                    listed = {nn.metadata.name for nn in v1.list_node().items}
                    with _NODES_LOCK:
                        stale_now = KNOWN_NODES.difference(listed)
                    for s in list(stale_now):
                        _remove_node_series(s)
                    last_reconcile = time.time()

        except Exception as e:
            print(f"[autoscale-probe] watch_nodes error: {e}", file=sys.stderr)
            time.sleep(2)

def _start_watchers_once():
    if not HAS_K8S:
        print("[autoscale-probe] k8s watcher disabled: client import failed", file=sys.stderr)
        return
    threading.Thread(target=watch_nodes, daemon=True).start()
    print("[autoscale-probe] k8s node watcher started", file=sys.stderr)

if ENABLE_K8S and HAS_K8S:
    _start_watchers_once()
else:
    print(f"[autoscale-probe] watcher not started (ENABLE_K8S_WATCHERS={ENABLE_K8S}, HAS_K8S={HAS_K8S})", file=sys.stderr)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)