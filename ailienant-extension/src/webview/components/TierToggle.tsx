import { InferenceTier } from '../../shared/config';

interface Props {
    tier: InferenceTier;
    onChange: (tier: InferenceTier) => void;
    disabled?: boolean;
}

const TIERS: { value: InferenceTier; label: string }[] = [
    { value: 'LOCAL_ONLY', label: '🖥 Local' },
    { value: 'HYBRID',     label: '⚡ Hybrid' },
    { value: 'SOLO_CLOUD', label: '☁ Cloud' },
];

export function TierToggle({ tier, onChange, disabled }: Props): JSX.Element {
    return (
        <div className="ai-tier-group" style={{ opacity: disabled ? 0.5 : 1 }}>
            {TIERS.map(t => (
                <button
                    key={t.value}
                    className="ai-tier-btn"
                    data-active={tier === t.value ? 'true' : 'false'}
                    data-tier={t.value}
                    disabled={disabled}
                    onClick={() => onChange(t.value)}
                    title={t.value}
                >
                    {t.label}
                </button>
            ))}
        </div>
    );
}
