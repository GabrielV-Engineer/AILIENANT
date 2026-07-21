import type { ButtonHTMLAttributes } from 'react';
import { Icon, type IconName } from '../../shared/Icon';

type Variant = 'primary' | 'secondary' | 'danger' | 'ghost';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
    variant?: Variant;
    icon?: IconName;
    block?: boolean;
}

const VARIANT_CLASS: Record<Variant, string> = {
    primary:   'db-btn-primary',
    secondary: 'db-btn-secondary',
    danger:    'db-btn-danger',
    ghost:     'db-btn-ghost',
};

/**
 * Typed wrapper over the `.db-btn` family so panels stop hand-writing
 * variant class strings. Forwards every native button attribute.
 */
export function Button({
    variant = 'secondary',
    icon,
    block = false,
    className,
    children,
    ...rest
}: ButtonProps): JSX.Element {
    const classes = ['db-btn', VARIANT_CLASS[variant], block ? 'db-btn--block' : '', className]
        .filter(Boolean)
        .join(' ');
    return (
        <button className={classes} {...rest}>
            {icon && <Icon name={icon} size={14} />}
            {children}
        </button>
    );
}
