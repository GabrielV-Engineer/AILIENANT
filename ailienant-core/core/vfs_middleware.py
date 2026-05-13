import os
import threading
from typing import Dict, List
from pydantic import BaseModel


# Contrato de datos alineado exactamente con el de vfs_reader.ts
class DirtyBuffer(BaseModel):
    uri: str
    content: str
    version: int
    languageId: str


class VFSMiddleware:
    """
    Virtual File System (VFS) Proxy.
    Actúa como la fuente única de verdad para el estado de los archivos durante una misión de la IA.
    """

    _instance = None
    _lock = threading.Lock()
    _ram_vfs: Dict[str, str]

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(VFSMiddleware, cls).__new__(cls)
                # Diccionario en RAM: { "ruta_absoluta": "contenido_sucio" }
                cls._instance._ram_vfs = {}
        return cls._instance

    def ingest_dirty_buffers(self, buffers: List[DirtyBuffer]) -> None:
        """
        Intercepta el payload de la API y sobreescribe el estado de la RAM.
        Complejidad de Tiempo: O(N) donde N es el número de buffers sucios.
        """
        with self._lock:
            self._ram_vfs.clear()  # Limpiamos estado anterior (Asume 1 tarea a la vez por ahora)
            for buf in buffers:
                # Normalizamos la ruta estandarizada para cruzar OS (Windows/Linux/Mac)
                normalized_path = os.path.normpath(buf.uri)
                self._ram_vfs[normalized_path] = buf.content

    def read(self, filepath: str) -> str:
        """
        Proxy de lectura transparente para las Tools de LangGraph.
        Si el archivo está en el IDE sin guardar, devuelve la memoria RAM.
        Si no, accede al disco duro de forma segura.
        """
        normalized_path = os.path.normpath(filepath)

        # 1. Búsqueda Rápida en RAM (Entropía viva)
        with self._lock:
            if normalized_path in self._ram_vfs:
                return self._ram_vfs[normalized_path]

        # 2. Fallback a I/O del Sistema Operativo
        try:
            with open(normalized_path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            raise FileNotFoundError(
                f"AILIENANT VFS Error: Archivo inexistente -> {normalized_path}"
            )
        except Exception as e:
            # Capturamos errores de permisos (RBAC) o de codificación binaria
            raise RuntimeError(f"AILIENANT VFS I/O Exception: {str(e)}")
