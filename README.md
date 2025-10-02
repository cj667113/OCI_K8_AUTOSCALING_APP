# OCI OKE Autoscale Demo — Two Load Balancers (App + Grafana)

Deploy a CPU‑intensive demo app (**autoscale-probe**) and a separate **Grafana** deployment.  
Each component is exposed through its **own** OCI Layer‑7 Load Balancer **on port 80**.

- **App:** `http://<APP_LB_IP>/` (port **80** → container **8080**)  
- **Health:** `http://<APP_LB_IP>/healthz`  
- **Grafana:** `http://<GRAFANA_LB_IP>/` (port **80** → container **3000**, default creds `admin` / `admin` — change in prod)

> The manifest includes: Metrics Server, Prometheus, HorizontalPodAutoscaler (HPA), and RBAC for enabling the OCI Cluster Autoscaler add‑on.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [What’s in the Manifest](#whats-in-the-manifest)
- [Prerequisites](#prerequisites)
- [Deploy](#deploy)
- [Grab the Load Balancer IPs](#grab-the-load-balancer-ips)
- [Use the App and Grafana](#use-the-app-and-grafana)
- [Sample Grafana Dashboard](#sample-grafana-dashboard)
- [Generate Load & Watch Autoscaling](#generate-load--watch-autoscaling)
- [Enable the OCI Cluster Autoscaler](#enable-the-oci-cluster-autoscaler)
- [Troubleshooting](#troubleshooting)
- [Cleanup](#cleanup)

---

## Overview

This setup targets **OKE** and demonstrates HPA‑driven scaling of a CPU‑bound app while visualizing metrics in **Grafana**.  
Unlike the combined‑pod approach, **Grafana runs in its own Deployment** and is published by a **separate** `Service` of type `LoadBalancer`.

---

## Architecture

```
                      +-----------------------------+         +-----------------------------+
                      |   OCI LB (App)              |         |   OCI LB (Grafana)          |
Internet  ----------->|   Port 80 ---> / (App)      |         |   Port 80 ---> / (Grafana)  |
                      +-----------------------------+         +-----------------------------+
                                 |                                        |
                       Service: autoscale-probe-lb             Service: grafana-lb
                                 |                                        |
                      +-------------------------+                 +-------------------------+
                      |  Pod: autoscale-probe   |                 |  Pod: grafana           |
                      |  Container: app (8080)  |                 |  Container: grafana     |
                      +-------------------------+                 +-------------------------+

Namespaces:
- autoscale   : app + grafana + LB Services + HPA + Grafana config
- monitoring  : Prometheus
- kube-system : metrics-server + Cluster Autoscaler (addon to be enabled in OKE)
```

---

## What’s in the Manifest

- **Namespace `autoscale`**
  - `Deployment autoscale-probe` (container: **app**)
  - `Service autoscale-probe-lb` (type **LoadBalancer**, port **80** → app)
  - `Service autoscale-probe-metrics` (ClusterIP for scraping)
  - `HorizontalPodAutoscaler autoscale-probe-hpa` (60% CPU, 1→20 replicas)
  - `Deployment grafana` (separate Pod)
  - `Service grafana-lb` (type **LoadBalancer**, port **80** → Grafana UI)
  - Grafana provisioning `ConfigMap`s (`grafana-datasource`, `grafana-dashboard`, `grafana-dash-provider`)

- **Namespace `monitoring`**
  - `Deployment prometheus` + `Service prometheus` (ClusterIP)

- **Namespace `kube-system`**
  - `metrics-server` (Deployment + APIService `v1beta1.metrics.k8s.io`)
  - **Cluster Autoscaler RBAC** (enable the addon in OKE via OCI CLI)

---

## Prerequisites

- **OKE** cluster with the **OCI Cloud Controller Manager** (for LBs)
- `kubectl` configured to point at your cluster
- (Optional) [`hey`](https://github.com/rakyll/hey) for load generation
- Basic egress access for image pulls (ghcr.io, grafana, prometheus, k8s images)

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

## Grab the Load Balancer IPs

```bash
kubectl -n autoscale get svc autoscale-probe-lb -w
kubectl -n autoscale get svc grafana-lb -w
```

Wait for **EXTERNAL-IP** to appear for each Service — those are your `APP_LB_IP` and `GRAFANA_LB_IP`.

---

## Use the App and Grafana

```text
App (root)    : http://<APP_LB_IP>/
Health        : http://<APP_LB_IP>/healthz
Grafana (UI)  : http://<GRAFANA_LB_IP>/  (admin / admin)
```

Prometheus is internal to the cluster:

```bash
kubectl -n monitoring get svc prometheus
```

---

## Sample Grafana Dashboard

<p align="center">
  <a href="images/sample.png">
    <img src="images/sample.png" alt="Grafana dashboard showing OKE autoscaling metrics" width="100%">
  </a>
</p>

<sub><em>Grafana dashboard included via ConfigMaps (see <code>grafana-dashboard</code> and <code>grafana-datasource</code> in the manifest).</em></sub>

---

## Generate Load & Watch Autoscaling

The HPA targets **60% CPU** with `minReplicas: 1`, `maxReplicas: 20`.

Generate sustained CPU load for 500s with concurrency 36:

```bash
hey -z 500s -c 36 -disable-keepalive "http://<APP_LB_IP>/hot?cpu_ms=750&parallel=6"
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
kubectl -n kube-system logs -f deploy/cluster-autoscaler | egrep -i "scale-down|unneeded|removing|utilization|NoScaleDown"
```

---

## Troubleshooting

**No EXTERNAL-IP on Service**
- `kubectl -n autoscale describe svc autoscale-probe-lb`
- `kubectl -n autoscale describe svc grafana-lb`
- Verify OCI CCM is running and LB quota/permissions are OK
- Ensure subnet/security lists allow inbound **80**

**Grafana not loading**
- Confirm security lists / firewall for `<GRAFANA_LB_IP>:80`
- `kubectl -n autoscale logs deploy/grafana`

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

# Load test (replace APP_LB_IP)
hey -z 500s -c 36 -disable-keepalive "http://<APP_LB_IP>/hot?cpu_ms=750&parallel=6"
```

---

Enjoy! 🎉
