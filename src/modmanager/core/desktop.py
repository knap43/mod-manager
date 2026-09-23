"""Registering as the handler for nxm:// links (Nexus "Mod Manager Download" buttons)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import modmanager

NXM_DESKTOP = "modmanager-nxm.desktop"
NXM_MIME = "x-scheme-handler/nxm"


def applications_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "applications"


def launch_command() -> list[str]:
    """How to start this copy of the program: AppImage, installed script, or source tree."""
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return [appimage]
    script = shutil.which("modmanager")
    package_dir = Path(modmanager.__file__).resolve().parent
    if script and any(part in str(package_dir) for part in ("site-packages", "dist-packages")):
        return [script]
    # Running from a source checkout.
    return ["env", f"PYTHONPATH={package_dir.parent}", sys.executable, "-m", "modmanager"]


def _quote(arg: str) -> str:
    if not any(c in arg for c in ' \t"\'\\$`'):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'


def desktop_entry(command: list[str]) -> str:
    exec_line = " ".join(_quote(a) for a in command) + " %u"  # %u may expand to nothing
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Mod Manager (Nexus links)\n"
        "Comment=Download mods from Nexus Mods\n"
        f"Exec={exec_line}\n"
        "Icon=modmanager\n"
        "NoDisplay=true\n"
        "Terminal=false\n"
        f"MimeType={NXM_MIME};\n"
    )


def register_nxm_handler() -> Path:
    folder = applications_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / NXM_DESKTOP
    path.write_text(desktop_entry(launch_command()))
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(folder)], capture_output=True)
    if shutil.which("xdg-mime"):
        proc = subprocess.run(["xdg-mime", "default", NXM_DESKTOP, NXM_MIME], capture_output=True, text=True)
        if proc.returncode != 0:
            raise OSError(proc.stderr.strip() or "xdg-mime failed")
    else:
        raise OSError("xdg-mime not found (install xdg-utils)")
    return path


def nxm_handler() -> str | None:
    if not shutil.which("xdg-mime"):
        return None
    proc = subprocess.run(["xdg-mime", "query", "default", NXM_MIME], capture_output=True, text=True)
    return proc.stdout.strip() or None
