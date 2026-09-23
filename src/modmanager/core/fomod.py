"""FOMOD installers (``fomod/ModuleConfig.xml``).

Implements the XML installer format used by most Skyrim mods: install steps
made of option groups, condition flags set by chosen options, plugin types that
depend on flags, installed files and game version, and conditional file installs.

The engine is toolkit-independent: :class:`FomodSession` holds the user's
choices and turns them into a list of files, which :meth:`FomodSession.build`
copies into a folder laid out like the game's Data folder.
"""

from __future__ import annotations

import mmap
import os
import re
import shutil
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from modmanager.core import paths

# ---------------------------------------------------------------------- model

REQUIRED, RECOMMENDED, OPTIONAL, NOT_USABLE, COULD_BE_USABLE = (
    "Required", "Recommended", "Optional", "NotUsable", "CouldBeUsable",
)
SELECT_ONE, SELECT_AT_MOST_ONE, SELECT_AT_LEAST_ONE, SELECT_ALL, SELECT_ANY = (
    "SelectExactlyOne", "SelectAtMostOne", "SelectAtLeastOne", "SelectAll", "SelectAny",
)
GROUP_TYPES = {SELECT_ONE, SELECT_AT_MOST_ONE, SELECT_AT_LEAST_ONE, SELECT_ALL, SELECT_ANY}


class FomodError(Exception):
    pass


@dataclass
class FomodContext:
    """What the installer may ask about the user's setup."""

    # Returns "Active", "Inactive" or "Missing" for a Data-relative path.
    file_state: Callable[[str], str] = lambda _path: "Missing"
    game_version: tuple[int, ...] | None = None
    script_extender_version: tuple[int, ...] | None = None


@dataclass
class FileInstall:
    source: str
    destination: str
    folder: bool = False
    priority: int = 0
    always: bool = False        # alwaysInstall: installed even if the option isn't chosen
    if_usable: bool = False     # installIfUsable: installed unless the option is NotUsable
    order: int = 0              # Document order, the tie-breaker for equal priorities


@dataclass
class Condition:
    """A composite dependency: ``operator`` over flag/file/game checks and nested conditions."""

    operator: str = "And"
    flags: list[tuple[str, str]] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)
    game_versions: list[tuple[int, ...]] = field(default_factory=list)
    se_versions: list[tuple[int, ...]] = field(default_factory=list)
    nested: list["Condition"] = field(default_factory=list)

    def check(self, flags: dict[str, str], ctx: FomodContext) -> bool:
        results: list[bool] = []
        results += [flags.get(name, "") == value for name, value in self.flags]
        results += [_file_matches(ctx.file_state(f), state) for f, state in self.files]
        results += [ctx.game_version is None or ctx.game_version >= v for v in self.game_versions]
        results += [ctx.script_extender_version is None or ctx.script_extender_version >= v
                    for v in self.se_versions]
        results += [c.check(flags, ctx) for c in self.nested]
        if not results:
            return True
        return any(results) if self.operator.lower() == "or" else all(results)

    def describe(self) -> list[str]:
        """Human-readable requirements, for error messages."""
        out = [f"{f} must be {s.lower()}" for f, s in self.files]
        out += ["game version " + ".".join(map(str, v)) + " or newer" for v in self.game_versions]
        out += ["script extender " + ".".join(map(str, v)) + " or newer" for v in self.se_versions]
        for c in self.nested:
            out += c.describe()
        return out


def _file_matches(actual: str, wanted: str) -> bool:
    wanted = wanted.lower()
    if wanted == "active":
        return actual == "Active"
    if wanted == "inactive":
        return actual == "Inactive"
    if wanted == "missing":
        return actual == "Missing"
    return True


@dataclass
class Option:
    name: str
    description: str = ""
    image: str = ""
    files: list[FileInstall] = field(default_factory=list)
    flags: list[tuple[str, str]] = field(default_factory=list)
    default_type: str = OPTIONAL
    type_patterns: list[tuple[Condition, str]] = field(default_factory=list)

    def type(self, flags: dict[str, str], ctx: FomodContext) -> str:
        for cond, t in self.type_patterns:
            if cond.check(flags, ctx):
                return t
        return self.default_type


