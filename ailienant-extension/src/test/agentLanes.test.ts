/**
 * agentLanes.ts — consecutive agent-lane folding, role/tier badge formatting.
 *
 * Pure function tests, no JSDOM/React needed.
 */
import * as assert from 'assert';
import { buildAgentLanes, formatModelBadge, formatRoleLabel } from '../workspace/utils/agentLanes';
import type { TimelineEntry } from '../shared/config';

function entry(over: Partial<TimelineEntry> & { id: string }): TimelineEntry {
    return { seq: 0, ts: 100, kind: 'read', status: 'done', ...over };
}

suite('13.1.9 — buildAgentLanes', () => {
    test('folds a consecutive run of the same role into one lane', () => {
        const lanes = buildAgentLanes([
            entry({ id: 'a', role: 'researcher' }),
            entry({ id: 'b', role: 'researcher' }),
            entry({ id: 'c', role: 'researcher' }),
        ]);
        assert.strictEqual(lanes.length, 1);
        assert.strictEqual(lanes[0].role, 'researcher');
        assert.strictEqual(lanes[0].entries.length, 3);
    });

    test('a role change starts a new lane, even when the earlier role recurs', () => {
        const lanes = buildAgentLanes([
            entry({ id: 'a', role: 'researcher' }),
            entry({ id: 'b', role: 'coder' }),
            entry({ id: 'c', role: 'researcher' }),
        ]);
        assert.deepStrictEqual(lanes.map(l => l.role), ['researcher', 'coder', 'researcher']);
        assert.strictEqual(lanes.length, 3, 'the two researcher runs must NOT be merged into one lane');
    });

    test('lane ids are stable — keyed off the first entry in the lane', () => {
        const lanes = buildAgentLanes([entry({ id: 'a', role: 'coder' }), entry({ id: 'b', role: 'coder' })]);
        assert.strictEqual(lanes[0].id, 'lane:a');
    });

    test('a role-less run of entries folds into one unattributed lane', () => {
        const lanes = buildAgentLanes([entry({ id: 'a' }), entry({ id: 'b' })]);
        assert.strictEqual(lanes.length, 1);
        assert.strictEqual(lanes[0].role, undefined);
    });

    test('the lane picks up the first non-undefined model tier among its entries', () => {
        const lanes = buildAgentLanes([
            entry({ id: 'a', role: 'coder', modelTier: undefined }),
            entry({ id: 'b', role: 'coder', modelTier: 'big' }),
            entry({ id: 'c', role: 'coder', modelTier: 'big' }),
        ]);
        assert.strictEqual(lanes[0].modelTier, 'big');
    });

    test('an empty entries array yields no lanes', () => {
        assert.deepStrictEqual(buildAgentLanes([]), []);
    });
});

suite('13.1.9 — formatRoleLabel', () => {
    test('title-cases a plain role', () => {
        assert.strictEqual(formatRoleLabel('researcher'), 'Researcher');
    });

    test('splits and title-cases each underscore-separated word', () => {
        assert.strictEqual(formatRoleLabel('core_dev'), 'Core Dev');
        assert.strictEqual(formatRoleLabel('agentic_cell'), 'Agentic Cell');
    });

    test('upper-cases the known acronym exceptions', () => {
        assert.strictEqual(formatRoleLabel('qa_tester'), 'QA Tester');
        assert.strictEqual(formatRoleLabel('vcs_manager'), 'VCS Manager');
    });
});

suite('13.1.9 — formatModelBadge', () => {
    test('joins tier + real model name when config carries one', () => {
        const config = { tiers: { small: 's', medium: 'm', big: 'qwen2.5-coder:32b' } } as never;
        assert.strictEqual(formatModelBadge('big', config), 'big · qwen2.5-coder:32b');
    });

    test('degrades to the bare tier when config is null', () => {
        assert.strictEqual(formatModelBadge('big', null), 'big');
    });

    test('degrades to the bare tier when the real name is missing for that tier', () => {
        const config = { tiers: { small: 's', medium: 'm', big: '' } } as never;
        assert.strictEqual(formatModelBadge('cloud', config), 'cloud');
    });

    test('an unrecognized tier string passes through unchanged', () => {
        assert.strictEqual(formatModelBadge('weird', null), 'weird');
    });
});
