"""Running Windows programs through Proton with umu-launcher.

umu-launcher runs Proton inside Steam's runtime container without needing
Steam to launch the game. It is configured through environment variables:

``WINEPREFIX``  the prefix to use (created on first run)
``GAMEID``      umu database id, e.g. ``umu-489830``; the number becomes SteamAppId
``PROTONPATH``  a Proton build folder, ``GE-Proton`` (latest, auto-downloaded) or
                unset for UMU-Proton
``STORE``       storefront for protonfixes lookups (``steam``, ``gog``, ``egs``, ...)
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

WINDOWS_EXTS = (".exe", ".bat", ".msi", ".lnk")


class ProtonError(Exception):
    pass


def find_umu_run(configured: str = "") -> str | None:
    if configured:
        path = Path(configured).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    found = shutil.which("umu-run")
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "umu-run",
        Path("/usr/local/bin/umu-run"),
        Path("/usr/bin/umu-run"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def wine_root(prefix: Path) -> Path:
    """The folder that contains ``drive_c``.

    umu makes ``<prefix>/pfx`` a link back to the prefix itself, while Steam's
    compatdata folders keep the real prefix in ``pfx``. Both resolve here.
    """
    pfx = prefix / "pfx"
    if (pfx / "drive_c").is_dir():
        return pfx
    return prefix


def user_dir(prefix: Path) -> Path:
    return wine_root(prefix) / "drive_c" / "users" / "steamuser"


def appdata_local(prefix: Path) -> Path:
    return user_dir(prefix) / "AppData" / "Local"


def my_games(prefix: Path) -> Path:
    return user_dir(prefix) / "Documents" / "My Games"


def prefix_initialized(prefix: Path) -> bool:
    return (wine_root(prefix) / "system.reg").is_file()


# Files Proton copies from Steam's legacycompat folder into every prefix it sets up
# for a Steam game, so the game's Steam DRM and steam_api can reach the running
# client. umu clears STEAM_COMPAT_CLIENT_INSTALL_PATH, so Proton skips this step
# in prefixes Steam never touched, and such games quit right after starting.
STEAM_CLIENT_FILES = (
    ("steamclient.dll", "steamclient.dll"),
    ("steamclient64.dll", "steamclient64.dll"),
    ("GameOverlayRenderer64.dll", "GameOverlayRenderer64.dll"),
    ("SteamService.exe", "steam.exe"),
    ("Steam.dll", "Steam.dll"),
)


def steam_client_dir(prefix: Path) -> Path:
    return wine_root(prefix) / "drive_c" / "Program Files (x86)" / "Steam"


def install_steam_client_files(prefix: Path, steam_root: Path) -> list[str]:
    """Copy Steam's client files into the prefix as Proton does; return the names copied."""
    src_dir = steam_root / "legacycompat"
    dst_dir = steam_client_dir(prefix)
    copied: list[str] = []
    for src_name, dst_name in STEAM_CLIENT_FILES:
        src = src_dir / src_name
        if not src.is_file():
            continue
        dst = dst_dir / dst_name
        try:
            if dst.is_file() and dst.stat().st_size == src.stat().st_size \
                    and dst.stat().st_mtime >= src.stat().st_mtime:
                continue
        except OSError:
            pass
        dst_dir.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink():
            dst.unlink()
        shutil.copy2(src, dst)
        copied.append(dst_name)
    return copied


# Wine marks its own PE files in the DOS header area; Microsoft's DLLs have no such text.
WINE_DLL_MARKERS = (b"Wine builtin DLL", b"Wine placeholder DLL")


