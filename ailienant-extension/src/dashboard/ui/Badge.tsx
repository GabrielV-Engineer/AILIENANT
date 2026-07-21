import type { ReactNode } from 'react';
import { Icon, type IconName } from '../../shared/Icon';

export type BadgeStatus = 'good' | 'warning' | 'serious' | 'critical' | 'info' | 'neutral';

interface BadgeProps {
    status?: BadgeStatus;
    icon?: IconName;
    children: ReactNode;
}

/**
 * Small status pill. Consumes the reserved status tokens so state is never
 * conveyed by color alone — pass an `icon` (or a text label) alongside the hue.
 */
export function Badge({ status = 'neutral', icon, children }: BadgeProps): JSX.Element {
    return (
        <span className="ui-badge" data-status={status}>
            {icon && <Icon name={icon} size={11} />}
            {children}
        </span>
    );
}
