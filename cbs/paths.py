"""Runtime storage locations shared by the Python entry points.

Only bundled resources belong under ROOT; resolving paths never creates files.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def resolve_paths(data_dir=None, characters_root=None, *, root=None):
    """Return (data, characters); explicit storage also isolates partner files."""
    root = Path(root) if root is not None else ROOT
    explicit = data_dir or os.environ.get("CBS_DATA_DIR")
    development = (root / ".git").exists()
    if explicit:
        data = Path(explicit)
    elif development:
        data = root / "data"
    else:
        local = os.environ.get("LOCALAPPDATA")
        data = (Path(local) if local else Path.home() / "AppData" / "Local") / "Awalia"
    characters = Path(characters_root) if characters_root is not None else (
        root / "characters" if development and not explicit else data / "characters")
    return data, characters
