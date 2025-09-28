# nfs-home mutating webhook

This webhook rewrites container volume mount paths from `/home` to `/blah/home/` for Pods and Deployments labeled `nfs-home=true`.

## Build

```bash
docker build -t nfs-home-webhook:latest .
```

Push to your registry or load into your cluster as needed.

## Deploy (Kubernetes)

1) Apply namespace, service, and webhook configuration:

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/webhook.yaml
```

2) Generate TLS certs, create the secret, and patch `caBundle`:

```bash
chmod +x scripts/generate-certs.sh
scripts/generate-certs.sh
```

3) Deploy the webhook Deployment (now that the secret exists):

```bash
kubectl apply -f k8s/deployment.yaml
```

4) Verify

Create a Pod or Deployment with label `nfs-home=true` and a mountPath under `/home`. The webhook will rewrite it to `/blah/home/...`.

## Deploy (OpenShift)

On OpenShift, serving certs and the webhook `caBundle` are injected automatically.

```bash
# Log in and select/create the namespace
oc login https://api.example.openshift.com:6443 --username=<user> --password=<pass>

oc apply -f k8s/namespace.yaml
oc apply -f k8s/serviceaccount.yaml
oc apply -f k8s/service.yaml     # creates secret: webhook-server-cert
oc apply -f k8s/webhook.yaml     # auto-injects caBundle

# Build the image using BuildConfig from this repo (Dockerfile)
oc apply -f k8s/ocp-build.yaml
oc start-build nfs-home-webhook --from-dir=. -n nfs-home-system --wait --follow

# Deploy using the internal registry image reference (no edits needed)
oc apply -f k8s/deployment-ocp.yaml
```

## Configuration

Environment variables supported by the webhook:

- TARGET_LABEL_KEY: default `nfs-home`
- TARGET_LABEL_VALUE: default `true`
- REWRITE_FROM: default `/home`
- REWRITE_TO: default `/blah/home`
- LOG_LEVEL: default `INFO` (one of `DEBUG, INFO, WARNING, ERROR, CRITICAL`)
- DEBUG_ADMISSION: default `false` (set to `true` to log AdmissionReview bodies)
- DEBUG_PATCHES: default `false` (set to `true` to log generated JSONPatch ops)

## Notes

- The webhook is limited by `objectSelector` to only resources with `nfs-home=true`.
- Failure policy is Fail; adjust in `k8s/webhook.yaml` if needed.
- The TLS secret is `webhook-server-cert` in namespace `nfs-home-system`, mounted at `/tls`.
