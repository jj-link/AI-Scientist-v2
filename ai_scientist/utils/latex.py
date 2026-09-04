"""Portable TeX tool resolution.

Prefers the directory named by AI_SCIENTIST_TEX_BIN_DIR when set, then PATH.
No workstation-specific paths are hardcoded here.
"""

import os
import shutil

TEX_BIN_DIR_ENV = "AI_SCIENTIST_TEX_BIN_DIR"


def resolve_tex_tool(name: str) -> str:
    """Resolve a TeX executable name to a launchable path.

    Resolution order:
      1. ``AI_SCIENTIST_TEX_BIN_DIR``: ``name`` (and ``name.exe`` on Windows)
         inside that directory.
      2. ``PATH`` via :func:`shutil.which`.

    An explicitly configured directory that does not contain the tool raises
    ``FileNotFoundError`` naming the variable and directory; it is never
    silently ignored.
    """
    bin_dir = os.environ.get(TEX_BIN_DIR_ENV)
    if bin_dir:
        for candidate in (name, f"{name}.exe"):
            path = os.path.join(bin_dir, candidate)
            if os.path.isfile(path):
                return path
        raise FileNotFoundError(
            f"{TEX_BIN_DIR_ENV}={bin_dir!r} does not contain {name!r} "
            f"(tried '{name}' and '{name}.exe')."
        )
    resolved = shutil.which(name)
    if resolved:
        return resolved
    raise FileNotFoundError(
        f"TeX tool {name!r} was not found on PATH. Install a TeX distribution "
        f"or set {TEX_BIN_DIR_ENV} to its bin directory."
    )
