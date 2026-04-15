# ailienant-core/core/llm_gateway.py

import os
import logging
from typing import Optional
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic.v1 import SecretStr

# Cargamos las variables del archivo .env (si existe)
load_dotenv()

logger = logging.getLogger("LLM_GATEWAY")

class LLMGateway:
    """
    Gateway agnóstico. Se conecta a CUALQUIER proveedor que respete 
    el formato de API de OpenAI (LM Studio, Ollama, vLLM, OpenAI, Groq, etc.).
    """
    
    @staticmethod
    def get_model(
        temperature: float = 0.0, 
        model_name: Optional[str] = None,
        override_base_url: Optional[str] = None, # 👈 Parámetro dinámico del usuario
        override_api_key: Optional[str] = None   # 👈 Parámetro dinámico del usuario
    ) -> ChatOpenAI:
        """
        Retorna la instancia del LLM configurada a través de preferencias dinámicas o variables de entorno.
        """
        # 1. Jerarquía de Configuración: UI > .env > Default
        base_url = override_base_url or os.getenv("AILIENANT_LLM_BASE_URL", "http://localhost:1234/v1")
        raw_api_key = override_api_key or os.getenv("AILIENANT_LLM_API_KEY", "lm-studio")
        target_model = model_name or os.getenv("AILIENANT_LLM_MODEL", "local-model")
        
        # 2. Envolver la API Key en SecretStr para satisfacer la seguridad de Pydantic
        api_key_secret = SecretStr(raw_api_key) if raw_api_key else None
        
        logger.debug(f"Conectando a LLM en {base_url} con el modelo {target_model}")
        
        try:
            # 3. Instanciar el modelo agnóstico con la configuración final
            llm = ChatOpenAI(
                base_url=base_url,
                api_key=api_key_secret,
                model=target_model,
                temperature=temperature,
                max_retries=2  # Resiliencia contra micro-cortes de red
            )
            return llm
            
        except Exception as e:
            logger.error(f"❌ Error al inicializar ChatOpenAI en el Gateway: {str(e)}")
            raise e
   