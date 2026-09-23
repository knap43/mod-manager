"""The main window: mod list on the left, plugins and downloads on the right, log below."""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QModelIndex, QObject, QProcess, QProcessEnvironment, QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QKeySequence, QTextCharFormat
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSizePolicy, QSplitter, QTabWidget, QToolButton, QTreeView, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from modmanager import APP_NAME, __version__
from modmanager.core import archives, proton
from modmanager.core.installer import Installer, detect_data_root, guess_info
from modmanager.core.instance import Instance, InstanceRegistry
from modmanager.core.manager import Manager
from modmanager.core.modlist import ModList
from modmanager.ui import tasks, theme
from modmanager.ui.dialogs import (
    ConflictsDialog, ExecutablesDialog, InstallDialog, InstancePicker, SettingsDialog,
)
from modmanager.ui.models import ModListModel, PluginListModel

log = logging.getLogger(__name__)
LOG_COLORS = {logging.DEBUG: theme.TEXT_DIM, logging.WARNING: theme.WARN, logging.ERROR: theme.BAD}


class LogEmitter(QObject):
    message = Signal(str, int)


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

        m = mb.addMenu("&Proton")
        m.addAction("Wine configuration (winecfg)", lambda: self.run_tool("winecfg"))
        m.addAction("Winetricks…", self.run_winetricks)
        m.addSeparator()
        m.addAction("Open Wine prefix folder", self.open_prefix)
        m.addAction("Open plugins.txt folder", self.open_plugin_dir)
        m.addAction("Open My Games folder", self.open_my_games)

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
        self.downloads.itemDoubleClicked.connect(lambda item, _c: self.install_archive(Path(item.data(0, Qt.UserRole))))
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
        sb.addWidget(self.deploy_label, 1)
        sb.addPermanentWidget(self.busy_label)
        sb.addPermanentWidget(self.progress)

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

    def refresh_downloads(self) -> None:
        self.downloads.clear()
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
        item = self.downloads.currentItem()
        if item is not None:
            self.install_archive(Path(item.data(0, Qt.UserRole)))

    def install_archive(self, source: Path) -> None:
        if self.busy:
            return
        installer = Installer(self.instance.mods_dir, self.instance.tmp_dir, self.instance.game)
        game = self.instance.game

        def work(_progress):
            staging = installer.extract(source)
            root, confident = detect_data_root(staging, game)
            return staging, root, confident

        self.set_busy(f"Extracting {source.name}…")
        tasks.start(work, lambda r: self._install_confirm(installer, source, *r), self._task_failed)

    def _install_confirm(self, installer: Installer, source: Path, staging: Path, root: Path, confident: bool) -> None:
        self.set_busy(None)
        info = guess_info(source)
        dlg = InstallDialog(staging, root, confident, info.name, self.instance.game,
                            [m.name for m in self.modlist.mods], self)
        if not dlg.exec():
            installer.cleanup(staging)
            return
        name, data_root, mode = dlg.name.text().strip(), dlg.root, dlg.mode

        def work(_progress):
            try:
                return installer.install(data_root, name, mode, source=source, info=info)
            finally:
                installer.cleanup(staging)

        def done(installed: str):
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

        self.set_busy(f"Installing {name}…")
        tasks.start(work, done, self._task_failed)

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
        name = self.instance.game.my_games_name
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
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)
