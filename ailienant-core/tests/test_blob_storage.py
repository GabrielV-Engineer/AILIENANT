# ailienant-core/tests/test_blob_storage.py
#
# DoD: pytest tests/test_blob_storage.py -v must pass with 0 failures.

import hashlib

import pytest

from core.blob_storage import ContentAddressableStorage, _apply_unified_diff


# ---------------------------------------------------------------------------
# ContentAddressableStorage — unit tests
# ---------------------------------------------------------------------------


def test_put_returns_blake2b_hash() -> None:
    """put() must return the blake2b hexdigest of the UTF-8 encoded content."""
    cas = ContentAddressableStorage()
    content = "hello world"
    expected = hashlib.blake2b(content.encode("utf-8")).hexdigest()
    result = cas.put(content)
    assert result == expected


def test_get_returns_stored_content() -> None:
    """get(hash) must return the exact string that was stored via put()."""
    cas = ContentAddressableStorage()
    content = "def foo():\n    return 42\n"
    h = cas.put(content)
    assert cas.get(h) == content


def test_put_same_content_returns_same_hash() -> None:
    """Content-addressing invariant: identical content always produces the same hash."""
    cas = ContentAddressableStorage()
    content = "x" * 5_000_000  # 5 MB blob
    h1 = cas.put(content)
    h2 = cas.put(content)
    assert h1 == h2, "Same content must hash to the same key"
    assert len(cas) == 1, "Deduplication: only one entry stored for identical content"


def test_apply_patch_updates_stored_content_and_hash() -> None:
    """apply_patch must produce a new hash whose stored content reflects the diff."""
    cas = ContentAddressableStorage()
    original = "line1\nline2\nline3\n"
    old_hash = cas.put(original)

    # Unified diff that replaces "line2" with "line2_modified"
    patch = (
        "--- a/file.py\n"
        "+++ b/file.py\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n"
        "-line2\n"
        "+line2_modified\n"
        " line3\n"
    )

    new_hash = cas.apply_patch(old_hash, patch)
    assert new_hash is not None, "apply_patch must return a hash on success"
    assert new_hash != old_hash, "Patched content must have a different hash"

    patched_content = cas.get(new_hash)
    assert patched_content == "line1\nline2_modified\nline3\n"


def test_apply_patch_returns_none_on_bad_hunk() -> None:
    """apply_patch must return None (not raise) when the diff does not apply cleanly."""
    cas = ContentAddressableStorage()
    original = "hello\n"
    old_hash = cas.put(original)

    # Diff that expects "world" on line 1 — won't match "hello"
    bad_patch = (
        "--- a/file.py\n"
        "+++ b/file.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-world\n"
        "+universe\n"
    )

    result = cas.apply_patch(old_hash, bad_patch)
    assert result is None, "apply_patch must return None on bad hunk"


# ---------------------------------------------------------------------------
# _apply_unified_diff — internal helper
# ---------------------------------------------------------------------------


def test_apply_unified_diff_add_lines() -> None:
    """Diff that inserts a new line at the end must be applied correctly."""
    original = "a\nb\n"
    patch = (
        "--- a/f\n"
        "+++ b/f\n"
        "@@ -1,2 +1,3 @@\n"
        " a\n"
        " b\n"
        "+c\n"
    )
    assert _apply_unified_diff(original, patch) == "a\nb\nc\n"


def test_apply_unified_diff_raises_on_mismatch() -> None:
    """_apply_unified_diff must raise ValueError when removal line doesn't match."""
    original = "x\n"
    patch = (
        "--- a/f\n"
        "+++ b/f\n"
        "@@ -1,1 +1,1 @@\n"
        "-y\n"
        "+z\n"
    )
    with pytest.raises(ValueError):
        _apply_unified_diff(original, patch)
