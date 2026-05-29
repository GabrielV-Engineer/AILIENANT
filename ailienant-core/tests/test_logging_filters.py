"""Phase 6.7 — SecretsScrubber engine + SecretsScrubberFilter unit tests."""
import hashlib
import logging
from typing import cast

from shared.logging_filters import SecretsScrubber, SecretsScrubberFilter


def _h8(secret: str) -> str:
    return hashlib.blake2b(secret.encode("utf-8")).hexdigest()[:8]


def test_openai_key_redacted() -> None:
    raw = "config key sk-ABCDEFGHIJKLMNOPQRSTUVWX trailing"
    out = SecretsScrubber.scrub(raw)
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in out
    assert "REDACTED:" in out


def test_anthropic_key_redacted() -> None:
    raw = "auth sk-ant-AAAAAAAAAAAAAAAAAAAA tail"
    out = SecretsScrubber.scrub(raw)
    assert "sk-ant-AAAAAAAAAAAAAAAAAAAA" not in out
    assert "REDACTED:" in out


def test_bearer_and_jwt_redacted() -> None:
    bearer = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123"
    out_b = SecretsScrubber.scrub(bearer)
    assert "abcdefghijklmnopqrstuvwxyz0123" not in out_b
    assert "REDACTED:" in out_b

    jwt = "tok eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkw.dozjgNryP4J3jVmNHDpr5"
    out_j = SecretsScrubber.scrub(jwt)
    assert "eyJzdWIiOiIxMjM0NTY3ODkw" not in out_j
    assert "REDACTED:" in out_j


def test_url_credentials_redacted() -> None:
    raw = "db at https://admin:supersecret@lancedb.local/path"
    out = SecretsScrubber.scrub(raw)
    assert out == f"db at https://REDACTED:{_h8('admin:supersecret')}@lancedb.local/path"


def test_determinism() -> None:
    raw = "sk-ant-AAAAAAAAAAAAAAAAAAAA"
    assert SecretsScrubber.scrub(raw) == SecretsScrubber.scrub(raw)
    assert SecretsScrubber.scrub(raw) == f"REDACTED:{_h8(raw)}"


def test_filter_mutates_logrecord_msg_and_tuple_args() -> None:
    f = SecretsScrubberFilter()
    record = logging.LogRecord(
        name="TEST", level=logging.INFO, pathname=__file__, lineno=1,
        msg="leaked sk-ant-AAAAAAAAAAAAAAAAAAAA in msg",
        args=("arg sk-BBBBBBBBBBBBBBBBBBBBBB here",), exc_info=None,
    )
    assert f.filter(record) is True
    assert "sk-ant-AAAAAAAAAAAAAAAAAAAA" not in record.msg
    assert "REDACTED:" in record.msg
    assert record.args is not None
    assert "sk-BBBBBBBBBBBBBBBBBBBBBB" not in cast("tuple[str, ...]", record.args)[0]


def test_filter_scrubs_dict_args() -> None:
    f = SecretsScrubberFilter()
    record = logging.LogRecord(
        name="TEST", level=logging.INFO, pathname=__file__, lineno=1,
        msg="user %(user)s",
        args=({"user": "Bearer abcdefghijklmnopqrstuvwxyz0123"},), exc_info=None,
    )
    assert f.filter(record) is True
    assert record.args is not None
    assert "abcdefghijklmnopqrstuvwxyz0123" not in cast("dict[str, str]", record.args)["user"]
