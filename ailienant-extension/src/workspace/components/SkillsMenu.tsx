import { useEffect, useState } from 'react';
import { Icon } from '../../shared/Icon';
import { vscode } from '../vscode_bridge';
import type { SkillTemplate } from '../../shared/types';

export type SkillsView = 'skills-insert' | 'skills-create';

interface Props {
    view: SkillsView;
    onClose: () => void;
    onSwitchView: (view: SkillsView) => void;
}

export function SkillsMenu({ view, onClose, onSwitchView }: Props): JSX.Element {
    const [skills, setSkills] = useState<SkillTemplate[] | null>(null);
    const [name, setName] = useState('');
    const [body, setBody] = useState('');
    const [description, setDescription] = useState('');
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        const handler = (event: MessageEvent): void => {
            const msg = event.data as { type: string; skills?: SkillTemplate[] };
            if (msg.type === 'SKILLS_DATA') {
                setSkills(msg.skills ?? []);
                if (saving) { setSaving(false); setName(''); setBody(''); setDescription(''); onSwitchView('skills-insert'); }
            }
        };
        window.addEventListener('message', handler);
        vscode.postMessage({ type: 'GET_SKILLS' });
        return () => window.removeEventListener('message', handler);
    }, [saving, onSwitchView]);

    // ── Insert ───────────────────────────────────────────────────
    if (view === 'skills-insert') {
        if (skills === null) { return <div className="ws-models-body"><div className="ws-models-empty">Loading skills…</div></div>; }
        return (
            <div className="ws-models-body">
                {skills.length === 0 ? (
                    <div className="ws-models-empty">
                        <span>No skills yet.</span>
                        <button className="ws-core-menu-btn" onClick={() => onSwitchView('skills-create')}>
                            <Icon name="plus" size={13} /> Create your first skill
                        </button>
                    </div>
                ) : (
                    <div className="ws-models-list">
                        {skills.map(s => (
                            <div key={s.id} className="ws-models-row" style={{ alignItems: 'center', gap: 8 }}>
                                <button
                                    className="ws-mode-row"
                                    style={{ flex: 1, background: 'none', border: 'none', padding: 0, textAlign: 'left' }}
                                    onClick={() => {
                                        window.postMessage({ type: 'INVOKE_SKILL', id: s.id, name: s.name }, '*');
                                        onClose();
                                    }}
                                >
                                    <div className="ws-mode-row-text">
                                        <span className="ws-mode-row-title">{s.name}</span>
                                        <span className="ws-mode-row-desc">
                                            {s.description
                                                ? s.description.slice(0, 80) + (s.description.length > 80 ? '…' : '')
                                                : s.body.slice(0, 80) + (s.body.length > 80 ? '…' : '')}
                                        </span>
                                    </div>
                                </button>
                                <button className="ws-core-menu-btn" onClick={() => vscode.postMessage({ type: 'DELETE_SKILL', id: s.id })}>
                                    <Icon name="trash" size={13} />
                                </button>
                            </div>
                        ))}
                    </div>
                )}
                <p className="ws-models-note">Selecting a skill attaches it to your next submit.</p>
            </div>
        );
    }

    // ── Create ───────────────────────────────────────────────────
    const save = (): void => {
        if (!name.trim() || !body.trim()) { return; }
        setSaving(true);
        vscode.postMessage({
            type: 'SAVE_SKILL',
            skill: {
                name: name.trim(),
                body: body.trim(),
                description: description.trim() || undefined,
            },
        });
    };
    return (
        <div className="ws-models-body">
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                <input className="ws-input" placeholder="Skill name, e.g. Security audit" value={name} onChange={e => setName(e.target.value)} />
                <input className="ws-input" placeholder="Description (used for auto-match)" value={description} onChange={e => setDescription(e.target.value)} />
                <textarea className="ws-input" rows={6} placeholder="Prompt template…" value={body} onChange={e => setBody(e.target.value)} />
                <div style={{ display: 'flex', gap: 6 }}>
                    <button className="ws-core-menu-btn" disabled={saving || !name.trim() || !body.trim()} onClick={save}>
                        {saving ? 'Saving…' : 'Save skill'}
                    </button>
                    <button className="ws-core-menu-btn" onClick={() => onSwitchView('skills-insert')}>Cancel</button>
                </div>
            </div>
        </div>
    );
}
