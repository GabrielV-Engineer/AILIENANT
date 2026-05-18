# ailienant-core/tests/test_perception_tools.py
#
# Phase 5.3 smoke tests for the 6 ReadOnly perception tools.
# Style mirrors test_mcp_handshake.py: AsyncMock + __aenter__/__aexit__ for
# httpx; PrivateAttr injections for tool callables; no global fixtures.

from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path
from typing import Any, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.ast_engine import ASTEngine
from core.permissions import ToolPrivilegeTier
from core.tool_rag import ToolRAGStore
from tools.agent_tools import make_read_file_tool
from tools.perception_tools import (
    DocumentParserTool,
    GetSymbolReferencesTool,
    InspectASTNodeTool,
    TraceDataFlowTool,
    WebFetchTool,
    register_perception_tools,
)

_BOUNDARY = "abc123def456abc123def456abc123de"


def _boundary_provider() -> str:
    return _BOUNDARY


# =====================================================================
# Task A — FileReadTool extension
# =====================================================================


def test_read_file_tool_basic_read() -> None:
    tool = make_read_file_tool(lambda _path: "line1\nline2\nline3\n")
    result = tool.invoke({"path": "any.py"})
    assert "line1\nline2\nline3" in result


def test_read_file_tool_offset_and_limit() -> None:
    tool = make_read_file_tool(lambda _path: "a\nb\nc\nd\ne\n")
    result = tool.invoke({"path": "x.txt", "offset": 1, "limit": 2})
    assert result == "b\nc\n"


def test_read_file_tool_missing_file() -> None:
    tool = make_read_file_tool(lambda _path: None)
    result = tool.invoke({"path": "missing.py"})
    assert "ERROR" in result


def test_read_file_tool_record_read_fires() -> None:
    records: List[Tuple[str, Any]] = []

    def recorder(path: str, vfs_file: Any) -> None:
        records.append((path, vfs_file))

    tool = make_read_file_tool(lambda _p: "hello", record_read=recorder)
    tool.invoke({"path": "src/foo.py"})
    assert len(records) == 1
    path, vfs_file = records[0]
    assert path == "src/foo.py"
    # VFSFile has blob_hash, document_version_id, is_dirty
    assert vfs_file.blob_hash
    assert vfs_file.document_version_id
    assert vfs_file.is_dirty is False


def test_read_file_tool_record_read_uses_vfs_stat_when_provided() -> None:
    captured: List[Any] = []

    def recorder(_path: str, vfs_file: Any) -> None:
        captured.append(vfs_file)

    def stat(_path: str) -> Optional[Tuple[str, str]]:
        return ("blobhash-from-stat", "version-from-stat")

    tool = make_read_file_tool(
        lambda _p: "data", vfs_stat=stat, record_read=recorder
    )
    tool.invoke({"path": "src/bar.py"})
    assert captured[0].blob_hash == "blobhash-from-stat"
    assert captured[0].document_version_id == "version-from-stat"


def test_read_file_tool_factory_without_record_read_backward_compat() -> None:
    """Existing callers that pass only `vfs_read` must still work."""
    tool = make_read_file_tool(lambda _p: "content")
    result = tool.invoke({"path": "x.py"})
    assert result == "content"


# =====================================================================
# Task B — DocumentParserTool
# =====================================================================


@pytest.mark.anyio
async def test_document_parser_csv() -> None:
    csv_bytes = b"name,age\nalice,30\nbob,25\n"
    payload = base64.b64encode(csv_bytes).decode()
    tool = DocumentParserTool(boundary_provider=_boundary_provider)
    out = await tool._arun(mime_type="text/csv", payload_b64=payload)
    assert _BOUNDARY in out
    assert "alice,30" in out
    assert "bob,25" in out


@pytest.mark.anyio
async def test_document_parser_docx() -> None:
    # Build a minimal DOCX zip in-memory with one paragraph.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "word/document.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>Phase 5.3 smoke text</w:t></w:r></w:p></w:body>'
                '</w:document>'
            ),
        )
    payload = base64.b64encode(buf.getvalue()).decode()
    tool = DocumentParserTool(boundary_provider=_boundary_provider)
    out = await tool._arun(
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        payload_b64=payload,
    )
    assert "Phase 5.3 smoke text" in out
    assert _BOUNDARY in out


@pytest.mark.anyio
async def test_document_parser_pdf() -> None:
    # Patch pypdf.PdfReader where DocumentParserTool imports it (inside method).
    fake_reader = MagicMock()
    fake_page = MagicMock()
    fake_page.extract_text.return_value = "PDF page text"
    fake_reader.pages = [fake_page, fake_page]
    fake_module = MagicMock()
    fake_module.PdfReader = MagicMock(return_value=fake_reader)

    payload = base64.b64encode(b"%PDF-1.4 fake bytes").decode()
    tool = DocumentParserTool(boundary_provider=_boundary_provider)

    with patch.dict("sys.modules", {"pypdf": fake_module}):
        out = await tool._arun(mime_type="application/pdf", payload_b64=payload)

    assert "PDF page text" in out
    assert _BOUNDARY in out


