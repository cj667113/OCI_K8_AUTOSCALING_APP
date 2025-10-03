# app.py
import os, time, threading, sys, hashlib
from flask import Flask, Response, request
from prometheus_client import (
    CollectorRegistry, Gauge,
    generate_latest, CONTENT_TYPE_LATEST,
    ProcessCollector, PlatformCollector, GCCollector,
)

# ---------- Config (env vars)
MAX_INFLIGHT = int(os.getenv("MAX_INFLIGHT", "200"))     # cap concurrent /burn handlers
CPU_MS       = int(os.getenv("CPU_MS", "20"))            # default burn per request (ms)
PBKDF2_ITERS = int(os.getenv("PBKDF2_ITERS", "200000"))  # PBKDF2 iterations
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

# Node gauges used by the dashboard
NODE_READY_GAUGE        = Gauge("k8s_node_ready_gauge", "Node Ready (1/0)", ["node"], registry=REG)
NODE_CAP_CPU_CORES      = Gauge("k8s_node_capacity_cpu_cores", "Node capacity.cpu (cores)", ["node"], registry=REG)
NODE_ALLOC_CPU_CORES    = Gauge("k8s_node_allocatable_cpu_cores", "Node allocatable.cpu (cores)", ["node"], registry=REG)
NODE_CAP_MEM_BYTES      = Gauge("k8s_node_capacity_memory_bytes", "Node capacity.memory (bytes)", ["node"], registry=REG)
NODE_ALLOC_MEM_BYTES    = Gauge("k8s_node_allocatable_memory_bytes", "Node allocatable.memory (bytes)", ["node"], registry=REG)

# ---------- CPU burner (for generating load)
_gate = threading.BoundedSemaphore(MAX_INFLIGHT)
_payload = b"x" * 64
_salt = b"salt"

def _busy_ms(ms: int):
    end = time.time() + (ms / 1000.0)
    while time.time() < end:
        hashlib.pbkdf2_hmac("sha256", _payload, _salt, PBKDF2_ITERS)

# ---------- Flask app
app = Flask(__name__)
application = app  # mod_wsgi compatibility

@app.get("/burn")
def burn():
    # simple CPU burner; no request metrics exported
    try:
        ms = max(0, int(request.args.get("cpu_ms", CPU_MS)))
    except Exception:
        ms = CPU_MS

    _gate.acquire()
    try:
        _busy_ms(ms)
        return "ok\n", 200
    finally:
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
        "  /burn?cpu_ms=NN   -> CPU burn (PBKDF2), blocks when saturated\n"
        "  /metrics          -> Prometheus exposition\n"
        "  /healthz          -> liveness/readiness\n",
        200,
    )

# ---------- Helpers for K8s node quantities
def _node_is_ready(n) -> bool:
    for cond in (n.status.conditions or []):
        if cond.type == "Ready":
            return cond.status == "True"
    return False

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
    for g in (
        NODE_READY_GAUGE,
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
    name = n.metadata.name
    NODE_READY_GAUGE.labels(node=name).set(1 if _node_is_ready(n) else 0)
    cap   = n.status.capacity or {}
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

            # Prime
            current = {n.metadata.name: n for n in v1.list_node().items}
            for n in current.values():
                _export_node(n)
            with _NODES_LOCK:
                stale = KNOWN_NODES.difference(current.keys())
            for name in list(stale):
                _remove_node_series(name)

            last_reconcile = time.time()

            # Stream
            for ev in w.stream(v1.list_node, _request_timeout=300):
                et = ev.get("type")
                n  = ev.get("object")
                if not n or not getattr(n, "metadata", None):
                    continue
                name = n.metadata.name

                if et == "DELETED":
                    _remove_node_series(name)
                    continue

                _export_node(n)

                # periodic reconcile to catch missed deletes
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