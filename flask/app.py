# app.py
import os, time, threading, sys, hashlib
from datetime import datetime, timezone
from flask import Flask, Response, request
from prometheus_client import (
    CollectorRegistry, Counter, Histogram, Gauge,
    generate_latest, CONTENT_TYPE_LATEST
)

# ----- Optional Kubernetes client (app still runs without it)
HAS_K8S = True
try:
    from kubernetes import client, config, watch
except Exception as e:
    HAS_K8S = False
    print(f"[autoscale-probe] k8s client unavailable: {e}", file=sys.stderr)

# -------------------------
# Config (env vars)
# -------------------------
MAX_INFLIGHT       = int(os.getenv("MAX_INFLIGHT", "200"))     # concurrent requests before 503
ACQUIRE_TIMEOUT_MS = int(os.getenv("ACQUIRE_TIMEOUT_MS", "0")) # 0=immediate 503; >0=wait then 503
CPU_MS             = int(os.getenv("CPU_MS", "20"))            # default per-request burn (ms)
PBKDF2_ITERS       = int(os.getenv("PBKDF2_ITERS", "200000"))  # PBKDF2 iterations (CPU intensity)
WATCH_NS           = os.getenv("WATCH_NAMESPACE", "default")
WATCH_SEL          = os.getenv("WATCH_SELECTOR",  "app=stress-app")
ENABLE_K8S         = os.getenv("ENABLE_K8S_WATCHERS", "1") == "1"

# -------------------------
# Prometheus metrics
# -------------------------
REG = CollectorRegistry()
REQ_COUNTER = Counter("loadgen_requests_total", "Total /hot requests by status",
                      ["status"], registry=REG)
REQ_HIST    = Histogram("loadgen_request_duration_seconds",
                        "Duration of /hot requests (seconds)", registry=REG)
NODE_READY_SECONDS = Gauge("k8s_node_ready_seconds",
                           "Seconds from Node creationTimestamp to first Ready=True",
                           ["node"], registry=REG)
NODE_READY_GAUGE   = Gauge("k8s_node_ready_gauge",
                           "Node Ready flag (1=Ready, 0=NotReady)",
                           ["node"], registry=REG)
POD_SCHEDULE_SECONDS = Gauge("k8s_pod_schedule_seconds",
                             "Seconds from Pod Pending to first Running",
                             ["pod","node"], registry=REG)
INFLIGHT = Gauge("loadgen_inflight", "In-flight requests (per-process)", registry=REG)

# -------------------------
# Concurrency gate and CPU burn
# -------------------------
_gate = threading.BoundedSemaphore(MAX_INFLIGHT)

_payload = b"x" * 64
_salt = b"salt"  # fixed salt; we only care about CPU work, not cryptographic properties

def _busy_ms(ms: int):
    """Burn CPU for approximately `ms` using PBKDF2 (C code -> releases the GIL)."""
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

# -------------------------
# Flask app
# -------------------------
app = Flask(__name__)
application = app  # for mod_wsgi compatibility (if ever used)

@app.get("/hot")
def hot():
    start = time.time()

    # burn time (ms) per thread
    try:
        ms = max(0, int(request.args.get("cpu_ms", CPU_MS)))
    except Exception:
        ms = CPU_MS

    # fan-out threads (default 1). For “use the whole node”, pass parallel≈#cores (or higher).
    try:
        parallel = int(request.args.get("parallel", "1"))
    except Exception:
        parallel = 1
    parallel = max(1, min(parallel, (os.cpu_count() or 1) * 8))  # sane cap

    # admission control: generate 503s when saturated
    if ACQUIRE_TIMEOUT_MS <= 0:
        acquired = _gate.acquire(blocking=False)  # instant 503 if busy
    else:
        acquired = _gate.acquire(timeout=ACQUIRE_TIMEOUT_MS / 1000.0)

    if not acquired:
        REQ_COUNTER.labels(status="503").inc()
        return ("busy\n", 503)

    INFLIGHT.inc()
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
        _gate.release()

@app.get("/metrics")
def metrics():
    return Response(generate_latest(REG), mimetype=CONTENT_TYPE_LATEST)

@app.get("/healthz")
def healthz():
    return "ok\n", 200

@app.get("/")
def root():
    return "autoscale-probe: /hot?cpu_ms=NN&parallel=M, /metrics, /healthz\n", 200

# -------------------------
# Kubernetes watchers (optional)
# -------------------------
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

            # prime
            now = datetime.now(timezone.utc)
            for p in v1.list_namespaced_pod(WATCH_NS, label_selector=selector).items:
                if p.status.phase == "Pending":
                    pending_seen[p.metadata.uid] = now
                if p.status.phase == "Running":
                    start = pending_seen.get(p.metadata.uid) or (p.status.start_time or now)
                    d = (now - start).total_seconds() if isinstance(start, datetime) else 0
                    POD_SCHEDULE_SECONDS.labels(pod=p.metadata.name, node=(p.spec.node_name or "unknown")).set(max(0, d))

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
    # Flask dev server is fine here; threaded=True allows concurrent handlers,
    # PBKDF2 releases the GIL so threads will use multiple cores.
    app.run(host="0.0.0.0", port=8080, threaded=True)
