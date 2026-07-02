"""Resource constraints declared inside a job script.

A job script may advertise the resources it needs via header comment lines
(``#WORKER_*`` directives). A :class:`Worker` parses these *before* claiming a
task and only runs the task when the machine's current resources satisfy them;
otherwise the task is left in ``pending/`` and re-checked on the next poll.

Recognised directives (scanned from the top of the file, stopping at the first
non-comment, non-blank line)::

    #WORKER_GPU_MEM 20GB      # min free memory required on each target GPU
    #WORKER_GPU_LOAD 80%      # each target GPU's utilisation must be < 80%
    #WORKER_MEM 100GB         # min available system RAM
    #WORKER_GPU_DEVICES 0,1   # GPU indices the task uses (also exported as
                              #   CUDA_VISIBLE_DEVICES to the task)

Target GPU set resolution (each GPU in the set must *individually* satisfy the
memory and load constraints):

1. ``#WORKER_GPU_DEVICES`` if present, else
2. ``CUDA_VISIBLE_DEVICES`` from the worker's environment, else
3. all GPUs on the machine.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Tuple

from .resources import GpuInfo, parse_percent, parse_size

# Directive lines look like ``#WORKER_GPU_MEM 20GB`` with an optional space
# after ``#`` and any case for the keyword. Only the header comment block of a
# script is scanned, so a bare ``#`` prefix is enough to identify a directive.
_DIRECTIVE_RE = re.compile(
    r"^#\s*(?P<key>WORKER_[A-Z_]+)\b\s*(?P<value>.*?)\s*$",
    re.IGNORECASE,
)


def _human_bytes(num: int) -> str:
    """Format a byte count with a binary unit, for log messages."""
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}TiB"


@dataclass
class Constraints:
    """Resource requirements parsed from a job script's header.

    All fields are optional; a script with no directives yields an empty
    :class:`Constraints` (see :meth:`is_empty`). ``errors`` collects any
    malformed directive values so the worker can treat the task as
    unsatisfiable instead of crashing.
    """

    gpu_mem_bytes: Optional[int] = None
    gpu_load_pct: Optional[float] = None
    ram_bytes: Optional[int] = None
    gpu_devices: Optional[List[int]] = None
    errors: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #

    def is_empty(self) -> bool:
        """True if there is nothing to check (no directives, no parse errors)."""
        return (
            self.gpu_mem_bytes is None
            and self.gpu_load_pct is None
            and self.ram_bytes is None
            and self.gpu_devices is None
            and not self.errors
        )

    @property
    def has_gpu_constraint(self) -> bool:
        return self.gpu_mem_bytes is not None or self.gpu_load_pct is not None

    def cuda_visible_devices(self) -> Optional[str]:
        """Value to export as ``CUDA_VISIBLE_DEVICES``, or ``None``.

        Only set when the script explicitly declares ``#WORKER_GPU_DEVICES``;
        otherwise the task inherits whatever the worker's environment has.
        """
        if self.gpu_devices is None:
            return None
        return ",".join(str(d) for d in self.gpu_devices)

    # ------------------------------------------------------------------ #

    def _target_gpu_indices(
        self, env: Optional[Mapping[str, str]]
    ) -> Optional[List[int]]:
        """Resolve which GPU indices the task will use.

        Returns ``None`` to mean "all GPUs" (no explicit selection), or an
        explicit list of indices (possibly empty when ``CUDA_VISIBLE_DEVICES``
        is set to an empty string).
        """
        if self.gpu_devices is not None:
            return list(self.gpu_devices)

        env = os.environ if env is None else env
        raw = env.get("CUDA_VISIBLE_DEVICES")
        if raw is None:
            return None
        raw = raw.strip()
        if raw == "":
            return []
        indices: List[int] = []
        for token in raw.split(","):
            token = token.strip()
            try:
                indices.append(int(token))
            except ValueError:
                # UUID form or similar; we cannot map it to a probed index,
                # so ignore this token rather than guessing.
                continue
        return indices

    def check(
        self,
        gpus: List[GpuInfo],
        ram: Optional[int],
        env: Optional[Mapping[str, str]] = None,
    ) -> Tuple[bool, List[str]]:
        """Validate this constraint set against a resource snapshot.

        Args:
            gpus: Current GPU snapshot (see :func:`resources.query_gpus`).
            ram: Currently available RAM in bytes, or ``None`` if unknown.
            env: Environment used to resolve ``CUDA_VISIBLE_DEVICES`` when no
                ``#WORKER_GPU_DEVICES`` directive is present. Defaults to
                :data:`os.environ`.

        Returns:
            ``(ok, reasons)`` where ``ok`` is True only if every constraint is
            satisfied. ``reasons`` lists human-readable descriptions of each
            unmet constraint (empty when ``ok`` is True).
        """
        reasons: List[str] = []
        reasons.extend(self.errors)

        if self.ram_bytes is not None:
            if ram is None:
                reasons.append(
                    "RAM constraint set but available RAM is unknown "
                    "(psutil missing?)"
                )
            elif ram < self.ram_bytes:
                reasons.append(
                    f"RAM {_human_bytes(ram)} available < "
                    f"{_human_bytes(self.ram_bytes)} required"
                )

        if self.has_gpu_constraint:
            reasons.extend(self._check_gpus(gpus, env))

        return (not reasons, reasons)

    def _check_gpus(
        self, gpus: List[GpuInfo], env: Optional[Mapping[str, str]]
    ) -> List[str]:
        reasons: List[str] = []

        target_indices = self._target_gpu_indices(env)
        by_index = {g.index: g for g in gpus}

        if target_indices is None:
            targets = list(gpus)
        else:
            targets = [by_index[i] for i in target_indices if i in by_index]
            missing = [i for i in target_indices if i not in by_index]
            if missing:
                reasons.append(
                    "requested GPU(s) not found: "
                    + ",".join(str(i) for i in missing)
                )

        if not targets:
            reasons.append(
                "GPU constraint set but no target GPUs available "
                "(nvidia-smi missing or no matching devices)"
            )
            return reasons

        for gpu in targets:
            if (
                self.gpu_mem_bytes is not None
                and gpu.mem_free < self.gpu_mem_bytes
            ):
                reasons.append(
                    f"GPU {gpu.index} free mem {_human_bytes(gpu.mem_free)} < "
                    f"{_human_bytes(self.gpu_mem_bytes)} required"
                )
            if self.gpu_load_pct is not None and gpu.util >= self.gpu_load_pct:
                reasons.append(
                    f"GPU {gpu.index} load {gpu.util:.0f}% >= "
                    f"{self.gpu_load_pct:.0f}% limit"
                )
        return reasons


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _parse_devices(value: str) -> List[int]:
    return [int(tok.strip()) for tok in value.split(",") if tok.strip() != ""]


def parse_script(path: str) -> Constraints:
    """Parse ``#WORKER_*`` directives from the header of *path*.

    Only the leading block of comment / shebang / blank lines is scanned;
    parsing stops at the first line of actual code, so directives buried below
    real statements are ignored by design (keep them at the top).

    Malformed values are recorded in :attr:`Constraints.errors` rather than
    raised, so a single bad directive does not crash the worker loop.
    """
    constraints = Constraints()

    try:
        with open(path, "r", errors="replace") as fh:
            lines = fh
            _scan(lines, constraints)
    except OSError as exc:
        constraints.errors.append(f"could not read script: {exc}")

    return constraints


def _scan(lines, constraints: Constraints) -> None:
    for raw_line in lines:
        line = raw_line.strip()
        if line == "":
            continue
        if not line.startswith("#"):
            # First real line of code: the header block is over.
            break

        match = _DIRECTIVE_RE.match(line)
        if match is None:
            # A normal comment (including the shebang); keep scanning.
            continue

        key = match.group("key").upper()
        value = match.group("value").strip()
        _apply_directive(constraints, key, value)


def _apply_directive(constraints: Constraints, key: str, value: str) -> None:
    try:
        if key == "WORKER_GPU_MEM":
            constraints.gpu_mem_bytes = parse_size(value)
        elif key == "WORKER_GPU_LOAD":
            constraints.gpu_load_pct = parse_percent(value)
        elif key == "WORKER_MEM":
            constraints.ram_bytes = parse_size(value)
        elif key == "WORKER_GPU_DEVICES":
            constraints.gpu_devices = _parse_devices(value)
        else:
            constraints.errors.append(f"unknown directive {key}")
    except ValueError as exc:
        constraints.errors.append(f"{key}: {exc}")
