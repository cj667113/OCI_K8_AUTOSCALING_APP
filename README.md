# OKE Autoscale Demo — Single Load Balancer (App + Grafana Sidecar)

Deploy a CPU-intensive demo app (**autoscale-probe**) alongside **Grafana** in the **same Pod** and expose both through **one** OCI Layer‑7 Load Balancer Service.

- **App:** `http://<LB_IP>/` (port **80** → container **8080**)
- **Health:** `http://<LB_IP>/healthz`
- **Grafana:** `http://<LB_IP>:3000` (default creds `admin` / `admin` — change in prod)

> The manifest includes: Metrics Server, Prometheus, HorizontalPodAutoscaler (HPA), and RBAC for enabling the OCI Cluster Autoscaler add‑on.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [What’s in the Manifest](#whats-in-the-manifest)
- [Prerequisites](#prerequisites)
- [Deploy](#deploy)
- [Grab the Load Balancer IP](#grab-the-load-balancer-ip)
- [Use the App and Grafana](#use-the-app-and-grafana)
- [Generate Load & Watch Autoscaling](#generate-load--watch-autoscaling)
- [Enable the OCI Cluster Autoscaler](#enable-the-oci-cluster-autoscaler)
- [Troubleshooting](#troubleshooting)
- [Cleanup](#cleanup)

---

## Overview

This setup is for **OKE** and demonstrates HPA-driven scaling of a CPU‑bound app while visualizing metrics in **Grafana**. A **single** `Service` of type `LoadBalancer` publishes two ports:

- **80 → app** container (`8080`)
- **3000 → Grafana** sidecar (`3000`)

All of this lives in a single `Deployment` (`autoscale-probe`) so the OCI LB can route to both containers via **Pod IPs**.

---

## Architecture

```
                        +-------------------------------+
                        |         OCI LB (L7)           |
                        |  Port 80  --->  / (App)       |
   Internet  ---------->|  Port 3000 --->  / (Grafana)  |
                        +-------------------------------+
                                   |
                          Service: autoscale-probe-lb
                                   |
                          +-------------------------+
                          |  Pod: autoscale-probe   |
                          |-------------------------|
                          |  Container: app         |  <- port 8080 (name: http)
                          |  Container: grafana     |  <- port 3000 (name: grafana)
                          +-------------------------+

Namespaces:
- autoscale   : app + grafana (sidecar) + LB Service + HPA + Grafana config
- monitoring  : Prometheus
- kube-system : metrics-server + Cluster Autoscaler (addon to be enabled in OKE)
```

---

## What’s in the Manifest

- **Namespace `autoscale`**
  - `Deployment autoscale-probe` (containers: **app** + **grafana**)
  - `Service autoscale-probe-lb` (type **LoadBalancer** with ports **80** and **3000**)
  - `Service autoscale-probe-metrics` (ClusterIP for scraping)
  - `HorizontalPodAutoscaler autoscale-probe-hpa` (60% CPU, 1→20 replicas)
  - Grafana provisioning `ConfigMap`s (`grafana-datasource`, `grafana-dashboard`, `grafana-dash-provider`)

- **Namespace `monitoring`**
  - `Deployment prometheus` + `Service prometheus` (ClusterIP)

- **Namespace `kube-system`**
  - `metrics-server` (Deployment + APIService `v1beta1.metrics.k8s.io`)
  - **Cluster Autoscaler RBAC** (enable the addon in OKE via OCI CLI)

---

## Prerequisites

- **OKE** cluster with the **OCI Cloud Controller Manager** (for LB)
- `kubectl` configured to point at your cluster
- (Optional) [`hey`](https://github.com/rakyll/hey) for load generation
- Basic egress access for image pulls (ghcr.io, grafana, prom, k8s images)

> **Security:** Grafana defaults to `admin/admin` in this demo. For production, inject credentials via a `Secret` and env, or use an IdP.

---

## Deploy

**Option A — Clone repo:**

```bash
git clone https://github.com/cj667113/OCI_K8_AUTOSCALING_APP.git
cd OCI_K8_AUTOSCALING_APP
kubectl apply -f oke-autoscale.yaml
```

**Option B — Local file:**

```bash
kubectl apply -f oke-autoscale.yaml
```

Check core components:

```bash
kubectl get nodes
kubectl get pods -n kube-system
kubectl get pods -n autoscale
```

---

## Grab the Load Balancer IP

```bash
kubectl -n autoscale get svc autoscale-probe-lb -w
```

Wait for **EXTERNAL-IP** to appear — that value is your `LB_IP`.

---

## Use the App and Grafana

```text
App (root)    : http://<LB_IP>/
Health        : http://<LB_IP>/healthz
Grafana (UI)  : http://<LB_IP>:3000  (admin / admin)
```

Prometheus is internal to the cluster:

```bash
kubectl -n monitoring get svc prometheus
```

---

## Generate Load & Watch Autoscaling

The HPA targets **60% CPU** with `minReplicas: 1`, `maxReplicas: 20`.

Generate sustained CPU load for 500s with concurrency 36:

```bash
hey -z 500s -c 36 -disable-keepalive "http://<LB_IP>/hot?cpu_ms=750&parallel=6"
```

Observe metrics and scaling:

```bash
watch -n 1 kubectl top pods -A
kubectl get hpa -n autoscale
kubectl get nodes -w
```

---

## Enable the OCI Cluster Autoscaler

Docs:  
https://docs.oracle.com/en-us/iaas/Content/ContEng/Tasks/contengconfiguringclusteraddons-configurationarguments.htm#contengconfiguringclusteraddons-configurationarguments_ClusterAutoscaler

OCI CLI example:

```bash
oci ce cluster update-addon   --addon-name ClusterAutoscaler   --from-json file://<path-to-config-file>   --cluster-id <cluster-ocid>

kubectl -n kube-system rollout restart deploy/cluster-autoscaler
```

Tail autoscaler logs:

```bash
kubectl -n kube-system logs -f deploy/cluster-autoscaler |   egrep -i "scale-down|unneeded|removing|utilization|NoScaleDown"
```

---

## Troubleshooting

**No EXTERNAL-IP on Service**
- `kubectl -n autoscale describe svc autoscale-probe-lb`
- Verify OCI CCM is running and LB quota/permissions are OK
- Ensure subnet/security lists allow inbound **80** and **3000**

**Grafana not loading**
- Confirm security lists / firewall for `<LB_IP>:3000`
- `kubectl -n autoscale logs deploy/autoscale-probe -c grafana`

**HPA shows unknown metrics**
- `kubectl get apiservices | grep metrics` → `v1beta1.metrics.k8s.io` must be **Available**
- `kubectl top pods -A` should return CPU/Memory (metrics-server healthy)
- `kubectl describe hpa -n autoscale autoscale-probe` for details

---

## Cleanup

```bash
kubectl delete -f oke-autoscale.yaml
```

---

### Handy Commands (copy/paste)

```bash
kubectl apply -f oke-autoscale.yaml
kubectl -n kube-system rollout restart deploy/cluster-autoscaler
kubectl get nodes
watch -n 1 kubectl top pods -A
kubectl get pods -n kube-system
kubectl get pods -n autoscale
kubectl top pods -A | head
kubectl -n kube-system logs -f deploy/cluster-autoscaler | egrep -i "scale-down|unneeded|removing|utilization|NoScaleDown"
kubectl get nodes -w

# Load test (replace LB_IP)
hey -z 500s -c 36 -disable-keepalive "http://<LB_IP>/hot?cpu_ms=750&parallel=6"
```


Enjoy! 🎉
