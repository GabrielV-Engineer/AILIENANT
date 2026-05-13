# alienant-core/utils/token_counter.py
import tiktoken


def count_tokens(text: str, model_name: str = "gpt-4") -> int:
    """
    Calcula el número exacto de tokens en una cadena de texto.
    O(N) donde N es la longitud del texto.
    """
    try:
        # Intentamos obtener la codificación específica del modelo
        encoding = tiktoken.encoding_for_model(model_name)
    except KeyError:
        # Fallback a cl100k_base (usado por la mayoría de modelos modernos)
        encoding = tiktoken.get_encoding("cl100k_base")

    return len(encoding.encode(text))


# Ejemplo de integración con nuestro Router
# prompt = user_input + context_from_graphrag
# total_tokens = count_tokens(prompt)
# decision = calculate_3d_route(..., prompt_estimated_tokens=total_tokens, ...)
