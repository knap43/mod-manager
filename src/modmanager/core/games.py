"""Game definitions.

A :class:`GameDef` describes everything the manager needs to know about a game:
where its data folder lives, how its plugin load order is stored, and how to
launch it through Proton. The ``generic`` definition works for any game whose
mods are files dropped into a folder.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

# Top-level folders that identify the root of a Bethesda "Data" tree inside an archive.
BETHESDA_DATA_DIRS = frozenset(
    {
        "meshes", "textures", "scripts", "interface", "sound", "music", "seq", "strings",
        "video", "materials", "shadersfx", "lodsettings", "grass", "source", "dialogueviews",
        "skse", "f4se", "sksevr", "calientetools", "dyndolod", "nemesis_engine", "platform",
        "netscriptframework", "tools", "lightplacer", "skypatcher", "distantdetail",
        "shaders", "vis", "misc", "programs", "terrain", "trees", "fonts", "facegen",
    }
)
BETHESDA_DATA_EXTS = frozenset({".esp", ".esm", ".esl", ".bsa", ".ba2"})

# Files in a game folder that reveal which store it came from (checked in this order).
STORE_MARKERS = (
    ("gog", ("galaxy64.dll", "galaxy.dll", "goggame-*.info", "gog.ico")),
    ("epic", ("eossdk-win64-shipping.dll",)),
    ("steam", ("steam_api64.dll", "steam_api.dll")),
)


def detect_store(game_dir: Path) -> str | None:
    """Return "gog", "epic" or "steam" for a game folder, or None if unknown."""
    try:
        names = [e.name.lower() for e in os.scandir(game_dir) if e.is_file()]
    except OSError:
        return None
    for store, patterns in STORE_MARKERS:
        if any(fnmatch.fnmatchcase(n, pat) for pat in patterns for n in names):
            return store
    return None


@dataclass(frozen=True)
class GameDef:
    id: str
    name: str
    steam_app_id: int | None = None
    steam_dir: str | None = None
    binary: str | None = None
    launcher: str | None = None
    data_subdir: str = "Data"
    # Folder names below the Wine user's AppData/Local and Documents/My Games.
    appdata_name: str | None = None
    my_games_name: str | None = None
    # Some store editions use different AppData/My Games folders, e.g.
    # "Skyrim Special Edition GOG". Maps a detected store to that folder name.
    store_folders: tuple[tuple[str, str], ...] = ()
    # "asterisk": plugins.txt order with '*' marking active plugins (SSE, FO4).
    # "timestamp": plugins.txt lists active plugins, file mtimes decide order (Skyrim LE).
    # None: the game has no plugin load order.
    plugin_format: str | None = None
    implicit_plugins: tuple[str, ...] = ()
    ccc_file: str | None = None
    supports_light_plugins: bool = False
    data_dirs: frozenset[str] = frozenset()
    data_exts: frozenset[str] = frozenset()
    umu_id_override: str | None = None
    extra_executables: tuple[tuple[str, str], ...] = field(default=())

    @property
    def umu_id(self) -> str:
        if self.umu_id_override:
            return self.umu_id_override
        return f"umu-{self.steam_app_id}" if self.steam_app_id else "umu-default"

    def local_folder(self, game_dir: Path) -> str | None:
        """AppData/Local and My Games folder name for the edition installed in ``game_dir``."""
        store = detect_store(game_dir) if self.store_folders else None
        return dict(self.store_folders).get(store or "", self.appdata_name)

    def my_games_folder(self, game_dir: Path) -> str | None:
        store = detect_store(game_dir) if self.store_folders else None
        return dict(self.store_folders).get(store or "", self.my_games_name)

    @property
    def has_plugins(self) -> bool:
        return self.plugin_format is not None


SKYRIM_SE_MASTERS = ("Skyrim.esm", "Update.esm", "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm")

GAMES: dict[str, GameDef] = {
    g.id: g
    for g in (
        GameDef(
            id="skyrimse",
            name="Skyrim Special Edition",
            steam_app_id=489830,
            steam_dir="Skyrim Special Edition",
            binary="SkyrimSE.exe",
            launcher="SkyrimSELauncher.exe",
            appdata_name="Skyrim Special Edition",
            my_games_name="Skyrim Special Edition",
            store_folders=(("gog", "Skyrim Special Edition GOG"), ("epic", "Skyrim Special Edition EPIC")),
            plugin_format="asterisk",
            implicit_plugins=SKYRIM_SE_MASTERS,
            ccc_file="Skyrim.ccc",
            supports_light_plugins=True,
            data_dirs=BETHESDA_DATA_DIRS,
            data_exts=BETHESDA_DATA_EXTS,
        ),
        GameDef(
            id="skyrimse_gog",
            name="Skyrim Special Edition (GOG)",
            binary="SkyrimSE.exe",
            launcher="SkyrimSELauncher.exe",
            appdata_name="Skyrim Special Edition GOG",
            my_games_name="Skyrim Special Edition GOG",
            plugin_format="asterisk",
            implicit_plugins=SKYRIM_SE_MASTERS,
            ccc_file="Skyrim.ccc",
            supports_light_plugins=True,
            data_dirs=BETHESDA_DATA_DIRS,
            data_exts=BETHESDA_DATA_EXTS,
            umu_id_override="umu-489830",
        ),
        GameDef(
            id="skyrim",
            name="Skyrim (Legendary Edition)",
            steam_app_id=72850,
            steam_dir="Skyrim",
            binary="TESV.exe",
            launcher="SkyrimLauncher.exe",
            appdata_name="Skyrim",
            my_games_name="Skyrim",
            plugin_format="timestamp",
            implicit_plugins=("Skyrim.esm", "Update.esm"),
            data_dirs=BETHESDA_DATA_DIRS,
            data_exts=BETHESDA_DATA_EXTS,
        ),
        GameDef(
            id="fallout4",
            name="Fallout 4",
            steam_app_id=377160,
            steam_dir="Fallout 4",
            binary="Fallout4.exe",
            launcher="Fallout4Launcher.exe",
            appdata_name="Fallout4",
            my_games_name="Fallout4",
            plugin_format="asterisk",
            implicit_plugins=(
                "Fallout4.esm", "DLCRobot.esm", "DLCworkshop01.esm", "DLCCoast.esm",
                "DLCworkshop02.esm", "DLCworkshop03.esm", "DLCNukaWorld.esm",
                "DLCUltraHighResolution.esm",
            ),
            ccc_file="Fallout4.ccc",
            supports_light_plugins=True,
            data_dirs=BETHESDA_DATA_DIRS,
            data_exts=BETHESDA_DATA_EXTS,
        ),
        GameDef(
            id="generic",
            name="Other game (generic)",
            data_subdir="",
        ),
    )
}


def get_game(game_id: str) -> GameDef:
    try:
        return GAMES[game_id]
    except KeyError:
        raise ValueError(f"Unknown game: {game_id}") from None
