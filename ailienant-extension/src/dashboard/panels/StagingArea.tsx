import { useState, useEffect } from 'react';
import Editor, { DiffEditor } from '@monaco-editor/react';

interface PatchItem {
    approval_id:    string;
    file_path:      string;
    unified_diff:   string;
    original:       string;
    proposed:       string;
    stale:          boolean;
    document_version_id: string;
}

export default function StagingArea(): JSX.Element {
    const [patches, setPatches] = useState<PatchItem[]>([]);
    const [expanded, setExpanded] = useState<string | undefined>();
    const [editedContent, setEditedContent] = useState<Record<string, string>>({});

    // In production: receive patches via WebSocket server_vfs_patch_approved events
    // For dashboard, we set up a BroadcastChannel listener
    useEffect(() => {
        const bc = new BroadcastChannel('ailienant_patches');
        bc.onmessage = (e: MessageEvent): void => {
            const patch = e.data as PatchItem;
            setPatches(prev => {
                const idx = prev.findIndex(p => p.approval_id === patch.approval_id);
                if (idx >= 0) {
                    const updated = [...prev];
                    updated[idx] = patch;
                    return updated;
                }
                return [...prev, patch];
            });
        };
        return () => bc.close();
    }, []);

    const respond = async (approvalId: string, approved: boolean): Promise<void> => {
        const patch = patches.find(p => p.approval_id === approvalId);
        if (!patch) { return; }
        const body: Record<string, unknown> = { approval_id: approvalId, approved };
        if (approved && editedContent[approvalId]) {
            body.edited_content = editedContent[approvalId];
        }
        try {
            await fetch('/api/v1/hitl/respond', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
        } catch { /* no-op */ }
        setPatches(prev => prev.filter(p => p.approval_id !== approvalId));
        setExpanded(undefined);
    };

    if (patches.length === 0) {
        return (
            <div>
                <div className="db-section-title">Staging Area</div>
                <div className="db-card" style={{ textAlign: 'center', padding: 40 }}>
                    <div style={{ fontSize: 32, marginBottom: 8 }}>🔍</div>
                    <div className="db-muted">No pending patches. Changes will appear here when the agent proposes a mutation.</div>
                </div>
            </div>
        );
    }

    return (
        <div>
            <div className="db-section-title">Staging Area — {patches.length} pending</div>
            {patches.map(p => (
                <div key={p.approval_id} className="db-card">
                    <div className="db-diff-header">
                        <div className="db-row" style={{ gap: 8 }}>
                            <span className="db-file-badge">{p.file_path}</span>
                            {p.stale && (
                                <span style={{ fontSize: 11, color: '#E8C43A', fontWeight: 600 }}>
                                    ⚠ STALE — file changed since this patch was generated
                                </span>
                            )}
                        </div>
                        <button
                            className="db-btn db-btn-secondary"
                            style={{ fontSize: 11 }}
                            onClick={() => setExpanded(prev => prev === p.approval_id ? undefined : p.approval_id)}
                        >
                            {expanded === p.approval_id ? 'Collapse' : 'Review'}
                        </button>
                    </div>

                    {expanded === p.approval_id && (
                        <div style={{ height: 400, marginBottom: 12 }}>
                            <DiffEditor
                                original={p.original}
                                modified={editedContent[p.approval_id] ?? p.proposed}
                                language="typescript"
                                theme="vs-dark"
                                options={{
                                    readOnly:    false,
                                    minimap:     { enabled: false },
                                    fontSize:    12,
                                    renderSideBySide: true,
                                }}
                                onMount={(editor) => {
                                    const modifiedEditor = editor.getModifiedEditor();
                                    modifiedEditor.onDidChangeModelContent(() => {
                                        setEditedContent(prev => ({
                                            ...prev,
                                            [p.approval_id]: modifiedEditor.getValue(),
                                        }));
                                    });
                                }}
                            />
                        </div>
                    )}

                    <div className="db-row">
                        <button
                            className="db-btn db-btn-primary"
                            onClick={() => respond(p.approval_id, true)}
                            disabled={p.stale}
                        >
                            ✓ Approve{editedContent[p.approval_id] ? ' (edited)' : ''}
                        </button>
                        <button
                            className="db-btn db-btn-danger"
                            onClick={() => respond(p.approval_id, false)}
                        >
                            ✗ Reject
                        </button>
                        {p.stale && (
                            <span className="db-muted" style={{ fontSize: 11 }}>
                                Approve blocked on stale state
                            </span>
                        )}
                    </div>
                </div>
            ))}
        </div>
    );
}
