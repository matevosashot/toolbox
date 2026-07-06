from __future__ import annotations

import inspect
import os
import sys
from typing import Optional


def _caller_file_via_frame(stacklevel: int = 1) -> str:
    """Return the filename of a calling frame via :func:`sys._getframe`.

    Fast (O(1), no I/O) but relies on a CPython implementation detail and
    may not be available or may be slow on other Python implementations
    (e.g. PyPy needs to materialize the frame, breaking JIT optimizations).

    Args:
        stacklevel: ``1`` returns the direct caller of this helper, ``2`` its
            caller, and so on. Follows the same convention as
            :func:`warnings.warn`.

    Raises:
        AttributeError: if the running interpreter does not expose
            ``sys._getframe``.
        ValueError: if ``stacklevel`` is deeper than the current call stack.
    """
    return sys._getframe(stacklevel).f_code.co_filename


def _caller_file_via_stack(stacklevel: int = 1) -> str:
    """Return the filename of a calling frame via :func:`inspect.stack`.

    Portable across Python implementations but slower: it walks the entire
    call stack and allocates a list of ``FrameInfo`` records. ``context=0``
    is passed to skip the (more expensive) source-line lookup.

    Args:
        stacklevel: Same convention as :func:`_caller_file_via_frame`.
    """
    return inspect.stack(0)[stacklevel].filename


def _is_synthetic_caller(caller_file: str) -> bool:
    """Return whether ``caller_file`` is not a real on-disk source file.

    Interactive environments run code from synthetic files that do not
    reflect the user's project layout, so ascending their ancestors is
    meaningless. Examples:

    * IPython/Jupyter (VS Code, JupyterLab) write each cell to a temporary
      file such as ``/tmp/ipykernel_3142250/1914498567.py``.
    * Older IPython uses angle-bracket names like ``<ipython-input-1-...>``.
    * ``exec``/``eval`` of a string reports ``<string>``.
    """
    base = os.path.basename(caller_file)
    if caller_file.startswith("<") or base.startswith("<"):
        return True
    if "ipykernel_" in caller_file.replace(os.sep, "/"):
        return True
    return not os.path.isabs(caller_file)


def update(
    depth: int = 1,
    ancestor_name: Optional[str] = None,
    contains: Optional[str] = None
) -> str:
    """Insert an ancestor directory of the *caller's* file into ``sys.path``.

    The function inspects the call stack to find the file from which it was
    invoked, then ascends by one of three strategies and inserts the
    resulting path at the front of ``sys.path``:

    * a fixed number of levels (``depth``),
    * until a directory whose basename matches ``ancestor_name``, or
    * until a directory that contains a child entry named ``contains``.

    ``ancestor_name`` and ``contains`` are mutually exclusive; when either is
    given, ``depth`` is ignored.

    In interactive environments (IPython/Jupyter, including VS Code
    notebooks) the caller executes from a synthetic file that does not
    reflect the project layout, so the current working directory is used as
    the starting point instead of the caller's file location.

    Args:
        depth: Number of directory levels to ascend above the caller's file.
            ``depth=0`` adds the directory directly containing the caller;
            ``depth=1`` (the default) adds its parent, and so on. Ignored
            when ``ancestor_name`` or ``contains`` is provided.
        ancestor_name: If given, ascend until the basename of the current
            directory equals this string. The search starts at the directory
            containing the caller's file, so passing the caller's own
            directory name returns immediately without ascending.
        contains: If given, ascend until the current directory contains a
            child entry (file or directory) with this name. The search starts
            at the directory containing the caller's file. Inserting the
            resulting directory makes ``contains`` importable/accessible from
            ``sys.path``. Mutually exclusive with ``ancestor_name``.

    Returns:
        The directory that was inserted into ``sys.path``.

    Raises:
        ValueError: If both ``ancestor_name`` and ``contains`` are given; if
            ``ancestor_name``/``contains`` is given and no ancestor directory
            matches (i.e. the filesystem root is reached first); or if
            ``depth`` ascends past the filesystem root.
    """
    if ancestor_name is not None and contains is not None:
        raise ValueError("Pass either 'ancestor_name' or 'contains', not both")

    try:
        caller_file = _caller_file_via_frame(stacklevel=2)
    except (AttributeError, ValueError):
        caller_file = _caller_file_via_stack(stacklevel=2)

    if _is_synthetic_caller(caller_file):
        # Interactive/Jupyter cells run from a synthetic file; the kernel's
        # working directory reflects the real project location instead.
        path = os.path.abspath(os.getcwd())
    else:
        path = os.path.dirname(os.path.abspath(caller_file))

    if ancestor_name is not None:
        while os.path.basename(path) != ancestor_name:
            parent = os.path.dirname(path)
            if parent == path:
                raise ValueError(
                    f"Directory named {ancestor_name!r} not found in ancestors "
                    f"of {caller_file!r}"
                )
            path = parent
    elif contains is not None:
        while not os.path.exists(os.path.join(path, contains)):
            parent = os.path.dirname(path)
            if parent == path:
                raise ValueError(
                    f"No ancestor of {caller_file!r} contains an entry named "
                    f"{contains!r}"
                )
            path = parent
    else:
        for _ in range(depth):
            parent = os.path.dirname(path)
            if parent == path:
                raise ValueError("Too deep: reached the filesystem root")
            path = parent

    sys.path.insert(0, path)
    return path


def find_in_path(path: str) -> str:
    """Locate a file or directory either at ``path`` or under any ``sys.path`` entry.

    Resolution order:

    1. If ``path`` exists as given (absolute, or relative to the current
       working directory), its absolute form is returned.
    2. If ``path`` is absolute and does not exist, :class:`ValueError` is
       raised — an absolute path cannot be meaningfully resolved against
       ``sys.path``.
    3. Otherwise, each entry of ``sys.path`` is joined with ``path`` in order
       and the first existing result is returned as an absolute path.

    Args:
        path: File or directory to find. May be absolute (in which case it
            must already exist) or relative. Must be non-empty.

    Returns:
        The absolute path of an existing file or directory.

    Raises:
        ValueError: If ``path`` is empty or is absolute and does not exist.
        FileNotFoundError: If ``path`` is relative and is not found under any
            ``sys.path`` entry.
    """
    if not path:
        raise ValueError("path must be a non-empty string")

    if os.path.exists(path):
        return os.path.abspath(path)
    if os.path.isabs(path):
        raise ValueError(f"Absolute path {path!r} does not exist")

    for base_dir in sys.path:
        abs_path = os.path.join(base_dir, path)
        if os.path.exists(abs_path):
            return os.path.abspath(abs_path)
    raise FileNotFoundError(f"Path {path!r} not found in sys.path")
