"""Opt-in, CI-skipped real-memory hardware stress script (DEBT-067).

Applies REAL memory pressure and observes `shared.hardware.HardwareDetector`'s
actual probing path degrade `suggested_mode` under genuine load. This is
deliberately distinct from `tests/chaos/test_hardware_stress_sim.py`, which
injects a synthetic starved `HardwareProfile` instead of allocating anything —
that test is the CI-safe, deterministic contract check for the graceful-
degradation *routing* logic; this script is the opt-in complement that
exercises the detector's *real* psutil/pynvml probing path, which the
synthetic test cannot reach by construction. Real allocation is inherently
non-deterministic and can destabilize the host, which is exactly why it is
never invoked by pytest or any CI lane (plain script, no `test_` prefix, no
`__init__.py` in this directory).

Usage:
    AILIENANT_ENABLE_HW_STRESS=1 python scripts/hardware_stress_sim.py --target-free-gb 1.0

VRAM pressure (`--vram`) is best-effort. Probing whether a GPU exists (via
`pynvml`, already an optional project dependency) is not the same as being
able to consume its memory — that needs a GPU compute framework (torch/cupy),
which this project does not depend on. Per Charter Sec. 9 (no new heavyweight
dependency for a narrow need — the precedent being `scipy` rejected in favor
of a hand-rolled `degree_centrality`), this script does not add one either:
when a GPU is present but no such framework is importable, VRAM stress is
explicitly skipped with a printed reason, never silently omitted.
"""
from __future__ import annotations

import argparse
import gc
import os
import sys
from pathlib import Path
from typing import List, Optional

# Runs as a standalone script (not via pytest), so nothing has put ailienant-core
# on sys.path yet — the interpreter only seeds sys.path[0] with this file's own
# directory (scripts/). Insert the package root explicitly before `shared.*`
# becomes importable (mirrors tests/e2e/seed_dashboard_fixture.py's identical
# bootstrap for the same reason).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_ENV_GATE = "AILIENANT_ENABLE_HW_STRESS"

# Never push the host below this much free RAM, regardless of what
# --target-free-gb requests — a caller-supplied floor of 0 (or negative) must
# not be allowed to threaten the host's own stability.
_MIN_SAFE_FREE_GB: float = 0.5
_CHUNK_MB: int = 128
# Escape hatch: if psutil's reported `available` doesn't budge for this many
# consecutive chunks (OS reclaim racing the allocator, or a misreporting
# host), stop rather than loop toward an OOM kill chasing a target that will
# never be reached.
_MAX_STALL_CHUNKS: int = 20


def _refuse_unless_opted_in() -> None:
    if os.environ.get(_ENV_GATE) != "1":
        print(
            f"[hardware_stress_sim] refusing to run: set {_ENV_GATE}=1 to opt in.\n"
            "This script allocates REAL memory and can be destabilizing; it is "
            "never invoked by pytest or CI.",
            file=sys.stderr,
        )
        sys.exit(1)


def _stress_ram(target_free_gb: float) -> List[bytearray]:
    """Allocate real RAM in bounded chunks until `available` drops to the
    (floor-clamped) target, a stall is detected, or the host raises
    MemoryError first. Returns the allocated chunks so the caller releases
    them explicitly in a `finally` — never left dangling on any exit path.
    """
    import psutil

    floor_gb = max(target_free_gb, _MIN_SAFE_FREE_GB)
    chunks: List[bytearray] = []
    stall_count = 0
    prev_available = psutil.virtual_memory().available

    print(f"[hardware_stress_sim] targeting {floor_gb:.2f} GB free (floor-clamped)...")
    while True:
        available_gb = psutil.virtual_memory().available / (1024 ** 3)
        if available_gb <= floor_gb:
            print(f"[hardware_stress_sim] reached {available_gb:.2f} GB free — stopping.")
            break
        try:
            chunks.append(bytearray(_CHUNK_MB * 1024 * 1024))
        except MemoryError:
            print(
                "[hardware_stress_sim] host raised MemoryError before reaching "
                "target — stopping."
            )
            break

        current_available = psutil.virtual_memory().available
        if current_available >= prev_available:
            stall_count += 1
            if stall_count >= _MAX_STALL_CHUNKS:
                print(
                    "[hardware_stress_sim] `available` isn't decreasing after "
                    f"{_MAX_STALL_CHUNKS} chunks (OS reclaim or misreport) — "
                    "stopping rather than risk an unbounded loop."
                )
                break
        else:
            stall_count = 0
        prev_available = current_available

    return chunks


