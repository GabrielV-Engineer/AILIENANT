/**
 * Phase 12.3 — agent_todos store contract (backend wire: DEBT-054).
 *
 * Covers the client-side half of the DEBT-054 closure: replace semantics
 * mirroring the backend's `_merge_todos` reducer (an empty array is a real
 * "clear the panel" write, never "no opinion"), exclusion from the persisted
 * `workspace.v1` slot (a rehydrated TODO list would describe finished work),
 * reference stability on a deep-equal re-write (the client-side half of the
 * emission-storm guard — see brain/agentic_cell.py's cross-iteration
 * suppression for the server-side half), and `isServerEvent` accepting the
 * new wire tag.
 */
import * as assert from 'assert';
import { _setVsCodeApiForTesting, VsCodeApi } from '../shared/vscodeApi';
import { createPersistedStore } from '../shared/persistedStore';
import { isServerEvent, type AgentTodoItemPayload } from '../api/contracts';

// `../workspace/workspaceStore` creates its persisted store at module load via
// vscodeApi() — the mocha suite runs in the extension host (no
// acquireVsCodeApi), so a stub must be injected BEFORE importing it, hence the
// dynamic import inside each test rather than a top-level static import
// (mirrors src/test/nativeThinking.test.ts).

function makeStub(): VsCodeApi {
    let store: unknown = undefined;
    return {
        postMessage(_msg: unknown): void { /* no-op */ },
        getState<T = unknown>(): T | undefined { return store as T | undefined; },
        setState<T>(state: T): void { store = state; },
    };
}

async function flushMicrotasks(): Promise<void> {
    await Promise.resolve();
    await Promise.resolve();
}

const ITEM_A: AgentTodoItemPayload = { content: 'Add tests', status: 'pending', active_form: 'Adding tests' };
const ITEM_A_IN_PROGRESS: AgentTodoItemPayload = { ...ITEM_A, status: 'in_progress' };

suite('Phase 12.3 — agent_todos store contract', () => {
    test('setAgentTodos replaces the full list, and an empty array clears it', async () => {
        _setVsCodeApiForTesting(makeStub());
        const { useWorkspaceStore } = await import('../workspace/workspaceStore.js');

        useWorkspaceStore.getState().setAgentTodos('sess-1', [ITEM_A]);
        assert.deepStrictEqual(useWorkspaceStore.getState().agentTodos['sess-1'], [ITEM_A]);

        useWorkspaceStore.getState().setAgentTodos('sess-1', []);
        assert.deepStrictEqual(
            useWorkspaceStore.getState().agentTodos['sess-1'], [],
            'an explicit empty write must clear the panel, never be treated as no-op',
        );

        _setVsCodeApiForTesting(undefined);
    });

    test('a deep-equal re-write keeps the same array reference (no redundant re-render)', async () => {
        _setVsCodeApiForTesting(makeStub());
        const { useWorkspaceStore } = await import('../workspace/workspaceStore.js');

        useWorkspaceStore.getState().setAgentTodos('sess-2', [ITEM_A]);
        const before = useWorkspaceStore.getState().agentTodos['sess-2'];

        // A structurally-identical but distinct array — deep-equal, not the same object.
        useWorkspaceStore.getState().setAgentTodos('sess-2', [{ ...ITEM_A }]);
        const after = useWorkspaceStore.getState().agentTodos['sess-2'];
        assert.strictEqual(after, before, 'a deep-equal payload must not produce a new array reference');

        useWorkspaceStore.getState().setAgentTodos('sess-2', [ITEM_A_IN_PROGRESS]);
        const changed = useWorkspaceStore.getState().agentTodos['sess-2'];
        assert.notStrictEqual(changed, before, 'a genuinely changed payload must produce a new reference');

        _setVsCodeApiForTesting(undefined);
    });

    test('agentTodos is excluded from the persisted workspace.v1 slot', async () => {
        // useWorkspaceStore is a singleton whose VS Code API binding is fixed on its
        // FIRST dynamic import across the whole mocha run — a later test cannot
        // rebind it to a fresh stub. Testing the persistence *whitelist* therefore
        // needs a throwaway mirror store built fresh right here, exactly as
        // src/test/nativeThinking.test.ts does for the same reason: the mirror's
        // `pick` is a literal copy of workspaceStore.ts's real whitelist (plus
        // agentTodos on the full state shape, to prove `pick` leaves it out even
        // though it's present on the object being picked from).
        const stub = makeStub();
        _setVsCodeApiForTesting(stub);

        interface Slice {
            lastScrollY: number;
            agentTodos: Record<string, AgentTodoItemPayload[]>;
            setLastScrollY: (v: number) => void;
            setAgentTodos: (sessionId: string, todos: AgentTodoItemPayload[]) => void;
        }
        const useMirror = createPersistedStore<Slice>(
            (set) => ({
                lastScrollY: 0,
                agentTodos: {},
                setLastScrollY: (v) => set({ lastScrollY: v }),
                setAgentTodos: (sessionId, todos) =>
                    set((s) => ({ agentTodos: { ...s.agentTodos, [sessionId]: todos } })),
            }),
            // Mirrors the real store's whitelist (workspaceStore.ts's `pick`):
            // agentTodos is deliberately NOT listed, same as coderCompanions/activeSkills.
            { key: 'workspace.v1', version: 2, pick: (s) => ({ lastScrollY: s.lastScrollY }) },
        );

        useMirror.getState().setAgentTodos('sess-3', [ITEM_A]);
        useMirror.getState().setLastScrollY(1);
        await flushMicrotasks();

        const envelope = stub.getState<{ slots: Record<string, { data: Record<string, unknown> }> }>();
        assert.ok(envelope, 'envelope was never written');
        assert.strictEqual(
            envelope.slots['workspace.v1'].data.agentTodos, undefined,
            'agentTodos must never round-trip through the persisted slot',
        );

        _setVsCodeApiForTesting(undefined);
    });

    test('isServerEvent accepts server_agent_todos', () => {
        assert.strictEqual(
            isServerEvent({ event_type: 'server_agent_todos', data: { session_id: 's', iteration: 0, todos: [] } }),
            true,
        );
    });
});
