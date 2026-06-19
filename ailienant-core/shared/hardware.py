# ailienant-core/shared/hardware.py

import platform
from typing import Optional

from pydantic import BaseModel, Field

from shared.config import VRAM_FULL_SWARM_GB, VRAM_MICRO_SWARM_GB


def effective_vram_gb(profile: "HardwareProfile") -> float:
    """Memory budget that gates swarm mode and cloud-reroute decisions.

    Apple-Silicon unified memory exposes no discrete VRAM, so the available RAM
    is the working budget; a discrete GPU contributes its free VRAM (total used).
    Floored at zero so a stale used > total reading can never go negative.
    """
    if profile.is_apple_silicon:
        return profile.ram_available_gb
    return max(0.0, profile.vram_gb - profile.vram_used_gb)


class HardwareProfile(BaseModel):
    """Snapshot of host hardware capabilities used by the 3D routing engine."""

    os_type: str = Field(description="Operating system: 'windows' | 'macos' | 'linux'")
    is_apple_silicon: bool = Field(description="True when running on arm64 macOS (M-series chip).")
    vram_gb: float = Field(default=0.0, description="NVIDIA VRAM total in GB via pynvml; 0.0 if no GPU or driver unavailable.")
    vram_used_gb: float = Field(default=0.0, description="NVIDIA VRAM currently used in GB.")
    gpu_name: Optional[str] = Field(default=None, description="Human-readable GPU model name.")
    ram_gb: float = Field(default=0.0, description="Total system RAM in GB via psutil; 0.0 if psutil unavailable.")
    ram_available_gb: float = Field(default=0.0, description="Available (free + reclaimable) system RAM in GB.")
    cpu_name: str = Field(default="", description="Human-readable CPU model string.")
    cpu_cores: int = Field(default=0, description="Physical (non-hyperthreaded) CPU core count.")
    cpu_freq_mhz: float = Field(default=0.0, description="Maximum CPU frequency in MHz.")
    suggested_mode: str = Field(default="SEQUENTIAL", description="Hardware-derived recommended execution mode.")


class HardwareDetector:
    """Static utility that probes the host at runtime and returns a HardwareProfile.

    Both psutil and pynvml are optional dependencies. If they are not installed
    or their drivers are absent, the affected fields default to 0.0 / None so the
    routing engine degrades gracefully instead of raising at startup.
    """

    @staticmethod
    def detect() -> HardwareProfile:
        os_raw = platform.system().lower()
        os_type = "macos" if os_raw == "darwin" else os_raw
        is_apple_silicon = os_type == "macos" and platform.machine() == "arm64"

        ram_gb, ram_available_gb = HardwareDetector._detect_ram()
        vram_gb, vram_used_gb, gpu_name = HardwareDetector._detect_vram()
        cpu_name, cpu_cores, cpu_freq_mhz = HardwareDetector._detect_cpu()

        profile = HardwareProfile(
            os_type=os_type,
            is_apple_silicon=is_apple_silicon,
            vram_gb=vram_gb,
            vram_used_gb=vram_used_gb,
            gpu_name=gpu_name,
            ram_gb=ram_gb,
            ram_available_gb=ram_available_gb,
            cpu_name=cpu_name,
            cpu_cores=cpu_cores,
            cpu_freq_mhz=cpu_freq_mhz,
        )

        # Swarm-mode gate from the configurable effective-memory thresholds.
        effective_gb = effective_vram_gb(profile)
        if effective_gb >= VRAM_FULL_SWARM_GB:
            profile.suggested_mode = "FULL_SWARM"
        elif effective_gb >= VRAM_MICRO_SWARM_GB:
            profile.suggested_mode = "MICRO_SWARM"
        else:
            profile.suggested_mode = "SEQUENTIAL"

        return profile

    @staticmethod
    def _detect_ram() -> tuple[float, float]:
        try:
            import psutil
            vm = psutil.virtual_memory()
            return vm.total / (1024 ** 3), vm.available / (1024 ** 3)
        except Exception:
            return 0.0, 0.0

    @staticmethod
    def _detect_vram() -> tuple[float, float, Optional[str]]:
        try:
            import pynvml  # type: ignore[import-untyped]
            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            name: str = pynvml.nvmlDeviceGetName(handle)
            gpu_name = name.decode("utf-8") if isinstance(name, bytes) else name
            vram_total = info.total / (1024 ** 3)
            vram_used = info.used / (1024 ** 3)
            pynvml.nvmlShutdown()
            return vram_total, vram_used, gpu_name
        except Exception:
            return 0.0, 0.0, None

    @staticmethod
    def _detect_cpu() -> tuple[str, int, float]:
        """Returns (cpu_name, physical_cores, max_freq_mhz). No new dependencies."""
        import subprocess

        name = ""
        os_type = platform.system()
        try:
            if os_type == "Darwin":
                name = subprocess.check_output(
                    ["sysctl", "-n", "machdep.cpu.brand_string"],
                    text=True, timeout=2,
                ).strip()
            elif os_type == "Linux":
                with open("/proc/cpuinfo", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("model name"):
                            name = line.split(":", 1)[1].strip()
                            break
            elif os_type == "Windows":
                import winreg
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
                ) as key:
                    name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        except Exception:
            pass

        if not name:
            name = platform.processor() or "Unknown CPU"

        cores: int = 0
        freq_mhz: float = 0.0
        try:
            import psutil
            cores = psutil.cpu_count(logical=False) or 0
            fi = psutil.cpu_freq()
            if fi:
                freq_mhz = fi.max or fi.current or 0.0
        except Exception:
            pass

        return name, cores, freq_mhz
