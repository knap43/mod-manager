"""Ties an instance, its mod list, the deployer and Proton together."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from modmanager.core import fomod, paths, proton, script_extender
from modmanager.core.deploy import Deployer, DeployResult, ProgressFn
from modmanager.core.instance import Executable, Instance
from modmanager.core.modlist import ModList
from modmanager.core.plugins import PluginEntry, is_plugin_name

log = logging.getLogger(__name__)

# Skyrim LE decides load order by file modification time.
TIMESTAMP_BASE = 1_200_000_000


class Manager:
    def __init__(self, instance: Instance):
        self.instance = instance
        self.modlist = ModList(instance)
        self.deployer = self._make_deployer()

    def _make_deployer(self) -> Deployer:
        inst = self.instance
        deployer = Deployer(inst.data_dir, inst.deploy_dir, inst.deploy_method)
        old = deployer.state.get("data_dir")
        if old and Path(old) != inst.data_dir and deployer.is_deployed:
            log.warning("Game folder changed; removing mods from the previous location %s", old)
            Deployer(Path(old), inst.deploy_dir, inst.deploy_method).purge()
            deployer = Deployer(inst.data_dir, inst.deploy_dir, inst.deploy_method)
        return deployer

    def reload(self) -> None:
        """Re-read settings after they were edited (game folder, method, profile)."""
        self.modlist = ModList(self.instance)
        self.deployer = self._make_deployer()

    # ---------------------------------------------------------------- plugins

    def plugins(self) -> list[PluginEntry]:
        return self.modlist.plugin_entries(self.deployer.untracked_root_files())

    # ------------------------------------------------------------- deployment

    def needs_deploy(self) -> bool:
        plan = self.modlist.deployment_plan()
        files = self.deployer.files
        if plan.keys() != files.keys():
            return True
        return any(files[k]["source"] != str(src) for k, (_rel, src) in plan.items())

    def deploy(self, progress: ProgressFn | None = None) -> DeployResult:
        for mod in self.modlist.active_sources():
            mod.invalidate()
        self.modlist.update_conflicts()
        result = self.deployer.deploy(self.modlist.deployment_plan(), progress)
        self.write_game_plugin_files()
        log.info("Deployed to %s: %s", self.instance.data_dir, result.summary())
        return result

    def purge(self, progress: ProgressFn | None = None) -> DeployResult:
        result = self.deployer.purge(progress)
        log.info("Purged %s: %s", self.instance.data_dir, result.summary())
        return result

    def change_deploy_method(self, method: str) -> None:
        """Switching methods requires relinking everything, so purge first."""
        if method == self.instance.deploy_method:
            return
        was_deployed = self.deployer.is_deployed
        if was_deployed:
            self.purge()
        self.instance.config["deploy_method"] = method
        self.instance.save()
        self.deployer = self._make_deployer()
        if was_deployed:
            self.deploy()

    def plugin_file_dir(self) -> Path | None:
        game = self.instance.game
        prefix = self.instance.proton.get("prefix")
        folder = game.local_folder(self.instance.game_dir)
        if not (game.has_plugins and folder and prefix):
            return None
        return proton.appdata_local(Path(prefix).expanduser()) / folder

    def write_game_plugin_files(self) -> None:
        """Write plugins.txt/loadorder.txt where the game reads them inside the prefix."""
        target = self.plugin_file_dir()
        if target is None:
            return
        game = self.instance.game
        entries = self.plugins()
        if game.plugin_format == "asterisk":
            lines = ["# This file is used by the game to keep track of your downloaded content.",
                     "# Please do not modify this file."]
            lines += [("*" if e.enabled else "") + e.name for e in entries if not e.implicit]
        else:
            lines = [e.name for e in entries if e.enabled]
            self._apply_timestamps(entries)
        paths.write_text(target / "plugins.txt", "\r\n".join(lines) + "\r\n", encoding="cp1252", newline="")
        paths.write_text(
            target / "loadorder.txt", "\r\n".join(e.name for e in entries) + "\r\n",
            encoding="cp1252", newline="",
        )
        active = sum(1 for e in entries if e.enabled and not e.implicit)
        log.info("Wrote %d active plugin(s) to %s", active, target / "plugins.txt")

    def _apply_timestamps(self, entries: list[PluginEntry]) -> None:
        data = self.instance.data_dir
        for i, e in enumerate(entries):
            path = data / e.name
            try:
                stamp = TIMESTAMP_BASE + i * 60
                os.utime(path, (stamp, stamp))
            except OSError:
                pass

    # ----------------------------------------------------------------- fomod

    def fomod_context(self) -> fomod.FomodContext:
        """What FOMOD conditions see: files from enabled mods and the base game, active plugins."""
        tracked = set(self.deployer.files)
        present = {k for k in self.snapshot_data() if k not in tracked}
        present |= set(self.modlist.deployment_plan())
        active = {e.name.lower() for e in self.plugins() if e.enabled}

        def state(path: str) -> str:
            key = path.replace("\\", "/").strip("/").lower()
            if key in active:
                return "Active"
            if key in present:
                return "Inactive" if is_plugin_name(key) else "Active"
            return "Missing"

        se = self.script_extender_status(refresh=True)
        binary = self.instance.game.binary
        exe = paths.find_child_ci(self.instance.game_dir, binary) if binary else None
        version = se.game_version if se else (fomod.pe_file_version(exe) if exe else None)
        return fomod.FomodContext(file_state=state, game_version=version,
                                  script_extender_version=se.version if se and se.installed else None)

    # -------------------------------------------------------- script extender

    def script_extender_status(self, refresh: bool = False) -> script_extender.ScriptExtenderStatus | None:
        if refresh or not hasattr(self, "_se_status"):
            self._se_status = script_extender.check(self.instance.game, self.instance.game_dir)
        return self._se_status

    def ensure_script_extender_executable(self) -> bool:
        """Add the script extender loader to the executables once, selected by default."""
        status = self.script_extender_status()
        if status is None or not status.installed:
            return False
        exes = self.instance.executables
        loader = str(status.loader)
        if any(Path(e.path) == status.loader for e in exes) or self.instance.config.get("se_added") == loader:
            return False
        exes.insert(0, Executable(status.extender.name, loader))
        self.instance.executables = exes
        self.instance.config["selected_executable"] = 0
        self.instance.config["se_added"] = loader  # Don't re-add it if the user removes it.
        self.instance.save()
        log.info("Added %s to the executables", status.extender.name)
        return True

    def enabled_script_extender_plugins(self) -> list[str]:
        se = self.instance.game.script_extender
        if se is None:
            return []
        return script_extender.plugin_dlls(set(self.modlist.deployment_plan()), se)

    # ---------------------------------------------------------------- running

    def launch_spec(self, exe: Executable) -> proton.LaunchSpec:
        extra: dict[str, str] = {}
        if self.instance.deploy_method != "hardlink" or any(
            e.get("method") == "symlink" for e in self.deployer.files.values()
        ):
            # Symlinks point into the instance, which must be visible inside
            # Steam's runtime container that umu uses.
            extra["PRESSURE_VESSEL_FILESYSTEMS_RW"] = ":".join(
                p for p in (os.environ.get("PRESSURE_VESSEL_FILESYSTEMS_RW", ""), str(self.instance.path)) if p
            )
        return proton.build_launch(
            self.instance.proton, exe.path, exe.args, exe.cwd, exe.use_proton, extra_env=extra
        )

    def snapshot_data(self) -> set[str]:
        return self.deployer.snapshot() if self.instance.data_dir.is_dir() else set()

    def capture_new_files(self, before: set[str]) -> list[str]:
        """Move files the game created in Data into Overwrite, then redeploy them."""
        if not self.instance.data_dir.is_dir():
            return []
        moved = self.deployer.capture_new_files(before, self.instance.overwrite_dir)
        if moved:
            log.info("Moved %d new file(s) from the game folder into Overwrite", len(moved))
            self.modlist.overwrite.invalidate()
            self.deploy()
        return moved
