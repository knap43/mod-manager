"""Dependency and setup checks shown in the Problems tab.

Each check produces :class:`Problem` objects. Where the manager can resolve a
problem itself (enable a mod or plugin, fix the load order) the problem carries
a ``fix``; otherwise it may carry a ``url`` to get what is missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from modmanager.core import nexus, script_extender
from modmanager.core.plugins import PluginEntry, fix_master_order, is_plugin_name

if TYPE_CHECKING:
    from modmanager.core.manager import Manager

ERROR, WARNING = "error", "warning"
MAX_FULL_PLUGINS = 254  # Load indices 00–FD; FE is for light plugins, FF for runtime forms.


@dataclass
class Problem:
    key: str                      # Stable identifier, used to ignore a problem
    severity: str
    title: str
    detail: str = ""
    fix_label: str = ""
    fix: Callable[[], None] | None = field(default=None, repr=False)
    url: str = ""


def find_problems(manager: "Manager") -> list[Problem]:
    problems: list[Problem] = []
    entries = manager.plugins() if manager.instance.game.has_plugins else []
    plan = set(manager.modlist.deployment_plan())
    problems += _master_problems(manager, entries)
    problems += _plugin_limit(manager, entries)
    problems += _script_extender_problems(manager, plan)
    problems += _nexus_requirements(manager)
    order = {ERROR: 0, WARNING: 1}
    return sorted(problems, key=lambda p: (order.get(p.severity, 2), p.title.lower()))


# ------------------------------------------------------------------- masters


def _master_problems(manager: "Manager", entries: list[PluginEntry]) -> list[Problem]:
    problems: list[Problem] = []
    by_name = {e.name.lower(): e for e in entries}
    needed_by: dict[str, list[str]] = {}
    spelled: dict[str, str] = {}
    for e in entries:
        for m in e.missing_masters:
            needed_by.setdefault(m.lower(), []).append(e.name)
            spelled.setdefault(m.lower(), m)

    providers = _disabled_mod_plugins(manager)
    for key, users in sorted(needed_by.items()):
        master = spelled[key]
        users_text = ", ".join(users)
        if key in by_name:
            problems.append(Problem(
                f"master:{key}", ERROR, f"{master} is disabled but required",
                f"Required by {users_text}. The game crashes at startup when a master is missing.",
                f"Enable {master}", lambda m=master: manager.set_plugins_enabled([m], True),
            ))
        elif key in providers:
            mod = providers[key]
            problems.append(Problem(
                f"master:{key}", ERROR, f"{master} is in the disabled mod {mod}",
                f"Required by {users_text}. The game crashes at startup when a master is missing.",
                f"Enable {mod}", lambda m=mod: manager.modlist.set_enabled([m], True),
            ))
        else:
            problems.append(Problem(
                f"master:{key}", ERROR, f"{master} is missing",
                f"Required by {users_text}, but no installed mod provides it. Install the mod it "
                "comes from, or disable the plugins that need it — the game crashes at startup otherwise.",
            ))

    late = [e for e in entries if e.late_masters]
    if late:
        detail = "\n".join(f"{e.name} loads before {', '.join(e.late_masters)}" for e in late[:20])
        problems.append(Problem(
            "order:masters", ERROR, f"{len(late)} plugin(s) load before their masters", detail,
            "Fix load order", lambda: manager.fix_master_order(),
        ))
    return problems


def _disabled_mod_plugins(manager: "Manager") -> dict[str, str]:
    """Plugin file name (lower case) -> disabled mod that contains it."""
    result: dict[str, str] = {}
    for mod in manager.modlist.mods:
        if mod.enabled or mod.is_separator:
            continue
        for key in mod.files():
            if "/" not in key and is_plugin_name(key):
                result.setdefault(key, mod.name)
    return result


def _plugin_limit(manager: "Manager", entries: list[PluginEntry]) -> list[Problem]:
    full = sum(1 for e in entries if e.enabled and not e.is_light)
    if full <= MAX_FULL_PLUGINS:
        return []
    return [Problem(
        "limit:plugins", ERROR, f"{full} full plugins are active; the game supports {MAX_FULL_PLUGINS}",
        "Disable plugins, merge them, or use ESL-flagged versions (they don't count towards the limit).",
    )]


# ----------------------------------------------------------- script extender


def _script_extender_problems(manager: "Manager", plan: set[str]) -> list[Problem]:
    status = manager.script_extender_status()
    if status is None:
        return []
    se = status.extender
    dlls = script_extender.plugin_dlls(plan, se)
    if not dlls:
        return []
    names = ", ".join(d.rsplit("/", 1)[-1] for d in dlls[:8]) + (" …" if len(dlls) > 8 else "")
    page = nexus.mod_url(manager.instance.game.nexus_domain, se.nexus_id) \
        if se.nexus_id and manager.instance.game.nexus_domain else se.url
    if not status.installed:
        return [Problem(
            "se:missing", ERROR, f"{se.name} is not installed, but {len(dlls)} mod(s) need it",
            f"Plugins that will not load: {names}\n\n{status.details()}", url=page,
        )]
    problems: list[Problem] = []
    if status.compatible is False:
        problems.append(Problem(
            "se:version", ERROR, f"{se.name} does not match the game version", status.details(), url=page,
        ))
    if se.address_library_id and status.game_version:
        wanted = script_extender.address_library_file(status.game_version)
        folder = se.plugins_dir.lower()
        have = any(k.startswith(folder + "/") and k.endswith(".bin") and k.rsplit("/", 1)[-1] == wanted
                   for k in plan)
        if wanted and not have and not _data_has(manager, f"{se.plugins_dir}/{wanted}"):
            problems.append(Problem(
                "se:addresslib", WARNING,
                f"Address Library for game version {script_extender.fmt(status.game_version)} is missing",
                f"Most {se.name} plugins need \"Address Library for SKSE Plugins\" ({wanted}) and "
                f"crash or refuse to load without it. Enabled {se.name} plugins: {names}",
                url=nexus.mod_url(manager.instance.game.nexus_domain, se.address_library_id)
                if manager.instance.game.nexus_domain else "",
            ))
    return problems


def _data_has(manager: "Manager", rel: str) -> bool:
    from modmanager.core.fomod import resolve_ci

    found = resolve_ci(manager.instance.data_dir, rel)
    return found is not None and found.is_file()


# -------------------------------------------------------- Nexus requirements


def _nexus_requirements(manager: "Manager") -> list[Problem]:
    game = manager.instance.game
    if not game.nexus_domain:
        return []
    installed: dict[int, list] = {}
    for mod in manager.modlist.mods:
        if mod.meta.get("nexus_id"):
            installed.setdefault(int(mod.meta["nexus_id"]), []).append(mod)
    se_status = manager.script_extender_status()
    se = game.script_extender

    wanted: dict[int, dict] = {}   # required mod id -> {"name", "url", "by": [mods]}
    for mod in manager.modlist.mods:
        if not mod.enabled:
            continue
        for req in mod.meta.get("nexus_requirements") or []:
            rid = req.get("mod_id")
            if req.get("external") or rid is None or req.get("game", game.nexus_domain) != game.nexus_domain:
                continue
            if se and rid == se.nexus_id and se_status and se_status.installed:
                continue
            if rid == int(mod.meta.get("nexus_id") or 0):
                continue
            entry = wanted.setdefault(rid, {"name": req.get("name") or f"mod {rid}", "url": req.get("url"), "by": []})
            entry["by"].append(mod.display_name)

    problems: list[Problem] = []
    for rid, info in sorted(wanted.items(), key=lambda kv: kv[1]["name"].lower()):
        providers = installed.get(rid, [])
        if any(m.enabled for m in providers):
            continue
        by = ", ".join(sorted(set(info["by"])))
        url = info["url"] or nexus.mod_url(game.nexus_domain, rid)
        if providers:
            names = [m.name for m in providers]
            problems.append(Problem(
                f"req:{rid}", WARNING, f"{info['name']} is required but disabled",
                f"Listed as a requirement on Nexus Mods by: {by}",
                f"Enable {providers[0].display_name}", lambda n=names[:1]: manager.modlist.set_enabled(n, True),
                url=url,
            ))
        else:
            problems.append(Problem(
                f"req:{rid}", WARNING, f"{info['name']} is required but not installed",
                f"Listed as a requirement on Nexus Mods by: {by}\n\nAuthors sometimes list optional "
                "patches or alternatives here; ignore this if you have an equivalent installed.",
                url=url,
            ))
    return problems
