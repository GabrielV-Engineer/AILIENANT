# ailienant-core/tests/test_gateway_eval_surface.py
#
# Eval-surface tests for the External Capability Gateway: run_benchmark (EXECUTE,
# budget-gated, async) submits a benchmark to the host over loopback and charges
# the caller upfront with a refund on failure; get_report (READ_ONLY) reads the
# machine-readable report back. The host-side benchmark service is exercised
# directly for its path-safety, single-flight, and failure-record guarantees.
# Loopback seams, the runner factory, and the ledger are stubbed — no real host,
# socket, model, or Docker.

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Iterator, List

import httpx
import pytest

from core import benchmark_service
from core.config.host_discovery import HostCoords, HostNotRunningError
from core.benchmark.metrics import ProblemMetrics
from core.benchmark.report import BenchmarkReport, build_report, validate_report
from gateway import handlers, ledger, server

_ARMS = ("G1", "G2", "G3", "G4", "G4_FORCE_CLOUD")
_HEX32 = "0123456789abcdef0123456789abcdef"


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def iso_ledger(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the ledger at an isolated tmp file and reset env knobs to defaults."""
    path = tmp_path / "gateway_ledger.json"
    monkeypatch.setattr(ledger, "LEDGER_PATH", path)
    for var in (
        "AILIENANT_GATEWAY_RATE_CAP",
        "AILIENANT_GATEWAY_RATE_REFILL_PER_S",
        "AILIENANT_GATEWAY_BUDGET",
        "AILIENANT_GATEWAY_CALLER_ID",
        "AILIENANT_GATEWAY_TOKEN",
        "AILIENANT_GATEWAY_BENCHMARK_COST",
    ):
        monkeypatch.delenv(var, raising=False)
    return path


@pytest.fixture()
def iso_bench(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Isolate the benchmark artifact dir and reset the run/slot state."""
    bench_dir = tmp_path / "benchmark"
    monkeypatch.setattr(benchmark_service, "BENCHMARK_DIR", bench_dir)
    monkeypatch.setattr(benchmark_service, "_runs", {})
    monkeypatch.setattr(benchmark_service, "_inflight", 0)
    monkeypatch.delenv("AILIENANT_GATEWAY_BENCH_CONCURRENCY", raising=False)
    return bench_dir


@pytest.fixture()
def registered() -> Iterator[None]:
    """Bind the real capability handlers, restoring the registry on teardown."""
    saved = dict(server._HANDLERS)
    for name, handler in handlers.CAPABILITY_HANDLERS.items():
        server.register_handler(name, handler)
    try:
        yield
    finally:
        server._HANDLERS.clear()
        server._HANDLERS.update(saved)


def _dispatch(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    result = asyncio.run(server.dispatch_call(name, args))
    return json.loads(result[0].text)  # type: ignore[no-any-return]


def _canned_report() -> BenchmarkReport:
    """A small schema-valid report a stub runner can return."""
    metrics: List[ProblemMetrics] = [
        ProblemMetrics(
            arm=arm,
            problem_id="p1",
            tokens_local=10.0,
            tokens_cloud=5.0,
            est_usd=0.0,
            tci=70.0,
            css=50.0,
            latency_s=0.0,
            verdict="passed",
        )
        for arm in _ARMS
    ]
    return build_report(metrics, corpus_sha="a3f1c7e2", complete=True)


class _StubRunner:
    """Stands in for a live BenchmarkRunner; returns a canned report."""

    def __init__(self, report: BenchmarkReport) -> None:
        self._report = report

    async def run_report(self, _problems: Any) -> BenchmarkReport:
        return self._report


def _consumed(caller: str = "anonymous") -> float:
    return ledger._load().get(caller, {}).get("budget_consumed", 0.0)


# ---------------------------------------------------------------------------
# run_benchmark: submit, fail-fast, budget
# ---------------------------------------------------------------------------


def test_run_benchmark_submits_and_returns_handle(
    iso_ledger: Any, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: Dict[str, Any] = {}

    async def _resolve() -> HostCoords:
        return HostCoords(port=9999, token="tok", pid=1)

    async def _submit(coords: HostCoords, task_id: str, suite: str) -> Dict[str, Any]:
        captured["task_id"] = task_id
        captured["suite"] = suite
        return {"status": "accepted", "task_id": task_id}

    monkeypatch.setattr(handlers, "resolve_host_or_error", _resolve)
    monkeypatch.setattr(handlers, "_submit_benchmark_loopback", _submit)

    payload = _dispatch("run_benchmark", {"suite": "v1"})
    assert payload["status"] == "ok"
    assert payload["result"]["status"] == "submitted"
    assert payload["result"]["task_id"] == captured["task_id"]
    assert payload["result"]["then"] == "get_report"
    assert captured["suite"] == "v1"


def test_run_benchmark_fails_fast_when_host_down(
    iso_ledger: Any, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _no_host() -> HostCoords:
        raise HostNotRunningError("not running")

    monkeypatch.setattr(handlers, "resolve_host_or_error", _no_host)

    payload = _dispatch("run_benchmark", {})
    assert payload["status"] == "error"
    assert payload["reason"] == "host_unavailable"
    assert _consumed() == pytest.approx(0.0)  # never charged when the host is down


def test_run_benchmark_charges_upfront_and_refunds_on_host_failure(
    iso_ledger: Any, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _resolve() -> HostCoords:
        return HostCoords(port=9999, token="tok", pid=1)

    async def _submit_fail(coords: HostCoords, task_id: str, suite: str) -> Dict[str, Any]:
        raise httpx.ConnectError("loopback boom")

    monkeypatch.setattr(handlers, "resolve_host_or_error", _resolve)
    monkeypatch.setattr(handlers, "_submit_benchmark_loopback", _submit_fail)

    payload = _dispatch("run_benchmark", {})
    assert payload["status"] == "error"  # the loopback error surfaced
    assert _consumed() == pytest.approx(0.0)  # charge fully refunded

    # A successful submit leaves exactly one charge on the books.
    async def _submit_ok(coords: HostCoords, task_id: str, suite: str) -> Dict[str, Any]:
        return {"status": "accepted", "task_id": task_id}

    monkeypatch.setattr(handlers, "_submit_benchmark_loopback", _submit_ok)
    _dispatch("run_benchmark", {})
    assert _consumed() == pytest.approx(1.0)


def test_run_benchmark_over_budget_is_denied(
    iso_ledger: Any, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AILIENANT_GATEWAY_BUDGET", "0.5")
    asyncio.run(ledger.consume_budget("anonymous", 1.0))  # already over the ceiling

    payload = _dispatch("run_benchmark", {})
    assert payload["status"] == "denied"
    assert payload["reason"] == "budget_exceeded"


def test_run_benchmark_busy_refunds_and_reports(
    iso_ledger: Any, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _resolve() -> HostCoords:
        return HostCoords(port=9999, token="tok", pid=1)

    async def _submit_busy(coords: HostCoords, task_id: str, suite: str) -> Dict[str, Any]:
        return {"status": "busy", "task_id": task_id}

    monkeypatch.setattr(handlers, "resolve_host_or_error", _resolve)
    monkeypatch.setattr(handlers, "_submit_benchmark_loopback", _submit_busy)

    payload = _dispatch("run_benchmark", {})
    assert payload["status"] == "ok"
    assert payload["result"]["reason"] == "benchmark_busy"
    assert _consumed() == pytest.approx(0.0)  # busy run refunded to net zero


# ---------------------------------------------------------------------------
# get_report
# ---------------------------------------------------------------------------


def test_get_report_passthrough(
    iso_ledger: Any, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _resolve() -> HostCoords:
        return HostCoords(port=9999, token=None, pid=1)

    async def _get(coords: HostCoords, task_id: str) -> Any:
        return {"status": "completed", "task_id": task_id, "report": {"ok": True}}

    monkeypatch.setattr(handlers, "resolve_host_or_error", _resolve)
    monkeypatch.setattr(handlers, "_get_report_loopback", _get)

    payload = _dispatch("get_report", {"task_id": "abc"})
    assert payload["status"] == "ok"
    assert payload["result"]["status"] == "completed"


def test_get_report_rejects_missing_task_id(iso_ledger: Any, registered: None) -> None:
    payload = _dispatch("get_report", {})
    assert payload["status"] == "error"
    assert payload["reason"] == "invalid_arguments"
    assert payload["missing"] == ["task_id"]


# ---------------------------------------------------------------------------
# Host benchmark service: path safety, suite allowlist, single-flight, records
# ---------------------------------------------------------------------------


def test_artifact_path_rejects_traversal(iso_bench: Any) -> None:
    with pytest.raises(ValueError):
        benchmark_service.read_report("../../etc/passwd")
    with pytest.raises(ValueError):
        benchmark_service._resolve_artifact("not-hex-id")

    path = benchmark_service._resolve_artifact(_HEX32)
    assert path.resolve().is_relative_to(iso_bench.resolve())


def test_run_benchmark_rejects_unknown_suite(iso_bench: Any) -> None:
    with pytest.raises(ValueError):
        benchmark_service.try_reserve("../../x")
    with pytest.raises(ValueError):
        benchmark_service.try_reserve("not_a_corpus")
    assert benchmark_service.try_reserve("v1") is True  # the allowlisted corpus


def test_benchmark_service_runs_and_writes_report(
    iso_bench: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        benchmark_service, "_runner_factory", lambda _root: _StubRunner(_canned_report())
    )
    task_id = "a" * 32
    asyncio.run(benchmark_service.run_benchmark(task_id, "v1"))

    res = benchmark_service.read_report(task_id)
    assert res["status"] == "completed"
    validate_report(res["report"])
    assert (iso_bench / f"{task_id}.json").exists()


def test_failed_run_keeps_record(
    iso_bench: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Boom:
        async def run_report(self, _problems: Any) -> Any:
            raise RuntimeError("kaboom")

    monkeypatch.setattr(benchmark_service, "_runner_factory", lambda _root: _Boom())
    task_id = "b" * 32
    asyncio.run(benchmark_service.run_benchmark(task_id, "v1"))

    res = benchmark_service.read_report(task_id)
    assert res["status"] == "failed"  # the failure record is kept, not cleared
    assert "kaboom" in res["detail"]
    assert not (iso_bench / f"{task_id}.json").exists()


def test_single_flight_rejects_second_run(iso_bench: Any) -> None:
    assert benchmark_service.try_reserve("v1") is True
    assert benchmark_service.try_reserve("v1") is False  # at the cap of 1
    benchmark_service.release_flight()
    assert benchmark_service.try_reserve("v1") is True


# ---------------------------------------------------------------------------
# Host endpoint: register/auto-deregister + leak-proof slot release
# ---------------------------------------------------------------------------


def test_benchmark_submit_registers_then_auto_deregisters(
    iso_bench: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import main

    gate = asyncio.Event()

    class _Hold:
        async def run_report(self, _problems: Any) -> BenchmarkReport:
            await gate.wait()
            return _canned_report()

    async def _scenario() -> None:
        monkeypatch.setattr(benchmark_service, "_runner_factory", lambda _root: _Hold())
        task_id = "c" * 32
        ack = await main.submit_benchmark(
            main.BenchmarkSubmitPayload(suite="v1"), x_task_id=task_id
        )
        assert ack["status"] == "accepted"
        t = main.task_service._active_tasks[task_id]
        assert main.task_service.get_task_status(task_id)["status"] == "running"
        assert benchmark_service._inflight == 1

        gate.set()
        await t
        await asyncio.sleep(0)  # let the done-callbacks run

        assert task_id not in main.task_service._active_tasks
        assert benchmark_service._inflight == 0

    asyncio.run(_scenario())


def test_inflight_released_when_registration_fails(
    iso_bench: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import main

    never = asyncio.Event()  # the held run is cancelled, never completes normally

    class _Hold:
        async def run_report(self, _problems: Any) -> BenchmarkReport:
            await never.wait()
            return _canned_report()

    def _boom_register(_sid: str, _t: Any) -> None:
        raise RuntimeError("register failed")

    async def _scenario() -> None:
        monkeypatch.setattr(benchmark_service, "_runner_factory", lambda _root: _Hold())
        monkeypatch.setattr(main.task_service, "register_active_task", _boom_register)

        with pytest.raises(RuntimeError):
            await main.submit_benchmark(
                main.BenchmarkSubmitPayload(suite="v1"), x_task_id="d" * 32
            )

        # The reserved slot is released by the cancelled task's done-callback.
        for _ in range(200):
            if benchmark_service._inflight == 0:
                break
            await asyncio.sleep(0.005)
        assert benchmark_service._inflight == 0
        assert benchmark_service.try_reserve("v1") is True  # not bricked busy

    asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# Refund must not mask the original error; refund floors at zero
# ---------------------------------------------------------------------------


def test_refund_does_not_mask_loopback_error(
    iso_ledger: Any, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _resolve() -> HostCoords:
        return HostCoords(port=9999, token="tok", pid=1)

    async def _submit(coords: HostCoords, task_id: str, suite: str) -> Dict[str, Any]:
        raise httpx.ConnectError("loopback boom")

    async def _consume(caller: str, amount: float) -> None:
        if amount < 0:
            raise RuntimeError("ledger locked on refund")  # the refund itself faults

    monkeypatch.setattr(handlers, "resolve_host_or_error", _resolve)
    monkeypatch.setattr(handlers, "_submit_benchmark_loopback", _submit)
    monkeypatch.setattr(handlers.ledger, "consume_budget", _consume)

    payload = _dispatch("run_benchmark", {})
    assert payload["status"] == "error"
    assert payload["reason"] == "handler_error"
    assert "loopback boom" in payload["detail"]  # original error, not the refund fault


def test_ledger_refund_floors_at_zero(iso_ledger: Any) -> None:
    asyncio.run(ledger.consume_budget("anonymous", 5.0))
    asyncio.run(ledger.consume_budget("anonymous", -100.0))  # over-refund
    assert _consumed() == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# End-to-end (the DoD): trigger a benchmark and retrieve the report
# ---------------------------------------------------------------------------


def test_end_to_end_trigger_and_retrieve(
    iso_ledger: Any, iso_bench: Any, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import main

    monkeypatch.setattr(
        benchmark_service, "_runner_factory", lambda _root: _StubRunner(_canned_report())
    )

    async def _resolve() -> HostCoords:
        return HostCoords(port=9999, token=None, pid=1)

    async def _submit(coords: HostCoords, task_id: str, suite: str) -> Any:
        return await main.submit_benchmark(
            main.BenchmarkSubmitPayload(suite=suite), x_task_id=task_id
        )

    async def _get(coords: HostCoords, task_id: str) -> Any:
        return await main.benchmark_report(task_id)

    monkeypatch.setattr(handlers, "resolve_host_or_error", _resolve)
    monkeypatch.setattr(handlers, "_submit_benchmark_loopback", _submit)
    monkeypatch.setattr(handlers, "_get_report_loopback", _get)

    async def _flow() -> None:
        triggered = await server.dispatch_call("run_benchmark", {"suite": "v1"})
        payload = json.loads(triggered[0].text)
        assert payload["status"] == "ok"
        task_id = payload["result"]["task_id"]

        t = main.task_service._active_tasks.get(task_id)
        if t is not None:
            await t
        await asyncio.sleep(0)

        report_env = json.loads(
            (await server.dispatch_call("get_report", {"task_id": task_id}))[0].text
        )
        assert report_env["status"] == "ok"
        assert report_env["result"]["status"] == "completed"
        validate_report(report_env["result"]["report"])

    asyncio.run(_flow())
