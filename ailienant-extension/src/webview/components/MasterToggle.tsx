import { vscode } from "../vscode_bridge";

interface Props {
    active: boolean;
    onChange: (next: boolean) => void;
}

export function MasterToggle({ active, onChange }: Props): JSX.Element {
    const click = (): void => {
        const next = !active;
        onChange(next);
        vscode.postMessage({ type: "master_toggle", value: next });
    };

    return (
        <button
            role="switch"
            aria-checked={active}
            onClick={click}
            style={{
                display: "block",
                width: "100%",
                padding: "8px 14px",
                margin: "8px 0",
                borderRadius: 4,
                border: `2px solid var(--vscode-${active ? "focusBorder" : "button-border"},#888)`,
                background: `var(--vscode-button-${active ? "" : "secondary"}background)`,
                color:      `var(--vscode-button-${active ? "" : "secondary"}foreground)`,
                cursor: "pointer",
                fontWeight: "bold",
                fontSize: 13,
            }}
        >
            AILIENANT: {active ? "ON" : "OFF"}
        </button>
    );
}
