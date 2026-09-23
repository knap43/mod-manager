"""XDG locations and small filesystem helpers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from modmanager import APP_ID


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_ID


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_ID


def default_instances_dir() -> Path:
    return data_home() / "instances"


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def write_json(path: Path, data) -> None:
    """Write JSON atomically so a crash never leaves a truncated file behind."""
    write_text(path, json.dumps(data, indent=2, sort_keys=False) + "\n")


def write_text(path: Path, text: str, encoding: str = "utf-8", newline: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, errors="replace", newline=newline) as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def find_child_ci(parent: Path, name: str) -> Path | None:
    """Return the entry in ``parent`` whose name matches ``name`` case-insensitively."""
    exact = parent / name
    if exact.exists():
        return exact
    lowered = name.lower()
    try:
        for entry in os.scandir(parent):
            if entry.name.lower() == lowered:
                return Path(entry.path)
    except FileNotFoundError:
        pass
    return None


def same_filesystem(a: Path, b: Path) -> bool:
    """True when hard links between ``a`` and ``b`` are possible."""

    def existing(p: Path) -> Path:
        p = p.absolute()
        while not p.exists() and p != p.parent:
            p = p.parent
        return p

    try:
        return existing(a).stat().st_dev == existing(b).stat().st_dev
    except OSError:
        return False
