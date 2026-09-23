"""Nexus Mods API client, nxm:// links and API key storage.

Uses the public v1 REST API (mod and file info, download links, update feeds)
and the v2 GraphQL API (mod requirements), authenticated with the user's
personal API key (Nexus Mods account settings, "API Keys").
Only the standard library is used, so it works the same in the AppImage.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from modmanager import APP_NAME, __version__
from modmanager.core import paths

log = logging.getLogger(__name__)

API_URL = "https://api.nexusmods.com"
SITE_URL = "https://www.nexusmods.com"
API_KEY_PAGE = f"{SITE_URL}/users/myaccount?tab=api"
CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",   # Arch, Debian, Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",     # Fedora
    "/etc/ssl/cert.pem",
    "/etc/ssl/ca-bundle.pem",               # openSUSE
)


class NexusError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ nxm links


@dataclass
class NxmLink:
    game: str
    mod_id: int
    file_id: int
    key: str | None = None
    expires: int | None = None
    user_id: int | None = None

    @classmethod
    def parse(cls, url: str) -> "NxmLink":
        """Parse ``nxm://<game>/mods/<mod>/files/<file>?key=…&expires=…&user_id=…``."""
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme.lower() != "nxm":
            raise ValueError(f"Not an nxm link: {url}")
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) != 4 or parts[0] != "mods" or parts[2] != "files":
            raise ValueError(f"Unsupported nxm link: {url}")
        query = urllib.parse.parse_qs(parsed.query)

        def first(name: str) -> str | None:
            return query.get(name, [None])[0]

        return cls(
            game=parsed.netloc.lower(),
            mod_id=int(parts[1]),
            file_id=int(parts[3]),
            key=first("key"),
            expires=int(first("expires")) if first("expires") else None,
            user_id=int(first("user_id")) if first("user_id") else None,
        )


def mod_url(game: str, mod_id: int) -> str:
    return f"{SITE_URL}/{game}/mods/{mod_id}"


# ---------------------------------------------------------------- API key


def key_file() -> Path:
    return paths.config_home() / "nexus.json"


def load_api_key() -> str:
    env = os.environ.get("NEXUS_API_KEY")
    if env:
        return env.strip()
    data = paths.read_json(key_file(), {}) or {}
    return str(data.get("api_key", "")).strip()


def save_api_key(key: str) -> None:
    path = key_file()
    data = paths.read_json(path, {}) or {}
    data["api_key"] = key.strip()
    paths.write_json(path, data)
    os.chmod(path, 0o600)


# ------------------------------------------------------------------- client


def ssl_context() -> ssl.SSLContext:
    """Default context, falling back to well-known CA bundles (for bundled Pythons)."""
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca", 0) == 0:
        for bundle in CA_BUNDLES:
            if os.path.isfile(bundle):
                ctx.load_verify_locations(bundle)
                break
    return ctx