def _try_stress_vram() -> Optional[str]:
    """Best-effort VRAM pressure. Returns a skip reason (never raises) when no
    compute framework is available to actually consume GPU memory — see the
    module docstring for why this script does not add one.
    """
    try:
        import pynvml  # type: ignore[import-untyped]
        pynvml.nvmlInit()
        pynvml.nvmlDeviceGetHandleByIndex(0)
        pynvml.nvmlShutdown()
    except Exception:
        return "no NVIDIA GPU/driver detected via pynvml"

    try:
        import torch  # type: ignore[import-not-found] # noqa: F401 — optional; deliberately not a project dependency
    except ImportError:
        return (
            "a GPU is present, but consuming real VRAM needs a compute framework "
            "(torch/cupy) this project does not depend on (Charter Sec. 9); skipping"
        )
    return None  # pragma: no cover — only reached when torch happens to be installed


def main() -> None:
    _refuse_unless_opted_in()

    try:
        import psutil  # noqa: F401 — presence check; _stress_ram does the real import
    except ImportError:
        print(
            "[hardware_stress_sim] psutil is not installed — cannot measure `available` "
            "RAM to know when to stop, so refusing rather than allocate unguided.",
            file=sys.stderr,
        )
        sys.exit(1)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-free-gb", type=float, default=1.0,
        help="Stop allocating once free RAM drops to (at least) this many GB.",
    )
    parser.add_argument(
        "--vram", action="store_true",
        help="Also attempt VRAM pressure (best-effort; see module docstring).",
    )
    args = parser.parse_args()

    from shared.hardware import HardwareDetector

    baseline = HardwareDetector.detect()
    print(f"[hardware_stress_sim] baseline suggested_mode={baseline.suggested_mode}")

    chunks: List[bytearray] = []
    try:
        chunks = _stress_ram(args.target_free_gb)

        if args.vram:
            skip_reason = _try_stress_vram()
            if skip_reason:
                print(f"[hardware_stress_sim] VRAM stress skipped: {skip_reason}")

        under_pressure = HardwareDetector.detect()
        print(
            f"[hardware_stress_sim] under pressure suggested_mode="
            f"{under_pressure.suggested_mode} (was {baseline.suggested_mode})"
        )
        if under_pressure.suggested_mode == baseline.suggested_mode:
            # shared.hardware.effective_vram_gb gates suggested_mode on GPU VRAM
            # headroom for every platform EXCEPT Apple Silicon, where system RAM
            # is the direct gate. Diagnose which case this is rather than print a
            # one-size-fits-all "try a lower target" that would be misleading on
            # a non-Apple host with no GPU (RAM pressure cannot move the gate at
            # all there — it isn't in the formula).
            if baseline.is_apple_silicon:
                print(
                    "[hardware_stress_sim] WARNING: suggested_mode did not degrade "
                    "— on Apple Silicon, RAM IS the direct gate; try a lower "
                    "--target-free-gb."
                )
            elif baseline.vram_gb <= 0.0:
                print(
                    "[hardware_stress_sim] NOTE: suggested_mode cannot degrade via RAM "
                    "pressure on this host — no GPU was detected, and on a non-Apple "
                    "platform suggested_mode is gated by GPU VRAM headroom "
                    "(shared.hardware.effective_vram_gb), not system RAM. This is a "
                    "structural limitation of RAM-only pressure, not a sizing issue "
                    "with --target-free-gb."
                )
            else:
                print(
                    "[hardware_stress_sim] NOTE: suggested_mode is gated by GPU VRAM "
                    "headroom on this platform, not system RAM — RAM pressure alone "
                    "cannot move it. Retry with --vram (best-effort; needs a compute "
                    "framework this project doesn't depend on — see module docstring)."
                )
    finally:
        # Always release, even on an exception or Ctrl-C, so the script never
        # leaves the host memory-pressured after it exits (Charter Sec. 5.1).
        chunks.clear()
        gc.collect()
        released = HardwareDetector.detect()
        print(f"[hardware_stress_sim] after release suggested_mode={released.suggested_mode}")


if __name__ == "__main__":
    main()
