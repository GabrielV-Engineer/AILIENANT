"""Coder read-capability, tool-role strategy, and tool-debt sweep — checkpoint gate.

Every row asserts an END-TO-END property, never a helper in isolation. That is the
lesson this division was built on: `_read_file_ast` had a passing unit test while its
result was discarded before reaching the model, and the tool-role rows fail only at
dispatch, never at schema level.

Rows
  READ1-4    the cell's read primitive produces an observation the model receives
  HINT1      the advertised primitive list is derived from CELL_TOOLS
  PATH1-5    read_safe confines paths to the workspace root
  ROLE1-2    every newly granted tool ADMITs at ToolDispatcher.classify
  DERIVE1-3  converged role sets equal their derived expressions
  CONTRACT1  the legacy role whitelist and the live RBAC agree
  BUDGET1    the eager/deferred decision for core_dev matches the measurement
  CAP1-3     document_parser bounds payload, output, and decompression
  PIN1-3     web_fetch pins the validated address without weakening TLS
  WIRE1-2    task_list/task_stop reachable; deleted names in neither record
"""
from __future__ import annotations

import base64
import datetime
import http.server
import io
import ssl
import threading
import zipfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

import brain.agentic_cell as ac
from core.deferred_tool_loader import DeferredToolLoader
from core.path_guard import confine_to_root
from core.permissions import PermissionDecision, SessionPermissionMode, ToolPrivilegeTier
from core.tool_dispatch import RegisteredTool, ToolCall, ToolDispatcher
from core.tool_registry import _INTENTIONALLY_UNREGISTERED, all_registrable_names
from core.vfs_middleware import VFSMiddleware
from shared.rbac import (
    ALL_ROLES,
    resolve_dispatch_permission,
    CODE_NAVIGATION_ROLES,
    DEV_ROLES,
    GRAPH_SEMANTICS_ROLES,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# =====================================================================
# READ — the cell's read primitive actually reaches the model
# =====================================================================


def test_read1_read_file_ast_result_becomes_a_replayable_record() -> None:
    """A read must produce a trajectory record `_build_messages` replays.

    The defect this locks: the dispatch computed the skeleton and discarded it
    (`_ = _read_file_ast(...)`), so a tool the model was told to call returned
    nothing — and the OCC diagnostic pointed it right back at that tool.
    """
    observation = {"role": "system", "content": "[read_file_ast] a.py\ndef f(): ..."}
    state: Dict[str, Any] = {
        "user_input": "task",
        "agentic_trajectory": [
            {"iteration": 0, "edits": [], "occ_conflicts": [], "exit_code": None,
             "diagnostics": "", "status": "continue"},
            observation,
        ],
    }
    messages = ac._build_messages(state)
    assert any("[read_file_ast]" in m["content"] for m in messages), (
        "a read observation never reached the model's next-iteration messages"
    )


def test_read2_unopened_file_is_served_from_the_vfs_not_as_empty(tmp_path: Path) -> None:
    """A path absent from the working set falls through to the VFS.

    `working.get(path, "")` reported an unopened file as empty, which reads to a
    model as "this file has no content" rather than "you have not read it".
    """
    target = tmp_path / "mod.py"
    target.write_text("def hello():\n    return 1\n", encoding="utf-8")
    state = {"project_id": "p", "workspace_root": str(tmp_path), "task_id": "t"}

    source = ac._resolve_read_source(state, {}, str(target))
    assert source is not None and "def hello" in source


def test_read3_working_set_wins_over_disk(tmp_path: Path) -> None:
    """An in-flight edit must not be shadowed by the on-disk original."""
    target = tmp_path / "mod.py"
    target.write_text("def old(): ...\n", encoding="utf-8")
    state = {"project_id": "p", "workspace_root": str(tmp_path), "task_id": "t"}

    source = ac._resolve_read_source(state, {str(target): "def edited(): ..."}, str(target))
    assert source == "def edited(): ..."


def test_read4_non_python_file_yields_a_real_skeleton() -> None:
    """Language is resolved from the extension, not assumed Python.

    Every non-.py path used to be labelled "text", which the tree-sitter engine
    does not support — so it returned '' for half this repository.
    """
    ts_source = "export function add(a: number, b: number): number {\n  return a + b;\n}\n"
    out = ac._read_file_ast(ts_source, "calc.ts")
    assert out, "TypeScript produced no output at all"
    assert "add" in out


def test_read4b_unsupported_language_falls_back_to_bounded_head() -> None:
    """No skeleton is possible → a bounded head of the source, never ''."""
    body = "key = value\n" * 5000
    out = ac._read_file_ast(body, "settings.conf")
    assert out.startswith("key = value")
    assert len(out) <= ac._READ_FALLBACK_MAX_CHARS


# =====================================================================
# HINT — the advertised contract is derived, not restated
# =====================================================================


def test_hint1_primitive_list_is_derived_from_cell_tools() -> None:
    """Every primitive's name and args come from CELL_TOOLS itself."""
    signature = ac._cell_tools_signature()
    for model in ac.CELL_TOOLS:
        assert model.TOOL_NAME in signature
        for field in model.model_fields:
            assert field in signature, f"{model.TOOL_NAME} arg {field!r} missing"


# =====================================================================
# PATH — workspace confinement (§6.2)
# =====================================================================


@pytest.mark.parametrize(
    "relative",
    ["../outside.txt", "../../outside.txt", "sub/../../outside.txt"],
)
def test_path1_traversal_is_denied(tmp_path: Path, relative: str) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    assert confine_to_root(str(root / relative), str(root)) is not None


def test_path2_absolute_path_outside_root_is_denied(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY", encoding="utf-8")
    assert confine_to_root(str(secret), str(root)) is not None


def test_path3_in_root_path_is_allowed(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "pkg").mkdir(parents=True)
    assert confine_to_root(str(root / "pkg" / "a.py"), str(root)) is None
    assert confine_to_root(str(root), str(root)) is None


def test_path4_read_safe_refuses_an_escaping_path(tmp_path: Path) -> None:
    """The chokepoint answers with PATH_ESCAPE and never raises.

    read_safe was a CONTENT firewall only — ignore rules, binary, size — with no
    confinement, so an absolute path walked straight through every layer.
    """
    root = tmp_path / "ws"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("token=abc123", encoding="utf-8")

    result = VFSMiddleware().read_safe(str(secret), project_id="p", project_root=str(root))
    assert not result.ok
    assert result.error == "PATH_ESCAPE"
    assert result.content is None


def test_path5_rootless_call_is_admitted_by_design() -> None:
    """No root means no jail to enforce — documented, not an accidental hole."""
    assert confine_to_root("/anywhere/at/all", None) is None
    assert confine_to_root("/anywhere/at/all", "") is None


# =====================================================================
# ROLE — the grant is real at DISPATCH, not merely at schema level
# =====================================================================

_GRANTED_READ_TOOLS = [
    "find_symbol_callers", "get_dependents", "trace_cross_boundary",
    "architecture_digest", "query_graphrag", "grep", "glob",
    "workspace_structure", "read_file",
]


def _dispatcher(role: str, tools: Dict[str, RegisteredTool]) -> ToolDispatcher:
    return ToolDispatcher(
        tools=tools,
        active_role=role,
        session_mode=SessionPermissionMode.AUTO,
        state={"task_id": "gate"},
        agent_permission=resolve_dispatch_permission(role),
    )


@pytest.mark.parametrize("role", sorted(DEV_ROLES))
def test_role1_every_dev_role_is_admitted_for_the_granted_read_tools(role: str) -> None:
    """Admission is asserted at classify(), which is where the old gap failed.

    Widening a schema without widening its RegisteredTool leaves the model able to
    see a tool and unable to call it — a failure no schema-level assertion catches.
    """
    from tools.researcher_tools import build_researcher_tools

    tools = build_researcher_tools({"workspace_root": "/ws", "project_id": "p", "task_id": "t"})
    for name in _GRANTED_READ_TOOLS:
        registered = tools.get(name)
        if registered is None:
            continue  # read_file resolves through _simple_factories, covered by ROLE2
        _reg, decision, reason = _dispatcher(role, tools).classify(ToolCall(name=name, args={}))
        assert decision is not PermissionDecision.DENY, (
            f"{role} denied {name}: {reason}"
        )


@pytest.mark.anyio
async def test_role2_read_file_is_visible_and_resolvable_for_a_coder_role() -> None:
    """read_file is granted on BOTH sides: catalog visibility and a live factory."""
    from core.tool_registry import resolve_tools
    from core.tool_rag import ToolSchema

    schema = ToolSchema(
        name="read_file",
        description="read",
        json_schema="{}",
        privilege_tier=ToolPrivilegeTier.READ_ONLY,
        allowed_roles=CODE_NAVIGATION_ROLES,
    )
    assert "core_dev" in CODE_NAVIGATION_ROLES
    resolved = resolve_tools([schema], {"workspace_root": "/ws", "project_id": "p", "task_id": "t"})
    assert "read_file" in resolved


def test_role3_read_file_schema_and_executable_describe_the_same_tool() -> None:
    """The advertised window and the executed window must be the same window.

    Caught live while bounding the schema: `FormalisedReadFileInput` is schema-only
    and the executable lives in `tools/agent_tools.py`, so bounding one left the
    other returning whole files for a model that simply omitted `limit` — the same
    two-sided divergence as a schema-widened-but-builder-not role grant.
    """
    from shared.config import READ_FILE_DEFAULT_LINES, READ_FILE_MAX_LINES
    from tools.agent_tools import make_read_file_tool
    from tools.researcher_tools import FormalisedReadFileInput

    field = FormalisedReadFileInput.model_fields["limit"]
    assert field.default == READ_FILE_DEFAULT_LINES

    body = "".join(f"line {i}\n" for i in range(READ_FILE_MAX_LINES * 2))
    tool = make_read_file_tool(lambda _p: body)

    # Omitting `limit` must yield the default window, not the whole file.
    default_out = tool.invoke({"path": "big.py"})
    assert default_out.count("line ") <= READ_FILE_DEFAULT_LINES + 1
    assert "more line(s)" in default_out

    # An over-ceiling request is clamped, never honoured.
    huge_out = tool.invoke({"path": "big.py", "limit": READ_FILE_MAX_LINES * 10})
    assert huge_out.count("line ") <= READ_FILE_MAX_LINES + 1


# =====================================================================
# DERIVE — role sets equal their derived expressions (§5.7)
# =====================================================================


def test_derive1_perception_roles_equal_dev_roles() -> None:
    from tools.perception_tools import _ALLOWED_PERCEPTION_ROLES

    assert _ALLOWED_PERCEPTION_ROLES == DEV_ROLES


def test_derive2_capability_bundles_contain_every_dev_role() -> None:
    """A bundle that lost a dev role would silently re-open the original gap."""
    assert DEV_ROLES <= CODE_NAVIGATION_ROLES
    assert DEV_ROLES <= GRAPH_SEMANTICS_ROLES
    assert CODE_NAVIGATION_ROLES <= ALL_ROLES
    assert GRAPH_SEMANTICS_ROLES <= ALL_ROLES


def test_derive3_task_manager_audience_derives_from_the_creator_set() -> None:
    from tools.execution_tools import TASK_CREATE_ROLES
    from tools.gateway_tools import _TASK_MGR_ROLES

    assert TASK_CREATE_ROLES <= _TASK_MGR_ROLES


# =====================================================================
# CONTRACT — the legacy role whitelist and the live RBAC agree
# =====================================================================

# agents/roles.py's `allowed_tools` predates the live schema gate and was recorded
# as vestigial. It is not: it named the contract the live gate had drifted away
# from — every role's entry holds FileReadTool/GrepTool/GlobTool/query_graphrag,
# which dispatch granted to the researcher alone. Bridging the two vocabularies
# here turns a dead snapshot into the cross-check it should always have been.
_LEGACY_TO_LIVE: Dict[str, str] = {
    "FileReadTool": "read_file",
    "GrepTool": "grep",
    "GlobTool": "glob",
    "query_graphrag": "query_graphrag",
    "DocumentParserTool": "document_parser",
    "BashTool": "sandbox_bash",
}


@pytest.mark.anyio
async def test_contract1_legacy_whitelist_agrees_with_live_allowed_roles(
    tmp_path: Path,
) -> None:
    """A role's legacy whitelist entry must be reachable in the live RBAC.

    One direction only: the legacy record may omit tools added since. What must
    never happen again is the reverse — the live gate silently withholding a
    capability the role contract says it has.
    """
    from agents.roles import ROLE_REGISTRY
    from tests.test_phase8_8_tool_parity_gate import _isolated_store, _register_all

    store = _isolated_store(tmp_path)
    await _register_all(store)
    by_name = {s.name: s for s in store.all_schemas()}

    failures: List[str] = []
    for role, config in ROLE_REGISTRY.items():
        for legacy in config["allowed_tools"]:
            live = _LEGACY_TO_LIVE.get(legacy)
            if live is None or live not in by_name:
                continue
            if role not in by_name[live].allowed_roles:
                failures.append(f"{role} holds {legacy!r} but dispatch denies {live!r}")
    assert not failures, "role contract and live RBAC disagree:\n" + "\n".join(failures)


# =====================================================================
# BUDGET — the grant does not move the eager/deferred branch
# =====================================================================


@pytest.mark.anyio
async def test_budget1_core_dev_branch_matches_the_measurement(tmp_path: Path) -> None:
    """core_dev was already deferred at the default window before this grant.

    Measured at ship: 15 schemas / 8603 chars before, 24 / ~12000 after, against a
    3276-char threshold at the 8192-token default and 13107 at 32768. The branch is
    therefore invariant to the grant — deferred below, eager above. Asserted rather
    than trusted, because a later grant WILL cross the 32k ceiling.
    """
    from tests.test_phase8_8_tool_parity_gate import _isolated_store, _register_all

    store = _isolated_store(tmp_path)
    await _register_all(store)
    eager = DeferredToolLoader._visible_eager(store, "core_dev", SessionPermissionMode.AUTO)
    eager_chars = sum(len(s.json_schema) for s in eager)

    assert eager_chars > DeferredToolLoader.threshold_chars(8192), (
        "core_dev now fits the 8k eager budget — re-measure before assuming deferred"
    )
    assert eager_chars <= DeferredToolLoader.threshold_chars(32768), (
        "core_dev's catalog crossed the 32k eager ceiling; a local-tier turn just "
        "lost the whole-slice injection. Trim the slice or raise the threshold "
        "deliberately, do not relax this row."
    )


# =====================================================================
# CAP — document_parser is bounded like web_fetch (§5.5)
# =====================================================================


@pytest.mark.anyio
async def test_cap1_oversized_payload_is_refused() -> None:
    from shared.config import DOCUMENT_PARSER_MAX_PAYLOAD_BYTES
    from tools.perception_tools import DocumentParserTool

    payload = b"a" * (DOCUMENT_PARSER_MAX_PAYLOAD_BYTES + 1)
    out = await DocumentParserTool()._arun(
        mime_type="text/csv", payload_b64=base64.b64encode(payload).decode()
    )
    assert "over the" in out and "ceiling" in out


@pytest.mark.anyio
async def test_cap2_extracted_text_is_truncated() -> None:
    from shared.config import DOCUMENT_PARSER_MAX_CHARS
    from tools.perception_tools import DocumentParserTool

    payload = ("x" * 200 + "\n").encode() * 500
    out = await DocumentParserTool()._arun(
        mime_type="text/csv", payload_b64=base64.b64encode(payload).decode()
    )
    assert "TRUNCATED" in out
    assert len(out) < DOCUMENT_PARSER_MAX_CHARS + 2000


@pytest.mark.anyio
async def test_cap3_zip_bomb_is_refused_before_extraction() -> None:
    """The DECLARED uncompressed size is checked before the member is opened.

    A DOCX is a ZIP container: a small payload can declare gigabytes, so reading
    first and measuring after is what makes this a memory-exhaustion vector.
    """
    from shared.config import DOCUMENT_PARSER_MAX_UNCOMPRESSED_BYTES
    from tools.perception_tools import DocumentParserTool

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"\0" * (DOCUMENT_PARSER_MAX_UNCOMPRESSED_BYTES + 1))
    out = await DocumentParserTool()._arun(
        mime_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        payload_b64=base64.b64encode(buf.getvalue()).decode(),
    )
    assert "uncompressed bytes" in out and "ceiling" in out


# =====================================================================
# PIN — the validated address is the one connected to (DEBT-213)
# =====================================================================


def test_pin1_target_carries_the_validated_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard hands back what it approved, so the client cannot re-resolve."""
    import ipaddress

    import core.url_guard as ug

    monkeypatch.setattr(ug, "_resolve_all", lambda h: [ipaddress.ip_address("93.184.216.34")])
    denial, target = ug.resolve_fetch_target("https://example.com/docs?k=v")
    assert denial is None and target is not None
    assert target.host == "example.com"
    assert target.address == "93.184.216.34"
    assert target.connect_url == "https://93.184.216.34/docs?k=v"


def test_pin2_rebinding_cannot_survive_the_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second, different answer is never consulted — the pin is taken once.

    Simulates the attack directly: the resolver answers public first and private
    second. Because the caller connects to the pinned address, the second answer
    has nowhere to take effect.
    """
    import ipaddress

    import core.url_guard as ug

    answers = [
        [ipaddress.ip_address("93.184.216.34")],
        [ipaddress.ip_address("169.254.169.254")],
    ]
    monkeypatch.setattr(ug, "_resolve_all", lambda h: answers.pop(0))
    denial, target = ug.resolve_fetch_target("https://rebind.example/")
    assert denial is None and target is not None
    assert target.connect_url.startswith("https://93.184.216.34")
    assert answers, "the guard resolved twice — the pin is not being taken"


def test_pin3_pinned_ip_still_verifies_tls_against_the_hostname(tmp_path: Path) -> None:
    """Pinning must not weaken certificate verification.

    Serves TLS on loopback with a certificate valid ONLY for a name that does not
    resolve. Connecting to the IP with the SNI override must succeed; the same
    request WITHOUT it must be rejected. If this row ever needs `verify=False` to
    pass, the pinning approach is wrong and must be reverted, not relaxed.
    """
    pytest.importorskip("cryptography")
    import asyncio

    import httpx
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    host = "pinned.invalid"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = tmp_path / "c.pem", tmp_path / "k.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — stdlib handler contract
            body = b"pinned-ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return  # keep the gate's output clean; the server is incidental here

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def _probe() -> None:
        url = f"https://127.0.0.1:{port}/"
        async with httpx.AsyncClient(verify=str(certfile), timeout=10.0) as client:
            resp = await client.get(
                url, headers={"Host": host}, extensions={"sni_hostname": host}
            )
            assert resp.status_code == 200 and resp.text == "pinned-ok"

            with pytest.raises(Exception):
                await client.get(url, headers={"Host": host})

    try:
        asyncio.run(_probe())
    finally:
        server.shutdown()
        thread.join(timeout=5)


# =====================================================================
# WIRE — DEBT-131 outcomes
# =====================================================================


def test_wire1_task_management_is_reachable_by_task_creators() -> None:
    """Spawning a background task without being able to stop one is a defect.

    task_list/task_stop lived in a different module from task_create and inherited
    an orchestrator-only audience, so a hung task had no cleanup path.
    """
    from core.tool_registry import _build_task_tools

    tools = _build_task_tools({})
    assert {"task_create", "task_get", "task_list", "task_stop"} <= set(tools)
    # _manager is a PrivateAttr, invisible to the BaseTool type — read reflectively
    # so this row can assert the shared-instance invariant without a cast.
    managers = {
        id(getattr(tools[n], "_manager"))
        for n in ("task_create", "task_get", "task_list", "task_stop")
    }
    assert len(managers) == 1, (
        "the four task tools must share one BackgroundTaskManager — a stop issued "
        "against a different instance cannot see the creating manager's process handle"
    )


def test_wire2_deleted_benchmark_names_appear_in_neither_record() -> None:
    """A deleted tool must leave no allowlist entry claiming it is 'excluded'."""
    for name in ("run_benchmark", "get_benchmark_report"):
        assert name not in _INTENTIONALLY_UNREGISTERED
        assert name not in all_registrable_names()


def test_wire3_every_registrable_name_has_no_exclusion_entry() -> None:
    """The two records must stay disjoint — a name cannot be both."""
    overlap = set(all_registrable_names()) & set(_INTENTIONALLY_UNREGISTERED)
    assert not overlap, f"names recorded as both resolvable and excluded: {sorted(overlap)}"
