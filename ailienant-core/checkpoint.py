"""
AILIENANT CORE - Checkpointing System
Este módulo gestiona la persistencia del estado del grafo en SQLite.
Permite: Time-travel debugging, Human-in-the-loop y recuperación de sesiones.
"""

import sqlite3
from contextlib import contextmanager
from langgraph.checkpoint.sqlite import SqliteSaver

# Ubicación de la base de datos de estados (local al proyecto)
DB_PATH = "ailienant_state.sqlite"

class CheckpointManager:
    """
    Orquestador de la persistencia de LangGraph.
    Implementa un acceso seguro a la base de datos SQLite.
    """
    
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    @contextmanager
    def get_saver(self):
        """
        Context Manager que entrega una instancia de SqliteSaver.
        Uso:
            with manager.get_saver() as saver:
                app = workflow.compile(checkpointer=saver)
        """
        # Establecemos la conexión con la base de datos local
        # check_same_thread=False es necesario para FastAPI (multithreading)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            # SqliteSaver es la implementación oficial de LangGraph para persistencia
            saver = SqliteSaver(conn)
            yield saver
        finally:
            # Cerramos la conexión al finalizar el ciclo de vida del grafo
            conn.close()

# Instancia global para ser importada por el grafo
checkpoint_manager = CheckpointManager()