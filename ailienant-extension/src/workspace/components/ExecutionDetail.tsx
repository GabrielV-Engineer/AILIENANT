/**
 * The I/O body for one 'command' Glass-Box Timeline node — Ailienant's
 * analogue of a terminal's input/output box, deliberately honest about NOT
 * being the user's own shell: unlike a local CLI, a command here can run in
 * one of several isolation envelopes (a trusted devcontainer, the locked
 * Docker oracle cage, a Wasm sandbox, or a human-approved host fallback), so
 * the header names exactly which one actually ran it and where.
 *
 * stdout/stderr arrive already masked and length-capped by the backend's
 * single masking site (core/exec_log.py::record_exec) — rendered verbatim,
 * never re-processed. `execution.error` (set only when the adapter raised
 * before a verdict existed) takes the place of stdout/stderr entirely.
 *
 * 13.1.9 — `execution.initiator` no longer renders here: the row's own
 * AgentTimeline lane header now names the acting agent, and for the two
 * kinds that dispatch through `ToolDispatcher` (research/analyst/subagent
 * calls) `initiator` was the exact same string, shown a second time
 * immediately below. The field itself stays on the wire (still useful
 * telemetry) — only its redundant render here is gone.
 */
import type { ExecutionDetailShape } from '../../shared/config';

export interface ExecutionDetailProps {
    execution: ExecutionDetailShape;
}

const SOURCE_LABEL: Record<ExecutionDetailShape['source'], string> = {
    devcontainer: 'DEVCONTAINER',
    docker: 'DOCKER',
    wasm: 'WASM',
    native_host: 'NATIVE HOST',
    unknown: 'UNKNOWN ENVIRONMENT',
};

export function ExecutionDetail({ execution }: ExecutionDetailProps): JSX.Element {
    const { source, cwd, stdout, stderr, exit_code, duration_ms, truncated, error } = execution;
    const failed = error != null || (exit_code != null && exit_code !== 0);

    return (
        <div className="ws-exec">
            <div className="ws-exec-meta">
                <span className="ws-exec-source">{SOURCE_LABEL[source]}</span>
                {cwd && <span className="ws-exec-cwd" title={cwd}>{cwd}</span>}
            </div>
            {error != null ? (
                <pre className="ws-exec-error">{error}</pre>
            ) : (
                <>
                    {stdout && <pre className="ws-exec-stdout">{stdout}</pre>}
                    {stderr && <pre className="ws-exec-stderr">{stderr}</pre>}
                    {!stdout && !stderr && <div className="ws-exec-empty">No output.</div>}
                </>
            )}
            {truncated && (
                <div className="ws-exec-note">Output truncated — showing head and tail.</div>
            )}
            <div className="ws-exec-footer" data-failed={failed ? 'true' : 'false'}>
                <span>{exit_code != null ? `exit ${exit_code}` : 'running…'}</span>
                {duration_ms != null && <span className="ws-exec-duration">{Math.round(duration_ms)} ms</span>}
            </div>
        </div>
    );
}
