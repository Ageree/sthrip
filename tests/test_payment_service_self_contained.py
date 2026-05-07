"""Sprint 5 — Self-contained payment-service deploy artefact tests.

These tests verify that ``gcp/payment-tee-deploy/`` produces a deployable
TEE payment service that:

* Imports ONLY payment-related modules (no marketplace / escrow / MCP / SLA /
  reviews / channels / swaps / stablecoins / Tor sidecar code). Verified via
  ``sys.modules`` snapshot diff and the runtime import_guard.
* Exposes ``/health``, ``/attestation``, and ``/v2/payments/hub-routing``.
* Ships a multi-stage non-root Dockerfile that exposes 8080.
* Provides an idempotent ``setup-vm.sh`` with ``--dry-run`` support that emits
  ``gcloud compute instances create`` with ``--confidential-compute-type=SEV_SNP``.
* Generates a CA + server cert + client cert via ``mtls/generate-certs.sh``.

No live ``gcloud`` calls. No real GCP resources. No Docker daemon required.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
GCP_DIR = REPO_ROOT / "gcp" / "payment_tee_deploy"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Modules whose import inside the TEE payment service would prove a leak of
# non-payment functionality across the trust boundary. We scope these to
# *project* modules — third-party packages whose names happen to share these
# substrings (e.g. ``sqlalchemy.dialects.postgresql.operators`` contains the
# legacy ``tor`` substring) are ignored.
FORBIDDEN_SUBSTRINGS = (
    "marketplace",
    "admin_ui",
    "integrations.sthrip_mcp",
    "tor_sidecar",
    "subscription_billing_service",
    "matchmaking_service",
    "messaging_service",
    "review_service",
    "sla_service",
    "channel_service",
    "swap_service",
    "stablecoin",
    "escrow_service",
    "api.routers.escrow",
    "api.routers.marketplace",
    "api.routers.reviews",
    "api.routers.sla",
    "api.routers.channels",
    "api.routers.swaps",
    "api.routers.stablecoin",
    "api.admin_ui",
)

# Mirrors gcp.payment_tee_deploy.import_guard.ALLOWED_OVERRIDES — see that
# module for rationale (sthrip/__init__.py eagerly loads several sibling
# subpackages we cannot disable without restructuring the package).
ALLOWED_OVERRIDES = (
    "sthrip.db.escrow_repo",
    "sthrip.db.milestone_repo",
    "sthrip.db.channel_repo",
    "sthrip.db.matchmaking_repo",
    "sthrip.db.multisig_repo",
    "sthrip.db.review_repo",
    "sthrip.db.sla_repo",
    "sthrip.db.swap_repo",
    "sthrip.db.treasury_repo",
    "sthrip.swaps",
    "sthrip.swaps.btc",
    "sthrip.swaps.btc.htlc",
    "sthrip.swaps.btc.rpc_client",
    "sthrip.swaps.btc.watcher",
    "sthrip.swaps.xmr",
    "sthrip.swaps.xmr.multisig",
    "sthrip.swaps.xmr.wallet",
    "sthrip.escrow",
    "sthrip.channels",
)

PROJECT_PACKAGE_PREFIXES = (
    "sthrip.",
    "sthrip_sdk.",
    "api.",
    "cli.",
    "integrations.",
    "examples.",
    "scripts.",
)


def _is_project_module(name: str) -> bool:
    lower = name.lower()
    return any(lower.startswith(p) for p in PROJECT_PACKAGE_PREFIXES)


def _run(cmd: list[str], cwd: Path | None = None, env: dict | None = None,
         input_str: str | None = None) -> subprocess.CompletedProcess:
    """Run a subprocess and return the CompletedProcess. Never raises."""
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env if env is not None else os.environ.copy(),
        input=input_str,
        capture_output=True,
        text=True,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# Test 1 — payment service imports stay within the payment hot-path allowlist.
# ---------------------------------------------------------------------------

def test_payment_service_imports_only_payment_deps():
    """Boot ``payment_service`` in a subprocess; verify forbidden modules are
    NOT loaded into ``sys.modules``.

    A subprocess is used to avoid cross-contamination from prior tests in the
    same interpreter that may have imported marketplace/escrow code.
    """
    # Build a small probe script that imports the service and dumps the loaded
    # module names. The probe also exercises the runtime import_guard, which
    # MUST not blow up when only allowed modules are loaded.
    probe = (
        "import sys, json\n"
        "sys.path.insert(0, %r)\n"
        "import gcp.payment_tee_deploy.payment_service as svc  # noqa: F401\n"
        "print(json.dumps(sorted(sys.modules.keys())))\n"
    ) % str(REPO_ROOT)

    # Force the guard to run in the subprocess. In normal unit tests we leave
    # TEE_ENFORCE_BOUNDARY unset because pytest collection legitimately loads
    # marketplace/escrow code from earlier suites — running the guard there
    # would produce false positives. The subprocess starts fresh, so the
    # guard sees only what the payment_service legitimately loads.
    env = os.environ.copy()
    env["TEE_ENFORCE_BOUNDARY"] = "1"
    result = _run([sys.executable, "-c", probe], env=env)

    assert result.returncode == 0, (
        f"payment_service import failed.\nSTDOUT: {result.stdout}\n"
        f"STDERR: {result.stderr}"
    )

    import json as _json
    loaded = _json.loads(result.stdout.strip().splitlines()[-1])

    leaks = [
        m for m in loaded
        if _is_project_module(m)
        and m.lower() not in ALLOWED_OVERRIDES
        and any(bad in m.lower() for bad in FORBIDDEN_SUBSTRINGS)
    ]
    assert not leaks, (
        f"Forbidden modules leaked into payment_service trust boundary:\n"
        f"{leaks}"
    )

    # Negative case: verify the guard ACTUALLY trips when a forbidden
    # module is loaded. We pre-load ``api.routers.escrow`` (forbidden),
    # then import the payment service. The guard MUST raise.
    negative_probe = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "import api.routers.escrow  # noqa: F401  -- forbidden\n"
        "try:\n"
        "    import gcp.payment_tee_deploy.payment_service\n"
        "except Exception as exc:\n"
        "    print('GUARD_TRIPPED:' + type(exc).__name__)\n"
        "    sys.exit(0)\n"
        "print('GUARD_FAILED_TO_TRIP')\n"
        "sys.exit(1)\n"
    ) % str(REPO_ROOT)
    neg_result = _run([sys.executable, "-c", negative_probe], env=env)
    assert neg_result.returncode == 0 and "GUARD_TRIPPED" in neg_result.stdout, (
        "Import guard failed to trip on a forbidden import.\n"
        f"STDOUT: {neg_result.stdout}\nSTDERR: {neg_result.stderr}"
    )


# ---------------------------------------------------------------------------
# Test 2 — /health.
# ---------------------------------------------------------------------------

def test_payment_service_health_endpoint():
    from fastapi.testclient import TestClient
    sys.path.insert(0, str(REPO_ROOT))
    from gcp.payment_tee_deploy.payment_service import app  # type: ignore

    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Test 3 — /attestation now serves the Sprint 7 payload (or 503 if unset).
# ---------------------------------------------------------------------------

def test_payment_service_attestation_stub(monkeypatch):
    """Sprint 7 superseded the stub.  When the signing key is set, the
    endpoint returns the canonical Sprint 7 payload; when it is unset it
    returns 503 (operator must explicitly opt in to attestation).
    """
    import base64
    import importlib

    from fastapi.testclient import TestClient
    from nacl.signing import SigningKey

    sys.path.insert(0, str(REPO_ROOT))

    sk = SigningKey.generate()
    monkeypatch.setenv(
        "STHRIP_TEE_ATTESTATION_KEY", base64.b64encode(sk.encode()).decode()
    )
    monkeypatch.setenv("STHRIP_TEE_IMAGE_HASH", "sha256:" + "0" * 64)

    from gcp.payment_tee_deploy import payment_service  # type: ignore
    importlib.reload(payment_service)

    client = TestClient(payment_service.app)
    resp = client.get("/attestation")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in ("quote_b64", "image_hash_sha256", "timestamp", "signature_b64", "mode"):
        assert key in body, body
    assert body["mode"] in ("snp_real", "snp_stub")


# ---------------------------------------------------------------------------
# Test 4 — /v2/payments/hub-routing endpoint exists.
# ---------------------------------------------------------------------------

def test_payment_service_hub_routing_endpoint_exists():
    """Verifies the route is registered. Auth/payload validation MUST reject
    an unauthenticated/empty POST — proving the endpoint is wired without
    requiring a full DB.
    """
    from fastapi.testclient import TestClient
    sys.path.insert(0, str(REPO_ROOT))
    from gcp.payment_tee_deploy.payment_service import app  # type: ignore

    client = TestClient(app)
    # Send an obviously invalid body. We expect 401/403/422 — anything that
    # proves the route exists and validates input. A 404 would mean the route
    # is missing.
    resp = client.post("/v2/payments/hub-routing", json={})
    assert resp.status_code != 404, (
        f"Route /v2/payments/hub-routing missing. Got {resp.status_code}: "
        f"{resp.text}"
    )
    assert resp.status_code in (400, 401, 403, 422), (
        f"Expected validation/auth rejection, got {resp.status_code}: "
        f"{resp.text}"
    )


# ---------------------------------------------------------------------------
# Test 5 — Dockerfile lints.
# ---------------------------------------------------------------------------

def test_dockerfile_lints():
    dockerfile = GCP_DIR / "Dockerfile"
    assert dockerfile.exists(), f"Missing {dockerfile}"

    text = dockerfile.read_text()

    # Multi-stage: at least 2 FROM directives.
    from_lines = [ln for ln in text.splitlines() if ln.strip().startswith("FROM ")]
    assert len(from_lines) >= 2, "Dockerfile must be multi-stage (>=2 FROM)"

    # Pinned base: python:3.9-slim somewhere.
    assert "python:3.9-slim" in text, "Base image must be python:3.9-slim"

    # Non-root user.
    assert "USER " in text, "Dockerfile must declare a non-root USER"
    # User must NOT be root.
    user_lines = [
        ln.strip() for ln in text.splitlines() if ln.strip().startswith("USER ")
    ]
    assert all("root" not in ln.lower() for ln in user_lines), (
        f"USER must not be root: {user_lines}"
    )

    # Exposes 8080.
    assert "EXPOSE 8080" in text, "Dockerfile must EXPOSE 8080"

    # apt-get install always uses --no-install-recommends.
    for ln in text.splitlines():
        stripped = ln.strip()
        if "apt-get install" in stripped:
            assert "--no-install-recommends" in stripped, (
                f"apt-get install must use --no-install-recommends: {ln}"
            )

    # Runs uvicorn against payment_service:app.
    assert "payment_service:app" in text, "CMD must launch payment_service:app"


# ---------------------------------------------------------------------------
# Test 6 — setup-vm.sh --dry-run prints expected gcloud command.
# ---------------------------------------------------------------------------

def test_setup_vm_dry_run():
    script = GCP_DIR / "setup-vm.sh"
    assert script.exists(), f"Missing {script}"

    # Sanitise env so the script can't accidentally hit real gcloud.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "GCP_PROJECT": "sthrip-tee-test",
        "GCP_REGION": "us-central1",
        "GCP_ZONE": "us-central1-a",
        "VM_NAME": "sthrip-payment-tee-test",
    }
    result = _run(["bash", str(script), "--dry-run"], env=env)

    assert result.returncode == 0, (
        f"setup-vm.sh --dry-run failed.\nSTDOUT: {result.stdout}\n"
        f"STDERR: {result.stderr}"
    )

    out = result.stdout
    assert "gcloud compute instances create" in out, (
        f"--dry-run must print the gcloud create command. Got:\n{out}"
    )
    assert "--confidential-compute-type=SEV_SNP" in out, (
        f"--dry-run must include --confidential-compute-type=SEV_SNP. Got:\n{out}"
    )
    assert "n2d-standard-2" in out, (
        f"--dry-run must include n2d-standard-2 machine type. Got:\n{out}"
    )
    # Idempotency probe MUST also be a no-op describe call in dry-run.
    assert "instances describe" in out, (
        "--dry-run must show the idempotency `describe` probe"
    )


# ---------------------------------------------------------------------------
# Test 7 — setup-vm.sh idempotency.
# ---------------------------------------------------------------------------

def test_setup_vm_idempotent():
    """When ``gcloud compute instances describe`` returns success, setup-vm.sh
    must short-circuit with an "already exists" message and NOT call
    ``instances create``.

    We mock gcloud by injecting a fake binary on PATH that returns 0 for
    ``describe`` (instance exists) and exits 99 for any ``create`` call so
    the test fails loudly if create is invoked.
    """
    with tempfile.TemporaryDirectory() as tmp:
        bindir = Path(tmp) / "bin"
        bindir.mkdir()
        fake_gcloud = bindir / "gcloud"
        fake_gcloud.write_text(
            "#!/usr/bin/env bash\n"
            "for arg in \"$@\"; do\n"
            "  if [ \"$arg\" = \"describe\" ]; then\n"
            "    # Pretend the instance already exists.\n"
            "    echo 'name: existing-instance'\n"
            "    exit 0\n"
            "  fi\n"
            "  if [ \"$arg\" = \"create\" ]; then\n"
            "    echo 'ERROR: setup-vm.sh tried to create even though instance exists' >&2\n"
            "    exit 99\n"
            "  fi\n"
            "done\n"
            "exit 0\n"
        )
        fake_gcloud.chmod(0o755)

        env = {
            "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
            "HOME": os.environ.get("HOME", ""),
            "GCP_PROJECT": "sthrip-tee-test",
            "GCP_REGION": "us-central1",
            "GCP_ZONE": "us-central1-a",
            "VM_NAME": "sthrip-payment-tee-test",
        }
        script = GCP_DIR / "setup-vm.sh"
        result = _run(["bash", str(script)], env=env)

        assert result.returncode == 0, (
            f"setup-vm.sh failed unexpectedly.\nSTDOUT: {result.stdout}\n"
            f"STDERR: {result.stderr}"
        )
        out = (result.stdout + result.stderr).lower()
        assert "already exists" in out, (
            f"setup-vm.sh must print 'already exists' on idempotent re-run.\n"
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# Test 8 — mtls/generate-certs.sh produces 3 cert files.
# ---------------------------------------------------------------------------

def test_mtls_cert_script_generates_three_certs():
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available on this host")

    script = GCP_DIR / "mtls" / "generate-certs.sh"
    assert script.exists(), f"Missing {script}"

    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "CERT_OUT_DIR": tmp,
        }
        result = _run(["bash", str(script)], env=env)
        assert result.returncode == 0, (
            f"generate-certs.sh failed.\nSTDOUT: {result.stdout}\n"
            f"STDERR: {result.stderr}"
        )

        for fname in ("ca.crt", "server.crt", "client.crt"):
            path = Path(tmp) / fname
            assert path.exists(), f"Missing cert {fname}"
            assert path.stat().st_size > 0, f"Empty cert {fname}"
            head = path.read_text(errors="replace").splitlines()[0]
            assert "BEGIN CERTIFICATE" in head, (
                f"{fname} does not look like a PEM cert: {head!r}"
            )
