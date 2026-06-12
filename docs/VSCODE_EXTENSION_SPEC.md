# VS Code ↔ AILIENANT Backend Contract (Phase 3.4.5 — The Mirror)

This document is the single source of truth for the **MCTS Mirror** integration
between the Python FastAPI backend (`ailienant-core/`) and the VS Code extension
(`ailienant-extension/`). It exists so contributors on either side can read one
file to understand the contract without inferring it from the implementation.

## Overview

The MCTS Overnight Daemon (Phase 3.4.3a) maintains a tree of "parallel
universes" — each `MCTSNode` carries a `vfs_view: Dict[str, str]` mapping
workspace-relative paths to CAS blob hashes. The Mirror exposes these dreamed
states to the IDE so users can:

1. **Visually inspect** any node's proposed code via VS Code's native diff view
2. **One-Click Merge** a stable node's `vfs_view` onto physical disk

No physical files are touched until the user explicitly invokes the merge.

---

## URI scheme

```
ailienant-vision://{node_id}/{relative_path}
```

| Component | Meaning |
|---|---|
| `authority` (`{node_id}`) | MCTS node UUID hex (32 lowercase hex chars) |
| `path` (`/{relative_path}`) | Workspace-relative file path (always begins with `/`) |

Examples:
```
ailienant-vision://a1b2c3d4e5f60718.../src/auth.py
ailienant-vision://a1b2c3d4e5f60718.../tests/test_auth.py
```

VS Code calls `MirrorContentProvider.provideTextDocumentContent(uri)` for any
URI with this scheme. The provider strips the leading `/` from `uri.path` and
fetches the content via the backend.

---

## Backend HTTP endpoints

Base URL: `http://127.0.0.1:<port>/api/v1`, where `<port>` is the free loopback port the extension assigns at launch (`findFreePort()` → injected as `AILIENANT_API_PORT`, e.g. `59247`). `8000` is only the fallback default for a manual `uvicorn` launch.

### `GET /mcts/{node_id}/vfs?path={rel_path}`

Read a single file out of an MCTS node's `vfs_view`.

- **Response 200**: `text/plain` body with the file content (UTF-8). Falls
  back to disk if the path is not shadowed by the node's `vfs_view`.
- **Response 404**: `{"detail": "node or path not found"}` — node_id is not
  in the live registry (e.g. pruned, evicted, or a stale URI from a prior
  daemon run).

**Implementation**: `api/mcts_mirror.get_virtual_file(node_id, path)` →
`VirtualDocumentProvider(node.vfs_view).read(path)`.

### `POST /mcts/{node_id}/merge`

Atomically write a stable MCTS node's `vfs_view` onto physical disk.

**Request body**:
```json
{ "workspace_root": "/absolute/path/to/workspace" }
```

**Response 200** (`MergeReport`):
```json
{
  "success": true,
  "merged_files": 3,
  "workspace_root": "/abs/path",
  "errors": [],
  "prune_count": 1,
  "merged_paths": ["src/auth.py", "tests/test_auth.py"]
}
```

`merged_paths` (Phase 3.4.7) is the list of workspace-relative paths
**actually written**. The TS side uses these to register Bounding Boxes for
the silent-rejection telemetry loop.

**Safety guarantees** (in order of execution):
1. **Sandbox**: every `vfs_view` path must resolve inside `workspace_root`.
   Paths like `"../escape.txt"` are rejected.
2. **Preflight**: all CAS blob hashes must be retrievable BEFORE the first
   disk write. Any miss aborts without touching disk.
3. **Atomic per-file**: each write uses `tempfile.NamedTemporaryFile +
   os.replace`. Readers never observe a half-written file.
4. **Post-merge**: the merged node and all descendants are pruned via
   `MCTSTree.prune_branch()` (which clears `vfs_view` dicts so CAS LRU can
   reclaim) and a `record_prune(node_id, "user_merge_applied")` audit row is
   inserted in the `mcts_episodes` SQLite table.

If `MergeReport.success === false`, inspect `errors[]` for diagnostics:
- `"node_not_found"` — node_id not in the registry
- `"workspace_not_a_directory"` — bad `workspace_root`
- `"path_escape:<rel_path>"` — preflight: path resolves outside `workspace_root`
- `"cas_miss:<rel_path>:<hash_prefix>"` — preflight: CAS blob evicted before merge
- `"write_failed:<full_path>:<reason>"` — per-file write failure

---

## VS Code commands (registered by `ailienant-extension`)

| Command | Args | Purpose |
|---|---|---|
| `ailienant.showMctsDiff` | `(nodeId: string, filePath: string)` | Opens the native diff view comparing `workspace/<filePath>` ↔ `ailienant-vision://<nodeId>/<filePath>`. |
| `ailienant.applyMerge` | `(nodeId: string)` | Shows a modal confirmation, POSTs `/merge`, shows a success/error toast. |

Both are programmatic commands — they expect arguments from the caller (e.g. a
sidebar webview or a code lens). They also appear under the **"AILIENANT"**
category in the command palette but require args to do anything useful.

---

## TypeScript types (extension side)