@pytest.mark.anyio
async def test_document_parser_invalid_base64() -> None:
    tool = DocumentParserTool(boundary_provider=_boundary_provider)
    out = await tool._arun(mime_type="text/csv", payload_b64="not!!!base64==")
    assert "ERROR" in out
    assert _BOUNDARY in out


@pytest.mark.anyio
async def test_document_parser_fallback_boundary_when_provider_absent() -> None:
    csv_bytes = b"x,y\n1,2\n"
    payload = base64.b64encode(csv_bytes).decode()
    tool = DocumentParserTool()  # no boundary_provider
    out = await tool._arun(mime_type="text/csv", payload_b64=payload)
    # boundary still wraps but is a freshly generated uuid (32 hex chars).
    assert out.startswith("<") and out.endswith(">")
    assert "1,2" in out


# =====================================================================
# Task C — InspectASTNodeTool
# =====================================================================


_PY_SAMPLE = '''\
"""Module docstring."""


def alpha():
    """Alpha doc."""
    return 1


class Beta:
    """Beta class doc."""

    def gamma(self):
        return 2
'''


@pytest.mark.anyio
async def test_inspect_ast_node_function() -> None:
    engine = ASTEngine()
    tool = InspectASTNodeTool(
        vfs_read=lambda _p: _PY_SAMPLE,
        ast_engine=engine,
        boundary_provider=_boundary_provider,
    )
    out = await tool._arun(file_path="x.py", symbol="alpha", language_id="python")
    assert "def alpha" in out
    assert "return 1" in out
    assert _BOUNDARY in out


@pytest.mark.anyio
async def test_inspect_ast_node_class() -> None:
    engine = ASTEngine()
    tool = InspectASTNodeTool(
        vfs_read=lambda _p: _PY_SAMPLE,
        ast_engine=engine,
        boundary_provider=_boundary_provider,
    )
    out = await tool._arun(file_path="x.py", symbol="Beta", language_id="python")
    assert "class Beta" in out
    assert "def gamma" in out


@pytest.mark.anyio
async def test_inspect_ast_node_missing_symbol() -> None:
    engine = ASTEngine()
    tool = InspectASTNodeTool(
        vfs_read=lambda _p: _PY_SAMPLE,
        ast_engine=engine,
        boundary_provider=_boundary_provider,
    )
    out = await tool._arun(file_path="x.py", symbol="missing", language_id="python")
    assert "ERROR" in out
    assert "not found" in out


@pytest.mark.anyio
async def test_inspect_ast_node_missing_file() -> None:
    engine = ASTEngine()
    tool = InspectASTNodeTool(
        vfs_read=lambda _p: None,
        ast_engine=engine,
        boundary_provider=_boundary_provider,
    )
    out = await tool._arun(file_path="missing.py", symbol="alpha", language_id="python")
    assert "not in VFS" in out


@pytest.mark.anyio
async def test_inspect_ast_node_unsupported_language() -> None:
    engine = ASTEngine()
    tool = InspectASTNodeTool(
        vfs_read=lambda _p: "anything",
        ast_engine=engine,
        boundary_provider=_boundary_provider,
    )
    out = await tool._arun(file_path="x.zzz", symbol="alpha", language_id="brainfuck")
    assert "ERROR" in out


# =====================================================================
# Task D — GetSymbolReferencesTool
# =====================================================================


@pytest.mark.anyio
async def test_get_symbol_references_with_dependents() -> None:
    mock = AsyncMock(return_value=["src/a.py", "src/b.py"])
    tool = GetSymbolReferencesTool(get_dependents=mock, boundary_provider=_boundary_provider)
    out = await tool._arun(target_file_path="src/foo.py")
    assert "src/a.py" in out
    assert "src/b.py" in out
    assert _BOUNDARY in out
    mock.assert_awaited_once_with("src/foo.py", "")


@pytest.mark.anyio
async def test_get_symbol_references_no_dependents() -> None:
    mock = AsyncMock(return_value=[])
    tool = GetSymbolReferencesTool(get_dependents=mock, boundary_provider=_boundary_provider)
    out = await tool._arun(target_file_path="orphan.py")
    assert "No files reference" in out
    assert _BOUNDARY in out


# =====================================================================
# Task E — TraceDataFlowTool
# =====================================================================


@pytest.mark.anyio
async def test_trace_data_flow_both_directions() -> None:
    extractor = MagicMock()
    extractor.bfs_k_hop_forward = AsyncMock(return_value=["src/dep1.py", "src/dep2.py"])
    extractor.bfs_k_hop_backward = AsyncMock(return_value=["src/caller.py"])
    tool = TraceDataFlowTool(extractor=extractor, boundary_provider=_boundary_provider)
    out = await tool._arun(file_path="src/foo.py", depth=2)
    assert '"forward"' in out
    assert '"backward"' in out
    assert "src/dep1.py" in out
    assert "src/caller.py" in out
    assert _BOUNDARY in out


