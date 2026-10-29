"""Schede video presenti, di qualunque produttore: nome e memoria.

    NVIDIA   nvidia-smi (arriva con il driver)
    AMD      i file del driver amdgpu in /sys: nessun programma in più

Serve a scegliere modelli e precisione in base alla memoria video. Se c'è più di
una scheda conta quella con più memoria (di solito la dedicata, non quella
integrata nel processore).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

AMD_VENDOR_ID = "0x1002"


@dataclass(frozen=True)
class Gpu:
    vendor: str      # "NVIDIA" | "AMD"
    name: str
    vram_mb: int


def nvidia_gpus() -> list[Gpu]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return []  # nvidia-smi rimasto installato senza scheda NVIDIA
    gpus = []
    for line in out.splitlines():
        name, _, mem = line.rpartition(",")
        if mem.strip().isdigit():
            gpus.append(Gpu("NVIDIA", name.strip(), int(mem.strip())))
    return gpus


def amd_gpus(drm: Path = Path("/sys/class/drm")) -> list[Gpu]:
    gpus = []
    for card in sorted(drm.glob("card*")):
        if not re.fullmatch(r"card\d+", card.name):   # card1-DP-1 ecc. sono le uscite video
            continue
        dev = card / "device"
        try:
            if (dev / "vendor").read_text().strip() != AMD_VENDOR_ID:
                continue
            vram = int((dev / "mem_info_vram_total").read_text().strip()) // 1024**2
        except (OSError, ValueError):
            continue
        gpus.append(Gpu("AMD", _amd_name(dev), vram))
    return gpus


def _amd_name(dev: Path) -> str:
    """Nome commerciale da lspci ("Navi 44 [Radeon RX 9060 XT]" → "Radeon RX 9060 XT")."""
    exe = shutil.which("lspci")
    if exe:
        try:
            out = subprocess.run([exe, "-s", dev.resolve().name], capture_output=True, text=True,
                                 timeout=5, check=True).stdout
            m = re.search(r"\[([^\]]*Radeon[^\]]*)\]", out) or re.search(r":\s*([^:]+?)(?: \(rev [^)]*\))?$", out)
            if m:
                return m.group(1).strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return "Radeon"


def detect() -> list[Gpu]:
    return nvidia_gpus() + amd_gpus()


def main_gpu() -> Gpu | None:
    gpus = detect()
    return max(gpus, key=lambda g: g.vram_mb) if gpus else None
