import { vscode } from "../vscode_bridge";
import { IntelligenceProfile, PROFILE_LABELS } from "../../shared/config";

interface Props {
    selected: IntelligenceProfile;
    disabled: boolean;
    onChange: (next: IntelligenceProfile) => void;
}

const PROFILES: IntelligenceProfile[] = ["Medium", "Big", "Cloud", "Hybrid"];

export function ProfileSelector({ selected, disabled, onChange }: Props): JSX.Element {
    const choose = (p: IntelligenceProfile): void => {
        onChange(p);
        vscode.postMessage({ type: "profile_change", value: p });
    };

    return (
        <fieldset
            disabled={disabled}
            style={{ border: "1px solid var(--vscode-panel-border)", padding: 8 }}
        >
            <legend>Intelligence Profile</legend>
            {PROFILES.map((p) => (
                <label
                    key={p}
                    style={{
                        display: "block",
                        padding: "4px 0",
                        cursor: disabled ? "not-allowed" : "pointer",
                    }}
                >
                    <input
                        type="radio"
                        name="ailienant-profile"
                        value={p}
                        checked={selected === p}
                        onChange={() => choose(p)}
                    />
                    {" "}{PROFILE_LABELS[p]}
                </label>
            ))}
        </fieldset>
    );
}
