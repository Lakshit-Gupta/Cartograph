"""Jellyfin-aware scaler.

The Pi co-hosts Jellyfin. When Jellyfin is actively transcoding (CPU > 60% on
ffmpeg cgroup), we drop browser replicas to 1 to keep playback smooth.
Polled by the scheduler.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(slots=True)
class HostLoad:
    cpu_percent: float
    jellyfin_busy: bool
    mem_available_mb: int


def read_host_load() -> HostLoad:
    cpu = 0.0
    mem_mb = 0
    try:
        with open("/proc/loadavg", encoding="utf-8") as fp:
            cpu = float(fp.read().split()[0])
    except OSError:
        pass
    try:
        with open("/proc/meminfo", encoding="utf-8") as fp:
            for line in fp:
                if line.startswith("MemAvailable:"):
                    mem_mb = int(line.split()[1]) // 1024
                    break
    except OSError:
        pass
    return HostLoad(
        cpu_percent=cpu,
        jellyfin_busy=_jellyfin_transcoding(),
        mem_available_mb=mem_mb,
    )


def _jellyfin_transcoding() -> bool:
    if shutil.which("pgrep") is None:
        return False
    try:
        r = subprocess.run(
            ["pgrep", "-fa", "ffmpeg.*jellyfin"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        return bool(r.stdout.strip())
    except Exception:
        return False


def desired_browser_replicas(load: HostLoad, default: int = 3) -> int:
    if load.jellyfin_busy:
        return 1
    if load.mem_available_mb < 800:
        return 1
    if load.cpu_percent > 4.0:
        return max(1, default - 1)
    return default
