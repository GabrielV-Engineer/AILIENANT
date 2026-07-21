import type { ReactNode } from 'react';

interface SectionHeaderProps {
    title: string;
    subtitle?: string;
    /** Optional controls (buttons, filters) aligned to the right. */
    actions?: ReactNode;
}

/**
 * Standard page-level heading for a panel — replaces the ad-hoc
 * `.db-section-title` usage and reserves space for right-aligned actions.
 */
export function SectionHeader({ title, subtitle, actions }: SectionHeaderProps): JSX.Element {
    return (
        <div className="ui-section-header">
            <div>
                <h1 className="db-section-title" style={{ marginBottom: subtitle ? 2 : 0 }}>{title}</h1>
                {subtitle && <div className="db-muted">{subtitle}</div>}
            </div>
            {actions && <div className="ui-section-actions">{actions}</div>}
        </div>
    );
}
