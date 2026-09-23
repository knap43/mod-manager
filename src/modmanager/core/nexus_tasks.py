"""Nexus workflows: downloading nxm links, update checks, requirements, identification.

Results are stored in each mod's ``meta.json``:

``nexus_id`` / ``nexus_file_id`` / ``nexus_game``  where the mod came from
``nexus_latest_version`` / ``nexus_update``         result of the last update check
``nexus_requirements``                              requirements listed on the mod page
``nexus_checked``                                   when it was last checked (Unix time)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from modmanager.core import paths
from modmanager.core.modlist import Mod
from modmanager.core.nexus import NexusClient, NexusError, NxmLink, md5_file, sidecar, unique_download_path

log = logging.getLogger(__name__)

RECHECK_AFTER = 30 * 24 * 3600   # Re-check every mod at least monthly…
UPDATED_PERIOD = "1m"            # …and in between only those Nexus reports as updated.
DEAD_CATEGORIES = {"OLD_VERSION", "ARCHIVED", "DELETED", "REMOVED"}


def normalize_version(v: str) -> str:
    return str(v or "").strip().lower().lstrip("v")


# ------------------------------------------------------------------ download


def download_nxm(client: NexusClient, link: NxmLink, folder: Path,
                 progress: Callable[[int, int], None] | None = None,
                 cancelled: Callable[[], bool] | None = None,
                 on_start: Callable[[Path], None] | None = None) -> Path:
    """Download the file an nxm link points to and record where it came from."""
    info = client.file_info(link.game, link.mod_id, link.file_id)
    try:
        mod = client.mod_info(link.game, link.mod_id)
    except NexusError:
        mod = {}
    urls = client.download_links(link.game, link.mod_id, link.file_id, link.key, link.expires)
    if not urls:
        raise NexusError("Nexus Mods returned no download location for this file.")
    folder.mkdir(parents=True, exist_ok=True)
    dest = unique_download_path(folder, info.get("file_name") or f"{link.mod_id}-{link.file_id}.7z")
    meta = {
        "game": link.game,
        "mod_id": link.mod_id,
        "file_id": link.file_id,
        "mod_name": mod.get("name") or info.get("name") or dest.stem,
        "file_name": info.get("name") or "",
        "version": info.get("version") or info.get("mod_version") or mod.get("version") or "",
        "category": info.get("category_name") or "",
        "downloaded": int(time.time()),
    }
    paths.write_json(sidecar(dest), {**meta, "state": "downloading"})
    if on_start:
        on_start(dest)
    last_error: Exception | None = None
    for url in urls:
        try:
            client.download(url["URI"], dest, progress, cancelled)
            break
        except NexusError as exc:
            if str(exc) == "Download cancelled":
                sidecar(dest).unlink(missing_ok=True)
                raise
            last_error = exc
            log.warning("Download from %s failed: %s", url.get("name", "?"), exc)
    else:
        sidecar(dest).unlink(missing_ok=True)
        raise last_error or NexusError("Download failed")
    paths.write_json(sidecar(dest), meta)
    return dest


# ------------------------------------------------------------ update checks


@dataclass
class CheckResult:
    checked: int = 0
    skipped: int = 0
    updates: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def check_mods(client: NexusClient, game: str, mods: list[Mod], force: bool = False,
               progress: Callable[[int, int], None] | None = None) -> CheckResult:
    """Check Nexus mods for updates and fetch their requirements.

    Mods checked within the last month are only re-queried if Nexus lists them as
    updated since, which keeps a large mod list well inside the API rate limits.
    """
    result = CheckResult()
    candidates = [m for m in mods if m.meta.get("nexus_id") and m.meta.get("nexus_game", game) == game]
    recently: dict[int, int] = {}
    if not force and any(m.meta.get("nexus_checked") for m in candidates):
        try:
            recently = {int(e["mod_id"]): int(e.get("latest_file_update") or 0)
                        for e in client.updated(game, UPDATED_PERIOD)}
        except NexusError as exc:
            log.warning("Could not fetch the list of updated mods: %s", exc)
    now = int(time.time())
    for i, mod in enumerate(candidates):
        if progress:
            progress(i, len(candidates))
        mod_id = int(mod.meta["nexus_id"])
        checked = int(mod.meta.get("nexus_checked") or 0)
        fresh = checked and now - checked < RECHECK_AFTER and recently.get(mod_id, 0) <= checked
        if fresh and not force:
            result.skipped += 1
            if mod.meta.get("nexus_update"):
                result.updates.append(mod.name)
            continue
        try:
            check_mod(client, game, mod)
            result.checked += 1
            if mod.meta.get("nexus_update"):
                result.updates.append(mod.name)
        except NexusError as exc:
            result.errors.append(f"{mod.name}: {exc}")
            if exc.status in (401, 429):
                break
    if progress:
        progress(len(candidates), len(candidates))
    return result


def check_mod(client: NexusClient, game: str, mod: Mod) -> None:
    mod_id = int(mod.meta["nexus_id"])
    installed = normalize_version(mod.meta.get("version", ""))
    file_id = mod.meta.get("nexus_file_id")
    latest = ""
    update = False
    info = client.mod_info(game, mod_id)
    if file_id:
        data = client.mod_files(game, mod_id)
        files = {int(f["file_id"]): f for f in data.get("files", [])}
        chain = {int(u["old_file_id"]): int(u["new_file_id"]) for u in data.get("file_updates", [])}
        current, seen = int(file_id), set()
        while current in chain and current not in seen:
            seen.add(current)
            current = chain[current]
        newest = files.get(current)
        if current != int(file_id) and newest is not None:
            update, latest = True, str(newest.get("version") or "")
        else:
            own = files.get(int(file_id))
            latest = str((own or {}).get("version") or "")
            if own is None or own.get("category_name") in DEAD_CATEGORIES:
                # The file was retired without a successor: compare with the newest main file.
                mains = [f for f in files.values() if f.get("category_name") == "MAIN"]
                if mains:
                    latest = str(max(mains, key=lambda f: f.get("uploaded_timestamp") or 0).get("version") or "")
                    update = bool(installed) and normalize_version(latest) != installed
    else:
        latest = str(info.get("version") or "")
        update = bool(installed and latest) and normalize_version(latest) != installed
    mod.meta.update({
        "nexus_latest_version": latest,
        "nexus_update": update,
        "nexus_checked": int(time.time()),
    })
    # Requirements are a bonus: never let them spoil the update check.
    try:
        requirements = client.requirements(game, mod_id, info)
    except NexusError as exc:
        log.warning("Could not fetch requirements for %s: %s", mod.name, exc)
        requirements = None
    if requirements is not None:
        mod.meta["nexus_requirements"] = [r.to_dict() for r in requirements]
    mod.save_meta()


def identify(client: NexusClient, game: str, mod: Mod) -> bool:
    """Find a mod on Nexus by the MD5 of the archive it was installed from."""
    source = Path(mod.meta.get("source", ""))
    if not source.is_file():
        raise NexusError("The archive this mod was installed from is no longer available.")
    matches = client.md5_search(game, md5_file(source))
    if not matches:
        return False
    size_kb = source.stat().st_size / 1024
    best = min(matches, key=lambda m: abs((m.get("file_details") or {}).get("size_kb", 0) - size_kb))
    details, info = best.get("file_details") or {}, best.get("mod") or {}
    mod.meta.update({
        "nexus_id": int(info.get("mod_id") or details.get("mod_id")),
        "nexus_file_id": int(details["file_id"]) if details.get("file_id") else None,
        "nexus_game": game,
    })
    if details.get("version") and not mod.meta.get("version"):
        mod.meta["version"] = details["version"]
    mod.meta.pop("nexus_checked", None)
    mod.save_meta()
    return True
