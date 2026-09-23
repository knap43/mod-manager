"""Deploy mods into the game's Data folder and remove them again.

Mod Organizer 2 relies on a Windows-only virtual file system. Here mods are
materialised in the real Data folder instead, by hard link where possible
(instant, no extra disk space, invisible to Wine), falling back to symbolic
links or copies. Every file placed is recorded in a manifest so that deployment
is incremental and a purge restores the folder exactly:

* game files that a mod replaces are moved to ``deploy/backup`` and put back on purge;
* folders created for mods are removed again once empty;
* files the game or a tool creates while running can be moved to Overwrite.

Windows games expect a case-insensitive file system, so ``Textures/a.dds`` from
one mod and ``textures/b.dds`` from another must end up in the same folder.
Destination paths are therefore resolved case-insensitively against what is
already on disk.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from modmanager.core import paths

log = logging.getLogger(__name__)

MANIFEST_VERSION = 1
ProgressFn = Callable[[int, int], None]


@dataclass
class DeployResult:
    added: int = 0
    removed: int = 0
    unchanged: int = 0
    backed_up: int = 0
    fallbacks: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.added} linked", f"{self.removed} removed", f"{self.unchanged} unchanged"]
        if self.backed_up:
            parts.append(f"{self.backed_up} game files backed up")
        if self.fallbacks:
            parts.append(f"{self.fallbacks} symlinked (hard links impossible)")
        if self.errors:
            parts.append(f"{len(self.errors)} errors")
        return ", ".join(parts)


class CaseResolver:
    """Resolve relative paths inside ``root`` case-insensitively, caching directory listings."""

    def __init__(self, root: Path):
        self.root = root
        self._dirs: dict[str, dict[str, str] | None] = {}

    def _listing(self, real_dir: str) -> dict[str, str] | None:
        if real_dir not in self._dirs:
            full = self.root / real_dir if real_dir else self.root
            try:
                self._dirs[real_dir] = {e.name.lower(): e.name for e in os.scandir(full)}
            except (FileNotFoundError, NotADirectoryError):
                self._dirs[real_dir] = None
        return self._dirs[real_dir]

    def resolve(self, rel: str) -> str:
        """Return the on-disk spelling of ``rel``; missing components keep their spelling."""
        parts = rel.split("/")
        real: list[str] = []
        exists = True
        for part in parts:
            if exists:
                listing = self._listing("/".join(real))
                match = listing.get(part.lower()) if listing is not None else None
                if match is None:
                    exists = False
                    real.append(part)
                else:
                    real.append(match)
            else:
                real.append(part)
        return "/".join(real)

    def added(self, real_rel: str) -> None:
        parent, _, name = real_rel.rpartition("/")
        listing = self._dirs.get(parent)
        if listing is not None:
            listing[name.lower()] = name
        self._dirs.setdefault(real_rel, None)
        if self._dirs[real_rel] is None and (self.root / real_rel).is_dir():
            self._dirs[real_rel] = {}

    def removed(self, real_rel: str) -> None:
        parent, _, name = real_rel.rpartition("/")
        listing = self._dirs.get(parent)
        if listing is not None:
            listing.pop(name.lower(), None)
        self._dirs.pop(real_rel, None)


class Deployer:
    def __init__(self, data_dir: Path, state_dir: Path, method: str = "hardlink"):
        self.data_dir = data_dir
        self.state_dir = state_dir
        self.method = method
        self.manifest_file = state_dir / "manifest.json"
        self.backup_dir = state_dir / "backup"
        self.state = self._load()

    # --------------------------------------------------------------- manifest

    def _load(self) -> dict:
        state = paths.read_json(self.manifest_file) or {}
        if state.get("version") != MANIFEST_VERSION:
            state = {"version": MANIFEST_VERSION, "data_dir": str(self.data_dir), "files": {}, "dirs": []}
        return state

    def _save(self) -> None:
        self.state["data_dir"] = str(self.data_dir)
        paths.write_json(self.manifest_file, self.state)

    @property
    def files(self) -> dict[str, dict]:
        return self.state["files"]

    @property
    def is_deployed(self) -> bool:
        return bool(self.files) or bool(self.state.get("dirs"))

    def tracked_names(self) -> set[str]:
        """Real relative paths of every deployed file."""
        return {entry["path"] for entry in self.files.values()}

    def untracked_root_files(self) -> list[str]:
        """File names in the Data folder root that were not placed by the manager."""
        tracked = {e["path"].lower() for e in self.files.values() if "/" not in e["path"]}
        try:
            return [
                e.name for e in os.scandir(self.data_dir)
                if e.is_file(follow_symlinks=True) and e.name.lower() not in tracked
            ]
        except FileNotFoundError:
            return []

    # ------------------------------------------------------------- deployment

    def deploy(self, plan: dict[str, tuple[str, Path]], progress: ProgressFn | None = None) -> DeployResult:
        """Make the Data folder match ``plan`` (lower-cased key -> (relative path, source))."""
        if not self.data_dir.is_dir():
            raise FileNotFoundError(f"Data folder not found: {self.data_dir}")
        result = DeployResult()
        resolver = CaseResolver(self.data_dir)
        files = self.files
        total = len(files) + len(plan)
        done = 0

        # 1. Remove files that are no longer wanted, or whose source or copy changed.
        for key in list(files):
            entry = files[key]
            want = plan.get(key)
            if want is not None and entry["source"] == str(want[1]) and self._check(entry) == "ok":
                result.unchanged += 1
            else:
                self._undeploy_file(key, entry, resolver, result)
                result.removed += 1
            done += 1
            if progress and done % 500 == 0:
                progress(done, total)

        # 2. Place new files.
        created_dirs: set[str] = set(self.state.get("dirs", []))
        for key, (rel, source) in plan.items():
            done += 1
            if progress and done % 500 == 0:
                progress(done, total)
            if key in files:
                continue
            real = resolver.resolve(rel)
            dest = self.data_dir / real
            try:
                self._ensure_parents(real, resolver, created_dirs)
                backup = None
                if dest.exists() or dest.is_symlink():
                    if dest.is_dir() and not dest.is_symlink():
                        raise IsADirectoryError(f"{real} is a folder in the game directory")
                    backup = self._backup(real)
                    result.backed_up += 1
                used = self._place(source, dest)
                if used != self.method:
                    result.fallbacks += 1
                resolver.added(real)
                files[key] = {
                    "path": real, "source": str(source), "method": used, "backup": backup,
                    "src": _sig(source, follow=True), "dst": _sig(dest),
                }
                result.added += 1
            except OSError as exc:
                result.errors.append(f"{rel}: {exc}")
                log.error("Could not deploy %s: %s", rel, exc)

        self.state["dirs"] = sorted(created_dirs)
        self._prune_dirs(resolver)
        self._save()
        if progress:
            progress(total, total)
        return result

    def purge(self, progress: ProgressFn | None = None) -> DeployResult:
        """Remove every deployed file and restore backed-up game files."""
        result = DeployResult()
        if not self.data_dir.is_dir():
            # Nothing to clean up in a Data folder that no longer exists.
            self.state = {"version": MANIFEST_VERSION, "data_dir": str(self.data_dir), "files": {}, "dirs": []}
            self._save()
            return result
        resolver = CaseResolver(self.data_dir)
        total = len(self.files)
        for i, key in enumerate(list(self.files)):
            self._undeploy_file(key, self.files[key], resolver, result)
            result.removed += 1
            if progress and i % 500 == 0:
                progress(i, total)
        self._prune_dirs(resolver, remove_all=True)
        self._save()
        if progress:
            progress(total, total)
        return result

    # ---------------------------------------------------------------- capture

    def snapshot(self) -> set[str]:
        """Lower-cased relative paths of every file currently in the Data folder."""
        root = str(self.data_dir)
        cut = len(root) + 1
        found: set[str] = set()
        for dirpath, _dirs, filenames in os.walk(root):
            rel_dir = dirpath[cut:].replace(os.sep, "/")
            for fn in filenames:
                found.add((f"{rel_dir}/{fn}" if rel_dir else fn).lower())
        return found

    def capture_new_files(self, before: set[str], overwrite_dir: Path) -> list[str]:
        """Move files that appeared in Data since ``before`` into the Overwrite folder."""
        tracked = set(self.files)
        moved: list[str] = []
        root = str(self.data_dir)
        cut = len(root) + 1
        for dirpath, _dirs, filenames in os.walk(root):
            rel_dir = dirpath[cut:].replace(os.sep, "/")
            for fn in filenames:
                rel = f"{rel_dir}/{fn}" if rel_dir else fn
                key = rel.lower()
                if key in before or key in tracked:
                    continue
                dest = overwrite_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(os.path.join(dirpath, fn), dest)
                    moved.append(rel)
                except OSError as exc:
                    log.error("Could not move %s to Overwrite: %s", rel, exc)
        if moved:
            self._prune_dirs(CaseResolver(self.data_dir))
            self._save()
        return moved

    # ---------------------------------------------------------------- helpers

    def _check(self, entry: dict) -> str:
        """Compare a deployed file with the manifest.

        Returns ``ok``, ``missing``, ``data_modified`` (the file in Data was replaced
        or edited by the game or a tool), ``source_changed`` (the mod was updated)
        or ``both``.
        """
        dest = self.data_dir / entry["path"]
        try:
            d = _sig(dest)
        except OSError:
            return "missing"
        try:
            s = _sig(Path(entry["source"]), follow=True)
        except OSError:
            s = None
        method = entry["method"]
        if method == "hardlink":
            if s is not None and d[:2] == s[:2]:
                return "ok"  # Still one inode: in-place edits already live in the mod.
            dst_changed = d[:2] != entry["dst"][:2]
            src_changed = s is None or s[:2] != entry["src"][:2]
        elif method == "symlink":
            ours = dest.is_symlink() and os.readlink(dest) == entry["source"]
            if ours and s is not None:
                return "ok"
            dst_changed = not ours
            src_changed = s is None or s != entry["src"]
        else:
            dst_changed = d[2:] != entry["dst"][2:]
            src_changed = s is None or s[2:] != entry["src"][2:]
        if not dst_changed and not src_changed:
            return "ok"
        if dst_changed and not src_changed:
            return "data_modified"
        return "source_changed" if not dst_changed else "both"

    def _undeploy_file(self, key: str, entry: dict, resolver: CaseResolver, result: DeployResult) -> None:
        real = entry["path"]
        dest = self.data_dir / real
        try:
            status = self._check(entry)
            if status == "data_modified":
                # Keep edits made in the game folder by writing them back into the mod.
                source = Path(entry["source"])
                source.parent.mkdir(parents=True, exist_ok=True)
                if source.exists() or source.is_symlink():
                    source.unlink()
                shutil.move(dest, source)
                log.info("Saved changes to %s back into %s", real, source)
                resolver.removed(real)
            elif status == "both":
                keep = self.state_dir / "modified" / real
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(dest, keep)
                log.warning("%s changed both in the game folder and in its mod; game copy saved to %s", real, keep)
                resolver.removed(real)
            elif status != "missing":
                dest.unlink()
                resolver.removed(real)
            if entry.get("backup"):
                backup = self.backup_dir / entry["backup"]
                if backup.exists() or backup.is_symlink():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, dest)
                    resolver.added(real)
        except OSError as exc:
            result.errors.append(f"{real}: {exc}")
            log.error("Could not remove %s: %s", real, exc)
            return
        del self.files[key]

    def _backup(self, real: str) -> str:
        target = self.backup_dir / real
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            target.unlink()
        os.replace(self.data_dir / real, target)
        return real

    def _ensure_parents(self, real: str, resolver: CaseResolver, created: set[str]) -> None:
        parts = real.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            sub = "/".join(parts[:i])
            full = self.data_dir / sub
            if not full.exists():
                full.mkdir()
                created.add(sub)
                resolver.added(sub)

    def _place(self, source: Path, dest: Path) -> str:
        if self.method == "hardlink":
            try:
                os.link(source, dest)
                return "hardlink"
            except OSError as exc:
                if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOTSUP):
                    raise
            os.symlink(source, dest)
            return "symlink"
        if self.method == "symlink":
            os.symlink(source, dest)
            return "symlink"
        shutil.copy2(source, dest)
        return "copy"

    def _prune_dirs(self, resolver: CaseResolver, remove_all: bool = False) -> None:
        """Remove folders the manager created that are now empty (deepest first)."""
        remaining: list[str] = []
        for sub in sorted(self.state.get("dirs", []), key=lambda s: s.count("/"), reverse=True):
            full = self.data_dir / sub
            try:
                if full.is_dir() and not any(os.scandir(full)):
                    full.rmdir()
                    resolver.removed(sub)
                    continue
            except OSError:
                pass
            if full.is_dir():
                remaining.append(sub)
        self.state["dirs"] = sorted(remaining)


def _sig(path: Path, follow: bool = False) -> list[int]:
    st = os.stat(path) if follow else os.lstat(path)
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]


def is_writable_dir(path: Path) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and os.access(path, os.W_OK | os.X_OK)
