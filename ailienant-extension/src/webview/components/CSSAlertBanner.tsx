import { useState } from 'react';
import { TelemetryFrame } from '../../shared/config';

interface Props {
    telemetry: TelemetryFrame | undefined;
}

export function CSSAlertBanner({ telemetry }: Props): JSX.Element | null {
    const [dismissed, setDismissed] = useState(false);

    const shouldShow =
        !dismissed &&
        telemetry !== undefined &&
        (telemetry.is_red_alert || telemetry.css_total < 40);

    if (!shouldShow) { return null; }

    return (
        <div className="ai-alert-banner" role="alert">
            <span>⚠️</span>
            <span>
                Context insuficiente ({telemetry!.css_total.toFixed(0)}%). El contexto inyectado
                no garantiza resolución lógica del task. Adjunta más archivos o ejecuta{' '}
                <strong>/context</strong>.
            </span>
            <button
                className="ai-alert-banner-close"
                onClick={() => setDismissed(true)}
                aria-label="Dismiss alert"
                title="Dismiss"
            >
                ×
            </button>
        </div>
    );
}
