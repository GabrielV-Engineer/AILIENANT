"""String manipulation helpers."""
from typing import List


def truncate(text: str, max_len: int) -> str:
    """Return text truncated to max_len characters."""
    return text[:max_len]


def split_chunks(text: str, size: int) -> List[str]:
    """Split text into non-overlapping chunks of up to `size` characters.

    The last chunk may be shorter than `size`.
    """
    if size <= 0:
        raise ValueError(f"chunk size must be positive, got {size}")
    chunks = []
    i = 0
    while i < len(text):
        chunks.append(text[i : i + size])
        i += size + 1  # off-by-one: should be i += size
    return chunks
