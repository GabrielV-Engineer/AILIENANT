/**
 * Multi-question clarification grill (DEBT-172) — pure-logic coverage.
 *
 * This project's mocha suite tests component logic directly rather than
 * rendering into a DOM (see agentTodoPanel.test.ts) — there is no
 * React-rendering harness here, so the normalization/answer-building logic
 * lives in clarificationLogic.ts (no React/vscode imports, so importing it
 * never triggers vscode_bridge.ts's eager acquireVsCodeApi() call, which
 * throws outside a real WebView) specifically so it can be unit-tested this
 * way. Covers: legacy single-question shape synthesizes a one-item batch
 * with the first suggested option marked recommended; a real `questions`
 * batch passes through untouched; an unanswered question is never treated
 * as answered; and building the submit payload reflects picked options vs.
 * free-text "Other" answers per question.
 */
import * as assert from 'assert';
import {
    normalizeQuestions,
    isQuestionAnswered,
    buildAnswers,
    type AnswerState,
} from '../workspace/utils/clarificationLogic';
import type { HITLIntervention } from '../workspace/components/HITLInterventionCard';
import type { ClarificationQuestion } from '../api/contracts';

const BASE_INTERVENTION: HITLIntervention = {
    approval_id: 'req-1',
    action_description: 'fallback headline',
};

suite('DEBT-172 — ClarificationGrillCard logic', () => {
    test('normalizeQuestions synthesizes a one-item batch from the legacy shape, marking the first option recommended', () => {
        const intervention: HITLIntervention = {
            ...BASE_INTERVENTION,
            question: 'Which approach?',
            context: 'need a concrete target',
            suggested_options: ['A', 'B', 'C'],
        };
        const questions = normalizeQuestions(intervention);
        assert.strictEqual(questions.length, 1);
        assert.strictEqual(questions[0].id, 'q0');
        assert.strictEqual(questions[0].question, 'Which approach?');
        assert.strictEqual(questions[0].context, 'need a concrete target');
        assert.deepStrictEqual(
            questions[0].options,
            [
                { label: 'A', recommended: true },
                { label: 'B', recommended: false },
                { label: 'C', recommended: false },
            ],
        );
    });

    test('normalizeQuestions falls back to action_description when the legacy question is absent', () => {
        const questions = normalizeQuestions(BASE_INTERVENTION);
        assert.strictEqual(questions[0].question, 'fallback headline');
        assert.deepStrictEqual(questions[0].options, []);
    });

    test('normalizeQuestions passes a real questions batch through untouched', () => {
        const batch: ClarificationQuestion[] = [
            {
                id: 'q0', header: 'Docker setup', question: 'How to dockerize?',
                options: [{ label: 'Single container', recommended: true }],
                multi_select: false,
            },
            {
                id: 'q1', header: 'Docs', question: 'Commit docs now?',
                options: [{ label: 'Commit now', recommended: true }, { label: 'Keep iterating' }],
                multi_select: false,
            },
        ];
        const intervention: HITLIntervention = { ...BASE_INTERVENTION, questions: batch };
        assert.strictEqual(normalizeQuestions(intervention), batch);
    });

    test('isQuestionAnswered requires a picked option, or non-blank free text under "Other"', () => {
        const q: ClarificationQuestion = {
            id: 'q0', header: 'H', question: 'Q?', options: [{ label: 'A' }], multi_select: false,
        };
        assert.strictEqual(isQuestionAnswered(q, {}), false, 'no entry at all');
        assert.strictEqual(
            isQuestionAnswered(q, { q0: { selected: [], freeText: '', useFreeText: false } }),
            false,
            'entry present but nothing picked',
        );
        assert.strictEqual(
            isQuestionAnswered(q, { q0: { selected: [], freeText: '   ', useFreeText: true } }),
            false,
            'whitespace-only free text does not count',
        );
        assert.strictEqual(
            isQuestionAnswered(q, { q0: { selected: ['A'], freeText: '', useFreeText: false } }),
            true,
        );
        assert.strictEqual(
            isQuestionAnswered(q, { q0: { selected: [], freeText: 'later this week', useFreeText: true } }),
            true,
        );
    });

    test('buildAnswers reflects picked options and free-text answers per question, in question order', () => {
        const questions: ClarificationQuestion[] = [
            { id: 'q0', header: 'Docker setup', question: 'How?', options: [{ label: 'Single container' }], multi_select: false },
            { id: 'q1', header: 'Docs', question: 'When?', options: [{ label: 'Now' }], multi_select: false },
        ];
        const answers: Record<string, AnswerState> = {
            q0: { selected: ['Single container'], freeText: '', useFreeText: false },
            q1: { selected: [], freeText: 'Later this week', useFreeText: true },
        };
        assert.deepStrictEqual(buildAnswers(questions, answers), [
            { id: 'q0', selected_labels: ['Single container'], free_text: null },
            { id: 'q1', selected_labels: [], free_text: 'Later this week' },
        ]);
    });

    test('buildAnswers degrades an unanswered question to an empty answer rather than dropping it', () => {
        const questions: ClarificationQuestion[] = [
            { id: 'q0', header: 'H', question: 'Q?', options: [{ label: 'A' }], multi_select: false },
        ];
        assert.deepStrictEqual(buildAnswers(questions, {}), [
            { id: 'q0', selected_labels: [], free_text: null },
        ]);
    });

    test('buildAnswers supports multi_select questions carrying more than one picked label', () => {
        const questions: ClarificationQuestion[] = [
            { id: 'q0', header: 'H', question: 'Which files?', options: [{ label: 'a.py' }, { label: 'b.py' }], multi_select: true },
        ];
        const answers: Record<string, AnswerState> = {
            q0: { selected: ['a.py', 'b.py'], freeText: '', useFreeText: false },
        };
        assert.deepStrictEqual(buildAnswers(questions, answers), [
            { id: 'q0', selected_labels: ['a.py', 'b.py'], free_text: null },
        ]);
    });
});
