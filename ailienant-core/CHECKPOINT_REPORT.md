# CHECKPOINT REPORT — Phase 2.25

Generated: 2026-05-15T19:22:40.004962+00:00

## Average Parser Latency

| Scenario | Latency (ms) |
|---|---|
| no_fence_ms | 0.005 |
| fenced_ms | 0.026 |
| large_noise_ms | 0.095 |
| malformed_fence_ok | True |

**Average:** 0.042 ms (threshold: < 50 ms)

## Swarm Success Rate

100%

## Error Recovery Status

PASS — graph exits cleanly after MAX_RETRIES=2

## Test Suite

Exit status: `0` (0 = all passed)