@dataclass
class Group:
    name: str
    type: str
    options: list[Option]


@dataclass
class Step:
    name: str
    visible: Condition | None
    groups: list[Group]


@dataclass
class ModuleConfig:
    name: str = ""
    image: str = ""
    dependencies: Condition | None = None
    required_files: list[FileInstall] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    conditional: list[tuple[Condition, list[FileInstall]]] = field(default_factory=list)


@dataclass
class ModuleInfo:
    name: str = ""
    author: str = ""
    version: str = ""
    website: str = ""
    description: str = ""


# -------------------------------------------------------------------- parsing


def find_fomod(folder: Path, depth: int = 3) -> Path | None:
    """Return the folder that contains ``fomod/ModuleConfig.xml`` (the installer root)."""
    fomod = paths.find_child_ci(folder, "fomod")
    if fomod is not None and fomod.is_dir() and paths.find_child_ci(fomod, "ModuleConfig.xml"):
        return folder
    if depth <= 0:
        return None
    try:
        subdirs = [Path(e.path) for e in os.scandir(folder) if e.is_dir() and not e.name.startswith(".")]
    except OSError:
        return None
    # Only descend through wrapper folders; an archive with several option
    # folders and no fomod at the top is not a FOMOD.
    if len(subdirs) == 1:
        return find_fomod(subdirs[0], depth - 1)
    return None


def _read_xml(path: Path) -> ET.Element:
    """Parse FOMOD XML, tolerating the wrong encodings real installers ship with."""
    data = path.read_bytes()
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        text = None
        if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = data.decode("utf-16")
        else:
            for enc in ("utf-8-sig", "utf-16", "cp1252"):
                try:
                    text = data.decode(enc)
                    if "<" in text:
                        break
                except UnicodeDecodeError:
                    continue
        if text is None:
            raise FomodError(f"Cannot decode {path.name}")
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text.lstrip("﻿"))
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise FomodError(f"{path.name} is not valid XML: {exc}") from exc
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text)[:4]) or (0,)


def _sort(items: list, order: str | None, key=lambda x: x.name):
    order = (order or "Ascending").lower()
    if order == "explicit":
        return items
    return sorted(items, key=lambda x: key(x).casefold(), reverse=order == "descending")


def _parse_condition(el: ET.Element | None) -> Condition | None:
    if el is None:
        return None
    cond = Condition(operator=el.get("operator", "And"))
    for child in el:
        tag = child.tag
        if tag == "flagDependency":
            cond.flags.append((child.get("flag", ""), child.get("value", "")))
        elif tag == "fileDependency":
            cond.files.append((child.get("file", "").replace("\\", "/"), child.get("state", "Active")))
        elif tag == "gameDependency":
            cond.game_versions.append(_version(child.get("version", "0")))
        elif tag in ("foseDependency", "scriptExtenderDependency"):
            cond.se_versions.append(_version(child.get("version", "0")))
        elif tag == "dependencies":
            nested = _parse_condition(child)
            if nested is not None:
                cond.nested.append(nested)
        # fommDependency (mod manager version) is always satisfied.
    return cond


def _parse_files(el: ET.Element | None, counter: list[int]) -> list[FileInstall]:
    files: list[FileInstall] = []
    if el is None:
        return files
    for child in el:
        if child.tag not in ("file", "folder"):
            continue
        source = child.get("source", "").replace("\\", "/").strip("/")
        dest = child.get("destination")
        dest = source if dest is None else dest.replace("\\", "/").lstrip("/")
        try:
            priority = int(child.get("priority", "0"))
        except ValueError:
            priority = 0
        counter[0] += 1
        files.append(FileInstall(
            source=source,
            destination=dest,
            folder=child.tag == "folder",
            priority=priority,
            always=child.get("alwaysInstall", "false").lower() == "true",
            if_usable=child.get("installIfUsable", "false").lower() == "true",
            order=counter[0],
        ))
    return files


