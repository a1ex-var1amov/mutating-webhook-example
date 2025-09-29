#!/usr/bin/env bash
set -euo pipefail

# Script: generate-certs.sh
# Purpose: Generate a self-signed CA and server cert for the webhook Service,
#          create/update the TLS Secret, and patch the MutatingWebhookConfiguration caBundle.
#
# Defaults:
#   NAMESPACE=nfs-home-system
#   SERVICE=nfs-home-webhook
#   SECRET_NAME=webhook-server-cert
#   CERTS_DIR=certs
#
# Usage:
#   ./scripts/generate-certs.sh
#
# With overrides:
#   NAMESPACE=my-ns SERVICE=my-svc SECRET_NAME=my-tls CERTS_DIR=out \
#     ./scripts/generate-certs.sh
#
# Example (run from repo root):
#   NAMESPACE=nfs-home-system SERVICE=nfs-home-webhook SECRET_NAME=webhook-server-cert \
#     ./scripts/generate-certs.sh
#
# Help:
#   ./scripts/generate-certs.sh -h

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'USAGE'
generate-certs.sh - generate self-signed TLS certs and patch webhook caBundle

Defaults:
  NAMESPACE=nfs-home-system
  SERVICE=nfs-home-webhook
  SECRET_NAME=webhook-server-cert
  CERTS_DIR=certs

Usage:
  ./scripts/generate-certs.sh

With overrides:
  NAMESPACE=my-ns SERVICE=my-svc SECRET_NAME=my-tls CERTS_DIR=out \
    ./scripts/generate-certs.sh

Example:
  NAMESPACE=nfs-home-system SERVICE=nfs-home-webhook SECRET_NAME=webhook-server-cert \
    ./scripts/generate-certs.sh
USAGE
  exit 0
fi

NAMESPACE=${NAMESPACE:-nfs-home-system}
SERVICE=${SERVICE:-nfs-home-webhook}
SECRET_NAME=${SECRET_NAME:-webhook-server-cert}
TMPDIR=$(mktemp -d)
CERTS_DIR=${CERTS_DIR:-certs}
mkdir -p "$CERTS_DIR"

cat > "$TMPDIR/openssl.cnf" <<EOF
[ req ]
default_bits       = 2048
distinguished_name = req_distinguished_name
req_extensions     = v3_req

[ req_distinguished_name ]
CN = ${SERVICE}.${NAMESPACE}.svc

[ v3_req ]
subjectAltName = @alt_names

[ alt_names ]
DNS.1 = ${SERVICE}
DNS.2 = ${SERVICE}.${NAMESPACE}
DNS.3 = ${SERVICE}.${NAMESPACE}.svc
DNS.4 = ${SERVICE}.${NAMESPACE}.svc.cluster.local
EOF

# Generate CA
openssl genrsa -out "$CERTS_DIR/ca.key" 2048 2>/dev/null
openssl req -x509 -new -nodes -key "$CERTS_DIR/ca.key" -subj "/CN=${SERVICE}-ca" -days 3650 -out "$CERTS_DIR/ca.crt" 2>/dev/null

# Generate server key and CSR
openssl genrsa -out "$CERTS_DIR/server.key" 2048 2>/dev/null
openssl req -new -key "$CERTS_DIR/server.key" -subj "/CN=${SERVICE}.${NAMESPACE}.svc" -out "$TMPDIR/server.csr" -config "$TMPDIR/openssl.cnf" 2>/dev/null

# Sign the CSR with our CA
openssl x509 -req -in "$TMPDIR/server.csr" -CA "$CERTS_DIR/ca.crt" -CAkey "$CERTS_DIR/ca.key" -CAcreateserial -out "$CERTS_DIR/server.crt" -days 3650 -extensions v3_req -extfile "$TMPDIR/openssl.cnf" 2>/dev/null

# Create namespace if missing
kubectl get ns "$NAMESPACE" >/dev/null 2>&1 || kubectl create ns "$NAMESPACE"

# Create or update the TLS secret
kubectl -n "$NAMESPACE" delete secret "$SECRET_NAME" >/dev/null 2>&1 || true
kubectl -n "$NAMESPACE" create secret tls "$SECRET_NAME" \
  --cert="$CERTS_DIR/server.crt" --key="$CERTS_DIR/server.key"

# Patch the webhook caBundle
CABUNDLE=$(base64 < "$CERTS_DIR/ca.crt" | tr -d '\n')

kubectl get mutatingwebhookconfiguration nfs-home-webhook >/dev/null 2>&1 || {
  echo "MutatingWebhookConfiguration nfs-home-webhook not found. Apply k8s/webhook.yaml first." >&2
  exit 1
}

kubectl patch mutatingwebhookconfiguration nfs-home-webhook \
  --type=json \
  -p "[ {\"op\": \"replace\", \"path\": \"/webhooks/0/clientConfig/caBundle\", \"value\": \"${CABUNDLE}\" } ]"

echo "Certificates generated in ${CERTS_DIR}/ and webhook caBundle updated."
