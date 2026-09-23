"""Dark grey, dark blue, square corners."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

BG = "#2b2b2b"          # Window background
BASE = "#222222"        # Lists, text fields
ALT = "#272727"         # Alternating rows
PANEL = "#333333"       # Buttons, headers, tabs
BORDER = "#3f3f3f"
TEXT = "#d6d6d6"
TEXT_DIM = "#8a8a8a"
ACCENT = "#1f4a7a"      # Selection / highlight
ACCENT_HOVER = "#285a91"
ACCENT_BORDER = "#3669a3"

SEPARATOR_BG = "#1b2430"
WIN_BG = "#1f3321"      # Selected mod overwrites this one
LOSE_BG = "#3a2020"     # Selected mod is overwritten by this one
GOOD = "#6fbf73"
BAD = "#d9645c"
WARN = "#d8b04c"

STYLESHEET = f"""
* {{ border-radius: 0px; outline: none; }}
QWidget {{ background: {BG}; color: {TEXT}; font-size: 10pt; }}
QMainWindow::separator {{ background: {BORDER}; width: 1px; height: 1px; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {ACCENT_BORDER}; padding: 3px; }}

QMenuBar {{ background: {BG}; border-bottom: 1px solid {BORDER}; }}
QMenuBar::item {{ padding: 4px 10px; background: transparent; }}
QMenuBar::item:selected {{ background: {ACCENT}; }}
QMenu {{ background: {PANEL}; border: 1px solid {BORDER}; padding: 2px; }}
QMenu::item {{ padding: 4px 22px 4px 18px; }}
QMenu::item:selected {{ background: {ACCENT}; }}
QMenu::item:disabled {{ color: {TEXT_DIM}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 3px 4px; }}

