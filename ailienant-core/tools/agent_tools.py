# ailienant-core/tools/agent_tools.py
#
# Phase 2.20 — VFS-Sandboxed Agent Tool Factories.
#
# Factory functions inject the VFS instance via closure so LangChain's @tool
# schema only exposes path/content arguments to the LLM. No internal service
# objects (VFSMiddleware, os, open()) reach the tool schema.
#
# Phase 4 wires these into run_logic_node via tool_registry.

from __future__ import annotations

import logging
from typing import Callable, Optional

from langchain_core.tools import tool

logger = logging.getLogger("AGENT_TOOLS")


def make_read_file_tool(vfs_read: Callable[[str], Optional[str]]):
    """Factory: returns a @tool that reads a VFS file by path.

    Args:
        vfs_read: Callable e.g. vfs_middleware.read(path) → str | None
    """
    @tool
    def read_file(path: str) -> str:
        """Read a file from the Virtual File System by its workspace-relative path."""
        content = vfs_read(path)
        if content is None:
            logger.warning("read_file: '%s' not found in VFS.", path)
            return f"[read_file] ERROR: '{path}' not found in VFS or on disk."
        logger.debug("read_file: '%s' read (%d chars).", path, len(content))
        return content

    return read_file


def make_write_file_tool(vfs_write: Callable[[str, str], None]):
    """Factory: returns a @tool that writes content to a VFS file.

    Args:
        vfs_write: Callable e.g. vfs_middleware.write(path, content) → None
    """
    @tool
    def write_file(path: str, content: str) -> str:
        """Write content to a file in the Virtual File System."""
        vfs_write(path, content)
        logger.info("write_file: '%s' written (%d chars).", path, len(content))
        return f"[write_file] OK: '{path}' written to VFS."

    return write_file


def make_run_command_tool():
    """Factory: returns a @tool stub for shell command execution.

    Phase 4 replaces this with sandboxed subprocess execution.
    Phase 2.20: raises NotImplementedError to surface the stub clearly.
    """
    @tool
    def run_command(command: str) -> str:
        """Execute a shell command in the sandboxed workspace environment."""
        raise NotImplementedError(
            "run_command() stub — Phase 4 implements sandboxed subprocess execution. "
            f"Command attempted: {command!r}"
        )

    return run_command
