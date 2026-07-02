"""Runtime resource probing for worker task constraints.

Two things live here:

* Parsers for the human-friendly values used in job-script directives
  (:func:`parse_size` for byte sizes like ``20GB``; :func:`parse_percent`
  for values like ``80%``).
* Probes for the machine's *current* resource state: :func:`query_gpus`
  (via ``nvidia-smi``) and :func:`available_ram` (via ``psutil``).

Everything is expressed in bytes / percent so that the constraint layer in
:mod:`toolbox.tasker.constraints` can compare declared requirements against
live readings without unit juggling.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional

# --------------------------------------------------------------------------- #
# Value parsing
# --------------------------------------------------------------------------- #

# Binary (1024-based) multipliers. Bare ``GB``/``MB`` are treated as binary to
# match how GPU memory is conventionally quoted (nvidia-smi reports MiB), so
# ``20GB`` and ``20GiB`` mean the same thing here.
_SIZE_UNITS = {
    "": 1,
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "KIB": 1024,
    "M": 1024 ** 2,
    "MB": 1024 ** 2,
    "MIB": 1024 ** 2,
    "G": 1024 ** 3,
    "GB": 1024 ** 3,
    "GIB": 1024 ** 3,
    "T": 1024 ** 4,
    "TB": 1024 ** 4,
    "TIB": 1024 ** 4,
}

_SIZE_RE = re.compile(r"^\s*(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[a-zA-Z]*)\s*$")
_PERCENT_RE = re.compile(r"^\s*(?P<num>\d+(?:\.\d+)?)\s*%?\s*$")


def parse_size(text: str) -> int:
    """Parse a human byte size (e.g. ``"20GB"``, ``"100MB"``) into bytes.

    Units are case-insensitive and binary (1024-based). A bare number is
    interpreted as bytes.

    Raises:
        ValueError: if *text* is not a recognisable size.
    """
    match = _SIZE_RE.match(text)
    if match is None:
        raise ValueError(f"Invalid size: {text!r}")
    unit = match.group("unit").upper()
    if unit not in _SIZE_UNITS:
        raise ValueError(f"Unknown size unit in {text!r}")
    return int(float(match.group("num")) * _SIZE_UNITS[unit])


def parse_percent(text: str) -> float:
    """Parse a percentage (e.g. ``"80%"`` or ``"80"``) into a float in [0, 100].

    Raises:
        ValueError: if *text* is not a recognisable percentage.
    """
    match = _PERCENT_RE.match(text)
    if match is None:
        raise ValueError(f"Invalid percentage: {text!r}")
    return float(match.group("num"))


# --------------------------------------------------------------------------- #
# GPU probing
# --------------------------------------------------------------------------- #

@dataclass
class GpuInfo:
    """A snapshot of one GPU's memory and utilisation."""

    index: int
    mem_total: int  # bytes
    mem_used: int   # bytes
    mem_free: int   # bytes
    util: float     # percent, 0-100


_NVIDIA_SMI_QUERY = (
    "index,memory.total,memory.used,memory.free,utilization.gpu"
)


def query_gpus() -> List[GpuInfo]:
    """Return a snapshot of every visible GPU via ``nvidia-smi``.

    Returns an empty list when ``nvidia-smi`` is not installed or fails for
    any reason (no NVIDIA driver, transient error, unparseable output). The
    constraint layer treats "GPU constraint declared but no GPUs found" as
    unsatisfiable, so returning ``[]`` here is safe.

    ``nvidia-smi`` reports memory in MiB and utilisation as a percentage; both
    are normalised (memory to bytes) before being returned.
    """
    if shutil.which("nvidia-smi") is None:
        return []

    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                f"--query-gpu={_NVIDIA_SMI_QUERY}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    if out.returncode != 0:
        return []

    gpus: List[GpuInfo] = []
    mib = 1024 ** 2
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = [f.strip() for f in line.split(",")]
        if len(fields) != 5:
            continue
        try:
            index, total, used, free, util = fields
            gpus.append(
                GpuInfo(
                    index=int(index),
                    mem_total=int(float(total)) * mib,
                    mem_used=int(float(used)) * mib,
                    mem_free=int(float(free)) * mib,
                    util=float(util),
                )
            )
        except ValueError:
            # Skip a malformed row rather than failing the whole probe.
            continue
    return gpus


# --------------------------------------------------------------------------- #
# RAM probing
# --------------------------------------------------------------------------- #

def available_ram() -> Optional[int]:
    """Return currently available system RAM in bytes.

    Uses ``psutil.virtual_memory().available`` (memory that can be given to
    processes without swapping). Returns ``None`` if ``psutil`` is not
    importable, which the constraint layer treats as unsatisfiable for any
    RAM constraint.
    """
    try:
        import psutil
    except ImportError:
        return None
    return int(psutil.virtual_memory().available)
