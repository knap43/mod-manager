import io
import json
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from modmanager.core import desktop, ipc, nexus, nexus_tasks
from modmanager.core.installer import Installer, guess_info
from modmanager.core.modlist import ModList

GAME = "skyrimspecialedition"
KEY = "secret-key"


def make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Cool Mod/Data/textures/cool.dds", "c")
    return buf.getvalue()


ZIP = make_zip()


class FakeNexus(BaseHTTPRequestHandler):
    calls: list[str] = []

    def log_message(self, *args):
        pass

    def _send(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("x-rl-daily-remaining", "2400")
        self.send_header("x-rl-hourly-remaining", "99")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        FakeNexus.calls.append(self.path)
        if self.path == "/cdn/cool.zip":
            return self._send(200, ZIP, "application/zip")
        if self.headers.get("apikey") != KEY:
            return self._send(401, {"message": "Please provide a valid API Key"})
        base = f"/v1/games/{GAME}/mods"
        routes = {
            "/v1/users/validate": {"name": "tester", "is_premium": False, "user_id": 1},
            f"{base}/100": {"name": "Cool Mod", "version": "1.3", "mod_id": 100, "game_id": 1704},
            f"{base}/101": {"name": "Other", "version": "2.0", "mod_id": 101, "game_id": 1704, "requirements": {
                "nexusRequirements": {"nodes": [{"modId": "7", "modName": "Seven", "url": "u", "gameId": GAME,
                                                 "externalRequirement": False}]}}},
            f"{base}/100/files/200": {"file_name": "Cool Mod-100-1-2-1700000000.zip", "version": "1.2",
                                      "name": "Main file", "category_name": "MAIN"},
            f"{base}/100/files/200/download_link?key=K&expires=9": [
                {"name": "Broken", "URI": self.base + "/cdn/missing.zip"},
                {"name": "CDN", "URI": self.base + "/cdn/cool.zip"},
            ],
            f"{base}/100/files": {
                "files": [{"file_id": 200, "version": "1.2", "category_name": "OLD_VERSION"},
                          {"file_id": 201, "version": "1.3", "category_name": "MAIN"}],
                "file_updates": [{"old_file_id": 200, "new_file_id": 201}],
            },
            f"{base}/updated?period=1m": [{"mod_id": 100, "latest_file_update": int(time.time()) + 10}],
        }
        if self.path in routes:
            return self._send(200, routes[self.path])
        if self.path.startswith(f"{base}/md5_search/"):
            return self._send(200, [{"mod": {"mod_id": 100}, "file_details": {"file_id": 200, "size_kb": 1, "version": "1.2"}}])
        if self.path.endswith("/download_link"):
            return self._send(403, {"message": "You don't have permission to get download links from the API without visting nexusmods.com - this is for premium users only."})
        self._send(404, {"message": "Not found"})

    graphql_mode = "uid"   # "uid": modsByUid works; "none": the live API rejects it

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeNexus.calls.append("graphql:" + str(body["variables"]))
        query = body["query"]
        if "modRequirements(modId:" in query or FakeNexus.graphql_mode == "none":
            # What the production API answers for fields it doesn't have.
            return self._send(200, {"data": None, "errors": [
                {"message": "Field 'modRequirements' doesn't exist on type 'Query'"},
                {"message": "Variable $modId is declared by modRequirements but not used"},
            ]})
        assert "modsByUid(uids: $uids" in query
        assert body["variables"]["uids"] == [str((1704 << 32) | 100)]
        nodes = [
            {"modId": "300", "modName": "Some Framework", "url": f"https://www.nexusmods.com/{GAME}/mods/300",
             "gameId": GAME, "notes": "", "externalRequirement": False},
        ]
        self._send(200, {"data": {"modsByUid": {"nodes": [
            {"modId": 100, "modRequirements": {"nexusRequirements": {"nodes": nodes}}},
        ]}}})


@pytest.fixture(autouse=True)
def fresh_session():
    nexus._graphql_requirements = None
    FakeNexus.graphql_mode = "uid"
    yield


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeNexus)
    FakeNexus.base = f"http://127.0.0.1:{srv.server_port}"
    FakeNexus.calls = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield FakeNexus.base
    srv.shutdown()


def test_nxm_parse():
    link = nexus.NxmLink.parse("nxm://SkyrimSpecialEdition/mods/12604/files/35407?key=abc&expires=1700000000&user_id=5")
    assert (link.game, link.mod_id, link.file_id, link.key, link.expires, link.user_id) == (
        GAME, 12604, 35407, "abc", 1700000000, 5)
    with pytest.raises(ValueError):
        nexus.NxmLink.parse("https://example.com")


def test_bad_key(server):
    with pytest.raises(nexus.NexusError) as err:
        nexus.NexusClient("wrong", server).validate()
    assert err.value.status == 401


