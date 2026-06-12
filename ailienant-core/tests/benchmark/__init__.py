"""In-process precision-benchmark and ablation harness.

This package drives coding problems through the agent pipeline directly (no HTTP
or WebSocket), collects raw per-problem metrics (tokens, complexity, context
sufficiency), and degrades parts of the pipeline per ablation arm to attribute
value to individual capabilities.

The ablation toggles are applied purely at the harness boundary; the production
code path is never modified and never reads a benchmark flag.
"""
