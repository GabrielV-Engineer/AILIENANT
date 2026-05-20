import { ReasoningPreset } from '../../shared/config';
import type { IconName } from '../../shared/Icon';

export interface PresetConfig {
    temperature: number;
    top_p: number;
    tool_rag_top_k: number;
    context_window_pct: number;
    enable_mcts?: boolean;
    preferred_tools?: string[];
}

const PRESETS: Record<ReasoningPreset, PresetConfig> = {
    surgeon: {
        temperature:        0.0,
        top_p:              0.1,
        tool_rag_top_k:     3,
        context_window_pct: 0.5,
    },
    architect: {
        temperature:        0.5,
        top_p:              0.85,
        tool_rag_top_k:     5,
        context_window_pct: 1.0,
        enable_mcts:        true,
    },
    explorer: {
        temperature:        0.2,
        top_p:              0.9,
        tool_rag_top_k:     10,
        context_window_pct: 0.75,
        preferred_tools:    ['TraceDataFlowInput', 'ScanDirectory'],
    },
};

export const PRESET_META: Record<ReasoningPreset, { icon: IconName; label: string; desc: string }> = {
    surgeon:   { icon: 'microscope', label: 'Surgeon',   desc: 'Max accuracy · deterministic · narrow context' },
    architect: { icon: 'columns',    label: 'Architect', desc: 'Structured creativity · MCTS-aware · full context' },
    explorer:  { icon: 'telescope',  label: 'Explorer',  desc: 'Broad debug · Tool-RAG boost · wide search' },
};

export function getPresetConfig(preset: ReasoningPreset): PresetConfig {
    return PRESETS[preset];
}
