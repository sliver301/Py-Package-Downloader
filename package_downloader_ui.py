# Package Downloader - Python packages for PCs without internet
# Copyright (C) 2026 Package Downloader contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE file.

"""
Package Downloader - the windows (PySide6).

package_downloader.py holds all the logic and calls init(core) before opening a window, so the
names used here (SETTINGS, JOB, library(), PyPI, build_job, OfflineInstall, ...) come from it.
"""

import os
import re
import sys
import threading
import traceback
from concurrent.futures.thread import ThreadPoolExecutor  # not the lazy package name (Python 3.15)

from PySide6.QtCore import (Qt, QObject, Signal, Slot, QTimer, QSize, QByteArray, QPropertyAnimation,
                            QEasingCurve, Property, QRectF, QRect, QPoint, QEvent, QUrl)
from PySide6.QtGui import (QIcon, QPixmap, QPainter, QColor, QFont, QPalette, QGuiApplication, QPen, QFontMetricsF,
                           QFontDatabase, QDesktopServices, QKeySequence, QShortcut, QPainterPath, QBrush)
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame, QLabel, QHBoxLayout, QVBoxLayout,
                               QGridLayout, QToolButton, QPushButton, QButtonGroup, QStackedWidget, QLineEdit,
                               QTreeWidget, QTreeWidgetItem, QHeaderView, QAbstractItemView, QStyledItemDelegate,
                               QStyle, QScrollArea, QSizePolicy, QDialog, QComboBox, QFileDialog, QMenu,
                               QPlainTextEdit, QAbstractButton, QLayout, QGraphicsOpacityEffect, QStyleOptionViewItem)
from PySide6.QtSvg import QSvgRenderer


def init(core):
    """Borrow every public name from package_downloader (settings, library, jobs, PyPI...)."""
    g = globals()
    for k, v in vars(core).items():
        if not k.startswith("__") and k not in g:
            g[k] = v


# =========================================================================================
#  Theme (palettes and accent colours)
# =========================================================================================

BASE_PALETTES = {
    "dark": dict(
        chrome="#17181b", bg="#1c1d20", surface="#212226", th="#27282c", border="#2f3136",
        fg="#e4e5e9", muted="#8f929b", faint="#6b6e76", row_hover="#26272c", hover="#2a2c31",
        pressed="#33353b", seg_bg="#202125", seg_active="#34363c", scroll="#3a3c42", input="#232428",
        icon="#c9cbd1", switch_off="#45474e", ok="#3fb950", warn="#d29922", bad="#f85149",
        log_bg="#141518", log_fg="#c9cbd1", shadow="#000000",
    ),
    "light": dict(
        chrome="#f5f6f8", bg="#ffffff", surface="#ffffff", th="#f4f5f7", border="#e3e5ea",
        fg="#1f2328", muted="#6b7180", faint="#9aa0ab", row_hover="#f6f7f9", hover="#eceef2",
        pressed="#dde0e6", seg_bg="#eceef2", seg_active="#ffffff", scroll="#c9cdd4", input="#ffffff",
        icon="#3d424b", switch_off="#c7ccd4", ok="#1a7f37", warn="#9a6700", bad="#cf222e",
        log_bg="#f6f7f9", log_fg="#2b3038", shadow="#101828",
    ),
}
ACCENTS = {  # name: (dark theme, light theme)
    "Blue": ("#6aa9ff", "#1f6feb"), "Indigo": ("#8f90ff", "#5b5bd6"), "Teal": ("#2ec4b0", "#0f8a7a"),
    "Green": ("#4cc76a", "#1a7f37"), "Orange": ("#ff9f5a", "#c4561a"), "Rose": ("#ff7aa8", "#c22d6b"),
}

P = {}            # the current palette, shared by everything below
_THEMED = []      # callbacks that re-colour icons etc. after a theme change


def qcolor(s):
    """QColor from '#rrggbb' or 'rgba(r,g,b,a)'."""
    m = re.match(r"rgba\((\d+),(\d+),(\d+),([\d.]+)\)", s.replace(" ", ""))
    if m:
        c = QColor(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        c.setAlphaF(float(m.group(4)))
        return c
    return QColor(s)


def rgba(hex_color, alpha):
    c = QColor(hex_color)
    return "rgba(%d,%d,%d,%s)" % (c.red(), c.green(), c.blue(), alpha)


def make_palette(theme, accent_name):
    p = dict(BASE_PALETTES[theme])
    pair = ACCENTS.get(accent_name, ACCENTS["Blue"])
    acc = pair[0] if theme == "dark" else pair[1]
    c = QColor(acc)
    p.update(accent=acc, accent_soft=rgba(acc, 0.16), accent_softer=rgba(acc, 0.09), sel=rgba(acc, 0.22),
             accent_hover=(c.lighter(112) if theme == "dark" else c.darker(112)).name(),
             on_accent="#0d1117" if theme == "dark" else "#ffffff", theme=theme,
             ok_soft=rgba(p["ok"], 0.14), warn_soft=rgba(p["warn"], 0.15), bad_soft=rgba(p["bad"], 0.13))
    return p


def resolve_theme():
    t = os.environ.get("PD_THEME") or SETTINGS.get("theme", "system")
    if t in ("dark", "light"):
        return t
    try:
        if QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Light:
            return "light"
        if QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark:
            return "dark"
    except Exception:
        pass
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
        except Exception:
            pass
    return "light"


def themed(fn):
    """Run fn now and again after every theme change (used for icons and custom colours)."""
    _THEMED.append(fn)
    fn()
    return fn


def apply_theme(app):
    P.clear()
    P.update(make_palette(resolve_theme(), SETTINGS.get("accent", "Blue")))
    app.setPalette(qt_palette())
    app.setStyleSheet(build_qss())
    alive = []
    for fn in _THEMED:
        try:
            fn()
            alive.append(fn)
        except RuntimeError:          # its widget is gone
            pass
    _THEMED[:] = alive
    for w in app.topLevelWidgets():
        style_titlebar(w)
        w.update()


# =========================================================================================
#  Icons (Lucide-style line icons from SVG: crisp at any size, recoloured per theme)
# =========================================================================================

ICON_PATHS = {
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
    "moon": '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    "settings": '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
    "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "check": '<polyline points="20 6 9 17 4 12"/>',
    "x": '<path d="M18 6 6 18M6 6l12 12"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "chevron-up": '<path d="m18 15-6-6-6 6"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    "folder": '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/>',
    "folder-open": '<path d="m6 14 1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5c0-1.1.9-2 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2"/>',
    "trending": '<polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/>',
    "file-code": '<path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/><path d="m10 13-2 2 2 2M14 17l2-2-2-2"/>',
    "library": '<path d="m16 6 4 14M12 6v14M8 8v12M4 4v16"/>',
    "package": '<path d="M16.5 9.4 7.55 4.24"/><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.29 7 12 12 20.71 7"/><line x1="12" y1="22" x2="12" y2="12"/>',
    "trash": '<path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6M14 11v6"/>',
    "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
    "wifi-off": '<path d="M12 20h.01M8.5 16.43a5 5 0 0 1 7 0M2 8.82a15 15 0 0 1 4.17-2.65M10.66 5c4.01-.36 8.14.9 11.34 3.76M16.85 11.25a10 10 0 0 1 2.22 1.68M5 13a10 10 0 0 1 5.24-2.76M2 2l20 20"/>',
    "check-circle": '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    "alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4M12 17h.01"/>',
    "x-circle": '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6M9 9l6 6"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
    "terminal": '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
    "copy": '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "external": '<path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "monitor": '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
    "cpu": '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M15 2v2M15 20v2M2 15h2M2 9h2M20 15h2M20 9h2M9 2v2M9 20v2"/>',
    "sparkles": '<path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z"/>',
    "list": '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
    "tag": '<path d="M12.59 2.59A2 2 0 0 0 11.17 2H4a2 2 0 0 0-2 2v7.17a2 2 0 0 0 .59 1.42l8.7 8.7a2.43 2.43 0 0 0 3.42 0l6.58-6.58a2.43 2.43 0 0 0 0-3.42z"/><circle cx="7.5" cy="7.5" r=".5"/>',
    "history": '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5M12 7v5l4 2"/>',
    "palette": '<circle cx="13.5" cy="6.5" r=".5"/><circle cx="17.5" cy="10.5" r=".5"/><circle cx="8.5" cy="7.5" r=".5"/><circle cx="6.5" cy="12.5" r=".5"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.93 0 1.65-.75 1.65-1.69 0-.44-.18-.84-.44-1.13-.29-.29-.44-.65-.44-1.13a1.64 1.64 0 0 1 1.67-1.67h2c3.05 0 5.56-2.5 5.56-5.56C22 6.01 17.46 2 12 2z"/>',
    "zap": '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
}

LOGO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#5fa2ff"/><stop offset="1" stop-color="#7a5cff"/></linearGradient></defs>
<rect x="3" y="3" width="58" height="58" rx="15" fill="url(#g)"/>
<path d="M32 15v22M23 29l9 9 9-9" fill="none" stroke="#fff" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M18 44v3a3 3 0 0 0 3 3h22a3 3 0 0 0 3-3v-3" fill="none" stroke="#fff" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""


def svg_markup(name, color, stroke=1.9):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="%s" '
            'stroke-width="%s" stroke-linecap="round" stroke-linejoin="round">%s</svg>' % (color, stroke, ICON_PATHS[name]))


def svg_pixmap(svg, size, dpr=2.0):
    r = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    px = QPixmap(int(size * dpr), int(size * dpr))
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r.render(p)
    p.end()
    px.setDevicePixelRatio(dpr)
    return px


def icon(name, color=None, size=20, stroke=1.9):
    col = P.get(color, color) if color else P["icon"]
    ic = QIcon()
    ic.addPixmap(svg_pixmap(svg_markup(name, col, stroke), size))
    return ic


def set_icon(w, name, color="icon", size=18, stroke=1.9):
    """Give a button an icon that follows the theme."""
    def f():
        w.setIcon(icon(name, color, size, stroke))
        w.setIconSize(QSize(size, size))
    themed(f)


def logo_pixmap(size):
    return svg_pixmap(LOGO_SVG, size)


def app_icon():
    p = resource_path("app_icon.ico")
    if os.path.exists(p):
        return QIcon(p)
    ic = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        ic.addPixmap(svg_pixmap(LOGO_SVG, s, 1.0))
    return ic


_ARROWS = {}


def check_file(on):
    """Checkbox images for tree rows (rounded, accent when ticked)."""
    import tempfile
    col = P["accent"] if on else P["muted"]
    key = ("chk", on, col, P["surface"])
    if key not in _ARROWS:
        d = os.path.join(tempfile.gettempdir(), "package_downloader_icons")
        os.makedirs(d, exist_ok=True)
        f = os.path.join(d, "chk_%d_%s_%s.svg" % (on, col.strip("#"), P["surface"].strip("#")))
        if on:
            svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18"><rect x="1" y="1" width="16" '
                   'height="16" rx="4.5" fill="%s"/><path d="M5 9.2l2.6 2.6L13 6.4" fill="none" stroke="%s" '
                   'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>' % (col, P["on_accent"]))
        else:
            svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18"><rect x="1.5" y="1.5" width="15" '
                   'height="15" rx="4.5" fill="none" stroke="%s" stroke-width="1.4"/></svg>' % col)
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(svg)
        _ARROWS[key] = f.replace("\\", "/")
    return _ARROWS[key]


def arrow_file(name, color):
    """QSS needs image files for combo arrows; write tiny SVGs to the temp folder."""
    key = (name, color)
    if key not in _ARROWS:
        import tempfile
        d = os.path.join(tempfile.gettempdir(), "package_downloader_icons")
        os.makedirs(d, exist_ok=True)
        f = os.path.join(d, "%s_%s.svg" % (name, color.strip("#")))
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(svg_markup(name, color, 2.2))
        _ARROWS[key] = f.replace("\\", "/")
    return _ARROWS[key]


def style_titlebar(widget):
    """Dark/light native title bar, tinted to match the app (Windows 10/11)."""
    if not IS_WIN or not P:
        return
    try:
        import ctypes
        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi
        val = ctypes.c_int(1 if P["theme"] == "dark" else 0)
        for attr in (20, 19):
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break

        def colorref(hexc):
            c = QColor(hexc)
            return ctypes.c_int(c.red() | (c.green() << 8) | (c.blue() << 16))
        cap, txt = colorref(P["chrome"]), colorref(P["fg"])
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap), ctypes.sizeof(cap))
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(txt), ctypes.sizeof(txt))
    except Exception:
        pass


def qt_palette():
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: P["bg"], QPalette.ColorRole.WindowText: P["fg"],
        QPalette.ColorRole.Base: P["surface"], QPalette.ColorRole.AlternateBase: P["surface"],
        QPalette.ColorRole.Text: P["fg"], QPalette.ColorRole.Button: P["surface"],
        QPalette.ColorRole.ButtonText: P["fg"], QPalette.ColorRole.Highlight: P["accent"],
        QPalette.ColorRole.HighlightedText: P["on_accent"], QPalette.ColorRole.ToolTipBase: P["surface"],
        QPalette.ColorRole.ToolTipText: P["fg"], QPalette.ColorRole.PlaceholderText: P["faint"],
        QPalette.ColorRole.Link: P["accent"],
    }
    for role, col in roles.items():
        pal.setColor(role, QColor(col))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(P["faint"]))
    return pal


