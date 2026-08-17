# ailienant-core/tests/test_project_init.py
"""core/project_init.py — /init: on-demand AILIENANT.md drafting.

Mirrors tests/test_manual_dreaming.py's structure and injectable-seam style
(``budget_fn`` / ``overview_fn`` / ``llm_invoke``), since ``project_init.py``
deliberately mirrors ``brain/daemon.py``'s shape (budget gate -> overview ->
one LLM call -> guarded write). The write path is exercised against a real
``tmp_path`` filesystem — not mocked — because the pristine-vs-user-content
branching is the feature's core safety property: it must never overwrite a
human's own `AILIENANT.md`.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from core.project_init import (
    ProjectInitResult,
    _is_effectively_empty,
    _resolve_target,
    run_project_init,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _resp(text: str) -> SimpleNamespace:
    """Minimal litellm-shaped response: ``.choices[0].message.content``."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


_DRAFT_TEXT = (
    "## Stack & Conventions\n\n- Python 3.12, FastAPI\n\n"
    "## Always\n\n- Use type hints\n\n## Never\n\n- \n"
)


# ---------------------------------------------------------------------------
# _is_effectively_empty
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n\n  ",
        "<!-- just a comment -->",
        (
            "# AILIENANT Project Instructions\n\n"
            "## Stack & Conventions\n\n- \n\n## Always\n\n- \n\n## Never\n\n- \n"
        ),
    ],
)
def test_is_effectively_empty_true(text: str) -> None:
    assert _is_effectively_empty(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "## Stack & Conventions\n\n- Python 3.12, FastAPI\n",
        "Always add type hints.",
        "# Title\n\n- some bullet with real content",
    ],
)
def test_is_effectively_empty_false(text: str) -> None:
    assert _is_effectively_empty(text) is False


# ---------------------------------------------------------------------------
# _resolve_target
# ---------------------------------------------------------------------------


def test_resolve_target_no_file_defaults_to_dotdir(tmp_path: Path) -> None:
    target = _resolve_target(str(tmp_path))
    assert target == tmp_path / ".ailienant" / "AILIENANT.md"


def test_resolve_target_pristine_file_writes_in_place(tmp_path: Path) -> None:
    dotdir = tmp_path / ".ailienant"
    dotdir.mkdir()
    md = dotdir / "AILIENANT.md"
    md.write_text(
        "# AILIENANT Project Instructions\n\n## Stack & Conventions\n\n- \n",
        encoding="utf-8",
    )
    assert _resolve_target(str(tmp_path)) == md


def test_resolve_target_user_content_forks_to_generated(tmp_path: Path) -> None:
    dotdir = tmp_path / ".ailienant"
    dotdir.mkdir()
    md = dotdir / "AILIENANT.md"
    md.write_text("## Stack & Conventions\n\n- Python 3.12\n", encoding="utf-8")
    assert _resolve_target(str(tmp_path)) == dotdir / "AILIENANT.generated.md"


def test_resolve_target_prefers_dotdir_over_flat(tmp_path: Path) -> None:
    """When both `.ailienant/AILIENANT.md` and flat `AILIENANT.md` exist, the
    dotdir candidate wins — same priority order as core/project_instructions.py."""
    dotdir = tmp_path / ".ailienant"
    dotdir.mkdir()
    (dotdir / "AILIENANT.md").write_text("- \n", encoding="utf-8")
    (tmp_path / "AILIENANT.md").write_text("## Stack\n\n- flat, real content\n", encoding="utf-8")
    assert _resolve_target(str(tmp_path)) == dotdir / "AILIENANT.md"


# ---------------------------------------------------------------------------
# run_project_init — seam-driven
# ---------------------------------------------------------------------------


def _seams(
    *,
    overview: str = "Workspace root: demo\nsrc/app.py",
    invested: float = 0.0,
    invoke: Optional[Any] = None,
) -> Dict[str, Any]:
    captured: Dict[str, Any] = {"called": False, "messages": None}

    async def _default_invoke(messages: List[Dict[str, Any]], **kwargs: Any) -> Any:
        captured["called"] = True
        captured["messages"] = messages
        return _resp(_DRAFT_TEXT)

    return {
        "overview_fn": lambda root, **kw: overview,
        "budget_fn": lambda: {"estimated_invested_usd": invested},
        "llm_invoke": invoke or _default_invoke,
        "captured": captured,
    }


async def test_written_to_pristine_target(tmp_path: Path) -> None:
    seams = _seams()
    result = await run_project_init(
        "proj",
        workspace_root=str(tmp_path),
        session_id="init:c1",
        overview_fn=seams["overview_fn"],
        budget_fn=seams["budget_fn"],
        llm_invoke=seams["llm_invoke"],
    )
    assert result.status == "written"
    target = Path(result.path)
    assert target == tmp_path / ".ailienant" / "AILIENANT.md"
    assert target.read_text(encoding="utf-8").startswith("## Stack & Conventions")
    assert result.chars == len(_DRAFT_TEXT.strip())


