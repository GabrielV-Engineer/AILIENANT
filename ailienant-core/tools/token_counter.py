# ailienant-core/tools/token_counter.py

import math

import tiktoken


class PrecisionTokenCounter:
    """Tiktoken-based token counter with a configurable safety buffer.

    Use `count()` for exact measurements (e.g. cost estimation).
    Use `estimate_with_buffer()` for context-window capacity checks — the 10%
    overhead accounts for special tokens, message formatting, and metadata that
    the proxy may inject before the call reaches the model.
    """

    SAFETY_BUFFER: float = 0.10  # 10% overhead

    @staticmethod
    def _get_encoding(model: str) -> tiktoken.Encoding:
        try:
            return tiktoken.encoding_for_model(model)
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")

    @staticmethod
    def count(text: str, model: str = "gpt-4") -> int:
        """Raw token count for *text* using the encoding for *model*."""
        enc = PrecisionTokenCounter._get_encoding(model)
        return len(enc.encode(text))

    @staticmethod
    def estimate_with_buffer(text: str, model: str = "gpt-4") -> int:
        """Token count inflated by SAFETY_BUFFER, rounded up.

        Always use this method when comparing against a model's context_window
        to avoid OOM/truncation errors at inference time.
        """
        raw = PrecisionTokenCounter.count(text, model)
        return math.ceil(raw * (1.0 + PrecisionTokenCounter.SAFETY_BUFFER))