def build_qss():
    down = arrow_file("chevron-down", P["muted"])
    right = arrow_file("chevron-right", P["muted"])
    return """
QMainWindow, QDialog {{ background: {bg}; }}
QWidget {{ color: {fg}; font-family: "Segoe UI Variable Text", "Segoe UI", "Inter", sans-serif; font-size: 10pt; }}
#chrome {{ background: {chrome}; border-bottom: 1px solid {border}; }}
#brand {{ font-size: 12pt; font-weight: 700; }}
#brandSub {{ color: {muted}; font-size: 8.5pt; }}
#page {{ background: {bg}; }}
#card {{ background: {surface}; border: 1px solid {border}; border-radius: 14px; }}
#card QLabel {{ background: transparent; }}
#soft {{ background: {th}; border: 1px solid {border}; border-radius: 12px; }}
#soft QLabel, #soft QWidget {{ background: transparent; }}
#okBox {{ background: {ok_soft}; border: 1px solid {ok_soft}; border-radius: 12px; }}
#badBox {{ background: {bad_soft}; border: 1px solid {bad_soft}; border-radius: 12px; }}
#warnBox {{ background: {warn_soft}; border: 1px solid {warn_soft}; border-radius: 12px; }}
#okBox QLabel, #badBox QLabel, #warnBox QLabel, #okBox QWidget, #badBox QWidget, #warnBox QWidget {{ background: transparent; }}
#h1 {{ font-size: 17pt; font-weight: 700; }}
#h2 {{ font-size: 13pt; font-weight: 700; }}
#h3 {{ font-size: 10.5pt; font-weight: 650; }}
#muted {{ color: {muted}; font-size: 9pt; }}
#faint {{ color: {faint}; font-size: 8.5pt; }}
#section {{ color: {muted}; font-size: 8pt; font-weight: 700; letter-spacing: 0.6px; }}
#okText {{ color: {ok}; font-weight: 650; }}
#badText {{ color: {bad}; font-weight: 650; }}
#warnText {{ color: {warn}; }}
#accentText {{ color: {accent}; font-weight: 700; }}
#big {{ font-size: 20pt; font-weight: 700; color: {accent}; }}
#stat {{ font-size: 11pt; font-weight: 700; }}
#count {{ background: {accent_soft}; color: {accent}; border-radius: 10px; padding: 1px 9px; font-weight: 700; font-size: 9pt; }}
#line {{ background: {border}; }}
#mono {{ font-family: "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace; font-size: 8.5pt; color: {muted}; }}

QToolButton#tb {{ background: transparent; border: 1px solid transparent; border-radius: 9px; padding: 6px 10px;
    font-weight: 600; color: {fg}; }}
QToolButton#tb:hover {{ background: {hover}; }}
QToolButton#tb:pressed {{ background: {pressed}; }}
QToolButton#tb:disabled {{ color: {faint}; }}
QToolButton#tb::menu-indicator {{ image: none; width: 0; }}
QToolButton#iconBtn {{ background: transparent; border: none; border-radius: 8px; padding: 4px; }}
QFrame#fileChip {{ background: {input}; border: 1px solid {border}; border-radius: 16px; }}
QFrame#fileChip:hover {{ border-color: {muted}; }}
QToolButton#chipX {{ background: transparent; border: none; border-radius: 10px; padding: 2px; }}
QToolButton#chipX:hover {{ background: {hover}; }}
QToolButton#iconBtn:hover {{ background: {hover}; }}
QToolButton#verBtn {{ background: {th}; border: 1px solid {border}; border-radius: 9px; padding: 3px 8px 3px 10px;
    font-size: 9pt; font-weight: 600; color: {fg}; }}
QToolButton#verBtn:hover {{ border-color: {muted}; }}
QToolButton#verBtn[saved="true"] {{ color: {ok}; }}
QToolButton#verBtn::menu-indicator {{ image: none; width: 0; }}

#segment {{ background: {seg_bg}; border: 1px solid {border}; border-radius: 12px; }}
QPushButton#segbtn {{ background: transparent; border: none; border-radius: 9px; padding: 7px 16px;
    color: {muted}; font-weight: 600; min-width: 40px; }}
QPushButton#segbtn:hover {{ color: {fg}; }}
QPushButton#segbtn:checked {{ background: {seg_active}; color: {fg}; }}

QPushButton {{ background: {input}; border: 1px solid {border}; border-radius: 10px; padding: 8px 16px; font-weight: 600; }}
QPushButton:hover {{ background: {hover}; }}
QPushButton:pressed {{ background: {pressed}; }}
QPushButton:disabled {{ color: {faint}; background: {th}; }}
QPushButton#primary {{ background: {accent}; border-color: {accent}; color: {on_accent}; }}
QPushButton#primary:hover {{ background: {accent_hover}; border-color: {accent_hover}; }}
QPushButton#primary:disabled {{ background: {accent_soft}; border-color: transparent; color: {muted}; }}
QPushButton#bigPrimary {{ background: {accent}; border: none; color: {on_accent}; border-radius: 12px;
    padding: 12px 18px; font-size: 10.5pt; font-weight: 700; }}
QPushButton#bigPrimary:hover {{ background: {accent_hover}; }}
QPushButton#bigPrimary:disabled {{ background: {accent_soft}; color: {muted}; }}
QPushButton#ghost {{ background: transparent; border-color: transparent; color: {muted}; }}
QPushButton#ghost:hover {{ background: {hover}; color: {fg}; }}
QPushButton#danger {{ background: transparent; color: {bad}; }}
QPushButton#danger:hover {{ border-color: {bad}; background: {bad_soft}; }}
QPushButton#chip {{ background: {input}; border: 1px solid {border}; border-radius: 15px; padding: 0 14px;
    min-height: 28px; max-height: 28px;
    font-weight: 600; font-size: 9.5pt; min-width: 30px; }}
QPushButton#chip:hover {{ border-color: {muted}; }}
QPushButton#chip:checked {{ background: {accent}; border-color: {accent}; color: {on_accent}; }}
QPushButton#link {{ background: transparent; border: none; padding: 0; color: {accent}; font-weight: 600; }}
QPushButton#link:hover {{ text-decoration: underline; }}

QLineEdit {{ background: {input}; border: 1px solid {border}; border-radius: 10px; padding: 7px 12px;
    selection-background-color: {sel}; selection-color: {fg}; }}
QLineEdit:hover {{ border-color: {muted}; }}
QLineEdit:focus {{ border-color: {accent}; }}
QLineEdit#bigSearch {{ font-size: 11.5pt; padding: 10px 14px; border-radius: 12px; }}
QComboBox {{ background: {input}; border: 1px solid {border}; border-radius: 10px; padding: 7px 12px; min-height: 18px; }}
QComboBox:hover {{ border-color: {muted}; }}
QComboBox:focus {{ border-color: {accent}; }}
QComboBox:disabled {{ color: {faint}; }}
QComboBox::drop-down {{ border: none; width: 28px; }}
QComboBox::down-arrow {{ image: url("{down}"); width: 14px; height: 14px; }}
QComboBox QAbstractItemView {{ background: {surface}; border: 1px solid {border}; border-radius: 8px;
    selection-background-color: {hover}; selection-color: {fg}; outline: 0; padding: 4px; }}

QTreeWidget {{ background: {surface}; border: none; outline: 0; selection-background-color: transparent; }}
QTreeWidget::item {{ padding: 0 4px; border: none; min-height: 34px; }}
QTreeWidget::item:hover {{ background: {row_hover}; }}
QTreeWidget::item:selected {{ background: {sel}; color: {fg}; }}
QTreeWidget::branch {{ background: transparent; }}
QTreeWidget::branch:has-children:closed {{ image: url("{right}"); }}
QTreeWidget::branch:has-children:open {{ image: url("{down}"); }}
QTreeWidget::indicator {{ width: 18px; height: 18px; }}
QTreeWidget::indicator:unchecked {{ image: url("{chk0}"); }}
QTreeWidget::indicator:checked {{ image: url("{chk1}"); }}
QHeaderView {{ background: {surface}; border: none; }}
QHeaderView::section {{ background: {surface}; color: {muted}; border: none; border-bottom: 1px solid {border};
    padding: 8px 8px; font-size: 8.5pt; font-weight: 700; }}
QHeaderView::section:hover {{ color: {fg}; }}

QMenu {{ background: {surface}; border: 1px solid {border}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 28px 7px 10px; border-radius: 6px; background: transparent; }}
QMenu::item:selected {{ background: {hover}; }}
QMenu::item:disabled {{ color: {faint}; }}
QMenu::separator {{ height: 1px; background: {border}; margin: 5px 8px; }}
QToolTip {{ background: {surface}; color: {fg}; border: 1px solid {border}; padding: 6px 9px; border-radius: 7px; }}
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {scroll}; border-radius: 4px; min-height: 36px; margin: 2px 3px; }}
QScrollBar::handle:vertical:hover {{ background: {muted}; }}
QScrollBar:horizontal {{ background: {th}; height: 14px; margin: 0; border-radius: 7px; }}
QScrollBar::handle:horizontal {{ background: {scroll}; border-radius: 5px; min-width: 48px; margin: 2px 2px; }}
QScrollBar::handle:horizontal:hover {{ background: {muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QScrollArea, QScrollArea > QWidget > QWidget#scrollBody {{ background: transparent; border: none; }}
QPlainTextEdit#log {{ background: {log_bg}; color: {log_fg}; border: 1px solid {border}; border-radius: 10px;
    padding: 8px; font-family: "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace; font-size: 8.5pt;
    selection-background-color: {sel}; }}
QStatusBar {{ background: {chrome}; border-top: 1px solid {border}; min-height: 28px; }}
QStatusBar::item {{ border: none; }}
#status {{ color: {muted}; font-size: 8.5pt; }}
QPushButton#statusLink {{ background: transparent; border: none; border-radius: 6px; padding: 3px 8px;
    color: {muted}; font-size: 8.5pt; font-weight: 400; }}
QPushButton#statusLink:hover {{ background: {hover}; color: {fg}; }}
#toast {{ background: {fg}; color: {bg}; border-radius: 12px; padding: 9px 16px; font-weight: 600; }}
#dlgHead {{ background: {bg}; }}
#dlgFoot {{ background: {chrome}; border-top: 1px solid {border}; }}
#row {{ background: transparent; }}
#bundleRow {{ background: transparent; border-radius: 10px; }}
#bundleRow:hover {{ background: {row_hover}; }}
#dropZone {{ background: {th}; border: 2px dashed {border}; border-radius: 16px; }}
#dropZone[hot="true"] {{ background: {accent_softer}; border-color: {accent}; }}
#dropZone QLabel {{ background: transparent; }}
""".format(down=down, right=right, chk0=check_file(False), chk1=check_file(True), **P)


# =========================================================================================
#  Small widgets
# =========================================================================================

def lbl(text="", obj=None, wrap=False, sel=False):
    w = QLabel(text)
    if obj:
        w.setObjectName(obj)
    if wrap:
        w.setWordWrap(True)
    if sel:
        w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return w


def hbox(*items, spacing=8, margins=(0, 0, 0, 0)):
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, int):
            lay.addSpacing(it)
        elif isinstance(it, QLayout):
            lay.addLayout(it)
        else:
            lay.addWidget(it)
    return lay


def vbox(*items, spacing=8, margins=(0, 0, 0, 0)):
    lay = QVBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, int):
            lay.addSpacing(it)
        elif isinstance(it, QLayout):
            lay.addLayout(it)
        else:
            lay.addWidget(it)
    return lay


def frame(obj=None, layout=None):
    f = QFrame()
    if obj:
        f.setObjectName(obj)
    if layout is not None:
        f.setLayout(layout)
    return f


def button(text, kind=None, ico=None, tip=None, slot=None, size=16):
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if ico:
        col = {"primary": "on_accent", "bigPrimary": "on_accent", "danger": "bad", "ghost": "muted"}.get(kind, "icon")
        set_icon(b, ico, col, size)
    if tip:
        b.setToolTip(tip)
    if slot:
        b.clicked.connect(slot)
    return b


def tool(text, ico, tip=None, slot=None, obj="tb", size=18):
    b = QToolButton()
    b.setObjectName(obj)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if text:
        b.setText(text)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    set_icon(b, ico, "icon", size)
    if tip:
        b.setToolTip(tip)
    if slot:
        b.clicked.connect(slot)
    return b


def icon_label(name, color="muted", size=18):
    w = QLabel()
    w.setFixedSize(size, size)
    themed(lambda: w.setPixmap(svg_pixmap(svg_markup(name, P.get(color, color)), size)))
    return w


def hline():
    f = QFrame()
    f.setObjectName("line")
    f.setFixedHeight(1)
    return f


def repolish(w):
    w.style().unpolish(w)
    w.style().polish(w)


def card(margins=(20, 18, 20, 18), spacing=10, obj="card"):
    f = QFrame()
    f.setObjectName(obj)
    lay = QVBoxLayout(f)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    return f, lay


class ToggleSwitch(QAbstractButton):
    """Animated pill switch."""

    def __init__(self, on=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(40, 22)
        self._p = 1.0 if on else 0.0
        self.setChecked(on)
        self._anim = QPropertyAnimation(self, b"progress", self)
        self._anim.setDuration(170)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, on):
        self._anim.stop()
        self._anim.setStartValue(self._p)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def _get(self):
        return self._p

    def _set(self, v):
        self._p = v
        self.update()

    progress = Property(float, _get, _set)

    def sizeHint(self):
        return QSize(40, 22)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.45)
        off, on, t = QColor(P["switch_off"]), QColor(P["accent"]), self._p
        track = QColor.fromRgbF(off.redF() + (on.redF() - off.redF()) * t,
                                off.greenF() + (on.greenF() - off.greenF()) * t,
                                off.blueF() + (on.blueF() - off.blueF()) * t)
        w, h = self.width(), self.height()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        d = h - 6
        x = 3 + (w - 6 - d) * t
        p.setBrush(QColor(0, 0, 0, 45))
        p.drawEllipse(QRectF(x, 3.7, d, d))
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(x, 3, d, d))
        p.end()


class Segmented(QFrame):
    """A row of options where one is picked (segmented control)."""
    changed = Signal(str)

    def __init__(self, options, value, icons=None, small=False):
        super().__init__()
        self.setObjectName("segment")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.btns = {}
        for key, text in options:
            b = QPushButton(text)
            b.setObjectName("segbtn")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            if small:
                b.setStyleSheet("padding: 4px 12px; font-size: 9pt;")
            if icons and key in icons:
                ic_name = icons[key]

                def f(b=b, n=ic_name):
                    ic = QIcon()
                    ic.addPixmap(svg_pixmap(svg_markup(n, P["muted"]), 16), QIcon.Mode.Normal, QIcon.State.Off)
                    ic.addPixmap(svg_pixmap(svg_markup(n, P["accent"]), 16), QIcon.Mode.Normal, QIcon.State.On)
                    b.setIcon(ic)
                    b.setIconSize(QSize(16, 16))
                themed(f)
            self.group.addButton(b)
            lay.addWidget(b)
            self.btns[key] = b
            b.clicked.connect(lambda _=False, k=key: self.changed.emit(k))
        self.set(value)

    def set(self, key):
        if key in self.btns:
            self.btns[key].setChecked(True)

    def value(self):
        for k, b in self.btns.items():
            if b.isChecked():
                return k
        return None


class ProgressBar(QWidget):
    """Rounded progress bar with a smooth indeterminate mode."""

    def __init__(self, height=8):
        super().__init__()
        self.setFixedHeight(height)
        self.frac, self.busy, self._t = 0.0, False, 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def set(self, frac):
        self.busy = False
        self._timer.stop()
        self.frac = max(0.0, min(1.0, frac))
        self.update()

    def indeterminate(self):
        if not self.busy:
            self.busy = True
            self._timer.start(16)

    def _tick(self):
        self._t = (self._t + 0.012) % 1.4
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(P["border"]))
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        p.setBrush(QColor(P["accent"]))
        if self.busy:
            seg = w * 0.32
            x = (self._t - 0.3) * w
            a, b = max(0.0, x), min(float(w), x + seg)
            if b > a:
                p.setClipRect(QRectF(0, 0, w, h))
                path = QPainterPath()
                path.addRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
                p.setClipPath(path)
                p.drawRoundedRect(QRectF(a, 0, b - a, h), h / 2, h / 2)
        elif self.frac > 0:
            fw = max(h, w * self.frac)
            p.drawRoundedRect(QRectF(0, 0, fw, h), h / 2, h / 2)
        p.end()


class FlowLayout(QLayout):
    """Lays widgets out left to right and wraps to new lines (for chips)."""

    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self.items = []
        self.sp = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self.items.append(item)

    def count(self):
        return len(self.items)

    def itemAt(self, i):
        return self.items[i] if 0 <= i < len(self.items) else None

    def takeAt(self, i):
        return self.items.pop(i) if 0 <= i < len(self.items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._do(QRect(0, 0, w, 0), True)

    def setGeometry(self, r):
        super().setGeometry(r)
        self._do(r, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self.items:
            s = s.expandedTo(it.minimumSize())
        return s

    def _do(self, r, test):
        x, y, lh = r.x(), r.y(), 0
        for it in self.items:
            sz = it.sizeHint()
            nx = x + sz.width() + self.sp
            if nx - self.sp > r.right() + 1 and lh > 0:
                x, y = r.x(), y + lh + self.sp
                nx = x + sz.width() + self.sp
                lh = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), sz))
            x, lh = nx, max(lh, sz.height())
        return y + lh - r.y()


