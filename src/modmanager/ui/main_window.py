"""The main window: mod list on the left, plugins and downloads on the right, log below."""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QModelIndex, QObject, QProcess, QProcessEnvironment, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QKeySequence, QTextCharFormat
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSizePolicy, QSplitter, QTabWidget, QToolButton, QTreeView, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from modmanager import APP_NAME, __version__
from modmanager.core import archives, checks, fomod, nexus, nexus_tasks, proton
from modmanager.core.games import detect_store
from modmanager.core.installer import Installer, detect_data_root, guess_info
from modmanager.core.instance import Executable, Instance, InstanceRegistry
from modmanager.core.manager import Manager
from modmanager.core.modlist import ModList
from modmanager.ui import tasks, theme
from modmanager.ui.dialogs import (
    ConflictsDialog, ExecutablesDialog, InstallDialog, InstancePicker, SettingsDialog,
)
from modmanager.ui.fomod_dialog import FomodDialog
from modmanager.ui.models import ModListModel, PluginListModel

log = logging.getLogger(__name__)
LOG_COLORS = {logging.DEBUG: theme.TEXT_DIM, logging.WARNING: theme.WARN, logging.ERROR: theme.BAD}


class LogEmitter(QObject):
    message = Signal(str, int)


class TextEmitter(QObject):
    text = Signal(str)


class NxmRouter(QObject):
    """Delivers nxm:// links (from the command line or later launches) to the open window."""

    link = Signal(str)


nxm_router: NxmRouter | None = None


class QtLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.emitter = LogEmitter()
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.emitter.message.emit(self.format(record), record.levelno)
        except RuntimeError:
            pass  # Widget already destroyed during shutdown.


