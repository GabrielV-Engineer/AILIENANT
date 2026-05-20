import { InferenceTier } from '../../shared/config';
import { Icon, type IconName } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';

interface Props {
    tier: InferenceTier;
    onChange: (tier: InferenceTier) => void;
    disabled?: boolean;
}

const TIERS: { value: InferenceTier; label: string; icon: IconName; desc: string }[] = [
    { value: 'LOCAL_ONLY', label: 'Local',  icon: 'cpu',   desc: 'Run all inference on local hardware only' },
    { value: 'HYBRID',     label: 'Hybrid', icon: 'zap',   desc: 'Backend chooses local or cloud per task' },
    { value: 'SOLO_CLOUD', label: 'Cloud',  icon: 'cloud', desc: 'Route every call to the cloud tier' },
];

export function TierToggle({ tier, onChange, disabled }: Props): JSX.Element {
    return (
        <div className="ws-tier-group" data-disabled={disabled ? 'true' : 'false'}>
            {TIERS.map(t => (
                <Tooltip key={t.value} content={t.desc}>
                    <button
                        className="ws-tier-btn"
                        data-active={tier === t.value ? 'true' : 'false'}
                        data-tier={t.value}
                        disabled={disabled}
                        onClick={() => onChange(t.value)}
                        aria-label={`Inference tier: ${t.label}`}
                    >
                        <Icon name={t.icon} size={14} />
                        <span>{t.label}</span>
                    </button>
                </Tooltip>
            ))}
        </div>
    );
}