def parse_module_config(path: Path) -> ModuleConfig:
    root = _read_xml(path)
    counter = [0]
    config = ModuleConfig(name=_text(root.find("moduleName")))
    image = root.find("moduleImage")
    if image is not None and image.get("showImage", "true").lower() != "false":
        config.image = image.get("path", "").replace("\\", "/")
    config.dependencies = _parse_condition(root.find("moduleDependencies"))
    config.required_files = _parse_files(root.find("requiredInstallFiles"), counter)

    steps_el = root.find("installSteps")
    steps: list[Step] = []
    if steps_el is not None:
        for step_el in steps_el.findall("installStep"):
            groups: list[Group] = []
            groups_el = step_el.find("optionalFileGroups")
            if groups_el is not None:
                for group_el in groups_el.findall("group"):
                    options: list[Option] = []
                    plugins_el = group_el.find("plugins")
                    for opt_el in plugins_el.findall("plugin") if plugins_el is not None else []:
                        options.append(_parse_option(opt_el, counter))
                    if plugins_el is not None:
                        options = _sort(options, plugins_el.get("order"))
                    gtype = group_el.get("type", SELECT_ANY)
                    groups.append(Group(group_el.get("name", ""), gtype if gtype in GROUP_TYPES else SELECT_ANY, options))
                groups = _sort(groups, groups_el.get("order"))
            steps.append(Step(step_el.get("name", ""), _parse_condition(step_el.find("visible")), groups))
        steps = _sort(steps, steps_el.get("order"))
    config.steps = steps

    cond_el = root.find("conditionalFileInstalls")
    if cond_el is not None:
        for pattern in cond_el.iter("pattern"):
            cond = _parse_condition(pattern.find("dependencies")) or Condition()
            config.conditional.append((cond, _parse_files(pattern.find("files"), counter)))
    return config


def _parse_option(el: ET.Element, counter: list[int]) -> Option:
    opt = Option(name=el.get("name", ""), description=_text(el.find("description")))
    image = el.find("image")
    if image is not None:
        opt.image = image.get("path", "").replace("\\", "/")
    opt.files = _parse_files(el.find("files"), counter)
    flags_el = el.find("conditionFlags")
    if flags_el is not None:
        opt.flags = [(f.get("name", ""), f.text or "") for f in flags_el.findall("flag")]
    td = el.find("typeDescriptor")
    if td is not None:
        simple = td.find("type")
        dep_type = td.find("dependencyType")
        if simple is not None:
            opt.default_type = simple.get("name", OPTIONAL)
        elif dep_type is not None:
            default = dep_type.find("defaultType")
            opt.default_type = default.get("name", OPTIONAL) if default is not None else OPTIONAL
            for pattern in dep_type.iter("pattern"):
                cond = _parse_condition(pattern.find("dependencies")) or Condition()
                t = pattern.find("type")
                opt.type_patterns.append((cond, t.get("name", OPTIONAL) if t is not None else OPTIONAL))
    return opt


def parse_info(path: Path | None) -> ModuleInfo:
    if path is None or not path.is_file():
        return ModuleInfo()
    try:
        root = _read_xml(path)
    except (FomodError, OSError):
        return ModuleInfo()

    def get(tag: str) -> str:
        el = next((e for e in root.iter() if isinstance(e.tag, str) and e.tag.lower() == tag), None)
        return _text(el)

    return ModuleInfo(get("name"), get("author"), get("version"), get("website"), get("description"))


def load(root: Path) -> tuple[ModuleConfig, ModuleInfo]:
    fomod = paths.find_child_ci(root, "fomod")
    if fomod is None:
        raise FomodError("No fomod folder")
    config_file = paths.find_child_ci(fomod, "ModuleConfig.xml")
    if config_file is None:
        raise FomodError("No ModuleConfig.xml")
    return parse_module_config(config_file), parse_info(paths.find_child_ci(fomod, "info.xml"))


# -------------------------------------------------------------------- session


