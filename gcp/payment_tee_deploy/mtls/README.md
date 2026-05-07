# mTLS for sthrip-payment-tee

The Railway proxy and the GCP Confidential VM payment service authenticate
each other via mutual TLS. This directory provides the script that mints
the CA, server cert, and client cert.

## Files

```
generate-certs.sh   — bash script that produces ca.crt, server.{crt,key},
                      client.{crt,key} via openssl.
certs/              — generated artefacts (NOT checked in; .gitignored).
```

## Generating certs

Run on a SECURE host (your laptop). Never run this in CI — the CA private
key must not leak.

```bash
cd gcp/payment_tee_deploy/mtls
bash generate-certs.sh
```

Optional environment overrides:

| Variable        | Default                        | Notes                          |
|-----------------|--------------------------------|--------------------------------|
| `CERT_OUT_DIR`  | `./certs`                      | Output directory               |
| `CA_CN`         | `Sthrip Payment-TEE Root CA`   | CA Common Name                 |
| `SERVER_CN`     | `sthrip-payment-tee`           | Server CN — must match VM name |
| `CLIENT_CN`     | `sthrip-railway-proxy`         | Client CN                      |
| `DAYS_CA`       | `3650`                         | CA validity in days (10y)      |
| `DAYS_LEAF`     | `365`                          | Server/client validity (1y)    |
| `KEY_BITS`      | `4096`                         | RSA key size                   |

## Distribution

After generation, distribute the files as follows:

| File                    | Where it goes                          |
|-------------------------|----------------------------------------|
| `ca.crt`                | Both Railway secret + GCP Secret Mgr   |
| `server.crt` + `.key`   | GCP Secret Manager (mounted on VM)     |
| `client.crt` + `.key`   | Railway secret (mounted in proxy env)  |
| `ca.key`                | OFFLINE encrypted backup ONLY          |

## Loading on the GCP VM (Sprint 6)

Sprint 6 adds the Cloud Init / startup-script lines that fetch
`server.crt` and `server.key` from Secret Manager and mount them into the
container at `/etc/sthrip/mtls/`. The TEE service uses `cryptography` to
load and present them to incoming TLS connections.

## Loading on Railway (Sprint 6)

Sprint 6 adds the Railway proxy code that reads `client.crt`, `client.key`,
and `ca.crt` from Railway secrets and presents the client cert when calling
out to the GCP static IP.

## Rotation

mTLS leaf certs expire after `DAYS_LEAF` (default 365 days). Rotate at
least 30 days before expiry:

1. On the secure host, re-run `generate-certs.sh` (the existing `ca.crt` is
   re-used because `DAYS_CA` is 10 years; you only need to regenerate
   server/client leaf certs by deleting `server.*` and `client.*` first).
2. Upload the new certs to GCP Secret Manager and Railway secrets with a
   new version label.
3. Roll the GCP VM and Railway proxy in lock-step.
4. Once both sides are on the new certs, mark the old version stale.

## Security notes

* The CA private key (`ca.key`) NEVER goes online. Store offline (e.g.
  encrypted on a hardware token).
* Re-issuing the CA forces every client to update — avoid by keeping
  `DAYS_CA` long.
* The script intentionally does NOT add the certs to git. The directory's
  `.gitignore` excludes `certs/`.
