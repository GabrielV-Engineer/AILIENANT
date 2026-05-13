"""
AILIENANT CORE - Checkpointing System
Este módulo gestiona la persistencia del estado del grafo en SQLite.
Permite: Time-travel debugging, Human-in-the-loop y recuperación de sesiones.
"""

import sqlite3
from typing import Optional

from langgraph.checkpoint.sqlite import SqliteSaver

# Ubicación de la base de datos de estados (local al proyecto)
DB_PATH = "ailienant_state.sqlite"


class CheckpointManager:
    """
    Orquestador de la persistencia de LangGraph.

    Holds a single persistent SQLite connection for the lifetime of the
    FastAPI application. WAL pragmas are applied once on initialize() so
    concurrent reads never block on agent writes.

    Lifecycle (wired to FastAPI lifespan):
        startup  → checkpoint_manager.initialize()
        shutdown → checkpoint_manager.close()   (after WAL force-truncate)
    """

    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._saver: Optional[SqliteSaver] = None
        self._is_writing: bool = False  # guard for WALCheckpointer

    def initialize(self) -> None:
        """Open connection and apply WAL pragmas. Call once from lifespan startup."""
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        for pragma in (
            "PRAGMA journal_mode=WAL;",
            "PRAGMA synchronous=NORMAL;",
            "PRAGMA mmap_size=268435456;",
            "PRAGMA cache_size=-64000;",
        ):
            self._conn.execute(pragma)
        self._saver = SqliteSaver(self._conn)

    def get_saver(self) -> SqliteSaver:
        """Return the live saver. Lazily initializes if called before lifespan startup."""
        if self._saver is None:
            self.initialize()
        assert self._saver is not None
        return self._saver

    def close(self) -> None:
        """Close connection. Call from lifespan shutdown after WAL force-truncate."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._saver = None

    @property
    def conn(self) -> Optional[sqlite3.Connection]:
        return self._conn

    @property
    def is_writing(self) -> bool:
        return self._is_writing


# Instancia global para ser importada por el grafo y el WALCheckpointer
checkpoint_manager = CheckpointManager()
