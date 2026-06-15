"""Test suite for the precision-benchmark and ablation harness.

The harness itself lives in ``core.benchmark`` so the production eval surface can
import it without the test tree. These modules exercise that harness end-to-end
(scaffold smoke, codegen Pass@1, oracle Resolve@k, ablation verdicts, routing
study, reproducibility, report serialization). The ``report.schema.json`` fixture
stays here because the schema-parity test reads it relative to its own location.
"""
