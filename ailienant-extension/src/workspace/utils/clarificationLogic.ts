/**
 * Pure logic for the multi-question clarification grill (DEBT-172), kept free
 * of React/vscode imports so it can be unit-tested directly (no DOM-render
 * harness exists in this project's test suite — see agentTodoPanel.test.ts)
 * and so importing it never triggers `vscode_bridge.ts`'s eager
 * `acquireVsCodeApi()` call, which throws outside a real WebView.
 */
import type { ClarificationAnswer, ClarificationQuestion } from '../../api/contracts';
import type { HITLIntervention } from '../components/HITLInterventionCard';

export interface AnswerState {
    selected: string[];
    freeText: string;
    useFreeText: boolean;
}

/** Normalizes both wire shapes into one array: the multi-question batch
 *  (`intervention.questions`), or the legacy single-question fields
 *  (`question`/`context`/`suggested_options`), synthesized into a one-item
 *  batch with its first suggested option marked recommended — this is what
 *  upgrades the ambiguity-gate and Plan-mode-suggestion callers to the
 *  options UI for free. */
export function normalizeQuestions(intervention: HITLIntervention): ClarificationQuestion[] {
    if (intervention.questions && intervention.questions.length > 0) {
        return intervention.questions;
    }
    return [{
        id: 'q0',
        header: 'Question',
        question: intervention.question ?? intervention.action_description,
        context: intervention.context,
        options: (intervention.suggested_options ?? []).map((label, i) => ({
            label,
            recommended: i === 0,
        })),
        multi_select: false,
    }];
}

/** True once `q` has a usable answer — a picked option, or non-blank free
 *  text when "Other" is active. */
export function isQuestionAnswered(q: ClarificationQuestion, answers: Record<string, AnswerState>): boolean {
    const a = answers[q.id];
    if (!a) { return false; }
    return a.useFreeText ? a.freeText.trim().length > 0 : a.selected.length > 0;
}

/** Builds the batch `answers` payload sent back to the backend, one entry per
 *  question in stable order. An unanswered question (should not happen once
 *  the submit button's `allAnswered` gate holds) degrades to an empty answer
 *  rather than dropping the question, so the id list always round-trips. */
export function buildAnswers(
    questions: ClarificationQuestion[],
    answers: Record<string, AnswerState>,
): ClarificationAnswer[] {
    return questions.map(q => {
        const a = answers[q.id];
        if (!a) { return { id: q.id, selected_labels: [], free_text: null }; }
        return a.useFreeText
            ? { id: q.id, selected_labels: [], free_text: a.freeText.trim() || null }
            : { id: q.id, selected_labels: a.selected, free_text: null };
    });
}
