"""Tests for the memory dashboard REST surface — embedding browser, purge, and
the raised graph node cap. The vector store is stubbed so these stay hermetic
(no LanceDB round-trip); the goal is the endpoint contract: validation, Python-side
pagination/sort, purge idempotency, and the widened max_nodes band.
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List

import pytest
from fastapi.testclient import TestClient

VALID_PID = "proj_abc-123"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    from main import app
    with TestClient(app) as c:
        yield c


def _meta_rows(n: int) -> List[Dict[str, Any]]:
    return [
        {
            "file_path": f"/p/f{i}.py",
            "content_snippet": f"snippet-{i}",
            "token_count": 100 - i,
            "indexed_at": f"2026-07-{i + 1:02d}",
        }
        for i in range(n)
    ]


# ── Embedding list: validation ──────────────────────────────────────────────

def test_embeddings_rejects_bad_project_id(client: TestClient) -> None:
    resp = client.get("/api/v1/memory/embeddings", params={"project_id": "bad id!!"})
    assert resp.status_code == 400


def test_embeddings_rejects_unknown_sort(client: TestClient) -> None:
    resp = client.get(
        "/api/v1/memory/embeddings",
        params={"project_id": VALID_PID, "sort": "vector"},
    )
    assert resp.status_code == 422


def test_embeddings_rejects_bad_order(client: TestClient) -> None:
    resp = client.get(
        "/api/v1/memory/embeddings",
        params={"project_id": VALID_PID, "order": "sideways"},
    )
    assert resp.status_code == 422


# ── Embedding list: pagination + sort ───────────────────────────────────────

def test_embeddings_sorts_desc_and_limits(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.memory.semantic_memory import SemanticMemoryManager

    async def fake_list(self: Any, workspace_hash: str, folder_prefix: str = "", max_rows: int = 50000) -> List[Dict[str, Any]]:
        assert workspace_hash == VALID_PID
        return _meta_rows(5)

    monkeypatch.setattr(SemanticMemoryManager, "list_embeddings", fake_list)

    resp = client.get(
        "/api/v1/memory/embeddings",
        params={"project_id": VALID_PID, "sort": "token_count", "order": "desc", "offset": 0, "limit": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert body["offset"] == 0 and body["limit"] == 2
    assert len(body["rows"]) == 2
    # f0 has the highest token_count (100), f1 next (99).
    assert body["rows"][0]["token_count"] == 100
    assert body["rows"][0]["label"] == "f0.py"
    assert body["rows"][1]["token_count"] == 99


def test_embeddings_offset_slices_window(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.memory.semantic_memory import SemanticMemoryManager

    async def fake_list(self: Any, workspace_hash: str, folder_prefix: str = "", max_rows: int = 50000) -> List[Dict[str, Any]]:
        return _meta_rows(5)

    monkeypatch.setattr(SemanticMemoryManager, "list_embeddings", fake_list)

    resp = client.get(
        "/api/v1/memory/embeddings",
        params={"project_id": VALID_PID, "sort": "indexed_at", "order": "asc", "offset": 3, "limit": 10},
    )
    body = resp.json()
    assert body["total"] == 5
    assert len(body["rows"]) == 2  # only rows 3 and 4 remain past the offset
    assert body["rows"][0]["indexed_at"] == "2026-07-04"


# ── Purge: validation + idempotency ─────────────────────────────────────────

def test_purge_requires_confirm(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/memory/embeddings/purge",
        json={"project_id": VALID_PID, "file_path": "/p/f.py", "confirm": False},
    )
    assert resp.status_code == 422


def test_purge_rejects_bad_project_id(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/memory/embeddings/purge",
        json={"project_id": "bad!!", "file_path": "/p/f.py", "confirm": True},
    )
    assert resp.status_code == 400


def test_purge_is_idempotent(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.memory.semantic_memory import SemanticMemoryManager

    calls: List[tuple[str, str]] = []

    async def fake_delete(self: Any, file_path: str, workspace_hash: str) -> None:
        calls.append((file_path, workspace_hash))

    monkeypatch.setattr(SemanticMemoryManager, "semantic_delete", fake_delete)

    payload = {"project_id": VALID_PID, "file_path": "/p/gone.py", "confirm": True}
    r1 = client.post("/api/v1/memory/embeddings/purge", json=payload)
    r2 = client.post("/api/v1/memory/embeddings/purge", json=payload)

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["ok"] is True
    # Both dispatch; deleting an already-absent row is a no-op, so retrying is safe.
    assert calls == [("/p/gone.py", VALID_PID), ("/p/gone.py", VALID_PID)]


# ── Graph cap-raise ─────────────────────────────────────────────────────────

def test_graph_cap_raise_band(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import api.memory_dashboard as md

    async def fake_edges(project_id: str) -> List[Any]:
        return []

    async def fake_ppr(ids: Any, project_id: str) -> Dict[str, float]:
        return {}

    async def fake_comm(ids: Any, project_id: str) -> Dict[str, int]:
        return {}

    monkeypatch.setattr(md.catalog_db, "get_graph_edges_enriched", fake_edges)
    monkeypatch.setattr(md.catalog_db, "get_ppr_scores_bulk", fake_ppr)
    monkeypatch.setattr(md.catalog_db, "get_community_ids_bulk", fake_comm)

    # A value in the newly-allowed 2000..5000 band now validates.
    ok = client.get("/api/v1/memory/graph", params={"project_id": VALID_PID, "max_nodes": 4000})
    assert ok.status_code == 200
    # Beyond the new ceiling is still rejected by the query validator.
    over = client.get("/api/v1/memory/graph", params={"project_id": VALID_PID, "max_nodes": 6000})
    assert over.status_code == 422
