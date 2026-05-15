import { createRoot } from "react-dom/client";
import { useState, useCallback } from "react";
import { MasterToggle } from "./components/MasterToggle";
import { ProfileSelector } from "./components/ProfileSelector";
import { DEFAULT_PROFILE, IntelligenceProfile } from "../shared/config";

interface InitialState {
    masterEnabled: boolean;
    profile: IntelligenceProfile;
}

function App({ initial }: { initial: InitialState }): JSX.Element {
    const [enabled, setEnabled] = useState<boolean>(initial.masterEnabled);
    const [profile, setProfile] = useState<IntelligenceProfile>(initial.profile);

    const handleToggle  = useCallback((next: boolean): void => setEnabled(next), []);
    const handleProfile = useCallback((next: IntelligenceProfile): void => setProfile(next), []);

    return (
        <div style={{ padding: 8 }}>
            <MasterToggle active={enabled} onChange={handleToggle} />
            <ProfileSelector
                selected={profile}
                disabled={!enabled}
                onChange={handleProfile}
            />
        </div>
    );
}

function readInitialState(root: HTMLElement): InitialState {
    const raw = root.dataset.initial;
    if (!raw) {
        return { masterEnabled: false, profile: DEFAULT_PROFILE };
    }
    try {
        const parsed = JSON.parse(raw) as Partial<InitialState>;
        return {
            masterEnabled: parsed.masterEnabled ?? false,
            profile: parsed.profile ?? DEFAULT_PROFILE,
        };
    } catch {
        return { masterEnabled: false, profile: DEFAULT_PROFILE };
    }
}

document.addEventListener("DOMContentLoaded", () => {
    const root = document.getElementById("root");
    if (!root) { return; }
    createRoot(root).render(<App initial={readInitialState(root)} />);
});
