import type { CSSProperties, ReactNode } from 'react';

interface CardProps {
    /** Optional uppercase micro-title rendered as the card header. */
    title?: string;
    /** Optional element (e.g. a button) pinned to the header's right edge. */
    actions?: ReactNode;
    children: ReactNode;
    className?: string;
    style?: CSSProperties;
}

/**
 * Formalizes the `.db-card` elevated surface. When `title` is present the
 * header row renders with the shared `.db-card-title` treatment.
 */
export function Card({ title, actions, children, className, style }: CardProps): JSX.Element {
    const classes = ['db-card', className].filter(Boolean).join(' ');
    return (
        <div className={classes} style={style}>
            {(title || actions) && (
                <div className="db-card-head">
                    {title && <div className="db-card-title" style={{ marginBottom: 0 }}>{title}</div>}
                    {actions && <div className="db-card-actions">{actions}</div>}
                </div>
            )}
            {children}
        </div>
    );
}
