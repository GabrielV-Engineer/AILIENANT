/**
 * Phase 7.11.7 (ADR-706 §4.5f) — Native HITL push-notification bridge.
 *
 * When the workspace chat panel is HIDDEN (background tab, focused on the
 * editor in another column, or the tab disposed) and the backend asks for an
 * HITL approval, fire a VS Code OS-level toast with three buttons:
 *
 *      [ Approve ]   [ Reject ]   [ Open Chat ]
 *
 * Approve/Reject map directly to the existing `client_hitl_response` WS event —
 * no new transport, no new audit semantics; the backend sees the same row in
 * its blake2b chain that an in-chat Approve/Reject would produce. The toast is
 * purely a *fallback* surface: when the chat is visible, the rich in-chat
 * `HITLInterventionCard` remains the primary path (edit-before-apply, risk
 * metrics, …). Configurable via `ailienant.notifications.hitlNativeMode`:
 *
 *      • "auto"   (default) — fire only when chat is hidden
 *      • "always" — fire even when the chat is visible (second-monitor ops)
 *      • "never"  — disable native toasts entirely
 *
 * **Cybersecurity (ADR-705):** the toast surfaces `action_description` +
 * `request_kind` only — never `proposed_content`. `action_description` is
 * orchestration-tier plaintext (not raw model output); VS Code's notification
 * renderer strips markup regardless. The full diff stays inside the trusted
 * Webview boundary.
 *
 * **Dedupe:** a local Set of resolved approval_ids guards the toast/in-chat
 * race — clicking the toast Approve then clicking the in-chat Approve no-ops
 * the second call. The backend's `_hitl_responses.pop()` is also idempotent,
 * so this is defense-in-depth.
 */

export type HitlMode = 'auto' | 'always' | 'never';

/** Wire shape mirrors `HITLApprovalRequestPayload` in `api/ws_contracts.py`.
 *  Kept local to this module so the bridge has zero coupling to other
 *  webview-only types in `shared/config.ts`. */
export interface HITLApprovalRequestPayload {
    session_id: string;
    approval_id: string;
    action_description: string;
    proposed_content?: string | null;
    /** Phase 7.11.7 — `BUDGET_OVERFLOW` / `TOKEN_SPIKE` / `SANDBOX_DEGRADED_EXEC`
     *  / `BUDGET_CEILING` map to a warning-level toast; other kinds (and
     *  `null` / `undefined`) fall back to info-level. */
    request_kind?: string | null;
}

/** The slim subset of `vscode.window` we actually call. Defined as an interface
 *  so unit tests can inject a stub without dragging in the real VS Code API. */
export interface WindowApi {
    showInformationMessage(
        message: string,
        options: { modal?: boolean },
        ...items: string[]
    ): Thenable<string | undefined>;
    showWarningMessage(
        message: string,
        options: { modal?: boolean },
        ...items: string[]
    ): Thenable<string | undefined>;
}

/** Kinds that warrant a warning-level toast (yellow icon, slightly more
 *  attention-grabbing). All other kinds — including `undefined` / unknown
 *  future kinds — degrade gracefully to info-level. */
const WARNING_KINDS: ReadonlySet<string> = new Set([
    'BUDGET_OVERFLOW',
    'TOKEN_SPIKE',
    'SANDBOX_DEGRADED_EXEC',
    'BUDGET_CEILING',
]);

/** Maximum characters of `action_description` we surface in the toast body.
 *  Long descriptions are truncated mid-sentence; the user can [Open Chat] to
 *  see the full text. VS Code's notification renderer happily wraps at this
 *  length on every supported platform. */
const ACTION_PREVIEW_MAX = 140;

const APPROVE = 'Approve';
const REJECT  = 'Reject';
const OPEN    = 'Open Chat';

export class HitlNotifier {
    private _visible = false;
    private readonly _resolved = new Set<string>();

    constructor(private readonly opts: {
        windowApi: WindowApi;
        getMode: () => HitlMode;
        send: (approvalId: string, approved: boolean) => void;
        revealPanel: () => void;
    }) {}

    /** Called from the host's `onDidChangeViewState` / `onDidDispose` hooks. */
    setVisibility(visible: boolean): void {
        this._visible = visible;
    }

    /** Idempotent. The host calls this when the in-chat card resolves an
     *  approval so a stale toast click for the same id is a no-op. */
    markResolved(approvalId: string): void {
        this._resolved.add(approvalId);
    }

    /** Fired by the WS-event dispatcher in `workspace_panel.ts` whenever a
     *  `server_hitl_approval_request` arrives. Decides whether to surface a
     *  toast (mode + visibility + dedupe) and wires the button → WS reply. */
    onApprovalRequest(payload: HITLApprovalRequestPayload): void {
        const mode = this.opts.getMode();
        if (mode === 'never') { return; }
        if (mode === 'auto' && this._visible) { return; }
        if (this._resolved.has(payload.approval_id)) { return; }

        const kind = payload.request_kind ?? '';
        const action = (payload.action_description ?? '').slice(0, ACTION_PREVIEW_MAX);
        const title = kind
            ? `AILIENANT · ${kind.replace(/_/g, ' ').toLowerCase()} — approval required`
            : 'AILIENANT — approval required';
        const message = `${title}\n${action || '(no description provided)'}`;

        const fire = WARNING_KINDS.has(kind)
            ? this.opts.windowApi.showWarningMessage.bind(this.opts.windowApi)
            : this.opts.windowApi.showInformationMessage.bind(this.opts.windowApi);

        // Non-modal: the user can keep editing while deciding. The backend's
        // `asyncio.wait_for(timeout_s)` is the hard deadline; if the user
        // dismisses without clicking (`undefined` choice) we no-op and let
        // the timeout audit the row as "timeout".
        void fire(message, { modal: false }, APPROVE, REJECT, OPEN).then(choice => {
            if (this._resolved.has(payload.approval_id)) { return; }
            if (choice === APPROVE) {
                this._resolved.add(payload.approval_id);
                this.opts.send(payload.approval_id, true);
            } else if (choice === REJECT) {
                this._resolved.add(payload.approval_id);
                this.opts.send(payload.approval_id, false);
            } else if (choice === OPEN) {
                // [Open Chat] does NOT resolve the approval — it just reveals
                // the panel so the user can inspect the diff (or use
                // edit-before-apply) on the rich in-chat card.
                this.opts.revealPanel();
            }
            // undefined → dismissed without action; deliberate no-op.
        });
    }
}