class FomodSession:
    """The user's walk through an installer.

    ``selections[step][group]`` holds the indices of chosen options. Flags are
    always recomputed from the choices in the steps that are visible, so going
    back and changing an answer correctly shows or hides later steps.
    """

    def __init__(self, config: ModuleConfig, root: Path, ctx: FomodContext | None = None,
                 previous: dict | None = None):
        self.config = config
        self.root = root
        self.ctx = ctx or FomodContext()
        self.previous = previous or {}
        self.selections: dict[int, dict[int, list[int]]] = {}

    # --- flags and visibility

    def flags_before(self, step_index: int) -> dict[str, str]:
        flags: dict[str, str] = {}
        for i in range(step_index):
            step = self.config.steps[i]
            if not self._visible(step, flags):
                continue
            for g, chosen in self.selections.get(i, {}).items():
                for o in chosen:
                    for name, value in step.groups[g].options[o].flags:
                        flags[name] = value
        return flags

    def _visible(self, step: Step, flags: dict[str, str]) -> bool:
        return step.visible is None or step.visible.check(flags, self.ctx)

    def is_visible(self, step_index: int) -> bool:
        return self._visible(self.config.steps[step_index], self.flags_before(step_index))

    def next_step(self, after: int) -> int | None:
        for i in range(after + 1, len(self.config.steps)):
            if self.is_visible(i):
                return i
        return None

    def previous_step(self, before: int) -> int | None:
        for i in range(before - 1, -1, -1):
            if self.is_visible(i):
                return i
        return None

    def final_flags(self) -> dict[str, str]:
        return self.flags_before(len(self.config.steps))

    def module_requirements_met(self) -> bool:
        dep = self.config.dependencies
        return dep is None or dep.check({}, self.ctx)

    # --- options

    def option_type(self, step_index: int, group_index: int, option_index: int) -> str:
        opt = self.config.steps[step_index].groups[group_index].options[option_index]
        return opt.type(self.flags_before(step_index), self.ctx)

    def ensure_selection(self, step_index: int) -> dict[int, list[int]]:
        """Selections for a step, creating defaults on first visit and enforcing rules."""
        step = self.config.steps[step_index]
        existing = self.selections.get(step_index)
        prev_step = self.previous.get(step.name, {})
        result: dict[int, list[int]] = {}
        for g, group in enumerate(step.groups):
            types = [self.option_type(step_index, g, o) for o in range(len(group.options))]
            if existing is not None and g in existing:
                chosen = list(existing[g])
            elif group.name in prev_step:
                names = prev_step[group.name]
                chosen = [o for o, opt in enumerate(group.options) if opt.name in names]
            else:
                chosen = [o for o, t in enumerate(types) if t in (REQUIRED, RECOMMENDED)]
            result[g] = self._enforce(group, types, chosen)
        self.selections[step_index] = result
        return result

    def _enforce(self, group: Group, types: list[str], chosen: list[int]) -> list[int]:
        usable = [o for o, t in enumerate(types) if t != NOT_USABLE]
        chosen = [o for o in chosen if o in usable]
        required = [o for o, t in enumerate(types) if t == REQUIRED]
        if group.type == SELECT_ALL:
            return usable
        if group.type in (SELECT_ONE, SELECT_AT_MOST_ONE):
            pick = (required or chosen)[:1]
            if not pick and group.type == SELECT_ONE and usable:
                pick = [usable[0]]
            return pick
        chosen = sorted(set(chosen) | set(required))
        if not chosen and group.type == SELECT_AT_LEAST_ONE and usable:
            chosen = [usable[0]]
        return chosen

    def select(self, step_index: int, group_index: int, option_index: int, on: bool) -> None:
        sel = self.ensure_selection(step_index)
        group = self.config.steps[step_index].groups[group_index]
        types = [self.option_type(step_index, group_index, o) for o in range(len(group.options))]
        current = set(sel[group_index])
        if group.type in (SELECT_ONE, SELECT_AT_MOST_ONE):
            current = {option_index} if on else (set() if group.type == SELECT_AT_MOST_ONE else current)
        elif on:
            current.add(option_index)
        elif types[option_index] != REQUIRED:
            current.discard(option_index)
        sel[group_index] = self._enforce(group, types, sorted(current))

    def validate(self, step_index: int) -> str | None:
        sel = self.ensure_selection(step_index)
        for g, group in enumerate(self.config.steps[step_index].groups):
            if group.type == SELECT_AT_LEAST_ONE and not sel[g]:
                return f"Select at least one option in '{group.name}'."
            if group.type == SELECT_ONE and len(sel[g]) != 1:
                return f"Select one option in '{group.name}'."
        return None

    def choices(self) -> dict[str, dict[str, list[str]]]:
        """Chosen option names by step and group, to preselect them on reinstall."""
        out: dict[str, dict[str, list[str]]] = {}
        flags: dict[str, str] = {}
        for i, step in enumerate(self.config.steps):
            if not self._visible(step, flags) or i not in self.selections:
                continue
            out[step.name] = {
                step.groups[g].name: [step.groups[g].options[o].name for o in chosen]
                for g, chosen in self.selections[i].items()
            }
            for g, chosen in self.selections[i].items():
                for o in chosen:
                    for name, value in step.groups[g].options[o].flags:
                        flags[name] = value
        return out

    # --- files

    def files(self) -> list[FileInstall]:
        """Every file operation, in the order they are applied (later wins)."""
        result = list(self.config.required_files)
        flags: dict[str, str] = {}
        for i, step in enumerate(self.config.steps):
            visible = self._visible(step, flags)
            chosen_map = self.selections.get(i, {}) if visible else {}
            for g, group in enumerate(step.groups):
                chosen = set(chosen_map.get(g, []))
                for o, opt in enumerate(group.options):
                    selected = o in chosen
                    usable = opt.type(flags, self.ctx) != NOT_USABLE
                    for f in opt.files:
                        if selected or f.always or (f.if_usable and usable):
                            result.append(f)
            for g, chosen in chosen_map.items():
                for o in chosen:
                    for name, value in step.groups[g].options[o].flags:
                        flags[name] = value
        for cond, files in self.config.conditional:
            if cond.check(flags, self.ctx):
                result.extend(files)
        return sorted(result, key=lambda f: (f.priority, f.order))

    def build(self, out_dir: Path) -> list[str]:
        """Copy the selected files into ``out_dir``. Returns warnings for missing sources."""
        warnings: list[str] = []
        out_dir.mkdir(parents=True, exist_ok=True)
        for f in self.files():
            src = resolve_ci(self.root, f.source) if f.source else self.root
            if src is None or not src.exists():
                warnings.append(f"Missing in archive: {f.source}")
                continue
            if f.folder or src.is_dir():
                dest_dir = _ci_mkdirs(out_dir, f.destination)
                _copy_tree(src, dest_dir)
            else:
                dest = f.destination
                if not dest or dest.endswith("/"):
                    dest += src.name
                parent, _, name = dest.rpartition("/")
                target_dir = _ci_mkdirs(out_dir, parent)
                _copy_file(src, target_dir / name)
        return warnings


