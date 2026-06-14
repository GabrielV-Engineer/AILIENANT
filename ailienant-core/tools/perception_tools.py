"""Phase 5.3 — ReadOnly perception-tool bundle.

Five new LangChain BaseTool subclasses + a schema-registration helper:

    DocumentParserTool       — Parse PDF / CSV / DOCX payloads in RAM.
    InspectASTNodeTool       — Extract a class/function source by name via tree-sitter.
    GetSymbolReferencesTool  — File-level inbound dependents (1-hop reverse import).
    TraceDataFlowTool        — Forward + backward k-hop reachability over the graph.
    WebFetchTool             — HTTP fetch + HTML→Markdown conversion (5 s timeout).

Every tool wraps untrusted output in <{boundary_id}>...</{boundary_id}> (the
Phase 5.1.1 Cognitive Quarantine tag). `register_perception_tools(store)` pushes
all five schemas into the Tool RAG store with privilege_tier=READ_ONLY.

Heavy dependencies (pypdf, markdownify, httpx) are imported lazily inside the
tool body so module load stays fast (mentor's audit constraint).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import csv
import io
import json
import logging
import zipfile
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Literal,
    Optional,
    Type,
)
from xml.etree import ElementTree as ET

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from core.permissions import ToolPrivilegeTier
from core.tool_rag import ToolRAGStore, ToolSchema
from tools.quarantine import wrap_boundary

logger = logging.getLogger("PERCEPTION_TOOLS")


# =====================================================================
# Shared helpers
# =====================================================================


_ALLOWED_PERCEPTION_ROLES = frozenset(
    {
        "core_dev",
        "architect_refactor",
        "qa_tester",
        "secops",
        "doc_manager",
        "data_ml_engineer",
    }
)


# Canonical Cognitive-Quarantine wrapper lives in tools.quarantine; this alias
# preserves the original private call sites in this module unchanged.
_wrap_boundary = wrap_boundary


# =====================================================================
# Task B — DocumentParserTool
# =====================================================================


class DocumentParserInput(BaseModel):
    mime_type: Literal[
        "text/csv",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ] = Field(description="Document MIME type.")
    payload_b64: str = Field(description="Base64-encoded file bytes (no disk I/O).")


class DocumentParserTool(BaseTool):
    """Parse a PDF / CSV / DOCX payload into plain text. No disk I/O.

    Output is wrapped in the Cognitive Quarantine boundary tag so the model
    treats extracted content as inert data. PDF column ordering and DOCX
    table fidelity are best-effort — see DEV_JOURNAL tech-debt entries.
    """

    name: str = "document_parser"
    description: str = (
        "Parse a PDF, CSV, or DOCX payload (base64) into plain text without "
        "touching disk. Returns the extracted text wrapped in the Cognitive "
        "Quarantine tag. PDF column order and DOCX tables are best-effort."
    )
    args_schema: Type[BaseModel] = DocumentParserInput

    _boundary_provider: Optional[Callable[[], str]] = PrivateAttr(default=None)

    def __init__(self, *, boundary_provider: Optional[Callable[[], str]] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._boundary_provider = boundary_provider

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("DocumentParserTool is async-only — use _arun().")

    async def _arun(self, mime_type: str, payload_b64: str) -> str:
        try:
            payload = base64.b64decode(payload_b64, validate=True)
        except (ValueError, binascii.Error) as exc:
            return _wrap_boundary(
                f"[document_parser] ERROR: invalid base64 payload: {exc}",
                self._boundary_provider,
            )

        try:
            if mime_type == "text/csv":
                text = self._parse_csv(payload)
            elif mime_type == "application/pdf":
                text = self._parse_pdf(payload)
            elif mime_type == (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ):
                text = self._parse_docx(payload)
            else:
                return _wrap_boundary(
                    f"[document_parser] ERROR: unsupported mime_type {mime_type!r}.",
                    self._boundary_provider,
                )
        except Exception as exc:  # noqa: BLE001 — surface parse errors to the agent
            logger.warning("DocumentParserTool failed for %s: %s", mime_type, exc)
            return _wrap_boundary(
                f"[document_parser] ERROR parsing {mime_type}: {exc}",
                self._boundary_provider,
            )

        return _wrap_boundary(text, self._boundary_provider)

    @staticmethod
    def _parse_csv(payload: bytes) -> str:
        text = payload.decode("utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text))
        return "\n".join(",".join(row) for row in reader)

    @staticmethod
    def _parse_pdf(payload: bytes) -> str:
        try:
            import pypdf  # lazy: only loaded when a PDF arrives
        except ImportError as exc:
            raise RuntimeError(
                "PDF parsing requires the 'pypdf' package. "
                "Add `pypdf` to requirements.txt and reinstall."
            ) from exc
        reader = pypdf.PdfReader(io.BytesIO(payload))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)

    @staticmethod
    def _parse_docx(payload: bytes) -> str:
        # Pure-stdlib DOCX extraction — DOCX is a zipped XML container.
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            with zf.open("word/document.xml") as doc:
                tree = ET.parse(doc)
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        chunks: List[str] = []
        for paragraph in tree.iter(f"{ns}p"):
            text_parts = [t.text or "" for t in paragraph.iter(f"{ns}t")]
            line = "".join(text_parts).strip()
            if line:
                chunks.append(line)
        return "\n".join(chunks)


# =====================================================================
# Task C — InspectASTNodeTool
# =====================================================================


class InspectASTInput(BaseModel):
    file_path: str = Field(description="Workspace-relative path of the source file.")
    symbol: str = Field(description="Class or function name to extract.")
    language_id: str = Field(description="VS Code language id (e.g. 'python', 'typescript').")


_AST_DEFINITION_NODE_TYPES = {
    "class_definition",
    "function_definition",
    "method_definition",
    "class_declaration",
    "function_declaration",
}

_DOCSTRING_TRUNCATION_LINE_THRESHOLD = 30


class InspectASTNodeTool(BaseTool):
    """Return the clean source of a class or function by name.

    Walks the tree-sitter AST built by core.ast_engine.ASTEngine. Strips
    oversized top-level docstrings (> 30 lines). Wraps the result in the
    Cognitive Quarantine tag.
    """

    name: str = "inspect_ast_node"
    description: str = (
        "Extract the source code of a class or function in a workspace file by "
        "its symbol name. Returns clean source with oversized docstrings truncated."
    )
    args_schema: Type[BaseModel] = InspectASTInput

    _vfs_read: Callable[[str], Optional[str]] = PrivateAttr()
    _ast_engine: Any = PrivateAttr()
    _boundary_provider: Optional[Callable[[], str]] = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        vfs_read: Callable[[str], Optional[str]],
        ast_engine: Any,
        boundary_provider: Optional[Callable[[], str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vfs_read = vfs_read
        self._ast_engine = ast_engine
        self._boundary_provider = boundary_provider

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("InspectASTNodeTool is async-only — use _arun().")

    async def _arun(self, file_path: str, symbol: str, language_id: str) -> str:
        content = self._vfs_read(file_path)
        if content is None:
            return _wrap_boundary(
                f"[inspect_ast_node] ERROR: '{file_path}' not in VFS.",
                self._boundary_provider,
            )
        tree = self._ast_engine.parse(file_path, content, language_id)
        if tree is None:
            return _wrap_boundary(
                f"[inspect_ast_node] ERROR: unsupported language {language_id!r} or parse failed.",
                self._boundary_provider,
            )
        node = self._find_definition(tree.root_node, symbol)
        if node is None:
            return _wrap_boundary(
                f"[inspect_ast_node] ERROR: symbol {symbol!r} not found in {file_path}.",
                self._boundary_provider,
            )
        source = content.encode("utf-8")[node.start_byte : node.end_byte].decode(
            "utf-8", errors="replace"
        )
        source = self._truncate_oversized_docstring(source)
        return _wrap_boundary(source, self._boundary_provider)

    @classmethod
    def _find_definition(cls, root: Any, symbol: str) -> Optional[Any]:
        """Breadth-first walk for the first definition node whose name == symbol."""
        queue: List[Any] = list(root.children)
        while queue:
            node = queue.pop(0)
            if node.type in _AST_DEFINITION_NODE_TYPES:
                name = cls._node_name(node)
                if name == symbol:
                    return node
            queue.extend(node.children)
        return None

    @staticmethod
    def _node_name(node: Any) -> Optional[str]:
        """Return the identifier child's text, or None if no identifier."""
        for child in node.children:
            if child.type in {"identifier", "type_identifier", "property_identifier"}:
                text = child.text
                if isinstance(text, bytes):
                    return text.decode("utf-8", errors="replace")
                return str(text) if text is not None else None
        return None

    @staticmethod
    def _truncate_oversized_docstring(source: str) -> str:
        """If the first statement is a long docstring, replace it with a placeholder."""
        lines = source.splitlines(keepends=True)
        if len(lines) < 2:
            return source
        # Heuristic: docstring lines start with """ or '''
        stripped = lines[1].lstrip() if len(lines) > 1 else ""
        if not (stripped.startswith('"""') or stripped.startswith("'''")):
            return source
        # Find closing delimiter
        opener = stripped[:3]
        for end_idx in range(2, len(lines)):
            if opener in lines[end_idx]:
                length = end_idx - 1  # docstring body lines
                if length > _DOCSTRING_TRUNCATION_LINE_THRESHOLD:
                    return lines[0] + f'    {opener}<docstring truncated>{opener}\n' + "".join(
                        lines[end_idx + 1 :]
                    )
                break
        return source


