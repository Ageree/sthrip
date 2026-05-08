#!/usr/bin/env bash
#
# generate-certs.sh — produce a CA, server cert, and client cert for the
# mTLS link between Railway proxy and the GCP Confidential VM payment
# service.
#
# Output (in $CERT_OUT_DIR, default ./certs):
#   ca.crt        — root CA self-signed certificate (10y validity)
#   ca.key        — root CA private key (KEEP SECRET)
#   server.crt    — server cert for the GCP VM, signed by ca.crt
#   server.key    — server private key (loaded into the TEE VM)
#   client.crt    — client cert for the Railway proxy, signed by ca.crt
#   client.key    — client private key (loaded into Railway secrets)
#
# Run this in a SECURE environment (your laptop, NOT a CI runner). The
# generated keys never need to leave their respective hosts after upload.
#
# Re-run annually for rotation. The TEE VM and Railway secrets must be
# updated in lock-step (Sprint 6 documents the rotation runbook).
#
set -euo pipefail

CERT_OUT_DIR="${CERT_OUT_DIR:-$(pwd)/certs}"
mkdir -p "${CERT_OUT_DIR}"
chmod 700 "${CERT_OUT_DIR}"

CA_CN="${CA_CN:-Sthrip Payment-TEE Root CA}"
SERVER_CN="${SERVER_CN:-sthrip-payment-tee}"
CLIENT_CN="${CLIENT_CN:-sthrip-railway-proxy}"
DAYS_CA="${DAYS_CA:-3650}"
DAYS_LEAF="${DAYS_LEAF:-365}"
KEY_BITS="${KEY_BITS:-4096}"

cd "${CERT_OUT_DIR}"

echo "==> Generating ${KEY_BITS}-bit RSA root CA (${DAYS_CA} days)..."
openssl genrsa -out ca.key "${KEY_BITS}" 2>/dev/null
openssl req -x509 -new -nodes \
    -key ca.key \
    -days "${DAYS_CA}" \
    -sha256 \
    -subj "/CN=${CA_CN}" \
    -out ca.crt

echo "==> Generating server cert for ${SERVER_CN}..."
openssl genrsa -out server.key "${KEY_BITS}" 2>/dev/null
openssl req -new \
    -key server.key \
    -subj "/CN=${SERVER_CN}" \
    -out server.csr
cat >server.ext <<EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:${SERVER_CN},DNS:localhost,IP:127.0.0.1
EOF
openssl x509 -req \
    -in server.csr \
    -CA ca.crt -CAkey ca.key -CAcreateserial \
    -days "${DAYS_LEAF}" \
    -sha256 \
    -extfile server.ext \
    -out server.crt 2>/dev/null

echo "==> Generating client cert for ${CLIENT_CN}..."
openssl genrsa -out client.key "${KEY_BITS}" 2>/dev/null
openssl req -new \
    -key client.key \
    -subj "/CN=${CLIENT_CN}" \
    -out client.csr
cat >client.ext <<EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
EOF
openssl x509 -req \
    -in client.csr \
    -CA ca.crt -CAkey ca.key -CAcreateserial \
    -days "${DAYS_LEAF}" \
    -sha256 \
    -extfile client.ext \
    -out client.crt 2>/dev/null

# Permissions: keys 600, certs 644.
chmod 600 ca.key server.key client.key
chmod 644 ca.crt server.crt client.crt

# Cleanup transient artefacts.
rm -f server.csr client.csr server.ext client.ext ca.srl

echo
echo "==> Done. Files in ${CERT_OUT_DIR}:"
ls -la "${CERT_OUT_DIR}"
echo
echo "Next steps:"
echo "  1. Upload server.crt + server.key to the GCP VM Secret Manager."
echo "  2. Upload client.crt + client.key + ca.crt to Railway as secrets."
echo "  3. Store ca.key OFFLINE (e.g. encrypted backup) — used only to"
echo "     re-sign certs during annual rotation."
