import type { ReactNode } from 'react';
import { Icon, type IconName } from '../../shared/Icon';

interface EmptyStateProps {
    icon?: IconName;
    title: string;
    hint?: string;
    action?: ReactNode;
}

/**
 * Consistent empty / no-data placeholder. Used when a panel has loaded but has
 * nothing to show, distinguishing it from the loading (Skeleton) state.
 */
export function EmptyState({ icon = 'circle', title, hint, action }: EmptyStateProps): JSX.Element {
    return (
        <div className="ui-empty">
            <Icon name={icon} size={28} className="ui-empty-icon" />
            <div className="ui-empty-title">{title}</div>
            {hint && <div className="db-muted">{hint}</div>}
            {action && <div className="ui-empty-action">{action}</div>}
        </div>
    );
}