def open_path(path: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


class MainWindow(QMainWindow):
    def __init__(self, instance: Instance, registry: InstanceRegistry, log_handler: QtLogHandler):
        super().__init__()
        self.registry = registry
        self.manager = Manager(instance)
        self.log_handler = log_handler
        self.process: QProcess | None = None
        self._run_log = None
        self._run_started = 0.0
        self._text_emitter = TextEmitter()
        self._text_emitter.text.connect(lambda t: self.busy_label.setText(t) if self.busy else None)
        self.downloads_active: dict[str, dict] = {}
        self.nexus_checking = False
        if nxm_router is not None:
            nxm_router.link.connect(self.handle_nxm)
        self.busy = False
        self.dirty = False
        self.settings = QSettings("modmanager", "modmanager")
        self.setWindowTitle(f"{APP_NAME} — {instance.name} ({instance.game.name})")
        self.resize(1400, 860)
        self.setAcceptDrops(True)

        self._build_actions()
        self._build_menus()
        self._build_central()
        self._build_status()
        log_handler.emitter.message.connect(self._append_log)

        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        for key, splitter in (("split_h", self.h_split), ("split_v", self.v_split)):
            state = self.settings.value(key)
            if state is not None:
                splitter.restoreState(state)

        self.refresh_profiles()
        self.refresh_executables()
        self.refresh_all(rescan=False)
        self.dirty = self.manager.needs_deploy()
        self._update_status()
        self._startup_checks()
        self.refresh_script_extender()
        if self.manager.ensure_script_extender_executable():
            self.refresh_executables()

    @property
    def instance(self) -> Instance:
        return self.manager.instance

    @property
    def modlist(self) -> ModList:
        return self.manager.modlist

    # ================================================================== layout

    def _build_actions(self) -> None:
        def act(text, slot, shortcut=None, tip=None):
            a = QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            if tip:
                a.setStatusTip(tip)
            return a

        self.a_install = act("Install from archive…", self.install_archive_dialog, "Ctrl+O")
        self.a_install_dir = act("Install from folder…", self.install_folder_dialog)
        self.a_separator = act("Create separator…", self.create_separator)
        self.a_refresh = act("Refresh", lambda: self.refresh_all(rescan=True), "F5")
        self.a_deploy = act("Deploy", self.deploy, "Ctrl+D", "Link enabled mods into the game folder")
        self.a_purge = act("Purge", self.purge, None, "Remove all mods from the game folder")
        self.a_exes = act("Executables…", self.edit_executables)
        self.a_settings = act("Settings…", self.edit_settings, "Ctrl+,")
        self.a_instances = act("Instances…", self.switch_instance)
        self.a_quit = act("Quit", self.close, "Ctrl+Q")

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&File")
        m.addActions([self.a_install, self.a_install_dir, self.a_separator])
        m.addSeparator()
        m.addAction(self.a_instances)
        m.addAction("Open instance folder", lambda: open_path(self.instance.path))
        m.addAction("Open mods folder", lambda: open_path(self.instance.mods_dir))
        m.addAction("Open downloads folder", lambda: open_path(self.instance.downloads_dir))
        m.addSeparator()
        m.addAction(self.a_quit)

        m = mb.addMenu("&Game")
        m.addActions([self.a_deploy, self.a_purge, self.a_refresh])
        m.addSeparator()
        m.addActions([self.a_exes, self.a_settings])
        m.addSeparator()
        m.addAction("Open game folder", lambda: open_path(self.instance.game_dir))
        m.addAction("Open Data folder", lambda: open_path(self.instance.data_dir))
        if self.instance.game.loot_repo:
            m.addSeparator()
            m.addAction("Sort plugins (LOOT)", self.loot_sort)
            m.addAction("Update LOOT masterlist", self.loot_update)
            m.addAction("Edit LOOT userlist…", self.loot_userlist)

        m = mb.addMenu("&Proton")
        m.addAction("Wine configuration (winecfg)", lambda: self.run_tool("winecfg"))
        m.addAction("Winetricks…", self.run_winetricks)
        m.addSeparator()
        m.addAction("Open Wine prefix folder", self.open_prefix)
        m.addAction("Open plugins.txt folder", self.open_plugin_dir)
        m.addAction("Open My Games folder", self.open_my_games)

        m = mb.addMenu("&Nexus")
        domain = self.instance.game.nexus_domain
        m.setEnabled(bool(domain))
        m.addAction("Check for updates", lambda: self.nexus_check(force=False))
        m.addAction("Check every mod again", lambda: self.nexus_check(force=True))
        m.addSeparator()
        if domain:
            m.addAction("Open game page on Nexus Mods",
                        lambda: QDesktopServices.openUrl(QUrl(f"{nexus.SITE_URL}/{domain}")))
        m.addAction("Nexus settings…", self.edit_settings)

        m = mb.addMenu("&Help")
        m.addAction("About", self.about)

    def _build_central(self) -> None:
        # ---- left: profile + mod list + filter
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(6, 6, 0, 0)
        top = QHBoxLayout()
        top.addWidget(QLabel("Profile"))
        self.profile_combo = QComboBox()
        self.profile_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.profile_combo.activated.connect(self._profile_selected)
        top.addWidget(self.profile_combo, 1)
        prof_btn = QToolButton()
        prof_btn.setText("Profiles ▾")
        prof_btn.setPopupMode(QToolButton.InstantPopup)
        pm = QMenu(prof_btn)
        pm.addAction("New…", self.new_profile)
        pm.addAction("Copy current…", lambda: self.new_profile(copy=True))
        pm.addAction("Rename…", self.rename_profile)
        pm.addAction("Remove", self.remove_profile)
        prof_btn.setMenu(pm)
        top.addWidget(prof_btn)
        for a in (self.a_install, self.a_refresh):
            b = QToolButton()
            b.setDefaultAction(a)
            top.addWidget(b)
        lv.addLayout(top)

        self.mod_model = ModListModel(self.modlist)
        self.mod_view = self._make_view(self.mod_model)
        self.mod_view.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            self.mod_view.header().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.mod_view.customContextMenuRequested.connect(self._mod_menu)
        self.mod_view.selectionModel().currentRowChanged.connect(self._mod_selected)
        self.mod_view.doubleClicked.connect(lambda idx: self.show_conflicts())
        self.mod_model.changed.connect(self._mods_changed)
        lv.addWidget(self.mod_view, 1)

        self.mod_filter = QLineEdit()
        self.mod_filter.setPlaceholderText("Filter mods")
        self.mod_filter.setClearButtonEnabled(True)
        self.mod_filter.textChanged.connect(self._apply_mod_filter)
        self.mod_count = QLabel()
        self.mod_count.setObjectName("hint")
        fr = QHBoxLayout()
        fr.addWidget(self.mod_filter, 1)
        fr.addWidget(self.mod_count)
        lv.addLayout(fr)

        # ---- right: run bar + tabs
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 6, 6, 0)
        run = QHBoxLayout()
        self.exe_combo = QComboBox()
        self.exe_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.exe_combo.activated.connect(self._exe_selected)
        run.addWidget(self.exe_combo, 1)
        exe_btn = QToolButton()
        exe_btn.setText("Edit")
        exe_btn.clicked.connect(self.edit_executables)
        run.addWidget(exe_btn)
        self.run_btn = QPushButton("Run")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self._run_clicked)
        run.addWidget(self.run_btn)
        rv.addLayout(run)

        self.tabs = QTabWidget()
        # Plugins tab
        ptab = QWidget()
        pv = QVBoxLayout(ptab)
        pv.setContentsMargins(4, 4, 4, 4)
        self.plugin_model = PluginListModel(self.modlist, self.instance.game.supports_light_plugins)
        self.plugin_view = self._make_view(self.plugin_model)
        hdr = self.plugin_view.header()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.Interactive)
        hdr.resizeSection(3, 170)
        self.plugin_view.customContextMenuRequested.connect(self._plugin_menu)
        self.plugin_model.changed.connect(self._plugins_changed)
        pv.addWidget(self.plugin_view, 1)
        self.plugin_filter = QLineEdit()
        self.plugin_filter.setPlaceholderText("Filter plugins")
        self.plugin_filter.setClearButtonEnabled(True)
        self.plugin_filter.textChanged.connect(self._apply_plugin_filter)
        self.plugin_count = QLabel()
        self.plugin_count.setObjectName("hint")
        pr = QHBoxLayout()
        pr.addWidget(self.plugin_filter, 1)
        pr.addWidget(self.plugin_count)
        self.sort_btn = QPushButton("Sort (LOOT)")
        self.sort_btn.setToolTip("Sort the load order with LOOT's rules and masterlist")
        self.sort_btn.clicked.connect(self.loot_sort)
        self.sort_btn.setVisible(bool(self.instance.game.loot_repo))
        pr.addWidget(self.sort_btn)
        pv.addLayout(pr)
        self.tabs.addTab(ptab, "Plugins")
        if not self.instance.game.has_plugins:
            self.tabs.setTabVisible(0, False)

        # Downloads tab
        dtab = QWidget()
        dv = QVBoxLayout(dtab)
        dv.setContentsMargins(4, 4, 4, 4)
        self.downloads = QTreeWidget()
        self.downloads.setRootIsDecorated(False)
        self.downloads.setAlternatingRowColors(True)
        self.downloads.setHeaderLabels(["Archive", "Size", "Status"])
        self.downloads.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.downloads.itemDoubleClicked.connect(self._download_activated)
        self.downloads.setContextMenuPolicy(Qt.CustomContextMenu)
        self.downloads.customContextMenuRequested.connect(self._downloads_menu)
        dv.addWidget(self.downloads, 1)
        drow = QHBoxLayout()
        b = QPushButton("Install")
        b.clicked.connect(self._install_selected_download)
        drow.addWidget(b)
        b = QPushButton("Open folder")
        b.clicked.connect(lambda: open_path(self.instance.downloads_dir))
        drow.addWidget(b)
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh_downloads)
        drow.addWidget(b)
        drow.addStretch(1)
        drow.addWidget(self._hint_label("Drop archives onto the window to install them."))
        dv.addLayout(drow)
        self.tabs.addTab(dtab, "Downloads")

        # Problems tab
        ptab2 = QWidget()
        qv = QVBoxLayout(ptab2)
        qv.setContentsMargins(4, 4, 4, 4)
        self.problem_list = QTreeWidget()
        self.problem_list.setRootIsDecorated(False)
        self.problem_list.setAlternatingRowColors(True)
        self.problem_list.setHeaderLabels(["", "Problem"])
        self.problem_list.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.problem_list.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.problem_list.currentItemChanged.connect(lambda *_: self._problem_selected())
        qv.addWidget(self.problem_list, 3)
        self.problem_detail = QLabel()
        self.problem_detail.setWordWrap(True)
        self.problem_detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.problem_detail.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.problem_detail.setMinimumHeight(70)
        qv.addWidget(self.problem_detail, 1)
        prow = QHBoxLayout()
        self.problem_fix = QPushButton("Fix")
        self.problem_fix.setObjectName("primary")
        self.problem_fix.clicked.connect(self._fix_problem)
        self.problem_url = QPushButton("Open page")
        self.problem_url.clicked.connect(self._open_problem_url)
        self.problem_ignore = QPushButton("Ignore")
        self.problem_ignore.clicked.connect(self._ignore_problem)
        self.show_ignored = QCheckBox("Show ignored")
        self.show_ignored.toggled.connect(lambda _on: self.refresh_problems())
        for w in (self.problem_fix, self.problem_url, self.problem_ignore):
            prow.addWidget(w)
        prow.addStretch(1)
        prow.addWidget(self.show_ignored)
        qv.addLayout(prow)
        self.tabs.addTab(ptab2, "Problems")
        self.problems_tab = ptab2
        self._problems: dict[str, object] = {}
        self._problem_timer = QTimer(self)
        self._problem_timer.setSingleShot(True)
        self._problem_timer.setInterval(250)
        self._problem_timer.timeout.connect(self.refresh_problems)
        rv.addWidget(self.tabs, 1)

        bar = QHBoxLayout()
        for a in (self.a_deploy, self.a_purge):
            b = QToolButton()
            b.setDefaultAction(a)
            bar.addWidget(b)
        bar.addStretch(1)
        for a in (self.a_settings,):
            b = QToolButton()
            b.setDefaultAction(a)
            bar.addWidget(b)
        rv.addLayout(bar)

        self.h_split = QSplitter(Qt.Horizontal)
        self.h_split.addWidget(left)
        self.h_split.addWidget(right)
        self.h_split.setStretchFactor(0, 3)
        self.h_split.setStretchFactor(1, 2)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("log")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setLineWrapMode(QPlainTextEdit.NoWrap)

        self.v_split = QSplitter(Qt.Vertical)
        self.v_split.addWidget(self.h_split)
        self.v_split.addWidget(self.log_view)
        self.v_split.setStretchFactor(0, 5)
        self.v_split.setStretchFactor(1, 1)
        self.v_split.setSizes([680, 150])
        self.setCentralWidget(self.v_split)

    def _hint_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("hint")
        return label

    def _make_view(self, model) -> QTreeView:
        view = QTreeView()
        view.setModel(model)
        view.setRootIsDecorated(False)
        view.setUniformRowHeights(True)
        view.setAlternatingRowColors(True)
        view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        view.setSelectionBehavior(QAbstractItemView.SelectRows)
        view.setDragDropMode(QAbstractItemView.InternalMove)
        view.setDragEnabled(True)
        view.setAcceptDrops(True)
        view.setDropIndicatorShown(True)
        view.setDefaultDropAction(Qt.MoveAction)
        view.setContextMenuPolicy(Qt.CustomContextMenu)
        view.setAllColumnsShowFocus(True)
        view.header().setStretchLastSection(False)
        view.installEventFilter(self)
        return view

    def _build_status(self) -> None:
        sb = self.statusBar()
        self.deploy_label = QLabel()
        self.progress = QProgressBar()
        self.progress.setFixedWidth(220)
        self.progress.hide()
        self.busy_label = QLabel()
        self.se_label = QLabel()
        sb.addWidget(self.deploy_label, 1)
        sb.addPermanentWidget(self.busy_label)
        sb.addPermanentWidget(self.progress)
        sb.addPermanentWidget(self.se_label)

    def refresh_script_extender(self) -> None:
        status = self.manager.script_extender_status(refresh=True)
        if status is None:
            self.se_label.hide()
            return
        ok = status.installed and status.compatible is not False
        color = theme.GOOD if ok else (theme.BAD if status.installed else theme.TEXT_DIM)
        self.se_label.setText(status.summary())
        self.se_label.setStyleSheet(f"color: {color};")
        self.se_label.setToolTip(status.details())
        self.se_label.show()

    # ================================================================ refresh

    def refresh_all(self, rescan: bool = True) -> None:
        if rescan:
            self.modlist.refresh()
        self.mod_model.set_modlist(self.modlist)
        self.refresh_plugins()
        self.refresh_downloads()
        self._apply_mod_filter()

    def refresh_plugins(self) -> None:
        if not self.instance.game.has_plugins:
            return
        try:
            entries = self.manager.plugins()
        except OSError as exc:
            log.error("Could not read plugins: %s", exc)
            entries = []
        self.plugin_model.set_entries(entries, self.modlist)
        self._apply_plugin_filter()
        self.schedule_problems()

    def refresh_downloads(self) -> None:
        self.downloads.clear()
        for key, d in self.downloads_active.items():
            item = QTreeWidgetItem([d["label"], _size(d["total"]), _progress_text(d)])
            item.setData(0, Qt.UserRole, "")
            item.setData(0, Qt.UserRole + 1, key)
            item.setForeground(2, QColor(theme.ACCENT_BORDER))
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            self.downloads.addTopLevelItem(item)
        installed = {str(m.meta.get("source", "")) for m in self.modlist.mods}
        folder = self.instance.downloads_dir
        files = sorted((p for p in folder.iterdir() if p.is_file() and archives.is_archive(p)),
                       key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []
        for p in files:
            size = p.stat().st_size
            item = QTreeWidgetItem([p.name, f"{size / 1048576:.1f} MB", "Installed" if str(p) in installed else ""])
            item.setData(0, Qt.UserRole, str(p))
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            self.downloads.addTopLevelItem(item)

    def refresh_profiles(self) -> None:
        self.profile_combo.clear()
        names = self.instance.profile_names()
        self.profile_combo.addItems(names)
        self.profile_combo.setCurrentText(self.instance.active_profile.name)

    def refresh_executables(self) -> None:
        self.exe_combo.clear()
        for e in self.instance.executables:
            self.exe_combo.addItem(e.name, e.path)
        idx = int(self.instance.config.get("selected_executable", 0))
        if 0 <= idx < self.exe_combo.count():
            self.exe_combo.setCurrentIndex(idx)
        self.run_btn.setEnabled(self.exe_combo.count() > 0 and not self.busy)

    def _update_status(self) -> None:
        enabled = sum(1 for m in self.modlist.mods if m.enabled)
        total = sum(1 for m in self.modlist.mods if not m.is_separator)
        self.mod_count.setText(f"{enabled} / {total} active")
        active_plugins = sum(1 for e in self.plugin_model.entries if e.enabled)
        self.plugin_count.setText(f"{active_plugins} / {len(self.plugin_model.entries)} active")
        if self.process is not None:
            text = "Running — mod list is locked until the program exits"
        elif not self.manager.deployer.is_deployed:
            text = "Not deployed — mods are deployed automatically when you run a program"
        elif self.dirty:
            text = "Changes pending — they are deployed when you run a program, or with Deploy"
        else:
            text = f"Deployed to {self.instance.data_dir}"
        self.deploy_label.setText(text)

    def _startup_checks(self) -> None:
        if not self.instance.data_dir.is_dir():
            log.error("Data folder not found: %s — check Settings.", self.instance.data_dir)
        store = detect_store(self.instance.game_dir)
        if store and self.instance.game.store_folders:
            log.info("Detected the %s edition; plugin list: %s", store.upper() if store == "gog" else store.title(),
                     self.manager.plugin_file_dir())
        umu = proton.find_umu_run(self.instance.proton.get("umu_run", ""))
        if umu:
            log.info("umu-launcher: %s", umu)
        else:
            log.warning("umu-run not found. Install umu-launcher to run Windows programs through Proton.")
        tools = archives.available_tools()
        if not any(t in tools for t in ("7zz", "7z", "7za", "bsdtar")):
            log.warning("No 7-Zip or bsdtar found: only .zip archives can be installed.")

    # ============================================================ mod actions

    def selected_mods(self) -> list[str]:
        rows = sorted({i.row() for i in self.mod_view.selectionModel().selectedRows()})
        return [m.name for r in rows if (m := self.mod_model.mod_at(r)) and not m.is_overwrite]

    def _mods_changed(self) -> None:
        self.dirty = True
        self.refresh_plugins()
        self._update_status()

    def _plugins_changed(self) -> None:
        self.dirty = True
        self._update_status()
        self.schedule_problems()

    def _mod_selected(self, current: QModelIndex, _prev) -> None:
        mod = self.mod_model.mod_at(current.row()) if current.isValid() else None
        self.mod_model.set_highlight(mod.name if mod and (mod.enabled or mod.is_overwrite) else None)

    def _mod_menu(self, pos) -> None:
        names = self.selected_mods()
        idx = self.mod_view.indexAt(pos)
        mod = self.mod_model.mod_at(idx.row()) if idx.isValid() else None
        m = QMenu(self)
        if names:
            m.addAction("Enable", lambda: self.mod_model.set_enabled(names, True))
            m.addAction("Disable", lambda: self.mod_model.set_enabled(names, False))
            m.addSeparator()
            m.addAction("Send to top (lowest priority)", lambda: self.mod_model.move_rows(self._sel_rows(), 0))
            m.addAction("Send to bottom (highest priority)",
                        lambda: self.mod_model.move_rows(self._sel_rows(), len(self.modlist.mods)))
            m.addSeparator()
        if mod is not None:
            if not mod.is_separator:
                m.addAction("Show conflicts…", self.show_conflicts)
            m.addAction("Open folder", lambda: open_path(mod.path))
            domain = self.instance.game.nexus_domain
            if domain and mod.meta.get("nexus_id"):
                game = mod.meta.get("nexus_game", domain)
                m.addAction("Visit on Nexus Mods", lambda: QDesktopServices.openUrl(
                    QUrl(nexus.mod_url(game, int(mod.meta["nexus_id"])))))
                m.addAction("Check for update", lambda: self.nexus_check(force=True, only=[mod.name]))
            elif domain and not mod.is_separator and not mod.is_overwrite and Path(mod.meta.get("source", "")).is_file():
                m.addAction("Identify on Nexus Mods", lambda: self.nexus_identify(mod.name))
            if not mod.is_overwrite:
                m.addAction("Rename…", self.rename_mod)
            elif mod.files():
                m.addAction("Clear Overwrite…", self.clear_overwrite)
        m.addAction("Create separator…", self.create_separator)
        if names:
            m.addSeparator()
            m.addAction(f"Remove {len(names)} mod(s)…", self.remove_mods)
        m.addSeparator()
        m.addAction("Enable all", lambda: self.mod_model.set_enabled([x.name for x in self.modlist.mods], True))
        m.addAction("Disable all", lambda: self.mod_model.set_enabled([x.name for x in self.modlist.mods], False))
        m.exec(self.mod_view.viewport().mapToGlobal(pos))

    def _sel_rows(self) -> list[int]:
        return sorted({i.row() for i in self.mod_view.selectionModel().selectedRows()})

    def show_conflicts(self) -> None:
        idx = self.mod_view.currentIndex()
        mod = self.mod_model.mod_at(idx.row()) if idx.isValid() else None
        if mod is None or mod.is_separator:
            return
        won, lost = self.modlist.conflicting_files(mod.name)
        ConflictsDialog(mod.display_name, won, lost, self).exec()

    def rename_mod(self) -> None:
        idx = self.mod_view.currentIndex()
        mod = self.mod_model.mod_at(idx.row()) if idx.isValid() else None
        if mod is None or mod.is_overwrite:
            return
        new, ok = QInputDialog.getText(self, "Rename", "New name:", text=mod.display_name)
        if ok and new.strip() and new.strip() != mod.display_name:
            try:
                self.modlist.rename(mod.name, new.strip())
            except OSError as exc:
                QMessageBox.warning(self, "Rename", str(exc))
            self.mod_model.reset()
            self._mods_changed()

    def remove_mods(self) -> None:
        names = self.selected_mods()
        if not names:
            return
        shown = "\n".join(names[:15]) + ("\n…" if len(names) > 15 else "")
        if QMessageBox.question(
            self, "Remove mods", f"Delete these mods and their files?\n\n{shown}",
        ) != QMessageBox.Yes:
            return
        self.modlist.remove(names)
        self.mod_model.reset()
        self._mods_changed()
        self._apply_mod_filter()

    def create_separator(self) -> None:
        label, ok = QInputDialog.getText(self, "Separator", "Separator name:")
        if not ok or not label.strip():
            return
        rows = self._sel_rows()
        index = rows[0] if rows else len(self.modlist.mods)
        self.modlist.create_separator(label.strip(), index)
        self.mod_model.reset()
        self._update_status()

    def clear_overwrite(self) -> None:
        if QMessageBox.question(
            self, "Clear Overwrite", "Delete every file in the Overwrite folder?",
        ) != QMessageBox.Yes:
            return
        shutil.rmtree(self.instance.overwrite_dir, ignore_errors=True)
        self.instance.overwrite_dir.mkdir(parents=True, exist_ok=True)
        self.modlist.overwrite.invalidate()
        self.modlist.update_conflicts()
        self.mod_model.reset()
        self._mods_changed()

    def _apply_mod_filter(self) -> None:
        text = self.mod_filter.text().strip().lower()
        for row in range(self.mod_model.rowCount()):
            mod = self.mod_model.mod_at(row)
            hide = bool(text) and mod is not None and text not in mod.display_name.lower()
            self.mod_view.setRowHidden(row, QModelIndex(), hide)
        self.mod_view.setDragEnabled(not text)
        self._update_status()

    # ========================================================= plugin actions

    def _plugin_menu(self, pos) -> None:
        rows = sorted({i.row() for i in self.plugin_view.selectionModel().selectedRows()})
        m = QMenu(self)
        if rows:
            m.addAction("Enable", lambda: self.plugin_model.set_enabled(rows, True))
            m.addAction("Disable", lambda: self.plugin_model.set_enabled(rows, False))
            m.addSeparator()
        every = list(range(len(self.plugin_model.entries)))
        m.addAction("Enable all", lambda: self.plugin_model.set_enabled(every, True))
        m.addAction("Disable all", lambda: self.plugin_model.set_enabled(every, False))
        m.exec(self.plugin_view.viewport().mapToGlobal(pos))

    def _apply_plugin_filter(self) -> None:
        text = self.plugin_filter.text().strip().lower()
        for row, e in enumerate(self.plugin_model.entries):
            self.plugin_view.setRowHidden(row, QModelIndex(), bool(text) and text not in e.name.lower())
        self.plugin_view.setDragEnabled(not text)
        self._update_status()

    # ============================================================ keyboard

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress and obj in (self.mod_view, self.plugin_view) and not self.busy:
            key, mods = event.key(), event.modifiers()
            view = obj
            model = view.model()
            rows = sorted({i.row() for i in view.selectionModel().selectedRows()})
            if key == Qt.Key_Space and rows:
                if view is self.mod_view:
                    names = self.selected_mods()
                    if names:
                        target = not all(self.modlist.get(n).enabled for n in names if self.modlist.get(n))
                        self.mod_model.set_enabled(names, target)
                else:
                    target = not all(self.plugin_model.entries[r].enabled for r in rows)
                    self.plugin_model.set_enabled(rows, target)
                return True
            if key == Qt.Key_Delete and view is self.mod_view:
                self.remove_mods()
                return True
            if key in (Qt.Key_Up, Qt.Key_Down) and mods & Qt.ControlModifier and rows:
                dest = rows[0] - 1 if key == Qt.Key_Up else rows[-1] + 2
                if dest >= 0:
                    model.move_rows(rows, dest)
                    self._reselect(view, [r + (-1 if key == Qt.Key_Up else 1) for r in rows])
                return True
        return super().eventFilter(obj, event)

    def _reselect(self, view: QTreeView, rows: list[int]) -> None:
        sel = view.selectionModel()
        sel.clearSelection()
        model = view.model()
        for r in rows:
            if 0 <= r < model.rowCount():
                sel.select(model.index(r, 0), sel.SelectionFlag.Select | sel.SelectionFlag.Rows)
        if rows and 0 <= rows[0] < model.rowCount():
            sel.setCurrentIndex(model.index(rows[0], 0), sel.SelectionFlag.NoUpdate)

    # ============================================================== profiles

    def _profile_selected(self, _index: int) -> None:
        name = self.profile_combo.currentText()
        if name and name != self.instance.active_profile.name:
            self.instance.set_active_profile(name)
            self.manager.modlist = ModList(self.instance)
            self.refresh_all(rescan=False)
            self.dirty = self.manager.needs_deploy()
            self._update_status()
            log.info("Switched to profile '%s'", name)

    def new_profile(self, copy: bool = False) -> None:
        name, ok = QInputDialog.getText(self, "New profile", "Profile name:")
        if not ok or not name.strip():
            return
        try:
            self.instance.create_profile(name.strip(), self.instance.active_profile.name if copy else None)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Profile", str(exc))
            return
        self.refresh_profiles()
        self.profile_combo.setCurrentText(name.strip())
        self._profile_selected(0)

    def rename_profile(self) -> None:
        old = self.instance.active_profile.name
        new, ok = QInputDialog.getText(self, "Rename profile", "New name:", text=old)
        if ok and new.strip() and new.strip() != old:
            try:
                self.instance.rename_profile(old, new.strip())
            except OSError as exc:
                QMessageBox.warning(self, "Profile", str(exc))
                return
            self.modlist.profile = self.instance.active_profile
            self.refresh_profiles()

    def remove_profile(self) -> None:
        name = self.instance.active_profile.name
        if QMessageBox.question(self, "Remove profile", f"Delete profile '{name}'?") != QMessageBox.Yes:
            return
        try:
            self.instance.remove_profile(name)
        except ValueError as exc:
            QMessageBox.warning(self, "Profile", str(exc))
            return
        self.refresh_profiles()
        self.manager.modlist = ModList(self.instance)
        self.refresh_all(rescan=False)

    # ============================================================ installing

    def install_archive_dialog(self) -> None:
        exts = " ".join(f"*{e}" for e in archives.ARCHIVE_EXTS)
        files, _ = QFileDialog.getOpenFileNames(
            self, "Install mod", str(self.instance.downloads_dir), f"Archives ({exts});;All files (*)"
        )
        for f in files[:1]:
            self.install_archive(Path(f))

    def install_folder_dialog(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Install mod from folder", str(Path.home()))
        if folder:
            self.install_archive(Path(folder))

    def _install_selected_download(self) -> None:
        self._download_activated(self.downloads.currentItem())

    def _download_activated(self, item, _column: int = 0) -> None:
        if item is not None and item.data(0, Qt.UserRole):
            self.install_archive(Path(item.data(0, Qt.UserRole)))

    def _downloads_menu(self, pos) -> None:
        item = self.downloads.itemAt(pos)
        if item is None:
            return
        m = QMenu(self)
        key = item.data(0, Qt.UserRole + 1)
        if key:
            m.addAction("Cancel download", lambda: self.downloads_active.get(key, {}).update(cancel=True))
        else:
            path = Path(item.data(0, Qt.UserRole))
            m.addAction("Install", lambda: self.install_archive(path))
            meta = nexus.read_sidecar(path)
            if meta.get("mod_id"):
                m.addAction("Visit on Nexus Mods", lambda: QDesktopServices.openUrl(
                    QUrl(nexus.mod_url(meta.get("game", ""), int(meta["mod_id"])))))
            m.addAction("Delete archive…", lambda: self._delete_download(path))
        m.exec(self.downloads.viewport().mapToGlobal(pos))

    def _delete_download(self, path: Path) -> None:
        if QMessageBox.question(self, "Delete", f"Delete {path.name}?") == QMessageBox.Yes:
            path.unlink(missing_ok=True)
            nexus.sidecar(path).unlink(missing_ok=True)
            self.refresh_downloads()

    def install_archive(self, source: Path) -> None:
        if self.busy:
            return
        installer = Installer(self.instance.mods_dir, self.instance.tmp_dir, self.instance.game)
        game = self.instance.game

        def work(_progress):
            staging = installer.extract(source)
            root, confident = detect_data_root(staging, game)
            result = {"staging": staging, "root": root, "confident": confident, "fomod": None}
            fomod_root = fomod.find_fomod(staging)
            if fomod_root is not None:
                try:
                    config, finfo = fomod.load(fomod_root)
                    result["fomod"] = (fomod_root, config, finfo, self.manager.fomod_context())
                except (fomod.FomodError, OSError) as exc:
                    log.warning("Cannot use the FOMOD installer of %s (%s); installing manually.", source.name, exc)
            return result

        self.set_busy(f"Extracting {source.name}…")
        tasks.start(work, lambda r: self._install_confirm(installer, source, r), self._task_failed)

    def _previous_install(self, info) -> tuple[str, dict]:
        """Name and FOMOD choices of an installed copy of this mod, for reinstalls and updates."""
        for mod in self.modlist.mods:
            same_nexus = info.nexus_id and mod.meta.get("nexus_id") == info.nexus_id
            if mod.name == info.name or same_nexus:
                return mod.name, mod.meta.get("fomod") or {}
        return info.name, {}

    def _install_confirm(self, installer: Installer, source: Path, result: dict) -> None:
        self.set_busy(None)
        staging = result["staging"]
        info = guess_info(source)
        name, previous = self._previous_install(info)
        existing = [m.name for m in self.modlist.mods]

        if result["fomod"] is not None:
            root, config, finfo, ctx = result["fomod"]
            session = fomod.FomodSession(config, root, ctx, previous)
            proceed = True
            if not session.module_requirements_met():
                needs = "\n".join(f"• {r}" for r in config.dependencies.describe()) or "(unspecified)"
                proceed = QMessageBox.question(
                    self, "Requirements not met",
                    f"This mod's installer says your setup does not meet its requirements:\n\n{needs}\n\n"
                    "Install anyway?",
                ) == QMessageBox.Yes
            if not proceed:
                installer.cleanup(staging)
                return
            dlg = FomodDialog(session, finfo, name, existing, self)
            code = dlg.exec()
            if code == QDialog.Accepted:
                self._install_fomod(installer, source, staging, session, dlg.mod_name, dlg.mode, info)
                return
            if code != FomodDialog.MANUAL:
                installer.cleanup(staging)
                return

        dlg = InstallDialog(staging, result["root"], result["confident"], name, self.instance.game, existing, self)
        if not dlg.exec():
            installer.cleanup(staging)
            return
        name, data_root, mode = dlg.name.text().strip(), dlg.root, dlg.mode

        def work(_progress):
            try:
                return installer.install(data_root, name, mode, source=source, info=info)
            finally:
                installer.cleanup(staging)

        self.set_busy(f"Installing {name}…")
        tasks.start(work, lambda n: self._installed(n, source), self._task_failed)

    def _install_fomod(self, installer: Installer, source: Path, staging: Path, session, name: str,
                       mode: str, info) -> None:
        output = staging.with_name(staging.name + "-fomod")

        def work(_progress):
            try:
                warnings = session.build(output)
                for w in warnings:
                    log.warning("%s: %s", name, w)
                return installer.install(output, name, mode, source=source, info=info,
                                         extra_meta={"fomod": session.choices()})
            finally:
                installer.cleanup(output)
                installer.cleanup(staging)

        self.set_busy(f"Installing {name}…")
        tasks.start(work, lambda n: self._installed(n, source), self._task_failed)

    def _installed(self, installed: str, source: Path) -> None:
        self.set_busy(None)
        self.modlist.add_installed(installed, enabled=True)
        self.mod_model.reset()
        self._mods_changed()
        self.refresh_downloads()
        self._apply_mod_filter()
        row = self.modlist.index_of(installed)
        if row >= 0:
            self.mod_view.setCurrentIndex(self.mod_model.index(row, 0))
            self.mod_view.scrollTo(self.mod_model.index(row, 0))
        log.info("Installed '%s' from %s", installed, source.name)
        mod = self.modlist.get(installed)
        if mod is not None and mod.meta.get("nexus_id") and nexus.load_api_key() and self.instance.game.nexus_domain:
            client = nexus.NexusClient(nexus.load_api_key())
            domain = self.instance.game.nexus_domain
            tasks.start(lambda _p: nexus_tasks.check_mod(client, mod.meta.get("nexus_game", domain), mod),
                        lambda _r: (self.mod_model.reset(), self._mods_changed_quiet()),
                        lambda msg: log.warning("Could not fetch Nexus info for %s: %s", installed, msg))

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and not self.busy:
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        paths = [p for p in paths if p.is_dir() or archives.is_archive(p)]
        if paths:
            event.acceptProposedAction()
            self.install_archive(paths[0])
            if len(paths) > 1:
                log.warning("Only one mod can be installed at a time; installing %s", paths[0].name)

    # ========================================================== deploy / run

    def deploy(self) -> None:
        if self.busy:
            return
        self.set_busy("Deploying…")
        tasks.start(lambda p: self.manager.deploy(p), self._deployed, self._task_failed, self._progress)

    def _deployed(self, result) -> None:
        self.set_busy(None)
        self.dirty = False
        if result.errors:
            QMessageBox.warning(self, "Deploy", "Some files could not be deployed:\n\n" + "\n".join(result.errors[:20]))
        self.refresh_plugins()
        self._update_status()

    def purge(self) -> None:
        if self.busy:
            return
        self.set_busy("Purging…")
        tasks.start(lambda p: self.manager.purge(p), self._purged, self._task_failed, self._progress)

    def _purged(self, result) -> None:
        self.set_busy(None)
        self.dirty = False
        if result.errors:
            QMessageBox.warning(self, "Purge", "Some files could not be removed:\n\n" + "\n".join(result.errors[:20]))
        self.refresh_plugins()
        self._update_status()

    def _exe_selected(self, index: int) -> None:
        self.instance.config["selected_executable"] = index
        self.instance.save()

    def _run_clicked(self) -> None:
        if self.process is not None:
            self._unlock()
            return
        exes = self.instance.executables
        idx = self.exe_combo.currentIndex()
        if not 0 <= idx < len(exes):
            return
        exe = exes[idx]
        if not Path(exe.path).exists():
            QMessageBox.warning(self, "Run", f"Program not found:\n{exe.path}")
            return
        exe = self._offer_script_extender(exe)
        if exe is None:
            return
        launches_game = Path(exe.path).name.lower() in {
            n.lower() for n in (self.instance.game.binary, self.instance.game.script_extender
                                and self.instance.game.script_extender.loader) if n}
        if launches_game and not self._crash_risks():
            return
        try:
            self.manager.launch_spec(exe)  # Validate Proton settings before deploying.
        except proton.ProtonError as exc:
            QMessageBox.warning(self, "Proton", str(exc))
            return

        def work(progress):
            result = self.manager.deploy(progress)
            before = self.manager.snapshot_data()
            return result, before, self.manager.launch_spec(exe)

        self.set_busy(f"Deploying before starting {exe.name}…")
        tasks.start(work, lambda r: self._launch(exe.name, *r), self._task_failed, self._progress)

    # ============================================================== problems

    def schedule_problems(self) -> None:
        if hasattr(self, "_problem_timer"):
            self._problem_timer.start()

    def refresh_problems(self) -> None:
        try:
            shown = self.manager.problems(include_ignored=self.show_ignored.isChecked())
            active = self.manager.problems() if self.show_ignored.isChecked() else shown
        except OSError as exc:
            log.error("Could not check for problems: %s", exc)
            return
        ignored = set(self.instance.config.get("ignored_problems", []))
        current = self.problem_list.currentItem()
        current_key = current.data(0, Qt.UserRole) if current else None
        self.problem_list.clear()
        self._problems = {p.key: p for p in shown}
        for p in shown:
            item = QTreeWidgetItem(["Error" if p.severity == checks.ERROR else "Warning", p.title])
            item.setData(0, Qt.UserRole, p.key)
            item.setForeground(0, QColor(theme.BAD if p.severity == checks.ERROR else theme.WARN))
            if p.key in ignored:
                for c in (0, 1):
                    item.setForeground(c, QColor(theme.TEXT_DIM))
                item.setText(1, p.title + "  (ignored)")
            self.problem_list.addTopLevelItem(item)
            if p.key == current_key:
                self.problem_list.setCurrentItem(item)
        errors = sum(1 for p in active if p.severity == checks.ERROR)
        index = self.tabs.indexOf(self.problems_tab)
        self.tabs.setTabText(index, f"Problems ({len(active)})" if active else "Problems")
        self.tabs.tabBar().setTabTextColor(index, QColor(theme.BAD if errors else (theme.WARN if active else theme.TEXT)))
        if self.problem_list.currentItem() is None and self.problem_list.topLevelItemCount():
            self.problem_list.setCurrentItem(self.problem_list.topLevelItem(0))
        self._problem_selected()

    def _selected_problem(self):
        item = self.problem_list.currentItem()
        return self._problems.get(item.data(0, Qt.UserRole)) if item else None

    def _problem_selected(self) -> None:
        p = self._selected_problem()
        self.problem_detail.setText(p.detail if p else "No problems found.")
        fixable = bool(p and (p.fix or p.tool))
        self.problem_fix.setEnabled(fixable and self.process is None)
        self.problem_fix.setText(p.fix_label if fixable else "Fix")
        self.problem_url.setEnabled(bool(p and p.url))
        ignored = p is not None and p.key in set(self.instance.config.get("ignored_problems", []))
        self.problem_ignore.setEnabled(p is not None)
        self.problem_ignore.setText("Don't ignore" if ignored else "Ignore")

    def _fix_problem(self) -> None:
        p = self._selected_problem()
        if p is not None and p.tool:
            log.info("Fixing \"%s\": running %s in the Wine prefix", p.title, " ".join(p.tool))
            self.run_tool(p.tool[0], p.tool[1:])
            return
        if p is None or p.fix is None:
            return
        try:
            p.fix()
        except OSError as exc:
            QMessageBox.warning(self, "Fix", str(exc))
        log.info("Fixed: %s", p.title)
        self.mod_model.reset()
        self._apply_mod_filter()
        self._mods_changed()

    def _open_problem_url(self) -> None:
        p = self._selected_problem()
        if p and p.url:
            QDesktopServices.openUrl(QUrl(p.url))

    def _ignore_problem(self) -> None:
        p = self._selected_problem()
        if p is None:
            return
        ignored = p.key in set(self.instance.config.get("ignored_problems", []))
        self.manager.ignore_problem(p.key, not ignored)
        self.refresh_problems()

    def _crash_risks(self) -> bool:
        """Warn before starting the game with problems that make it crash. True to continue."""
        risks = [p for p in self.manager.problems()
                 if p.severity == checks.ERROR and p.key.split(":")[0] in ("master", "order", "limit")]
        if not risks:
            return True
        text = "\n".join(f"• {p.title}" for p in risks[:8])
        box = QMessageBox(QMessageBox.Warning, "Problems",
                          f"The game will probably crash at startup:\n\n{text}", parent=self)
        box.addButton("Start anyway", QMessageBox.DestructiveRole)
        show = box.addButton("Show problems", QMessageBox.AcceptRole)
        box.setDefaultButton(show)
        box.exec()
        if box.clickedButton() is show:
            self.tabs.setCurrentWidget(self.problems_tab)
            return False
        return True

    # ================================================================== LOOT

    def loot_sort(self) -> None:
        if self.busy or not self.instance.game.loot_repo:
            return
        self.set_busy("Sorting plugins…")
        texts = self._text_emitter

        def work(_progress):
            return self.manager.loot_sort(texts.text.emit)

        tasks.start(work, self._loot_sorted, self._loot_failed)

    def _loot_failed(self, message: str) -> None:
        self.set_busy(None)
        log.error("LOOT sorting failed: %s", message)
        QMessageBox.warning(self, "Sort plugins", message)

    def _loot_sorted(self, payload) -> None:
        self.set_busy(None)
        result, entries = payload
        for note in result.notes:
            log.warning("%s", note)
        before = [e.name for e in self.plugin_model.entries]
        after = [e.name for e in entries]
        if before == after:
            log.info("LOOT: the load order is already sorted")
            QMessageBox.information(self, "Sort plugins", "The load order is already sorted.")
            return
        old_pos = {n: i for i, n in enumerate(before)}
        changes = [f"{n}: {old_pos.get(n, '?')} → {i}" for i, n in enumerate(after) if old_pos.get(n) != i]
        box = QMessageBox(QMessageBox.Question, "Sort plugins",
                          f"Sorting changes the position of {len(changes)} plugin(s). Apply the new load order?",
                          parent=self)
        box.setDetailedText("\n".join(changes))
        apply = box.addButton("Apply", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(apply)
        box.exec()
        if box.clickedButton() is not apply:
            return
        self.manager.apply_plugin_order(entries)
        self.refresh_plugins()
        self._plugins_changed()
        log.info("LOOT: applied the sorted load order (%d plugins moved)", len(changes))

    def loot_update(self) -> None:
        from modmanager.core.loot import masterlist

        repo = self.instance.game.loot_repo
        if self.busy or not repo:
            return
        self.set_busy("Updating the LOOT masterlist…")

        def done(path) -> None:
            self.set_busy(None)
            self.schedule_problems()
            log.info("LOOT masterlist: %s", path)

        tasks.start(lambda _p: masterlist.update(repo, force=True), done, self._loot_failed)

    def loot_userlist(self) -> None:
        path = self.manager.loot_userlist
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(USERLIST_TEMPLATE)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        log.info("LOOT userlist: %s — changes apply to the next sort", path)

    # ================================================================= nexus

    def _nexus_client(self) -> nexus.NexusClient | None:
        key = nexus.load_api_key()
        if not key:
            if QMessageBox.question(
                self, "Nexus Mods",
                "This needs your Nexus Mods API key. Open the settings to enter it?",
            ) == QMessageBox.Yes:
                self.edit_settings()
            key = nexus.load_api_key()
        return nexus.NexusClient(key) if key else None

    def handle_nxm(self, url: str) -> None:
        self.raise_()
        self.activateWindow()
        try:
            link = nexus.NxmLink.parse(url)
        except ValueError as exc:
            log.error("%s", exc)
            return
        domain = self.instance.game.nexus_domain
        if link.game != domain:
            QMessageBox.warning(
                self, "Nexus Mods",
                f"This link is for the game '{link.game}', but the open instance is "
                f"{self.instance.game.name}. Switch instances and click the link again.",
            )
            return
        client = self._nexus_client()
        if client is None:
            return
        key = f"{link.mod_id}-{link.file_id}-{time.monotonic()}"
        entry = {"label": f"Nexus mod {link.mod_id}, file {link.file_id}", "done": 0, "total": 0, "cancel": False}
        self.downloads_active[key] = entry
        self.tabs.setCurrentIndex(self.tabs.indexOf(self.downloads.parentWidget()))
        self.refresh_downloads()
        log.info("Downloading Nexus mod %s, file %s", link.mod_id, link.file_id)

        def work(progress):
            return nexus_tasks.download_nxm(
                client, link, self.instance.downloads_dir, progress,
                cancelled=lambda: entry["cancel"],
                on_start=lambda dest: entry.update(label=dest.name),
            )

        def on_progress(done: int, total: int) -> None:
            entry.update(done=done, total=total)
            for i in range(self.downloads.topLevelItemCount()):
                item = self.downloads.topLevelItem(i)
                if item.data(0, Qt.UserRole + 1) == key:
                    item.setText(0, entry["label"])
                    item.setText(1, _size(total))
                    item.setText(2, _progress_text(entry))

        def done(path: Path) -> None:
            self.downloads_active.pop(key, None)
            self.refresh_downloads()
            log.info("Downloaded %s — double-click it in Downloads to install", path.name)

        def failed(message: str) -> None:
            self.downloads_active.pop(key, None)
            self.refresh_downloads()
            if message == "Download cancelled":
                log.info("Download cancelled")
            else:
                log.error("Download failed: %s", message)
                QMessageBox.warning(self, "Nexus Mods", message)

        tasks.start(work, done, failed, on_progress)

    def nexus_check(self, force: bool = False, only: list[str] | None = None) -> None:
        domain = self.instance.game.nexus_domain
        if not domain or self.nexus_checking:
            return
        client = self._nexus_client()
        if client is None:
            return
        mods = [m for m in self.modlist.mods if (only is None or m.name in only) and m.meta.get("nexus_id")]
        if not mods:
            log.info("No mods with a Nexus Mods ID to check. Mods installed from nxm downloads or "
                     "Nexus-named archives have one; use \"Identify on Nexus Mods\" for others.")
            return
        self.nexus_checking = True
        self.busy_label.setText(f"Checking {len(mods)} mod(s) on Nexus…")

        def done(result: nexus_tasks.CheckResult) -> None:
            self.nexus_checking = False
            self.busy_label.setText("")
            self.mod_model.reset()
            self._apply_mod_filter()
            self._mods_changed_quiet()
            summary = f"Nexus check: {result.checked} checked, {result.skipped} unchanged since the last check"
            if result.updates:
                summary += f"; updates for {', '.join(result.updates)}"
            log.info("%s", summary)
            for err in result.errors[:10]:
                log.warning("%s", err)
            if client.rate_limit:
                log.info("Nexus API requests left: %d today, %d this hour", *client.rate_limit)

        def failed(message: str) -> None:
            self.nexus_checking = False
            self.busy_label.setText("")
            log.error("Nexus check failed: %s", message)

        def progress(i: int, n: int) -> None:
            self.busy_label.setText(f"Checking mods on Nexus… {i}/{n}")

        tasks.start(lambda p: nexus_tasks.check_mods(client, domain, mods, force, p), done, failed, progress)

    def nexus_identify(self, name: str) -> None:
        client = self._nexus_client()
        mod = self.modlist.get(name)
        if client is None or mod is None:
            return

        def done(found: bool) -> None:
            if found:
                log.info("Identified '%s' as Nexus mod %s", name, mod.meta.get("nexus_id"))
                self.nexus_check(force=True, only=[name])
            else:
                log.warning("'%s' was not found on Nexus Mods by its archive's checksum", name)
            self.mod_model.reset()

        tasks.start(lambda _p: nexus_tasks.identify(client, self.instance.game.nexus_domain, mod),
                    done, lambda msg: log.error("Identify failed: %s", msg))

    def _mods_changed_quiet(self) -> None:
        """Refresh views after metadata changed without touching the deployment state."""
        self._update_status()
        self.schedule_problems()

    def _offer_script_extender(self, exe):
        """Starting the plain game with script extender plugins enabled silently skips them."""
        status = self.manager.script_extender_status(refresh=True)
        binary = self.instance.game.binary
        if status is None or not binary or Path(exe.path).name.lower() != binary.lower():
            return exe
        dlls = self.manager.enabled_script_extender_plugins()
        if not dlls:
            return exe
        name = status.extender.name
        if not status.installed:
            answer = QMessageBox.warning(
                self, name,
                f"{len(dlls)} enabled {name} plugin(s) will not load because {name} is not installed "
                f"in the game folder.\n\nGet it from {status.extender.url}\n\nStart the game anyway?",
                QMessageBox.Yes | QMessageBox.No,
            )
            return exe if answer == QMessageBox.Yes else None
        box = QMessageBox(QMessageBox.Question, name,
                          f"{len(dlls)} {name} plugin(s) are enabled, but {exe.name} starts the game "
                          f"without {name}, so they will not load.", parent=self)
        use_se = box.addButton(f"Start with {name}", QMessageBox.AcceptRole)
        box.addButton("Start without", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(use_se)
        box.exec()
        clicked = box.clickedButton()
        if clicked is use_se:
            exes = self.instance.executables
            match = next((i for i, e in enumerate(exes) if Path(e.path) == status.loader), None)
            if match is None:
                exes.insert(0, Executable(name, str(status.loader)))
                self.instance.executables = exes
                match = 0
            self.instance.config["selected_executable"] = match
            self.instance.save()
            self.refresh_executables()
            return self.instance.executables[match]
        if clicked is box.button(QMessageBox.Cancel):
            return None
        return exe

    def run_tool(self, tool: str, args: list[str] | None = None) -> None:
        if self.busy or self.process is not None:
            return
        try:
            spec = proton.build_tool(self.instance.proton, tool, args)
        except proton.ProtonError as exc:
            QMessageBox.warning(self, "Proton", str(exc))
            return
        self._launch(tool, None, None, spec)

    def run_winetricks(self) -> None:
        verbs, ok = QInputDialog.getText(
            self, "Winetricks", "Verbs to install (empty opens the Winetricks GUI):",
        )
        if ok:
            self.run_tool("winetricks", verbs.split() or ["--gui"])

    def _launch(self, label: str, result, before: set[str] | None, spec: proton.LaunchSpec) -> None:
        self.set_busy(None)
        if result is not None:
            self.dirty = False
            if result.errors:
                log.warning("%d file(s) could not be deployed", len(result.errors))
            self.refresh_plugins()
        prefix = Path(spec.env.get("WINEPREFIX", "")) if spec.env.get("WINEPREFIX") else None
        if prefix is not None and not proton.prefix_initialized(prefix):
            log.info("Creating the Wine prefix — the first start takes a while (umu may also download Proton).")
        log.info("Starting %s: %s", label, spec.describe())

        logs_dir = self.instance.path / "logs"
        logs_dir.mkdir(exist_ok=True)
        self._run_log = open(logs_dir / f"{''.join(c if c.isalnum() else '_' for c in label)}.log", "w",
                             encoding="utf-8", errors="replace")
        proc = QProcess(self)
        env = QProcessEnvironment()
        for k, v in spec.env.items():
            env.insert(k, v)
        proc.setProcessEnvironment(env)
        proc.setWorkingDirectory(spec.cwd)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(lambda: self._proc_output(proc))
        proc.finished.connect(lambda code, _status: self._proc_finished(label, before, code))
        proc.errorOccurred.connect(lambda err: self._proc_error(label, before, err))
        self.process = proc
        self._run_started = time.monotonic()
        self._lock(True, label)
        proc.start(spec.argv[0], spec.argv[1:])

    def _proc_output(self, proc: QProcess) -> None:
        text = bytes(proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        if self._run_log:
            self._run_log.write(text)
        for line in text.splitlines():
            if line.strip():
                self._append_log(line, logging.DEBUG)

    def _proc_error(self, label: str, before, err) -> None:
        if err == QProcess.FailedToStart:
            log.error("%s failed to start: %s", label, self.process.errorString() if self.process else err)
            self._proc_finished(label, before, -1)

    def _proc_finished(self, label: str, before: set[str] | None, code: int) -> None:
        if self.process is None:
            return
        self.process.deleteLater()
        self.process = None
        if self._run_log:
            self._run_log.close()
            self._run_log = None
        elapsed = time.monotonic() - self._run_started
        log.info("%s exited with code %s after %.0f s", label, code, elapsed)
        self._lock(False)
        if before is None:
            self.refresh_problems()  # A tool such as winetricks may have fixed something.
            return

        def work(_p):
            return self.manager.capture_new_files(before)

        def done(moved):
            self.set_busy(None)
            if moved:
                self.modlist.update_conflicts()
                self.mod_model.reset()
            self.refresh_plugins()
            self._update_status()

        self.set_busy("Checking the game folder for new files…")
        tasks.start(work, done, self._task_failed)

    def _lock(self, locked: bool, label: str = "") -> None:
        for w in (self.mod_view, self.plugin_view, self.profile_combo, self.exe_combo):
            w.setEnabled(not locked)
        self.mod_model.locked = locked
        self.plugin_model.locked = locked
        for a in (self.a_deploy, self.a_purge, self.a_install, self.a_install_dir, self.a_settings,
                  self.a_exes, self.a_instances, self.a_refresh):
            a.setEnabled(not locked)
        self.run_btn.setText("Unlock" if locked else "Run")
        self.run_btn.setToolTip(
            f"{label} is running. Unlock to edit mods anyway (new files are still collected when it exits)."
            if locked else ""
        )
        self._update_status()

    def _unlock(self) -> None:
        if QMessageBox.question(
            self, "Unlock",
            "The program is still running. Changing mods now can break the running game. Unlock anyway?",
        ) == QMessageBox.Yes:
            proc = self.process
            self._lock(False)
            self.run_btn.setEnabled(False)
            self.run_btn.setToolTip("Waiting for the running program to exit")
            if proc is not None:
                proc.finished.connect(lambda *_: self.run_btn.setEnabled(True))

    # ============================================================== dialogs

    def edit_executables(self) -> None:
        if ExecutablesDialog(self.instance, self).exec():
            self.refresh_executables()

    def edit_settings(self) -> None:
        old_data = self.instance.data_dir
        dlg = SettingsDialog(self.instance, self)
        if not dlg.exec():
            return
        if dlg.new_method:
            self.set_busy("Switching deployment method…")
            tasks.start(lambda _p: self.manager.change_deploy_method(dlg.new_method),
                        lambda _r: self._settings_applied(), self._task_failed)
        elif self.instance.data_dir != old_data:
            self.set_busy("Moving deployment to the new game folder…")
            tasks.start(lambda _p: self.manager.reload(), lambda _r: self._settings_applied(), self._task_failed)
        else:
            self._settings_applied()

    def _settings_applied(self) -> None:
        self.set_busy(None)
        self.refresh_script_extender()
        if self.manager.ensure_script_extender_executable():
            self.refresh_executables()
        self.refresh_all(rescan=False)
        self.dirty = self.manager.needs_deploy()
        self._update_status()
        log.info("Settings saved")

    def switch_instance(self) -> None:
        picker = InstancePicker(self.registry, self)
        if not picker.exec() or picker.chosen is None or picker.chosen == self.instance.path:
            return
        try:
            inst = Instance.load(picker.chosen)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Instance", str(exc))
            return
        self.registry.add(inst)
        win = MainWindow(inst, self.registry, self.log_handler)
        win.show()
        self.close()

    def open_prefix(self) -> None:
        prefix = self.instance.proton.get("prefix")
        if prefix:
            open_path(proton.wine_root(Path(prefix).expanduser()))

    def open_plugin_dir(self) -> None:
        d = self.manager.plugin_file_dir()
        if d is not None:
            open_path(d)

    def open_my_games(self) -> None:
        prefix = self.instance.proton.get("prefix")
        name = self.instance.game.my_games_folder(self.instance.game_dir)
        if prefix and name:
            open_path(proton.my_games(Path(prefix).expanduser()) / name)

    def about(self) -> None:
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> {__version__}<br>A native Linux mod manager modelled on Mod Organizer 2.<br><br>"
            "Mods are linked into the game folder on deploy and removed on purge; Windows programs "
            "run through Proton with umu-launcher.",
        )

    # ================================================================ helpers

    def set_busy(self, text: str | None) -> None:
        self.busy = text is not None
        self.busy_label.setText(text or "")
        self.progress.setVisible(self.busy)
        self.progress.setRange(0, 0)
        self.h_split.setEnabled(not self.busy)
        self.menuBar().setEnabled(not self.busy)
        if self.busy:
            self.setCursor(Qt.BusyCursor)
        else:
            self.unsetCursor()

    def _progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)

    def _task_failed(self, message: str) -> None:
        self.set_busy(None)
        log.error("%s", message)
        QMessageBox.critical(self, "Error", message)
        self.refresh_all(rescan=False)

    def _append_log(self, text: str, level: int) -> None:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(LOG_COLORS.get(level, theme.TEXT)))
        cursor = self.log_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        if not self.log_view.document().isEmpty():
            cursor.insertBlock()
        cursor.insertText(text, fmt)
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def closeEvent(self, event):
        if self.process is not None and QMessageBox.question(
            self, "Quit", "A program started from here is still running. Quit anyway?",
        ) != QMessageBox.Yes:
            event.ignore()
            return
        if self.busy:
            event.ignore()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("split_h", self.h_split.saveState())
        self.settings.setValue("split_v", self.v_split.saveState())
        try:
            self.log_handler.emitter.message.disconnect(self._append_log)
            if nxm_router is not None:
                nxm_router.link.disconnect(self.handle_nxm)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)


def _size(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n else ""


def _progress_text(d: dict) -> str:
    if d.get("cancel"):
        return "Cancelling…"
    if d.get("total"):
        return f"Downloading {100 * d['done'] // d['total']}%"
    return "Downloading…" if d.get("done") else "Starting…"


USERLIST_TEMPLATE = """\
# LOOT userlist: your own sorting rules, applied on top of the masterlist.
# Same format as LOOT's userlist.yaml. Examples:
#
# plugins:
#   - name: 'My Patch.esp'
#     after: ['Some Mod.esp']        # load after these plugins
#   - name: 'Some Overhaul.esp'
#     group: 'Late Loaders'          # one of the masterlist's groups
#
# groups:
#   - name: 'My Late Group'
#     after: ['Late Loaders']
plugins: []
"""
