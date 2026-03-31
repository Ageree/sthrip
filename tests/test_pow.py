"""Tests for hashcash-style proof-of-work Sybil prevention.

Covers:
- POWService: challenge creation, solving, verification, edge cases
- API endpoints: POST /v2/agents/register/challenge, registration with PoW
- SDK client: _solve_pow_challenge helper
"""

import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from sthrip.services.pow_service import POWService, get_pow_service, reset_pow_service


# ═══════════════════════════════════════════════════════════════════════════════
# Unit tests — POWService
# ═══════════════════════════════════════════════════════════════════════════════


class TestPOWServiceInit:
    def test_default_difficulty(self) -> None:
        svc = POWService()
        assert svc.difficulty_bits == 20

    def test_custom_difficulty(self) -> None:
        svc = POWService(difficulty_bits=16)
        assert svc.difficulty_bits == 16

    def test_rejects_zero_difficulty(self) -> None:
        with pytest.raises(ValueError, match="difficulty_bits must be between"):
            POWService(difficulty_bits=0)

    def test_rejects_excessive_difficulty(self) -> None:
        with pytest.raises(ValueError, match="difficulty_bits must be between"):
            POWService(difficulty_bits=33)


class TestPOWServiceCreateChallenge:
    def test_create_challenge_fields(self) -> None:
        svc = POWService(difficulty_bits=16)
        challenge = svc.create_challenge()
        assert challenge["algorithm"] == "sha256"
        assert challenge["difficulty_bits"] == 16
        assert "nonce" in challenge
        assert len(challenge["nonce"]) == 32  # hex(16 bytes)
        assert "expires_at" in challenge

    def test_create_challenge_expiry_in_future(self) -> None:
        svc = POWService(difficulty_bits=16)
        challenge = svc.create_challenge(ttl_minutes=5)
        expires_at = datetime.fromisoformat(challenge["expires_at"])
        now = datetime.now(timezone.utc)
        assert expires_at > now
        assert expires_at < now + timedelta(minutes=6)

    def test_create_challenge_unique_nonces(self) -> None:
        svc = POWService(difficulty_bits=16)
        nonces = {svc.create_challenge()["nonce"] for _ in range(10)}
        assert len(nonces) == 10  # All unique

    def test_create_challenge_rejects_zero_ttl(self) -> None:
        svc = POWService(difficulty_bits=16)
        with pytest.raises(ValueError, match="ttl_minutes must be >= 1"):
            svc.create_challenge(ttl_minutes=0)


class TestPOWServiceSolveAndVerify:
    """Use difficulty_bits=16 so solving takes <1 second in CI."""

    def test_solve_and_verify(self) -> None:
        svc = POWService(difficulty_bits=16)
        challenge = svc.create_challenge()
        solution = svc.solve(challenge)
        assert svc.verify(challenge, solution) is True

    def test_solution_is_numeric_string(self) -> None:
        svc = POWService(difficulty_bits=16)
        challenge = svc.create_challenge()
        solution = svc.solve(challenge)
        assert solution.isdigit() or (solution.startswith("-") is False and solution.isdigit())
        int(solution)  # Should not raise

    def test_solution_produces_valid_hash(self) -> None:
        svc = POWService(difficulty_bits=16)
        challenge = svc.create_challenge()
        solution = svc.solve(challenge)

        candidate = "{}:{}".format(challenge["nonce"], solution)
        digest = hashlib.sha256(candidate.encode()).hexdigest()
        bits = bin(int(digest, 16))[2:].zfill(256)
        assert bits[:16] == "0" * 16