class Toast(QLabel):
    """A short message that fades in at the bottom of a window and goes away by itself."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("toast")
        self.hide()
        self.eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.eff)
        self.anim = QPropertyAnimation(self.eff, b"opacity", self)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._fade)

    def show_msg(self, text, ms=2200):
        self.setText(text)
        self.adjustSize()
        par = self.parentWidget()
        self.move((par.width() - self.width()) // 2, par.height() - self.height() - 96)
        self.raise_()
        self.show()
        self.anim.stop()
        self.anim.setDuration(160)
        self.anim.setStartValue(self.eff.opacity() if self.isVisible() else 0.0)
        self.anim.setEndValue(1.0)
        self.anim.start()
        self.timer.start(ms)

    def _fade(self):
        self.anim.stop()
        self.anim.setDuration(400)
        self.anim.setStartValue(1.0)
        self.anim.setEndValue(0.0)
        self.anim.start()


class PillDelegate(QStyledItemDelegate):
    """Draws a column's text as a small rounded badge. The item's UserRole+1 says the colour:
    ok, warn, bad, accent, muted."""
    ROLE = Qt.ItemDataRole.UserRole + 1

    def paint(self, painter, option, index):
        text = index.data(Qt.ItemDataRole.DisplayRole)
        kind = index.data(self.ROLE)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        if not text:
            return
        if not kind:
            painter.save()
            painter.setPen(QColor(P["muted"]))
            painter.drawText(option.rect.adjusted(8, 0, -4, 0), Qt.AlignmentFlag.AlignVCenter, text)
            painter.restore()
            return
        fg = QColor(P.get(kind, P["muted"]))
        bgc = QColor(fg)
        bgc.setAlphaF(0.15 if kind != "muted" else 0.12)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = QFont(option.font)
        f.setPointSizeF(max(7.5, f.pointSizeF() - 1.2))
        f.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f)
        fm = QFontMetricsF(f)
        tw = fm.horizontalAdvance(text) + 2
        h = fm.height() + 6
        r = option.rect
        maxw = r.width() - 12
        w = tw + 20
        if w > maxw:
            w = maxw
            text = fm.elidedText(text, Qt.TextElideMode.ElideRight, w - 20)
        x, y = r.x() + 6, r.y() + (r.height() - h) / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bgc)
        painter.drawRoundedRect(QRectF(x, y, w, h), h / 2, h / 2)
        painter.setPen(fg)
        painter.drawText(QRectF(x, y, w, h), Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()


class NameDelegate(QStyledItemDelegate):
    """Package name in bold with a one-line description under it."""
    ROLE = Qt.ItemDataRole.UserRole + 3

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        return QSize(s.width(), 54)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        name = index.data(Qt.ItemDataRole.DisplayRole) or ""
        summ = index.data(self.ROLE) or ""
        r = QRectF(option.rect.adjusted(8, 0, -8, 0))
        painter.save()
        f1 = QFont(option.font)
        f1.setWeight(QFont.Weight.DemiBold)
        f2 = QFont(option.font)
        f2.setPointSizeF(max(7.5, f2.pointSizeF() - 1.3))
        fm1, fm2 = QFontMetricsF(f1), QFontMetricsF(f2)
        total = fm1.height() + (fm2.height() + 1 if summ else 0)
        y = r.y() + (r.height() - total) / 2
        painter.setFont(f1)
        painter.setPen(QColor(P["fg"]))
        painter.drawText(QRectF(r.x(), y, r.width(), fm1.height()), Qt.AlignmentFlag.AlignVCenter,
                         fm1.elidedText(name, Qt.TextElideMode.ElideRight, r.width()))
        if summ:
            painter.setFont(f2)
            painter.setPen(QColor(P["muted"]))
            painter.drawText(QRectF(r.x(), y + fm1.height() + 1, r.width(), fm2.height()), Qt.AlignmentFlag.AlignVCenter,
                             fm2.elidedText(summ, Qt.TextElideMode.ElideRight, r.width()))
        painter.restore()


class ActionDelegate(QStyledItemDelegate):
    """The 'Add' / 'Added' column: an outlined pill, filled when the package is in the bundle."""
    ROLE = Qt.ItemDataRole.UserRole + 2

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        state = index.data(self.ROLE)
        if state is None:
            return
        added = state == "in"
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = QFont(option.font)
        f.setPointSizeF(max(7.5, f.pointSizeF() - 1))
        f.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f)
        text = "Added" if added else "Add"
        fm = painter.fontMetrics()
        h = fm.height() + 8
        w = fm.horizontalAdvance("Added") + 40
        r = option.rect
        x, y = r.x() + (r.width() - w) / 2, r.y() + (r.height() - h) / 2
        acc = QColor(P["accent"])
        if added:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(acc)
            fg = QColor(P["on_accent"])
        else:
            painter.setPen(QPen(acc if hover else QColor(P["border"]), 1.2))
            painter.setBrush(qcolor(P["accent_softer"]) if hover else Qt.BrushStyle.NoBrush)
            fg = acc
        painter.drawRoundedRect(QRectF(x, y, w, h), h / 2, h / 2)
        ic = svg_pixmap(svg_markup("check" if added else "plus", fg.name(), 2.4), 13)
        tw = fm.horizontalAdvance(text)
        cx = x + (w - (13 + 5 + tw)) / 2
        painter.drawPixmap(int(cx), int(y + (h - 13) / 2), ic)
        painter.setPen(fg)
        painter.drawText(QRectF(cx + 18, y, tw + 2, h), Qt.AlignmentFlag.AlignVCenter, text)
        painter.restore()


def make_tree(columns, widths, stretch=None, root=False, hscroll=False):
    t = QTreeWidget()
    t.setColumnCount(len(columns))
    t.setHeaderLabels(columns)
    t.setRootIsDecorated(root)
    t.setUniformRowHeights(True)
    t.setAllColumnsShowFocus(True)
    t.setMouseTracking(True)
    t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    t.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded if hscroll
                                   else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    t.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    t.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    t.setIndentation(18 if root else 0)
    h = t.header()
    h.setStretchLastSection(hscroll)
    h.setHighlightSections(False)
    h.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    for i, w in enumerate(widths):
        t.setColumnWidth(i, w)
        h.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch if i == stretch else QHeaderView.ResizeMode.Interactive)
    return t


class FileChip(QFrame):
    """A loaded file: icon, name, and an X to take it out again."""

    def __init__(self, path, on_remove):
        super().__init__()
        self.setObjectName("fileChip")
        self.setFixedHeight(32)
        self.setToolTip(path)
        is_dir = os.path.isdir(path)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(11, 0, 5, 0)
        lay.setSpacing(7)
        lay.addWidget(icon_label("folder" if is_dir else "file-code", "accent", 15))
        name = QLabel(os.path.basename(os.path.normpath(path)) or path)
        name.setStyleSheet("font-weight: 600; background: transparent; border: none;")
        lay.addWidget(name)
        self.count = QLabel("")
        self.count.setObjectName("faint")
        self.count.setStyleSheet("background: transparent; border: none;")
        lay.addWidget(self.count)
        x = tool("", "x", "Remove this folder" if is_dir else "Remove this file", lambda: on_remove(path),
                 obj="chipX", size=14)
        x.setFixedSize(22, 22)
        lay.addWidget(x)


class EmptyState(QWidget):
    """Big icon + title + text, shown where a list would be empty."""

    def __init__(self, ico, title, text, action=None, compact=False):
        super().__init__()
        lay = QVBoxLayout(self)
        m = 8 if compact else 30
        lay.setContentsMargins(m, m, m, m)
        lay.addStretch(1)
        bubble = QLabel()
        bs = 44 if compact else 64
        bubble.setFixedSize(bs, bs)
        bubble.setAlignment(Qt.AlignmentFlag.AlignCenter)

        def f():
            bubble.setStyleSheet("background: %s; border-radius: %dpx;" % (P["accent_softer"], bs // 2))
            bubble.setPixmap(svg_pixmap(svg_markup(ico, P["accent"], 1.7), bs * 15 // 32))
        themed(f)
        lay.addWidget(bubble, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(6 if compact else 10)
        self.t = lbl(title, "h3")
        self.t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.t)
        self.s = lbl(self._wrap(text), "muted")
        self.s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.s, 0, Qt.AlignmentFlag.AlignHCenter)
        if action:
            lay.addSpacing(8)
            lay.addWidget(action, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addStretch(2)

    @staticmethod
    def _wrap(text):
        # explicit line breaks: word-wrapped labels get clipped inside stretchy layouts
        import textwrap
        return "\n".join(l for para in text.split("\n") for l in (textwrap.wrap(para, 62) or [""]))

    def set_text(self, title, text):
        self.t.setText(title)
        self.s.setText(self._wrap(text))


class Dialog(QDialog):
    """Themed dialog: title + subtitle head, body, button footer."""

    def __init__(self, parent, title, sub="", width=560):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(width)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        head = frame("dlgHead", vbox(lbl(title, "h2"), *( [lbl(sub, "muted", wrap=True)] if sub else []),
                                     spacing=4, margins=(26, 22, 26, 8)))
        outer.addWidget(head)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(26, 10, 26, 20)
        self.body.setSpacing(12)
        outer.addLayout(self.body, 1)
        self.foot = QHBoxLayout()
        self.foot.setContentsMargins(20, 12, 20, 12)
        self.foot.setSpacing(8)
        outer.addWidget(frame("dlgFoot", self.foot))

    def showEvent(self, e):
        style_titlebar(self)
        super().showEvent(e)


def confirm(parent, title, text, ok="OK", danger=False, cancel="Cancel"):
    d = Dialog(parent, title, width=460)
    d.body.addWidget(lbl(text, wrap=True))
    d.foot.addStretch(1)
    if cancel:
        d.foot.addWidget(button(cancel, slot=d.reject))
    b = button(ok, "danger" if danger else "primary", slot=d.accept)
    b.setDefault(True)
    d.foot.addWidget(b)
    return d.exec() == QDialog.DialogCode.Accepted


class Bridge(QObject):
    """Lets worker threads run code on the window thread."""
    call = Signal(object, object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @Slot(object, object)
    def _run(self, fn, args):
        try:
            fn(*args)
        except RuntimeError:
            pass                        # the widget it wanted to update is gone
        except Exception:
            traceback.print_exc()

    def __call__(self, fn, *args):
        self.call.emit(fn, args)


KIND_TXT = {"pure": ("Any Python", "ok"), "windows": ("Windows build", "accent"),
            "source": ("Source only", "warn"), "other": ("No Windows build", "bad")}


def human_t(s):
    if s is None:
        return "…"
    s = int(round(s))
    if s < 60:
        return "%d s" % s
    m = s // 60
    if m < 60:
        return "%d min %02d s" % (m, s % 60)
    return "%d h %d min" % (m // 60, m % 60)


def fmt_dl(n):
    if not n:
        return ""
    for d, u in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= d:
            return ("%.1f%s" % (n / d, u)) if u != "K" else ("%.0f%s" % (n / d, u))
    return str(n)


def open_in_explorer(path):
    try:
        if IS_WIN:
            if os.path.isfile(path):
                import subprocess
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(path)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path if os.path.isdir(path) else os.path.dirname(path)))
    except Exception:
        pass


def make_app():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(app_icon())
    app.setStyle("Fusion")
    f = QFont("Segoe UI Variable Text" if IS_WIN else "Inter")
    f.setPointSizeF(10)
    app.setFont(f)
    apply_theme(app)
    try:
        QGuiApplication.styleHints().colorSchemeChanged.connect(
            lambda *_: SETTINGS.get("theme", "system") == "system" and apply_theme(app))
    except Exception:
        pass
    return app


# =========================================================================================
#  Downloader window (runs on the PC with internet)
# =========================================================================================

VERSIONS = ["3.8", "3.9", "3.10", "3.11", "3.12", "3.13", "3.14", "3.15"]
PLATS = [("win_amd64", "64-bit"), ("win32", "32-bit"), ("win_arm64", "ARM")]
NAME_ROLE = Qt.ItemDataRole.UserRole


class ClickFrame(QFrame):
    clicked = Signal()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)


class BundleRow(QFrame):
    """One package in the bundle: name, where it will come from, a version picker and remove."""

    def __init__(self, win, b):
        super().__init__()
        self.win, self.b = win, b
        self.setObjectName("bundleRow")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 6, 7)
        lay.setSpacing(10)
        lay.addWidget(icon_label("package", "muted", 18))
        txt = QVBoxLayout()
        txt.setSpacing(0)
        name = lbl(b["name"] + (b.get("extra") or ""), "h3")
        name.setToolTip(b["name"])
        txt.addWidget(name)
        saved = win.lib.versions(b["name"])
        ver = b.get("ver")
        if ver and ver in saved:
            sub, saved_flag = "In the library, no download", True
        elif not ver and saved:
            best = sorted(saved, key=_vkey, reverse=True)[0]
            sub, saved_flag = "Library has %s" % best, False
        else:
            sub, saved_flag = "Will be downloaded", False
        if b.get("pin"):
            sub = "Version " + b["pin"]
        self.sub = lbl(sub, "faint")
        txt.addWidget(self.sub)
        lay.addLayout(txt, 1)
        vb = QToolButton()
        vb.setObjectName("verBtn")
        vb.setCursor(Qt.CursorShape.PointingHandCursor)
        vb.setText(ver or (b.get("pin") or "Newest"))
        vb.setProperty("saved", "true" if saved_flag else "false")
        vb.setToolTip("Pick a version")
        vb.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        vb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        vb.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        set_icon(vb, "chevron-down", "muted", 13)
        self.menu = QMenu(vb)
        self.menu.aboutToShow.connect(self._fill_menu)
        vb.setMenu(self.menu)
        lay.addWidget(vb)
        x = tool("", "x", "Remove from the bundle", lambda: win.remove_pkg(b["name"]), obj="iconBtn", size=15)
        lay.addWidget(x)

    def _fill_menu(self):
        m = self.menu
        m.clear()
        saved = sorted(self.win.lib.versions(self.b["name"]), key=_vkey, reverse=True)
        info = self.win.pypi.info_cache.get(canon(self.b["name"]))

        def pick(v):
            self.b["ver"] = v
            if v:
                self.b["pin"] = ""
            self.win.render_bundle()
        a = m.addAction("Newest" + (" (%s)" % info["version"] if info and info.get("version") else ""))
        a.triggered.connect(lambda: pick(""))
        if saved:
            m.addSection("In the library")
            for v in saved:
                a = m.addAction(icon("check", "ok", 14), v)
                a.triggered.connect(lambda _=False, v=v: pick(v))
        if info and info.get("versions"):
            m.addSection("On PyPI")
            for v in [v for v in info["versions"] if v not in saved][:30]:
                a = m.addAction(v)
                a.triggered.connect(lambda _=False, v=v: pick(v))
        elif info is None:
            a = m.addAction("Loading versions…")
            a.setEnabled(False)

            def work():
                self.win.pypi.info(self.b["name"])
            self.win.pool.submit(work)


class DownloaderWindow(QMainWindow):
    def __init__(self, files=None):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.setAcceptDrops(True)
        self.ui_call = Bridge()
        self.pool = ThreadPoolExecutor(8)
        self.pypi = PyPI()
        self.lib = library()
        self.bi = build_info()
        self.bundle = []        # starts empty every time (and again whenever the loaded files change)
        tg = SETTINGS.get("targets") or [self.bi["python"]]
        vs = list(VERSIONS) + ([self.bi["python"]] if self.bi["python"] not in VERSIONS else [])
        self.targets = {v: (v in tg) for v in vs}
        self.plat = SETTINGS.get("plat") or "win_amd64"
        if self.plat not in dict(PLATS):
            self.plat = "win_amd64"
        self.offline_only = bool(SETTINGS.get("offline_only"))
        self.pop_rows, self.pop_total, self.pop_upd = [], 0, ""
        self.items = {"pop": {}, "search": {}}
        self.lib_latest = {}
        self.scan = None
        self.scan_stem = ""
        self.log_lines = []
        self.log_dlg = None
        self.job_running = False
        self.job_since = 0
        self.last_result = None
        self._s_seq = 0

        self._build()
        self.toast = Toast(self.centralWidget())
        self.resize(1360, 880)
        geo = SETTINGS.get("geometry")
        if geo:
            try:
                self.restoreGeometry(QByteArray.fromHex(geo.encode()))
            except Exception:
                pass
        self.render_bundle(save=False)
        self.req_popular(True)
        self.render_library()
        self.go_page("file" if files else "pop")
        if files:
            QTimer.singleShot(200, lambda: self.scan_files(files))
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_filter)
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.choose_files)

    # ------------------------------------------------------------------ layout
    def _build(self):
        root = QWidget()
        root.setObjectName("page")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # top bar: brand | page switcher | theme + settings
        bar = frame("chrome")
        bar.setFixedHeight(68)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(20, 0, 16, 0)
        bl.setSpacing(12)
        logo = QLabel()
        logo.setPixmap(logo_pixmap(34))
        brand = QWidget()
        brand.setLayout(vbox(None, lbl(APP_NAME, "brand"), lbl("Python packages for PCs without internet", "brandSub"),
                             None, spacing=0))
        left = QWidget()
        left.setLayout(hbox(logo, brand, None, spacing=11))
        self.nav = Segmented([("pop", "Most downloaded"), ("search", "Search PyPI"), ("file", "From a file"),
                              ("lib", "Library")], "pop",
                             icons={"pop": "trending", "search": "search", "file": "file-code", "lib": "library"})
        self.nav.changed.connect(self.go_page)
        self.theme_btn = tool("", "moon", None, self.toggle_theme)
        self.theme_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        themed(self._theme_btn_look)
        sett = tool("Settings", "settings", "Theme, accent colour, library folder and more", self.open_settings)
        right = QWidget()
        right.setLayout(hbox(None, self.theme_btn, sett, spacing=4))
        left.setMinimumWidth(300)
        right.setMinimumWidth(300)
        bl.addWidget(left)
        bl.addStretch(1)
        bl.addWidget(self.nav, 0, Qt.AlignmentFlag.AlignVCenter)
        bl.addStretch(1)
        bl.addWidget(right, 0, Qt.AlignmentFlag.AlignRight)
        outer.addWidget(bar)

        body = QHBoxLayout()
        body.setContentsMargins(18, 18, 18, 18)
        body.setSpacing(18)
        outer.addLayout(body, 1)
        self.stack = QStackedWidget()
        self.pages = {}
        for key, build in (("pop", self._page_popular), ("search", self._page_search),
                           ("file", self._page_file), ("lib", self._page_library)):
            c, lay = card((24, 20, 24, 18), 12)
            build(lay)
            self.pages[key] = c
            self.stack.addWidget(c)
        body.addWidget(self.stack, 1)

        side = QScrollArea()
        side.setWidgetResizable(True)
        side.setFrameShape(QFrame.Shape.NoFrame)
        side.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        side.setFixedWidth(412)
        inner = QWidget()
        inner.setObjectName("scrollBody")
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, 0, 0)
        c, lay = card((20, 18, 20, 18), 10)
        self._build_side(lay)
        il.addWidget(c)
        side.setWidget(inner)
        body.addWidget(side)
        self.side = side

        # status bar
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        self.st_lib = button("", "statusLink", "library", "Open the package library folder",
                             lambda: open_in_explorer(self.lib.root), 14)
        sb.addWidget(self.st_lib)
        self.st_msg = lbl("", "status")
        sb.addWidget(self.st_msg, 1)
        sb.addPermanentWidget(lbl("v%s  " % APP_VERSION, "status"))

    def _theme_btn_look(self):
        dark = P["theme"] == "dark"
        self.theme_btn.setText("Light mode" if dark else "Dark mode")
        self.theme_btn.setIcon(icon("sun" if dark else "moon", "icon", 18))
        self.theme_btn.setIconSize(QSize(18, 18))
        self.theme_btn.setToolTip("Switch to %s mode (Settings can follow Windows instead)" % ("light" if dark else "dark"))

    def _header(self, lay, title, sub, right=None):
        t = QVBoxLayout()
        t.setSpacing(3)
        t.addWidget(lbl(title, "h1"))
        s = lbl(sub, "muted", wrap=True)
        t.addWidget(s)
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addLayout(t, 1)
        if right is not None:
            row.addWidget(right, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(row)
        return s

    def _filter_box(self, placeholder, slot, width=260):
        e = QLineEdit()
        e.setPlaceholderText(placeholder)
        e.setClearButtonEnabled(True)
        e.setFixedWidth(width)
        act = e.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition)
        themed(lambda: act.setIcon(icon("search", "faint", 16)))
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(280)
        timer.timeout.connect(slot)
        e.textChanged.connect(lambda *_: timer.start())
        return e

    def _pkg_tree(self, key):
        t = make_tree(["#", "Package", "Downloads / mo", "Latest", "Type", "In library", ""],
                      [46, 320, 112, 100, 132, 118, 108], stretch=1)
        pill = PillDelegate(t)
        t.setItemDelegateForColumn(1, NameDelegate(t))
        t.setItemDelegateForColumn(4, pill)
        t.setItemDelegateForColumn(5, pill)
        t.setItemDelegateForColumn(6, ActionDelegate(t))
        t.itemClicked.connect(lambda it, col: col == 6 and self.toggle_pkg(it.data(1, NAME_ROLE)))
        t.itemDoubleClicked.connect(lambda it, col: col != 6 and self.toggle_pkg(it.data(1, NAME_ROLE)))
        t.setProperty("key", key)
        return t

    # ---- pages
    def _page_popular(self, lay):
        self.pop_filter = self._filter_box("Filter by name…", lambda: self.req_popular(True))
        self.pop_sub = self._header(lay, "Most downloaded", "The packages people download most from PyPI.",
                                    self.pop_filter)
        self.pop_tv = self._pkg_tree("pop")
        lay.addWidget(self.pop_tv, 1)
        self.pop_more = button("Show more", slot=lambda: self.req_popular(False))
        lay.addLayout(hbox(lbl("Click Add, or double-click a row. Ctrl or Shift-click picks several.", "faint"), None,
                           self.pop_more, button("Add selected", "primary", "plus",
                                                 slot=lambda: self.add_selected(self.pop_tv))))

    def _page_search(self, lay):
        self._header(lay, "Search PyPI", "Every package on PyPI, the most downloaded first. The first search "
                                         "fetches the package list (about 10 MB); after that it's instant.")
        self.s_edit = QLineEdit()
        self.s_edit.setObjectName("bigSearch")
        self.s_edit.setPlaceholderText("Package name or a word, for example  excel,  pdf,  serial,  opencv")
        act = self.s_edit.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition)
        themed(lambda: act.setIcon(icon("search", "muted", 18)))
        self.s_edit.returnPressed.connect(self.search)
        st = QTimer(self)
        st.setSingleShot(True)
        st.setInterval(450)
        st.timeout.connect(lambda: len(self.s_edit.text().strip()) >= 2 and self.search())
        self.s_edit.textChanged.connect(lambda *_: st.start())
        lay.addLayout(hbox(self.s_edit, button("Search", "primary", slot=self.search), spacing=8))
        self.s_hint = lbl("", "muted")
        lay.addWidget(self.s_hint)
        self.s_stack = QStackedWidget()
        self.s_empty = EmptyState("search", "Search every package on PyPI",
                                  "Type a name, or a word that describes what you need. Results are sorted by how "
                                  "often people download them.")
        self.s_tv = self._pkg_tree("search")
        self.s_stack.addWidget(self.s_empty)
        self.s_stack.addWidget(self.s_tv)
        lay.addWidget(self.s_stack, 1)
        lay.addLayout(hbox(lbl("Click Add, or double-click a row.", "faint"), None,
                           button("Add selected", "primary", "plus", slot=lambda: self.add_selected(self.s_tv))))

    def _page_file(self, lay):
        self._header(lay, "From a file", "Finds the packages a script needs: import lines, pip install lines, "
                                         "requirements files and notebook cells.")
        dz = ClickFrame()
        dz.setObjectName("dropZone")
        dz.setCursor(Qt.CursorShape.PointingHandCursor)
        dz.clicked.connect(self.choose_files)
        dl = QHBoxLayout(dz)
        dl.setContentsMargins(26, 18, 26, 18)
        dl.setSpacing(18)
        bubble = QLabel()
        bubble.setFixedSize(52, 52)
        bubble.setAlignment(Qt.AlignmentFlag.AlignCenter)

        def f():
            bubble.setStyleSheet("background: %s; border-radius: 26px;" % P["accent_soft"])
            bubble.setPixmap(svg_pixmap(svg_markup("upload", P["accent"], 1.9), 24))
        themed(f)
        dl.addWidget(bubble)
        self.dz_title = lbl("Drop scripts or a whole folder here", "h3")
        self.dz_desc = lbl(".py  .pyw  .bat  .ps1  .ipynb  requirements.txt  -  add as many files and folders as you "
                           "like, now or later. The installer is named after the first one.", "muted", wrap=True)
        dl.addLayout(vbox(self.dz_title, self.dz_desc, spacing=3), 1)
        dl.addWidget(button("Choose files…", "primary", "file-code", "Pick one or more scripts", self.choose_files))
        dl.addWidget(button("Choose folder…", None, "folder", "Scan every script in a folder and its subfolders",
                            self.choose_folder))
        self.dz, self.dz_lay, self.dz_bubble = dz, dl, bubble
        lay.addWidget(dz)

        # the loaded files, each with an X, plus Clear all
        self.f_paths = []
        self.f_ticks = {}           # your own tick changes, kept when files are added or removed
        self.f_files = QWidget()
        fl = QVBoxLayout(self.f_files)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(8)
        self.f_files_lbl = lbl("FILES", "section")
        fl.addLayout(hbox(self.f_files_lbl, None,
                          button("Clear all", "ghost", "trash", "Remove every file and folder", self.clear_files),
                          spacing=4))
        chips = QWidget()
        self.f_chip_flow = FlowLayout(chips, 6)
        fl.addWidget(chips)
        self.f_files.hide()
        lay.addWidget(self.f_files)

        self.f_stack = QStackedWidget()
        self.f_empty = EmptyState("file-code", "No script loaded yet",
                                  "Drag scripts or a whole folder onto this window at any time, or onto "
                                  "Package Downloader.exe.")
        self.f_stack.addWidget(self.f_empty)
        res = QWidget()
        rl = QVBoxLayout(res)
        rl.setContentsMargins(0, 4, 0, 0)
        rl.setSpacing(10)
        self.f_title = lbl("", "h3")
        self.f_sub = lbl("", "muted", wrap=True)
        rl.addLayout(vbox(self.f_title, self.f_sub, spacing=2))
        self.f_tv = make_tree(["Package", "Type", "In library", "Found as", "What it is / notes"],
                              [200, 130, 110, 280, 420], hscroll=True)
        pill = PillDelegate(self.f_tv)
        self.f_tv.setItemDelegateForColumn(1, pill)
        self.f_tv.setItemDelegateForColumn(2, pill)
        self.f_tv.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.f_tv.itemChanged.connect(self._scan_ticked)
        rl.addWidget(self.f_tv, 1)
        self.f_notes = lbl("", "faint", wrap=True, sel=True)
        nbox = frame("soft", vbox(self.f_notes, margins=(14, 10, 14, 10)))
        rl.addWidget(nbox)
        self.f_stack.addWidget(res)
        lay.addWidget(self.f_stack, 1)
        self.f_hint = lbl("", "faint")
        self.f_add = button("Add ticked to bundle", "primary", "plus", slot=self.scan_add)
        self.f_add.setEnabled(False)
        lay.addLayout(hbox(self.f_hint, None, self.f_add))

    def _page_library(self, lay):
        self.l_filter = self._filter_box("Filter…", self.render_library, 220)
        self.l_sub = self._header(lay, "Library", "", self.l_filter)
        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.l_stats = {}
        for k, ic in (("Packages", "package"), ("Files", "list"), ("On disk", "folder")):
            v = lbl("0", "stat")
            t = frame("soft", hbox(icon_label(ic, "accent", 18), vbox(lbl(k, "faint"), v, spacing=0),
                                   spacing=10, margins=(14, 9, 18, 9)))
            self.l_stats[k] = v
            tiles.addWidget(t)
        tiles.addStretch(1)
        tiles.addWidget(button("Check for newer versions", None, "refresh", "Ask PyPI for the newest version of "
                                                                             "every package in the library",
                               self.lib_check))
        tiles.addWidget(button("Open folder", "ghost", "folder-open", slot=lambda: open_in_explorer(self.lib.files_dir)))
        tiles.addWidget(button("Move…", "ghost", "folder", "Choose another folder for the library",
                               self.change_library))
        lay.addLayout(tiles)
        self.l_stack = QStackedWidget()
        self.l_empty = EmptyState("library", "Your library is empty",
                                  "Every package you download is kept here with its exact version, and the next "
                                  "installer reuses it instead of downloading it again.")
        self.l_tv = make_tree(["Package", "Saved versions", "Files", "Size", "Newest on PyPI", "Status"],
                              [240, 190, 60, 90, 130, 220], stretch=5, root=True)
        self.l_tv.setItemDelegateForColumn(5, PillDelegate(self.l_tv))
        self.l_tv.itemDoubleClicked.connect(lambda *_: self.lib_add())
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self.l_tv, activated=self.lib_delete)
        self.l_stack.addWidget(self.l_empty)
        self.l_stack.addWidget(self.l_tv)
        lay.addWidget(self.l_stack, 1)
        lay.addLayout(hbox(lbl("Pick a package (newest saved version) or one file (that exact version).", "faint"),
                           None, button("Delete", "danger", "trash", slot=self.lib_delete),
                           button("Add to bundle", "primary", "plus", slot=self.lib_add)))

    def _build_side(self, lay):
        self.b_count = lbl("0", "count")
        lay.addLayout(hbox(lbl("Bundle", "h2"), self.b_count, None,
                           button("Clear", "ghost", "trash", "Empty the bundle", self.clear_bundle), spacing=8))
        self.b_stack = QStackedWidget()
        self.b_stack.setMinimumHeight(160)
        self.b_empty = EmptyState("package", "Nothing here yet",
                                  "Add packages from the list on the left.\nTheir dependencies come along "
                                  "automatically.", compact=True)
        self.b_scroll = QScrollArea()
        self.b_scroll.setWidgetResizable(True)
        self.b_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.b_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        bw = QWidget()
        bw.setObjectName("scrollBody")
        self.b_list = QVBoxLayout(bw)
        self.b_list.setContentsMargins(0, 0, 4, 0)
        self.b_list.setSpacing(2)
        self.b_list.addStretch(1)
        self.b_scroll.setWidget(bw)
        self.b_stack.addWidget(self.b_empty)
        self.b_stack.addWidget(self.b_scroll)
        lay.addWidget(self.b_stack, 1)
        lay.addWidget(hline())

        lay.addSpacing(4)
        lay.addWidget(lbl("INSTALLER NAME", "section"))
        self.name_edit = QLineEdit(SETTINGS.get("name") or "Python Packages")
        self.name_edit.setPlaceholderText("Python Packages")
        self.name_edit.textChanged.connect(self._name_changed)
        lay.addWidget(self.name_edit)
        self.name_prev = lbl("", "faint")
        self.name_prev.setWordWrap(True)
        lay.addWidget(self.name_prev)
        self._upd_name()

        lay.addSpacing(6)
        lay.addWidget(lbl("PYTHON ON THE OFFLINE PC", "section"))
        chips = QWidget()
        fl = FlowLayout(chips, 6)
        self.chips = {}
        for v, on in self.targets.items():
            star = v == self.bi["python"] and self.bi.get("from_build")
            c = QPushButton(v + ("  ★" if star else ""))
            c.setObjectName("chip")
            c.setCheckable(True)
            c.setChecked(on)
            c.setCursor(Qt.CursorShape.PointingHandCursor)
            if star:
                c.setToolTip("The Python of the PC this app was built on")
            c.toggled.connect(lambda on, v=v: self._chip(v, on))
            fl.addWidget(c)
            self.chips[v] = c
        lay.addWidget(chips)
        lay.addWidget(lbl("Pick every version the offline PCs use. You can pick several.", "faint"))

        lay.addSpacing(6)
        lay.addWidget(lbl("WINDOWS ON THE OFFLINE PC", "section"))
        self.plat_seg = Segmented(PLATS, self.plat, small=True)
        self.plat_seg.changed.connect(self._plat)
        lay.addLayout(hbox(self.plat_seg, None))

        lay.addSpacing(8)
        self.off_sw = ToggleSwitch(self.offline_only)
        self.off_sw.toggled.connect(self._offline)
        lay.addLayout(hbox(icon_label("wifi-off", "muted", 18),
                           vbox(lbl("Library only", "h3"),
                                lbl("Build from saved packages, no internet needed", "faint"), spacing=0),
                           None, self.off_sw, spacing=10))

        lay.addSpacing(8)
        self.go = button("Download && create installer", "bigPrimary", "download", slot=self.go_clicked, size=18)
        lay.addWidget(self.go)
        self.here_btn = button("Install now on this PC", None, "monitor",
                               "Download the bundle and install it into a Python on this PC right away", self.install_here)
        self.here_btn.setMinimumHeight(40)
        lay.addWidget(self.here_btn)

        # progress
        self.pbox = frame("soft")
        pl = QVBoxLayout(self.pbox)
        pl.setContentsMargins(16, 14, 16, 14)
        pl.setSpacing(10)
        self.p_stage = lbl("Getting ready…", "h3")
        self.p_stage.setWordWrap(True)
        self.p_pct = lbl("", "big")
        row = hbox(self.p_stage, self.p_pct)
        row.setStretch(0, 1)
        pl.addLayout(row)
        self.p_bar = ProgressBar(8)
        pl.addWidget(self.p_bar)
        stats = QHBoxLayout()
        self.p_vals = {}
        for k in ("Downloaded", "Speed", "Time left"):
            v = lbl("–", "stat")
            stats.addLayout(vbox(lbl(k, "faint"), v, spacing=1), 1)
            self.p_vals[k] = v
        pl.addLayout(stats)
        self.p_cur = lbl("", "mono")
        self.p_cur.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        pl.addLayout(hbox(self.p_cur, None, button("Details", "ghost", "terminal", "Everything pip is doing",
                                                   self.show_log)))
        self.pbox.hide()
        lay.addWidget(self.pbox)

        # result
        self.rbox = frame("okBox")
        rl = QVBoxLayout(self.rbox)
        rl.setContentsMargins(16, 14, 16, 14)
        rl.setSpacing(6)
        self.r_icon = QLabel()
        self.r_icon.setFixedSize(20, 20)
        self.r_title = lbl("", "okText")
        rl.addLayout(hbox(self.r_icon, self.r_title, None, spacing=8))
        self.r_file = lbl("", "h3", wrap=True, sel=True)
        self.r_text = lbl("", "muted", wrap=True)
        rl.addWidget(self.r_file)
        rl.addWidget(self.r_text)
        self.r_btns = QHBoxLayout()
        self.r_btns.setSpacing(6)
        rl.addSpacing(4)
        rl.addLayout(self.r_btns)
        self.rbox.hide()
        lay.addWidget(self.rbox)

    # ------------------------------------------------------------------ helpers
    def go_page(self, key):
        self.nav.set(key)
        self.stack.setCurrentWidget(self.pages[key])
        self.page = key
        if key == "lib":
            self.render_library()
        elif key == "search":
            QTimer.singleShot(0, self.s_edit.setFocus)

    def _focus_filter(self):
        w = {"pop": self.pop_filter, "search": self.s_edit, "lib": self.l_filter}.get(self.page)
        if w:
            w.setFocus()
            w.selectAll()

    def _save(self):
        SETTINGS.update({"bundle": self.bundle, "targets": [v for v, on in self.targets.items() if on],
                         "name": self.name_edit.text(), "plat": self.plat, "offline_only": self.offline_only,
                         "geometry": bytes(self.saveGeometry().toHex()).decode()})
        SETTINGS.save()

    def closeEvent(self, e):
        if self.job_running and not confirm(self, "A download is still running",
                                            "Close Package Downloader anyway? The download stops.", "Close",
                                            danger=True):
            e.ignore()
            return
        self._save()
        e.accept()
        os._exit(0)

    def showEvent(self, e):
        style_titlebar(self)
        super().showEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.toast.isVisible():
            self.toast.move((self.centralWidget().width() - self.toast.width()) // 2,
                            self.centralWidget().height() - self.toast.height() - 96)

    def toggle_theme(self):
        SETTINGS["theme"] = "light" if P["theme"] == "dark" else "dark"
        SETTINGS.save()
        apply_theme(QApplication.instance())

    def upd_status(self):
        st = self.lib.stats()
        self.st_lib.setText("Library  ·  %d packages, %d files, %s  ·  %s" % (
            st["packages"], st["files"], _human(st["size"]), self.lib.root))

    # ---- drag and drop anywhere on the window
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            if self.page != "file":
                self.go_page("file")
            self.dz.setProperty("hot", "true")
            repolish(self.dz)

    def dragLeaveEvent(self, e):
        self.dz.setProperty("hot", "false")
        repolish(self.dz)

    def dropEvent(self, e):
        self.dz.setProperty("hot", "false")
        repolish(self.dz)
        files = [u.toLocalFile() for u in e.mimeData().urls()
                 if u.isLocalFile() and (os.path.isfile(u.toLocalFile()) or os.path.isdir(u.toLocalFile()))]
        if files:
            self.scan_files(files)

    # ------------------------------------------------------------------ package rows
    def _saved(self, name):
        vs = self.lib.versions(name)
        if not vs:
            return ""
        best = sorted(vs, key=_vkey, reverse=True)[0]
        return best + ("  +%d" % (len(vs) - 1) if len(vs) > 1 else "")

    def in_bundle(self, name):
        c = canon(name)
        return any(canon(b["name"]) == c for b in self.bundle)

    def _set_row(self, it, name, rank=None, dl=None, info=None):
        info = info if info is not None else self.pypi.info_cache.get(canon(name))
        if rank is not None:
            it.setText(0, str(rank) if rank else "")
            it.setTextAlignment(0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            it.setText(1, name)
            it.setData(1, NAME_ROLE, name)
            it.setText(2, fmt_dl(dl) if dl else "")
        if info:
            if info.get("exists"):
                kt, kc = KIND_TXT.get(info.get("kind"), ("", None))
                it.setText(3, info.get("version") or "")
                it.setText(4, kt)
                it.setData(4, PillDelegate.ROLE, kc)
                it.setData(1, NameDelegate.ROLE, info.get("summary") or "")
                it.setToolTip(1, info.get("summary") or "")
            else:
                it.setText(4, "Not on PyPI")
                it.setData(4, PillDelegate.ROLE, "bad")
                it.setData(1, NameDelegate.ROLE, info.get("error") or "")
        saved = self._saved(name)
        it.setText(5, ("✓ " + saved) if saved else "")
        it.setData(5, PillDelegate.ROLE, "ok" if saved else None)
        it.setData(6, ActionDelegate.ROLE, "in" if self.in_bundle(name) else "out")

    def fill_tree(self, tv, key, rows, reset):
        if reset:
            tv.clear()
            self.items[key] = {}
        for r in rows:
            it = QTreeWidgetItem()
            self._set_row(it, r["name"], r.get("rank") or 0, r.get("downloads"))
            tv.addTopLevelItem(it)
            self.items[key][canon(r["name"])] = it
            if canon(r["name"]) not in self.pypi.info_cache:
                self.pool.submit(self._fetch_info, key, r["name"])

    def _fetch_info(self, key, name):
        info = self.pypi.info(name)
        self.ui_call(self._got_info, key, name, info)

    def _got_info(self, key, name, info):
        it = self.items.get(key, {}).get(canon(name))
        if it is not None:
            self._set_row(it, name, info=info)

    def refresh_marks(self):
        for key in self.items:
            for c, it in self.items[key].items():
                self._set_row(it, it.data(1, NAME_ROLE))
            {"pop": self.pop_tv, "search": self.s_tv}[key].viewport().update()

    def toggle_pkg(self, name):
        if not name:
            return
        if self.in_bundle(name):
            self.remove_pkg(name)
        else:
            self.add_pkg(name)
            self.toast.show_msg("Added %s to the bundle" % name)

    def add_selected(self, tv):
        names = [it.data(1, NAME_ROLE) for it in tv.selectedItems()]
        new = [n for n in names if n and not self.in_bundle(n)]
        for n in new:
            self.add_pkg(n, render=False)
        self.render_bundle()
        if new:
            self.toast.show_msg("Added %s" % (new[0] if len(new) == 1 else "%d packages" % len(new)))

    # ------------------------------------------------------------------ popular / search
    def req_popular(self, reset):
        q = self.pop_filter.text().strip()
        off = 0 if reset else len(self.pop_rows)

        def work():
            try:
                d = self.pypi.popular(off, 100, q)
            except Exception as e:
                d = {"rows": [], "total": 0, "updated": "error: %s" % e}
            self.ui_call(self._show_popular, d, reset)
        self.pool.submit(work)

    def _show_popular(self, d, reset):
        if reset:
            self.pop_rows = []
        self.pop_rows += d["rows"]
        self.pop_total, upd = d["total"], d.get("updated") or ""
        self.fill_tree(self.pop_tv, "pop", d["rows"], reset)
        upd = ("Updated " + upd[:10]) if re.match(r"^\d{4}-", upd) else upd
        self.pop_sub.setText("The packages people downloaded most from PyPI in the last 30 days  ·  %s packages%s" % (
            "{:,}".format(self.pop_total), ("  ·  " + upd) if upd else ""))
        self.pop_more.setEnabled(len(self.pop_rows) < self.pop_total)

    def search(self):
        q = self.s_edit.text().strip()
        if not q:
            return
        self._s_seq += 1
        seq = self._s_seq
        self.s_hint.setText("Searching for “%s”…" % q)

        def work():
            try:
                rows = self.pypi.search(q)
                self.ui_call(self._show_search, q, rows, seq)
            except Exception as e:
                self.ui_call(self.s_hint.setText, "Search failed: %s" % e)
        self.pool.submit(work)

    def _show_search(self, q, rows, seq):
        if seq != self._s_seq:
            return
        self.fill_tree(self.s_tv, "search", rows, True)
        if rows:
            self.s_stack.setCurrentWidget(self.s_tv)
            self.s_hint.setText("%d%s results for “%s”, most downloaded first" % (
                len(rows), "+" if len(rows) >= 60 else "", q))
        else:
            self.s_stack.setCurrentWidget(self.s_empty)
            self.s_empty.set_text("Nothing found for “%s”" % q, "Check the spelling, or try part of the name.")
            self.s_hint.setText("")

    # ------------------------------------------------------------------ file scan
    def choose_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Choose the script(s) of your app", "",
            "Scripts (*.py *.pyw *.bat *.cmd *.ps1 *.psm1 *.ipynb *.txt);;All files (*.*)")
        if files:
            self.scan_files(files)

    def choose_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Choose the folder of your app")
        if d:
            self.scan_files([d])

    def scan_files(self, paths):
        """Add files or folders to the loaded list (no duplicates) and scan them all together."""
        known = {os.path.normcase(os.path.abspath(p)) for p in self.f_paths}
        for p in paths:
            k = os.path.normcase(os.path.abspath(p))
            if k not in known:
                known.add(k)
                self.f_paths.append(p)
        self._rescan()

    def remove_file(self, path):
        self.f_paths = [p for p in self.f_paths if p != path]
        self._rescan()

    def clear_files(self):
        self.f_paths = []
        self.f_ticks = {}
        self._rescan()

    def _render_file_chips(self):
        while self.f_chip_flow.count():
            w = self.f_chip_flow.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.f_chip_widgets = {}
        for p in self.f_paths:
            c = FileChip(p, self.remove_file)
            self.f_chip_widgets[p] = c
            self.f_chip_flow.addWidget(c)
        n = len(self.f_paths)
        self.f_files_lbl.setText("%s  (%d)" % ("FILES AND FOLDERS" if any(os.path.isdir(p) for p in self.f_paths)
                                               else "FILES", n))
        self.f_files.setVisible(bool(n))
        self.f_chip_flow.invalidate()
        # once files are loaded the drop zone shrinks to one line, leaving room for the results
        self.dz_desc.setVisible(not n)
        self.dz_title.setText("Drop more files or folders here" if n else "Drop scripts or a whole folder here")
        self.dz_lay.setContentsMargins(*((18, 8, 18, 8) if n else (26, 18, 26, 18)))
        self.dz_bubble.setVisible(not n)

    def _rescan(self):
        self.go_page("file")
        if self.bundle:
            self.bundle = []
            self.render_bundle()
        self._render_file_chips()
        self.f_tv.clear()
        self.f_add.setEnabled(False)
        self.scan = None
        paths = list(self.f_paths)
        if not paths:
            self.f_stack.setCurrentIndex(0)
            self.f_hint.setText("")
            return
        self.f_stack.setCurrentIndex(1)
        self.f_title.setText("Reading %d file%s…" % (len(paths), "" if len(paths) == 1 else "s"))
        self.f_sub.setText("Looking for imports and pip lines, then checking each name on PyPI.")
        self.f_notes.setText("")
        self._scan_gen = getattr(self, "_scan_gen", 0) + 1
        gen = self._scan_gen

        def work():
            files, bad, local = [], [], set()
            jobs = []                         # (real path, label shown in the table)
            owner = {}                        # label -> the file or folder entry it came from
            for p in paths:
                if os.path.isdir(p):
                    found, loc, skipped = folder_files(p)
                    local |= loc
                    top = os.path.basename(os.path.normpath(p)) or p
                    for f in found:
                        label = top + "/" + os.path.relpath(f, p).replace(os.sep, "/")
                        jobs.append((f, label))
                        owner[label] = p
                    if not found:
                        bad.append("%s: no scripts or requirements files found in this folder." % top)
                    if skipped:
                        bad.append("%s: %d file%s skipped (too big, or more than 3000 files)." % (
                            top, skipped, "" if skipped == 1 else "s"))
                else:
                    jobs.append((p, os.path.basename(p)))
                    owner[os.path.basename(p)] = p
            n_dirs = sum(1 for p in paths if os.path.isdir(p))
            if n_dirs:
                self.ui_call(self.f_title.setText, "Reading %d file%s…" % (len(jobs), "" if len(jobs) == 1 else "s"))
            for p, label in jobs:
                try:
                    with open(p, "rb") as fh:
                        raw = fh.read()
                except OSError as e:
                    bad.append("Could not read %s: %s" % (os.path.basename(p), e.strerror or e))
                    continue
                txt = None
                if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
                    txt = raw.decode("utf-16", "replace")
                else:
                    for enc in ("utf-8-sig", "cp1252", "latin-1"):
                        try:
                            txt = raw.decode(enc)
                            break
                        except UnicodeDecodeError:
                            continue
                files.append({"name": p, "label": label, "content": txt or ""})
            try:
                res = scan_files(files, self.pypi, local)
            except Exception as e:
                self.ui_call(self.f_title.setText, "Scan failed: %s" % e)
                return
            res["notes"] = bad + res["notes"]
            res["n_files"] = len(files)
            res["owner"] = owner
            for it in res["packages"]:
                it["_on"] = bool(it.get("exists") and not it.get("optional") and not it.get("unsure")
                                 and it.get("kind") != "source")
            if gen == self._scan_gen:
                self.ui_call(self._show_scan, paths, res)
        self.pool.submit(work)

    def _show_scan(self, paths, res):
        if paths != self.f_paths:
            return                  # files changed while this scan ran; a newer scan is on its way
        self.scan = (paths, res)
        first = os.path.normpath(paths[0])
        self.scan_stem = os.path.basename(first) if os.path.isdir(first) else os.path.splitext(os.path.basename(first))[0]
        items = res["packages"]
        for p in items:
            k = canon(re.split(r"[\[<>=!~ ;]", p["name"], 1)[0])
            if k in self.f_ticks and p.get("exists"):
                p["_on"] = self.f_ticks[k]
        owner = res.get("owner", {})
        per_entry, files_in = {}, {}
        for label, entry in owner.items():
            files_in[entry] = files_in.get(entry, 0) + 1
        for p in items:
            if p.get("exists"):
                for entry in {owner.get(f) for f in p.get("files", [])}:
                    per_entry[entry] = per_entry.get(entry, 0) + 1
        for path, chip in getattr(self, "f_chip_widgets", {}).items():
            k = per_entry.get(path, 0)
            pk = "%d package%s" % (k, "" if k == 1 else "s")
            if os.path.isdir(path):
                nf = files_in.get(path, 0)
                chip.count.setText("· %d file%s · %s" % (nf, "" if nf == 1 else "s", pk))
                chip.count.setToolTip("%d script%s scanned in this folder and its subfolders" % (nf, "" if nf == 1 else "s"))
            else:
                chip.count.setText("· %d" % k if k else "· none")
                chip.count.setToolTip("%s found in this file" % pk)
        n = sum(1 for p in items if p.get("exists"))
        nf = res.get("n_files", len(paths))
        if len(paths) == 1 and os.path.isdir(paths[0]):
            who = "%s (%d files)" % (os.path.basename(os.path.normpath(paths[0])), nf)
        elif len(paths) == 1:
            who = os.path.basename(paths[0])
        else:
            who = "These %d files" % nf
        self.f_title.setText("%s need%s %d package%s from PyPI" % (
            who, "s" if len(paths) == 1 else "", n, "" if n == 1 else "s"))
        self.f_sub.setText("Ticked ones go into the bundle. Optional imports and names that might be your own "
                           "modules start unticked.")
        tv = self.f_tv
        tv.blockSignals(True)
        tv.clear()
        for i, p in enumerate(items):
            it = QTreeWidgetItem()
            it.setText(0, p["name"])
            it.setData(0, NAME_ROLE, i)
            f = it.font(0)
            f.setWeight(QFont.Weight.DemiBold)
            it.setFont(0, f)
            if p.get("exists"):
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(0, Qt.CheckState.Checked if p["_on"] else Qt.CheckState.Unchecked)
                kt, kc = KIND_TXT.get(p.get("kind"), ("", None))
            else:
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                kt, kc = "Not on PyPI", "bad"
            if p.get("optional"):
                kt, kc = "Optional", "muted"
            elif p.get("unsure"):
                kt, kc = "Check this", "warn"
            it.setText(1, kt)
            it.setData(1, PillDelegate.ROLE, kc)
            saved = self._saved(re.split(r"[\[<>=!~ ;]", p["name"], 1)[0])
            it.setText(2, ("✓ " + saved) if saved else "")
            it.setData(2, PillDelegate.ROLE, "ok" if saved else None)
            how = "%s  ·  %s" % (", ".join(p["how"]), ", ".join(re.sub(r" line (\d+)", r":\1", w) for w in p["where"][:2]))
            it.setText(3, how)
            it.setToolTip(3, "\n".join(p["where"]))
            note = p.get("hint") if p.get("unsure") else (
                "No ready-made build on PyPI (source only), usually can't be bundled. " + (p.get("summary") or "")
                if p.get("kind") == "source" else (p.get("summary") or p.get("hint") or p.get("error") or ""))
            it.setText(4, note)
            it.setToolTip(4, note)
            tv.addTopLevelItem(it)
        tv.blockSignals(False)
        tv.horizontalScrollBar().setValue(0)
        for col, cap in ((3, 520), (4, 900)):
            tv.resizeColumnToContents(col)
            tv.setColumnWidth(col, max(260, min(cap, tv.columnWidth(col) + 16)))
        if not items:
            self.f_title.setText("%s only use%s what comes with Python" % (who, "s" if len(paths) == 1 else ""))
            self.f_sub.setText("Nothing to download.")
        notes = []
        if res["stdlib"]:
            notes.append("Comes with Python (skipped): " + ", ".join(res["stdlib"]))
        if res["local"]:
            notes.append("Your own modules (skipped): " + ", ".join(res["local"]))
        notes += res["notes"]
        self.f_notes.setText("\n".join(notes) if notes else "Nothing else to note.")
        self.f_hint.setText("Installer:  “%s%s.exe”" % (self.scan_stem, INSTALL_SUFFIX))
        self.f_add.setEnabled(bool(n))

    def _scan_ticked(self, it, col):
        if col == 0 and self.scan:
            p = self.scan[1]["packages"][it.data(0, NAME_ROLE)]
            p["_on"] = it.checkState(0) == Qt.CheckState.Checked
            self.f_ticks[canon(re.split(r"[\[<>=!~ ;]", p["name"], 1)[0])] = p["_on"]

    def scan_add(self):
        n = 0
        for p in self.scan[1]["packages"]:
            if p.get("_on"):
                self.add_pkg(p["name"], render=False)
                n += 1
        self.name_edit.setText(self.scan_stem)
        self.render_bundle()
        self.toast.show_msg("Added %d package%s to the bundle" % (n, "" if n == 1 else "s"))

    # ------------------------------------------------------------------ library page
    def render_library(self):
        tv = self.l_tv
        q = canon(self.l_filter.text().strip()) if self.l_filter.text().strip() else ""
        opened = set()
        for i in range(tv.topLevelItemCount()):
            if tv.topLevelItem(i).isExpanded():
                opened.add(tv.topLevelItem(i).text(0))
        tv.clear()
        pk = self.lib.packages()
        st = self.lib.stats()
        self.l_sub.setText("Every package you've downloaded, kept with its exact version and reused next time.  "
                           "Folder:  %s" % self.lib.root)
        self.l_stats["Packages"].setText("{:,}".format(st["packages"]))
        self.l_stats["Files"].setText("{:,}".format(st["files"]))
        self.l_stats["On disk"].setText(_human(st["size"]))
        n = 0
        for d in pk:
            if q and q not in canon(d["name"]):
                continue
            vers = sorted(d["versions"], key=_vkey, reverse=True)
            files = [f for v in vers for f in d["versions"][v]]
            size = sum((self.lib.index.get(f) or {}).get("size") or 0 for f in files)
            latest = self.lib_latest.get(canon(d["name"]), "")
            if latest and vers and _vkey(latest) > _vkey(vers[0]):
                status, kind = "Newer: %s" % latest, "warn"
            elif latest:
                status, kind = "Up to date", "ok"
            else:
                added = max(((self.lib.index.get(f) or {}).get("added") or "") for f in files) if files else ""
                status, kind = ("Added " + added[:10]) if added else "", None
            it = QTreeWidgetItem([d["name"], ", ".join(vers[:4]) + (" …" if len(vers) > 4 else ""), str(len(files)),
                                  _human(size), latest, status])
            it.setData(5, PillDelegate.ROLE, kind)
            it.setData(0, NAME_ROLE, ("pkg", d["name"], None))
            f = it.font(0)
            f.setWeight(QFont.Weight.DemiBold)
            it.setFont(0, f)
            for v in vers:
                for fn in sorted(d["versions"][v]):
                    w = parse_wheel(fn) or {}
                    e = self.lib.index.get(fn) or {}
                    ch = QTreeWidgetItem([fn, v, "", _human(e.get("size")), "",
                                          "%s · %s" % (w.get("py", ""), w.get("plat", ""))])
                    ch.setData(0, NAME_ROLE, ("file", fn, (e.get("name"), e.get("version"))))
                    ch.setToolTip(0, "%s\nAdded %s" % (fn, e.get("added", "")))
                    ch.setForeground(0, QBrush(QColor(P["muted"])))
                    it.addChild(ch)
            tv.addTopLevelItem(it)
            if d["name"] in opened:
                it.setExpanded(True)
            n += 1
        if not pk:
            self.l_stack.setCurrentWidget(self.l_empty)
            self.l_empty.set_text("Your library is empty", "Every package you download is kept here with its exact "
                                  "version, and the next installer reuses it instead of downloading it again.")
        elif not n:
            self.l_stack.setCurrentWidget(self.l_empty)
            self.l_empty.set_text("No match", "Nothing in the library matches “%s”." % self.l_filter.text().strip())
        else:
            self.l_stack.setCurrentWidget(self.l_tv)
        self.upd_status()

    def _lib_sel(self):
        return [it.data(0, NAME_ROLE) for it in self.l_tv.selectedItems() if it.data(0, NAME_ROLE)]

    def lib_add(self):
        added = 0
        for kind, x, extra in self._lib_sel():
            if kind == "file":
                name, ver = extra
                if not name:
                    continue
                self.bundle = [b for b in self.bundle if canon(b["name"]) != canon(name)]
                self.add_pkg("%s==%s" % (name, ver), render=False)
            else:
                self.add_pkg(x, render=False)
            added += 1
        if added:
            self.render_bundle()
            self.toast.show_msg("Added %d to the bundle" % added)

    def lib_delete(self):
        fns = []
        for kind, x, extra in self._lib_sel():
            if kind == "file":
                fns.append(x)
            else:
                fns += [f for fs in self.lib.versions(x).values() for f in fs]
        fns = sorted(set(fns))
        if not fns:
            return
        size = sum((self.lib.index.get(f) or {}).get("size") or 0 for f in fns)
        if confirm(self, "Delete from the library?", "%d file%s (%s) will be deleted. They'll be downloaded again "
                   "if a bundle needs them." % (len(fns), "" if len(fns) == 1 else "s", _human(size)),
                   "Delete", danger=True):
            self.lib.delete(fns)
            self.render_library()
            self.refresh_marks()
            self.render_bundle()

    def lib_check(self):
        pk = self.lib.packages()
        if not pk:
            return
        self.st_msg.setText("Checking %d packages on PyPI…" % len(pk))
        for d in pk:
            self.pypi.info_cache.pop(canon(d["name"]), None)

        def work():
            with ThreadPoolExecutor(8) as ex:
                for d, info in zip(pk, ex.map(lambda d: self.pypi.info(d["name"]), pk)):
                    if info.get("version"):
                        self.lib_latest[canon(d["name"])] = info["version"]
            newer = sum(1 for d in pk if self.lib_latest.get(canon(d["name"])) and
                        _vkey(self.lib_latest[canon(d["name"])]) > _vkey(sorted(d["versions"], key=_vkey)[-1]))
            self.ui_call(self.render_library)
            self.ui_call(self.st_msg.setText, "")
            self.ui_call(self.toast.show_msg, "%d package%s have a newer version" % (newer, "" if newer == 1 else "s")
                         if newer else "Everything in the library is up to date")
        self.pool.submit(work)

    def change_library(self):
        d = QFileDialog.getExistingDirectory(self, "Choose a folder for the package library",
                                             os.path.dirname(self.lib.root))
        if not d:
            return
        d = os.path.normpath(d)
        if os.path.basename(d).lower() != "package downloader library":
            d = os.path.join(d, "Package Downloader Library")
        old = self.lib.files_dir
        n = len(self.lib.index)
        move = bool(n) and confirm(self, "Move the saved packages?", "Move the %d files already in the library to "
                                   "the new folder? Choose Keep to leave them where they are." % n, "Move", cancel="Keep")
        try:
            os.makedirs(os.path.join(d, "Packages"), exist_ok=True)
            if move:
                for fn in list(self.lib.index):
                    try:
                        shutil.move(os.path.join(old, fn), os.path.join(d, "Packages", fn))
                    except OSError:
                        pass
                try:
                    shutil.move(self.lib.index_path, os.path.join(d, "library.json"))
                except OSError:
                    pass
            self.lib.set_root(d)
        except OSError as e:
            confirm(self, "Couldn't use that folder", str(e), cancel=None)
            return
        SETTINGS["library_dir"] = d
        SETTINGS.save()
        self.render_library()
        self.refresh_marks()

    # ------------------------------------------------------------------ bundle
    def add_pkg(self, spec, render=True):
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)$", spec.strip())
        name, extra, pin = (m.group(1), m.group(2) or "", m.group(3)) if m else (spec, "", "")
        if self.in_bundle(name):
            return
        ver = ""
        pm = re.match(r"^==\s*([^,\s]+)$", pin)
        if pm:
            ver, pin = pm.group(1), ""
        self.bundle.append({"name": name, "extra": extra, "ver": ver, "pin": pin})
        if render:
            self.render_bundle()

    def remove_pkg(self, name):
        self.bundle = [b for b in self.bundle if canon(b["name"]) != canon(name)]
        self.render_bundle()

    def clear_bundle(self):
        if self.bundle:
            self.bundle = []
            self.render_bundle()

    def spec(self, b, ver=None):
        v = ver or b.get("ver")
        return b["name"] + (b.get("extra") or "") + ("==" + v if v else (b.get("pin") or ""))

    def render_bundle(self, save=True):
        while self.b_list.count() > 1:
            w = self.b_list.takeAt(0).widget()
            if w:
                w.deleteLater()
        for i, b in enumerate(self.bundle):
            self.b_list.insertWidget(i, BundleRow(self, b))
        self.b_stack.setCurrentWidget(self.b_scroll if self.bundle else self.b_empty)
        self.b_count.setText(str(len(self.bundle)))
        self.refresh_marks()
        self.upd_go()
        self.upd_status()
        if save:
            self._save()

    def _name_changed(self, *_):
        self._upd_name()
        self._save()

    def _upd_name(self):
        n = safe_filename(self.name_edit.text().strip() or "Python Packages")
        self.name_prev.setText("Saved as  “%s%s%s”" % (n, INSTALL_SUFFIX, ".exe" if find_stub() else ".zip"))

    def _chip(self, v, on):
        self.targets[v] = on
        self.upd_go()
        self._save()

    def _plat(self, k):
        self.plat = k
        self._save()

    def _offline(self, on):
        self.offline_only = on
        self.upd_go()
        self._save()

    def upd_go(self):
        ok = bool(self.bundle) and any(self.targets.values()) and not self.job_running
        self.go.setEnabled(ok)
        self.here_btn.setEnabled(bool(self.bundle) and not self.job_running)
        self.here_btn.setText("Install now on this PC (from the library)" if self.offline_only else "Install now on this PC")
        self.go.setText("Create installer from the library" if self.offline_only else "Download && create installer")
        if not self.bundle:
            self.go.setToolTip("Add packages to the bundle first")
        elif not any(self.targets.values()):
            self.go.setToolTip("Pick the offline PC's Python version")
        else:
            self.go.setToolTip("")

    # ------------------------------------------------------------------ settings
    def open_settings(self):
        SettingsDialog(self).exec()
        self.render_library()
        self.upd_status()
        self._upd_name()

    # ------------------------------------------------------------------ run
    def install_here(self):
        if self.job_running or JOB.running or not self.bundle:
            return
        d = InstallHereDialog(self)
        if d.exec() == QDialog.DialogCode.Accepted and d.choice:
            self.go_clicked(install=d.choice)

    def go_clicked(self, install=None):
        if self.job_running or JOB.running:
            return
        install = install if isinstance(install, dict) else None
        targets = [install["version"]] if install else [v for v, on in self.targets.items() if on]
        plat = install["platform"] if install else self.plat
        self.go.setEnabled(False)
        self.here_btn.setEnabled(False)
        (self.here_btn if install else self.go).setText("Checking the library…")

        def work():
            try:
                newer = [] if self.offline_only else check_newer(self.bundle, targets, plat, self.pypi, self.lib)
            except Exception:
                newer = []
            self.ui_call(self._go2, targets, newer, install)
        self.pool.submit(work)

    def _go2(self, targets, newer, install=None):
        use_saved = {}
        pol = SETTINGS.get("newer_policy", "ask")
        if newer:
            if pol == "saved":
                use_saved = {n["name"]: n["saved"] for n in newer}
            elif pol == "ask":
                d = NewerDialog(self, newer)
                if d.exec() != QDialog.DialogCode.Accepted:
                    self.upd_go()
                    return
                use_saved = d.choice()
        with JOB.lock:
            if JOB.running:
                self.upd_go()
                return
            JOB.reset()
            JOB.running = True
            JOB.state = "running"
        req = {"name": self.name_edit.text().strip(), "platform": self.plat, "targets": targets,
               "offline_only": self.offline_only,
               "packages": [{"name": b["name"], "spec": self.spec(b, use_saved.get(b["name"]))} for b in self.bundle]}
        if install:
            req["install_into"] = {"exe": install["exe"], "user": install.get("user")}
        self.job_running = True
        self.job_since = 0
        self.log_lines = []
        if self.log_dlg:
            self.log_dlg.text.clear()
        if use_saved:
            JOB.add("Using saved versions: %s" % ", ".join("%s %s" % kv for kv in use_saved.items()))
        self.rbox.hide()
        self.pbox.show()
        self.p_stage.setText("Getting ready…")
        self.p_pct.setText("")
        self.p_bar.indeterminate()
        for v in self.p_vals.values():
            v.setText("–")
        self.p_cur.setText("")
        self.upd_go()
        (self.here_btn if install else self.go).setText("Installing…" if install else "Working…")
        QTimer.singleShot(50, lambda: self.side.ensureWidgetVisible(self.pbox, 0, 0))
        threading.Thread(target=build_job, args=(req,), daemon=True).start()
        QTimer.singleShot(250, self._poll)

    def _poll(self):
        d = JOB.snapshot(self.job_since)
        self.job_since = d["count"]
        self._log(d["lines"])
        self.p_stage.setText(d.get("stage") or "Working…")
        p = d.get("progress")
        if p and p.get("phase") in ("download", "pack") and p.get("total"):
            frac = min(1.0, p["done"] / float(p["total"]))
            self.p_bar.set(frac)
            self.p_pct.setText("%d%%" % int(frac * 100))
            dl = p["phase"] == "download"
            self.p_vals["Downloaded"].setText("%s / %s" % (_human(p["done"]), _human(p["total"])))
            self.p_vals["Speed"].setText((_human(p["speed"]) + "/s") if dl and p.get("speed") else "–")
            self.p_vals["Time left"].setText("done" if p["done"] >= p["total"] else human_t(p.get("eta")))
            cur = "%d of %d files" % (p["files_done"], p["files_total"])
            if dl and p.get("current"):
                cur += "  ·  " + ", ".join(p["current"][:2])
            self.p_cur.setText(cur if len(cur) < 64 else cur[:62] + "…")
        else:
            self.p_bar.indeterminate()
            self.p_pct.setText("")
        if d["state"] in ("done", "error"):
            self.job_running = False
            self.last_result = (d["state"], d.get("result") or {})
            self._show_result(*self.last_result)
            self.render_bundle(save=False)
            self.render_library()
        else:
            QTimer.singleShot(250, self._poll)

    def _show_result(self, state, r):
        self.pbox.hide()
        while self.r_btns.count():
            w = self.r_btns.takeAt(0).widget()
            if w:
                w.deleteLater()
        if state == "done" and r.get("kind") == "installed":
            self.rbox.setObjectName("okBox")
            self.r_title.setObjectName("okText")
            self.r_title.setText("Installed on this PC")
            self.r_icon.setPixmap(svg_pixmap(svg_markup("check-circle", P["ok"], 2), 20))
            failed = list((r.get("failed") or {}).keys())
            reused = min(r.get("reused") or 0, r["files"])
            lib_txt = ("all from the library" if reused == r["files"] else
                       "%d from the library" % reused if reused else "all downloaded")
            self.r_file.setText("Python %s" % r["python_version"])
            text = "%d package%s · %d files · %s\n%s" % (
                len(r["packages"]), "" if len(r["packages"]) == 1 else "s", r["files"], lib_txt, r["python"])
            if failed:
                text += "\nNot installed: %s (see Details)." % ", ".join(failed)
            self.r_text.setText(text)
            self.r_btns.addStretch(1)
            self.r_btns.addWidget(button("Details", "ghost", "terminal", slot=self.show_log))
            self.toast.show_msg("Installed %d package%s" % (len(r["packages"]), "" if len(r["packages"]) == 1 else "s"), 3500)
        elif state == "done":
            self.rbox.setObjectName("okBox")
            self.r_title.setObjectName("okText")
            self.r_title.setText("Installer ready")
            self.r_icon.setPixmap(svg_pixmap(svg_markup("check-circle", P["ok"], 2), 20))
            failed = list((r.get("failed") or {}).keys())
            reused = min(r.get("reused") or 0, r["files"])
            lib_txt = ("all from the library, nothing downloaded" if reused == r["files"] else
                       "%d from the library" % reused if reused else "all downloaded")
            self.r_file.setText(os.path.basename(r["path"]))
            text = "%d package%s · %d files · %s MB · %s" % (
                len(r["packages"]), "" if len(r["packages"]) == 1 else "s", r["files"], r["size_mb"], lib_txt)
            if failed:
                text += "\nNot included: %s (no Windows build for that Python; see Details)." % ", ".join(failed)
            text += "\nCopy it to the offline PC and double-click it."
            self.r_text.setText(text)
            self.r_btns.addWidget(button("Show in folder", None, "folder-open", slot=lambda: open_in_explorer(r["path"])))
            cp = tool("", "copy", "Copy the installer's full path", obj="iconBtn", size=16,
                      slot=lambda: (QApplication.clipboard().setText(r["path"]), self.toast.show_msg("Path copied")))
            self.r_btns.addWidget(cp)
            self.r_btns.addStretch(1)
            self.r_btns.addWidget(button("Details", "ghost", "terminal", slot=self.show_log))
            self.toast.show_msg("Installer ready: %s" % os.path.basename(r["path"]), 3500)
        else:
            self.rbox.setObjectName("badBox")
            self.r_title.setObjectName("badText")
            self.r_title.setText("Something went wrong")
            self.r_icon.setPixmap(svg_pixmap(svg_markup("x-circle", P["bad"], 2), 20))
            self.r_file.setText("")
            self.r_text.setText(r.get("error") or "See Details.")
            self.r_btns.addWidget(button("Details", None, "terminal", slot=self.show_log))
            self.r_btns.addStretch(1)
        repolish(self.rbox)
        repolish(self.r_title)
        self.rbox.show()
        QTimer.singleShot(50, lambda: self.side.ensureWidgetVisible(self.rbox, 0, 0))
        self.upd_go()

    # ------------------------------------------------------------------ log
    def _log(self, lines):
        if not lines:
            return
        self.log_lines += lines
        if self.log_dlg:
            for l in lines:
                self.log_dlg.text.appendPlainText(l)

    def show_log(self):
        if not self.log_dlg:
            self.log_dlg = LogDialog(self)
        self.log_dlg.text.setPlainText("\n".join(self.log_lines) if self.log_lines else "Nothing has run yet.")
        self.log_dlg.text.moveCursor(self.log_dlg.text.textCursor().MoveOperation.End)
        self.log_dlg.show()
        self.log_dlg.raise_()


class InstallHereDialog(Dialog):
    """Pick the Python on this PC that 'Install now' installs into."""

    def __init__(self, win):
        super().__init__(win, "Install now on this PC",
                         "Downloads the bundle (or reuses the library) and installs it straight into a Python "
                         "on this PC. No installer file is made.", 600)
        self.win = win
        self.choice = None
        self.pys = []
        self.body.addWidget(lbl("INSTALL INTO", "section"))
        self.cb = QComboBox()
        self.cb.addItem("Looking for Python on this PC…")
        self.cb.setEnabled(False)
        self.cb.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.browse = button("Browse…", None, "folder-open", "Pick python.exe yourself", self._browse)
        row = hbox(self.cb, self.browse, spacing=8)
        row.setStretch(0, 1)
        self.body.addLayout(row)
        self.note = lbl("", "faint", wrap=True)
        self.body.addWidget(self.note)
        self.user_sw = ToggleSwitch(False)
        self.body.addLayout(hbox(self.user_sw, vbox(lbl("Only for my Windows user", "h3"),
                                                    lbl("No admin rights needed", "faint"), spacing=0), None, spacing=12))
        self.foot.addStretch(1)
        self.foot.addWidget(button("Cancel", slot=self.reject))
        self.ok = button("Install", "primary", "download", slot=self._ok)
        self.ok.setEnabled(False)
        self.foot.addWidget(self.ok)
        self.bridge = Bridge()
        threading.Thread(target=lambda: self.bridge(self._got, find_pythons()), daemon=True).start()

    def _label(self, p):
        return "Python %s  ·  %s%s  ·  %s" % (p["full"], "64-bit" if "amd64" in p["platform"] else p["platform"],
                                             "  ·  virtual env" if p["venv"] else "", p["exe"])

    def _got(self, pys):
        # newest normal install first, virtual envs after
        pys.sort(key=lambda p: (p["venv"], [-int(x) for x in p["version"].split(".")]))
        self.pys = pys
        self.cb.clear()
        if not pys:
            self.cb.addItem("No Python found on this PC")
            self.note.setText("Click Browse… to pick python.exe.")
            return
        for p in pys:
            self.cb.addItem(self._label(p))
        self.cb.setEnabled(True)
        self.ok.setEnabled(True)
        self.note.setText("Packages are matched to the Python you pick (its version and 32/64-bit).")

    def _browse(self):
        f, _ = QFileDialog.getOpenFileName(self, "Choose python.exe", "", "python.exe (python*.exe);;All files (*)")
        if not f:
            return
        info = _probe_python(f)
        if not info:
            self.note.setText("That file didn't run as Python.")
            return
        if not self.pys:
            self.cb.clear()
        self.pys.insert(0, info)
        self.cb.insertItem(0, self._label(info))
        self.cb.setCurrentIndex(0)
        self.cb.setEnabled(True)
        self.ok.setEnabled(True)

    def _ok(self):
        i = self.cb.currentIndex()
        if 0 <= i < len(self.pys):
            self.choice = dict(self.pys[i], user=self.user_sw.isChecked())
            self.accept()


class LogDialog(Dialog):
    def __init__(self, win):
        super().__init__(win, "Details", "Everything the last run did, including pip's messages.", 760)
        self.setModal(False)
        self.resize(820, 560)
        self.text = QPlainTextEdit()
        self.text.setObjectName("log")
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.body.addWidget(self.text, 1)
        self.foot.addWidget(button("Copy all", "ghost", "copy",
                                   slot=lambda: QApplication.clipboard().setText(self.text.toPlainText())))
        self.foot.addStretch(1)
        self.foot.addWidget(button("Close", "primary", slot=self.hide))


class NewerDialog(Dialog):
    def __init__(self, win, newer):
        super().__init__(win, "Newer versions are available",
                         "The library already has these packages, but PyPI has something newer. Use the saved "
                         "version (nothing to download) or download the newest?", 640)
        self.newer = newer
        c, lay = card((6, 4, 6, 4), 0)
        self.segs = {}
        for i, n in enumerate(newer):
            if i:
                lay.addWidget(hline())
            seg = Segmented([("saved", "Use saved %s" % n["saved"]), ("newest", "Download %s" % n["latest"])],
                            "saved", small=True)
            self.segs[n["name"]] = seg
            lay.addLayout(hbox(icon_label("package", "muted", 18),
                               vbox(lbl(n["name"], "h3"), lbl("saved %s  ·  newest %s" % (n["saved"], n["latest"]),
                                                               "faint"), spacing=0),
                               None, seg, spacing=10, margins=(12, 10, 10, 10)))
        self.body.addWidget(c)
        self.body.addLayout(hbox(button("Use saved for all", "ghost", slot=lambda: self._all("saved")),
                                 button("Download all newest", "ghost", slot=lambda: self._all("newest")), None))
        self.remember = ToggleSwitch(False)
        self.body.addLayout(hbox(self.remember, lbl("Remember my choice when it's the same for all "
                                                    "(change it in Settings)", "muted"), None, spacing=10))
        self.foot.addStretch(1)
        self.foot.addWidget(button("Cancel", slot=self.reject))
        b = button("Continue", "primary", slot=self.accept)
        b.setDefault(True)
        self.foot.addWidget(b)

    def _all(self, k):
        for s in self.segs.values():
            s.set(k)

    def choice(self):
        ch = {n["name"]: n["saved"] for n in self.newer if self.segs[n["name"]].value() == "saved"}
        if self.remember.isChecked():
            vals = {s.value() for s in self.segs.values()}
            if len(vals) == 1:
                SETTINGS["newer_policy"] = "saved" if vals == {"saved"} else "newest"
                SETTINGS.save()
        return ch


class SettingsDialog(Dialog):
    def __init__(self, win):
        super().__init__(win, "Settings", "Make Package Downloader look and behave the way you like.", 640)
        self.win = win
        self.body.addWidget(lbl("APPEARANCE", "section"))
        c, lay = card((4, 2, 4, 2), 0)
        th = Segmented([("system", "Match Windows"), ("light", "Light"), ("dark", "Dark")],
                       SETTINGS.get("theme", "system"), small=True)
        th.changed.connect(self._theme)
        self._row(lay, "Theme", "Light, dark, or follow Windows.", th)
        sw = QWidget()
        sl = QHBoxLayout(sw)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(6)
        self.swatches = {}
        for name in ACCENTS:
            b = QPushButton()
            b.setFixedSize(28, 28)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(name)
            b.clicked.connect(lambda _=False, n=name: self._accent(n))
            sl.addWidget(b)
            self.swatches[name] = b
        themed(self._paint_swatches)
        self._row(lay, "Accent colour", "Buttons, badges and the progress bar.", sw, line=True)
        self.body.addWidget(c)

        self.body.addWidget(lbl("FOLDERS", "section"))
        c, lay = card((4, 2, 4, 2), 0)
        self.lib_desc = self._row(lay, "Package library", win.lib.root,
                                  self._folder_btns(win.change_library, lambda: open_in_explorer(win.lib.root)))
        self.out_desc = self._row(lay, "Installers are saved to", output_dir(),
                                  self._folder_btns(self._change_output, lambda: open_in_explorer(output_dir())),
                                  line=True)
        self.body.addWidget(c)

        self.body.addWidget(lbl("DOWNLOADS", "section"))
        c, lay = card((4, 2, 4, 2), 0)
        pol = Segmented([("ask", "Ask"), ("saved", "Use saved"), ("newest", "Download")],
                        SETTINGS.get("newer_policy", "ask"), small=True)
        pol.changed.connect(lambda k: self._set("newer_policy", k))
        self._row(lay, "When PyPI has a newer version", "of a package the library already has.", pol)
        par = Segmented([("2", "2"), ("4", "4"), ("6", "6"), ("8", "8")], str(SETTINGS.get("parallel", 4)), small=True)
        par.changed.connect(lambda k: self._set("parallel", int(k)))
        self._row(lay, "Downloads at the same time", "More is faster on a good connection.", par, line=True)
        self.body.addWidget(c)
        self.foot.addWidget(lbl("Saved as you change them.", "faint"))
        self.foot.addStretch(1)
        b = button("Done", "primary", slot=self.accept)
        b.setDefault(True)
        self.foot.addWidget(b)

    def _row(self, lay, title, desc, widget, line=False):
        if line:
            lay.addWidget(hline())
        d = lbl("", "muted")
        self._desc(d, desc)
        lay.addLayout(hbox(vbox(lbl(title, "h3"), d, spacing=2), None, widget, spacing=16, margins=(16, 12, 16, 12)))
        return d

    def _desc(self, d, text):
        """One line; long folder paths are shortened in the middle (full text in the tooltip)."""
        fm = QFontMetricsF(d.font())
        d.setText(fm.elidedText(text, Qt.TextElideMode.ElideMiddle, 300))
        d.setToolTip(text)

    def _folder_btns(self, change, open_):
        w = QWidget()
        w.setLayout(hbox(button("Change…", slot=lambda: (change(), self._refresh())), button("Open", "ghost", slot=open_),
                         spacing=4))
        return w

    def _refresh(self):
        self._desc(self.lib_desc, self.win.lib.root)
        self._desc(self.out_desc, output_dir())

    def _change_output(self):
        d = QFileDialog.getExistingDirectory(self, "Where should installers be saved?", output_dir())
        if d:
            SETTINGS["output_dir"] = os.path.normpath(d)
            SETTINGS.save()

    def _set(self, k, v):
        SETTINGS[k] = v
        SETTINGS.save()

    def _theme(self, k):
        self._set("theme", k)
        apply_theme(QApplication.instance())

    def _accent(self, name):
        self._set("accent", name)
        apply_theme(QApplication.instance())

    def _paint_swatches(self):
        cur = SETTINGS.get("accent", "Blue")
        for name, b in self.swatches.items():
            col = ACCENTS[name][0 if P["theme"] == "dark" else 1]
            ring = P["fg"] if name == cur else "transparent"
            b.setStyleSheet("QPushButton { background: %s; border: 2px solid %s; border-radius: 14px; padding: 0; }"
                            "QPushButton:hover { border-color: %s; }" % (col, ring, P["muted"] if name != cur else ring))


# =========================================================================================
#  Installer window (runs on the PC without internet)
# =========================================================================================

class InstallerWindow(QWidget):
    def __init__(self, payload_file, argv):
        super().__init__()
        self.payload_file = payload_file
        self.title = os.path.splitext(os.path.basename(payload_file))[0]
        self.setWindowTitle(self.title)
        self.setWindowIcon(app_icon())
        self.setObjectName("page")
        self.ui_call = Bridge()
        self.inst = OfflineInstall(payload_file, lambda t: self.ui_call(self._log, t),
                                   lambda f, t: self.ui_call(self._prog, f, t))
        self.pys = []
        self.running = False
        self.done_ok = False
        self.extra_py = argv[argv.index("--python") + 1] if "--python" in argv and argv.index("--python") + 1 < len(argv) else None
        self._build("--user" in argv)
        self.resize(800, 600)
        threading.Thread(target=self._prepare, daemon=True).start()

    def _build(self, user):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        head = frame("chrome")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(24, 18, 24, 18)
        hl.setSpacing(14)
        logo = QLabel()
        logo.setPixmap(logo_pixmap(44))
        name = self.title.replace(INSTALL_SUFFIX, "").strip()
        self.sub = lbl("Unpacking…", "muted")
        hl.addWidget(logo)
        hl.addLayout(vbox(lbl("Packages for “%s”" % name, "h2"), self.sub, spacing=2), 1)
        outer.addWidget(head)

        body = QVBoxLayout()
        body.setContentsMargins(20, 18, 20, 18)
        body.setSpacing(14)
        outer.addLayout(body, 1)
        c, lay = card((22, 18, 22, 18), 10)
        body.addWidget(c, 1)

        lay.addWidget(lbl("PACKAGES", "section"))
        chips = QWidget()
        self.chip_flow = FlowLayout(chips, 6)
        lay.addWidget(chips)

        lay.addSpacing(6)
        lay.addWidget(lbl("INSTALL INTO", "section"))
        self.py_cb = QComboBox()
        self.py_cb.addItem("Looking for Python on this PC…")
        self.py_cb.setEnabled(False)
        self.py_cb.currentIndexChanged.connect(lambda *_: self._check_choice())
        self.browse = button("Browse…", None, "folder-open", "Pick python.exe yourself", self._browse)
        self.browse.setEnabled(False)
        row = hbox(self.py_cb, self.browse, spacing=8)
        row.setStretch(0, 1)
        self.py_cb.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.py_cb.setMinimumContentsLength(30)
        lay.addLayout(row)
        self.py_icon = QLabel()
        self.py_icon.setFixedSize(16, 16)
        self.py_note = lbl("", "muted", wrap=True)
        lay.addLayout(hbox(self.py_icon, self.py_note, spacing=8))
        lay.itemAt(lay.count() - 1).setAlignment(Qt.AlignmentFlag.AlignTop)

        self.user_sw = ToggleSwitch(user)
        lay.addSpacing(2)
        lay.addLayout(hbox(self.user_sw, vbox(lbl("Only for my Windows user", "h3"),
                                              lbl("No admin rights needed", "faint"), spacing=0), None, spacing=12))

        lay.addSpacing(6)
        self.pbox = frame("soft")
        pl = QVBoxLayout(self.pbox)
        pl.setContentsMargins(16, 14, 16, 14)
        pl.setSpacing(10)
        self.st_icon = QLabel()
        self.st_icon.setFixedSize(20, 20)
        self.st_icon.hide()
        self.stage = lbl("Getting ready…", "h3")
        self.stage.setWordWrap(True)
        self.pct = lbl("", "big")
        row = hbox(self.st_icon, self.stage, self.pct, spacing=8)
        row.setStretch(1, 1)
        pl.addLayout(row)
        self.bar = ProgressBar(8)
        self.bar.indeterminate()
        pl.addWidget(self.bar)
        lay.addWidget(self.pbox)

        self.log_btn = button("Show details", "ghost", "chevron-down", slot=self._toggle_log)
        lay.addLayout(hbox(self.log_btn, None))
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log.hide()
        lay.addWidget(self.log, 1)
        lay.addStretch(0)

        foot = frame("dlgFoot")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(20, 12, 20, 12)
        fl.addWidget(lbl("No internet needed. Everything is inside this file.", "faint"))
        fl.addStretch(1)
        self.close_btn = button("Close", slot=self.close)
        self.install_btn = button("Install", "primary", "download", slot=self._start)
        self.install_btn.setEnabled(False)
        fl.addWidget(self.close_btn)
        fl.addWidget(self.install_btn)
        outer.addWidget(foot)

    def showEvent(self, e):
        style_titlebar(self)
        super().showEvent(e)

    def _toggle_log(self):
        show = not self.log.isVisible()
        self.log.setVisible(show)
        self.log_btn.setText("Hide details" if show else "Show details")
        set_icon(self.log_btn, "chevron-up" if show else "chevron-down", "muted", 16)

    def _log(self, t):
        self.log.appendPlainText(t)

    def _prog(self, frac, text):
        self.bar.set(frac)
        self.pct.setText("%d%%" % int(frac * 100))
        self.stage.setText(text)

    def _status(self, text, kind=None):
        self.stage.setText(text)
        self.stage.setObjectName({"ok": "okText", "bad": "badText"}.get(kind, "h3"))
        repolish(self.stage)
        if kind:
            self.st_icon.setPixmap(svg_pixmap(svg_markup("check-circle" if kind == "ok" else "x-circle",
                                                         P[kind], 2), 20))
            self.st_icon.show()
        else:
            self.st_icon.hide()

    def _prepare(self):
        try:
            man = self.inst.unpack()
        except Exception as e:
            self.ui_call(self._fail, "Could not unpack the packages: %s" % e)
            return
        self.ui_call(self._show_manifest, man)
        pys = self.inst.ranked_pythons(self.extra_py)
        self.ui_call(self._got_pythons, pys)

    def _show_manifest(self, man):
        self.sub.setText("%d packages · %d files · made %s for Python %s" % (
            len(man["packages"]), len(man["files"]), man.get("created", ""), ", ".join(man["targets"])))
        for p in man["packages"]:
            c = QLabel(p)
            c.setObjectName("chipLabel")
            c.setStyleSheet("background: %s; color: %s; border-radius: 12px; padding: 4px 11px; font-weight: 600;"
                            % (P["accent_soft"], P["accent"]))
            self.chip_flow.addWidget(c)
        self.stage.setText("Looking for Python on this PC…")
        self._log("Packages: %s" % ", ".join(man["packages"]))

    def _label(self, p):
        return "Python %s  ·  %s%s  ·  %s" % (p["full"], "64-bit" if "amd64" in p["platform"] else p["platform"],
                                             "  ·  virtual env" if p["venv"] else "", p["exe"])

    def _got_pythons(self, pys):
        self.pys = pys
        self.browse.setEnabled(True)
        man = self.inst.man
        self.py_cb.blockSignals(True)
        self.py_cb.clear()
        if not pys:
            self.py_cb.addItem("Python is not installed on this PC")
            self.py_cb.blockSignals(False)
            self._note("Install Python %s first (the Microsoft Store shortcut doesn't count), then run this again, or "
                       "click Browse… to pick python.exe." % man["targets"][0], "bad")
            self._idle("Waiting for a Python")
            return
        for p in pys:
            self.py_cb.addItem(self._label(p))
        self.py_cb.setEnabled(True)
        self.py_cb.blockSignals(False)
        self.py_cb.setCurrentIndex(0)
        self.install_btn.setEnabled(True)
        self._check_choice()
        if any(self.inst.matches(p) for p in pys) or self.extra_py:
            self._start()          # the normal case: just install, no questions
        else:
            self._idle("Pick a Python and click Install")

    def _note(self, text, kind):
        self.py_note.setText(text)
        self.py_note.setObjectName({"ok": "okText", "bad": "badText", "warn": "warnText"}[kind])
        repolish(self.py_note)
        ic = {"ok": "check-circle", "bad": "x-circle", "warn": "alert"}[kind]
        self.py_icon.setPixmap(svg_pixmap(svg_markup(ic, P[kind], 2), 16))

    def _check_choice(self):
        i = self.py_cb.currentIndex()
        if not self.pys or i < 0 or i >= len(self.pys):
            return
        p = self.pys[i]
        if self.inst.matches(p):
            self._note("The packages were made for this Python.", "ok")
        else:
            self._note("These packages were made for Python %s (%s). Packages with compiled parts will fail here; "
                       "pure-Python ones may still work." % (" / ".join(self.inst.man["targets"]),
                                                             self.inst.man["platform"]), "warn")

    def _idle(self, text):
        self.bar.set(0)
        self.pct.setText("")
        self._status(text)

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "Choose python.exe", "", "python.exe (python*.exe);;All files (*.*)")
        if not p:
            return
        info = _probe_python(p)
        if not info:
            self._note("That file didn't run as Python.", "bad")
            return
        self.pys.insert(0, info)
        self.py_cb.insertItem(0, self._label(info))
        self.py_cb.setCurrentIndex(0)
        self.py_cb.setEnabled(True)
        self.install_btn.setEnabled(True)
        self._check_choice()

    def _start(self):
        if not self.pys or self.running:
            return
        self.running = True
        p = self.pys[self.py_cb.currentIndex()]
        for w in (self.install_btn, self.browse, self.close_btn, self.py_cb, self.user_sw):
            w.setEnabled(False)
        self._log("")
        self._log("Installing into %s" % p["exe"])
        self._status("Starting…")
        self._prog(0.02, "Starting…")
        user = self.user_sw.isChecked()

        def work():
            try:
                rc = self.inst.install(p, user=user)
            except Exception:
                self.ui_call(self._log, traceback.format_exc())
                rc = 5
            self.ui_call(self._done, rc)
        threading.Thread(target=work, daemon=True).start()

    def _done(self, rc):
        self.running = False
        self.close_btn.setEnabled(True)
        if rc == 0:
            self.done_ok = True
            self.bar.set(1.0)
            self.pct.setText("100%")
            self._status("All done. %d packages installed." % len(self.inst.man["packages"]), "ok")
            failed = self.inst.man.get("failed") or {}
            if failed:
                self._log("Not included (couldn't be downloaded): %s" % ", ".join(failed))
            self.close_btn.setObjectName("primary")
            repolish(self.close_btn)
            self.install_btn.hide()
        else:
            self.install_btn.setText("Try again")
            for w in (self.install_btn, self.browse, self.py_cb, self.user_sw):
                w.setEnabled(True)
            self._idle("")
            self._status("Installing failed. Open the details to see why.", "bad")
            if not self.log.isVisible():
                self._toggle_log()

    def _fail(self, text):
        self.bar.set(0)
        self._status("Error", "bad")
        self._log(text)
        if not self.log.isVisible():
            self._toggle_log()

    def closeEvent(self, e):
        if self.running:
            e.ignore()
            return
        self.inst.cleanup()
        e.accept()


# =========================================================================================
#  Entry points used by package_downloader.main()
# =========================================================================================

def run_downloader(files):
    app = make_app()
    w = DownloaderWindow(files)
    w.show()
    return app.exec()


def run_installer(payload, argv):
    app = make_app()
    w = InstallerWindow(payload, argv)
    w.show()
    return app.exec()