```typescript
// ailienant-extension/src/api/api_client.ts
export interface MergeReport {
  success: boolean;
  merged_files: number;
  workspace_root: string;
  errors: string[];
  prune_count: number;
}

// On the APIClient singleton:
fetchVirtualFile(nodeId: string, filePath: string): Promise<string>;
applyMerge(nodeId: string, workspaceRoot: string): Promise<MergeReport>;
```

```typescript
// ailienant-extension/src/providers/mirror.ts
export const MIRROR_SCHEME = 'ailienant-vision';

export class MirrorContentProvider implements vscode.TextDocumentContentProvider {
    // VS Code calls this when opening any `ailienant-vision://` URI.
    provideTextDocumentContent(uri: vscode.Uri): Promise<string>;
}

export function buildMirrorUri(nodeId: string, relPath: string): vscode.Uri;
export function showMctsDiff(nodeId: string, relPath: string): Promise<void>;
export function applyMergeCommand(nodeId: string): Promise<void>;
```

---

## Example usage (from a sidebar webview / code lens)

```typescript
// Open a diff between the user's working tree and an MCTS dream:
await vscode.commands.executeCommand(
    'ailienant.showMctsDiff',
    'a1b2c3d4e5f60718...',
    'src/auth.py',
);

// One-click merge:
await vscode.commands.executeCommand(
    'ailienant.applyMerge',
    'a1b2c3d4e5f60718...',
);
```

---

---

## Silent Telemetry (Phase 3.4.7)

After a successful `applyMerge`, the extension automatically tracks each
merged file via a **Bounding Box** registry. If the user edits ≥70% of the
AI's characters within 3 minutes, an `AI_PAYLOAD_REJECTED` event fires to the
backend, which distills a coding rule from the AI-vs-human diff and writes it
to the workspace's local `.ailienant.json` so future planner runs honor the
implicit preference. The user never sees a popup — preferences are learned
from natural editing behavior.

### Bounding Box lifecycle

| Phase | Trigger |
|---|---|
| **Register** | `applyMerge` returns `success=true`. One box per entry in `MergeReport.merged_paths`. |
| **Decay tick** | `vscode.workspace.onDidChangeTextDocument` fires; the listener bumps `cumulativeChangedChars += change.rangeLength` for every `contentChange` of the matching document. |
| **Trip** | `cumulativeChangedChars >= 0.70 * originalText.length` AND `now - timestamp < 3 minutes`. |
| **Untrack** | After firing the rejection (prevents spam), or after the 3-minute window elapses. |

The decay metric is **cumulative `rangeLength` across all changes** — this
counts replacements as well as deletions and is O(1) per change event.

### `POST /api/v1/telemetry/reject`

**Request body**:
```json
{
  "uri": "/abs/path/to/src/auth.py",
  "original_ai_code": "<full file content the AI wrote>",
  "current_user_code": "<file content after user edits>",
  "timestamp": 1747300000000,
  "workspace_root": "/abs/path/to/workspace"
}
```

**Response 200**:
```json
{
  "distilled": true,
  "rule": "Type-annotate all public functions",
  "appended": true
}
```

- `distilled` — `true` iff the LLM returned a non-null rule
- `rule` — the rule string, or `null`
- `appended` — `true` iff the local `.ailienant.json` was actually mutated
  (false on duplicate rule or write failure)

### Side effect

When `appended === true`, `<workspace>/.ailienant/.ailienant.json` is updated
**atomically** via `tempfile.NamedTemporaryFile + os.replace`. The file is
**shared** with `core/config/profile.py` (IntelligenceProfileConfig); rule
writes use read-modify-write to preserve any pre-existing keys
(`master_enabled`, `profile`, `thresholds`).

### TypeScript types

```typescript
export interface RejectTelemetryPayload {
    uri: string;
    original_ai_code: string;
    current_user_code: string;
    timestamp: number;
    workspace_root: string;
}

export interface BoundingBox {
    uri: string;
    workspaceRoot: string;
    originalText: string;
    originalLength: number;
    timestamp: number;
    cumulativeChangedChars: number;
}

export class BoundingBoxRegistry {
    register(box: Omit<BoundingBox, 'cumulativeChangedChars'>): void;
    get(uri: string): BoundingBox | undefined;
    untrack(uri: string): void;
    processChange(event: vscode.TextDocumentChangeEvent): BoundingBox | null;
}
```

### Failure semantics

Telemetry NEVER surfaces to the user. Network errors / 5xx responses are
caught and logged via `console.warn`. Backend distillation failures (LLM
unreachable, JSON parse error) return `{distilled: false, rule: null,
appended: false}` rather than HTTP error.

---

## Known limits & future work

- **Single-process registry**: `brain/mcts/registry.py` is process-local. If
  the MCTS daemon ever moves to a ProcessPool worker, the registry must be
  fanned out via the SQLite `mcts_episodes` table.
- **No cache invalidation hook**: `MirrorContentProvider.invalidate(uri)`
  exists but isn't fired automatically when backend state changes. Future
  phases can wire this to a WebSocket event.
- **No authentication**: endpoints trust localhost (Phase 6 will add auth).
- **No rollback** on partial multi-file write failure — the preflight prevents
  the common cases; truly atomic multi-file writes require a journal.