QToolBar {{ background: {BG}; border: none; border-bottom: 1px solid {BORDER}; spacing: 4px; padding: 3px; }}
QToolButton {{ background: {PANEL}; border: 1px solid {BORDER}; padding: 4px 10px; }}
QToolButton:hover {{ border-color: {ACCENT_BORDER}; background: #393939; }}
QToolButton:pressed, QToolButton:checked {{ background: {ACCENT}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}

QPushButton {{ background: {PANEL}; border: 1px solid {BORDER}; padding: 5px 14px; min-height: 16px; }}
QPushButton:hover {{ border-color: {ACCENT_BORDER}; background: #393939; }}
QPushButton:pressed {{ background: {ACCENT}; }}
QPushButton:disabled {{ color: {TEXT_DIM}; background: #2e2e2e; }}
QPushButton:default {{ border-color: {ACCENT_BORDER}; }}
QPushButton#primary {{ background: {ACCENT}; border-color: {ACCENT_BORDER}; font-weight: bold; padding: 5px 22px; }}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primary:disabled {{ background: #2e3a48; color: {TEXT_DIM}; }}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {{
    background: {BASE}; border: 1px solid {BORDER}; padding: 4px; selection-background-color: {ACCENT};
}}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border-color: {ACCENT_BORDER}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {TEXT_DIM}; }}
QComboBox {{ padding-right: 20px; }}
QComboBox::drop-down {{ border: none; border-left: 1px solid {BORDER}; width: 18px; }}
QComboBox::down-arrow {{ image: url(@ARROW@); width: 8px; height: 5px; }}
QComboBox QAbstractItemView {{ background: {PANEL}; border: 1px solid {BORDER}; selection-background-color: {ACCENT}; }}

QTreeView, QListView, QTableView, QTreeWidget, QListWidget {{
    background: {BASE}; alternate-background-color: {ALT}; border: 1px solid {BORDER};
    selection-background-color: {ACCENT}; selection-color: {TEXT};
}}
QTreeView::item, QListView::item {{ padding: 2px 0px; }}
QTreeView::item:selected, QListView::item:selected {{ background: {ACCENT}; }}
QTreeView::item:hover:!selected, QListView::item:hover:!selected {{ background: #2d3440; }}
QTreeView::indicator, QListView::indicator, QCheckBox::indicator {{
    width: 12px; height: 12px; border: 1px solid #5a5a5a; background: {BASE};
}}
QTreeView::indicator:checked, QListView::indicator:checked, QCheckBox::indicator:checked {{
    background: {ACCENT_HOVER}; border-color: {ACCENT_BORDER}; image: url(@CHECK@);
}}
QTreeView::indicator:disabled, QCheckBox::indicator:disabled {{ background: #303a46; border-color: #444; }}
QRadioButton::indicator {{ width: 12px; height: 12px; border: 1px solid #5a5a5a; background: {BASE}; }}
QRadioButton::indicator:checked {{ background: {ACCENT_HOVER}; border-color: {ACCENT_BORDER}; image: url(@RADIO@); }}
QRadioButton::indicator:disabled {{ background: #303a46; border-color: #444; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {TEXT_DIM}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QHeaderView {{ background: {PANEL}; border: none; }}
QHeaderView::section {{ background: {PANEL}; color: {TEXT}; border: none; border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER}; padding: 4px 6px; }}

QTabWidget::pane {{ border: 1px solid {BORDER}; top: -1px; }}
QTabBar::tab {{ background: {PANEL}; border: 1px solid {BORDER}; padding: 5px 14px; margin-right: -1px; }}
QTabBar::tab:selected {{ background: {BG}; border-top: 2px solid {ACCENT_BORDER}; border-bottom-color: {BG}; }}
QTabBar::tab:hover:!selected {{ background: #393939; }}

QScrollBar:vertical {{ background: {BASE}; width: 12px; margin: 0; border-left: 1px solid {BORDER}; }}
QScrollBar:horizontal {{ background: {BASE}; height: 12px; margin: 0; border-top: 1px solid {BORDER}; }}
QScrollBar::handle {{ background: #464646; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: {ACCENT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QSplitter::handle {{ background: {BG}; }}
QSplitter::handle:horizontal {{ width: 5px; }}
QSplitter::handle:vertical {{ height: 5px; }}
QStatusBar {{ background: {PANEL}; border-top: 1px solid {BORDER}; }}
QStatusBar QLabel {{ background: transparent; padding: 0 6px; }}
QProgressBar {{ background: {BASE}; border: 1px solid {BORDER}; text-align: center; max-height: 14px; }}
QProgressBar::chunk {{ background: {ACCENT}; }}
QGroupBox {{ border: 1px solid {BORDER}; margin-top: 12px; padding-top: 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {TEXT_DIM}; }}
QLabel#hint {{ color: {TEXT_DIM}; }}
QLabel#good {{ color: {GOOD}; }}
QLabel#bad {{ color: {BAD}; }}
QLabel#sectionTitle {{ color: {TEXT_DIM}; font-weight: bold; padding: 2px 0; }}
QPlainTextEdit#log {{ font-family: monospace; font-size: 9pt; }}
"""


def apply(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    roles = {
        QPalette.Window: BG, QPalette.WindowText: TEXT, QPalette.Base: BASE,
        QPalette.AlternateBase: ALT, QPalette.ToolTipBase: PANEL, QPalette.ToolTipText: TEXT,
        QPalette.Text: TEXT, QPalette.Button: PANEL, QPalette.ButtonText: TEXT,
        QPalette.Highlight: ACCENT, QPalette.HighlightedText: TEXT, QPalette.Link: ACCENT_BORDER,
        QPalette.PlaceholderText: TEXT_DIM, QPalette.BrightText: "#ffffff",
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor(TEXT_DIM))
    app.setPalette(pal)
    here = Path(__file__).parent
    app.setStyleSheet(
        STYLESHEET.replace("@CHECK@", (here / "check.svg").as_posix())
        .replace("@ARROW@", (here / "arrow.svg").as_posix())
        .replace("@RADIO@", (here / "radio.svg").as_posix())
    )
