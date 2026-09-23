"""Locate Steam libraries, installed games, Proton builds and compatdata prefixes."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


def parse_vdf(text: str) -> dict:
    """Parse Valve's text KeyValues format into nested dicts."""
    tokens = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', re.sub(r"//[^\n]*", "", text))
    stack: list[dict] = [{}]
    key: str | None = None
    for quoted, brace in tokens:
        if brace == "{":
            child: dict = {}
            stack[-1][key or ""] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = quoted
        else:
            stack[-1][key] = quoted.replace("\\\\", "\\")
            key = None
    return stack[0]


def steam_roots() -> list[Path]:
    home = Path.home()
    candidates = [
        home / ".steam" / "root",
        home / ".steam" / "steam",
        home / ".local" / "share" / "Steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",
        home / "snap" / "steam" / "common" / ".local" / "share" / "Steam",
    ]
    roots: list[Path] = []
    seen: set[Path] = set()
    for c in candidates:
        try:
            resolved = c.resolve()
        except OSError:
            continue
        if (resolved / "steamapps").is_dir() and resolved not in seen:
            seen.add(resolved)
            roots.append(resolved)
    return roots


def library_folders() -> list[Path]:
    libs: list[Path] = []
    seen: set[Path] = set()
    for root in steam_roots():
        found = [root]
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                data = parse_vdf(vdf.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                data = {}
            for entry in (data.get("libraryfolders") or {}).values():
                if isinstance(entry, dict) and entry.get("path"):
                    found.append(Path(entry["path"]))
        for lib in found:
            try:
                lib = lib.resolve()
            except OSError:
                continue
            if lib not in seen and (lib / "steamapps").is_dir():
                seen.add(lib)
                libs.append(lib)
    return libs


def find_game(app_id: int, fallback_dir: str | None = None) -> Path | None:
    """Return the install directory of a Steam game, if installed."""
    for lib in library_folders():
        manifest = lib / "steamapps" / f"appmanifest_{app_id}.acf"
        if manifest.is_file():
            try:
                data = parse_vdf(manifest.read_text(encoding="utf-8", errors="replace"))
                installdir = data.get("AppState", {}).get("installdir")
            except OSError:
                installdir = None
            if installdir:
                path = lib / "steamapps" / "common" / installdir
                if path.is_dir():
                    return path
        if fallback_dir:
            path = lib / "steamapps" / "common" / fallback_dir
            if path.is_dir():
                return path
    return None


def find_compatdata(app_id: int) -> Path | None:
    """Return Steam's Proton prefix directory for a game (the folder containing ``pfx``)."""
    for lib in library_folders():
        path = lib / "steamapps" / "compatdata" / str(app_id)
        if (path / "pfx").is_dir():
            return path
    return None


@dataclass(frozen=True)
class ProtonBuild:
    name: str
    path: Path


def proton_builds() -> list[ProtonBuild]:
    """List Proton builds from Steam, compatibilitytools.d and umu's own cache."""
    dirs: list[Path] = []
    for root in steam_roots():
        dirs.append(root / "compatibilitytools.d")
    for lib in library_folders():
        dirs.append(lib / "steamapps" / "common")
    dirs.append(Path("/usr/share/steam/compatibilitytools.d"))
    xdg = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    dirs.append(xdg / "umu" / "compatibilitytools")

    builds: dict[Path, ProtonBuild] = {}
    for d in dirs:
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            if (entry / "proton").is_file():
                resolved = entry.resolve()
                builds.setdefault(resolved, ProtonBuild(entry.name, resolved))
    return sorted(builds.values(), key=lambda b: b.name.lower())
