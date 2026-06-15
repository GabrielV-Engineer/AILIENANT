"""Arithmetic helpers."""


def factorial(n: int) -> int:
    """Return n! for n >= 0."""
    if n < 0:
        raise ValueError(f"factorial undefined for n={n}")
    result = 1
    for i in range(2, n + 1):
        result *= i
    return result