@dataclass
class Requirement:
    mod_id: int | None
    name: str
    url: str
    game: str
    external: bool
    notes: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class NexusClient:
    def __init__(self, api_key: str, base_url: str | None = None, timeout: float = 30):
        self.api_key = api_key
        # MODMANAGER_NEXUS_API points the client at another server (for testing).
        base_url = base_url or os.environ.get("MODMANAGER_NEXUS_API") or API_URL
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.rate_limit: tuple[int, int] | None = None  # (daily, hourly) remaining
        self._ctx = ssl_context() if self.base_url.startswith("https") else None

    # --- plumbing

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.api_key,
            "Application-Name": APP_NAME.replace(" ", ""),
            "Application-Version": __version__,
            "User-Agent": f"{APP_NAME.replace(' ', '')}/{__version__} (Linux)",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, body: dict | None = None):
        if not self.api_key:
            raise NexusError("No Nexus Mods API key configured (Settings › Nexus).")
        url = path if path.startswith("http") else self.base_url + path
        data = json.dumps(body).encode() if body is not None else None
        headers = self._headers()
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                daily = resp.headers.get("x-rl-daily-remaining")
                hourly = resp.headers.get("x-rl-hourly-remaining")
                if daily is not None and hourly is not None:
                    self.rate_limit = (int(daily), int(hourly))
                return json.loads(resp.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8", "replace")).get("message", "")
            except (ValueError, AttributeError):
                pass
            messages = {
                401: "The Nexus Mods API key was rejected. Check it in Settings › Nexus.",
                403: detail or "Nexus Mods refused the request (a premium account or a download "
                               "key from the website may be required).",
                404: detail or "Not found on Nexus Mods.",
                429: "Nexus Mods API rate limit reached; try again later.",
            }
            raise NexusError(messages.get(exc.code, detail or f"HTTP {exc.code}"), exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise NexusError(f"Cannot reach Nexus Mods: {reason}") from None

    def _get(self, path: str):
        return self._request("GET", "/v1" + path)

    def graphql(self, query: str, variables: dict) -> dict:
        res = self._request("POST", "/v2/graphql", {"query": query, "variables": variables})
        if not isinstance(res, dict):
            raise NexusError("Unexpected GraphQL response")
        if res.get("errors") and not res.get("data"):
            raise NexusError("; ".join(e.get("message", "?") for e in res["errors"]))
        return res.get("data") or {}

    # --- v1

    def validate(self) -> dict:
        return self._get("/users/validate")

    def mod_info(self, game: str, mod_id: int) -> dict:
        return self._get(f"/games/{game}/mods/{mod_id}")

    def mod_files(self, game: str, mod_id: int) -> dict:
        return self._get(f"/games/{game}/mods/{mod_id}/files")

    def file_info(self, game: str, mod_id: int, file_id: int) -> dict:
        return self._get(f"/games/{game}/mods/{mod_id}/files/{file_id}")

    def download_links(self, game: str, mod_id: int, file_id: int,
                       key: str | None = None, expires: int | None = None) -> list[dict]:
        path = f"/games/{game}/mods/{mod_id}/files/{file_id}/download_link"
        if key and expires:
            path += "?" + urllib.parse.urlencode({"key": key, "expires": expires})
        res = self._get(path)
        return res if isinstance(res, list) else []

    def md5_search(self, game: str, md5: str) -> list[dict]:
        try:
            res = self._get(f"/games/{game}/mods/md5_search/{md5}")
        except NexusError as exc:
            if exc.status == 404:
                return []
            raise
        return res if isinstance(res, list) else []

    def updated(self, game: str, period: str = "1m") -> list[dict]:
        res = self._get(f"/games/{game}/mods/updated?period={period}")
        return res if isinstance(res, list) else []

    # --- v2

    # Requirements are on the GraphQL Mod type, looked up by a 64-bit "uid":
    # the numeric game id in the high 32 bits, the mod id in the low 32 bits.
    REQUIREMENTS_QUERY = """query modsByUid($uids: [ID!]!, $count: Int) {
  modsByUid(uids: $uids, count: $count) {
    nodes {
      modId
      modRequirements { nexusRequirements { nodes { modId modName url gameId notes externalRequirement } } }
    }
  }
}"""

    def requirements(self, game: str, mod_id: int, info: dict | None = None) -> list[Requirement] | None:
        """Requirements listed on the mod page, or None if Nexus doesn't provide them.

        Tries the ``requirements`` field of v1 mod info first (free when ``info`` was
        fetched anyway), then GraphQL. A query shape the live API rejects is not
        retried for the rest of the session.
        """
        if info and isinstance(info.get("requirements"), dict):
            return _parse_requirements(info["requirements"], game)
        global _graphql_requirements
        if _graphql_requirements is False:
            return None
        game_id = (info or {}).get("game_id") or GAME_IDS.get(game)
        if not game_id:
            return None
        uid = str((int(game_id) << 32) | int(mod_id))
        try:
            data = self.graphql(self.REQUIREMENTS_QUERY, {"uids": [uid], "count": 1})
        except NexusError as exc:
            if _is_schema_error(str(exc)):
                _graphql_requirements = False
                log.info("Nexus Mods does not offer mod requirements through its API (%s); "
                         "requirement checks are skipped for now.", exc)
                return None
            raise
        _graphql_requirements = True
        nodes = ((data.get("modsByUid") or {}).get("nodes")) or []
        node = next((n for n in nodes if str(n.get("modId")) == str(mod_id)), nodes[0] if nodes else None)
        if node is None:
            return None
        return _parse_requirements(node.get("modRequirements") or {}, game)

    # --- downloads

    def download(self, url: str, dest: Path, progress: Callable[[int, int], None] | None = None,
                 cancelled: Callable[[], bool] | None = None) -> Path:
        """Stream ``url`` to ``dest`` through a ``.part`` file."""
        part = dest.with_name(dest.name + ".part")
        req = urllib.request.Request(url, headers={"User-Agent": self._headers()["User-Agent"]})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp, open(part, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                last = 0.0
                while True:
                    if cancelled and cancelled():
                        raise NexusError("Download cancelled")
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if progress and now - last > 0.2:
                        progress(done, total)
                        last = now
            if total and done != total:
                raise NexusError(f"Download incomplete ({done} of {total} bytes)")
            os.replace(part, dest)
            if progress:
                progress(done, total or done)
            return dest
        except urllib.error.URLError as exc:
            part.unlink(missing_ok=True)
            raise NexusError(f"Download failed: {getattr(exc, 'reason', exc)}") from None
        except BaseException:
            part.unlink(missing_ok=True)
            raise


# Numeric Nexus game ids (the v1 mod info's "game_id" is preferred when available).
GAME_IDS = {"skyrimspecialedition": 1704, "skyrim": 110, "fallout4": 1151}
_graphql_requirements: bool | None = None  # None: untried, False: rejected by the live API


def _is_schema_error(message: str) -> bool:
    m = message.lower()
    return any(s in m for s in ("doesn't exist on type", "cannot query field", "unknown argument",
                                "is declared by", "unknown type", "not defined by type"))


def _parse_requirements(data: dict, game: str) -> list[Requirement]:
    nodes = ((data.get("nexusRequirements") or {}).get("nodes")) or []
    result = []
    for n in nodes:
        try:
            rid = int(n.get("modId")) if n.get("modId") not in (None, "") else None
        except (TypeError, ValueError):
            rid = None
        result.append(Requirement(
            mod_id=rid, name=n.get("modName") or "", url=n.get("url") or "",
            game=n.get("gameId") or game, external=bool(n.get("externalRequirement")),
            notes=n.get("notes") or "",
        ))
    return result


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------ download meta


def sidecar(archive: Path) -> Path:
    """Metadata written next to a downloaded archive (like MO2's .meta files)."""
    return archive.with_name(archive.name + ".meta.json")


def read_sidecar(archive: Path) -> dict:
    return paths.read_json(sidecar(archive), {}) or {}


def unique_download_path(folder: Path, file_name: str) -> Path:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(file_name).name).strip() or "download"
    dest = folder / name
    stem, suffix = dest.stem, dest.suffix
    n = 2
    while dest.exists() or dest.with_name(dest.name + ".part").exists():
        dest = folder / f"{stem} ({n}){suffix}"
        n += 1
    return dest
