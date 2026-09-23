"""Game definitions.

A :class:`GameDef` describes everything the manager needs to know about a game:
where its data folder lives, how its plugin load order is stored, and how to
launch it through Proton. The ``generic`` definition works for any game whose
mods are files dropped into a folder.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