def test_download_install_and_check(env, server, tmp_path):
    client = nexus.NexusClient(KEY, server)
    assert client.validate()["name"] == "tester"
    link = nexus.NxmLink.parse(f"nxm://{GAME}/mods/100/files/200?key=K&expires=9")
    seen = []
    archive = nexus_tasks.download_nxm(client, link, env.downloads_dir, lambda d, t: seen.append((d, t)))
    assert archive.name == "Cool Mod-100-1-2-1700000000.zip" and archive.read_bytes() == ZIP
    assert seen[-1] == (len(ZIP), len(ZIP))
    assert client.rate_limit == (2400, 99)
    meta = nexus.read_sidecar(archive)
    assert (meta["mod_name"], meta["version"], meta["file_id"]) == ("Cool Mod", "1.2", 200)

    info = guess_info(archive)
    assert (info.name, info.version, info.nexus_id, info.file_id) == ("Cool Mod", "1.2", 100, 200)
    installer = Installer(env.mods_dir, env.tmp_dir, env.game)
    staging = installer.extract(archive)
    name = installer.install(staging / "Cool Mod" / "Data", info.name, source=archive, info=info)
    ml = ModList(env)
    mod = ml.get(name)
    assert (mod.meta["nexus_id"], mod.meta["nexus_file_id"], mod.meta["nexus_game"]) == (100, 200, GAME)

    result = nexus_tasks.check_mods(client, GAME, ml.mods)
    assert result.checked == 1 and result.updates == ["Cool Mod"]
    mod = ModList(env).get(name)
    assert mod.meta["nexus_latest_version"] == "1.3" and mod.meta["nexus_update"] is True
    assert mod.meta["nexus_requirements"][0]["mod_id"] == 300

    # A second check within a month only asks Nexus for mods listed as updated since.
    FakeNexus.calls = []
    result = nexus_tasks.check_mods(client, GAME, ModList(env).mods)
    assert FakeNexus.calls[0].endswith("/updated?period=1m") and result.checked == 1


def test_premium_only_download_is_explained(server, tmp_path):
    client = nexus.NexusClient(KEY, server)
    with pytest.raises(nexus.NexusError) as err:
        client.download_links(GAME, 100, 999)
    assert err.value.status == 403 and "premium" in str(err.value)


def test_identify_by_md5(env, server):
    archive = env.downloads_dir / "renamed.zip"
    archive.write_bytes(ZIP)
    root = env.mods_dir / "Mystery"
    root.mkdir()
    ml = ModList(env)
    mod = ml.get("Mystery")
    mod.meta["source"] = str(archive)
    assert nexus_tasks.identify(nexus.NexusClient(KEY, server), GAME, mod)
    assert ModList(env).get("Mystery").meta["nexus_id"] == 100


def test_ipc_roundtrip(tmp_path):
    got = []
    path = tmp_path / "mm.sock"
    listener = ipc.Listener(got.append, path)
    assert listener.start()
    assert not ipc.Listener(got.append, path).start()  # second instance defers
    assert ipc.send("nxm://skyrimspecialedition/mods/1/files/2", path)
    for _ in range(50):
        if got:
            break
        time.sleep(0.02)
    assert got == ["nxm://skyrimspecialedition/mods/1/files/2"]
    listener.stop()
    assert not ipc.send("x", path)


def test_desktop_entry_quoting():
    entry = desktop.desktop_entry(["/home/me/My Apps/ModManager.AppImage"])
    assert 'Exec="/home/me/My Apps/ModManager.AppImage" %u' in entry
    assert "MimeType=x-scheme-handler/nxm;" in entry


def test_api_key_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)
    nexus.save_api_key(" abc ")
    assert nexus.load_api_key() == "abc"
    assert (nexus.key_file().stat().st_mode & 0o777) == 0o600


def test_requirements_unavailable_do_not_break_update_check(env, server):
    FakeNexus.graphql_mode = "none"
    client = nexus.NexusClient(KEY, server)
    for name in ("A", "B"):
        (env.mods_dir / name).mkdir()
    ml = ModList(env)
    for name in ("A", "B"):
        ml.get(name).meta.update(nexus_id=100, version="1.2")
    result = nexus_tasks.check_mods(client, GAME, ml.mods)
    assert result.checked == 2 and not result.errors and len(result.updates) == 2
    assert "nexus_requirements" not in ModList(env).get("A").meta
    assert sum(1 for c in FakeNexus.calls if c.startswith("graphql:")) == 1  # not retried


def test_requirements_from_v1_mod_info(env, server):
    client = nexus.NexusClient(KEY, server)
    info = client.mod_info(GAME, 101)
    reqs = client.requirements(GAME, 101, info)
    assert [(r.mod_id, r.name) for r in reqs] == [(7, "Seven")]
    assert not any(c.startswith("graphql:") for c in FakeNexus.calls)
