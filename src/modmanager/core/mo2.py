"""Import a Mod Organizer 2 instance.

An MO2 instance already uses this manager's layout (``mods/``, ``overwrite/``,
``downloads/`` and ``profiles/<P>/modlist.txt`` + ``plugins.txt``), so importing
it only writes ``instance.json`` next to ``ModOrganizer.ini``: the mods are used
where they are, and MO2 can still open the folder afterwards.

``ModOrganizer.ini`` stores paths as Wine sees them (``Z:\\home\\...`` or a
``C:/...`` path inside the prefix), and the folders may have moved since, so
paths are resolved by looking for them relative to the game and MO2 folders.
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from modmanager.core import steam
from modmanager.core.games import GAMES, GameDef
from modmanager.core.instance import Executable, Instance

INI = "ModOrganizer.ini"

# MO2's gameName for the games this manager knows.
MO2_GAME_NAMES = {
    "skyrim special edition": "skyrimse",
    "skyrim": "skyrim",
    "fallout 4": "fallout4",
}

# MO2 executables that make no sense without MO2 itself.
SKIPPED_BINARIES = {"explorer++.exe", "modorganizer.exe"}


def is_mo2_instance(folder: Path) -> bool:
    return (folder / INI).is_file() and (folder / "mods").is_dir()


def _ini_value(value: str) -> str:
    """Undo QSettings quoting: ``@ByteArray(...)``, surrounding quotes and escapes."""
    value = value.strip()
    m = re.fullmatch(r"@ByteArray\((.*)\)", value, re.S)
    if m:
        value = m.group(1)
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value.replace("\\\\", "\\").replace('\\"', '"')


def read_ini(folder: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str  # keep "1\binary" as written
    cp.read(folder / INI, encoding="utf-8")
    return cp


def _windows_parts(path: str) -> tuple[str, list[str]]:
    """Split a Wine path into its drive letter (lower case, may be "") and components."""
    p = PureWindowsPath(path.replace("/", "\\"))
    drive = p.drive.rstrip(":").lower()
    parts = [x for x in p.parts if x not in (p.drive, p.anchor, "\\")]
    return drive, parts


def resolve_path(path: str, bases: list[Path]) -> Path | None:
    """Find the Linux file a Wine path from ``ModOrganizer.ini`` refers to.

    ``Z:`` maps onto ``/``; failing that (or for other drives) the longest trailing
    part of the path that exists below one of ``bases`` wins, which also finds
    folders that were moved since MO2 last saw them.
    """
    if not path:
        return None
    drive, parts = _windows_parts(path)
    if not parts:
        return None
    if drive == "z":
        direct = Path("/", *parts)
        if direct.exists():
            return direct
    for i in range(len(parts)):
        for base in bases:
            candidate = base.joinpath(*parts[i:])
            if candidate.exists():
                return candidate
    return None


@dataclass
class Mo2Import:
    """What ``ModOrganizer.ini`` says, resolved to Linux paths."""

    folder: Path
    game: GameDef
    game_dir: Path | None
    profile: str | None
    executables: list[Executable] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)  # executables whose file is gone


def inspect(folder: Path, game_dir: Path | None = None) -> Mo2Import:
    """Read an MO2 instance without changing anything."""
    folder = folder.resolve()
    if not is_mo2_instance(folder):
        raise FileNotFoundError(f"No Mod Organizer 2 instance (ModOrganizer.ini and mods/) in {folder}")
    cp = read_ini(folder)
    general = cp["General"] if cp.has_section("General") else {}

    game_name = _ini_value(general.get("gameName", ""))
    game_id = MO2_GAME_NAMES.get(game_name.lower())
    if game_id is None:
        raise ValueError(f"Mod Organizer 2 instance is for an unsupported game: {game_name or 'unknown'}")
    game = GAMES[game_id]
    if game_id == "skyrimse" and _ini_value(general.get("game_edition", "")).lower() == "gog":
        game = GAMES["skyrimse_gog"]

    if game_dir is None:
        game_dir = resolve_path(_ini_value(general.get("gamePath", "")), [])
        if game_dir is not None and game.binary and not (game_dir / game.binary).exists():
            game_dir = None
        if game_dir is None and game.steam_app_id:
            game_dir = steam.find_game(game.steam_app_id, game.steam_dir)

    profile = _ini_value(general.get("selected_profile", "")) or None

    bases = [b for b in (game_dir, folder, folder.parent, folder.parent.parent) if b is not None]
    result = Mo2Import(folder, game, game_dir, profile)
    if cp.has_section("customExecutables"):
        sec = cp["customExecutables"]
        try:
            count = int(sec.get("size", "0"))
        except ValueError:
            count = 0
        for i in range(1, count + 1):
            binary = _ini_value(sec.get(f"{i}\\binary", ""))
            title = _ini_value(sec.get(f"{i}\\title", "")) or PureWindowsPath(binary).stem
            if not binary or PureWindowsPath(binary).name.lower() in SKIPPED_BINARIES:
                continue
            path = resolve_path(binary, bases)
            if path is None or not path.is_file():
                result.unresolved.append(f"{title} ({binary})")
                continue
            cwd = resolve_path(_ini_value(sec.get(f"{i}\\workingDirectory", "")), bases)
            result.executables.append(Executable(
                name=title,
                path=str(path),
                args=_ini_value(sec.get(f"{i}\\arguments", "")),
                cwd=str(cwd) if cwd and cwd.is_dir() else "",
            ))
    return result


def import_instance(folder: Path, game_dir: Path | None = None, name: str | None = None,
                    deploy_method: str = "hardlink") -> Instance:
    """Turn an MO2 instance into an instance of this manager, in place."""
    info = inspect(folder, game_dir)
    if info.game_dir is None or not info.game_dir.is_dir():
        raise FileNotFoundError(f"Could not find the {info.game.name} folder; choose it manually")
    folder = info.folder
    if (folder / Instance.CONFIG).exists():
        return Instance.load(folder)

    # Instance.create would add a "Default" profile, which MO2 instances usually have already.
    config = {
        "name": name or f"{info.game.name} (MO2)",
        "game": info.game.id,
        "game_dir": str(info.game_dir),
        "data_subdir": info.game.data_subdir,
        "deploy_method": deploy_method,
        "active_profile": info.profile or "Default",
        "proton": {
            "umu_run": "",
            "prefix": str(folder / "prefix"),
            "proton_path": "",
            "game_id": info.game.umu_id,
            "store": "",
            "env": {},
        },
        "executables": [],
        "selected_executable": 0,
    }
    inst = Instance(folder, config)
    defaults = inst.default_executables()
    known = {Path(e.path).resolve() for e in defaults}
    extra = []
    for exe in info.executables:
        if Path(exe.path).resolve() not in known:
            known.add(Path(exe.path).resolve())
            extra.append(exe)
    inst.executables = defaults + extra
    inst.save()
    return Instance.load(folder)
