# ailienant-core/shared/token_counter.py
import tiktoken


def count_tokens(text: str, model_name: str = "gpt-4") -> int:
    """
    Compute the exact token count of a text string.
    O(N) in the length of the text.
    """
    try:
        # Prefer the encoding registered for this specific model.
        encoding = tiktoken.encoding_for_model(model_name)
    except KeyError:
        # Fall back to cl100k_base, used by most modern models.
        encoding = tiktoken.get_encoding("cl100k_base")

    return len(encoding.encode(text))


# Router integration example:
# prompt = user_input + context_from_graphrag
# total_tokens = count_tokens(prompt)
# decision = calculate_3d_route(..., prompt_estimated_tokens=total_tokens, ...)
