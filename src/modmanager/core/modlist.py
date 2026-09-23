"""The active profile's mod list: priorities, enabled state, conflicts and plugins."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from modmanager.core import paths
from modmanager.core.instance import Instance, Profile, sanitize_name
from modmanager.core.plugins import (
    PluginEntry,
    assign_load_indices,
    format_order_file,
    is_plugin_name,
    merge_saved_order,
    partition_masters,
    read_header,
    read_order_file,
    update_saved_order,
)

META_FILE = "meta.json"
SEPARATOR_SUFFIX = "_separator"
OVERWRITE = "Overwrite"
# Files in a mod's root that belong to the manager and are never deployed.
MOD_METADATA = {META_FILE, "meta.ini"}


def scan_files(root: Path) -> dict[str, str]:
    """Map lower-cased relative paths to their real relative paths (POSIX separators)."""
    result: dict[str, str] = {}
    root_str = str(root)
    prefix_len = len(root_str) + 1
    for dirpath, dirnames, filenames in os.walk(root_str):
        rel_dir = dirpath[prefix_len:].replace(os.sep, "/")
        for fn in filenames:
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            if not rel_dir and fn.lower() in MOD_METADATA:
                continue
            result.setdefault(rel.lower(), rel)
    return result


@dataclass
class Mod:
    name: str               # Folder name; identifies the mod in modlist.txt.
    path: Path
    enabled: bool = False
    meta: dict = field(default_factory=dict)
    _files: dict[str, str] | None = field(default=None, repr=False)

    @property
    def is_separator(self) -> bool:
        return self.name.endswith(SEPARATOR_SUFFIX)

    @property
    def is_overwrite(self) -> bool:
        return self.name == OVERWRITE and self.path.name == "overwrite"

    @property
    def display_name(self) -> str:
        return self.name[: -len(SEPARATOR_SUFFIX)] if self.is_separator else self.name

    @property
    def version(self) -> str:
        return str(self.meta.get("version", ""))

    def files(self) -> dict[str, str]:
        if self._files is None:
            self._files = {} if self.is_separator else scan_files(self.path)
        return self._files

    def invalidate(self) -> None:
        self._files = None

    def save_meta(self) -> None:
        if self.path.is_dir():  # A background task may finish after the mod was removed.
            paths.write_json(self.path / META_FILE, self.meta)


@dataclass
class ConflictInfo:
    overwrites: set[str] = field(default_factory=set)      # Mods this one wins against.
    overwritten_by: set[str] = field(default_factory=set)  # Mods that win against this one.
    winning_files: int = 0
    losing_files: int = 0
    total_files: int = 0

    @property
    def redundant(self) -> bool:
        return self.total_files > 0 and self.losing_files == self.total_files


class ModList:
    """Mods of an instance in priority order (index 0 = lowest priority)."""

    def __init__(self, instance: Instance):
        self.instance = instance
        self.profile: Profile = instance.active_profile
        self.mods: list[Mod] = []
        self.overwrite = Mod(OVERWRITE, instance.overwrite_dir, enabled=True)
        self.conflicts: dict[str, ConflictInfo] = {}
        self.file_owners: dict[str, list[str]] = {}
        self.refresh()

    # ------------------------------------------------------------------ loading

    def refresh(self) -> None:
        """Rescan the mods folder and reapply the profile's saved order."""
        self.profile = self.instance.active_profile
        on_disk: dict[str, Path] = {}
        if self.instance.mods_dir.is_dir():
            for entry in os.scandir(self.instance.mods_dir):
                if entry.is_dir() and not entry.name.startswith("."):
                    on_disk[entry.name] = Path(entry.path)

        mods: list[Mod] = []
        seen: set[str] = set()
        for name, enabled in self.profile.read_modlist():
            if name in on_disk and name not in seen:
                seen.add(name)
                mods.append(self._make_mod(name, on_disk[name], enabled))
        # Mods that are new to this profile go on top, disabled, like in MO2.
        for name in sorted(on_disk, key=str.lower):
            if name not in seen:
                mods.append(self._make_mod(name, on_disk[name], False))
        self.mods = mods
        self.overwrite.invalidate()
        self.update_conflicts()

    def _make_mod(self, name: str, path: Path, enabled: bool) -> Mod:
        meta = paths.read_json(path / META_FILE, {}) or {}
        return Mod(name, path, enabled=enabled and not name.endswith(SEPARATOR_SUFFIX), meta=meta)

    def save(self) -> None:
        self.profile.write_modlist([(m.name, m.enabled) for m in self.mods])

    def get(self, name: str) -> Mod | None:
        return next((m for m in self.mods if m.name == name), None)

    def index_of(self, name: str) -> int:
        return next((i for i, m in enumerate(self.mods) if m.name == name), -1)

    # ---------------------------------------------------------------- editing

    def move(self, names: list[str], dest: int) -> None:
        """Move the named mods so that they start at ``dest`` (an index before the move)."""
        moving = [m for m in self.mods if m.name in set(names)]
        if not moving:
            return
        before = sum(1 for m in self.mods[:dest] if m.name in set(names))
        rest = [m for m in self.mods if m.name not in set(names)]
        dest = max(0, min(len(rest), dest - before))
        self.mods = rest[:dest] + moving + rest[dest:]
        self.save()
        self.update_conflicts()

    def set_enabled(self, names: list[str], enabled: bool) -> None:
        for m in self.mods:
            if m.name in names and not m.is_separator:
                m.enabled = enabled
        self.save()
        self.update_conflicts()

    def unique_name(self, name: str) -> str:
        name = sanitize_name(name)
        candidate, n = name, 2
        while (self.instance.mods_dir / candidate).exists() or candidate.lower() == OVERWRITE.lower():
            candidate = f"{name} ({n})"
            n += 1
        return candidate

    def create_separator(self, label: str, index: int) -> Mod:
        name = self.unique_name(sanitize_name(label) + SEPARATOR_SUFFIX)
        if not name.endswith(SEPARATOR_SUFFIX):
            name += SEPARATOR_SUFFIX
        path = self.instance.mods_dir / name
        path.mkdir(parents=True)
        mod = Mod(name, path)
        self.mods.insert(max(0, min(index, len(self.mods))), mod)
        self.save()
        return mod

    def rename(self, old: str, new: str) -> str:
        mod = self.get(old)
        if mod is None:
            raise KeyError(old)
        new = sanitize_name(new)
        if mod.is_separator and not new.endswith(SEPARATOR_SUFFIX):
            new += SEPARATOR_SUFFIX
        if new == old:
            return old
        if (self.instance.mods_dir / new).exists():
            raise FileExistsError(f"A mod named '{new}' already exists")
        new_path = self.instance.mods_dir / new
        mod.path.rename(new_path)
        mod.name, mod.path = new, new_path
        mod.invalidate()
        self._rename_in_other_profiles(old, new)
        self.save()
        self.update_conflicts()
        return new

    def _rename_in_other_profiles(self, old: str, new: str) -> None:
        for pname in self.instance.profile_names():
            if pname == self.profile.name:
                continue
            prof = Profile(self.instance.profiles_dir / pname)
            entries = prof.read_modlist()
            if any(n == old for n, _ in entries):
                prof.write_modlist([(new if n == old else n, on) for n, on in entries])

    def remove(self, names: list[str]) -> None:
        for name in names:
            mod = self.get(name)
            if mod is None:
                continue
            shutil.rmtree(mod.path, ignore_errors=True)
            self.mods.remove(mod)
        self.save()
        self.update_conflicts()

    def add_installed(self, name: str, enabled: bool = True, index: int | None = None) -> Mod:
        """Register a freshly installed mod folder (or refresh an existing entry)."""
        path = self.instance.mods_dir / name
        existing = self.get(name)
        if existing is not None:
            existing.meta = paths.read_json(path / META_FILE, {}) or {}
            existing.invalidate()
            mod = existing
        else:
            mod = self._make_mod(name, path, enabled)
            mod.enabled = enabled
            self.mods.insert(len(self.mods) if index is None else index, mod)
        self.save()
        self.update_conflicts()
        return mod

    # -------------------------------------------------------------- conflicts

    def active_sources(self) -> list[Mod]:
        """Enabled mods from lowest to highest priority, with Overwrite last."""
        return [m for m in self.mods if m.enabled and not m.is_separator] + [self.overwrite]

    def update_conflicts(self) -> None:
        owners: dict[str, list[str]] = {}
        for mod in self.active_sources():
            for key in mod.files():
                owners.setdefault(key, []).append(mod.name)
        conflicts: dict[str, ConflictInfo] = {m.name: ConflictInfo() for m in self.active_sources()}
        for info_name, info in conflicts.items():
            mod = self.overwrite if info_name == OVERWRITE else self.get(info_name)
            info.total_files = len(mod.files()) if mod else 0
        for providers in owners.values():
            if len(providers) < 2:
                continue
            winner = providers[-1]
            conflicts[winner].winning_files += 1
            for i, loser in enumerate(providers[:-1]):
                conflicts[loser].losing_files += 1
                for higher in providers[i + 1 :]:
                    conflicts[loser].overwritten_by.add(higher)
                    conflicts[higher].overwrites.add(loser)
        self.file_owners = owners
        self.conflicts = conflicts

    def conflicting_files(self, name: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Return ``(won, lost)`` lists of ``(path, other mod)`` for a mod."""
        won: list[tuple[str, str]] = []
        lost: list[tuple[str, str]] = []
        mod = self.overwrite if name == OVERWRITE else self.get(name)
        if mod is None:
            return won, lost
        files = mod.files()
        for key, providers in self.file_owners.items():
            if name not in providers or len(providers) < 2:
                continue
            rel = files.get(key, key)
            if providers[-1] == name:
                won.append((rel, ", ".join(p for p in providers[:-1])))
            else:
                lost.append((rel, providers[-1]))
        return sorted(won), sorted(lost)

    def deployment_plan(self) -> dict[str, tuple[str, Path]]:
        """Map each lower-cased Data-relative path to ``(real relative path, source file)``."""
        plan: dict[str, tuple[str, Path]] = {}
        for mod in self.active_sources():
            for key, rel in mod.files().items():
                plan[key] = (rel, mod.path / rel)
        return plan

    # ----------------------------------------------------------------- plugins

    def plugin_entries(self, untracked_data_files: list[str]) -> list[PluginEntry]:
        """Build the plugin list for the current mod setup.

        ``untracked_data_files`` are file names at the root of the game's Data
        folder that the manager did not deploy (the base game and anything the
        user installed by hand).
        """
        game = self.instance.game
        if not game.has_plugins:
            return []
        available: dict[str, PluginEntry] = {}
        data_dir = self.instance.data_dir
        for fn in untracked_data_files:
            if is_plugin_name(fn):
                available[fn.lower()] = PluginEntry(fn, origin="", path=data_dir / fn)
        for mod in self.active_sources():
            for key, rel in mod.files().items():
                if "/" not in rel and is_plugin_name(rel):
                    available[key] = PluginEntry(rel, origin=mod.name, path=mod.path / rel)

        implicit = [p.lower() for p in game.implicit_plugins] + [p.lower() for p in self._ccc_plugins()]
        for entry in available.values():
            ext = entry.name.lower()[-4:]
            header = read_header(entry.path) if entry.path else None
            entry.masters = header.masters if header else []
            entry.is_master = ext in (".esm", ".esl") or bool(header and header.master_flag)
            entry.is_light = game.supports_light_plugins and (
                ext == ".esl" or bool(header and header.light_flag)
            )
            entry.implicit = entry.name.lower() in implicit

        saved = read_order_file(self.profile.plugins_file)
        ordered_names = merge_saved_order(saved, [e.name for e in available.values()])
        entries: list[PluginEntry] = []
        for name, enabled in ordered_names:
            entry = available[name.lower()]
            entry.enabled = True if entry.implicit else enabled
            entries.append(entry)
        # Implicit plugins load first, in the engine's fixed order.
        rank = {name: i for i, name in enumerate(implicit)}
        implicit_entries = sorted((e for e in entries if e.implicit), key=lambda e: rank[e.name.lower()])
        entries = implicit_entries + [e for e in entries if not e.implicit]
        entries = partition_masters(entries)
        assign_load_indices(entries, game.supports_light_plugins)
        return entries

    def save_plugin_order(self, entries: list[PluginEntry]) -> None:
        saved = read_order_file(self.profile.plugins_file)
        merged = update_saved_order(saved, [(e.name, e.enabled) for e in entries])
        paths.write_text(self.profile.plugins_file, format_order_file(merged))

    def _ccc_plugins(self) -> list[str]:
        ccc = self.instance.game.ccc_file
        if not ccc:
            return []
        path = paths.find_child_ci(self.instance.game_dir, ccc)
        if path is None:
            return []
        try:
            return [ln.strip() for ln in path.read_text(errors="replace").splitlines() if ln.strip()]
        except OSError:
            return []
