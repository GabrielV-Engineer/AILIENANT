import { useState } from 'react';
import { TelemetryFrame } from '../../shared/config';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';

interface Props { telemetry: TelemetryFrame | undefined; }

export function CSSAlertBanner({ telemetry }: Props): JSX.Element | null {
    const [dismissed, setDismissed] = useState(false);

    const shouldShow =
        !dismissed &&
        telemetry !== undefined &&
        (telemetry.is_red_alert || telemetry.css_total < 40);

    if (!shouldShow) { return null; }

    return (
        <div className="ws-alert" role="alert">
            <Icon name="alert" size={16} color="var(--accent-warn)" />
            <span>
                Context insufficient ({telemetry!.css_total.toFixed(0)}%). The injected context may not
                guarantee a logical resolution. Attach more files or run <strong>/context</strong>.
            </span>
            <Tooltip content="Dismiss alert for this session" side="left">
                <button
                    className="ai-btn"
                    data-variant="ghost"
                    style={{ padding: 4 }}
                    onClick={() => setDismissed(true)}
                    aria-label="Dismiss"
                >
                    <Icon name="x" size={14} />
                </button>
            </Tooltip>
        </div>
    );
}