@pytest.mark.anyio
async def test_trace_data_flow_depth_clamped() -> None:
    extractor = MagicMock()
    extractor.bfs_k_hop_forward = AsyncMock(return_value=[])
    extractor.bfs_k_hop_backward = AsyncMock(return_value=[])
    tool = TraceDataFlowTool(extractor=extractor, boundary_provider=_boundary_provider)
    await tool._arun(file_path="x.py", depth=99)
    # Forward call must use the capped depth (5), not 99.
    extractor.bfs_k_hop_forward.assert_awaited_once_with("x.py", 5)
    extractor.bfs_k_hop_backward.assert_awaited_once_with("x.py", 5)


@pytest.mark.anyio
async def test_trace_data_flow_minimum_depth() -> None:
    extractor = MagicMock()
    extractor.bfs_k_hop_forward = AsyncMock(return_value=[])
    extractor.bfs_k_hop_backward = AsyncMock(return_value=[])
    tool = TraceDataFlowTool(extractor=extractor, boundary_provider=_boundary_provider)
    await tool._arun(file_path="x.py", depth=0)
    extractor.bfs_k_hop_forward.assert_awaited_once_with("x.py", 1)


# =====================================================================
# Task F — WebFetchTool
# =====================================================================


def _make_response_mock(status_code: int, body: str, content_type: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = body
    resp.headers = {"content-type": content_type}
    return resp


def _patch_async_client(get_return: Any) -> Any:
    """Build the chain that httpx.AsyncClient() needs inside an `async with`."""
    client_instance = MagicMock()
    client_instance.get = AsyncMock(return_value=get_return) if not isinstance(
        get_return, Exception
    ) else AsyncMock(side_effect=get_return)
    client_instance.__aenter__ = AsyncMock(return_value=client_instance)
    client_instance.__aexit__ = AsyncMock(return_value=False)
    return client_instance


@pytest.mark.anyio
async def test_web_fetch_html_to_markdown() -> None:
    resp = _make_response_mock(200, "<h1>Hello</h1><p>World</p>", "text/html; charset=utf-8")
    client = _patch_async_client(resp)
    tool = WebFetchTool(boundary_provider=_boundary_provider)
    with patch("httpx.AsyncClient", return_value=client):
        out = await tool._arun(url="https://example.com/")
    # markdownify turns <h1> into "Hello\n=====" or "# Hello" depending on heading_style
    assert "Hello" in out
    assert "World" in out
    assert _BOUNDARY in out


@pytest.mark.anyio
async def test_web_fetch_non_html_returns_raw() -> None:
    resp = _make_response_mock(200, '{"k":"v"}', "application/json")
    client = _patch_async_client(resp)
    tool = WebFetchTool(boundary_provider=_boundary_provider)
    with patch("httpx.AsyncClient", return_value=client):
        out = await tool._arun(url="https://example.com/data.json")
    assert '"k":"v"' in out


@pytest.mark.anyio
async def test_web_fetch_4xx_status() -> None:
    resp = _make_response_mock(404, "Not Found", "text/html")
    client = _patch_async_client(resp)
    tool = WebFetchTool(boundary_provider=_boundary_provider)
    with patch("httpx.AsyncClient", return_value=client):
        out = await tool._arun(url="https://example.com/missing")
    assert "HTTP 404" in out


@pytest.mark.anyio
async def test_web_fetch_network_exception() -> None:
    client = _patch_async_client(ConnectionError("dns boom"))
    tool = WebFetchTool(boundary_provider=_boundary_provider)
    with patch("httpx.AsyncClient", return_value=client):
        out = await tool._arun(url="https://example.com/")
    assert "ERROR" in out
    assert "dns boom" in out


# =====================================================================
# Task G — register_perception_tools
# =====================================================================


def _isolated_store(tmp_path: Path) -> ToolRAGStore:
    import hashlib
    import struct

    async def fake_embed(text: str) -> List[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        floats: List[float] = []
        for i in range(8):
            chunk = digest[(i * 4) % len(digest) : (i * 4) % len(digest) + 4]
            if len(chunk) < 4:
                chunk = (chunk + b"\x00\x00\x00\x00")[:4]
            (val,) = struct.unpack("<f", chunk)
            floats.append(max(-1e3, min(1e3, val)))
        return floats

    return ToolRAGStore(
        embed_fn=fake_embed,
        store_path=str(tmp_path / "tool_rag"),
        embedding_dim=8,
        register_atexit_cleanup=False,
    )


@pytest.mark.anyio
async def test_register_perception_tools_registers_five_schemas(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    count = await register_perception_tools(store)
    assert count == 5
    schemas = store.all_schemas()
    names = {s.name for s in schemas}
    assert names == {
        "document_parser",
        "inspect_ast_node",
        "get_symbol_references",
        "trace_data_flow",
        "web_fetch",
    }


@pytest.mark.anyio
async def test_all_registered_schemas_are_read_only(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_perception_tools(store)
    for schema in store.all_schemas():
        assert schema.privilege_tier is ToolPrivilegeTier.READ_ONLY
        assert "core_dev" in schema.allowed_roles  # sanity check on role whitelist


# =====================================================================
# anyio backend constraint
# =====================================================================


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