def dll_origin(path: Path) -> str:
    """"missing", "wine" (Wine's reimplementation) or "native" (a real Windows DLL)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(0x200)
    except OSError:
        return "missing"
    return "wine" if any(m in head for m in WINE_DLL_MARKERS) else "native"


def dll_override(prefix: Path, dll: str, env: dict[str, str] | None = None) -> str | None:
    """The load order Wine uses for ``dll``: from WINEDLLOVERRIDES, else the prefix registry."""
    name = dll.lower().removesuffix(".dll")
    for part in (env or {}).get("WINEDLLOVERRIDES", "").split(";"):
        dlls, _, mode = part.partition("=")
        if name in [d.strip().lower().removesuffix(".dll") for d in dlls.split(",")]:
            return mode.strip() or None
    try:
        text = (wine_root(prefix) / "user.reg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    section = False
    for line in text.splitlines():
        if line.startswith("["):
            section = line.lower().startswith("[software\\\\wine\\\\dlloverrides]")
            continue
        if section:
            key, _, value = line.partition("=")
            if key.strip().strip('"').lstrip("*").lower() == name:
                return value.strip().strip('"')
    return None


def native_dll_status(prefix: Path, dll: str, env: dict[str, str] | None = None) -> str:
    """"ok" when Wine will load a real Windows ``dll``; otherwise why not:
    "missing" (not installed), "wine" (only Wine's version), "no_override" (installed but not preferred)."""
    origin = dll_origin(wine_root(prefix) / "drive_c" / "windows" / "system32" / dll)
    if origin != "native":
        return origin
    mode = dll_override(prefix, dll, env)
    return "ok" if mode and mode.lower().startswith("n") else "no_override"


@dataclass
class LaunchSpec:
    argv: list[str]
    env: dict[str, str]
    cwd: str

    def describe(self) -> str:
        keys = ("WINEPREFIX", "GAMEID", "PROTONPATH", "STORE")
        env = " ".join(f"{k}={shlex.quote(self.env[k])}" for k in keys if self.env.get(k))
        return f"{env} {shlex.join(self.argv)}".strip()


APPIMAGE_VARS = ("APPDIR", "APPIMAGE", "ARGV0", "OWD")


def host_env(env) -> dict[str, str]:
    """Environment for child processes, without anything pointing into our AppImage.

    umu-run is itself a Python program, so variables leaking from a bundled
    runtime (paths into the AppImage mount) must not reach it or the game.
    """
    env = dict(env)
    appdir = env.get("APPDIR")
    if not appdir:
        return env
    for key in APPIMAGE_VARS:
        env.pop(key, None)
    for key, value in list(env.items()):
        if appdir not in value:
            continue
        kept = [part for part in value.split(":") if appdir not in part]
        if kept and ":" in value:
            env[key] = ":".join(kept)
        else:
            del env[key]
    return env


def needs_proton(path: str) -> bool:
    return path.lower().endswith(WINDOWS_EXTS)


def build_launch(
    proton_cfg: dict,
    program: str,
    args: str = "",
    cwd: str = "",
    use_proton: bool = True,
    extra_env: dict[str, str] | None = None,
    base_env: dict[str, str] | None = None,
) -> LaunchSpec:
    """Build the command line and environment for running ``program``."""
    env = host_env(os.environ if base_env is None else base_env)
    env.update({k: str(v) for k, v in (proton_cfg.get("env") or {}).items()})
    if extra_env:
        env.update(extra_env)
    arg_list = shlex.split(args) if args else []
    workdir = cwd or str(Path(program).parent)

    if not (use_proton and needs_proton(program)):
        return LaunchSpec([program, *arg_list], env, workdir)

    umu = find_umu_run(proton_cfg.get("umu_run", ""))
    if umu is None:
        raise ProtonError(
            "umu-run was not found. Install umu-launcher (packaged as 'umu-launcher' on most "
            "distributions) or set its path in Settings > Proton."
        )
    prefix = proton_cfg.get("prefix")
    if not prefix:
        raise ProtonError("No Wine prefix configured (Settings > Proton).")
    Path(prefix).expanduser().mkdir(parents=True, exist_ok=True)
    env["WINEPREFIX"] = str(Path(prefix).expanduser())
    env["GAMEID"] = proton_cfg.get("game_id") or "umu-default"
    proton_path = proton_cfg.get("proton_path") or ""
    if proton_path:
        env["PROTONPATH"] = proton_path
    else:
        env.pop("PROTONPATH", None)
    if proton_cfg.get("store"):
        env["STORE"] = proton_cfg["store"]
    return LaunchSpec([umu, program, *arg_list], env, workdir)


def build_tool(proton_cfg: dict, tool: str, args: list[str] | None = None) -> LaunchSpec:
    """Run a Wine/umu helper such as ``winecfg`` or ``winetricks`` inside the prefix."""
    spec = build_launch(proton_cfg, "placeholder.exe")
    spec.argv = [spec.argv[0], tool, *(args or [])]
    spec.cwd = str(Path.home())
    return spec


def resolve_build(proton_cfg: dict, search_dirs: list[Path] | None = None) -> Path | None:
    """The Proton build umu will run: the configured folder, or the newest installed
    GE-Proton / UMU-Proton when ``PROTONPATH`` is "GE-Proton" or unset."""
    configured = (proton_cfg.get("proton_path") or "").strip()
    if configured and configured != "GE-Proton":
        path = Path(configured).expanduser()
        return path if (path / "proton").is_file() else None
    stem = "GE-Proton" if configured else "UMU-Proton"
    if search_dirs is None:
        from modmanager.core import steam
        search_dirs = [r / "compatibilitytools.d" for r in steam.steam_roots()]
        xdg = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        search_dirs.append(xdg / "umu" / "compatibilitytools")
    found = [
        d for base in search_dirs if base.is_dir()
        for d in base.iterdir() if d.name.startswith(stem) and (d / "proton").is_file()
    ]

    def version_key(path: Path) -> list[int]:
        return [int(n) for n in re.findall(r"\d+", path.name)]

    return max(found, key=version_key) if found else None


_FFMPEG_LIB = re.compile(rb"lib(?:av[a-z]+|swresample|swscale|postproc)\.so\.\d+")


def missing_wma_libraries(build: Path) -> list[str]:
    """FFmpeg libraries the build's GStreamer libav plugin needs but the build lacks.

    Wine decodes xWMA (Bethesda games' music and voice files) with that plugin.
    Valve's Proton and UMU-Proton ship the plugin without FFmpeg, relying on audio
    Steam converts in advance, which a launch outside Steam never gets.
    """
    lib_dir = build / "files" / "lib" / "x86_64-linux-gnu"
    plugin = lib_dir / "gstreamer-1.0" / "libgstlibav.so"
    try:
        data = plugin.read_bytes()
    except OSError:
        return []  # No such plugin: the build decodes audio some other way.
    needed = sorted({m.decode() for m in _FFMPEG_LIB.findall(data)})
    return [name for name in needed if not (lib_dir / name).exists()]