async def test_existing_user_content_never_overwritten(tmp_path: Path) -> None:
    dotdir = tmp_path / ".ailienant"
    dotdir.mkdir()
    md = dotdir / "AILIENANT.md"
    original = "## Stack & Conventions\n\n- Hand-written by the user\n"
    md.write_text(original, encoding="utf-8")

    seams = _seams()
    result = await run_project_init(
        "proj",
        workspace_root=str(tmp_path),
        session_id="init:c1",
        overview_fn=seams["overview_fn"],
        budget_fn=seams["budget_fn"],
        llm_invoke=seams["llm_invoke"],
    )
    assert result.status == "written"
    assert Path(result.path) == dotdir / "AILIENANT.generated.md"
    # The original, user-authored file is byte-for-byte untouched.
    assert md.read_text(encoding="utf-8") == original


async def test_over_budget_refuses(tmp_path: Path) -> None:
    seams = _seams(invested=999.0)
    result = await run_project_init(
        "proj",
        workspace_root=str(tmp_path),
        session_id="init:c1",
        overview_fn=seams["overview_fn"],
        budget_fn=seams["budget_fn"],
        llm_invoke=seams["llm_invoke"],
    )
    assert result.status == "refused_budget"
    assert seams["captured"]["called"] is False
    assert not (tmp_path / ".ailienant" / "AILIENANT.md").exists()


async def test_empty_overview_skips(tmp_path: Path) -> None:
    seams = _seams(overview="")
    result = await run_project_init(
        "proj",
        workspace_root=str(tmp_path),
        session_id="init:c1",
        overview_fn=seams["overview_fn"],
        budget_fn=seams["budget_fn"],
        llm_invoke=seams["llm_invoke"],
    )
    assert result.status == "skipped_empty"
    assert seams["captured"]["called"] is False


async def test_blank_llm_content_skips(tmp_path: Path) -> None:
    async def _blank_invoke(messages: List[Dict[str, Any]], **kwargs: Any) -> Any:
        return _resp("   ")

    result = await run_project_init(
        "proj",
        workspace_root=str(tmp_path),
        session_id="init:c1",
        overview_fn=lambda root, **kw: "Workspace root: demo",
        budget_fn=lambda: {"estimated_invested_usd": 0.0},
        llm_invoke=_blank_invoke,
    )
    assert result.status == "skipped_empty"
    assert not (tmp_path / ".ailienant" / "AILIENANT.md").exists()


async def test_stale_snapshot_aborts_without_writing(tmp_path: Path) -> None:
    seams = _seams()
    result = await run_project_init(
        "proj",
        workspace_root=str(tmp_path),
        session_id="init:c1",
        overview_fn=seams["overview_fn"],
        budget_fn=seams["budget_fn"],
        llm_invoke=seams["llm_invoke"],
        stale_check=lambda: True,
    )
    assert result.status == "aborted_stale"
    assert seams["captured"]["called"] is True  # the LLM ran; only the commit was skipped
    assert not (tmp_path / ".ailienant" / "AILIENANT.md").exists()


async def test_llm_exception_propagates_uncaught(tmp_path: Path) -> None:
    """A real transport/LLM fault is NOT a business outcome — it propagates to
    the caller uncaught, exactly like OvernightDaemon.run_consolidation (the
    catch-and-log layer is main.py's _trigger_project_init wrapper, not this
    function)."""

    async def _raising_invoke(messages: List[Dict[str, Any]], **kwargs: Any) -> Any:
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        await run_project_init(
            "proj",
            workspace_root=str(tmp_path),
            session_id="init:c1",
            overview_fn=lambda root, **kw: "Workspace root: demo",
            budget_fn=lambda: {"estimated_invested_usd": 0.0},
            llm_invoke=_raising_invoke,
        )
    assert not (tmp_path / ".ailienant" / "AILIENANT.md").exists()


def test_result_is_frozen() -> None:
    r = ProjectInitResult("written", "/ws/.ailienant/AILIENANT.md", 42)
    with pytest.raises(Exception):
        r.status = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# WS contract round-trip (client_project_init / server_project_init_complete)
# ---------------------------------------------------------------------------


def test_client_project_init_event_round_trips() -> None:
    from api.ws_contracts import ClientProjectInitEvent, ProjectInitPayload

    event = ClientProjectInitEvent(data=ProjectInitPayload())
    raw = event.model_dump_json()
    restored = ClientProjectInitEvent.model_validate_json(raw)
    assert restored.event_type == "client_project_init"


def test_server_project_init_complete_event_round_trips() -> None:
    from api.ws_contracts import (
        ProjectInitCompletePayload,
        ServerProjectInitCompleteEvent,
    )

    event = ServerProjectInitCompleteEvent(
        data=ProjectInitCompletePayload(
            status="written", path="/ws/.ailienant/AILIENANT.md", chars=123
        )
    )
    raw = event.model_dump_json()
    restored = ServerProjectInitCompleteEvent.model_validate_json(raw)
    assert restored.event_type == "server_project_init_complete"
    assert restored.data.status == "written"
    assert restored.data.path == "/ws/.ailienant/AILIENANT.md"
    assert restored.data.chars == 123


def test_project_init_events_are_members_of_websocket_message_union() -> None:
    from api.ws_contracts import WebSocketMessage

    members = getattr(WebSocketMessage, "__args__", ())
    names = {m.__name__ for m in members}
    assert "ClientProjectInitEvent" in names
    assert "ServerProjectInitCompleteEvent" in names