# --------------------------------------------------------------- file helpers


def resolve_ci(root: Path, rel: str) -> Path | None:
    current = root
    for part in [p for p in rel.replace("\\", "/").split("/") if p and p != "."]:
        found = paths.find_child_ci(current, part)
        if found is None:
            return None
        current = found
    return current


def _ci_mkdirs(root: Path, rel: str) -> Path:
    """Create ``rel`` below ``root``, reusing folders that differ only in case."""
    current = root
    for part in [p for p in rel.split("/") if p and p != "."]:
        found = paths.find_child_ci(current, part)
        if found is None or not found.is_dir():
            found = current / part
            found.mkdir(exist_ok=True)
        current = found
    return current


def _copy_file(src: Path, dest: Path) -> None:
    existing = paths.find_child_ci(dest.parent, dest.name)
    if existing is not None and existing.is_file():
        existing.unlink()
    shutil.copy2(src, dest)


def _copy_tree(src: Path, dest: Path) -> None:
    for entry in os.scandir(src):
        if entry.is_dir():
            _copy_tree(Path(entry.path), _ci_mkdirs(dest, entry.name))
        else:
            _copy_file(Path(entry.path), dest / entry.name)


# ------------------------------------------------------------------ PE version


def pe_file_version(path: Path) -> tuple[int, ...] | None:
    """Read FileVersion from a Windows executable's VS_FIXEDFILEINFO."""
    try:
        with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            key = "VS_VERSION_INFO".encode("utf-16-le")
            pos = mm.find(key)
            while pos != -1:
                sig = mm.find(b"\xbd\x04\xef\xfe", pos, pos + 128)
                if sig != -1:
                    ms, ls = struct.unpack_from("<II", mm, sig + 8)
                    return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
                pos = mm.find(key, pos + 1)
    except (OSError, ValueError):
        return None
    return None
