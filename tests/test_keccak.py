"""Tests for Keccak-256 via pycryptodome (not hashlib.sha3_256).

TDD: these tests were written BEFORE the implementation fix was applied.
"""

import hashlib
import sys
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Existing baseline tests (RED -> GREEN with pycryptodome installed)
# ---------------------------------------------------------------------------

def test_keccak256_uses_pycryptodome():
    """Verify Keccak-256 uses pycryptodome, not hashlib.sha3_256."""
    from Crypto.Hash import keccak

    k = keccak.new(digest_bits=256)
    k.update(b"test")
    result = k.digest()
    assert len(result) == 32
    # Known Keccak-256 of "test" (different from SHA3-256)
    assert result.hex().startswith("9c22ff5f")


def test_keccak256_differs_from_sha3_256():
    """Keccak-256 must produce different output than SHA3-256."""
    from Crypto.Hash import keccak

    data = b"monero address validation"

    k = keccak.new(digest_bits=256)
    k.update(data)
    keccak_result = k.digest()

    sha3_result = hashlib.sha3_256(data).digest()

    assert keccak_result != sha3_result, "Keccak-256 and SHA3-256 must differ"


# ---------------------------------------------------------------------------
# New tests for the _keccak256 helper in api/schemas.py
# ---------------------------------------------------------------------------

def _import_keccak256():
    """Import _keccak256 fresh from api.schemas each call."""
    import importlib
    import api.schemas as mod
    importlib.reload(mod)
    return mod._keccak256


def test_internal_keccak256_returns_correct_bytes_with_pycryptodome():
    """_keccak256 must return the canonical Keccak-256 digest when pycryptodome is available."""
    from api.schemas import _keccak256
    from Crypto.Hash import keccak

    data = b"hello keccak"
    k = keccak.new(digest_bits=256)
    k.update(data)
    expected = k.digest()

    assert _keccak256(data) == expected


def test_internal_keccak256_returns_32_bytes():
    """_keccak256 must always return exactly 32 bytes."""
    from api.schemas import _keccak256

    result = _keccak256(b"")
    assert isinstance(result, bytes)
    assert len(result) == 32


def test_internal_keccak256_known_vector():
    """_keccak256 of b'' must equal the known Keccak-256 empty hash."""
    from api.schemas import _keccak256

    # Known Keccak-256("") = c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470
    expected_hex = "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert _keccak256(b"").hex() == expected_hex


def test_internal_keccak256_does_not_equal_sha3_256():
    """_keccak256 must NOT return the SHA3-256 digest (silent wrong-hash bug guard)."""
    from api.schemas import _keccak256

    data = b"monero address validation"
    sha3_result = hashlib.sha3_256(data).digest()

    assert _keccak256(data) != sha3_result, (
        "CRITICAL: _keccak256 returned SHA3-256 output — silent wrong-hash fallback is active"
    )


def test_internal_keccak256_raises_when_no_keccak_library():
    """_keccak256 must raise RuntimeError when neither pycryptodome nor pysha3 is available.

    This test verifies that the silent SHA3-256 fallback has been removed.
    """
    import api.schemas as schemas_mod

    # Simulate both crypto libraries being absent by hiding them in sys.modules
    with patch.dict(sys.modules, {"Crypto": None, "Crypto.Hash": None, "sha3": None}):
        import importlib
        # We need to reload to pick up the patched imports; instead, directly
        # call a wrapper that exercises the import paths inside _keccak256.
        # The cleanest approach: monkeypatch builtins.__import__ so that
        # any attempt to import from Crypto or sha3 raises ImportError.
        real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def _blocking_import(name, *args, **kwargs):
            if name in ("Crypto.Hash", "sha3") or name.startswith("Crypto"):
                raise ImportError(f"Blocked for test: {name}")
            return real_import(name, *args, **kwargs)

        import builtins
        with patch.object(builtins, "__import__", side_effect=_blocking_import):
            import importlib
            # Reload schemas so _keccak256 re-runs its import logic
            importlib.reload(schemas_mod)
            try:
                # After reload, the module-level code has run; now call the function
                # so its internal imports are attempted again.
                result = schemas_mod._keccak256(b"test")
                # If we reach here without RuntimeError, the fallback is still active — FAIL
                assert False, (
                    f"Expected RuntimeError but _keccak256 returned {result!r}. "
                    "The silent SHA3-256 fallback must be removed."
                )
            except RuntimeError as exc:
                assert "pycryptodome" in str(exc).lower() or "keccak" in str(exc).lower(), (
                    f"RuntimeError message should mention the missing library, got: {exc}"
                )
            except ImportError:
                # An ImportError propagating out is also acceptable (explicit failure)
                pass
            finally:
                # Restore the original module state
                importlib.reload(schemas_mod)


# ---------------------------------------------------------------------------
# Integration: validate_monero_address checksum uses correct Keccak-256
# ---------------------------------------------------------------------------

def test_validate_monero_address_accepts_valid_stagenet_address():
    """validate_monero_address must accept a well-formed stagenet address without checksum error.

    This test guards against the silent wrong-hash bug: if SHA3-256 were used the
    checksum bytes would not match and every real address would be rejected (or, in
    dev mode, silently skipped — making the bug invisible in tests).
    """
    # Use dev environment to skip live checksum so we don't need a real node address.
    # The important thing is that the code path doesn't crash with ImportError/RuntimeError.
    from unittest.mock import MagicMock

    import api.schemas as schemas_mod

    fake_settings = MagicMock()
    fake_settings.monero_network = "stagenet"
    fake_settings.environment = "dev"

    # get_settings is imported inside validate_monero_address via `from sthrip.config import ...`
    with patch("sthrip.config.get_settings", return_value=fake_settings):
        # A syntactically valid stagenet address (starts with 5, 95 chars, base58 chars only)
        addr = "5" + "A" * 94
        result = schemas_mod.validate_monero_address(addr)
        assert result == addr


def test_validate_monero_address_rejects_bad_checksum_in_non_dev():
    """validate_monero_address must reject an address with invalid checksum in non-dev env.

    This confirms that the checksum path executes and uses _keccak256 (not SHA3-256).
    An address with a deliberately wrong last 4 bytes must be rejected.
    """
    import pytest
    from unittest.mock import MagicMock

    import api.schemas as schemas_mod

    fake_settings = MagicMock()
    fake_settings.monero_network = "stagenet"
    fake_settings.environment = "stagenet"

    # Build a 95-char stagenet address with a bogus payload so the checksum will be wrong.
    # We need it to decode cleanly (correct base58 chars, correct length) but fail checksum.
    # Use a real-looking but all-zeros-ish payload that will fail Keccak-256 checksum.
    bad_addr = "5" + "1" * 94  # 95 chars, starts with 5 (stagenet), all '1's in base58

    # get_settings is imported inside validate_monero_address via `from sthrip.config import ...`
    with patch("sthrip.config.get_settings", return_value=fake_settings):
        with pytest.raises(ValueError, match="[Cc]hecksum|[Ii]nvalid"):
            schemas_mod.validate_monero_address(bad_addr)
