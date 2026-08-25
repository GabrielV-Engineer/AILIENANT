# ailienant-core/tests/conftest.py
# Phase 2.25 — Tests-level conftest; writes CHECKPOINT_REPORT.md after the session.
# Phase 7.9.B.9 — Added _DirectAdapter autouse fixture to fix 6 failing execution tests.
from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import os
import sys
from typing import Dict, Iterator, Optional

import pytest

from core.sandbox import SandboxResult


class _DirectAdapter:
    """Minimal test double: runs subprocess directly (no HITL gate, no Docker).

    Restores the pre-Phase-6.2 behaviour so execution-tool tests pass under
    plain pytest without a live FastAPI lifespan or Docker daemon.
    """

    async def execute(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: Optional[str] = None,
    ) -> SandboxResult:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or None,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s
                )
                return SandboxResult(
                    exit_code=proc.returncode or 0,
                    stdout=stdout_b.decode("utf-8", errors="replace"),
                    stderr=stderr_b.decode("utf-8", errors="replace"),
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return SandboxResult(exit_code=124, stdout="", stderr="[timeout]")
        except Exception as exc:  # noqa: BLE001
            return SandboxResult(exit_code=1, stdout="", stderr=str(exc))


@pytest.fixture(autouse=True)
def _resolve_adapter(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Bind a direct-subprocess adapter so execution tests run without FastAPI lifespan."""
    import core.sandbox as sb

    monkeypatch.setattr(sb, "ACTIVE_ADAPTER", _DirectAdapter())
    monkeypatch.setattr(sb, "ACTIVE_TIER", "NATIVE_HITL")
    # Reset the trusted-tier injection seam so an e2e lifespan (which injects a real
    # WebSocketHostBridge) never leaks into a later unit test — an unresolved bridge
    # would otherwise park a trusted call on the 600 s provision wait. The e2e client
    # injects inside its own `with` block, which runs after this fixture.
    sb.set_trusted_bridge(None)
    sb.reset_trusted_adapter()
    # Trusted execution routes through the devcontainer tier in production, but unit
    # tests have no host bridge — routing there would land on the HITL-native fallback
    # and block on an approval that never arrives. Make the trusted selector delegate
    # to the (already faked) oracle adapter so existing tests that patch
    # `get_active_adapter` and pass a session_id keep working, and nothing hangs.
    # Tests exercising the real selector override this via their own monkeypatch.
    # Late binding (lambda calls at invocation time) so a test that patches
    # `get_active_adapter` after this fixture still has its fake resolved.
    monkeypatch.setattr(sb, "get_trusted_adapter", lambda: sb.get_active_adapter())
    # Same reasoning for the non-interactive trusted selector (DEBT-086):
    # check_type_integrity / coder_tools._exec resolve through this when bound
    # to a session, and it must land on the same faked oracle adapter rather
    # than building a real DevcontainerSandboxAdapter with no bridge.
    monkeypatch.setattr(sb, "get_trusted_adapter_silent", lambda: sb.get_active_adapter())
    # Phase 12.6 — the daemon circuit breaker, the exec-client timeout-bucket
    # cache, and the session→workspace-root resolver are process-global state
    # a real `docker.from_env()` failure (no daemon on this host) can trip via
    # any test that exercises the full lifespan. Reset before AND after so one
    # test tripping the breaker can never spuriously fail an unrelated later
    # test that happens to touch the sandbox module.
    sb.reset_sandbox_pool_state()
    yield
    sb.reset_sandbox_pool_state()


@pytest.fixture(autouse=True)
def _stub_blast_radius(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the pre-apply blast-radius check so unit tests stay hermetic.

    ``compute_blast_radius`` reads the real on-disk catalog DB (``DB_CATALOG_PATH``);
    without this stub, every test that reaches ``_run_coding_task``'s apply path would
    perform live I/O against the developer's actual project graph and could escalate
    nondeterministically depending on what happens to be indexed on that machine. A
    test exercising escalation overrides this via its own patch, applied after this
    fixture runs.
    """
    import core.blast_radius as br

    async def _empty(*_a: object, **_k: object) -> list:
        return []

    monkeypatch.setattr(br, "compute_blast_radius", _empty)


@pytest.fixture(autouse=True)
def _reset_skill_embed_cache() -> None:
    """Clear the shared skill-description embedding cache before every test.

    core/skill_resolver.py's module-level `_default_description_embed_cache`
    (DEBT-052) is keyed by description content, so two tests that happen to
    seed a skill with the same description string (e.g. both 12.3 regression
    tests use "candidate skill") would otherwise let the second test's embed
    spy see a cache hit from the first — an accidental pass rather than a
    real one, since both tests' assertions only require the spy to fire at
    least once.
    """
    from core.skill_resolver import _default_description_embed_cache

    _default_description_embed_cache.clear()


@pytest.fixture(autouse=True)
def _reset_response_cache() -> None:
    """Clear the shared LLM response cache before every test.

    core/response_cache.py's module-level ``response_cache`` singleton (DEBT-153)
    is keyed by a hash of intent + per-file content, so two tests in different
    files that happen to build the identical ``(target_file, current_content,
    rag_snippets, budget)`` tuple would otherwise let the second test's generation
    mock see a cache hit from the first — a silently skipped generation call whose
    capture-based assertions then see nothing, an order-dependent cross-file
    contamination rather than a real per-test failure.
    """
    from core.response_cache import response_cache

    response_cache.clear()


@contextlib.contextmanager
def _litellm_leak_guard() -> Iterator[None]:
    """Testable core of ``_guard_litellm_patch_leakage`` below.

    Factored out as a plain context manager (rather than exercising the
    ``pytest.fixture``-wrapped generator directly) so its restore-and-log
    behavior is unit-tested without depending on pytest's own fixture
    machinery or cross-test ordering.
    """
    import litellm

    real_aembedding = litellm.aembedding
    real_acompletion = litellm.acompletion
    try:
        yield
    finally:
        current_test = os.environ.get("PYTEST_CURRENT_TEST", "<unknown>")
        if litellm.aembedding is not real_aembedding:
            logging.getLogger("AILIENANT_TEST_ISOLATION").error(
                "litellm.aembedding leaked past test %s — a patch (monkeypatch/"
                "mock.patch) was not restored; forcing it back so later tests "
                "are not corrupted (DEBT-201).",
                current_test,
            )
            litellm.aembedding = real_aembedding
        if litellm.acompletion is not real_acompletion:
            logging.getLogger("AILIENANT_TEST_ISOLATION").error(
                "litellm.acompletion leaked past test %s — a patch (monkeypatch/"
                "mock.patch) was not restored; forcing it back so later tests "
                "are not corrupted (DEBT-201).",
                current_test,
            )
            litellm.acompletion = real_acompletion


@pytest.fixture(autouse=True)
def _guard_litellm_patch_leakage() -> Iterator[None]:
    """Self-heal a litellm patch a prior test failed to restore (DEBT-201).

    A live incident saw a cascade of real ``litellm.exceptions.BadRequestError:
    ... model=ailienant/embedding`` failures in unrelated tests, followed by
    one flaky failure in an e2e apply-gate test three tests later — the shape
    of a leaked mock (or a leaked real call where a mock was expected)
    corrupting whichever test ran next. Re-running the suite never reproduced
    it, so the exact leaking test/fixture was never pinned down. Rather than
    guess at which of the dozen call sites that mock ``litellm.aembedding``/
    ``acompletion`` is responsible, this restores the real functions the
    instant a leak is detected and logs the offending test's nodeid — turning
    the failure mode from "a mystery cascade" into "self-healed and
    attributable" the next time it happens.
    """
    with _litellm_leak_guard():
        yield


def pytest_sessionfinish(session, exitstatus):
    """Write CHECKPOINT_REPORT.md with metrics collected during the test session."""
    # Module may be keyed as "test_parser_stress" or "tests.test_parser_stress"
    # depending on pytest import mode — search by suffix to handle both.
    parser_mod = next(
        (m for k, m in sys.modules.items() if k.endswith("test_parser_stress")),
        None,
    )
    parser_metrics: dict = getattr(parser_mod, "_PARSER_METRICS", {}) if parser_mod else {}

    timed = [v for v in parser_metrics.values() if isinstance(v, float)]
    avg_latency = round(sum(timed) / len(timed), 3) if timed else "N/A"

    all_passed = exitstatus == 0
    swarm_success = "100%" if all_passed else "DEGRADED (see test output)"
    recovery_status = (
        "PASS — graph exits cleanly after MAX_RETRIES=2"
        if all_passed
        else "FAIL"
    )

    report_path = os.path.join(os.path.dirname(__file__), "..", "CHECKPOINT_REPORT.md")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("# CHECKPOINT REPORT — Phase 2.25\n\n")
        fh.write(f"Generated: {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n\n")
        fh.write("## Average Parser Latency\n\n")
        fh.write("| Scenario | Latency (ms) |\n|---|---|\n")
        for k, v in parser_metrics.items():
            fh.write(f"| {k} | {v} |\n")
        fh.write(f"\n**Average:** {avg_latency} ms (threshold: < 50 ms)\n\n")
        fh.write("## Swarm Success Rate\n\n")
        fh.write(f"{swarm_success}\n\n")
        fh.write("## Error Recovery Status\n\n")
        fh.write(f"{recovery_status}\n\n")
        fh.write(f"## Test Suite\n\nExit status: `{exitstatus}` (0 = all passed)\n")
