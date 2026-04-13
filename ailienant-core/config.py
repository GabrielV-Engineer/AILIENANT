import os

CLOUD_PROVIDER_KEYS = [
    "OPENAI_API_KEY", 
    "ANTHROPIC_API_KEY", 
    "GOOGLE_API_KEY", 
    "DEEPSEEK_API_KEY",
    "MISTRAL_API_KEY",
    "AILIENANT_CUSTOM_CLOUD_ENDPOINT" # Para proxies corporativos
]

# En core/graph.py
def check_cloud_availability() -> bool:
    """
    Verifica si existe CUALQUIER configuración que habilite el uso de Cloud.
    """
    return any(os.getenv(key) for key in CLOUD_PROVIDER_KEYS)

# Uso en el nodo
has_cloud_config = check_cloud_availability()