class TestPOWServiceVerifyRejects:
    def test_verify_rejects_wrong_solution(self) -> None:
        svc = POWService(difficulty_bits=16)
        challenge = svc.create_challenge()
        assert svc.verify(challenge, "wrong_nonce") is False

    def test_verify_rejects_random_number(self) -> None:
        svc = POWService(difficulty_bits=16)
        challenge = svc.create_challenge()
        # Very unlikely that 999999999 is the correct solution
        assert svc.verify(challenge, "999999999") is False

    def test_verify_rejects_expired_challenge(self) -> None:
        svc = POWService(difficulty_bits=16)
        challenge = svc.create_challenge()
        # Solve first (before we tamper with expiry)
        solution = svc.solve(challenge)
        # Now expire it
        expired = dict(challenge)
        expired["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        assert svc.verify(expired, solution) is False

    def test_verify_rejects_missing_expires_at(self) -> None:
        svc = POWService(difficulty_bits=16)
        challenge = svc.create_challenge()
        solution = svc.solve(challenge)
        no_expiry = {k: v for k, v in challenge.items() if k != "expires_at"}
        assert svc.verify(no_expiry, solution) is False

    def test_verify_rejects_invalid_expires_at(self) -> None:
        svc = POWService(difficulty_bits=16)
        challenge = svc.create_challenge()
        solution = svc.solve(challenge)
        bad_expiry = dict(challenge)
        bad_expiry["expires_at"] = "not-a-date"
        assert svc.verify(bad_expiry, solution) is False

    def test_verify_rejects_non_numeric_solution(self) -> None:
        svc = POWService(difficulty_bits=16)
        challenge = svc.create_challenge()
        assert svc.verify(challenge, "abc") is False

    def test_verify_rejects_none_solution(self) -> None:
        svc = POWService(difficulty_bits=16)
        challenge = svc.create_challenge()
        assert svc.verify(challenge, None) is False  # type: ignore[arg-type]

    def test_verify_rejects_missing_difficulty(self) -> None:
        svc = POWService(difficulty_bits=16)
        challenge = svc.create_challenge()
        solution = svc.solve(challenge)
        no_diff = {k: v for k, v in challenge.items() if k != "difficulty_bits"}
        assert svc.verify(no_diff, solution) is False


class TestPOWServiceSingleton:
    def setup_method(self) -> None:
        reset_pow_service()

    def teardown_method(self) -> None:
        reset_pow_service()

    def test_get_pow_service_returns_singleton(self) -> None:
        svc1 = get_pow_service(difficulty_bits=16)
        svc2 = get_pow_service(difficulty_bits=20)  # ignored (already created)
        assert svc1 is svc2
        assert svc1.difficulty_bits == 16

    def test_reset_clears_singleton(self) -> None:
        svc1 = get_pow_service(difficulty_bits=16)
        reset_pow_service()
        svc2 = get_pow_service(difficulty_bits=20)
        assert svc1 is not svc2
        assert svc2.difficulty_bits == 20


# ═══════════════════════════════════════════════════════════════════════════════
# API integration tests — challenge + registration endpoints
# ═══════════════════════════════════════════════════════════════════════════════


class TestPOWChallengeEndpoint:
    """Test POST /v2/agents/register/challenge."""

    def test_returns_challenge(self, client) -> None:
        resp = client.post("/v2/agents/register/challenge")
        assert resp.status_code == 200
        data = resp.json()
        assert data["algorithm"] == "sha256"
        assert "nonce" in data
        assert "difficulty_bits" in data
        assert "expires_at" in data

    def test_challenge_difficulty_matches_service(self, client) -> None:
        resp = client.post("/v2/agents/register/challenge")
        data = resp.json()
        assert isinstance(data["difficulty_bits"], int)
        assert data["difficulty_bits"] > 0


class TestRegistrationWithPOW:
    """Test POST /v2/agents/register with PoW."""

    def test_register_without_pow_still_works(self, client) -> None:
        """Backward compatibility: PoW is optional."""
        resp = client.post(
            "/v2/agents/register",
            json={"agent_name": "no-pow-agent", "privacy_level": "medium"},
        )
        assert resp.status_code == 201
        assert resp.json()["agent_name"] == "no-pow-agent"

    def test_register_with_valid_pow(self, client) -> None:
        """Full flow: get challenge, solve, register."""
        # Get challenge
        challenge_resp = client.post("/v2/agents/register/challenge")
        assert challenge_resp.status_code == 200
        challenge = challenge_resp.json()

        # Solve it (use POWService for convenience)
        svc = POWService(difficulty_bits=challenge["difficulty_bits"])
        solution = svc.solve(challenge)

        # Register with solved PoW
        resp = client.post(
            "/v2/agents/register",
            json={
                "agent_name": "pow-agent",
                "privacy_level": "medium",
                "pow_challenge": {
                    "nonce": challenge["nonce"],
                    "difficulty_bits": challenge["difficulty_bits"],
                    "expires_at": challenge["expires_at"],
                    "solution": solution,
                },
            },
        )
        assert resp.status_code == 201
        assert resp.json()["agent_name"] == "pow-agent"

    def test_register_with_invalid_pow_rejected(self, client) -> None:
        """Registration fails if PoW solution is wrong."""
        challenge_resp = client.post("/v2/agents/register/challenge")
        challenge = challenge_resp.json()

        resp = client.post(
            "/v2/agents/register",
            json={
                "agent_name": "bad-pow-agent",
                "privacy_level": "medium",
                "pow_challenge": {
                    "nonce": challenge["nonce"],
                    "difficulty_bits": challenge["difficulty_bits"],
                    "expires_at": challenge["expires_at"],
                    "solution": "99999999",
                },
            },
        )
        assert resp.status_code == 400
        assert "proof-of-work" in resp.json()["detail"].lower()

    def test_register_with_expired_pow_rejected(self, client) -> None:
        """Registration fails if PoW challenge is expired."""
        expired_ts = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()

        resp = client.post(
            "/v2/agents/register",
            json={
                "agent_name": "expired-pow-agent",
                "privacy_level": "medium",
                "pow_challenge": {
                    "nonce": "a" * 32,
                    "difficulty_bits": 16,
                    "expires_at": expired_ts,
                    "solution": "0",
                },
            },
        )
        assert resp.status_code == 400
        assert "proof-of-work" in resp.json()["detail"].lower()


# ═══════════════════════════════════════════════════════════════════════════════
# SDK client tests — _solve_pow_challenge
# ═══════════════════════════════════════════════════════════════════════════════


class TestSDKPOWSolver:
    """Test the SDK client's PoW auto-solve during registration."""

    def test_solve_pow_challenge_returns_valid_proof(self) -> None:
        from sdk.sthrip.client import Sthrip

        # Create a client without triggering auto-register
        client = object.__new__(Sthrip)
        client._api_url = "http://localhost"
        client._session = MagicMock()

        challenge_data = {
            "algorithm": "sha256",
            "difficulty_bits": 16,
            "nonce": "abcdef1234567890abcdef1234567890",
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
        }

        with patch.object(client, "_raw_post", return_value=challenge_data):
            result = client._solve_pow_challenge()

        assert result is not None
        assert result["nonce"] == challenge_data["nonce"]
        assert result["difficulty_bits"] == 16
        assert result["expires_at"] == challenge_data["expires_at"]
        assert "solution" in result

        # Verify the solution is actually valid
        svc = POWService(difficulty_bits=16)
        assert svc.verify(challenge_data, result["solution"]) is True

    def test_solve_pow_challenge_degrades_on_error(self) -> None:
        from sdk.sthrip.client import Sthrip

        client = object.__new__(Sthrip)
        client._api_url = "http://localhost"
        client._session = MagicMock()

        with patch.object(client, "_raw_post", side_effect=Exception("Server error")):
            result = client._solve_pow_challenge()

        assert result is None  # Graceful degradation
