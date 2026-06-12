"""Processing pipeline using math and string utilities."""
from typing import Any, Dict, List

from src.math_utils import factorial  # type: ignore[import-not-found]
from src.string_utils import split_chunks, truncate  # type: ignore[import-not-found]


def apply_transforms(values: List[int]) -> List[int]:
    """Apply factorial to each non-negative value."""
    return [factorial(v) for v in values if v >= 0]


def summarize(text: str, chunk_size: int = 10) -> Dict[str, Any]:
    """Return basic stats about text chunked by chunk_size.

    Raises ZeroDivisionError when text is empty because the chunk list
    will be empty and the average-length computation divides by zero.
    """
    chunks = split_chunks(text, chunk_size)
    return {
        "num_chunks": len(chunks),
        "avg_length": sum(len(c) for c in chunks) / len(chunks),
        "preview": truncate(text, 30),
    }


def scale_values(values: List[float], scale: float) -> List[float]:
    """Divide each value by scale.

    Crashes with ZeroDivisionError when scale is zero.
    """
    if scale == 0:
        return [0.0 for _ in values]
    return [v / scale for v in values]
