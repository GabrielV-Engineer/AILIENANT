import * as RadixTooltip from '@radix-ui/react-tooltip';
import type { ReactNode } from 'react';

interface TooltipProps {
    content: string;
    children: ReactNode;
    side?: 'top' | 'right' | 'bottom' | 'left';
    delay?: number;
}

export function Tooltip({ content, children, side = 'top', delay = 400 }: TooltipProps): JSX.Element {
    return (
        <RadixTooltip.Provider delayDuration={delay} skipDelayDuration={150}>
            <RadixTooltip.Root>
                <RadixTooltip.Trigger asChild>{children}</RadixTooltip.Trigger>
                <RadixTooltip.Portal>
                    <RadixTooltip.Content
                        className="ai-tooltip-content"
                        side={side}
                        sideOffset={6}
                        collisionPadding={8}
                    >
                        {content}
                    </RadixTooltip.Content>
                </RadixTooltip.Portal>
            </RadixTooltip.Root>
        </RadixTooltip.Provider>
    );
}
