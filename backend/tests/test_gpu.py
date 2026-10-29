"""Riconoscimento delle schede video (NVIDIA, AMD, nessuna) senza hardware vero."""

from __future__ import annotations

import subprocess

from app.pipeline import gpu
from app.pipeline.gpu import Gpu


def _amd_card(drm, name, vram_bytes, vendor="0x1002"):
    dev = drm / name / "device"
    dev.mkdir(parents=True)
    (dev / "vendor").write_text(vendor + "\n")
    (dev / "mem_info_vram_total").write_text(f"{vram_bytes}\n")


def test_amd_from_sysfs(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu.shutil, "which", lambda exe: None)       # niente lspci: nome generico
    _amd_card(tmp_path, "card0", 512 * 1024**2)                      # integrata nel processore
    _amd_card(tmp_path, "card1", 16304 * 1024**2)                    # dedicata
    _amd_card(tmp_path, "card2", 8 * 1024**3, vendor="0x8086")       # Intel: ignorata
    (tmp_path / "card1-DP-1").mkdir()                                # un'uscita video, non una scheda
    assert gpu.amd_gpus(tmp_path) == [Gpu("AMD", "Radeon", 512), Gpu("AMD", "Radeon", 16304)]


def test_amd_name_from_lspci(tmp_path, monkeypatch):
    _amd_card(tmp_path, "card1", 16 * 1024**3)
    monkeypatch.setattr(gpu.shutil, "which", lambda exe: "/usr/bin/lspci")
    out = "29:00.0 VGA compatible controller: Advanced Micro Devices, Inc. [AMD/ATI] Navi 44 [Radeon RX 9060 XT] (rev c0)\n"
    monkeypatch.setattr(gpu.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout=out))
    assert gpu.amd_gpus(tmp_path)[0].name == "Radeon RX 9060 XT"


def test_nvidia_from_nvidia_smi(monkeypatch):
    monkeypatch.setattr(gpu.shutil, "which", lambda exe: "/usr/bin/nvidia-smi")
    out = "NVIDIA GeForce RTX 4060, 8188\nNVIDIA RTX A2000, 12282\n"
    monkeypatch.setattr(gpu.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout=out))
    assert gpu.nvidia_gpus() == [Gpu("NVIDIA", "NVIDIA GeForce RTX 4060", 8188), Gpu("NVIDIA", "NVIDIA RTX A2000", 12282)]


def test_nvidia_smi_left_without_card(monkeypatch):
    """Dopo il passaggio a AMD nvidia-smi può restare installato ma fallire."""
    monkeypatch.setattr(gpu.shutil, "which", lambda exe: "/usr/bin/nvidia-smi")

    def fail(*a, **kw):
        raise subprocess.CalledProcessError(9, a[0])

    monkeypatch.setattr(gpu.subprocess, "run", fail)
    assert gpu.nvidia_gpus() == []


def test_main_gpu_is_the_largest(monkeypatch):
    monkeypatch.setattr(gpu, "nvidia_gpus", lambda: [])
    monkeypatch.setattr(gpu, "amd_gpus", lambda: [Gpu("AMD", "integrata", 512), Gpu("AMD", "RX 9060 XT", 16304)])
    assert gpu.main_gpu() == Gpu("AMD", "RX 9060 XT", 16304)
    monkeypatch.setattr(gpu, "amd_gpus", lambda: [])
    assert gpu.main_gpu() is None