# =====================================================================
# Task D — GetSymbolReferencesTool
# =====================================================================


class GetSymbolReferencesInput(BaseModel):
    target_file_path: str = Field(
        description="Workspace-relative path. Returns files that import this path."
    )
    project_id: str = Field(default="", description="Project id (default empty).")


class GetSymbolReferencesTool(BaseTool):
    """1-hop backward dependents of the given file.

    Phase 5.3 ships file-level only (GraphRAG edges are file→file imports).
    Symbol-level cross-file references land in Phase 5.6+. See DEV_JOURNAL.
    """

    name: str = "get_symbol_references"
    description: str = (
        "Return files in the workspace that import a given file (1-hop "
        "backward edges). Replaces shell `grep` for refactor impact analysis."
    )
    args_schema: Type[BaseModel] = GetSymbolReferencesInput

    _get_dependents: Callable[[str, str], Any] = PrivateAttr()
    _boundary_provider: Optional[Callable[[], str]] = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        get_dependents: Callable[[str, str], Any],
        boundary_provider: Optional[Callable[[], str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._get_dependents = get_dependents
        self._boundary_provider = boundary_provider

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("GetSymbolReferencesTool is async-only — use _arun().")

    async def _arun(self, target_file_path: str, project_id: str = "") -> str:
        dependents = await self._get_dependents(target_file_path, project_id)
        if not dependents:
            text = f"No files reference {target_file_path!r} in project_id={project_id!r}."
        else:
            text = "\n".join(f"- {p}" for p in dependents)
        return _wrap_boundary(text, self._boundary_provider)


# =====================================================================
# Task E — TraceDataFlowTool
# =====================================================================


_TRACE_DATA_FLOW_MAX_DEPTH = 5


class TraceDataFlowInput(BaseModel):
    file_path: str = Field(description="Seed file.")
    depth: int = Field(default=2, description="k-hop depth (1..5).")


class TraceDataFlowTool(BaseTool):
    """Forward + backward k-hop reachability over the dependency graph.

    Returns the set of files that the seed transitively imports (forward)
    and the set that transitively import the seed (backward). Useful before
    a refactor to predict collateral side-effects. File-level only in 5.3.
    """

    name: str = "trace_data_flow"
    description: str = (
        "k-hop forward + backward reachability over the dependency graph. "
        "Use before refactors to predict collateral impact across files."
    )
    args_schema: Type[BaseModel] = TraceDataFlowInput

    _extractor: Any = PrivateAttr()
    _boundary_provider: Optional[Callable[[], str]] = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        extractor: Any,
        boundary_provider: Optional[Callable[[], str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._extractor = extractor
        self._boundary_provider = boundary_provider

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("TraceDataFlowTool is async-only — use _arun().")

    async def _arun(self, file_path: str, depth: int = 2) -> str:
        capped_depth = max(1, min(int(depth), _TRACE_DATA_FLOW_MAX_DEPTH))
        forward = await self._extractor.bfs_k_hop_forward(file_path, capped_depth)
        backward = await self._extractor.bfs_k_hop_backward(file_path, capped_depth)
        payload: Dict[str, Any] = {
            "seed": file_path,
            "depth": capped_depth,
            "forward": list(forward),
            "backward": list(backward),
        }
        return _wrap_boundary(json.dumps(payload, indent=2), self._boundary_provider)


# =====================================================================
# Task F — WebFetchTool
# =====================================================================


_WEB_FETCH_TIMEOUT_SEC = 5.0
_WEB_FETCH_MAX_BYTES = 50_000  # cap raw text returned to model


class WebFetchInput(BaseModel):
    url: str = Field(description="HTTP/HTTPS URL of the documentation to fetch.")


class WebFetchTool(BaseTool):
    """Fetch a remote URL and convert HTML to clean Markdown.

    Hard 5 s timeout. Non-HTML responses returned as raw text (truncated).
    Network errors and non-2xx statuses degrade gracefully — never raise.
    """

    name: str = "web_fetch"
    description: str = (
        "Fetch a remote URL (HTTP/HTTPS). HTML responses are converted to "
        "clean Markdown; other content types returned as truncated raw text. "
        "Hard 5-second timeout."
    )
    args_schema: Type[BaseModel] = WebFetchInput

    _boundary_provider: Optional[Callable[[], str]] = PrivateAttr(default=None)

    def __init__(self, *, boundary_provider: Optional[Callable[[], str]] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._boundary_provider = boundary_provider

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("WebFetchTool is async-only — use _arun().")

    async def _arun(self, url: str) -> str:
        try:
            text = await asyncio.wait_for(self._fetch(url), timeout=_WEB_FETCH_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            return _wrap_boundary(
                f"[web_fetch] ERROR: timeout (>{_WEB_FETCH_TIMEOUT_SEC}s) for {url!r}.",
                self._boundary_provider,
            )
        except Exception as exc:  # noqa: BLE001 — network failures degrade gracefully
            return _wrap_boundary(
                f"[web_fetch] ERROR fetching {url!r}: {exc}",
                self._boundary_provider,
            )
        return _wrap_boundary(text, self._boundary_provider)

    @staticmethod
    async def _fetch(url: str) -> str:
        import httpx  # lazy: skip cost when WebFetchTool isn't used

        async with httpx.AsyncClient(timeout=httpx.Timeout(_WEB_FETCH_TIMEOUT_SEC)) as client:
            resp = await client.get(url, follow_redirects=True)

        if resp.status_code >= 400:
            return f"[web_fetch] HTTP {resp.status_code} for {url!r}."

        content_type = resp.headers.get("content-type", "").lower()
        body = resp.text[:_WEB_FETCH_MAX_BYTES]
        if "text/html" in content_type:
            try:
                import markdownify  # type: ignore[import-untyped]  # lazy

                return str(markdownify.markdownify(body, heading_style="ATX"))
            except Exception as exc:  # noqa: BLE001 — fall back to raw on conversion failure
                logger.warning("markdownify failed for %s: %s", url, exc)
                return body
        return body


# =====================================================================
# Task G — Schema registration helper
# =====================================================================


def _tool_schema(
    name: str,
    description: str,
    json_schema_class: Type[BaseModel],
    *,
    extra_roles: FrozenSet[str] = frozenset(),
) -> ToolSchema:
    """Build a READ_ONLY perception schema.

    `extra_roles` is unioned additively onto the base perception role set so a
    tool can be surfaced to an agent-level role (e.g. ``researcher``) without
    disturbing any existing assignment.
    """
    return ToolSchema(
        name=name,
        description=description,
        json_schema=json.dumps(json_schema_class.model_json_schema(), default=str),
        privilege_tier=ToolPrivilegeTier.READ_ONLY,
        allowed_roles=_ALLOWED_PERCEPTION_ROLES | extra_roles,
    )


# File-inspection tools are shared between the Researcher and Analyst roles.
# web_fetch is assigned to the Analyst only (not the Researcher).
_RESEARCHER_ROLE: FrozenSet[str] = frozenset({"researcher"})
_ANALYST_ROLE: FrozenSet[str] = frozenset({"analyst"})
_RESEARCHER_AND_ANALYST: FrozenSet[str] = frozenset({"researcher", "analyst"})


async def register_perception_tools(store: ToolRAGStore) -> int:
    """Register all 5 perception-tool schemas in the given store. Returns count.

    The function is exposed for tests and for the startup hook; it does NOT
    auto-register at module import.
    """
    schemas: List[ToolSchema] = [
        _tool_schema(
            "document_parser",
            "Parse a PDF, CSV, or DOCX payload into plain text without disk I/O.",
            DocumentParserInput,
            extra_roles=_RESEARCHER_ROLE,
        ),
        _tool_schema(
            "inspect_ast_node",
            "Extract the source code of a class or function by symbol name.",
            InspectASTInput,
            extra_roles=_RESEARCHER_AND_ANALYST,
        ),
        _tool_schema(
            "get_symbol_references",
            "Find files that import the given file (1-hop backward edges).",
            GetSymbolReferencesInput,
            extra_roles=_RESEARCHER_AND_ANALYST,
        ),
        _tool_schema(
            "trace_data_flow",
            "Forward + backward k-hop reachability over the dependency graph.",
            TraceDataFlowInput,
            extra_roles=_RESEARCHER_AND_ANALYST,
        ),
        _tool_schema(
            "web_fetch",
            "Fetch a URL and convert HTML to clean Markdown (5 s timeout).",
            WebFetchInput,
            extra_roles=_ANALYST_ROLE,
        ),
    ]
    for schema in schemas:
        await store.register_schema(schema)
    return len(schemas)
