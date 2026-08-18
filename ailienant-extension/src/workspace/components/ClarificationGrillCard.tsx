import { useState, useCallback, useMemo } from 'react';
import { Icon } from '../../shared/Icon';
import { useClarificationResponder } from '../utils/useClarificationResponder';
import { normalizeQuestions, isQuestionAnswered, buildAnswers, type AnswerState } from '../utils/clarificationLogic';
import type { HITLIntervention } from './HITLInterventionCard';
import type { ClarificationQuestion } from '../../api/contracts';

interface Props {
    intervention: HITLIntervention;
    nattName: string;
    onResolved: (approvalId: string) => void;
}

export function ClarificationGrillCard({ intervention, nattName, onResolved }: Props): JSX.Element {
    const questions = useMemo(() => normalizeQuestions(intervention), [intervention]);
    const [activeIndex, setActiveIndex] = useState(0);
    const [answers, setAnswers] = useState<Record<string, AnswerState>>({});
    const { respond } = useClarificationResponder(intervention.approval_id, onResolved);

    const active = questions[activeIndex];
    const activeAnswer = answers[active.id];
    const selectedSet = new Set(activeAnswer?.selected ?? []);

    const isAnswered = useCallback(
        (q: ClarificationQuestion): boolean => isQuestionAnswered(q, answers),
        [answers],
    );

    const allAnswered = questions.every(isAnswered);

    const toggleOption = useCallback((q: ClarificationQuestion, label: string) => {
        setAnswers(prev => {
            const cur = prev[q.id];
            const nextSelected = q.multi_select
                ? (cur?.selected.includes(label)
                    ? cur.selected.filter(l => l !== label)
                    : [...(cur?.selected ?? []), label])
                : [label];
            return { ...prev, [q.id]: { selected: nextSelected, freeText: cur?.freeText ?? '', useFreeText: false } };
        });
    }, []);

    const toggleOther = useCallback((q: ClarificationQuestion) => {
        setAnswers(prev => {
            const cur = prev[q.id];
            return { ...prev, [q.id]: { selected: [], freeText: cur?.freeText ?? '', useFreeText: true } };
        });
    }, []);

    const setFreeText = useCallback((q: ClarificationQuestion, value: string) => {
        setAnswers(prev => {
            const cur = prev[q.id];
            return { ...prev, [q.id]: { selected: cur?.selected ?? [], freeText: value, useFreeText: true } };
        });
    }, []);

    const handleSubmit = useCallback(() => {
        respond(buildAnswers(questions, answers));
    }, [questions, answers, respond]);

    return (
        <div className="ws-hitl-card ai-card ws-clarify-card" role="alertdialog" aria-live="assertive">
            <div className="ws-hitl-head">
                <Icon name="message" size={16} color="var(--accent-warn)" />
                <span className="ws-hitl-title">{nattName} needs your input</span>
            </div>

            {questions.length > 1 && (
                <div className="ws-clarify-tabs">
                    {questions.map((q, i) => (
                        <button
                            key={q.id}
                            type="button"
                            className="ws-clarify-tab"
                            data-active={i === activeIndex}
                            onClick={() => setActiveIndex(i)}
                        >
                            <Icon name={isAnswered(q) ? 'check-circle' : 'circle'} size={12} />
                            <span>{q.header}</span>
                        </button>
                    ))}
                </div>
            )}

            <div className="ws-clarify-panel">
                <div className="ws-clarify-question">{active.question}</div>
                {active.context && <div className="ws-clarify-context">{active.context}</div>}

                <div className="ws-clarify-options">
                    {active.options.map(opt => (
                        <label key={opt.label} className="ws-mode-row" data-active={selectedSet.has(opt.label)}>
                            <input
                                type={active.multi_select ? 'checkbox' : 'radio'}
                                name={`clarify-${active.id}`}
                                checked={selectedSet.has(opt.label)}
                                onChange={() => toggleOption(active, opt.label)}
                            />
                            <div className="ws-mode-row-text">
                                <div className="ws-mode-row-title">
                                    {opt.label}
                                    {opt.recommended && <span className="ws-clarify-recommended"> (Recommended)</span>}
                                </div>
                                {opt.description && <div className="ws-mode-row-desc">{opt.description}</div>}
                            </div>
                        </label>
                    ))}
                    <label className="ws-mode-row" data-active={!!activeAnswer?.useFreeText}>
                        <input
                            type={active.multi_select ? 'checkbox' : 'radio'}
                            name={`clarify-${active.id}`}
                            checked={!!activeAnswer?.useFreeText}
                            onChange={() => toggleOther(active)}
                        />
                        <div className="ws-mode-row-text">
                            <div className="ws-mode-row-title">Other</div>
                            {activeAnswer?.useFreeText && (
                                <input
                                    className="ai-input ws-clarify-freetext"
                                    type="text"
                                    placeholder="Type your answer…"
                                    value={activeAnswer.freeText}
                                    onChange={(e) => setFreeText(active, e.target.value)}
                                    autoFocus
                                />
                            )}
                        </div>
                    </label>
                </div>
            </div>

            <div className="ws-hitl-actions">
                <button
                    className="ai-btn"
                    data-variant="primary"
                    disabled={!allAnswered}
                    onClick={handleSubmit}
                    aria-label="Submit answers"
                >
                    <Icon name="send" size={14} /><span>Submit answers</span>
                </button>
            </div>
        </div>
    );
}
