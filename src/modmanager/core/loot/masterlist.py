"""LOOT masterlist download, caching and metadata lookup.

The masterlist (https://github.com/loot/<game>) assigns plugins to groups,
declares load-after rules, requirements and incompatibilities, and carries
messages such as "dirty, clean with SSEEdit". An optional per-instance
``loot/userlist.yaml`` in the same format adds or overrides entries.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from modmanager.core import paths

log = logging.getLogger(__name__)

BRANCHES = ("v0.26", "v0.21")  # Newest masterlist format first.
URL = "https://raw.githubusercontent.com/loot/{repo}/{branch}/masterlist.yaml"
MAX_AGE = 24 * 3600
REGEX_CHARS = set(":\\*?|")


class MasterlistError(Exception):
    pass


def cache_dir(repo: str) -> Path:
    return paths.data_home() / "loot" / repo


def _yaml_load(text: str):
    import yaml

    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    return yaml.load(text, Loader=loader)


def update(repo: str, force: bool = False, timeout: float = 30) -> Path:
    """Download the masterlist if the cached copy is older than a day. Returns its path."""
    folder = cache_dir(repo)
    target = folder / "masterlist.yaml"
    if not force and target.is_file() and time.time() - target.stat().st_mtime < MAX_AGE:
        return target
    from modmanager.core.nexus import ssl_context

    ctx = ssl_context()
    last_error: Exception | None = None
    for branch in BRANCHES:
        url = URL.format(repo=repo, branch=branch)
        try:
            with urllib.request.urlopen(url, timeout=timeout, context=ctx) as resp:
                text = resp.read().decode("utf-8")
            _yaml_load(text)  # Don't replace a good copy with a broken download.
            paths.write_text(target, text)
            paths.write_json(folder / "source.json", {"url": url, "fetched": int(time.time())})
            (folder / "masterlist.json").unlink(missing_ok=True)
            log.info("Updated the LOOT masterlist from %s", url)
            return target
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code != 404:
                break
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_error = exc
            break
        except Exception as exc:  # noqa: BLE001 - YAML errors
            last_error = exc
            break
    if target.is_file():
        log.warning("Could not update the LOOT masterlist (%s); using the cached copy.", last_error)
        return target
    raise MasterlistError(f"Could not download the LOOT masterlist: {last_error}")


def load_yaml_cached(path: Path) -> dict:
    """Parse YAML, caching the result as JSON next to it (parsing the masterlist is slow)."""
    cache = path.with_suffix(".json")
    if cache.is_file() and cache.stat().st_mtime >= path.stat().st_mtime:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            pass
    data = _yaml_load(path.read_text(encoding="utf-8")) or {}
    try:
        paths.write_text(cache, json.dumps(data))
    except (OSError, TypeError):
        pass
    return data


@dataclass
class FileRef:
    name: str
    condition: str | None = None
    display: str = ""

    @classmethod
    def parse(cls, raw) -> "FileRef":
        if isinstance(raw, dict):
            return cls(str(raw.get("name", "")), raw.get("condition"), str(raw.get("display") or ""))
        return cls(str(raw))


@dataclass
class PluginMeta:
    group: str | None = None
    after: list[FileRef] = field(default_factory=list)
    req: list[FileRef] = field(default_factory=list)
    inc: list[FileRef] = field(default_factory=list)
    msg: list[dict] = field(default_factory=list)
    dirty: list[dict] = field(default_factory=list)
    url: list[str] = field(default_factory=list)

    def merge(self, entry: dict) -> None:
        if entry.get("group"):
            self.group = entry["group"]
        self.after += [FileRef.parse(x) for x in entry.get("after", []) or []]
        self.req += [FileRef.parse(x) for x in entry.get("req", []) or []]
        self.inc += [FileRef.parse(x) for x in entry.get("inc", []) or []]
        self.msg += [m for m in entry.get("msg", []) or [] if isinstance(m, dict)]
        self.dirty += [d for d in entry.get("dirty", []) or [] if isinstance(d, dict)]
        for u in entry.get("url", []) or []:
            self.url.append(u if isinstance(u, str) else str(u.get("link", "")))


class Metadata:
    """Masterlist plus userlist, indexed for lookups by plugin name."""

    def __init__(self, lists: list[dict]):
        self.groups: dict[str, list[str]] = {}
        self.group_order: list[str] = []
        self._exact: dict[str, list[dict]] = {}
        self._regex: list[tuple[re.Pattern, dict]] = []
        for data in lists:
            new_groups = []
            for g in data.get("groups", []) or []:
                name = g.get("name")
                if not name:
                    continue
                if name not in self.groups:
                    new_groups.append(name)
                    self.groups[name] = []
                self.groups[name] += list(g.get("after", []) or [])
            # LOOT adds masterlist groups before userlist groups, each in lexicographical order.
            self.group_order += sorted(new_groups)
            for entry in data.get("plugins", []) or []:
                name = str(entry.get("name", ""))
                if REGEX_CHARS & set(name):
                    try:
                        self._regex.append((re.compile(name, re.I), entry))
                    except re.error:
                        log.debug("Bad regex in masterlist: %s", name)
                else:
                    self._exact.setdefault(name.lower(), []).append(entry)
        if "default" not in self.groups:
            self.groups["default"] = []
            self.group_order.insert(0, "default")

    @classmethod
    def load(cls, masterlist: Path | None, userlist: Path | None = None) -> "Metadata":
        lists = []
        if masterlist and masterlist.is_file():
            lists.append(load_yaml_cached(masterlist))
        if userlist and userlist.is_file():
            lists.append(_yaml_load(userlist.read_text(encoding="utf-8")) or {})
        return cls(lists)

    def for_plugin(self, name: str) -> PluginMeta:
        meta = PluginMeta()
        for rx, entry in self._regex:
            if rx.fullmatch(name):
                meta.merge(entry)
        for entry in self._exact.get(name.lower(), []):
            meta.merge(entry)
        if meta.group and meta.group not in self.groups:
            log.warning("LOOT metadata puts %s in unknown group %r; using default", name, meta.group)
            meta.group = None
        return meta
