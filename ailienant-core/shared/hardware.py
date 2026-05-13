# ailienant-core/shared/hardware.py

import platform
from typing import Optional

from pydantic import BaseModel, Field


class HardwareProfile(BaseModel):
    """Snapshot of host hardware capabilities used by the 3D routing engine."""

    os_type: str = Field(description="Operating system: 'windows' | 'macos' | 'linux'")
    is_apple_silicon: bool = Field(description="True when running on arm64 macOS (M-series chip).")
    vram_gb: float = Field(default=0.0, description="NVIDIA VRAM in GB via pynvml; 0.0 if no GPU or driver unavailable.")
    gpu_name: Optional[str] = Field(default=None, description="Human-readable GPU model name.")
    ram_gb: float = Field(default=0.0, description="Total system RAM in GB via psutil; 0.0 if psutil unavailable.")


class HardwareDetector:
    """Static utility that probes the host at runtime and returns a HardwareProfile.

    Both psutil and pynvml are optional dependencies. If they are not installed
    or their drivers are absent, the affected fields default to 0.0 / None so the
    routing engine degrades gracefully instead of raising at startup.
    """

    @staticmethod
    def detect() -> HardwareProfile:
        os_raw = platform.system().lower()
        # Normalise Darwin → macos; keep linux/windows as-is
        os_type = "macos" if os_raw == "darwin" else os_raw
        is_apple_silicon = os_type == "macos" and platform.machine() == "arm64"

        ram_gb = HardwareDetector._detect_ram()
        vram_gb, gpu_name = HardwareDetector._detect_vram()

        return HardwareProfile(
            os_type=os_type,
            is_apple_silicon=is_apple_silicon,
            vram_gb=vram_gb,
            gpu_name=gpu_name,
            ram_gb=ram_gb,
        )

    @staticmethod
    def _detect_ram() -> float:
        try:
            import psutil  # type: ignore[import]
            return psutil.virtual_memory().total / (1024 ** 3)
        except Exception:
            return 0.0

    @staticmethod
    def _detect_vram() -> tuple[float, Optional[str]]:
        try:
            import pynvml  # type: ignore[import]
            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            name: str = pynvml.nvmlDeviceGetName(handle)
            # pynvml may return bytes on older versions
            gpu_name = name.decode("utf-8") if isinstance(name, bytes) else name
            vram_gb = info.total / (1024 ** 3)
            pynvml.nvmlShutdown()
            return vram_gb, gpu_name
        except Exception:
            return 0.0, None
