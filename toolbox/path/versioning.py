from __future__ import annotations

import os
import re
from typing import Union


def get_versions(path: str) -> list[str]:
    """Return all existing versioned paths for the given base path.

    Matches both naming styles: ``name_vN.ext`` and ``name.ext_vN``.

    Args:
        path: Base file or directory path to search versions for.

    Returns:
        Sorted list of full paths matching the versioned pattern.
    """
    basedir, filename = os.path.split(path)
    name, extension = os.path.splitext(filename)
    basedir = basedir or "."

    results = []
    try:
        entries = os.listdir(basedir)
    except FileNotFoundError:
        return results

    pattern_before = re.compile(rf"^{re.escape(name)}_v(\d+){re.escape(extension)}$")
    pattern_after = re.compile(rf"^{re.escape(name)}{re.escape(extension)}_v(\d+)$")

    for entry in entries:
        if pattern_before.match(entry) or pattern_after.match(entry):
            results.append(os.path.join(basedir, entry))

    return sorted(results)


def path_versioned(
    path: str,
    version: Union[int, str] = "next",
    before_extension: bool = False,
) -> str:
    """Return a versioned variant of the given path.

    If ``version`` is ``"next"``, returns ``path`` unchanged when it does not
    exist, otherwise increments ``_v1``, ``_v2``, … until finding an unused path.
    If ``version`` is an integer, the versioned path is returned directly without
    any existence check.

    Args:
        path: Original file or directory path.
        version: Explicit version number, or ``"next"`` to auto-increment.
        before_extension: If ``True``, insert the version tag before the file
            extension (``name_vN.ext``); otherwise append after (``name.ext_vN``).

    Returns:
        The versioned path string.
    """
    basedir, filename = os.path.split(path)
    name, extension = os.path.splitext(filename)
    basedir = basedir or "."

    if version != "next":
        if before_extension:
            filename = f"{name}_v{version}{extension}"
        else:
            filename = f"{name}{extension}_v{version}"
        return os.path.join(basedir, filename)

    if not os.path.exists(path):
        return path

    n = 1
    while True:
        if before_extension:
            candidate = os.path.join(basedir, f"{name}_v{n}{extension}")
        else:
            candidate = os.path.join(basedir, f"{name}{extension}_v{n}")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def makedir_versioned(path: str) -> str:
    """Create and return a versioned directory path.

    If ``path`` does not exist, creates it and returns it as-is. Otherwise
    determines the next unused versioned path, creates that directory, and
    returns it.

    Args:
        path: Base directory path.

    Returns:
        The path of the directory that was created.
    """
    if not os.path.exists(path):
        os.makedirs(path)
        return path
    new_path = path_versioned(path, version="next")
    os.makedirs(new_path)
    return new_path
