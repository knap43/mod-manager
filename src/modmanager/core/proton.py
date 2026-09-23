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
