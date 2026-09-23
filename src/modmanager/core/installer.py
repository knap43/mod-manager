"""Install mods from archives or folders into an instance's ``mods`` directory."""

from __future__ import annotations

import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from modmanager.core import archives, paths
from modmanager.core.games import GameDef
from modmanager.core.instance import sanitize_name
from modmanager.core.modlist import META_FILE

# Nexus download names look like "Mod Name-12604-2-1-1-1683000000".
NEXUS_NAME = re.compile(r"^(?P<name>.+?)-(?P<id>\d+)-(?P<version>[\w]+(?:-[\w]+)*?)-(?P<stamp>\d{9,11})$")
IGNORED_WRAPPER_FILES = re.compile(r"(readme|changelog|license|credits).*\.(txt|md|pdf|html?)$|\.(jpe?g|png|url)$", re.I)


@dataclass
class ArchiveInfo:
    name: str
    version: str = ""
    nexus_id: int | None = None
    file_id: int | None = None
    game: str | None = None


def guess_info(archive: Path) -> ArchiveInfo:
    """Name, version and Nexus IDs from the download's metadata, or its file name."""
    meta = paths.read_json(archive.with_name(archive.name + ".meta.json"), {}) or {}
    if meta.get("mod_id") and meta.get("state") != "downloading":
        return ArchiveInfo(
            name=str(meta.get("mod_name") or archives.archive_stem(archive)).strip(),
            version=str(meta.get("version") or ""),
            nexus_id=int(meta["mod_id"]),
            file_id=int(meta["file_id"]) if meta.get("file_id") else None,
            game=meta.get("game"),
        )
    stem = archives.archive_stem(archive) if archives.is_archive(archive) else archive.name
    m = NEXUS_NAME.match(stem)
    if m:
        return ArchiveInfo(m["name"].strip(), m["version"].replace("-", "."), int(m["id"]))
    return ArchiveInfo(stem.strip())


def looks_like_data(folder: Path, game: GameDef) -> bool:
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return False
    for e in entries:
        lower = e.name.lower()
        if e.is_dir() and lower in game.data_dirs:
            return True
        if e.is_file() and os.path.splitext(lower)[1] in game.data_exts:
            return True
    return False


def has_fomod(folder: Path) -> bool:
    fomod = paths.find_child_ci(folder, "fomod")
    return bool(fomod and paths.find_child_ci(fomod, "ModuleConfig.xml"))


def detect_data_root(extracted: Path, game: GameDef) -> tuple[Path, bool]:
    """Find the folder inside an extracted archive that maps onto the game's Data folder.

    Returns ``(folder, confident)``. When not confident the user should choose.
    """
    current = extracted
    for _ in range(8):
        if game.data_dirs and looks_like_data(current, game):
            return current, True
        entries = [e for e in os.scandir(current) if not e.name.startswith(".")]
        dirs = [e for e in entries if e.is_dir()]
        data = next((d for d in dirs if d.name.lower() == "data" and game.data_subdir.lower() == "data"), None)
        if data is not None:
            current = Path(data.path)
            continue
        files = [e for e in entries if e.is_file() and not IGNORED_WRAPPER_FILES.search(e.name)]
        if len(dirs) == 1 and not files and dirs[0].name.lower() != "fomod":
            current = Path(dirs[0].path)
            continue
        break
    if not game.data_dirs:
        return current, True
    return current, False


class Installer:
    def __init__(self, mods_dir: Path, tmp_dir: Path, game: GameDef):
        self.mods_dir = mods_dir
        self.tmp_dir = tmp_dir
        self.game = game

    def extract(self, source: Path) -> Path:
        """Extract an archive (or copy a folder) into a fresh staging folder."""
        staging = self.tmp_dir / f"install-{uuid.uuid4().hex[:8]}"
        if source.is_dir():
            shutil.copytree(source, staging, symlinks=False)
        else:
            archives.extract(source, staging)
        return staging

    def install(
        self,
        data_root: Path,
        name: str,
        mode: str = "replace",
        source: Path | None = None,
        info: ArchiveInfo | None = None,
        extra_meta: dict | None = None,
    ) -> str:
        """Move ``data_root``'s contents into ``mods/<name>``.

        ``mode`` controls what happens when the mod already exists: ``replace``
        deletes the old files, ``merge`` overlays the new ones on top.
        """
        name = sanitize_name(name)
        dest = self.mods_dir / name
        old_meta = paths.read_json(dest / META_FILE, {}) or {}
        if dest.exists() and mode == "replace":
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        for entry in os.scandir(data_root):
            target = dest / entry.name
            if target.is_dir() and entry.is_dir():
                _merge_tree(Path(entry.path), target)
            else:
                if target.is_dir():
                    shutil.rmtree(target)
                shutil.move(entry.path, target)
        meta = dict(old_meta)
        meta["installed"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if source is not None:
            meta["source"] = str(source)
        if info is not None:
            if info.version:
                meta["version"] = info.version
            if info.nexus_id:
                meta["nexus_id"] = info.nexus_id
                # A new download of the mod: forget the previous update check.
                for key in ("nexus_update", "nexus_latest_version", "nexus_checked"):
                    meta.pop(key, None)
            if info.file_id:
                meta["nexus_file_id"] = info.file_id
            if info.game:
                meta["nexus_game"] = info.game
        meta.update(extra_meta or {})
        paths.write_json(dest / META_FILE, meta)
        return name

    def cleanup(self, staging: Path) -> None:
        shutil.rmtree(staging, ignore_errors=True)


def _merge_tree(src: Path, dest: Path) -> None:
    for entry in os.scandir(src):
        target = dest / entry.name
        if entry.is_dir() and target.is_dir():
            _merge_tree(Path(entry.path), target)
        else:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            shutil.move(entry.path, target)
