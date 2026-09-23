"""Script extender (SKSE64, SKSE, F4SE) detection and compatibility checks.

A script extender is installed into the game folder, next to the game's
executable, not into Data: a loader (``skse64_loader.exe``) plus a runtime DLL
built for exactly one game version (``skse64_1_6_1170.dll``). Its plugins live in
``Data/SKSE/Plugins`` and usually come from mods.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from modmanager.core import paths
from modmanager.core.fomod import pe_file_version
from modmanager.core.games import GameDef, ScriptExtender

Version = tuple[int, ...]


def fmt(version: Version | None, parts: int = 4) -> str:
    return ".".join(map(str, version[:parts])) if version else "unknown"


@dataclass
class ScriptExtenderStatus:
    extender: ScriptExtender
    loader: Path | None = None
    version: Version | None = None           # The loader's file version
    runtimes: list[Version] = field(default_factory=list)  # Game versions it has a DLL for
    game_version: Version | None = None

    @property
    def display_version(self) -> str:
        """SKSE writes its version with a leading zero (2.2.6 is stored as 0.2.2.6)."""
        v = self.version
        if not v:
            return "unknown version"
        return fmt(v[1:] if v[0] == 0 else v[:3], 3)

    @property
    def installed(self) -> bool:
        return self.loader is not None

    @property
    def compatible(self) -> bool | None:
        """Whether a runtime DLL matches the game. None when that can't be determined."""
        if not self.installed:
            return False
        if self.game_version is None or not self.runtimes:
            return None
        return self.game_version[:3] in [r[:3] for r in self.runtimes]

    def summary(self) -> str:
        name = self.extender.name
        if not self.installed:
            return f"{name} not installed"
        text = f"{name} {self.display_version}"
        if self.compatible is False:
            text += " (wrong game version)"
        return text

    def details(self) -> str:
        name = self.extender.name
        if not self.installed:
            return (f"{name} was not found in the game folder. Extract its archive (the loader, "
                    f"its DLLs and the Data folder) next to the game's executable.\n{self.extender.url}")
        lines = [f"Loader: {self.loader}", f"Version: {self.display_version}",
                 f"Game version: {fmt(self.game_version)}"]
        if self.runtimes:
            lines.append("Built for game version: " + ", ".join(fmt(r, 3) for r in self.runtimes))
        if self.compatible is False:
            lines.append(f"This {name} does not support your game version; download the build "
                         f"for {fmt(self.game_version, 3)}.")
        elif not self.runtimes:
            lines.append(f"No {self.extender.dll_prefix}*.dll found — the installation is incomplete.")
        return "\n".join(lines)


def check(game: GameDef, game_dir: Path) -> ScriptExtenderStatus | None:
    """Inspect the game folder. Returns None for games without a script extender."""
    se = game.script_extender
    if se is None:
        return None
    status = ScriptExtenderStatus(se)
    binary = paths.find_child_ci(game_dir, game.binary) if game.binary else None
    status.game_version = pe_file_version(binary) if binary else None
    status.loader = paths.find_child_ci(game_dir, se.loader)
    if status.loader is None:
        return status
    status.version = pe_file_version(status.loader)
    pattern = re.compile(rf"^{re.escape(se.dll_prefix)}(\d+)_(\d+)_(\d+)\.dll$", re.I)
    try:
        for entry in os.scandir(game_dir):
            m = pattern.match(entry.name)
            if m:
                status.runtimes.append(tuple(int(x) for x in m.groups()))
    except OSError:
        pass
    status.runtimes.sort()
    return status


def address_library_file(game_version: Version | None) -> str | None:
    """Address Library data file name for a Skyrim SE/AE version, e.g. ``versionlib-1-6-1170-0.bin``."""
    if not game_version or len(game_version) < 4:
        return None
    stem = "versionlib" if game_version[:2] >= (1, 6) else "version"
    return f"{stem}-" + "-".join(map(str, game_version[:4])) + ".bin"


def plugin_dlls(files: set[str], extender: ScriptExtender) -> list[str]:
    """Lower-cased Data paths of script extender plugin DLLs among ``files``."""
    prefix = extender.plugins_dir.lower() + "/"
    return sorted(f for f in files if f.startswith(prefix) and f.endswith(".dll") and "/" not in f[len(prefix):])
