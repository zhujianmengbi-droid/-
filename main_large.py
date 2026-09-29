# -*- coding: utf-8 -*-
"""逐渐工具中心的大 GUI 外壳。

当前把运行环境整理功能作为一个独立分区接入，同时预留总览、战局数据、
快捷工具和大厅聊天等入口。分区使用独立页面，后续新增功能只需要在
``_build_placeholder`` 或新的页面构建方法中注册即可，不会破坏已有的清理逻辑。
"""
import sys
import time
import json
import re
import ctypes
import uuid
import math
import os
import struct
import tempfile
import wave
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from functools import lru_cache
from collections import deque
from datetime import datetime

from PySide6.QtCore import (
    QByteArray, QEasingCurve, QPropertyAnimation, QSettings, QThread, QTimer, QSize, Qt, QRect, QRectF, QPoint, QPointF, QUrl, Signal, QEvent, QObject,
)
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QKeySequence, QLinearGradient, QPainter, QPalette, QPainterPath, QPen, QPixmap, QRadialGradient
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtNetwork import QAbstractSocket
from PySide6.QtWebSockets import QWebSocket
try:
    from PySide6.QtMultimedia import QSoundEffect
except Exception:  # pragma: no cover - portable builds may omit Multimedia
    QSoundEffect = None
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QGridLayout,
    QLineEdit,
    QComboBox,
    QAbstractItemView,
    QCheckBox,
    QHeaderView,
    QProgressBar,
    QPushButton,
    QSlider,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import bugland_api
import match_data
import optimizer
import taskbar_switcher_core
from main import App, STYLE, Backdrop, TitleBar, label, panel


# The version is also used as the GitHub release tag (for example, v2026.09.29).
# Bump it when publishing a new release so existing installations can discover it.
APP_VERSION = '2026.09.30.13'
GITHUB_REPOSITORY = 'zhujianmengbi-droid/-'
GITHUB_REPOSITORY_URL = f'https://github.com/{GITHUB_REPOSITORY}'
GITHUB_LATEST_RELEASE_API = (
    f'https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest')
# 大厅聊天使用 Supabase Realtime 公共频道。Broadcast 只经过 Realtime 转发，
# 不写入应用自己的数据库表；消息列表和在线状态都只存在本次运行的客户端内存。
# publishable key 设计上允许随桌面客户端发布，绝不在客户端放置 secret key。
SUPABASE_PROJECT_REF = 'jhgkpxuaaakgdjfbnurp'
SUPABASE_PROJECT_URL = f'https://{SUPABASE_PROJECT_REF}.supabase.co'
SUPABASE_PUBLISHABLE_KEY = 'sb_publishable_uhS8jC6RrSmUSEblKfeyYw_cZdhk7RH'
SUPABASE_REALTIME_URL = (
    f'wss://{SUPABASE_PROJECT_REF}.supabase.co/realtime/v1/websocket')
SUPABASE_CHAT_TOPIC = 'realtime:public-lobby'
LOBBY_CHAT_SERVER_URL = SUPABASE_REALTIME_URL
LOBBY_CHAT_JOIN_PATH = '/api/join'
LOBBY_CHAT_MESSAGES_PATH = '/api/chat/messages'
LOBBY_CHAT_PRESENCE_PATH = '/api/chat/presence'
GAME_ID_SETTINGS_KEY = 'profile/gameId'
CHAT_SESSION_CACHE_KEY = 'chat/sessionMessages'
CHAT_SOUND_ENABLED_KEY = 'chat/soundEnabled'
CHAT_SOUND_VOLUME_KEY = 'chat/soundVolume'
TASKBAR_DEFAULT_BINDINGS = {
    index: f'Alt+{digit}'
    for index, digit in enumerate('1234567890')
}


def _windows_hotkey_parts(sequence):
    """把单个 Qt 组合键转换成 RegisterHotKey 的修饰键和 VK 编码。"""
    parsed = QKeySequence.fromString(
        str(sequence or ''), QKeySequence.SequenceFormat.PortableText)
    if parsed.count() != 1:
        return None
    combined = parsed[0].toCombined()
    modifier_mask = 0xFE000000
    qt_modifiers = combined & modifier_mask
    key = combined & 0x01FFFFFF
    if ord('0') <= key <= ord('9') or ord('A') <= key <= ord('Z'):
        virtual_key = key
    else:
        qt_f1 = int(Qt.Key.Key_F1.value)
        qt_f12 = int(Qt.Key.Key_F12.value)
        if qt_f1 <= key <= qt_f12:
            virtual_key = 0x70 + key - qt_f1
        else:
            return None

    modifiers = 0x4000  # MOD_NOREPEAT
    if qt_modifiers & int(Qt.KeyboardModifier.ControlModifier.value):
        modifiers |= 0x0002  # MOD_CONTROL
    if qt_modifiers & int(Qt.KeyboardModifier.AltModifier.value):
        modifiers |= 0x0001  # MOD_ALT
    if qt_modifiers & int(Qt.KeyboardModifier.ShiftModifier.value):
        modifiers |= 0x0004  # MOD_SHIFT
    if qt_modifiers & int(Qt.KeyboardModifier.MetaModifier.value):
        modifiers |= 0x0008  # MOD_WIN
    if not modifiers & 0x000B:  # Require Ctrl, Alt or Win to avoid bare keys.
        return None
    return modifiers, virtual_key


def _release_version_tuple(value):
    """Convert a GitHub tag such as ``v2026.09.29`` to comparable numbers."""
    numbers = re.findall(r'\d+', str(value or ''))
    return tuple(int(number) for number in numbers) if numbers else (0,)


def _is_newer_release(tag_name):
    remote = _release_version_tuple(tag_name)
    current = _release_version_tuple(APP_VERSION)
    width = max(len(remote), len(current))
    return remote + (0,) * (width - len(remote)) > current + (0,) * (width - len(current))


class ThemedComboBox(QComboBox):
    """统一的主题下拉框。

    Qt/Fusion 会给 popup view 加一层平台边框，并在空间不足时把弹层翻到
    控件上方。排行榜和设置里的小型筛选框因此会出现黑边、不同圆角和不同
    方向。这个轻量子类给 popup 一个无边框顶层窗口，再在 Qt 完成布局后把
    它固定到控件正下方；颜色和边框交给应用级 QSS 的 ``comboPopup`` 规则。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrame(False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._prepare_popup()

    def _prepare_popup(self):
        view = self.view()
        if view is None:
            return
        # Leaderboard selectors have only a handful of choices. Keep the
        # complete list in one view so selecting a period/mode never requires
        # a second wheel gesture.
        self.setMaxVisibleItems(max(1, self.count()))
        view.setObjectName('comboPopupView')
        view.setMinimumHeight(0)
        view.setFrameShape(QFrame.Shape.NoFrame)
        view.setFrameShadow(QFrame.Shadow.Plain)
        view.setContentsMargins(0, 0, 0, 0)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        popup = view.window()
        popup.setObjectName('comboPopup')
        popup.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        popup.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        # NoDropShadowWindowHint is available on Windows and most Qt desktop
        # backends.  Keep the fallback for older Qt builds used by portable EXE.
        no_shadow = getattr(Qt.WindowType, 'NoDropShadowWindowHint', None)
        if no_shadow is not None:
            popup.setWindowFlag(no_shadow, True)

    def _popup_content_height(self):
        view = self.view()
        if view is None or self.count() <= 0:
            return 0
        row_height = max(1, view.sizeHintForRow(0))
        # Match the 5px view padding and a small top/bottom breathing room
        # without relying on the platform style's maxVisibleItems default.
        return row_height * self.count() + 12

    def _active_theme(self):
        widget = self
        while widget is not None:
            theme = getattr(widget, 'current_theme', None)
            if theme in MATCH_ROW_PALETTES:
                return theme
            widget = widget.parentWidget()
        app = QApplication.instance()
        theme = app.property('_themeKey') if app is not None else None
        return theme if theme in MATCH_ROW_PALETTES else 'light'

    def _apply_popup_style(self):
        """Paint the top-level popup directly as well as through global QSS.

        Qt does not always propagate application selectors to the private
        QComboBox container after its native flags are replaced.  A direct
        stylesheet keeps the surface identical across Fusion and Windows
        backends, including when the user switches theme while the window is
        open.
        """
        theme = self._active_theme()
        colors = MATCH_ROW_PALETTES.get(theme, MATCH_ROW_PALETTES['light'])
        surface = INPUT_SURFACES.get(theme, INPUT_SURFACES['light'])
        # Popup surfaces follow the same opaque input surface as the focused
        # search field. This avoids a light popup on dark themes (and the
        # reverse) when Qt's private combo container bypasses application QSS.
        popup_fill = surface['fill']
        popup_text = surface['text']
        popup_border = surface['border']
        popup_hover = colors['recent_hover']
        popup = self.view().window() if self.view() is not None else None
        if popup is None:
            return
        popup.setStyleSheet(f"""
QFrame#comboPopup {{
    color: {popup_text};
    background: {popup_fill};
    border: 1px solid {popup_border};
    border-radius: 10px;
    font-family: 'Microsoft YaHei UI';
    font-size: 13px;
}}
QListView#comboPopupView {{
    color: {popup_text};
    background: transparent;
    border: none;
    outline: none;
    padding: 6px;
    font-family: 'Microsoft YaHei UI';
    font-size: 13px;
}}
QListView#comboPopupView::item {{
    color: {popup_text};
    background: transparent;
    min-height: 32px;
    padding: 7px 12px;
    border-radius: 7px;
}}
QListView#comboPopupView::item:hover,
QListView#comboPopupView::item:selected {{
    color: {popup_text};
    background: {popup_hover};
}} 
QListView#comboPopupView::item:selected {{ font-weight: 700; }}
QAbstractItemView#comboPopupView {{ color: {popup_text}; background: transparent; border: none; outline: none; }}
QAbstractItemView#comboPopupView::item {{ color: {popup_text}; background: transparent; min-height: 32px; padding: 7px 12px; border-radius: 7px; }}
QAbstractItemView#comboPopupView::item:hover, QAbstractItemView#comboPopupView::item:selected {{ color: {popup_text}; background: {popup_hover}; }}
QComboBoxPrivateScroller {{ background: {popup_fill}; border: none; }}
QScrollBar {{ background: transparent; border: none; }}
QScrollBar::handle {{ background: {popup_border}; border-radius: 4px; min-height: 20px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
""")

    def showPopup(self):
        self._prepare_popup()
        super().showPopup()
        popup = self.view().window() if self.view() is not None else None
        if popup is not None:
            # QComboBoxPrivateContainer is created with a native frame on
            # Windows.  Replace its flags after Qt has created it, while it is
            # hidden for one event turn, so the platform cannot add a black
            # outline or shadow back to the themed surface.
            popup.hide()
            flags = Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
            no_shadow = getattr(Qt.WindowType, 'NoDropShadowWindowHint', None)
            if no_shadow is not None:
                flags |= no_shadow
            popup.setWindowFlags(flags)
            popup.setObjectName('comboPopup')
            popup.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
            self._apply_popup_style()
            # Set the target geometry before showing the new top-level window;
            # this prevents a one-frame upward flip near the bottom edge.
            origin = self.mapToGlobal(QPoint(0, self.height()))
            width = max(self.width(), popup.sizeHint().width(), self.view().sizeHintForColumn(0) + 24)
            height = max(popup.height(), self._popup_content_height())
            self.view().setMinimumHeight(max(0, height - 2))
            popup.resize(width, height)
            popup.move(origin)
            popup.show()
            popup.raise_()
        # QComboBox positions the popup asynchronously.  Repositioning on the
        # next event turn keeps the same width while making every popup expand
        # downward from the combo's lower edge.
        QTimer.singleShot(0, self._place_popup_below)

    def _place_popup_below(self):
        view = self.view()
        if view is None or not view.isVisible():
            return
        popup = view.window()
        if popup is None:
            return
        origin = self.mapToGlobal(QPoint(0, self.height()))
        width = max(self.width(), popup.sizeHint().width(), view.sizeHintForColumn(0) + 24)
        view.setMinimumHeight(0)
        height = max(popup.height(), self._popup_content_height())
        view.setMinimumHeight(max(0, height - 2))
        popup.resize(width, height)
        popup.move(origin)


LARGE_STYLE = STYLE + """
QLineEdit { border-radius: 9px; padding: 10px 12px; min-height: 20px; }
/* Keep small controls softly rounded even when a page does not assign a
   specialised object name.  Radius only: fill, border and geometry remain
   theme controlled below. */
QComboBox, QAbstractSpinBox { border-radius: 8px; }
QToolTip { border-radius: 8px; }
QScrollBar::handle:horizontal, QScrollBar::handle:vertical { border-radius: 4px; }
QFrame#updateBanner { min-height: 42px; background: rgba(63,121,180,34); border: 1px solid rgba(63,121,180,96); border-radius: 10px; }
QLabel#updateTitle { color: #1c2c3d; font-size: 12px; font-weight: 700; }
QLabel#updateHint { color: #5b6d80; font-size: 11px; }
QPushButton#updateOpen { min-height: 28px; padding: 4px 11px; border-radius: 8px; background: #3f79b4; color: #ffffff; border: 1px solid #6b9bc9; font-size: 11px; font-weight: 700; }
QPushButton#updateOpen:hover { background: #32699f; }
QPushButton#updateDismiss { min-width: 24px; min-height: 24px; padding: 0; border-radius: 7px; background: transparent; color: #5b6d80; border: 1px solid transparent; font-size: 16px; }
QPushButton#updateDismiss:hover { background: rgba(61,91,120,24); color: #1c2c3d; }
QLabel#statLabel { font-size: 10px; color: #6f8296; }
QLabel#statValue { font-size: 17px; font-weight: 700; }
QLabel#brand { font-size: 17px; font-weight: 600; color: #f2f7ff; }
QLabel#brandHint { color: #8ea5c0; font-size: 11px; }
QLabel#brandMark { color: #102640; background: #b7d7ff; border-radius: 12px; font-size: 12px; font-weight: 700; padding: 7px 6px; }
QLabel#eyebrow { color: #86c8ff; font-size: 10px; font-weight: 700; }
QLabel#pageTitle { font-size: 25px; font-weight: 650; }
QLabel#heroTitle { font-size: 21px; font-weight: 650; }
QLabel#pageHint { color: #afc0d5; font-size: 12px; }
QLabel#metricSmall { font-size: 25px; font-weight: 600; }
QLabel#metricSmallCompact { font-size: 16px; font-weight: 600; }
QLabel#metricLabel { color: #9db2ca; font-size: 11px; }
QLabel#cardTitle { font-size: 14px; font-weight: 600; }
QLabel#cardHint { color: #a6bad1; font-size: 11px; }
QLabel#metricIcon { background: rgba(165,210,255,22); border-radius: 9px; padding: 5px; }
/* Detail metrics use a deliberately stronger icon tile so the symbol remains
   readable at a glance beside the value.  The tone-specific rules below
   replace the fallback border/background for every theme. */
QLabel#statMetricIcon { background: rgba(165,210,255,28); border: 1px solid rgba(188,220,255,78); border-radius: 7px; padding: 2px; }
QFrame#heroIconTile { background: rgba(201,228,255,22); border-radius: 18px; }
QFrame#moduleIconTile { background: rgba(157,205,255,18); border-radius: 16px; }
QFrame#sectionIcon { background: rgba(157,205,255,22); border-radius: 9px; }
QFrame#rowGlyph { background: rgba(163,205,255,18); border-radius: 9px; }
QFrame#statusGlyph { background: rgba(99,190,170,24); border-radius: 12px; }
QLabel#statusDot { color: #9be3cb; font-size: 18px; }
QLabel#statusPill { color: #b8e9df; background: rgba(99,190,170,25); border: 1px solid rgba(145,230,208,40); border-radius: 10px; padding: 5px 10px; font-size: 11px; }
QLabel#placeholderIcon { color: #b7d7ff; font-size: 34px; font-weight: 600; }
QLabel#placeholderText { color: #c0cede; font-size: 13px; }
QFrame#sidebar { background: rgba(10, 23, 41, 156); border: 1px solid rgba(202,224,255,50); border-radius: 16px; }
QFrame#navLine { background: rgba(210,230,255,18); }
QPushButton#navButton { text-align: left; color: #b8c8dc; background: transparent; border: 1px solid transparent; border-radius: 10px; padding: 10px 13px; min-height: 28px; }
QPushButton#navButton { qproperty-iconSize: 20px 20px; }
QPushButton#navButton:hover { color: #eff5ff; background: rgba(182,213,255,26); border-color: rgba(202,224,255,34); }
QPushButton#navButton:checked { color: #102640; background: #b7d7ff; border-color: #d0e5ff; border-left: 3px solid #ffffff; font-weight: 600; }
QPushButton#navButton:disabled { color: #6e829b; }
QPushButton#windowControl { background: rgba(182,213,255,18); border: none; border-radius: 11px; padding: 0; }
QPushButton#windowControl:hover { background: rgba(182,213,255,34); }
QPushButton#windowControl[controlRole="close"]:hover { background: #d95463; }
QPushButton#settingsButton { text-align: left; color: #b8c8dc; background: transparent; border: 1px solid transparent; border-top-color: rgba(214,232,255,45); border-radius: 10px; margin-top: 6px; padding: 10px 13px; min-height: 28px; }
QPushButton#settingsButton { qproperty-iconSize: 20px 20px; }
QPushButton#settingsButton:hover { color: #eff5ff; background: rgba(182,213,255,26); border-color: rgba(202,224,255,34); }
QPushButton#settingsButton:checked { color: #102640; background: #b7d7ff; border-color: #d0e5ff; border-left: 3px solid #ffffff; font-weight: 600; }
QFrame#hero { background: rgba(65, 106, 164, 105); border: 1px solid rgba(202,224,255,72); border-left: 3px solid #a5d2ff; border-radius: 16px; }
QFrame#metricCard { background: rgba(20,35,57,96); border: 1px solid rgba(202,224,255,54); border-radius: 12px; }
QFrame#featureCard { background: rgba(20,35,57,70); border: 1px solid rgba(202,224,255,40); border-radius: 12px; }
QFrame#featureRow { background: rgba(182,213,255,12); border: 1px solid rgba(202,224,255,20); border-radius: 9px; }
QLabel#playerName { font-size: 14px; font-weight: 650; }
QFrame#matchRecord { border: 1px solid transparent; border-radius: 36px; }
QLabel#matchRowTitle { font-size: 14px; font-weight: 600; }
QLabel#matchRowMeta { font-size: 11px; }
QFrame#statChip { border: none; border-radius: 10px; }
QFrame#matchDetailHero { border: none; border-radius: 14px; }
QFrame#matchDetailRibbon { border: none; border-radius: 26px; padding: 2px; }
QLabel#queryTargetBadge { border-radius: 999px; padding: 5px 10px; font-size: 10px; font-weight: 700; }
QLabel#detailKicker { font-size: 10px; font-weight: 700; letter-spacing: 1px; }
QLabel#detailHighlight { font-size: 16px; font-weight: 700; }
QLabel#detailSubline { font-size: 12px; }
QLabel#detailStatValue { font-size: 19px; font-weight: 700; }
QLabel#detailStatCaption { font-size: 10px; }
QProgressBar#detailPulse { border: none; border-radius: 3px; height: 5px; background: rgba(255,255,255,35); }
QProgressBar#detailPulse::chunk { border-radius: 3px; background: #f7d47a; }
QComboBox#animationSpeed { min-height: 38px; padding: 5px 12px; border-radius: 9px; }
QFrame#dailyStatChip { border: none; border-radius: 11px; }
QPushButton#recentQueryButton { border-radius: 14px; padding: 5px 10px; min-height: 26px; font-size: 11px; }
QFrame#leaderboardHero { border-radius: 16px; }
QFrame#leaderboardCard { border-radius: 15px; }
QFrame#leaderboardRow { border-radius: 12px; }
QFrame#matchDetailPanel { border-radius: 16px; }
QFrame#teamOverviewCard { border-radius: 15px; }
QFrame#teamOverviewItem { border-radius: 12px; }
QFrame#teamOverviewItem[teamTint="blue"] { border-top: 3px solid #5b8fd8; }
QFrame#teamOverviewItem[teamTint="gold"] { border-top: 3px solid #c99a43; }
QFrame#teamOverviewItem[teamTint="violet"] { border-top: 3px solid #9b7bd3; }
QFrame#teamOverviewItem[teamTint="teal"] { border-top: 3px solid #48a7a1; }
QLabel#leaderboardRank { font-size: 20px; font-weight: 700; }
QLabel#leaderboardName { font-size: 14px; font-weight: 650; }
QLabel#leaderboardScore { font-size: 17px; font-weight: 750; }
QLabel#leaderboardMeta { font-size: 11px; }
QComboBox#leaderboardFilter { min-height: 38px; padding: 5px 12px; border-radius: 11px; }
QComboBox#leaderboardFilter::drop-down { width: 28px; border: none; border-left: 1px solid rgba(255,255,255,30); }
QComboBox#leaderboardFilter::down-arrow { width: 9px; height: 9px; }
QComboBox#leaderboardFilter QAbstractItemView { padding: 5px; border-radius: 10px; outline: none; }
QComboBox#leaderboardFilter QAbstractItemView::item { min-height: 32px; padding: 7px 12px; border-radius: 7px; }
/* Combo popups are top-level QFrame containers on Windows, so style both the
   view and its container.  The explicit container rule removes Fusion's
   platform black frame; ThemedComboBox also sets FramelessWindowHint. */
QComboBox { outline: none; }
QComboBox::drop-down { border: none; }
QFrame#comboPopup { border-radius: 10px; }
QAbstractItemView#comboPopupView { border: none; outline: none; padding: 5px; }
QAbstractItemView#comboPopupView::item { min-height: 32px; padding: 7px 12px; border-radius: 7px; }
QFrame#placeholder { background: rgba(20,35,57,70); border: 1px solid rgba(202,224,255,42); border-radius: 16px; }
QFrame#settingsSidebar { background: rgba(20,35,57,80); border: 1px solid rgba(202,224,255,48); border-radius: 14px; }
QFrame#settingsCard { background: rgba(20,35,57,80); border: 1px solid rgba(202,224,255,48); border-radius: 14px; }
QFrame#settingsOverlay { background: #12243b; border: 1px solid rgba(214,232,255,90); border-radius: 0; }
QPushButton#settingsBack { min-height: 36px; padding: 8px 16px; }
QPushButton#settingsCategory { text-align: left; color: #b8c8dc; background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 9px 11px; min-height: 32px; }
QPushButton#settingsCategory:hover { color: #eff5ff; background: rgba(182,213,255,20); }
QPushButton#settingsCategory:checked { color: #102640; background: #b7d7ff; font-weight: 600; }
QPushButton#themeOption { text-align: left; color: #b8c8dc; background: rgba(182,213,255,12); border: 1px solid rgba(202,224,255,34); border-radius: 10px; padding: 11px 12px; min-height: 66px; }
QPushButton#themeOption:hover { color: #eff5ff; background: rgba(182,213,255,25); border-color: #7495b9; }
QPushButton#themeOption:checked { color: #102640; background: #b7d7ff; border-color: #d0e5ff; font-weight: 600; }
"""


class GitHubUpdateWorker(QThread):
    """Fetch the latest public GitHub release without blocking the GUI thread."""

    checked = Signal(object)

    def __init__(self, endpoint, parent=None):
        super().__init__(parent)
        self.endpoint = endpoint

    def run(self):
        result = None
        try:
            request = Request(self.endpoint, headers={
                'Accept': 'application/vnd.github+json',
                'User-Agent': 'DEV-King-Optimizer-Updater',
                'X-GitHub-Api-Version': '2022-11-28',
            })
            with urlopen(request, timeout=6) as response:
                if response.status == 200:
                    result = json.loads(response.read().decode('utf-8'))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
            result = None
        self.checked.emit(result)


class BuglandRequestWorker(QThread):
    completed = Signal(str, object, str)

    def __init__(self, action, endpoint, payload, token, parent=None):
        super().__init__(parent)
        self.action = action
        self.endpoint = endpoint
        self.payload = payload
        self.token = token

    def run(self):
        try:
            result = bugland_api.post_json(self.endpoint, self.payload, self.token)
        except Exception as error:
            self.completed.emit(self.action, None, str(error))
        else:
            self.completed.emit(self.action, result, '')


def _player_response_exists(response):
    """Return whether the BuGLand ``/player`` response contains a player.

    The service has returned a couple of compatible envelopes over time
    (``data``/``player`` and direct player fields).  Keep the login gate
    tolerant of those envelopes while treating an empty successful response
    as a missing player.
    """
    if response is None:
        return False
    if isinstance(response, list):
        return bool(response)
    if not isinstance(response, dict):
        return bool(response)
    if response.get('success') is False:
        return False
    try:
        code = int(response.get('code'))
    except (TypeError, ValueError):
        code = 0
    if code and not 200 <= code < 300:
        return False
    identity_keys = {
        'username', 'player_name', 'playerName', 'playername', 'name', 'uuid',
        'player_uuid', 'playerUuid', 'rank', 'guild', 'vip',
    }

    def has_identity(value):
        if isinstance(value, list):
            return any(has_identity(item) for item in value)
        if not isinstance(value, dict):
            return value not in (None, '', False)
        # The /player endpoint returns data={uuid: null, playername: null}
        # for an unknown name.  A non-empty envelope alone is not proof that
        # a player exists; require at least one populated identity field.
        for key in identity_keys:
            if value.get(key) not in (None, '', False):
                return True
        for key in ('player', 'playerInfo', 'player_info', 'data', 'result'):
            if key in value and has_identity(value.get(key)):
                return True
        return False

    for key in ('player', 'playerInfo', 'player_info', 'data', 'result'):
        value = response.get(key)
        if has_identity(value):
            return True
    # Some deployments return the player object directly.
    return has_identity(response)


class PlayerLoginDialog(QDialog):
    """Blocking game-ID gate shown before the main GUI on first launch.

    Network validation runs on ``BuglandRequestWorker`` so the dialog keeps
    painting and remains cancellable while the API is being contacted.
    """

    def __init__(self, token, initial_name='', theme='light', parent=None):
        super().__init__(parent)
        self._token = str(token or '').strip()
        self._theme = theme if theme in THEME_PALETTES else 'light'
        self._worker = None
        self.player_name = ''
        self.setObjectName('playerLoginDialog')
        self.setWindowTitle('ZJ HUB · 登录游戏 ID')
        self.setModal(True)
        # The native Windows dialog frame ignores QSS corner radii and leaves
        # square white/black corners around the styled surface.  Use a
        # translucent frameless dialog and paint one antialiased rounded body
        # ourselves.  The dialog already has explicit in-app cancel/confirm
        # actions, so no native title bar controls are needed here.
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowSystemMenuHint)
        self.setFixedSize(500, 330)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self._apply_dialog_style()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 20)
        outer.setSpacing(12)
        heading = QHBoxLayout()
        mark = QLabel('ZJ')
        mark.setObjectName('playerLoginMark')
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(46, 46)
        heading.addWidget(mark)
        copy = QVBoxLayout()
        copy.setSpacing(3)
        title = QLabel('登录游戏 ID')
        title.setObjectName('playerLoginTitle')
        copy.addWidget(title)
        subtitle = QLabel('验证布吉岛玩家后，再打开 ZJ HUB')
        subtitle.setObjectName('playerLoginSubtitle')
        copy.addWidget(subtitle)
        heading.addLayout(copy, 1)
        outer.addLayout(heading)

        card = QFrame()
        card.setObjectName('playerLoginCard')
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(8)
        prompt = QLabel('游戏 ID')
        prompt.setObjectName('playerLoginPrompt')
        card_layout.addWidget(prompt)
        self.player_input = QLineEdit()
        self.player_input.setObjectName('themedInput')
        self.player_input.setPlaceholderText('输入布吉岛游戏 ID')
        self.player_input.setMaxLength(64)
        self.player_input.setMinimumHeight(44)
        self.player_input.setClearButtonEnabled(True)
        self.player_input.returnPressed.connect(self.verify_player)
        if initial_name:
            self.player_input.setText(str(initial_name))
        card_layout.addWidget(self.player_input)
        self.status_label = QLabel('输入后点击验证，成功后会保存到本机。')
        self.status_label.setObjectName('playerLoginStatus')
        self.status_label.setWordWrap(True)
        card_layout.addWidget(self.status_label)
        outer.addWidget(card)
        outer.addStretch(1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton('退出')
        cancel.setObjectName('playerLoginCancel')
        cancel.setMinimumHeight(38)
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self.verify_button = QPushButton('验证并进入')
        self.verify_button.setObjectName('primary')
        self.verify_button.setMinimumHeight(38)
        self.verify_button.setMinimumWidth(128)
        self.verify_button.clicked.connect(self.verify_player)
        actions.addWidget(self.verify_button)
        outer.addLayout(actions)
        self.player_input.setFocus()

    def _apply_dialog_style(self):
        colors = INPUT_SURFACES.get(self._theme, INPUT_SURFACES['light'])
        palette = MATCH_ROW_PALETTES.get(self._theme, MATCH_ROW_PALETTES['light'])
        if self._theme == 'light':
            dialog_bg, text, muted, card_bg = '#f5f7fa', '#1e2e3e', '#60758a', '#ffffff'
            accent, accent_text = '#3f79b4', '#ffffff'
        elif self._theme == 'glass':
            dialog_bg, text, muted, card_bg = '#171b21', '#f1f2f4', '#b2b6be', '#252a32'
            accent, accent_text = '#cfd2d8', '#202124'
        elif self._theme == 'liquid':
            dialog_bg, text, muted, card_bg = '#102638', '#effeff', '#a4cad6', '#1a5062'
            accent, accent_text = '#9cefff', '#082c3b'
        elif self._theme == 'blue':
            dialog_bg, text, muted, card_bg = '#082650', '#eef7ff', '#b8cce5', '#0b2e5e'
            accent, accent_text = '#a9d0fa', '#092448'
        else:
            dialog_bg, text, muted, card_bg = '#091421', '#eef5ff', '#b8c8dc', '#16263a'
            accent, accent_text = '#a9cef7', '#102640'
        self._dialog_background = dialog_bg
        self._dialog_outline = palette.get('outline', '#8aa6c1')
        self.setStyleSheet(f"""
QDialog#playerLoginDialog {{ background: transparent; color: {text}; border: none; }}
QLabel#playerLoginMark {{ color: {accent_text}; background: {accent};
    border-radius: 13px; font-size: 15px; font-weight: 800; }}
QLabel#playerLoginTitle {{ color: {text}; font-size: 20px; font-weight: 700; }}
QLabel#playerLoginSubtitle, QLabel#playerLoginPrompt {{ color: {muted}; font-size: 11px; }}
QFrame#playerLoginCard {{ background: {card_bg}; border: 1px solid {palette.get('outline', '#8aa6c1')}; border-radius: 12px; }}
QLabel#playerLoginStatus {{ color: {muted}; font-size: 11px; padding: 2px 0; }}
QPushButton#playerLoginCancel {{ color: {text}; background: transparent; border: 1px solid {palette.get('outline', '#8aa6c1')}; border-radius: 9px; padding: 7px 14px; }}
QPushButton#playerLoginCancel:hover {{ background: {palette.get('recent_hover', card_bg)}; }}
QPushButton#primary {{ color: {accent_text}; background: {accent}; border: 1px solid {accent}; border-radius: 9px; padding: 7px 16px; font-weight: 700; }}
QPushButton#primary:hover {{ background: {palette.get('recent_hover', accent)}; }}
QLineEdit#themedInput {{ color: {colors['text']}; background: {colors['fill']}; border: 1px solid {colors['border']}; border-radius: 9px; padding: 10px 12px; selection-background-color: {colors['selection']}; }}
QLineEdit#themedInput:focus {{ border-color: {colors['focus']}; }}
""")

    def paintEvent(self, event):
        """Paint the login surface with real transparent rounded corners."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.7, 0.7, -0.7, -0.7)
        radius = 17.0
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.setClipPath(path)
        painter.fillPath(path, QColor(self._dialog_background))
        painter.setClipping(False)
        outline = QColor(self._dialog_outline)
        # Alpha colours in the glass palette are accepted by QColor directly;
        # opaque themes receive a subtle one-pixel edge for separation.
        painter.setPen(QPen(outline, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.end()
        super().paintEvent(event)

    def verify_player(self):
        if self._worker is not None and self._worker.isRunning():
            return
        name = self.player_input.text().strip()
        if not name:
            self.status_label.setText('请输入游戏 ID。')
            self.player_input.setFocus()
            return
        if not self._token:
            self.status_label.setText('尚未设置 API Token，请先在设置中配置。')
            return
        self.verify_button.setEnabled(False)
        self.player_input.setEnabled(False)
        self.status_label.setText('正在访问布吉岛验证玩家……')
        worker = BuglandRequestWorker(
            'player_login', bugland_api.PLAYER_INFO_ENDPOINT,
            {'username': name}, self._token, self)
        worker.completed.connect(self._on_verified)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _on_verified(self, action, response, error):
        if action != 'player_login':
            return
        self._worker = None
        self.verify_button.setEnabled(True)
        self.player_input.setEnabled(True)
        if error:
            lowered = error.casefold()
            if '没有找到' in error or '404' in lowered or 'not found' in lowered:
                self.status_label.setText('未找到该用户，请检查游戏 ID。')
            else:
                self.status_label.setText(f'验证失败：{error}')
            return
        if not _player_response_exists(response):
            self.status_label.setText('未找到该用户，请检查游戏 ID。')
            return
        self.player_name = self.player_input.text().strip()
        settings = QSettings('DEV King', '逐渐工具箱')
        settings.setValue(GAME_ID_SETTINGS_KEY, self.player_name)
        settings.setValue('chat/gameId', self.player_name)
        settings.sync()
        self.status_label.setText('创建成功，正在打开 ZJ HUB……')
        self.verify_button.setEnabled(False)
        QTimer.singleShot(420, self.accept)

    def closeEvent(self, event):
        if self._worker is not None and self._worker.isRunning():
            self.status_label.setText('正在等待验证返回，请稍候。')
            event.ignore()
            return
        super().closeEvent(event)


class SupabaseRealtimeChatClient(QObject):
    """Supabase Realtime Phoenix v1 客户端，用于公共大厅群聊。

    只使用 Broadcast 和 Presence：消息不写入 Supabase 表，关闭软件后
    客户端内存中的消息也会被清空。网络事件在 Qt 主线程异步处理，避免
    阻塞主窗口；断线会用有限退避自动重连。
    """

    statusChanged = Signal(str)
    messageReceived = Signal(object)
    # Emitted once a broadcast is confirmed by either the server ACK or the
    # client's self echo.  The payload is the original message dictionary.
    messageSent = Signal(object)
    # Emitted once when a message cannot be confirmed.  The payload remains
    # available to the UI so it can restore the editor without re-sending it.
    messageFailed = Signal(object, str)
    presenceChanged = Signal(int)
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._socket = QWebSocket()
        self._socket.connected.connect(self._on_connected)
        self._socket.textMessageReceived.connect(self._on_message)
        self._socket.disconnected.connect(self._on_disconnected)
        self._socket.errorOccurred.connect(self._on_socket_error)
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._open_socket)
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(20_000)
        self._heartbeat_timer.timeout.connect(self._send_heartbeat)
        self._player_id = ''
        # Presence key must be unique per running client.  The displayed ID is
        # still included in the track payload, but two windows using the same
        # game ID count as two online clients.
        self._presence_key = uuid.uuid4().hex
        self._topic = SUPABASE_CHAT_TOPIC
        self._join_ref = None
        self._ref = 0
        self._joined = False
        self._wanted = False
        self._backoff_ms = 1000
        self._presence_state = {}
        # ref -> {'id': str, 'payload': dict}.  A broadcast is kept here until
        # its ACK or self echo arrives; no automatic retry is performed because
        # a retry could duplicate a message that was accepted by Realtime.
        self._pending_messages = {}
        # Insertion-ordered dict gives bounded FIFO de-duplication without
        # risking removal of the just-added id (set.pop() is arbitrary).
        self._seen_message_ids = {}
        self._max_seen_message_ids = 512

    @property
    def player_id(self):
        return self._player_id

    @property
    def joined(self):
        return self._joined

    def start(self, player_id):
        player_id = str(player_id or '').strip()
        if not player_id:
            self.stop()
            return
        identity_changed = player_id != self._player_id
        self._player_id = player_id
        self._wanted = True
        self._reconnect_timer.stop()
        self._backoff_ms = 1000
        if identity_changed and self._socket.isValid():
            self._fail_pending('身份已切换，消息未确认。')
            self._close_socket()
        if self._socket.state() in {
            QAbstractSocket.SocketState.ConnectedState,
            QAbstractSocket.SocketState.ConnectingState,
        }:
            return
        self._open_socket()

    def stop(self):
        self._wanted = False
        self._reconnect_timer.stop()
        self._heartbeat_timer.stop()
        self._fail_pending('大厅连接已停止，消息未确认。')
        if self._socket.state() == QAbstractSocket.SocketState.ConnectedState:
            self._send({
                'topic': self._topic,
                'event': 'phx_leave',
                'payload': {},
                'ref': self._next_ref(),
                'join_ref': self._join_ref,
            })
        self._close_socket()
        self._joined = False
        self._join_ref = None
        self._presence_state = {}
        self.presenceChanged.emit(0)

    def send_message(self, text):
        text = str(text or '').strip()
        if not text or not self._joined:
            return False
        message_id = uuid.uuid4().hex
        payload = {
            'id': message_id,
            'message': text,
            'text': text,
            'player_id': self._player_id,
            'username': self._player_id,
            'created_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        }
        ref = self._next_ref()
        self._pending_messages[ref] = {
            'id': message_id,
            'payload': dict(payload),
        }
        sent = self._send({
            'topic': self._topic,
            'event': 'broadcast',
            'payload': {'type': 'broadcast', 'event': 'chat', 'payload': payload},
            'ref': ref,
            'join_ref': self._join_ref,
        })
        if not sent:
            self._fail_pending_ref(ref, '消息未写入连接。')
            return False
        # Use the message id as well as the ref so a stale timeout from an old
        # connection cannot expire a newer message after refs are reset.
        QTimer.singleShot(
            10_000,
            lambda pending_ref=ref, pending_id=message_id:
                self._expire_pending(pending_ref, pending_id))
        return True

    def _next_ref(self):
        self._ref += 1
        return str(self._ref)

    def _open_socket(self):
        if not self._wanted or not self._player_id:
            return
        if self._socket.state() in {
            QAbstractSocket.SocketState.ConnectedState,
            QAbstractSocket.SocketState.ConnectingState,
        }:
            return
        self._joined = False
        self._join_ref = None
        self.statusChanged.emit('连接中…')
        url = (SUPABASE_REALTIME_URL + '?apikey=' + SUPABASE_PUBLISHABLE_KEY +
               '&vsn=1.0.0')
        self._socket.open(QUrl(url))

    def _close_socket(self):
        if self._socket.state() != QAbstractSocket.SocketState.UnconnectedState:
            self._socket.abort()

    def _send(self, message):
        if (not isinstance(message, dict) or
                self._socket.state() != QAbstractSocket.SocketState.ConnectedState):
            return False
        try:
            encoded = json.dumps(message, ensure_ascii=False,
                                 separators=(',', ':'))
            queued = self._socket.sendTextMessage(encoded)
        except (TypeError, ValueError, RuntimeError):
            return False
        # QWebSocket returns the number of queued bytes.  A chat frame is never
        # empty, so zero, a negative value, or a missing result is a write
        # failure; only a positive count is accepted.
        try:
            return bool(queued is not None and int(queued) > 0)
        except (TypeError, ValueError):
            return False

    def _on_connected(self):
        self._ref = 0
        self._join_ref = self._next_ref()
        self._send({
            'topic': self._topic,
            'event': 'phx_join',
            'payload': {
                'config': {
                    'broadcast': {'ack': True, 'self': True},
                    'presence': {'enabled': True, 'key': self._presence_key},
                    'postgres_changes': [],
                    'private': False,
                }
            },
            'ref': self._join_ref,
            'join_ref': self._join_ref,
        })

    def _track_presence(self):
        self._send({
            'topic': self._topic,
            'event': 'presence',
            'payload': {
                'type': 'presence',
                'event': 'track',
                'payload': {
                    'user_id': self._player_id,
                    'name': self._player_id,
                },
            },
            'ref': self._next_ref(),
            'join_ref': self._join_ref,
        })

    def _send_heartbeat(self):
        if self._socket.state() == QAbstractSocket.SocketState.ConnectedState:
            self._send({
                'topic': 'phoenix',
                'event': 'heartbeat',
                'payload': {},
                'ref': self._next_ref(),
                'join_ref': None,
            })

    def _on_message(self, raw):
        if not isinstance(raw, (str, bytes, bytearray)):
            return
        try:
            message = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(message, dict):
            return
        event = message.get('event')
        payload = message.get('payload')
        if not isinstance(payload, dict):
            payload = {}
        if event == 'phx_reply':
            status = payload.get('status')
            ref = str(message.get('ref') or '')
            if ref == self._join_ref:
                if status == 'ok':
                    self._joined = True
                    self._backoff_ms = 1000
                    self._heartbeat_timer.start()
                    self._track_presence()
                    self.statusChanged.emit('在线')
                else:
                    reason = payload.get('response')
                    if isinstance(reason, dict):
                        reason = reason.get('reason')
                    reason = reason or '加入大厅失败'
                    self.error.emit(str(reason))
                    self._fail_pending('加入大厅失败，消息未确认。')
                    self._close_socket()
            elif ref in self._pending_messages:
                if status == 'ok':
                    pending = self._pending_messages.get(ref)
                    self._complete_pending(ref, pending)
                else:
                    reason = payload.get('response')
                    if isinstance(reason, dict):
                        reason = reason.get('reason')
                    self._fail_pending_ref(
                        ref, str(reason or '服务器未确认消息。'))
            return
        if event == 'presence_state':
            self._presence_state = dict(payload)
            self._emit_presence_count()
            return
        if event == 'presence_diff':
            self._apply_presence_diff(payload)
            self._emit_presence_count()
            return
        if event == 'broadcast':
            incoming = payload.get('payload')
            if payload.get('event') == 'chat' and isinstance(incoming, dict):
                item = dict(incoming)
                message_id = str(item.get('id') or '').strip()
                if not message_id:
                    message_id = uuid.uuid4().hex
                    item['id'] = message_id
                pending_ref = self._pending_ref_for_id(message_id)
                self._emit_message_once(item)
                if pending_ref is not None:
                    self._complete_pending(
                        pending_ref, self._pending_messages.get(pending_ref),
                        emit_message=False)
            return
        if event in {'phx_error', 'phx_close'}:
            detail = payload.get('reason') if isinstance(payload, dict) else ''
            self.error.emit(str(detail or '大厅连接已关闭'))
            self._fail_pending('大厅连接已关闭，消息未确认。')
            self._close_socket()

    def _remember_message_id(self, message_id):
        message_id = str(message_id or '').strip()
        if not message_id or message_id in self._seen_message_ids:
            return False
        self._seen_message_ids[message_id] = None
        if len(self._seen_message_ids) > self._max_seen_message_ids:
            oldest = next(iter(self._seen_message_ids), None)
            if oldest is not None:
                self._seen_message_ids.pop(oldest, None)
        return True

    def _emit_message_once(self, payload):
        if not isinstance(payload, dict):
            return False
        message_id = str(payload.get('id') or '').strip()
        if not message_id:
            message_id = uuid.uuid4().hex
            payload = dict(payload)
            payload['id'] = message_id
        if not self._remember_message_id(message_id):
            return False
        self.messageReceived.emit(dict(payload))
        return True

    def _pending_ref_for_id(self, message_id):
        message_id = str(message_id or '').strip()
        for ref, pending in self._pending_messages.items():
            if isinstance(pending, dict) and pending.get('id') == message_id:
                return ref
        return None

    def _complete_pending(self, ref, pending, emit_message=True):
        if not isinstance(pending, dict):
            return False
        current = self._pending_messages.get(ref)
        if current is not pending:
            return False
        self._pending_messages.pop(ref, None)
        payload = dict(pending.get('payload') or {})
        if emit_message:
            self._emit_message_once(payload)
        self.messageSent.emit(payload)
        return True

    def _fail_pending_ref(self, ref, reason):
        pending = self._pending_messages.pop(ref, None)
        if not isinstance(pending, dict):
            return False
        payload = dict(pending.get('payload') or {})
        self.messageFailed.emit(payload, str(reason or '消息未确认。'))
        return True

    def _fail_pending(self, reason):
        for ref in list(self._pending_messages):
            self._fail_pending_ref(ref, reason)

    def _expire_pending(self, ref, message_id):
        pending = self._pending_messages.get(ref)
        if not isinstance(pending, dict) or pending.get('id') != message_id:
            return
        self._fail_pending_ref(ref, '消息确认超时（服务器未返回 ACK）。')

    def _apply_presence_diff(self, payload):
        if not isinstance(payload, dict):
            return
        joins = payload.get('joins')
        if not isinstance(joins, dict):
            joins = {}
        leaves = payload.get('leaves')
        if not isinstance(leaves, dict):
            leaves = {}
        for key, value in joins.items():
            if isinstance(value, dict):
                self._presence_state[str(key)] = value
        for key, value in leaves.items():
            key = str(key)
            current = self._presence_state.get(key)
            if not isinstance(current, dict) or not isinstance(value, dict):
                self._presence_state.pop(key, None)
                continue
            metas = value.get('metas')
            if not isinstance(metas, list):
                metas = []
            leaving = {str(item.get('phx_ref')) for item in metas
                       if isinstance(item, dict)}
            current_metas = current.get('metas')
            if not isinstance(current_metas, list):
                current_metas = []
            kept = [item for item in current_metas
                    if isinstance(item, dict) and
                    str(item.get('phx_ref')) not in leaving]
            if kept:
                current['metas'] = kept
            else:
                self._presence_state.pop(key, None)

    def _emit_presence_count(self):
        count = sum(1 for value in self._presence_state.values()
                    if isinstance(value, dict) and value.get('metas'))
        self.presenceChanged.emit(count)

    def _schedule_reconnect(self):
        if self._wanted and not self._reconnect_timer.isActive():
            self._reconnect_timer.start(self._backoff_ms)
            self._backoff_ms = min(self._backoff_ms * 2, 10_000)

    def _on_disconnected(self):
        self._heartbeat_timer.stop()
        self._fail_pending('连接已断开，消息未确认。')
        was_joined = self._joined
        self._joined = False
        self._join_ref = None
        if was_joined:
            self.statusChanged.emit('已断开，正在重连…')
        self.presenceChanged.emit(0)
        self._presence_state = {}
        self._schedule_reconnect()

    def _on_socket_error(self, _error):
        detail = self._socket.errorString() or '无法连接 Supabase Realtime。'
        self.error.emit(detail)
        self._fail_pending('连接发生错误，消息未确认。')
        self._close_socket()


class LobbyChatWorker(QThread):
    """在后台读取大厅消息，避免轮询或发送卡住主窗口。"""

    completed = Signal(str, object, str)

    def __init__(self, action, server_url, player_id='', message='', session_id='', parent=None):
        super().__init__(parent)
        self.action = str(action)
        self.server_url = str(server_url or '').strip().rstrip('/')
        self.player_id = str(player_id or '').strip()
        self.message = str(message or '')
        self.session_id = str(session_id or '').strip()

    def _request(self, method, path, payload=None):
        if not self.server_url or 'YOUR-RENDER-CHAT-SERVICE' in self.server_url:
            raise RuntimeError('大厅聊天服务器尚未配置。')
        url = self.server_url + str(path)
        data = None
        headers = {
            'Accept': 'application/json',
            'User-Agent': 'DEVKing-Toolbox-LobbyChat/1.0',
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = Request(url, data=data, headers=headers, method=method)
        # Render free instances may need a few seconds to wake from sleep.
        with urlopen(request, timeout=18) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise RuntimeError('大厅消息响应过大。')
        if not raw:
            return {}
        try:
            return json.loads(raw.decode('utf-8-sig'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError('大厅服务器返回了无法识别的数据。') from None

    def run(self):
        try:
            if self.action == 'join':
                result = self._request('POST', LOBBY_CHAT_JOIN_PATH,
                                       {'player_id': self.player_id})
            elif self.action == 'send':
                result = self._request(
                    'POST', LOBBY_CHAT_MESSAGES_PATH,
                    {'player_id': self.player_id, 'message': self.message,
                     'session_id': self.session_id})
            elif self.action == 'presence':
                result = self._request('GET', LOBBY_CHAT_PRESENCE_PATH)
            else:
                # limit 参数是后端约定的软限制，兼容不支持 query 参数的服务。
                query = '?limit=60'
                if self.session_id:
                    query += '&session_id=' + self.session_id
                result = self._request('GET', LOBBY_CHAT_MESSAGES_PATH + query)
            self.completed.emit(self.action, result, '')
        except HTTPError as error:
            try:
                detail = error.read(4096).decode('utf-8-sig', errors='ignore')
            except OSError:
                detail = ''
            self.completed.emit(self.action, None,
                                f'大厅服务器 HTTP {error.code}' + (f'：{detail[:120]}' if detail else ''))
        except (URLError, TimeoutError, OSError, RuntimeError) as error:
            self.completed.emit(self.action, None, str(error) or '暂时无法连接大厅服务器。')


class TaskbarActionWorker(QThread):
    """在独立线程读取任务栏 UI Automation，并按真实位置切换窗口。"""

    completedAction = Signal(object)

    def __init__(self, action, index=None, expected_title='', parent=None):
        super().__init__(parent)
        self.action = action
        self.index = index
        self.expected_title = expected_title

    def run(self):
        try:
            with taskbar_switcher_core.auto.UIAutomationInitializerInThread():
                items = taskbar_switcher_core.taskbar_items()
                if self.action == 'refresh':
                    data = [
                        {'name': item['name'], 'title': item['title']}
                        for item in items[:10]
                    ]
                    self.completedAction.emit({
                        'action': self.action, 'items': data, 'error': ''})
                    return
                if self.index is None or self.index >= len(items):
                    raise RuntimeError('这个位置没有正在运行的软件，请刷新列表。')
                selected = items[self.index]
                if self.expected_title and selected['title'] != self.expected_title:
                    raise RuntimeError('任务栏顺序已变化，请刷新列表后再试。')
                taskbar_switcher_core.activate_item(selected)
                self.completedAction.emit({
                    'action': self.action,
                    'index': self.index,
                    'title': selected['title'],
                    'error': '',
                })
        except Exception as error:
            self.completedAction.emit({
                'action': self.action, 'index': self.index,
                'items': [], 'error': str(error)})


class TaskbarHotkeyWorker(QThread):
    """在专用 Win32 消息线程中注册用户配置的全局快捷键。"""

    hotkeysReady = Signal(object)
    pressed = Signal(int)

    def __init__(self, bindings=None, parent=None):
        super().__init__(parent)
        self._win_thread_id = 0
        self._stop_requested = False
        self._registered = {}
        self.bindings = dict(bindings or TASKBAR_DEFAULT_BINDINGS)

    def stop(self):
        self._stop_requested = True
        if self._win_thread_id:
            taskbar_switcher_core.u.PostThreadMessageW(
                self._win_thread_id, taskbar_switcher_core.WM_QUIT, 0, 0)

    def run(self):
        user32 = taskbar_switcher_core.u
        msg = taskbar_switcher_core.wt.MSG()
        self._win_thread_id = int(taskbar_switcher_core.k.GetCurrentThreadId())
        # 先建立线程消息队列，再向该线程投递 WM_QUIT。
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        for index, sequence in sorted(self.bindings.items()):
            if not sequence:
                continue
            parts = _windows_hotkey_parts(sequence)
            if parts is None:
                continue
            modifiers, virtual_key = parts
            if user32.RegisterHotKey(None, int(index) + 1, modifiers, virtual_key):
                self._registered[int(index)] = str(sequence)
        self.hotkeysReady.emit(dict(self._registered))
        try:
            if self._stop_requested:
                return
            while self._registered:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result <= 0:
                    break
                if msg.message == taskbar_switcher_core.WM_HOTKEY:
                    self.pressed.emit(int(msg.wParam) - 1)
        finally:
            for hotkey_id in self._registered:
                user32.UnregisterHotKey(None, hotkey_id + 1)
            self._registered.clear()


class HotkeyCaptureEdit(QLineEdit):
    """单组合键输入框；聚焦期间让全局热键线程释放按键。"""

    captureStarted = Signal()
    captureFinished = Signal()
    sequenceChanged = Signal(str)

    def __init__(self, sequence='', parent=None):
        super().__init__(parent)
        self._sequence = str(sequence or '')
        self._before_capture = self._sequence
        self.setReadOnly(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setPlaceholderText('点击后按下组合键')
        self.setToolTip('支持 Ctrl、Alt 或 Win 加字母、数字或 F1–F12；Esc 取消')
        self.setText(self._sequence or '未设置')

    def setSequence(self, sequence):
        self._sequence = str(sequence or '')
        if not self.hasFocus():
            self.setText(self._sequence or '未设置')

    def focusInEvent(self, event):
        self._before_capture = self._sequence
        self.setText('')
        self.setPlaceholderText('请按组合键，Esc 取消')
        super().focusInEvent(event)
        self.captureStarted.emit()

    def focusOutEvent(self, event):
        if not self._sequence:
            self.setText('未设置')
        else:
            self.setText(self._sequence)
        self.setPlaceholderText('点击后按下组合键')
        super().focusOutEvent(event)
        self.captureFinished.emit()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._sequence = self._before_capture
            self.clearFocus()
            event.accept()
            return
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            self._sequence = ''
            self.setText('未设置')
            self.sequenceChanged.emit('')
            self.clearFocus()
            event.accept()
            return

        key_value = int(key)
        qt_f1 = int(Qt.Key.Key_F1.value)
        qt_f12 = int(Qt.Key.Key_F12.value)
        supported = (
            ord('0') <= key_value <= ord('9')
            or ord('A') <= key_value <= ord('Z')
            or qt_f1 <= key_value <= qt_f12
        )
        if not supported:
            self.setPlaceholderText('支持字母、数字或 F1–F12')
            event.accept()
            return

        modifier_state = event.modifiers()
        modifiers = int(
            modifier_state.value if hasattr(modifier_state, 'value') else modifier_state)
        required = (
            int(Qt.KeyboardModifier.ControlModifier.value)
            | int(Qt.KeyboardModifier.AltModifier.value)
            | int(Qt.KeyboardModifier.MetaModifier.value)
        )
        if not modifiers & required:
            self.setPlaceholderText('请同时按住 Ctrl、Alt 或 Win')
            event.accept()
            return
        allowed = required | int(Qt.KeyboardModifier.ShiftModifier.value)
        sequence = QKeySequence((modifiers & allowed) | key_value).toString(
            QKeySequence.SequenceFormat.PortableText)
        if _windows_hotkey_parts(sequence) is None:
            self.setPlaceholderText('此组合键无法由 Windows 全局注册')
            event.accept()
            return
        self._sequence = sequence
        self.setText(sequence)
        self.sequenceChanged.emit(sequence)
        self.clearFocus()
        event.accept()


class MatchRecordCard(QFrame):
    pressed = Signal()
    doubleClicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.pressed.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.doubleClicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class MatchHistoryScrollArea(QScrollArea):
    """History list with a slightly larger wheel step and a soft end-stop.

    QScrollArea clamps its scrollbar at the end, so a normal wheel gesture can
    feel as if it stopped abruptly.  The signal lets the page add a short
    elastic spacer and animate it back without moving the real content or
    interfering with the API pagination queue.
    """

    overscrollRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('matchHistoryScroll')
        self.verticalScrollBar().setSingleStep(46)
        self.verticalScrollBar().setPageStep(260)

    def wheelEvent(self, event):
        bar = self.verticalScrollBar()
        delta = event.angleDelta().y()
        if delta < 0 and bar.maximum() > 0 and bar.value() >= bar.maximum():
            self.overscrollRequested.emit()
            event.accept()
            return
        super().wheelEvent(event)


class ZJStartupSplash(QWidget):
    """A compact, theme-aware ZJ mark with a clean 60 fps reveal."""

    finished = Signal()

    _THEMES = {
        'light': {
            'top': '#f8fafc', 'bottom': '#e6ebf1', 'ink': '#1d2c3e',
            'accent': '#3f79b4', 'edge': '#8eafd0', 'glow': '#9bbddd',
            'muted': '#71849a',
        },
        'glass': {
            'top': '#303236', 'bottom': '#1d1f22', 'ink': '#f1f3f6',
            'accent': '#bfc6cf', 'edge': '#e1e5eb', 'glow': '#89939f',
            'muted': '#a4aab3',
        },
        'liquid': {
            'top': '#303542', 'bottom': '#151923', 'ink': '#f5f7fb',
            'accent': '#b9d6ff', 'edge': '#ffffff', 'glow': '#8b9fd0',
            'muted': '#b6bfce',
        },
        'dark': {
            'top': '#141d29', 'bottom': '#080e16', 'ink': '#edf4fd',
            'accent': '#8cb5df', 'edge': '#d5e8ff', 'glow': '#517da8',
            'muted': '#91a7bd',
        },
        'blue': {
            'top': '#113768', 'bottom': '#071b3b', 'ink': '#f0f7ff',
            'accent': '#8ec8ff', 'edge': '#d9efff', 'glow': '#3f91d7',
            'muted': '#9dc0e2',
        },
    }

    def __init__(self, theme='light'):
        super().__init__(None, Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint)
        # The surface is deliberately opaque. It avoids the expensive translucent
        # DWM composition that made the old particle animation stutter.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setFixedSize(520, 300)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._started_at = 0.0
        self._phase = 0.0
        self._opacity = 0.0
        self._finishing = False
        self._duration_ms = 1160
        self._fade_ms = 220
        self._theme_key = theme if theme in self._THEMES else 'light'
        self._build_logo()

    def set_theme(self, theme):
        if theme in self._THEMES and theme != self._theme_key:
            self._theme_key = theme
            self.update()

    def _build_logo(self):
        # Filled geometric bars keep the mark typographic and crisp at any DPI.
        path = QPainterPath()
        path.moveTo(132, 82)
        path.lineTo(260, 82)
        path.lineTo(260, 100)
        path.lineTo(160, 190)
        path.lineTo(260, 190)
        path.lineTo(260, 210)
        path.lineTo(132, 210)
        path.lineTo(132, 192)
        path.lineTo(232, 102)
        path.lineTo(132, 102)
        path.closeSubpath()
        path.moveTo(310, 82)
        path.lineTo(424, 82)
        path.lineTo(424, 102)
        path.lineTo(399, 102)
        path.lineTo(399, 169)
        path.cubicTo(399, 197, 383, 211, 355, 211)
        path.cubicTo(328, 211, 307, 198, 300, 176)
        path.lineTo(322, 176)
        path.cubicTo(328, 187, 339, 192, 352, 192)
        path.cubicTo(368, 192, 377, 184, 377, 168)
        path.lineTo(377, 102)
        path.lineTo(310, 102)
        path.closeSubpath()
        self._logo_path = path
        self._logo_bounds = path.boundingRect()
        # Each line is an intentional construction stroke, not a cloud of dots.
        # It travels in from one direction and settles on the final geometry.
        self._segments = []
        final = [
            ((132, 92), (260, 92), (-92, -35), (42, -35), 0.00),
            ((260, 92), (146, 197), (66, -40), (66, 40), 0.10),
            ((146, 200), (260, 200), (-78, 38), (56, 38), 0.16),
            ((310, 92), (424, 92), (60, -32), (170, -32), 0.06),
            ((388, 92), (388, 171), (458, -46), (458, 120), 0.14),
            ((388, 171), (352, 201), (456, 152), (442, 250), 0.22),
            ((352, 201), (310, 180), (430, 250), (292, 240), 0.27),
        ]
        for start, end, from_a, from_b, delay in final:
            self._segments.append({
                'a': QPointF(*start), 'b': QPointF(*end),
                'sa': QPointF(start[0] + from_a[0], start[1] + from_a[1]),
                'sb': QPointF(end[0] + from_b[0], end[1] + from_b[1]),
                'delay': delay,
            })

    @staticmethod
    def _ease(value):
        value = max(0.0, min(1.0, value))
        return value * value * (3.0 - 2.0 * value)

    @staticmethod
    def _mix(first, second, amount):
        return QPointF(
            first.x() + (second.x() - first.x()) * amount,
            first.y() + (second.y() - first.y()) * amount,
        )

    def start(self):
        screen = QApplication.primaryScreen()
        if screen is not None:
            self.move(screen.availableGeometry().center() - self.rect().center())
        self._started_at = time.monotonic()
        self._phase = 0.0
        self._opacity = 0.0
        self._finishing = False
        self.show()
        self.raise_()
        self._timer.start()
        self.update()

    def finish(self):
        """Request an early finish while preserving the fade-out and signal."""
        if not self.isVisible() or self._finishing:
            return
        self._started_at = time.monotonic() - self._duration_ms / 1000.0
        self._finishing = True

    def _tick(self):
        if not self.isVisible():
            self._timer.stop()
            return
        elapsed_ms = (time.monotonic() - self._started_at) * 1000.0
        if elapsed_ms <= self._duration_ms:
            self._phase = elapsed_ms / self._duration_ms
            self._opacity = min(1.0, elapsed_ms / 150.0)
        else:
            self._phase = 1.0
            self._opacity = max(0.0, 1.0 - (elapsed_ms - self._duration_ms) / self._fade_ms)
            self._finishing = True
        if elapsed_ms >= self._duration_ms + self._fade_ms:
            self._timer.stop()
            self.close()
            self.finished.emit()
            return
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        width, height = self.width(), self.height()
        progress = max(0.0, min(1.0, self._phase))
        alpha = max(0.0, min(1.0, self._opacity))
        colors = self._THEMES[self._theme_key]

        background = QLinearGradient(0, 0, width, height)
        background.setColorAt(0.0, QColor(colors['top']))
        background.setColorAt(1.0, QColor(colors['bottom']))
        painter.fillRect(self.rect(), background)
        # A single soft focal light establishes depth without the old perspective grid.
        glow = QRadialGradient(width * 0.52, height * 0.43, width * 0.48)
        glow_color = QColor(colors['glow'])
        glow_color.setAlpha(int(42 * alpha))
        glow.setColorAt(0.0, glow_color)
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(self.rect(), glow)

        # Construction strokes settle first. All geometry is fixed, so repainting is
        # bounded to a handful of lines and stays smooth on slower integrated GPUs.
        stroke_progress = self._ease(progress / 0.72)
        accent = QColor(colors['accent'])
        edge = QColor(colors['edge'])
        for segment in self._segments:
            local = (stroke_progress - segment['delay']) / max(0.001, 1.0 - segment['delay'])
            amount = self._ease(local)
            if amount <= 0.0:
                continue
            first = self._mix(segment['sa'], segment['a'], amount)
            second = self._mix(segment['sb'], segment['b'], amount)
            shadow = QColor(colors['ink'])
            shadow.setAlpha(int(95 * alpha * amount))
            painter.setPen(QPen(shadow, 8.0, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawLine(first + QPointF(2.0, 3.0), second + QPointF(2.0, 3.0))
            line_color = QColor(accent)
            line_color.setAlpha(int(205 * alpha * amount))
            painter.setPen(QPen(line_color, 3.0, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawLine(first, second)

        body_progress = self._ease((progress - 0.36) / 0.42)
        if body_progress > 0.0:
            body_alpha = int(255 * alpha * body_progress)
            # Five restrained extrusion layers communicate depth without looking like
            # a bevel stack or a hand-drawn logo.
            for depth in range(4, 0, -1):
                depth_color = QColor(colors['ink'])
                depth_color.setAlpha(int(body_alpha * (0.16 + depth * 0.04)))
                painter.save()
                painter.translate(depth * 1.15, depth * 1.35)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(depth_color)
                painter.drawPath(self._logo_path)
                painter.restore()
            top = QColor(colors['edge'])
            bottom = QColor(accent)
            top.setAlpha(body_alpha)
            bottom.setAlpha(body_alpha)
            gradient = QLinearGradient(0, self._logo_bounds.top(), 0, self._logo_bounds.bottom())
            gradient.setColorAt(0.0, top)
            gradient.setColorAt(1.0, bottom)
            outline = QColor(colors['edge'])
            outline.setAlpha(int(body_alpha * 0.9))
            painter.setPen(QPen(outline, 1.2))
            painter.setBrush(gradient)
            painter.drawPath(self._logo_path)

            # One narrow sheen passes over the mark once, then disappears.
            sweep = self._logo_bounds.left() - 115 + body_progress * (self._logo_bounds.width() + 230)
            sheen = QLinearGradient(sweep - 34, 0, sweep + 34, 0)
            sheen.setColorAt(0.0, QColor(255, 255, 255, 0))
            sheen.setColorAt(0.5, QColor(255, 255, 255, int(body_alpha * 0.36)))
            sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.save()
            painter.setClipPath(self._logo_path)
            painter.fillRect(self._logo_bounds, sheen)
            painter.restore()

        text_progress = self._ease((progress - 0.63) / 0.27)
        if text_progress > 0.0:
            text_color = QColor(colors['ink'])
            text_color.setAlpha(int(220 * alpha * text_progress))
            painter.setPen(text_color)
            painter.setFont(QFont('Microsoft YaHei UI', 13, QFont.Weight.DemiBold))
            painter.drawText(QRectF(0, 235, width, 24), Qt.AlignmentFlag.AlignCenter, 'ZJ HUB')
            muted = QColor(colors['muted'])
            muted.setAlpha(int(180 * alpha * text_progress))
            painter.setPen(muted)
            painter.setFont(QFont('Segoe UI', 8, QFont.Weight.Medium))
            painter.drawText(QRectF(0, 261, width, 18), Qt.AlignmentFlag.AlignCenter, 'DEV KING  /  MATCH INTELLIGENCE')
        painter.end()


THEME_PALETTES = {
    # 玻璃主题只保留轻微的中性遮罩，让桌面色彩能够透过来；卡片本身
    # 负责建立层级，避免整块灰底把玻璃材质压成一张灰色背景。
    'glass': {
        'name': '玻璃',
        'description': '透明玻璃材质，使用柔和边界区分层级',
        'icon': 'layers',
        'start': (18, 20, 24, 28),
        'end': (10, 12, 16, 46),
        'glow1': (180, 205, 232, 22),
        'glow2': (108, 145, 165, 16),
    },
    'liquid': {
        'name': '液态玻璃',
        'description': '透明动态光场、系统模糊玻璃与白色高光边缘',
        'icon': 'waves',
        # Liquid glass has no static backplate.  The native DWM blur shows
        # the desktop through the transparent window while the animated
        # optical field below supplies all of the motion and colour.
        'start': (0, 0, 0, 0),
        'end': (0, 0, 0, 0),
        'glow1': (0, 0, 0, 0),
        'glow2': (0, 0, 0, 0),
    },
    'dark': {
        'name': '暗色',
        'description': '低亮度深色界面，适合夜间使用',
        'icon': 'moon',
        'start': (9, 13, 20, 240),
        'end': (5, 8, 14, 248),
        'glow1': (47, 80, 128, 30),
        'glow2': (29, 91, 87, 18),
    },
    'light': {
        'name': '明亮',
        'description': '浅色高对比界面，适合白天使用',
        'icon': 'sun',
        'start': (244, 245, 247, 248),
        'end': (226, 229, 233, 250),
        'glow1': (255, 255, 255, 112),
        'glow2': (203, 207, 212, 32),
    },
    'blue': {
        'name': '深海蓝',
        'description': '更深的蓝色界面，突出层次感',
        'icon': 'waves',
        'start': (9, 34, 72, 238),
        'end': (6, 18, 45, 248),
        'glow1': (41, 112, 211, 58),
        'glow2': (32, 144, 166, 34),
    },
}


# Both editable inputs use a fully opaque surface in every interactive state.
# This prevents a focused editor from revealing the translucent window backdrop.
INPUT_SURFACES = {
    'glass': {
        # Editable fields stay opaque to prevent the old hollow/flicker bug;
        # their neutral tint still matches the transparent glass surfaces.
        'fill': '#252a31', 'text': '#f6f8fb', 'border': '#8a99aa',
        'focus': '#e1edf9', 'selection': '#536f8d',
    },
    'liquid': {
        'fill': '#252a36', 'text': '#f7f8fc', 'border': '#9fb6d5',
        'focus': '#dceaff', 'selection': '#426c9e',
    },
    'dark': {
        'fill': '#0c1521', 'text': '#e8eef8', 'border': '#53667e',
        'focus': '#a7c7ea', 'selection': '#5f7795',
    },
    'light': {
        'fill': '#ffffff', 'text': '#1c2c3d', 'border': '#71849a',
        'focus': '#3f79b4', 'selection': '#8ca9c5',
    },
    'blue': {
        'fill': '#082248', 'text': '#e6f0ff', 'border': '#577da8',
        'focus': '#9ed1ff', 'selection': '#4879b3',
    },
}


THEME_OVERRIDES = {
    'glass': """
QWidget { color: #ececef; }
QDialog { color: #f2f6fb; background: #171b21; }
QMessageBox { color: #f2f6fb; background: #171b21; }
QToolTip { color: #f1f1f2; background: #303236; border: 1px solid #55575c; padding: 6px; }
QFrame#updateBanner { background: rgba(195,197,202,24); border-color: rgba(220,222,226,72); }
QLabel#updateTitle { color: #f1f1f2; }
QLabel#updateHint { color: #b7b9be; }
QPushButton#updateOpen { background: #c4c6ca; border-color: #dadce0; color: #202124; }
QPushButton#updateOpen:hover { background: #d5d7da; }
QPushButton#updateDismiss { color: #b7b9be; }
QPushButton#updateDismiss:hover { color: #f5f5f6; background: rgba(205,207,211,28); }
QLineEdit { color: #f2f6fb; background: #252a31; border: 1px solid rgba(255,255,255,86); selection-background-color: #536f8d; }
QLineEdit:focus { color: #f2f6fb; background: #252a31; border: 1px solid #d9eaff; }
QLabel#brandMark { color: #202124; background: #c3c5c9; }
QLabel#brandHint, QLabel#muted, QLabel#pageHint, QLabel#metricLabel, QLabel#cardHint { color: #b7b9be; }
QLabel#statLabel { color: #aeb0b5; }
QLabel#eyebrow { color: #c4c6ca; }
QLabel#placeholderIcon { color: #d0d2d6; }
QFrame#navLine { background: rgba(215,217,221,20); }
QFrame#heroIconTile { background: rgba(220,222,226,30); }
QFrame#moduleIconTile { background: rgba(205,207,211,24); }
QFrame#sectionIcon, QFrame#rowGlyph { background: rgba(192,194,199,20); }
QLabel#metricIcon { background: rgba(192,194,199,22); }
QFrame#row { background: rgba(215,217,221,5); border-bottom-color: rgba(215,217,221,14); }
QPushButton { color: #e1e2e5; background: rgba(195,197,202,14); border-color: rgba(195,197,202,28); }
QPushButton:hover { background: rgba(205,207,211,30); border-color: rgba(220,222,226,44); }
QPushButton:pressed { background: rgba(205,207,211,42); }
QPushButton#primary { color: #202124; background: #c4c6ca; border-color: #dadce0; }
QPushButton#primary:hover { background: #d5d7da; }
QPushButton#navButton, QPushButton#settingsButton, QPushButton#settingsCategory, QPushButton#themeOption { color: #c4c6ca; }
QPushButton#navButton:hover, QPushButton#settingsButton:hover, QPushButton#settingsCategory:hover, QPushButton#themeOption:hover { color: #f5f5f6; background: rgba(205,207,211,28); }
QPushButton#navButton:checked, QPushButton#settingsButton:checked, QPushButton#settingsCategory:checked, QPushButton#themeOption:checked { color: #202124; background: #b9bbc0; border-color: #d5d7da; }
QPushButton#windowControl { background: rgba(195,197,202,15); }
QPushButton#windowControl:hover { background: rgba(205,207,211,30); }
QCheckBox::indicator:unchecked { background: #25272b; border: 1px solid #65676c; border-radius: 4px; }
QCheckBox::indicator:checked { background: #c4c6ca; border: 1px solid #dedfe2; border-radius: 4px; }
QPlainTextEdit { color: #c7c9ce; }
QLabel#matchWinBadge { color: #a9e8c9; background: rgba(45,145,101,55); border-radius: 9px; padding: 5px 9px; font-weight: 700; }
QLabel#matchLossBadge { color: #ffb4b1; background: rgba(201,75,75,55); border-radius: 9px; padding: 5px 9px; font-weight: 700; }
QLabel#matchUnknownBadge { color: #c4c6ca; background: rgba(195,197,202,24); border-radius: 9px; padding: 5px 9px; }
QLabel#mvpBadge { color: #ffe29a; background: rgba(190,143,38,48); border-radius: 9px; padding: 4px 8px; font-weight: 700; }
QProgressBar { background: rgba(194,196,200,24); }
QProgressBar::chunk { background: #aeb1b6; }
QScrollBar:vertical { width: 8px; margin: 3px 1px 3px 1px; background: transparent; }
QScrollBar::handle:vertical { min-height: 24px; background: rgba(194,196,200,45); border-radius: 4px; }
QScrollBar::handle:vertical:hover { background: rgba(215,217,221,75); }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
""",
    'liquid': """
/* The reference uses a neutral frosted pane and a white specular rim; the
   wallpaper supplies the colour.  Keep text and controls on this quiet gray
   scale, with one restrained iOS-like blue accent. */
QWidget { color: #f4f6fb; }
QDialog { color: #f4f6fb; background: #1a1d25; }
QMessageBox { color: #f4f6fb; background: #1a1d25; }
QToolTip { color: #f4f6fb; background: #2b303b; border: 1px solid #b7c8df; border-radius: 9px; padding: 6px; }
QFrame#updateBanner { background: rgba(112,157,216,58); border-color: rgba(222,237,255,172); }
QLabel#updateTitle { color: #ffffff; }
QLabel#updateHint, QLabel#muted, QLabel#pageHint, QLabel#brandHint, QLabel#metricLabel, QLabel#cardHint { color: #bac4d3; }
QLabel#statLabel { color: #a5b0c0; }
QLineEdit { color: #f4f6fb; background: #252a35; border: 1px solid #9eafc7; selection-background-color: #426c9e; border-radius: 11px; }
QLineEdit:focus { color: #ffffff; background: #2a303c; border: 1px solid #dceaff; }
QPushButton { color: #f4f6fb; background: rgba(230,235,244,32); border-color: rgba(239,245,255,132); }
QPushButton:hover { background: rgba(230,238,250,64); border-color: #ffffff; }
QPushButton:pressed { background: rgba(202,219,242,86); }
QPushButton#primary { color: #102039; background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #f7f9ff, stop:0.55 #d4e4fb, stop:1 #aac9f4); border-color: #ffffff; }
QPushButton#primary:hover { background: #ffffff; }
QPushButton#navButton, QPushButton#settingsButton, QPushButton#settingsCategory, QPushButton#themeOption { color: #cbd4e1; }
QPushButton#navButton:hover, QPushButton#settingsButton:hover, QPushButton#settingsCategory:hover, QPushButton#themeOption:hover { color: #ffffff; background: rgba(221,232,249,58); }
QPushButton#navButton:checked, QPushButton#settingsButton:checked, QPushButton#settingsCategory:checked, QPushButton#themeOption:checked { color: #15243a; background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #d5e8ff, stop:1 #abc8ee); border-color: #ffffff; }
QPushButton#windowControl { background: rgba(226,235,247,38); border-color: rgba(239,245,255,90); }
QPushButton#windowControl:hover { background: rgba(245,249,255,110); }
QCheckBox::indicator:unchecked { background: #252a35; border: 1px solid #9eafc7; border-radius: 5px; }
QCheckBox::indicator:checked { background: #b8d3f4; border: 1px solid #ffffff; border-radius: 5px; }
QPlainTextEdit { color: #c2cbd8; }
QProgressBar { background: rgba(220,230,244,40); border-radius: 4px; }
QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #a8c8f0, stop:1 #e6edfa); border-radius: 4px; }
QScrollBar:vertical { width: 9px; margin: 4px 1px 4px 1px; background: transparent; }
QScrollBar::handle:vertical { min-height: 26px; background: rgba(220,232,249,110); border-radius: 4px; }
QScrollBar::handle:vertical:hover { background: rgba(247,250,255,190); }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
""",
    'dark': """
QWidget { color: #e8eef8; }
QDialog { color: #e8eef8; background: #111923; }
QMessageBox { background: #111923; }
QToolTip { color: #e8eef8; background: #172333; border: 1px solid #506b8a; }
QFrame#updateBanner { background: rgba(111,143,181,28); border-color: rgba(169,210,255,72); }
QLabel#updateTitle { color: #e8eef8; }
QLabel#updateHint { color: #a8b9cc; }
QPushButton#updateOpen { background: #a9cef7; border-color: #c9e0ff; color: #102640; }
QPushButton#updateOpen:hover { background: #bfe0ff; }
QPushButton#updateDismiss { color: #a8b9cc; }
QPushButton#updateDismiss:hover { color: #f1f6ff; background: rgba(137,174,218,30); }
QLineEdit { color: #e8eef8; background: #0c1521; border: 1px solid rgba(177,205,236,90); selection-background-color: #5f7795; }
QLineEdit:focus { color: #e8eef8; background: #0c1521; border: 1px solid #a7c7ea; }
QLabel#muted, QLabel#pageHint, QLabel#brandHint, QLabel#metricLabel { color: #9cacc0; }
QLabel#cardHint { color: #a8b9cc; }
QLabel#statLabel { color: #9cacc0; }
QLabel#badge { color: #b8e9df; background: rgba(50, 133, 119, 42); }
QLabel#matchWinBadge { color: #9de8bd; background: rgba(39,133,94,55); border-radius: 9px; padding: 5px 9px; font-weight: 700; }
QLabel#matchLossBadge { color: #ffaaa6; background: rgba(182,61,65,55); border-radius: 9px; padding: 5px 9px; font-weight: 700; }
QLabel#matchUnknownBadge { color: #a9b7c8; background: rgba(111,143,181,22); border-radius: 9px; padding: 5px 9px; }
QLabel#mvpBadge { color: #ffe29a; background: rgba(190,143,38,40); border-radius: 9px; padding: 4px 8px; font-weight: 700; }
QFrame#glass, QFrame#settingsCard { background: rgba(11, 18, 28, 208); border-color: rgba(177, 205, 236, 38); }
QFrame#sidebar { background: rgba(8, 14, 23, 230); border-color: rgba(177, 205, 236, 38); }
QFrame#settingsSidebar { background: rgba(15, 24, 37, 210); border-color: rgba(177, 205, 236, 32); }
QFrame#settingsOverlay { background: #0b121c; border-color: rgba(177, 205, 236, 52); }
QFrame#hero { background: rgba(32, 68, 112, 150); border-color: rgba(177, 205, 236, 45); }
QFrame#metricCard, QFrame#placeholder { background: rgba(12, 21, 33, 200); border-color: rgba(177, 205, 236, 32); }
QFrame#featureCard { background: rgba(12, 21, 33, 190); border-color: rgba(177, 205, 236, 38); }
QFrame#featureRow { background: rgba(111, 143, 181, 20); border-color: rgba(177, 205, 236, 28); }
QFrame#row { border-bottom-color: rgba(177, 205, 236, 20); }
QPushButton { color: #e8eef8; background: rgba(111, 143, 181, 24); border-color: rgba(177, 205, 236, 42); }
QPushButton:hover { background: rgba(137, 174, 218, 42); border-color: #718eaf; }
QPushButton:pressed { background: rgba(137, 174, 218, 60); }
QPushButton#primary { color: #102640; background: #a9cef7; border-color: #c9e0ff; }
QPushButton#navButton, QPushButton#settingsButton, QPushButton#settingsCategory, QPushButton#themeOption { color: #b8c8dc; }
QPushButton#navButton:hover, QPushButton#settingsButton:hover, QPushButton#settingsCategory:hover, QPushButton#themeOption:hover { color: #f1f6ff; background: rgba(137, 174, 218, 30); }
QPushButton#navButton:checked, QPushButton#settingsButton:checked, QPushButton#settingsCategory:checked, QPushButton#themeOption:checked { color: #102640; background: #a9cef7; border-color: #c9e0ff; }
QPlainTextEdit { color: #aabbd0; }
QProgressBar { background: rgba(154, 183, 220, 24); }
""",
    'light': """
QWidget { color: #1c2c3d; }
QDialog { color: #1c2c3d; background: #f5f9fd; }
QMessageBox { background: #f5f9fd; }
QToolTip { color: #1c2c3d; background: #ffffff; border: 1px solid #9ab1c8; }
QFrame#updateBanner { background: rgba(63,121,180,24); border-color: rgba(63,121,180,82); }
QLabel#updateTitle { color: #1c2c3d; }
QLabel#updateHint { color: #5b6d80; }
QPushButton#updateOpen { background: #3f79b4; border-color: #6b9bc9; color: #ffffff; }
QPushButton#updateOpen:hover { background: #32699f; }
QPushButton#updateDismiss { color: #5b6d80; }
QPushButton#updateDismiss:hover { color: #1c2c3d; background: rgba(61,91,120,24); }
QLineEdit { color: #1c2c3d; background: #ffffff; border: 1px solid rgba(61,91,120,100); selection-background-color: #8ca9c5; }
QLineEdit:focus { color: #1c2c3d; background: #ffffff; border: 1px solid #3f79b4; }
QLabel#muted, QLabel#pageHint, QLabel#brandHint, QLabel#metricLabel { color: #5b6d80; }
QLabel#cardHint { color: #5b6d80; }
QLabel#statLabel { color: #66798d; }
QLabel#title, QLabel#section, QLabel#metric, QLabel#metricSmall, QLabel#brand, QLabel#placeholderIcon, QLabel#placeholderText { color: #1c2c3d; }
QLabel#badge { color: #1d6657; background: rgba(54, 170, 141, 32); }
QLabel#matchWinBadge { color: #126340; background: #d9f2e4; border-radius: 9px; padding: 5px 9px; font-weight: 700; }
QLabel#matchLossBadge { color: #a8323c; background: #fde4e6; border-radius: 9px; padding: 5px 9px; font-weight: 700; }
QLabel#matchUnknownBadge { color: #5b6d80; background: #e7ebf0; border-radius: 9px; padding: 5px 9px; }
QLabel#mvpBadge { color: #765209; background: #fff0bd; border-radius: 9px; padding: 4px 8px; font-weight: 700; }
QFrame#glass, QFrame#settingsCard { background: rgba(250, 251, 252, 250); border-color: rgba(61, 71, 83, 34); }
QFrame#sidebar { background: rgba(244, 246, 248, 250); border-color: rgba(61, 71, 83, 30); }
QFrame#settingsSidebar { background: rgba(236, 239, 243, 252); border-color: rgba(61, 71, 83, 28); }
QFrame#settingsOverlay { background: #e5e8ec; border-color: rgba(61, 71, 83, 38); }
QFrame#hero { background: rgba(255, 255, 255, 230); border-color: rgba(61, 71, 83, 34); }
QFrame#metricCard, QFrame#placeholder { background: rgba(249, 250, 251, 242); border-color: rgba(61, 71, 83, 30); }
QFrame#featureCard { background: rgba(246, 248, 250, 246); border-color: rgba(61, 71, 83, 30); }
QFrame#featureRow { background: rgba(231, 234, 238, 246); border-color: rgba(61, 71, 83, 24); }
QFrame#row { border-bottom-color: rgba(61, 91, 120, 28); }
QPushButton { color: #1c2c3d; background: rgba(255, 255, 255, 190); border-color: rgba(61, 91, 120, 62); }
QPushButton:hover { background: rgba(226, 239, 252, 230); border-color: #6e8eae; }
QPushButton:pressed { background: rgba(205, 225, 245, 240); }
QPushButton#primary { color: #ffffff; background: #3f79b4; border-color: #6b9bc9; }
QPushButton#primary:hover { background: #32699f; }
QPushButton#navButton, QPushButton#settingsButton, QPushButton#settingsCategory, QPushButton#themeOption { color: #536679; }
QPushButton#navButton:hover, QPushButton#settingsButton:hover, QPushButton#settingsCategory:hover, QPushButton#themeOption:hover { color: #1c2c3d; background: rgba(209, 228, 246, 220); }
QPushButton#navButton:checked, QPushButton#settingsButton:checked, QPushButton#settingsCategory:checked, QPushButton#themeOption:checked { color: #ffffff; background: #3f79b4; border-color: #6b9bc9; }
QPlainTextEdit { color: #536679; }
QProgressBar { background: rgba(76, 111, 145, 30); }
QProgressBar::chunk { background: #3f79b4; }
""",
    'blue': """
QWidget { color: #e6f0ff; }
QDialog { color: #e6f0ff; background: #0b2347; }
QMessageBox { background: #0b2347; }
QToolTip { color: #e6f0ff; background: #12345f; border: 1px solid #5481b4; }
QFrame#updateBanner { background: rgba(68,125,191,30); border-color: rgba(161,216,255,78); }
QLabel#updateTitle { color: #e6f0ff; }
QLabel#updateHint { color: #a9c1de; }
QPushButton#updateOpen { background: #a9d0fa; border-color: #d0e6ff; color: #092448; }
QPushButton#updateOpen:hover { background: #bfddff; }
QPushButton#updateDismiss { color: #a9c1de; }
QPushButton#updateDismiss:hover { color: #f1f7ff; background: rgba(79,148,224,40); }
QLineEdit { color: #e6f0ff; background: #082248; border: 1px solid rgba(173,214,255,100); selection-background-color: #4879b3; }
QLineEdit:focus { color: #e6f0ff; background: #082248; border: 1px solid #9ed1ff; }
QLabel#muted, QLabel#pageHint, QLabel#brandHint, QLabel#metricLabel { color: #a9c1de; }
QLabel#cardHint { color: #a9c1de; }
QLabel#statLabel { color: #a9c1de; }
QLabel#badge { color: #b9f1e4; background: rgba(54, 170, 141, 40); }
QLabel#matchWinBadge { color: #a2e9c0; background: rgba(38,148,103,56); border-radius: 9px; padding: 5px 9px; font-weight: 700; }
QLabel#matchLossBadge { color: #ffb0ad; background: rgba(198,68,75,56); border-radius: 9px; padding: 5px 9px; font-weight: 700; }
QLabel#matchUnknownBadge { color: #a9c1de; background: rgba(68,125,191,28); border-radius: 9px; padding: 5px 9px; }
QLabel#mvpBadge { color: #ffe29a; background: rgba(190,143,38,45); border-radius: 9px; padding: 4px 8px; font-weight: 700; }
QFrame#glass, QFrame#settingsCard { background: rgba(8, 31, 66, 190); border-color: rgba(173, 214, 255, 48); }
QFrame#sidebar { background: rgba(6, 23, 52, 224); border-color: rgba(173, 214, 255, 48); }
QFrame#settingsSidebar { background: rgba(9, 37, 76, 205); border-color: rgba(173, 214, 255, 38); }
QFrame#settingsOverlay { background: #082650; border-color: rgba(173, 214, 255, 56); }
QFrame#hero { background: rgba(24, 74, 137, 135); border-color: rgba(173, 214, 255, 58); }
QFrame#metricCard, QFrame#placeholder { background: rgba(8, 34, 72, 175); border-color: rgba(173, 214, 255, 38); }
QFrame#featureCard { background: rgba(8, 34, 72, 160); border-color: rgba(173, 214, 255, 44); }
QFrame#featureRow { background: rgba(68, 125, 191, 28); border-color: rgba(173, 214, 255, 34); }
QFrame#row { border-bottom-color: rgba(173, 214, 255, 22); }
QPushButton { color: #e6f0ff; background: rgba(68, 125, 191, 30); border-color: rgba(173, 214, 255, 46); }
QPushButton:hover { background: rgba(79, 148, 224, 55); border-color: #70a9dc; }
QPushButton:pressed { background: rgba(79, 148, 224, 75); }
QPushButton#primary { color: #092448; background: #a9d0fa; border-color: #d0e6ff; }
QPushButton#navButton, QPushButton#settingsButton, QPushButton#settingsCategory, QPushButton#themeOption { color: #b8cce5; }
QPushButton#navButton:hover, QPushButton#settingsButton:hover, QPushButton#settingsCategory:hover, QPushButton#themeOption:hover { color: #f1f7ff; background: rgba(79, 148, 224, 40); }
QPushButton#navButton:checked, QPushButton#settingsButton:checked, QPushButton#settingsCategory:checked, QPushButton#themeOption:checked { color: #092448; background: #a9d0fa; border-color: #d0e6ff; }
QPlainTextEdit { color: #adc5e0; }
QProgressBar { background: rgba(151, 191, 231, 26); }
QProgressBar::chunk { background: #8fc2f5; }
""",
}


# 所有主题都明确区分侧栏、主工作区、主卡片、信息卡和内嵌行五级表面；
# 这样切换主题时只换材质色，不会因为透明度叠加让层级消失。
REFINED_SURFACES = {
    'glass': """
/* Glass uses a quiet neutral tint plus a single clean perimeter.  The old
   opaque gradients looked like rough gray slabs and made every border merge
   into the background. */
QFrame#sidebar { background: rgba(255,255,255,24); border: 1px solid rgba(255,255,255,76); border-radius: 18px; }
QFrame#glass { background: rgba(255,255,255,20); border: 1px solid rgba(255,255,255,82); border-radius: 16px; }
QFrame#matchDetailPanel { background: rgba(8,12,18,94); border: 1px solid rgba(255,255,255,54); border-radius: 16px; }
QFrame#hero { background: rgba(255,255,255,34); border: 1px solid rgba(255,255,255,112); border-left: 4px solid rgba(224,239,255,220); border-radius: 16px; }
QFrame#metricCard { background: rgba(255,255,255,22); border: 1px solid rgba(255,255,255,76); border-radius: 14px; }
QFrame#featureCard { background: rgba(255,255,255,18); border: 1px solid rgba(255,255,255,68); border-radius: 16px; }
QFrame#featureRow { background: rgba(255,255,255,22); border: 1px solid rgba(255,255,255,56); border-radius: 11px; }
QFrame#matchRecord { background: rgba(255,255,255,24); border: 1px solid transparent; }
QFrame#matchRecord:hover { background: rgba(255,255,255,38); }
QFrame#matchRecord[selected="true"] { background: rgba(255,255,255,54); border-left-color: #dcecff; }
QFrame#statChip { background: rgba(255,255,255,26); }
QFrame#matchDetailHero { background: rgba(255,255,255,32); border: 1px solid rgba(255,255,255,98); }
QFrame#placeholder { background: rgba(255,255,255,22); border: 1px solid rgba(255,255,255,72); border-radius: 16px; }
QFrame#settingsSidebar { background: rgba(255,255,255,28); border: 1px solid rgba(255,255,255,84); border-radius: 16px; }
QFrame#settingsCard { background: rgba(255,255,255,30); border: 1px solid rgba(255,255,255,92); border-radius: 16px; }
QFrame#settingsOverlay { background: #171b21; border: 1px solid rgba(255,255,255,92); }
QFrame#row { background: rgba(215,217,221,5); border-bottom: 1px solid rgba(215,217,221,14); }
QPushButton { border: 1px solid rgba(255,255,255,54); border-radius: 10px; }
QPushButton#navButton { border: none; border-left: 4px solid transparent; border-radius: 8px; }
QPushButton#navButton:hover { background: rgba(255,255,255,34); border-radius: 8px; color: #f7f7f8; }
QPushButton#navButton:checked { border: none; border-left: 4px solid #dcecff; border-radius: 8px; background: rgba(255,255,255,48); color: #f7f7f8; }
QPushButton#settingsButton { border: none; border-radius: 8px; }
QPushButton#settingsButton:hover { background: rgba(255,255,255,34); border-radius: 8px; color: #f7f7f8; }
QPushButton#settingsButton:checked { border: none; border-left: 4px solid #dcecff; border-radius: 8px; background: rgba(255,255,255,48); color: #f7f7f8; }
QPushButton#settingsCategory:checked { border: none; border-left: 3px solid #dcecff; background: rgba(255,255,255,48); color: #f7f7f8; }
QPushButton#themeOption { border: 1px solid rgba(255,255,255,54); }
QPushButton#themeOption:checked { border: 1px solid rgba(255,255,255,105); border-left: 3px solid #dcecff; background: rgba(255,255,255,48); color: #f7f7f8; }
""",
    'liquid': """
/* The pane hierarchy is intentionally neutral gray/white like the reference
   material.  Wallpaper colour remains visible behind it while the crisp rim
   and the tiny blue accent provide depth and focus. */
QFrame#sidebar { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(45,49,61,224), stop:0.55 rgba(30,34,45,216), stop:1 rgba(59,48,68,208)); border: 1px solid rgba(245,248,255,176); border-radius: 20px; }
QFrame#glass { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(236,240,248,92), stop:0.38 rgba(176,188,208,70), stop:0.72 rgba(111,125,151,76), stop:1 rgba(239,226,239,72)); border: 1px solid rgba(255,255,255,198); border-radius: 20px; }
QFrame#matchDetailPanel { background: rgba(37,42,53,164); border: 1px solid rgba(241,246,255,144); border-radius: 20px; }
QFrame#hero { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(242,246,253,132), stop:0.48 rgba(174,193,222,108), stop:1 rgba(228,211,229,108)); border: 1px solid rgba(255,255,255,230); border-left: 4px solid #dceaff; border-radius: 20px; }
QFrame#metricCard { background: rgba(226,232,243,82); border: 1px solid rgba(255,255,255,165); border-radius: 16px; }
QFrame#featureCard { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(208,216,231,76), stop:0.6 rgba(135,149,174,70), stop:1 rgba(214,195,218,66)); border: 1px solid rgba(255,255,255,158); border-radius: 20px; }
QFrame#featureRow { background: rgba(229,235,245,66); border: 1px solid rgba(255,255,255,135); border-radius: 14px; }
QFrame#matchRecord { background: rgba(231,236,246,56); border: 1px solid rgba(255,255,255,102); border-radius: 14px; }
QFrame#matchRecord:hover { background: rgba(245,248,255,86); }
QFrame#matchRecord[selected="true"] { background: rgba(212,229,251,112); border-left-color: #f8fbff; }
QFrame#statChip { background: rgba(242,246,253,70); border: 1px solid rgba(255,255,255,98); border-radius: 10px; }
QFrame#matchDetailHero { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(241,245,253,126), stop:0.48 rgba(178,197,225,104), stop:1 rgba(226,209,229,102)); border: 1px solid rgba(255,255,255,215); border-radius: 19px; }
QFrame#placeholder { background: rgba(209,218,233,76); border: 1px solid rgba(255,255,255,150); border-radius: 20px; }
QFrame#settingsSidebar { background: rgba(44,49,61,230); border: 1px solid rgba(245,248,255,178); border-radius: 20px; }
QFrame#settingsCard { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(221,228,240,92), stop:0.65 rgba(128,143,170,86), stop:1 rgba(214,193,220,78)); border: 1px solid rgba(255,255,255,180); border-radius: 20px; }
QFrame#settingsOverlay { background: rgba(27,30,38,248); border: 1px solid rgba(255,255,255,205); }
QFrame#row { background: rgba(235,240,248,24); border-bottom: 1px solid rgba(255,255,255,74); }
QPushButton { border: 1px solid rgba(255,255,255,138); border-radius: 12px; }
QPushButton#navButton { border: none; border-left: 4px solid transparent; border-radius: 9px; }
QPushButton#navButton:hover { background: rgba(240,246,255,70); border-radius: 9px; color: #ffffff; }
QPushButton#navButton:checked { border: none; border-left: 4px solid #ffffff; border-radius: 9px; background: rgba(208,226,250,116); color: #ffffff; }
QPushButton#settingsButton { border: none; border-radius: 9px; }
QPushButton#settingsButton:hover { background: rgba(240,246,255,70); border-radius: 9px; color: #ffffff; }
QPushButton#settingsButton:checked { border: none; border-left: 4px solid #ffffff; border-radius: 9px; background: rgba(208,226,250,104); color: #ffffff; }
QPushButton#settingsCategory:checked { border: none; border-left: 3px solid #ffffff; background: rgba(208,226,250,88); }
QPushButton#themeOption { border: 1px solid rgba(255,255,255,128); border-radius: 13px; }
QPushButton#themeOption:checked { border: 1px solid rgba(255,255,255,225); border-left: 3px solid #ffffff; background: rgba(208,226,250,98); color: #ffffff; }
""",
    'dark': """
QFrame#sidebar { background: rgba(7,13,22,248); border: 1px solid rgba(122,155,194,52); border-radius: 18px; }
QFrame#glass { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(20,34,52,236), stop:1 rgba(11,20,32,232)); border: 1px solid rgba(137,174,218,46); border-radius: 16px; }
QFrame#matchDetailPanel { background: rgba(6,14,24,150); border: 1px solid rgba(120,158,198,42); border-radius: 16px; }
QFrame#hero { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(40,83,134,214), stop:1 rgba(19,39,65,236)); border: 1px solid rgba(169,210,255,72); border-left: 4px solid #a8d3ff; border-radius: 16px; }
QFrame#metricCard { background: rgba(22,38,58,242); border: 1px solid rgba(128,165,207,48); border-radius: 14px; }
QFrame#featureCard { background: rgba(17,30,47,238); border: 1px solid rgba(126,162,201,56); border-radius: 16px; }
QFrame#featureRow { background: rgba(9,19,31,238); border: 1px solid rgba(119,154,193,48); border-radius: 11px; }
QFrame#matchRecord { background: rgba(111,143,181,20); }
QFrame#matchRecord:hover { background: rgba(111,143,181,34); }
QFrame#matchRecord[selected="true"] { background: rgba(111,143,181,44); border-left-color: #b9dcff; }
QFrame#statChip { background: rgba(111,143,181,25); }
QFrame#matchDetailHero { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(44,92,148,218), stop:1 rgba(14,27,45,244)); border: 1px solid rgba(172,211,255,58); }
QFrame#placeholder { background: rgba(12,23,37,244); border: 1px solid rgba(120,158,198,48); border-radius: 16px; }
QFrame#settingsSidebar { background: rgba(13,23,37,246); border: 1px solid rgba(120,158,198,52); border-radius: 16px; }
QFrame#settingsCard { background: rgba(22,38,58,244); border: 1px solid rgba(135,173,214,56); border-radius: 16px; }
QFrame#settingsOverlay { background: #091421; border: 1px solid rgba(145,186,231,64); }
QFrame#row { border-bottom: none; }
QPushButton { border: none; }
QPushButton#navButton { border: none; border-left: 4px solid transparent; border-radius: 8px; }
QPushButton#navButton:hover { background: rgba(137,174,218,24); border-radius: 8px; }
QPushButton#navButton:checked { border: none; border-left: 4px solid #b9dcff; border-radius: 8px; background: rgba(137,174,218,34); color: #f1f6ff; }
QPushButton#settingsButton { border: none; border-radius: 8px; }
QPushButton#settingsButton:hover { background: rgba(137,174,218,24); border-radius: 8px; }
QPushButton#settingsButton:checked { border: none; border-left: 4px solid #b9dcff; border-radius: 8px; background: rgba(137,174,218,34); color: #f1f6ff; }
QPushButton#settingsCategory:checked { border: none; border-left: 3px solid #b9dcff; }
QPushButton#themeOption { border: none; }
QPushButton#themeOption:checked { border: none; border-left: 3px solid #b9dcff; }
""",
    'light': """
QFrame#sidebar { background: rgba(239,243,247,252); border: 1px solid rgba(102,128,153,48); border-radius: 18px; }
QFrame#glass { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(255,255,255,252), stop:1 rgba(238,242,246,248)); border: 1px solid rgba(112,140,167,52); border-radius: 16px; }
QFrame#matchDetailPanel { background: rgba(226,233,240,130); border: 1px solid rgba(102,130,157,42); border-radius: 16px; }
QFrame#hero { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(255,255,255,252), stop:1 rgba(232,239,246,250)); border: 1px solid rgba(73,121,168,66); border-left: 4px solid #3f79b4; border-radius: 16px; }
QFrame#metricCard { background: rgba(252,253,254,252); border: 1px solid rgba(112,140,167,48); border-radius: 14px; }
QFrame#featureCard { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(246,249,252,252), stop:1 rgba(232,238,244,250)); border: 1px solid rgba(102,130,157,58); border-radius: 16px; }
QFrame#featureRow { background: rgba(220,228,237,250); border: 1px solid rgba(102,130,157,48); border-radius: 11px; }
QFrame#matchRecord { background: rgba(246,248,250,252); }
QFrame#matchRecord:hover { background: #eef2f6; }
QFrame#matchRecord[selected="true"] { background: #e5edf6; border-left-color: #3f79b4; }
QFrame#statChip { background: #edf1f5; }
QFrame#matchDetailHero { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #ffffff, stop:1 #e4edf5); border: 1px solid rgba(73,121,168,58); }
QFrame#placeholder { background: rgba(241,245,249,252); border: 1px solid rgba(102,130,157,48); border-radius: 16px; }
QFrame#settingsSidebar { background: rgba(231,236,242,252); border: 1px solid rgba(102,130,157,48); border-radius: 16px; }
QFrame#settingsCard { background: rgba(255,255,255,252); border: 1px solid rgba(102,130,157,58); border-radius: 16px; }
QFrame#heroIconTile, QFrame#moduleIconTile { background: rgba(63,121,180,18); border: none; }
QFrame#sectionIcon, QFrame#rowGlyph { background: rgba(63,121,180,16); border: none; }
QFrame#statusGlyph { background: rgba(33,133,108,18); border: none; }
QLabel#metricIcon { background: rgba(63,121,180,16); border: none; }
QFrame#settingsOverlay { background: #dfe5eb; border: 1px solid rgba(78,108,137,64); }
QFrame#row { border-bottom: none; }
QPushButton { border: none; }
QPushButton#navButton { border: none; border-left: 4px solid transparent; border-radius: 8px; }
QPushButton#navButton:hover { background: rgba(79,132,181,24); border-radius: 8px; }
QPushButton#navButton:checked { border: none; border-left: 4px solid #3f79b4; border-radius: 8px; background: rgba(63,121,180,24); color: #1c2c3d; }
QPushButton#settingsButton { border: none; border-radius: 8px; }
QPushButton#settingsButton:hover { background: rgba(79,132,181,24); border-radius: 8px; }
QPushButton#settingsButton:checked { border: none; border-left: 4px solid #3f79b4; border-radius: 8px; background: rgba(63,121,180,24); color: #1c2c3d; }
QPushButton#settingsCategory:checked { border: none; border-left: 3px solid #ffffff; }
QPushButton#themeOption { border: none; }
QPushButton#themeOption:checked { border: none; border-left: 3px solid #ffffff; }
""",
    'blue': """
QFrame#sidebar { background: rgba(5,20,46,248); border: 1px solid rgba(119,174,230,58); border-radius: 18px; }
QFrame#glass { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(10,42,88,234), stop:1 rgba(5,24,53,230)); border: 1px solid rgba(132,190,246,54); border-radius: 16px; }
QFrame#matchDetailPanel { background: rgba(4,22,48,148); border: 1px solid rgba(116,174,232,48); border-radius: 16px; }
QFrame#hero { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(32,94,172,210), stop:1 rgba(10,47,102,235)); border: 1px solid rgba(161,216,255,78); border-left: 4px solid #b3dcff; border-radius: 16px; }
QFrame#metricCard { background: rgba(12,48,99,242); border: 1px solid rgba(121,180,239,52); border-radius: 14px; }
QFrame#featureCard { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(10,44,92,238), stop:1 rgba(6,29,65,234)); border: 1px solid rgba(124,184,242,62); border-radius: 16px; }
QFrame#featureRow { background: rgba(5,25,55,238); border: 1px solid rgba(111,169,227,54); border-radius: 11px; }
QFrame#matchRecord { background: rgba(68,125,191,24); }
QFrame#matchRecord:hover { background: rgba(79,148,224,36); }
QFrame#matchRecord[selected="true"] { background: rgba(79,148,224,48); border-left-color: #d4eaff; }
QFrame#statChip { background: rgba(68,125,191,30); }
QFrame#matchDetailHero { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(34,100,180,220), stop:1 rgba(7,33,75,240)); border: 1px solid rgba(163,216,255,64); }
QFrame#placeholder { background: rgba(7,31,67,242); border: 1px solid rgba(116,174,232,52); border-radius: 16px; }
QFrame#settingsSidebar { background: rgba(7,30,64,245); border: 1px solid rgba(116,174,232,58); border-radius: 16px; }
QFrame#settingsCard { background: rgba(11,46,94,242); border: 1px solid rgba(130,190,246,64); border-radius: 16px; }
QFrame#settingsOverlay { background: #061f43; border: 1px solid rgba(150,210,255,70); }
QFrame#row { border-bottom: none; }
QPushButton { border: none; }
QPushButton#navButton { border: none; border-left: 4px solid transparent; border-radius: 8px; }
QPushButton#navButton:hover { background: rgba(79,148,224,28); border-radius: 8px; }
QPushButton#navButton:checked { border: none; border-left: 4px solid #d4eaff; border-radius: 8px; background: rgba(79,148,224,38); color: #f1f7ff; }
QPushButton#settingsButton { border: none; border-radius: 8px; }
QPushButton#settingsButton:hover { background: rgba(79,148,224,28); border-radius: 8px; }
QPushButton#settingsButton:checked { border: none; border-left: 4px solid #d4eaff; border-radius: 8px; background: rgba(79,148,224,38); color: #f1f7ff; }
QPushButton#settingsCategory:checked { border: none; border-left: 3px solid #d4eaff; }
QPushButton#themeOption { border: none; }
QPushButton#themeOption:checked { border: none; border-left: 3px solid #d4eaff; }
""",
}


MATCH_ROW_PALETTES = {
    'glass': {
        'win_bg': 'rgba(62,160,107,92)', 'win_hover': 'rgba(74,184,123,112)', 'win_text': '#e4f7e9',
        'win_meta': '#b7ddc2', 'win_border': 'rgba(130,227,164,145)',
        'loss_bg': 'rgba(206,82,94,92)', 'loss_hover': 'rgba(226,98,111,112)', 'loss_text': '#ffe9e9',
        'loss_meta': '#e8b9bc', 'loss_border': 'rgba(255,163,171,145)',
        'unknown_bg': 'rgba(255,255,255,38)', 'unknown_hover': 'rgba(255,255,255,58)',
        'unknown_text': '#ececef', 'unknown_meta': '#c2c4c8',
        'daily_bg': 'rgba(255,255,255,34)', 'recent_bg': 'rgba(255,255,255,54)', 'recent_hover': 'rgba(255,255,255,72)',
        'recent_text': '#f0f0f2', 'positive': '#a6e6bd', 'negative': '#ffb5b5',
        'neutral': '#ececef', 'outline': 'rgba(255,255,255,108)',
    },
    'liquid': {
        'win_bg': 'rgba(54,156,104,108)', 'win_hover': 'rgba(68,181,121,132)', 'win_text': '#e3f8e9',
        'win_meta': '#b5ddc0', 'win_border': 'rgba(154,233,178,170)',
        'loss_bg': 'rgba(178,70,76,102)', 'loss_hover': 'rgba(205,88,94,126)', 'loss_text': '#ffe7e8',
        'loss_meta': '#e6b8bc', 'loss_border': 'rgba(255,171,177,166)',
        'unknown_bg': 'rgba(238,243,251,42)', 'unknown_hover': 'rgba(250,252,255,68)',
        'unknown_text': '#f4f6fb', 'unknown_meta': '#c2cad7',
        'daily_bg': 'rgba(225,232,244,40)', 'recent_bg': 'rgba(235,241,251,62)', 'recent_hover': 'rgba(248,251,255,86)',
        'recent_text': '#f4f6fb', 'positive': '#a8e6bc', 'negative': '#ffb1b5',
        'neutral': '#f4f6fb', 'outline': 'rgba(245,249,255,148)',
    },
    'dark': {
        'win_bg': '#17452f', 'win_hover': '#20583a', 'win_text': '#e0f7e8',
        'win_meta': '#add9bb', 'win_border': '#2f7049',
        'loss_bg': '#4d272b', 'loss_hover': '#603036', 'loss_text': '#ffebeb',
        'loss_meta': '#e4b3b6', 'loss_border': '#7b444a',
        'unknown_bg': '#223247', 'unknown_hover': '#2b3e55',
        'unknown_text': '#e8eef8', 'unknown_meta': '#b8c7d9',
        'daily_bg': '#192638', 'recent_bg': '#24364c', 'recent_hover': '#304762',
        'recent_text': '#e8eef8', 'positive': '#9fe0b6', 'negative': '#ffaaa8',
        'neutral': '#e8eef8', 'outline': '#a7c7ea',
    },
    'light': {
        'win_bg': '#dff3e6', 'win_hover': '#d1eddb', 'win_text': '#155d38',
        'win_meta': '#477654', 'win_border': '#aad3b7',
        'loss_bg': '#fae1e3', 'loss_hover': '#f5d3d6', 'loss_text': '#8e2e36',
        'loss_meta': '#9d555c', 'loss_border': '#e9b6bb',
        'unknown_bg': '#edf1f5', 'unknown_hover': '#e4ebf2',
        'unknown_text': '#1c2c3d', 'unknown_meta': '#5b6d80',
        'daily_bg': '#f0f3f6', 'recent_bg': '#eaf0f6', 'recent_hover': '#dce8f3',
        'recent_text': '#365b7c', 'positive': '#168052', 'negative': '#bb3c47',
        'neutral': '#1c2c3d', 'outline': '#3f79b4',
    },
    'blue': {
        'win_bg': '#174833', 'win_hover': '#205a3d', 'win_text': '#e3f7e9',
        'win_meta': '#b4dbbf', 'win_border': '#34734c',
        'loss_bg': '#51292d', 'loss_hover': '#653238', 'loss_text': '#ffebeb',
        'loss_meta': '#e6b3b6', 'loss_border': '#82484e',
        'unknown_bg': '#12345f', 'unknown_hover': '#194273',
        'unknown_text': '#e6f0ff', 'unknown_meta': '#afc8e5',
        'daily_bg': '#0c2b55', 'recent_bg': '#164072', 'recent_hover': '#205087',
        'recent_text': '#e6f0ff', 'positive': '#a4e6bd', 'negative': '#ffaaa8',
        'neutral': '#e6f0ff', 'outline': '#9ed1ff',
    },
}

# Team identity is intentionally kept separate from semantic metric colors.
# The player cards use these restrained tints so a red/blue/yellow/green team
# remains recognizable in every theme without making the card look like a
# win/loss status chip.
TEAM_PLAYER_PALETTES = {
    'glass': {
        'red': {'bg': '#492c34', 'hover': '#5a3540', 'border': '#d36b79', 'text': '#ffdfe3'},
        'blue': {'bg': '#293c58', 'hover': '#334d70', 'border': '#75a9ec', 'text': '#e1efff'},
        'yellow': {'bg': '#4a4029', 'hover': '#5d5030', 'border': '#e2bd58', 'text': '#ffe9ac'},
        'green': {'bg': '#294436', 'hover': '#345743', 'border': '#67c694', 'text': '#d9f7e7'},
    },
    'liquid': {
        'red': {'bg': '#633044', 'hover': '#7b3b56', 'border': '#f080a6', 'text': '#ffe1ed'},
        'blue': {'bg': '#234e70', 'hover': '#2d638a', 'border': '#76d8ff', 'text': '#ddf7ff'},
        'yellow': {'bg': '#665327', 'hover': '#7e672f', 'border': '#f4d36e', 'text': '#fff1b7'},
        'green': {'bg': '#215d4c', 'hover': '#2b755e', 'border': '#71e4b7', 'text': '#d7fff0'},
    },
    'dark': {
        'red': {'bg': '#3b222a', 'hover': '#4f2d38', 'border': '#d85b6b', 'text': '#ffd9df'},
        'blue': {'bg': '#1d334f', 'hover': '#274664', 'border': '#6fa9ef', 'text': '#d9ebff'},
        'yellow': {'bg': '#3d341d', 'hover': '#514522', 'border': '#d9b44f', 'text': '#ffe8a0'},
        'green': {'bg': '#1d3a2c', 'hover': '#28503b', 'border': '#55c285', 'text': '#d2f5df'},
    },
    'light': {
        'red': {'bg': '#fff0f1', 'hover': '#fbe1e4', 'border': '#d35b67', 'text': '#8f2935'},
        'blue': {'bg': '#eaf2ff', 'hover': '#dceafe', 'border': '#4b82c1', 'text': '#24578c'},
        'yellow': {'bg': '#fff7df', 'hover': '#ffefc2', 'border': '#c58b12', 'text': '#765300'},
        'green': {'bg': '#e8f7ef', 'hover': '#d8f0e3', 'border': '#3a9d6a', 'text': '#17613d'},
    },
    'blue': {
        'red': {'bg': '#48252e', 'hover': '#5d303a', 'border': '#ee7182', 'text': '#ffe0e5'},
        'blue': {'bg': '#17375e', 'hover': '#214a7b', 'border': '#83baff', 'text': '#dceeff'},
        'yellow': {'bg': '#493b1a', 'hover': '#5e4d21', 'border': '#e7c35c', 'text': '#ffebad'},
        'green': {'bg': '#1b4434', 'hover': '#245a43', 'border': '#68d59a', 'text': '#d9f9e8'},
    },
}

# 对局详情中的“高光指标”阈值。比较严格使用大于号：刚好达到阈值仍保持
# 普通的正负语义色，超过阈值才使用主题金色高光。
DETAIL_METRIC_THRESHOLDS = {
    '最终击杀': (8, ('final_kill', 'finalKill', 'final_kills', 'finalKills')),
    '击杀': (15, ('kill', 'kills', 'total_kills', 'totalKills')),
    '伤害': (400, ('damage', 'damage_dealt', 'damageDealt', 'total_damage', 'totalDamage')),
    '绿宝石': (100, ('emerald', 'emeralds', 'emerald_collected', 'emeralds_collected', 'emeraldsPicked')),
    '钻石': (80, ('diamond', 'diamonds', 'diamond_collected', 'diamonds_collected', 'diamondsPicked')),
}

# Every detail metric uses the same 24px outline icon language. The names map
# to ICON_PATHS below and intentionally avoid emoji/platform glyphs.
DETAIL_METRIC_ICONS = {
    '击杀': 'sword',
    '最终击杀': 'target',
    '死亡': 'skull',
    '最终死亡': 'skull',
    '拆床': 'bed',
    '伤害': 'bolt',
    '绿宝石': 'gem',
    '钻石': 'diamond',
    '放置': 'blocks',
    '破坏': 'hammer',
    '承伤': 'shield',
}


# 排行榜层级色独立于战绩的胜负色，避免把“名次”误读成“胜负”。
# 每套主题都保留金/银/铜的高光关系，第四名以后逐渐收敛到普通表面。
LEADERBOARD_RANK_PALETTES = {
    'glass': {
        '1': {'bg': '#5c4b22', 'hover': '#725f2b', 'border': '#d6b45e', 'text': '#ffe7a0'},
        '2': {'bg': '#4a4d54', 'hover': '#5d6068', 'border': '#bfc6d1', 'text': '#edf1f6'},
        '3': {'bg': '#553d2d', 'hover': '#684a35', 'border': '#d59a68', 'text': '#ffd0a4'},
        '4': {'bg': '#3e4045', 'hover': '#4a4c52', 'border': '#777a82', 'text': '#d6d8dc'},
        '5': {'bg': '#393b40', 'hover': '#45474c', 'border': '#62656c', 'text': '#bfc2c8'},
    },
    'liquid': {
        '1': {'bg': '#5b4a22', 'hover': '#735e2b', 'border': '#f0ca67', 'text': '#fff0ba'},
        '2': {'bg': '#4a4e57', 'hover': '#5d626c', 'border': '#c8d0dc', 'text': '#f1f4f9'},
        '3': {'bg': '#5a3f30', 'hover': '#704d39', 'border': '#e0a477', 'text': '#ffe2cd'},
        '4': {'bg': '#394454', 'hover': '#4b586b', 'border': '#96a9c0', 'text': '#dce5f0'},
        '5': {'bg': '#303a49', 'hover': '#414d5e', 'border': '#7d91a8', 'text': '#c7d2e0'},
    },
    'dark': {
        '1': {'bg': '#493a17', 'hover': '#5d4a1e', 'border': '#d8b24f', 'text': '#ffe59a'},
        '2': {'bg': '#2c3948', 'hover': '#3a4b5e', 'border': '#91a5bb', 'text': '#e8f0f8'},
        '3': {'bg': '#463126', 'hover': '#593d2f', 'border': '#bd8059', 'text': '#ffd0ad'},
        '4': {'bg': '#1e2d3d', 'hover': '#293c50', 'border': '#5e748c', 'text': '#c2d0de'},
        '5': {'bg': '#1a2838', 'hover': '#25394d', 'border': '#4f647a', 'text': '#aebed0'},
    },
    'light': {
        '1': {'bg': '#fff1c8', 'hover': '#ffe6a1', 'border': '#d6a72d', 'text': '#684c00'},
        '2': {'bg': '#e9eef4', 'hover': '#dfe7ef', 'border': '#9aa8b7', 'text': '#334252'},
        '3': {'bg': '#f7e8dc', 'hover': '#f1d9c9', 'border': '#c58b63', 'text': '#6e3e24'},
        '4': {'bg': '#f4f6f8', 'hover': '#e9edf2', 'border': '#cbd3dc', 'text': '#4e6071'},
        '5': {'bg': '#f0f3f6', 'hover': '#e5eaf0', 'border': '#d7dee6', 'text': '#647587'},
    },
    'blue': {
        '1': {'bg': '#554617', 'hover': '#6c5920', 'border': '#e0bc55', 'text': '#ffe7a1'},
        '2': {'bg': '#203b59', 'hover': '#2c4d70', 'border': '#8fb0d0', 'text': '#e5f1ff'},
        '3': {'bg': '#4a3428', 'hover': '#5e4332', 'border': '#c38a61', 'text': '#ffd2b0'},
        '4': {'bg': '#153454', 'hover': '#1e456c', 'border': '#5d86ae', 'text': '#c4ddf5'},
        '5': {'bg': '#122f4c', 'hover': '#1b4164', 'border': '#4c7399', 'text': '#adc7df'},
    },
}


def _match_state_style(theme_key):
    colors = MATCH_ROW_PALETTES.get(theme_key, MATCH_ROW_PALETTES['light'])
    rank_colors = LEADERBOARD_RANK_PALETTES.get(theme_key, LEADERBOARD_RANK_PALETTES['light'])
    team_colors = TEAM_PLAYER_PALETTES.get(theme_key, TEAM_PLAYER_PALETTES['light'])
    return f"""
QFrame#matchRecord[recordOutcome="win"] {{ background: {colors['win_bg']}; border-color: transparent; }}
QFrame#matchRecord[recordOutcome="win"]:hover {{ background: {colors['win_hover']}; }}
QFrame#matchRecord[recordOutcome="win"][selected="true"] {{ border-color: {colors['win_border']}; }}
QFrame#matchRecord[recordOutcome="loss"] {{ background: {colors['loss_bg']}; border-color: transparent; }}
QFrame#matchRecord[recordOutcome="loss"]:hover {{ background: {colors['loss_hover']}; }}
QFrame#matchRecord[recordOutcome="loss"][selected="true"] {{ border-color: {colors['loss_border']}; }}
QFrame#matchRecord[recordOutcome="unknown"] {{ background: {colors['unknown_bg']}; border-color: transparent; }}
QFrame#matchRecord[recordOutcome="unknown"]:hover {{ background: {colors['unknown_hover']}; }}
QFrame#matchRecord[recordOutcome="unknown"][selected="true"] {{ border-color: {colors['outline']}; }}
QFrame#matchDetailRibbon[recordOutcome="win"] {{ background: {colors['win_bg']}; border: 1px solid {colors['win_border']}; }}
QFrame#matchDetailRibbon[recordOutcome="loss"] {{ background: {colors['daily_bg']}; border: 1px solid {colors['outline']}; }}
QFrame#matchDetailRibbon[recordOutcome="unknown"] {{ background: {colors['unknown_bg']}; border: 1px solid {colors['outline']}; }}
QFrame#matchDetailHero[recordOutcome="win"] {{ background: {colors['win_bg']}; border: 1px solid {colors['win_border']}; }}
QFrame#matchDetailHero[recordOutcome="loss"] {{ background: {colors['daily_bg']}; border: 1px solid {colors['outline']}; }}
QFrame#matchDetailHero[recordOutcome="unknown"] {{ background: {colors['unknown_bg']}; border: 1px solid {colors['outline']}; }}
QFrame#featureRow[queryTarget="true"] {{ background: {colors['recent_bg']}; border: 2px solid {colors['outline']}; }}
QFrame#featureRow[queryTarget="true"] QLabel#cardTitle {{ color: {colors['neutral']}; font-weight: 700; }}
QFrame#featureRow[playerOutcome="win"] {{ border-left: 3px solid {colors['positive']}; }}
QFrame#featureRow[playerOutcome="loss"] {{ border-left: 3px solid {colors['negative']}; }}
QFrame#featureRow[playerOutcome="neutral"] {{ border-left: 3px solid {colors['outline']}; }}
QFrame#featureRow[teamTint="red"] {{ background: {team_colors['red']['bg']}; border: 1px solid {team_colors['red']['border']}; border-left: 4px solid {team_colors['red']['border']}; }}
QFrame#featureRow[teamTint="red"]:hover {{ background: {team_colors['red']['hover']}; }}
QFrame#featureRow[teamTint="blue"] {{ background: {team_colors['blue']['bg']}; border: 1px solid {team_colors['blue']['border']}; border-left: 4px solid {team_colors['blue']['border']}; }}
QFrame#featureRow[teamTint="blue"]:hover {{ background: {team_colors['blue']['hover']}; }}
QFrame#featureRow[teamTint="yellow"] {{ background: {team_colors['yellow']['bg']}; border: 1px solid {team_colors['yellow']['border']}; border-left: 4px solid {team_colors['yellow']['border']}; }}
QFrame#featureRow[teamTint="yellow"]:hover {{ background: {team_colors['yellow']['hover']}; }}
QFrame#featureRow[teamTint="green"] {{ background: {team_colors['green']['bg']}; border: 1px solid {team_colors['green']['border']}; border-left: 4px solid {team_colors['green']['border']}; }}
QFrame#featureRow[teamTint="green"]:hover {{ background: {team_colors['green']['hover']}; }}
QFrame#featureRow[queryTarget="true"][teamTint="red"], QFrame#featureRow[queryTarget="true"][teamTint="blue"],
QFrame#featureRow[queryTarget="true"][teamTint="yellow"], QFrame#featureRow[queryTarget="true"][teamTint="green"] {{ border-width: 2px; border-left-width: 5px; }}
QLabel#teamTintBadge {{ border-radius: 999px; padding: 3px 8px; font-weight: 700; }}
QLabel#teamTintBadge[tint="red"] {{ color: {team_colors['red']['text']}; background: {team_colors['red']['border']}; }}
QLabel#teamTintBadge[tint="blue"] {{ color: {team_colors['blue']['text']}; background: {team_colors['blue']['border']}; }}
QLabel#teamTintBadge[tint="yellow"] {{ color: {team_colors['yellow']['text']}; background: {team_colors['yellow']['border']}; }}
QLabel#teamTintBadge[tint="green"] {{ color: {team_colors['green']['text']}; background: {team_colors['green']['border']}; }}
QLabel#eliminationBadge {{ border-radius: 999px; padding: 3px 8px; font-weight: 700; }}
QLabel#eliminationBadge[elimination="eliminated"] {{ color: {colors['loss_text']}; background: {colors['loss_bg']}; border: 1px solid {colors['loss_border']}; }}
QLabel#eliminationBadge[elimination="active"] {{ color: {colors['win_text']}; background: {colors['win_bg']}; border: 1px solid {colors['win_border']}; }}
QLabel#playerName {{ color: {colors['neutral']}; }}
QLabel#queryTargetBadge {{ color: {colors['neutral']}; background: {colors['recent_bg']}; border: 1px solid {colors['outline']}; }}
QFrame#statChip[metricTone="positive"] {{ background: {colors['win_bg']}; border: 1px solid {colors['win_border']}; }}
QFrame#statChip[metricTone="positive"] QLabel#statValue {{ color: {colors['positive']}; }}
QFrame#statChip[metricTone="positive"] QLabel#statMetricIcon {{ background: {colors['win_hover']}; border: 1px solid {colors['positive']}; }}
QFrame#statChip[metricTone="negative"] {{ background: {colors['loss_bg']}; border: 1px solid {colors['loss_border']}; }}
QFrame#statChip[metricTone="negative"] QLabel#statValue {{ color: {colors['negative']}; }}
QFrame#statChip[metricTone="negative"] QLabel#statMetricIcon {{ background: {colors['loss_hover']}; border: 1px solid {colors['negative']}; }}
QFrame#statChip[metricTone="neutral"] {{ background: {colors['daily_bg']}; border: 1px solid {colors['outline']}; }}
QFrame#statChip[metricTone="neutral"] QLabel#statValue {{ color: {colors['neutral']}; }}
QFrame#statChip[metricTone="neutral"] QLabel#statMetricIcon {{ background: {colors['unknown_hover']}; border: 1px solid {colors['outline']}; }}
QFrame#statChip[metricTone="highlight"] {{ background: {rank_colors['1']['bg']}; border: 1px solid {rank_colors['1']['border']}; }}
QFrame#statChip[metricTone="highlight"] QLabel#statLabel,
QFrame#statChip[metricTone="highlight"] QLabel#statValue {{ color: {rank_colors['1']['text']}; font-weight: 750; }}
QFrame#statChip[metricTone="highlight"] QLabel#statMetricIcon {{ background: {rank_colors['1']['hover']}; border: 2px solid {rank_colors['1']['border']}; }}
QFrame#teamOverviewCard {{ background: {colors['daily_bg']}; border: 1px solid {colors['outline']}; }}
QFrame#teamOverviewItem {{ background: {colors['recent_bg']}; border: 1px solid {colors['outline']}; }}
QFrame#teamOverviewItem[teamResult="win"] {{ border-top: 3px solid {colors['positive']}; }}
QFrame#teamOverviewItem[teamResult="loss"] {{ border-top: 3px solid {colors['negative']}; }}
QFrame#teamOverviewItem[teamResult="neutral"] {{ border-top: 3px solid {colors['outline']}; }}
QFrame#matchDetailRibbon QLabel#detailKicker,
QFrame#matchDetailRibbon QLabel#detailHighlight {{ color: {colors['win_text']}; }}
QFrame#matchDetailRibbon[recordOutcome="loss"] QLabel#detailKicker,
QFrame#matchDetailRibbon[recordOutcome="loss"] QLabel#detailHighlight {{ color: {colors['neutral']}; }}
QFrame#matchDetailRibbon[recordOutcome="unknown"] QLabel#detailKicker,
QFrame#matchDetailRibbon[recordOutcome="unknown"] QLabel#detailHighlight {{ color: {colors['unknown_text']}; }}
QFrame#matchDetailRibbon QLabel#detailSubline,
QFrame#matchDetailRibbon QLabel#detailStatCaption {{ color: {colors['win_meta']}; }}
QFrame#matchDetailRibbon[recordOutcome="loss"] QLabel#detailSubline,
QFrame#matchDetailRibbon[recordOutcome="loss"] QLabel#detailStatCaption {{ color: {colors['unknown_meta']}; }}
QFrame#matchDetailRibbon[recordOutcome="unknown"] QLabel#detailSubline,
QFrame#matchDetailRibbon[recordOutcome="unknown"] QLabel#detailStatCaption {{ color: {colors['unknown_meta']}; }}
QFrame#matchDetailRibbon QLabel#detailStatValue {{ color: {colors['neutral']}; }}
QFrame#matchDetailRibbon[recordOutcome="win"] QLabel#detailStatValue {{ color: {colors['win_text']}; }}
QFrame#matchDetailRibbon[recordOutcome="loss"] QLabel#detailStatValue {{ color: {colors['neutral']}; }}
QProgressBar#detailPulse::chunk {{ background: {colors['outline']}; }}
QFrame#matchDetailRibbon[recordOutcome="win"] QProgressBar#detailPulse::chunk {{ background: {colors['positive']}; }}
QFrame#matchDetailRibbon[recordOutcome="loss"] QProgressBar#detailPulse::chunk {{ background: {colors['outline']}; }}
QComboBox#animationSpeed {{ color: {colors['neutral']}; background: {colors['daily_bg']}; border: 1px solid {colors['outline']}; }}
QComboBox#animationSpeed::drop-down {{ width: 28px; border: none; border-left: 1px solid {colors['outline']}; }}
QFrame#comboPopup, QWidget#comboPopup, QComboBoxPrivateContainer#comboPopup {{ color: {colors['neutral']}; background: {colors['daily_bg']}; border: 1px solid {colors['outline']}; border-radius: 10px; }}
QAbstractItemView#comboPopupView, QListView#comboPopupView {{ color: {colors['neutral']}; background: {colors['daily_bg']}; border: none; outline: none; }}
QAbstractItemView#comboPopupView::item:hover,
QAbstractItemView#comboPopupView::item:selected {{ color: {colors['neutral']}; background: {colors['recent_hover']}; }}
QFrame#matchRecord[recordOutcome="win"] QLabel#matchRowTitle,
QFrame#matchRecord[recordOutcome="win"] QLabel#matchRowOutcome {{ color: {colors['win_text']}; }}
QFrame#matchRecord[recordOutcome="win"] QLabel#matchRowMeta {{ color: {colors['win_meta']}; }}
QFrame#matchRecord[recordOutcome="loss"] QLabel#matchRowTitle,
QFrame#matchRecord[recordOutcome="loss"] QLabel#matchRowOutcome {{ color: {colors['loss_text']}; }}
QFrame#matchRecord[recordOutcome="loss"] QLabel#matchRowMeta {{ color: {colors['loss_meta']}; }}
QFrame#matchRecord[recordOutcome="unknown"] QLabel#matchRowTitle,
QFrame#matchRecord[recordOutcome="unknown"] QLabel#matchRowOutcome {{ color: {colors['unknown_text']}; }}
QFrame#matchRecord[recordOutcome="unknown"] QLabel#matchRowMeta {{ color: {colors['unknown_meta']}; }}
QLabel#matchRowOutcome {{ background: rgba(255,255,255,28); border-radius: 10px; padding: 5px 9px; font-weight: 700; }}
QFrame#dailyStatChip {{ background: {colors['daily_bg']}; }}
QLabel#dailyStatValue[semantic="positive"] {{ color: {colors['positive']}; font-size: 16px; font-weight: 700; }}
QLabel#dailyStatValue[semantic="negative"] {{ color: {colors['negative']}; font-size: 16px; font-weight: 700; }}
QLabel#dailyStatValue[semantic="neutral"] {{ color: {colors['neutral']}; font-size: 16px; font-weight: 700; }}
QLabel#matchPullHint {{ color: {colors['unknown_meta']}; background: {colors['daily_bg']}; border: 1px solid {colors['outline']}; border-radius: 12px; font-size: 11px; padding: 3px 10px; }}
QPushButton#recentQueryButton {{ color: {colors['recent_text']}; background: {colors['recent_bg']}; border: 1px solid transparent; }}
QPushButton#recentQueryButton:hover {{ background: {colors['recent_hover']}; border-color: {colors['outline']}; }}
QFrame#leaderboardHero {{ background: {colors['recent_bg']}; border: 1px solid {colors['outline']}; }}
QFrame#leaderboardCard {{ background: {colors['daily_bg']}; border: 1px solid {colors['outline']}; }}
QFrame#leaderboardRow {{ background: {colors['recent_bg']}; border: 1px solid {colors['outline']}; }}
QFrame#leaderboardRow:hover {{ background: {colors['recent_hover']}; border-color: {colors['outline']}; }}
QFrame#leaderboardRow[rankLevel="1"] {{ background: {rank_colors['1']['bg']}; border: 1px solid {rank_colors['1']['border']}; }}
QFrame#leaderboardRow[rankLevel="1"]:hover {{ background: {rank_colors['1']['hover']}; }}
QFrame#leaderboardRow[rankLevel="2"] {{ background: {rank_colors['2']['bg']}; border: 1px solid {rank_colors['2']['border']}; }}
QFrame#leaderboardRow[rankLevel="2"]:hover {{ background: {rank_colors['2']['hover']}; }}
QFrame#leaderboardRow[rankLevel="3"] {{ background: {rank_colors['3']['bg']}; border: 1px solid {rank_colors['3']['border']}; }}
QFrame#leaderboardRow[rankLevel="3"]:hover {{ background: {rank_colors['3']['hover']}; }}
QFrame#leaderboardRow[rankLevel="4"] {{ background: {rank_colors['4']['bg']}; border: 1px solid {rank_colors['4']['border']}; }}
QFrame#leaderboardRow[rankLevel="5"] {{ background: {rank_colors['5']['bg']}; border: 1px solid {rank_colors['5']['border']}; }}
QFrame#leaderboardRow[rankLevel="1"] QLabel#leaderboardRank {{ color: {rank_colors['1']['text']}; font-size: 26px; font-weight: 850; }}
QFrame#leaderboardRow[rankLevel="2"] QLabel#leaderboardRank {{ color: {rank_colors['2']['text']}; font-size: 24px; font-weight: 800; }}
QFrame#leaderboardRow[rankLevel="3"] QLabel#leaderboardRank {{ color: {rank_colors['3']['text']}; font-size: 22px; font-weight: 780; }}
QFrame#leaderboardRow[rankLevel="4"] QLabel#leaderboardRank {{ color: {rank_colors['4']['text']}; font-size: 21px; }}
QFrame#leaderboardRow[rankLevel="5"] QLabel#leaderboardRank {{ color: {rank_colors['5']['text']}; font-size: 20px; }}
QFrame#leaderboardRow[rankLevel="1"] QLabel#leaderboardName {{ color: {rank_colors['1']['text']}; font-size: 16px; font-weight: 800; }}
QFrame#leaderboardRow[rankLevel="2"] QLabel#leaderboardName {{ color: {rank_colors['2']['text']}; font-size: 15px; font-weight: 750; }}
QFrame#leaderboardRow[rankLevel="3"] QLabel#leaderboardName {{ color: {rank_colors['3']['text']}; font-size: 14px; font-weight: 700; }}
QFrame#leaderboardRow[rankLevel="1"] QLabel#leaderboardScore {{ color: {rank_colors['1']['text']}; font-size: 20px; font-weight: 850; }}
QFrame#leaderboardRow[rankLevel="2"] QLabel#leaderboardScore {{ color: {rank_colors['2']['text']}; font-size: 19px; font-weight: 800; }}
QFrame#leaderboardRow[rankLevel="3"] QLabel#leaderboardScore {{ color: {rank_colors['3']['text']}; font-size: 18px; font-weight: 780; }}
QFrame#leaderboardRow[rankLevel="4"] QLabel#leaderboardName,
QFrame#leaderboardRow[rankLevel="4"] QLabel#leaderboardScore {{ color: {rank_colors['4']['text']}; }}
QFrame#leaderboardRow[rankLevel="5"] QLabel#leaderboardName,
QFrame#leaderboardRow[rankLevel="5"] QLabel#leaderboardScore {{ color: {rank_colors['5']['text']}; }}
QLabel#leaderboardRank {{ color: {colors['outline']}; }}
QLabel#leaderboardName {{ color: {colors['neutral']}; }}
QLabel#leaderboardScore {{ color: {colors['positive']}; }}
QLabel#leaderboardMeta {{ color: {colors['unknown_meta']}; }}
QComboBox#leaderboardFilter {{ color: {colors['neutral']}; background: {colors['daily_bg']}; border: 1px solid {colors['outline']}; selection-background-color: {colors['recent_hover']}; }}
QComboBox#leaderboardFilter::drop-down {{ width: 28px; border: none; border-left: 1px solid {colors['outline']}; }}
QComboBox#leaderboardFilter:hover {{ background: {colors['recent_bg']}; border-color: {rank_colors['1']['border']}; }}
QComboBox#leaderboardFilter:focus {{ background: {colors['recent_bg']}; border: 1px solid {colors['outline']}; padding: 5px 12px; }}
QComboBox#animationSpeed {{ color: {colors['neutral']}; background: {colors['daily_bg']}; border: 1px solid {colors['outline']}; selection-background-color: {colors['recent_hover']}; }}
QComboBox#animationSpeed:hover {{ background: {colors['recent_bg']}; border-color: {rank_colors['1']['border']}; }}
QComboBox#animationSpeed:focus {{ background: {colors['recent_bg']}; border: 1px solid {colors['outline']}; padding: 5px 12px; }}
QComboBox#leaderboardFilter QAbstractItemView,
QComboBox#animationSpeed QAbstractItemView,
QAbstractItemView#comboPopupView {{ color: {colors['neutral']}; background: transparent; border: none; outline: none; selection-background-color: {colors['recent_hover']}; selection-color: {colors['neutral']}; }}
QFrame#comboPopup, QWidget#comboPopup, QComboBoxPrivateContainer#comboPopup {{ color: {colors['neutral']}; background: {colors['daily_bg']}; border: 1px solid {colors['outline']}; border-radius: 10px; }}
QAbstractItemView#comboPopupView::item, QListView#comboPopupView::item {{ color: {colors['neutral']}; background: transparent; min-height: 32px; padding: 7px 12px; border-radius: 7px; }}
QAbstractItemView#comboPopupView::item:hover, QListView#comboPopupView::item:hover {{ background: {colors['recent_hover']}; color: {colors['neutral']}; }}
QAbstractItemView#comboPopupView::item:selected, QListView#comboPopupView::item:selected {{ background: {colors['recent_hover']}; color: {colors['neutral']}; font-weight: 700; }}
/* Match detail surfaces stay neutral; red/green is reserved for result badges
   and metric semantics so two stacked cards do not become one large red block. */
QFrame#matchDetailHero {{ background: {colors['daily_bg']}; border: 1px solid {colors['outline']}; }}
QFrame#matchDetailRibbon {{ background: {colors['recent_bg']}; border: 1px solid {colors['outline']}; }}
QFrame#matchDetailHero QLabel#detailKicker,
QFrame#matchDetailHero QLabel#detailHighlight,
QFrame#matchDetailRibbon QLabel#detailKicker,
QFrame#matchDetailRibbon QLabel#detailHighlight {{ color: {colors['neutral']}; }}
QFrame#matchDetailHero QLabel#detailSubline,
QFrame#matchDetailHero QLabel#detailStatCaption,
QFrame#matchDetailRibbon QLabel#detailSubline,
QFrame#matchDetailRibbon QLabel#detailStatCaption {{ color: {colors['unknown_meta']}; }}
QFrame#matchDetailHero QLabel#detailStatValue,
QFrame#matchDetailRibbon QLabel#detailStatValue {{ color: {colors['neutral']}; }}
QProgressBar#detailPulse {{ background: {colors['unknown_bg']}; border: 1px solid {colors['outline']}; }}
QProgressBar#detailPulse::chunk {{ background: {colors['outline']}; }}
"""


@lru_cache(maxsize=4)
def build_style(theme_key):
    """返回完整主题样式，并在所有分区共用统一的材质层级。"""
    if theme_key not in THEME_PALETTES:
        theme_key = 'light'
    return (LARGE_STYLE + THEME_OVERRIDES[theme_key]
            + REFINED_SURFACES[theme_key] + _match_state_style(theme_key)
            + _taskbar_switcher_style(theme_key)
            + _lobby_chat_style(theme_key))


def _lobby_chat_style(theme_key):
    """大厅聊天的层级样式，颜色跟随战局数据的主题调色板。"""
    colors = MATCH_ROW_PALETTES.get(theme_key, MATCH_ROW_PALETTES['light'])
    if theme_key == 'light':
        surface, raised, text, muted = '#f4f7fa', '#eaf0f6', '#1c2c3d', '#5b6d80'
        mine, mine_border = '#dfeeff', '#8db6df'
        online = '#168052'
    elif theme_key == 'glass':
        surface, raised, text, muted = 'rgba(255,255,255,28)', 'rgba(255,255,255,48)', '#f0f0f2', '#c2c4c8'
        mine, mine_border = 'rgba(185,218,255,56)', 'rgba(185,218,255,138)'
        online = '#a6e6bd'
    elif theme_key == 'liquid':
        surface, raised, text, muted = 'rgba(226,232,243,74)', 'rgba(245,248,255,86)', '#f4f6fb', '#c2cad7'
        mine, mine_border = 'rgba(208,226,250,112)', 'rgba(245,249,255,190)'
        online = '#b6efcf'
    elif theme_key == 'blue':
        surface, raised, text, muted = '#0d2e5d', '#123b73', '#e6f0ff', '#afc8e5'
        mine, mine_border = '#174476', '#78aee9'
        online = '#a4e6bd'
    else:
        surface, raised, text, muted = '#12243a', '#1b304b', '#e8eef8', '#b8c7d9'
        mine, mine_border = '#1b3b60', '#6fa9ef'
        online = '#9fe0b6'
    return f"""
QFrame#chatOverviewCard, QFrame#chatCard {{ background: {surface}; border: 1px solid {colors['outline']}; border-radius: 16px; }}
QFrame#chatOnlineCard {{ background: {raised}; border: 1px solid {colors['outline']}; border-radius: 12px; min-width: 86px; }}
QLabel#chatOnlineCaption {{ color: {muted}; font-size: 10px; }}
QLabel#chatOnlineCount {{ color: {online}; font-size: 21px; font-weight: 800; }}
QLabel#chatConnectionBadge {{ color: {colors['positive']}; background: {colors['win_bg']}; border: 1px solid {colors['win_border']}; border-radius: 999px; padding: 3px 8px; font-size: 10px; font-weight: 700; }}
QLabel#chatServerHint, QLabel#chatStatus, QLabel#chatEmpty {{ color: {muted}; }}
QScrollArea#chatScroll {{ background: transparent; border: none; border-radius: 12px; }}
QWidget#chatMessagesHost {{ background: transparent; }}
QFrame#chatMessageBubble {{ background: {raised}; border: 1px solid {colors['outline']}; border-radius: 13px; }}
QFrame#chatMessageBubble[mine="true"] {{ background: {mine}; border-color: {mine_border}; }}
QLabel#chatMessageAuthor {{ color: {text}; font-size: 11px; font-weight: 700; }}
QLabel#chatMessageTime {{ color: {muted}; font-size: 10px; }}
QLabel#chatMessageText {{ color: {text}; font-size: 12px; padding-top: 2px; }}
QPushButton#chatRefreshButton, QPushButton#chatSendButton {{ min-height: 36px; padding: 8px 15px; border-radius: 10px; }}
QPushButton#chatEmojiButton {{ color: {text}; background: {raised}; border: 1px solid {colors['outline']}; border-radius: 10px; min-width: 38px; min-height: 36px; padding: 0; font-size: 18px; }}
QPushButton#chatEmojiButton:hover {{ background: {colors['recent_hover']}; border-color: {mine_border}; }}
QPushButton#chatEmojiButton:pressed {{ background: {mine}; }}
QPushButton#chatEmojiButton:disabled {{ color: {muted}; background: transparent; border-color: {colors['outline']}; }}
QFrame#chatEmojiPopup {{ color: {text}; background: {raised}; border: 1px solid {colors['outline']}; border-radius: 14px; }}
QPushButton#chatEmojiItem {{ color: {text}; background: transparent; border: none; border-radius: 8px; min-width: 34px; min-height: 34px; padding: 0; font-size: 18px; }}
QPushButton#chatEmojiItem:hover {{ background: {colors['recent_hover']}; }}
QPushButton#chatEmojiItem:pressed {{ background: {mine}; }}
"""


def _taskbar_switcher_style(theme_key):
    """快捷工具的任务栏窗口列表共用应用主题的颜色和层级。"""
    palettes = {
        'glass': {
            'surface': 'rgba(255,255,255,28)', 'raised': 'rgba(255,255,255,42)',
            'field': '#252a31', 'text': '#f2f6fb', 'muted': '#c0ccd8',
            'border': 'rgba(255,255,255,76)', 'accent': '#d9eaff',
            'accent_text': '#152233', 'hover': 'rgba(255,255,255,44)',
            'selected': 'rgba(185,218,255,76)', 'error': '#ffb4b1',
        },
        'liquid': {
            'surface': 'rgba(42,47,59,218)', 'raised': 'rgba(223,231,244,74)',
            'field': '#252a36', 'text': '#f4f6fb', 'muted': '#c2cad7',
            'border': 'rgba(245,249,255,150)', 'accent': '#d6e7ff',
            'accent_text': '#16243a', 'hover': 'rgba(235,242,253,86)',
            'selected': 'rgba(205,225,251,112)', 'error': '#ffb1b5',
        },
        'dark': {
            'surface': '#16263a', 'raised': '#0d1928', 'field': '#0c1521',
            'text': '#e8eef8', 'muted': '#a9c1de', 'border': '#53667e',
            'accent': '#a7c7ea', 'accent_text': '#102640',
            'hover': '#1d2c40', 'selected': '#29415f', 'error': '#ffaaa6',
        },
        'light': {
            'surface': '#ffffff', 'raised': '#f1f5f9', 'field': '#ffffff',
            'text': '#1c2c3d', 'muted': '#536679', 'border': '#c4ced8',
            'accent': '#3f79b4', 'accent_text': '#ffffff',
            'hover': '#e8eef4', 'selected': '#dce8f5', 'error': '#a8323c',
        },
        'blue': {
            'surface': 'rgba(8,31,66,224)', 'raised': 'rgba(6,23,52,230)',
            'field': '#082248', 'text': '#e6f0ff', 'muted': '#a9c1de',
            'border': 'rgba(173,214,255,74)', 'accent': '#a9d0fa',
            'accent_text': '#092448', 'hover': 'rgba(79,148,224,40)',
            'selected': 'rgba(68,125,191,74)', 'error': '#ffb0ad',
        },
    }
    colors = palettes.get(theme_key, palettes['light'])
    return f"""
QFrame#taskbarInfoCard, QFrame#taskbarListCard {{
    color: {colors['text']}; background: {colors['surface']};
    border: 1px solid {colors['border']}; border-radius: 16px;
}}
QLabel#taskbarStatus {{ color: {colors['text']}; font-weight: 700; }}
QLabel#taskbarNotice {{ color: {colors['muted']}; }}
QLabel#taskbarNotice[noticeLevel="error"] {{ color: {colors['error']}; font-weight: 650; }}
QLabel#taskbarHotkeyHint {{ color: {colors['muted']}; }}
QTableWidget#taskbarTable {{
    color: {colors['text']}; background: {colors['raised']};
    alternate-background-color: {colors['surface']};
    border: 1px solid {colors['border']}; border-radius: 10px;
    gridline-color: {colors['border']}; selection-background-color: {colors['selected']};
    selection-color: {colors['text']}; outline: none;
}}
QTableWidget#taskbarTable::item {{ padding: 8px 10px; border: none; border-radius: 5px; }}
QTableWidget#taskbarTable::item:hover {{ background: {colors['hover']}; }}
QTableWidget#taskbarTable::item:selected {{ background: {colors['selected']}; color: {colors['text']}; }}
QHeaderView#taskbarHeader::section {{
    color: {colors['muted']}; background: {colors['surface']};
    border: none; border-bottom: 1px solid {colors['border']};
    padding: 8px 10px; font-weight: 700;
}}
QPushButton#taskbarRefresh {{ color: {colors['text']}; background: {colors['raised']};
    border: 1px solid {colors['border']}; border-radius: 9px; padding: 8px 12px; }}
QPushButton#taskbarRefresh:hover {{ background: {colors['hover']}; }}
QPushButton#taskbarAction {{ color: {colors['accent_text']}; background: {colors['accent']};
    border: 1px solid {colors['accent']}; border-radius: 10px; padding: 9px 14px; font-weight: 700; }}
QPushButton#taskbarAction:hover {{ background: {colors['hover']}; color: {colors['text']}; }}
QPushButton#taskbarAction:disabled, QPushButton#taskbarRefresh:disabled {{ color: {colors['muted']}; background: {colors['raised']}; border-color: {colors['border']}; }}
"""


# 线性 SVG 图标采用 Morphicons 同样的视觉语言：统一笔画、圆角端点、
# 状态色切换。图标在 Qt 内渲染为 QIcon，因此 EXE 不需要额外的 Node 运行时。
ICON_PATHS = {
    'home': '<path d="M3 10.7 12 3l9 7.7"/><path d="M5.5 9.6V21h13V9.6"/><path d="M9.5 21v-6h5v6"/>',
    'shield': '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/>',
    'sparkles': '<path d="m12 3 1.1 4.9L18 9l-4.9 1.1L12 15l-1.1-4.9L6 9l4.9-1.1Z"/><path d="m19 15 .6 2.4L22 18l-2.4.6L19 21l-.6-2.4L16 18l2.4-.6Z"/>',
    'chart': '<path d="M4 19V5"/><path d="M4 19h16"/><path d="m7 15 3-4 3 2 4-6"/>',
    'command': '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="m8 9 3 3-3 3M13 15h3"/>',
    'network': '<circle cx="12" cy="5" r="2"/><circle cx="5" cy="18" r="2"/><circle cx="19" cy="18" r="2"/><path d="m10.8 6.7-4.5 9.6M13.2 6.7l4.5 9.6M7 18h10"/>',
    'user': '<circle cx="12" cy="8" r="3.5"/><path d="M5 21a7 7 0 0 1 14 0"/>',
    'chat': '<path d="M4 5.5h16v11H9l-5 3v-14Z"/><path d="M8 9h8M8 12.5h5"/>',
    'send': '<path d="m3 11 18-8-8 18-2.5-7.5Z"/><path d="M10.5 13.5 21 3"/>',
    'refresh': '<path d="M20 11a8 8 0 0 0-14.7-4L3 10"/><path d="M3 5v5h5"/><path d="M4 13a8 8 0 0 0 14.7 4L21 14"/><path d="M21 19v-5h-5"/>',
    'settings': '<path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/><circle cx="12" cy="12" r="4"/>',
    'check': '<circle cx="12" cy="12" r="9"/><path d="m8 12 2.5 2.5L16 9"/>',
    'minimize': '<path d="M5 12h14"/>',
    'maximize': '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    'close': '<path d="m6 6 12 12M18 6 6 18"/>',
    'layers': '<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/>',
    'moon': '<path d="M20.5 14.2A8.5 8.5 0 0 1 9.8 3.5 8.7 8.7 0 1 0 20.5 14.2Z"/>',
    'sun': '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.42 1.42M17.65 17.65l1.42 1.42M2 12h2M20 12h2M4.93 19.07l1.42-1.42M17.65 6.35l1.42-1.42"/>',
    'waves': '<path d="M2 8c2.5 0 2.5-2 5-2s2.5 2 5 2 2.5-2 5-2 2.5 2 5 2"/><path d="M2 13c2.5 0 2.5-2 5-2s2.5 2 5 2 2.5-2 5-2 2.5 2 5 2"/><path d="M2 18c2.5 0 2.5-2 5-2s2.5 2 5 2 2.5-2 5-2 2.5 2 5 2"/>',
    'sword': '<path d="m5 19 9.7-9.7"/><path d="m13.1 5.2 5.7 5.7"/><path d="m15.3 3 5.7 5.7-2.2 2.2-5.7-5.7Z"/><path d="m4 20 3.7-1L5 16.3Z"/>',
    'target': '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="3.2"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>',
    'skull': '<path d="M7.2 14.8a7 7 0 1 1 9.6 0v2.2H7.2Z"/><circle cx="9.6" cy="10.5" r="1.1"/><circle cx="14.4" cy="10.5" r="1.1"/><path d="M9.5 17v3h5v-3M10.5 14h3"/>',
    'bed': '<path d="M4 18v-7.5h16V18"/><path d="M4 14h16M7 10.5V8h5a3 3 0 0 1 3 3"/><path d="M4 18v2M20 18v2"/>',
    'bolt': '<path d="m13.5 2-8 11h6l-1.5 9 8-12h-6Z"/>',
    'gem': '<path d="m4 9 4-5h8l4 5-8 11Z"/><path d="m4 9h16M8 4l4 5 4-5M8 9l4 11 4-11"/>',
    'diamond': '<path d="m4 9 4-5h8l4 5-8 11Z"/><path d="m4 9h16M8 4l4 5 4-5"/>',
    'blocks': '<path d="m4 7 4-2 4 2-4 2Z"/><path d="m12 7 4-2 4 2-4 2Z"/><path d="M4 7v5l4 2 4-2V7M12 7v5l4 2 4-2V7"/><path d="m8 12 4 2 4-2v5l-4 2-4-2Z"/>',
    'hammer': '<path d="m4 20 8.5-8.5"/><path d="m13 4 7 7"/><path d="m11 6 3-3 7 7-3 3Z"/>',
}
ICON_ALIASES = {
    '⌂': 'home', '✦': 'sparkles', '◌': 'chart', '◒': 'command', '⌁': 'network',
    '⚙': 'settings', '⚡': 'sparkles', '●': 'check', '◆': 'sparkles',
}


@lru_cache(maxsize=256)
def _render_svg_icon(name, color, size=22, stroke_width=1.8):
    """Render a line icon once and reuse its immutable QPixmap.

    Metric cards, navigation buttons and theme refreshes request the same small
    set of SVGs repeatedly.  QSvgRenderer is relatively expensive on the GUI
    thread; caching the resulting pixmap removes that repeated rasterisation
    without changing geometry, colours or the widget tree.  QPixmap is
    implicitly shared, so QLabel.setPixmap() safely keeps its own reference.
    """
    body = ICON_PATHS.get(resolve_icon_name(name), ICON_PATHS['command'])
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 24 24"><g fill="none" stroke="{color}" '
        f'stroke-width="{stroke_width}" stroke-linecap="round" stroke-linejoin="round">'
        f'{body}</g></svg>'
    )
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode('utf-8'))).render(painter)
    painter.end()
    return pixmap


def line_icon(name, normal='#b8c8dc', active='#eff7ff', size=22, hovered=None):
    """创建主题一致、支持悬停和选中状态的轻量线性图标。"""
    icon = QIcon()
    icon.addPixmap(_render_svg_icon(name, normal, size), QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(_render_svg_icon(name, active, size), QIcon.Mode.Normal, QIcon.State.On)
    if hovered:
        icon.addPixmap(_render_svg_icon(name, hovered, size), QIcon.Mode.Active, QIcon.State.Off)
        icon.addPixmap(_render_svg_icon(name, active, size), QIcon.Mode.Active, QIcon.State.On)
    return icon


def primary_icon_color(theme_key):
    """Return a readable glyph color for the current primary button fill."""
    return '#ffffff' if theme_key == 'light' else '#102640'


def resolve_icon_name(name):
    return name if name in ICON_PATHS else ICON_ALIASES.get(name, 'command')


def icon_tile(name, object_name='moduleIconTile', color='#a9d4ff', size=24, tile_size=48):
    """创建统一的线性图标容器，页面模块和状态行共用同一套视觉语言。"""
    tile = QFrame()
    tile.setObjectName(object_name)
    tile.setFixedSize(tile_size, tile_size)
    tile_layout = QHBoxLayout(tile)
    tile_layout.setContentsMargins(0, 0, 0, 0)
    tile_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
    glyph = QLabel()
    glyph.setPixmap(_render_svg_icon(resolve_icon_name(name), color, size))
    glyph.setFixedSize(size, size)
    glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
    glyph.setProperty('iconTileGlyph', True)
    glyph.setProperty('iconName', resolve_icon_name(name))
    glyph.setProperty('iconPixelSize', size)
    glyph.setProperty('iconSemanticRole', 'status' if object_name == 'statusGlyph' else 'accent')
    glyph.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    tile_layout.addWidget(glyph)
    return tile


class ThemeBackdrop(Backdrop):
    """主题背景。

    The WebGL reference renders each pane from a live scene texture and adds
    refraction, a bevel highlight and a slow specular pass.  A translucent
    Qt window cannot safely host a second full-screen OpenGL surface on every
    Windows compositor, so the native implementation keeps the same visual
    ingredients in a bounded paint pass: a cached optical base plus a small
    animated light field, caustic ribbons and a moving rim sheen.  Only this
    one background widget repaints at 30 fps while the liquid theme is active;
    child widgets keep their normal cached backing stores.
    """

    def __init__(self, theme='light', parent=None):
        self.theme = theme
        super().__init__(parent)
        self._liquid_phase = 0.0
        self._liquid_timer = QTimer(self)
        self._liquid_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._liquid_timer.setInterval(33)
        self._liquid_timer.timeout.connect(self._advance_liquid)
        self.setMouseTracking(True)
        if self.theme == 'liquid':
            self._liquid_timer.start()

    def _advance_liquid(self):
        # Window dragging is deliberately quiet: the compositor already has
        # more work to do while moving a frameless window, and a frozen frame
        # looks steadier than a competing animation during the gesture.
        host = self.window()
        if not self.isVisible() or host is None or host.isMinimized():
            return
        if getattr(host, '_dragging', False):
            return
        self._liquid_phase = (self._liquid_phase + 0.024) % (math.tau)
        self.update()

    def set_theme(self, theme):
        if theme not in THEME_PALETTES or theme == self.theme:
            return
        self.theme = theme
        if theme == 'liquid':
            self._liquid_timer.start()
        else:
            self._liquid_timer.stop()
        self._background_cache = None
        self._cache_key = None
        self.update()

    def _rebuild_cache(self):
        width, height = self.width(), self.height()
        if width <= 0 or height <= 0:
            self._background_cache = None
            self._cache_key = None
            return
        palette = THEME_PALETTES[self.theme]
        cache = QPixmap(width, height)
        cache.fill(Qt.GlobalColor.transparent)
        if self.theme == 'liquid':
            # Do not paint a charcoal or coloured base under the live field.
            # Keeping a real transparent pixmap also avoids the opaque
            # fallback path while Windows DWM supplies the frosted glass.
            self._background_cache = cache
            self._cache_key = (width, height, round(self.devicePixelRatioF(), 2))
            return
        painter = QPainter(cache)
        base = QLinearGradient(0, 0, width, height)
        base.setColorAt(0, QColor(*palette['start']))
        base.setColorAt(1, QColor(*palette['end']))
        painter.fillRect(0, 0, width, height, base)
        for x, y, radius, color in [
            (.05, .0, .65, palette['glow1']),
            (.95, .65, .5, palette['glow2']),
        ]:
            glow = QRadialGradient(width * x, height * y, width * radius)
            glow.setColorAt(0, QColor(*color))
            glow.setColorAt(1, QColor(0, 0, 0, 0))
            painter.fillRect(0, 0, width, height, glow)
        painter.end()
        self._background_cache = cache
        # ThemeBackdrop 在切换主题时会清空缓存；这里保持与父类相同的 key
        # 结构，避免每次普通 paintEvent 都重复重建渐变。
        self._cache_key = (width, height, round(self.devicePixelRatioF(), 2))

    @staticmethod
    def _rgba(values, alpha_scale=1.0):
        """Convert a theme tuple into a QColor without leaking alpha math."""
        r, g, b, a = values
        return QColor(int(r), int(g), int(b), max(0, min(255, int(a * alpha_scale))))

    def _paint_liquid_layers(self, painter):
        """Paint the animated optical field over the cached background."""
        width, height = self.width(), self.height()
        if width <= 0 or height <= 0:
            return
        phase = self._liquid_phase
        short = float(min(width, height))
        # Two broad colour volumes move on different orbits.  The overlap is
        # what gives the background the soft cyan-to-violet dispersion visible
        # through the semi-transparent cards.
        blobs = [
            (0.16 + 0.08 * math.sin(phase * 0.71),
             0.20 + 0.12 * math.cos(phase * 0.53),
             0.60, (79, 155, 235, 62)),
            (0.82 + 0.10 * math.cos(phase * 0.47),
             0.30 + 0.14 * math.sin(phase * 0.63),
             0.53, (210, 73, 181, 48)),
            (0.58 + 0.12 * math.sin(phase * 0.38),
             0.88 + 0.08 * math.cos(phase * 0.59),
             0.44, (235, 137, 104, 40)),
            (0.28 + 0.10 * math.cos(phase * 0.31),
             0.68 + 0.10 * math.sin(phase * 0.43),
             0.36, (104, 204, 208, 32)),
        ]
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        for x, y, radius_factor, color in blobs:
            cx, cy = width * x, height * y
            radius = short * radius_factor
            glow = QRadialGradient(QPointF(cx, cy), radius)
            glow.setColorAt(0.0, self._rgba(color, 1.0))
            glow.setColorAt(0.34, self._rgba(color, 0.58))
            glow.setColorAt(0.72, self._rgba(color, 0.16))
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            painter.setBrush(glow)
            painter.drawEllipse(QPointF(cx, cy), radius, radius)

        # Broad, low-alpha ribbons imitate the distortion bands of a liquid
        # lens.  Their paths stay inside the backdrop so no child widget is
        # repainted and no clipping artifacts appear at panel edges.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        ribbon_specs = [
            (0.16 + 0.035 * math.sin(phase * 0.45), 0.22, 0.22,
             QColor(228, 237, 255, 18), QColor(246, 250, 255, 62)),
            (0.64 + 0.045 * math.cos(phase * 0.36), -0.12, 0.20,
             QColor(245, 216, 255, 14), QColor(255, 241, 250, 48)),
            (0.44 + 0.04 * math.sin(phase * 0.57), 0.70, 0.16,
             QColor(255, 222, 197, 12), QColor(255, 245, 225, 42)),
        ]
        for index, (anchor, slope, curve, broad, edge) in enumerate(ribbon_specs):
            path = QPainterPath()
            y0 = height * (anchor + 0.12 * math.sin(phase * (0.24 + index * 0.07)))
            path.moveTo(-width * 0.15, y0 + height * slope)
            path.cubicTo(width * 0.18, y0 - height * curve,
                         width * 0.55, y0 + height * curve,
                         width * 1.15, y0 - height * slope)
            painter.setPen(QPen(broad, max(18.0, short * 0.045),
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(path)
            painter.setPen(QPen(edge, max(1.0, short * 0.0022),
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(path)

        # A narrow specular band slowly travels across the scene.  It is kept
        # below the opaque text/cards and reads as reflected light rather than
        # a noisy animation.
        sheen_x = -width * 0.3 + ((phase / math.tau) % 1.0) * width * 1.6
        sheen = QLinearGradient(sheen_x - width * 0.18, 0,
                                sheen_x + width * 0.18, 0)
        sheen.setColorAt(0.0, QColor(240, 244, 255, 0))
        sheen.setColorAt(0.5, QColor(255, 255, 255, 30))
        sheen.setColorAt(1.0, QColor(221, 231, 255, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(sheen)
        painter.drawRect(0, 0, width, height)

        # Fine arcs add a meniscus-like edge cue without drawing literal
        # outlines around every widget.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(3):
            cx = width * (0.18 + i * 0.37) + math.sin(phase * 0.4 + i) * width * 0.06
            cy = height * (0.25 + i * 0.28) + math.cos(phase * 0.32 + i) * height * 0.05
            radius = short * (0.22 + i * 0.035)
            pen_color = QColor(237, 244, 255, 30 if i != 1 else 40)
            painter.setPen(QPen(pen_color, max(1.0, short * 0.0018)))
            painter.drawArc(QRectF(cx - radius, cy - radius, radius * 2, radius * 2),
                            int((phase * 180 / math.pi + i * 72) * 16), 94 * 16)
        painter.restore()

    def paintEvent(self, event):
        cache_key = (self.width(), self.height(), round(self.devicePixelRatioF(), 2))
        if self._background_cache is None or self._cache_key != cache_key:
            self._rebuild_cache()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, self.theme == 'liquid')
        if self._background_cache is not None:
            painter.drawPixmap(0, 0, self._background_cache)
        else:
            painter.fillRect(self.rect(), QColor(0, 0, 0, 0))
        if self.theme == 'liquid':
            self._paint_liquid_layers(painter)
        painter.end()


class LargeApp(App):
    """把原优化助手嵌入可扩展的主界面。"""

    def __init__(self):
        self.current_theme = 'light'
        settings = QSettings('DEV King', '逐渐工具箱')
        self.animation_speed = str(settings.value('ui/animationSpeed', 'standard') or 'standard')
        self._settings = settings
        if self.animation_speed not in {'off', 'fast', 'standard', 'slow'}:
            self.animation_speed = 'standard'
        self._button_effects = {}
        self._button_animations = {}
        self._ui_animations = []
        self._metric_icon_labels = []
        self._api_token = bugland_api.load_token()
        self._api_token_is_default = self._api_token == bugland_api.DEFAULT_API_TOKEN
        self._player_id = self._load_player_id()
        self._api_worker = None
        self._api_action = None
        self._api_request_times = deque()
        self._match_page = 1
        self._match_player_name = ''
        self._match_records_accumulated = []
        self._match_detail_cache = {}
        self._api_queue = deque()
        self._closing = False
        self._api_generation = 0
        self._api_context = None
        self._history_pages = {}
        # Match history is rendered as one continuous list.  Keep page state
        # internally for the API, but let the user load the next page by
        # scrolling instead of exposing page buttons in the UI.
        self._history_loading_pages = set()
        self._history_has_more = True
        self._page_size = 10
        self._match_player_uuid = ''
        self._today_scan_complete = False
        self._today_scan_error = ''
        self._hydrating_keys = set()
        self._recent_queries = self._load_recent_queries()
        self._leaderboard_cache = {}
        self._leaderboard_pending_key = ''
        self._leaderboard_last_key = ''
        self._update_worker = None
        self._update_release_url = GITHUB_REPOSITORY_URL
        self._update_release_tag = ''
        self._taskbar_items = []
        self._taskbar_hotkeys = {}
        self._taskbar_action_worker = None
        self._taskbar_hotkey_worker = None
        self._taskbar_action_kind = ''
        self._taskbar_hotkeys_paused = False
        self._taskbar_bindings = {}
        self._chat_worker = None
        self._chat_messages = []
        self._chat_message_keys = set()
        self._chat_session_id = ''
        self._chat_pending_message = ''
        self._chat_sending_message = ''
        sound_enabled = settings.value(CHAT_SOUND_ENABLED_KEY, True)
        if isinstance(sound_enabled, str):
            sound_enabled = sound_enabled.strip().lower() not in {'0', 'false', 'off', 'no'}
        self.chat_sound_enabled = bool(sound_enabled)
        try:
            self.chat_sound_volume = max(0, min(100, int(
                settings.value(CHAT_SOUND_VOLUME_KEY, 65))))
        except (TypeError, ValueError):
            self.chat_sound_volume = 65
        self._chat_sound_effect = None
        self._chat_sound_path = ''
        self._chat_poll_timer = None
        # 旧版本保存过 Render HTTP 地址；聊天现在直接使用项目的 Realtime
        # WebSocket，忽略旧地址，避免把 WebSocket 协议误发到 HTTP 服务。
        self._chat_server_url = LOBBY_CHAT_SERVER_URL
        self._game_id = str(settings.value(GAME_ID_SETTINGS_KEY, '') or '').strip()
        # ``chat/gameId`` was used by an earlier preview build.  Migrate it
        # once so an existing login is not lost when the shared profile key is
        # introduced by the startup login dialog.
        if not self._game_id:
            self._game_id = str(settings.value('chat/gameId', '') or '').strip()
            if self._game_id:
                settings.setValue(GAME_ID_SETTINGS_KEY, self._game_id)
        for index, default in TASKBAR_DEFAULT_BINDINGS.items():
            key = f'plugins/taskbar_switcher/hotkeys/{index}'
            stored = settings.value(key, default)
            self._taskbar_bindings[index] = str(default if stored is None else stored)
        # Avoid repeatedly re-polishing the four daily summary labels when a
        # burst of detail requests completes without changing their visible
        # values.  The cache is purely a paint/update optimisation.
        self._daily_summary_visual = None
        self._match_overscroll_animations = []
        self._match_overscroll_hint_animation = None

        super().__init__()
        self._chat_client = SupabaseRealtimeChatClient(self)
        self._chat_client.statusChanged.connect(self._on_supabase_chat_status)
        self._chat_client.messageReceived.connect(self._on_supabase_chat_message)
        self._chat_client.messageSent.connect(self._on_supabase_message_sent)
        self._chat_client.messageFailed.connect(self._on_supabase_message_failed)
        self._chat_client.presenceChanged.connect(self._on_supabase_presence_changed)
        self._chat_client.error.connect(self._on_supabase_chat_error)
        self._initialize_chat_sound()
        # Restore the current session cache after the chat page has been built.
        # It is removed in closeEvent, so a normal restart starts with a clean
        # room while navigating between pages does not lose visible messages.
        self._restore_chat_session_cache()
        self.setWindowTitle('ZJ HUB')
        self.resize(1180, 800)
        # 留出标题、指标和两张功能卡的完整空间，避免缩放到窄窗口时说明文字被挤成一行。
        self.setMinimumSize(1020, 700)
        self.apply_theme(self.current_theme)
        self._taskbar_hotkey_worker = TaskbarHotkeyWorker(self._taskbar_bindings, self)
        self._taskbar_hotkey_worker.hotkeysReady.connect(self._on_taskbar_hotkeys_ready)
        self._taskbar_hotkey_worker.pressed.connect(self._on_taskbar_hotkey_pressed)
        self._taskbar_hotkey_worker.start()
        self._taskbar_auto_refresh_timer = QTimer(self)
        self._taskbar_auto_refresh_timer.setInterval(5000)
        self._taskbar_auto_refresh_timer.timeout.connect(self._refresh_taskbar_if_visible)
        if self._player_id and hasattr(self, 'match_player_input'):
            self.match_player_input.setText(self._player_id)
            # The startup login dialog persists the identity before LargeApp
            # exists; mirror it into the visible recent-query chips now.
            self._remember_recent_query(self._player_id)
        QTimer.singleShot(1800, self._start_update_check)

    @staticmethod
    def _load_player_id():
        value = QSettings('DEV King', '逐渐工具箱').value(GAME_ID_SETTINGS_KEY, '')
        return str(value or '').strip()

    def _save_player_identity(self, player_name, add_recent=True):
        """Persist the verified game ID and mirror it into query history."""
        normalized = str(player_name or '').strip()
        if not normalized:
            return
        self._player_id = normalized
        self.set_game_id(normalized)
        if hasattr(self, 'match_player_input'):
            self.match_player_input.setText(normalized)
        if hasattr(self, 'chat_account_input'):
            self.chat_account_input.setText(normalized)
        if hasattr(self, 'chat_account_hint'):
            self.chat_account_hint.setText(f'已验证 {normalized}，大厅聊天将使用此身份。')
        if add_recent:
            self._remember_recent_query(normalized)
        self._refresh_player_settings()

    def _open_player_login_dialog(self, initial_name=None):
        token = self._api_token or bugland_api.load_token()
        if initial_name is None and hasattr(self, 'chat_account_input'):
            initial_name = self.chat_account_input.text().strip()
        dialog = PlayerLoginDialog(
            token, initial_name or self._player_id, self.current_theme, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.player_name:
            self._save_player_identity(dialog.player_name, add_recent=True)

    def _refresh_player_settings(self):
        if not hasattr(self, 'player_id_value'):
            return
        if self._player_id:
            self.player_id_value.setText(self._player_id)
            self.player_id_status.setText('已验证并保存在当前 Windows 账户。')
        else:
            self.player_id_value.setText('尚未登录')
            self.player_id_status.setText('启动时会先验证游戏 ID。')

    def _animation_factor(self):
        return {'off': 0.0, 'fast': 0.55, 'standard': 1.0, 'slow': 1.65}.get(
            self.animation_speed, 1.0)

    def _animation_duration(self, base):
        factor = self._animation_factor()
        return 0 if factor == 0 else max(70, int(base * factor))

    def _animate_button(self, button, target):
        if self._animation_factor() == 0 or not button.isEnabled():
            return
        try:
            effect = self._button_effects.get(button)
            if effect is None:
                effect = QGraphicsOpacityEffect(button)
                effect.setOpacity(0.92)
                button.setGraphicsEffect(effect)
                self._button_effects[button] = effect
            previous = self._button_animations.get(button)
            if previous is not None:
                previous.stop()
            animation = QPropertyAnimation(effect, b'opacity', self)
            animation.setDuration(self._animation_duration(130))
            animation.setStartValue(effect.opacity())
            animation.setEndValue(float(target))
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            animation.finished.connect(lambda: self._button_animations.pop(button, None))
            self._button_animations[button] = animation
            animation.start()
        except RuntimeError:
            self._button_effects.pop(button, None)

    def _animate_widget(self, widget, base=220):
        if widget is None or not widget.isVisible() or self._animation_factor() == 0:
            return
        try:
            old_effect = widget.graphicsEffect()
            if old_effect is not None:
                active = next((entry for entry in self._ui_animations if entry[1] is widget), None)
                if active is None:
                    return
                active[0].stop()
                try:
                    widget.setGraphicsEffect(None)
                except RuntimeError:
                    return
                self._ui_animations.remove(active)
            effect = QGraphicsOpacityEffect(widget)
            effect.setOpacity(0.0)
            widget.setGraphicsEffect(effect)
            animation = QPropertyAnimation(effect, b'opacity', self)
            animation.setDuration(self._animation_duration(base))
            animation.setStartValue(0.0)
            animation.setEndValue(1.0)
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            entry = (animation, widget, effect)
            self._ui_animations.append(entry)

            def finish():
                try:
                    if widget.graphicsEffect() is effect:
                        widget.setGraphicsEffect(None)
                except RuntimeError:
                    pass
                if entry in self._ui_animations:
                    self._ui_animations.remove(entry)
                animation.deleteLater()

            animation.finished.connect(finish)
            animation.start()
        except RuntimeError:
            return

    def _stop_ui_animations(self):
        for animation, widget, effect in list(self._ui_animations):
            animation.stop()
            try:
                if widget.graphicsEffect() is effect:
                    widget.setGraphicsEffect(None)
            except RuntimeError:
                pass
            animation.deleteLater()
        self._ui_animations.clear()
        for animation in list(self._button_animations.values()):
            animation.stop()
            animation.deleteLater()
        self._button_animations.clear()
        for button, effect in list(self._button_effects.items()):
            try:
                if button.graphicsEffect() is effect:
                    button.setGraphicsEffect(None)
            except RuntimeError:
                pass
        self._button_effects.clear()

    def _set_animation_speed(self, value):
        value = str(value)
        if value not in {'off', 'fast', 'standard', 'slow'}:
            return
        self.animation_speed = value
        QSettings('DEV King', '逐渐工具箱').setValue('ui/animationSpeed', value)
        if value == 'off':
            self._stop_ui_animations()
        if hasattr(self, 'animation_speed_hint'):
            text = {'off': '已关闭过渡动画。', 'fast': '快速反馈，适合低功耗设备。',
                    'standard': '平衡反馈和流畅度。', 'slow': '更明显的过渡和层级变化。'}[value]
            self.animation_speed_hint.setText(text)

    def _build_ui(self):
        root = ThemeBackdrop(self.current_theme)
        self.backdrop = root
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(28, 24, 28, 22)
        root_layout.setSpacing(18)

        # 无边框窗口的自绘标题栏，沿用原助手的拖动和玻璃效果。
        title_bar = TitleBar()
        header = QHBoxLayout(title_bar)
        header.setContentsMargins(0, 0, 0, 0)
        title_copy = QVBoxLayout()
        title_copy.setSpacing(4)
        title_copy.addWidget(label('ZJ HUB', 'title'))
        title_copy.addWidget(label('DEV King', 'muted'))
        header.addLayout(title_copy)
        header.addStretch()
        header.addWidget(label('管理员模式' if optimizer.is_admin() else '普通模式', 'badge'))
        self.window_controls = []
        for icon_name, tip, action, role in [
            ('minimize', '最小化', self.showMinimized, 'normal'),
            ('maximize', '最大化 / 还原', self.toggle_maximized, 'normal'),
            ('close', '关闭', self.close, 'close'),
        ]:
            control = QPushButton()
            control.setObjectName('windowControl')
            control.setProperty('iconName', icon_name)
            control.setProperty('iconRole', 'window')
            control.setProperty('controlRole', role)
            control.setFixedSize(38, 34)
            control.setIcon(line_icon(icon_name, size=18, hovered='#ffffff'))
            control.setIconSize(QSize(18, 18))
            control.setCursor(Qt.CursorShape.PointingHandCursor)
            control.setToolTip(tip)
            control.setAccessibleName(tip)
            control.clicked.connect(action)
            header.addWidget(control)
            self.window_controls.append(control)
        for text_widget in title_bar.findChildren(QLabel):
            text_widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        root_layout.addWidget(title_bar)

        self.update_banner = self._build_update_banner()
        root_layout.addWidget(self.update_banner)

        workspace = QHBoxLayout()
        workspace.setSpacing(16)
        root_layout.addLayout(workspace, 1)

        sidebar = self._build_sidebar()
        workspace.addWidget(sidebar)

        self.page_stack = QStackedWidget()
        self.page_stack.setObjectName('pageStack')
        self.page_stack.setContentsMargins(0, 0, 0, 0)
        workspace.addWidget(self.page_stack, 1)

        self._section_page_indices = {}
        pages = [
            ('overview', 'home', '总览', '查看起床战争工具状态', self._build_dashboard()),
            ('optimizer', 'sparkles', '电脑清理', '扫描并清理临时文件', self._build_optimizer_page()),
            ('memory', 'chart', '战局数据', '查询最近对局与单局详情', self._build_match_data_page()),
            ('leaderboard', 'chart', '排行榜', '查询各个模式的日榜、周榜、年榜和总榜', self._build_leaderboard_page()),
            ('startup', 'command', '快捷工具', '用快捷键快速切换任务栏窗口', self._build_taskbar_switcher_page()),
            ('lobby-chat', 'chat', '大厅聊天', '连接起床战争公共大厅聊天', self._build_lobby_chat_page()),
        ]
        for key, icon, title, hint, page in pages:
            self._section_page_indices[key] = self.page_stack.addWidget(page)

        # 设置是覆盖在主界面之上的独立层，打开时会完全遮住下面的 GUI。
        self.settings_overlay = self._build_settings_overlay(root)

        self._build_footer(root_layout)

        # 默认打开总览；导航按钮会在这里与页面索引绑定。
        self._register_navigation(pages)
        self.settings_button.clicked.connect(lambda: self.switch_section('settings'))
        self._active_section = 'overview'
        self.switch_section('overview')

    def _build_update_banner(self):
        banner = QFrame()
        banner.setObjectName('updateBanner')
        banner_layout = QHBoxLayout(banner)
        banner_layout.setContentsMargins(14, 7, 8, 7)
        banner_layout.setSpacing(9)
        title = label('可更新', 'updateTitle')
        title.setObjectName('updateTitle')
        banner_layout.addWidget(title)
        hint = label('发现新的 ZJ HUB 版本。', 'updateHint')
        hint.setObjectName('updateHint')
        banner_layout.addWidget(hint, 1)
        open_button = QPushButton('打开 GitHub')
        open_button.setObjectName('updateOpen')
        open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        open_button.setAccessibleName('打开 GitHub 更新页面')
        open_button.clicked.connect(self._open_update_page)
        banner_layout.addWidget(open_button)
        dismiss = QPushButton('×')
        dismiss.setObjectName('updateDismiss')
        dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss.setAccessibleName('暂不提示更新')
        dismiss.setToolTip('暂不提示')
        dismiss.clicked.connect(banner.hide)
        banner_layout.addWidget(dismiss)
        banner._title_label = title
        banner._hint_label = hint
        banner._open_button = open_button
        banner.hide()
        return banner

    def _start_update_check(self):
        if self._closing or self._update_worker is not None:
            return
        worker = GitHubUpdateWorker(GITHUB_LATEST_RELEASE_API, self)
        worker.checked.connect(self._handle_update_result)
        worker.finished.connect(self._finish_update_check)
        self._update_worker = worker
        worker.start()

    def _finish_update_check(self):
        worker = self._update_worker
        self._update_worker = None
        if worker is not None:
            worker.deleteLater()

    def _handle_update_result(self, release):
        if self._closing or not isinstance(release, dict):
            return
        tag_name = str(release.get('tag_name') or release.get('name') or '').strip()
        release_url = str(release.get('html_url') or GITHUB_REPOSITORY_URL).strip()
        if not tag_name or not release_url or not _is_newer_release(tag_name):
            return
        self._update_release_tag = tag_name
        self._update_release_url = release_url
        if not hasattr(self, 'update_banner'):
            return
        self.update_banner._title_label.setText('可更新')
        self.update_banner._hint_label.setText(
            f'发现新版本 {tag_name}，当前版本 {APP_VERSION}。')
        self.update_banner.show()
        self.update_banner.raise_()
        self._animate_widget(self.update_banner, 180)

    def _open_update_page(self):
        url = self._update_release_url or GITHUB_REPOSITORY_URL
        QDesktopServices.openUrl(QUrl(url))

    def _build_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName('sidebar')
        sidebar.setMinimumWidth(218)
        sidebar.setMaximumWidth(244)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.setSpacing(10)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(0, 0, 0, 0)
        brand_row.setSpacing(10)
        mark = label('ZJ', 'brandMark')
        mark.setFixedSize(34, 34)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_row.addWidget(mark)
        brand_copy = QVBoxLayout()
        brand_copy.setSpacing(2)
        brand_copy.addWidget(label('ZJ HUB', 'brand'))
        brand_copy.addWidget(label('DEV King', 'brandHint'))
        brand_row.addLayout(brand_copy)
        layout.addLayout(brand_row)
        line = QFrame()
        line.setObjectName('navLine')
        line.setFixedHeight(1)
        layout.addWidget(line)
        layout.addSpacing(5)

        self.nav_layout = QVBoxLayout()
        self.nav_layout.setContentsMargins(0, 0, 0, 0)
        self.nav_layout.setSpacing(6)
        layout.addLayout(self.nav_layout)
        layout.addStretch(1)

        self.settings_button = QPushButton('设置')
        self.settings_button.setObjectName('settingsButton')
        self.settings_button.setCheckable(True)
        self.settings_button.setProperty('iconName', 'settings')
        self.settings_button.setProperty('iconRole', 'navigation')
        self.settings_button.setIcon(line_icon('settings'))
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.setAccessibleName('设置')
        self.settings_button.setToolTip('主题与其他偏好设置')
        layout.addWidget(self.settings_button)
        return sidebar

    def _register_navigation(self, pages):
        self.nav_buttons = {}
        for key, icon, title, hint, _page in pages:
            icon_name = {
                'overview': 'home',
                'optimizer': 'sparkles',
                'memory': 'chart',
                'leaderboard': 'chart',
                'startup': 'command',
                'lobby-chat': 'chat',
            }.get(key, 'command')
            button = QPushButton(title)
            button.setObjectName('navButton')
            button.setCheckable(True)
            button.setProperty('iconName', icon_name)
            button.setProperty('iconRole', 'navigation')
            button.setIcon(line_icon(icon_name))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(hint)
            button.setAccessibleName(title)
            button.clicked.connect(lambda checked=False, section=key: self.switch_section(section))
            self.nav_layout.addWidget(button)
            self.nav_buttons[key] = button

    def switch_section(self, key):
        if key == 'settings':
            self.show_settings_overlay()
            return
        index = self._section_page_indices.get(key)
        if index is None:
            return
        if hasattr(self, 'settings_overlay') and self.settings_overlay.isVisible():
            self.settings_overlay.hide()
        self._active_section = key
        self.page_stack.setCurrentIndex(index)
        # TaskbarSwitcher 页面包含原生 QTableWidget viewport。对整页施加
        # QGraphicsOpacityEffect 时，Windows 合成器会在首帧把 viewport
        # 画成黑色镂空条；页面本身保持稳定，内部控件仍可正常交互。
        if key != 'startup':
            self._animate_widget(self.page_stack.currentWidget(), 210)
        for name, button in self.nav_buttons.items():
            button.setChecked(name == key)
        if hasattr(self, 'settings_button'):
            self.settings_button.setChecked(False)
        if key == 'optimizer':
            self.refresh()
        if hasattr(self, '_taskbar_auto_refresh_timer'):
            if key == 'startup':
                self._refresh_taskbar_apps()
                self._taskbar_auto_refresh_timer.start()
            else:
                self._taskbar_auto_refresh_timer.stop()
        if hasattr(self, '_chat_poll_timer') and self._chat_poll_timer is not None:
            if key == 'lobby-chat':
                self._refresh_chat_identity()
                self._chat_poll_timer.start()
                self._poll_lobby_chat()
            else:
                self._chat_poll_timer.stop()

    def _build_dashboard(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(14)

        heading = QVBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(label('BEDWARS TOOL CENTER', 'eyebrow'))
        heading.addWidget(label('总览', 'pageTitle'))
        heading.addWidget(label('查看起床战争工具状态，选择对应功能开始使用。', 'pageHint'))
        layout.addLayout(heading)

        hero = QFrame()
        hero.setObjectName('hero')
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(22, 20, 22, 20)
        hero_layout.setSpacing(18)
        hero_copy = QVBoxLayout()
        hero_copy.setSpacing(7)
        hero_copy.addWidget(label('READY FOR NEXT MATCH', 'eyebrow'))
        hero_copy.addWidget(label('准备开始下一局', 'heroTitle'))
        hero_copy.addWidget(label('从一个入口管理起床战争的运行环境和常用工具。', 'muted'))
        hero_copy.addWidget(label('ZJ HUB 会持续加入战绩、快捷切换和大厅聊天功能。', 'muted'))
        hero_layout.addWidget(icon_tile('shield', 'heroIconTile', '#cbe6ff', 28, 56))
        hero_layout.addLayout(hero_copy, 1)
        open_optimizer = self.button('打开电脑清理', lambda: self.switch_section('optimizer'), True)
        open_optimizer.setProperty('themeIconName', 'sparkles')
        open_optimizer.setIcon(line_icon(
            'sparkles', normal=primary_icon_color(self.current_theme),
            active=primary_icon_color(self.current_theme), size=18))
        open_optimizer.setIconSize(QSize(18, 18))
        hero_layout.addWidget(open_optimizer)
        layout.addWidget(hero)

        metrics = QHBoxLayout()
        metrics.setSpacing(12)
        self.dashboard_memory = self._metric_card(metrics, '运行内存', '—', 'chart')
        self.dashboard_cpu = self._metric_card(metrics, '运行负载', '读取中', 'sparkles')
        self.dashboard_uptime = self._metric_card(metrics, '本机运行', '—', 'command', True)
        self.dashboard_junk = self._metric_card(metrics, '可释放空间', '尚未扫描', 'settings')
        layout.addLayout(metrics)

        lower = QHBoxLayout()
        lower.setSpacing(14)
        actions, actions_layout = panel()
        actions.setObjectName('featureCard')
        self._section_header(actions_layout, '快捷操作', 'command')
        actions_layout.addWidget(label('常用起床战争工具会随着功能分区逐步加入。', 'muted'))
        actions_layout.addSpacing(4)
        scan_button = self.button('扫描可释放空间', self._dashboard_scan, True)
        scan_button.setProperty('themeIconName', 'chart')
        scan_button.setIcon(line_icon(
            'chart', normal=primary_icon_color(self.current_theme),
            active=primary_icon_color(self.current_theme), size=17))
        scan_button.setIconSize(QSize(17, 17))
        actions_layout.addWidget(scan_button)
        free_button = self.button('释放运行内存', self.free_memory)
        free_button.setIcon(line_icon('sparkles', size=17))
        free_button.setIconSize(QSize(17, 17))
        actions_layout.addWidget(free_button)
        self._feature_row(actions_layout, '运行环境', '为下一局整理缓存和临时文件。', 'shield')
        self._feature_row(actions_layout, 'ZJ HUB 工具', '战绩、快捷切换和大厅聊天模块持续接入。', 'sparkles')
        actions_layout.addStretch()
        lower.addWidget(actions, 1)

        status, status_layout = panel()
        status.setObjectName('featureCard')
        self._section_header(status_layout, '当前状态', 'check')
        status_line = QHBoxLayout()
        status_line.setSpacing(8)
        status_line.addWidget(icon_tile('check', 'statusGlyph', '#b8f2e2', 15, 24))
        self.dashboard_state = label('就绪 · ZJ HUB 在本机运行', 'muted')
        self.dashboard_state.setWordWrap(True)
        status_line.addWidget(self.dashboard_state, 1)
        status_layout.addLayout(status_line)
        status_layout.addSpacing(5)
        status_layout.addWidget(label('后续可以继续接入战绩、快捷切换、大厅聊天和地图等工具。', 'muted'))
        self._feature_row(status_layout, '玻璃界面', '主题和布局可以在设置中调整。', 'sparkles')
        self._feature_row(status_layout, '本地运行', '工具操作不依赖云端，启动更轻。', 'network')
        status_layout.addStretch()
        lower.addWidget(status, 1)
        layout.addLayout(lower, 1)
        return page

    def _feature_row(self, parent_layout, title, description, mark='●'):
        row = QFrame()
        row.setObjectName('featureRow')
        row.setMinimumHeight(42)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(10, 6, 10, 6)
        row_layout.setSpacing(9)
        row_layout.addWidget(icon_tile(mark, 'rowGlyph', '#bfe1ff', 15, 28))
        copy = QVBoxLayout()
        copy.setSpacing(1)
        copy.addWidget(label(title, 'cardTitle'))
        hint = label(description, 'cardHint')
        hint.setWordWrap(True)
        copy.addWidget(hint)
        row_layout.addLayout(copy, 1)
        parent_layout.addWidget(row)

    def _section_header(self, parent_layout, title, icon_name='command'):
        """统一面板标题的图标、字号和间距，避免每个分区各用一套符号。"""
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(icon_tile(icon_name, 'sectionIcon', '#bfe1ff', 15, 28))
        header.addWidget(label(title, 'section'))
        header.addStretch(1)
        parent_layout.addLayout(header)

    def _metric_card(self, parent_layout, title, value, icon_name='command', compact=False):
        card = QFrame()
        card.setObjectName('metricCard')
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(15, 13, 15, 13)
        card_layout.setSpacing(4)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        value_label = label(value, 'metricSmallCompact' if compact else 'metricSmall')
        top.addWidget(value_label)
        top.addStretch(1)
        icon_label = QLabel()
        icon_label.setObjectName('metricIcon')
        icon_label.setFixedSize(30, 30)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setPixmap(_render_svg_icon(icon_name, '#a9d4ff', 20))
        icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        top.addWidget(icon_label)
        icon_label.setProperty('iconName', icon_name)
        self._metric_icon_labels.append(icon_label)
        card_layout.addLayout(top)
        card_layout.addWidget(label(title, 'metricLabel'))
        parent_layout.addWidget(card, 1)
        return value_label

    def _dashboard_scan(self):
        self.switch_section('optimizer')
        self.scan()

    def _build_optimizer_page(self):
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(4, 0, 4, 0)
        page_layout.setSpacing(14)

        heading = QVBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(label('MATCH PREP / SYSTEM TUNING', 'eyebrow'))
        heading.addWidget(label('电脑清理', 'pageTitle'))
        heading.addWidget(label('为起床战争整理运行环境，减少卡顿，准备开始战斗。', 'pageHint'))
        page_layout.addLayout(heading)

        body = QHBoxLayout()
        body.setSpacing(16)
        page_layout.addLayout(body, 1)

        left = QVBoxLayout()
        left.setSpacing(14)
        body.addLayout(left, 3)

        status, status_layout = panel()
        left.addWidget(status)
        self._section_header(status_layout, '运行概览', 'chart')
        status_layout.addWidget(label('运行内存', 'muted'))
        self.memory = label('—', 'metric')
        status_layout.addWidget(self.memory)
        self.memory_bar = self._progress_bar()
        status_layout.addWidget(self.memory_bar)
        self.memory_hint = label('读取中', 'muted')
        status_layout.addWidget(self.memory_hint)
        status_layout.addSpacing(8)
        status_layout.addWidget(label('连续运行', 'muted'))
        self.uptime = label('—', 'section')
        status_layout.addWidget(self.uptime)
        self.memory_button = self.button('释放运行内存', self.free_memory)
        self.memory_button.setIcon(line_icon('sparkles', size=17))
        self.memory_button.setIconSize(QSize(17, 17))
        self.memory_button.setToolTip('压缩工作集不等于解决内存泄漏，应用再次访问页面时可能暂时变慢。')
        status_layout.addWidget(self.memory_button)

        activity, activity_layout = panel()
        left.addWidget(activity, 1)
        self._section_header(activity_layout, '操作记录', 'command')
        from PySide6.QtWidgets import QPlainTextEdit
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setUndoRedoEnabled(False)
        self.log_text.document().setMaximumBlockCount(150)
        self.log_text.setMinimumHeight(65)
        activity_layout.addWidget(self.log_text, 1)
        self.log('准备就绪。扫描只统计文件，不会删除内容。')

        cleanup, clean_layout = panel()
        body.addWidget(cleanup, 6)
        summary = QHBoxLayout()
        summary.addWidget(icon_tile('shield', 'sectionIcon', '#bfe1ff', 15, 28))
        summary.addWidget(label('运行环境整理', 'section'))
        summary.addStretch()
        self.scan_button = self.button('扫描文件', self.scan)
        self.scan_button.setIcon(line_icon('chart', size=17))
        self.scan_button.setIconSize(QSize(17, 17))
        summary.addWidget(self.scan_button)
        clean_layout.addLayout(summary)
        self.junk = label('先扫描，了解可以整理的内容', 'muted')
        self.junk.setWordWrap(True)
        clean_layout.addWidget(self.junk)
        self.scan_progress = self._progress_bar()
        self.scan_progress.setRange(0, len(self.targets))
        self.scan_progress.hide()
        clean_layout.addWidget(self.scan_progress)

        from PySide6.QtWidgets import QCheckBox, QScrollArea
        scroll = QScrollArea()
        self.scroll_area = scroll
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rows = QWidget()
        rows.setObjectName('cleanRows')
        rows.setStyleSheet('QWidget#cleanRows { background: transparent; }')
        rows_layout = QVBoxLayout(rows)
        rows_layout.setContentsMargins(0, 0, 4, 0)
        rows_layout.setSpacing(0)
        for target in self.targets:
            row = QFrame()
            row.setObjectName('row')
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 10, 0, 10)
            checkbox = QCheckBox(target.name)
            checkbox.setChecked(target.default_on)
            checkbox.toggled.connect(self.update_selection)
            checkbox.setAccessibleName(target.name)
            checkbox.setToolTip(target.description)
            copy = QVBoxLayout()
            copy.setSpacing(3)
            copy.addWidget(checkbox)
            hint = label(target.description, 'muted')
            hint.setWordWrap(True)
            hint.setContentsMargins(29, 0, 0, 0)
            hint.setStyleSheet('font-size: 11px;')
            copy.addWidget(hint)
            row_layout.addLayout(copy, 1)
            size = label('未扫描', 'muted')
            size.setMinimumWidth(76)
            size.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row_layout.addWidget(size)
            rows_layout.addWidget(row)
            self.checks[target.key], self.sizes[target.key] = checkbox, size
        rows_layout.addStretch()
        scroll.setWidget(rows)
        clean_layout.addWidget(scroll, 1)

        actions = QHBoxLayout()
        reset_button = self.button('重置选择', self.reset_selection)
        reset_button.setIcon(line_icon('close', size=17))
        reset_button.setIconSize(QSize(17, 17))
        actions.addWidget(reset_button)
        actions.addStretch()
        self.optimize_button = self.button('一键优化', self.optimize, True)
        self.optimize_button.setProperty('themeIconName', 'check')
        self.optimize_button.setIcon(line_icon(
            'check', normal=primary_icon_color(self.current_theme),
            active=primary_icon_color(self.current_theme), size=17))
        self.optimize_button.setIconSize(QSize(17, 17))
        actions.addWidget(self.optimize_button)
        clean_layout.addLayout(actions)
        return page

    def _build_leaderboard_page(self):
        """排行榜查询页：按玩法、指标和周期请求 BuGLand /leaderboard。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(14)

        heading = QVBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(label('BEDWARS / LEADERBOARD', 'eyebrow'))
        heading.addWidget(label('排行榜', 'pageTitle'))
        heading.addWidget(label('选择玩法、榜单指标和周期，查看布吉岛公开榜单。', 'pageHint'))
        layout.addLayout(heading)

        query_card, query_layout = panel()
        query_card.setObjectName('leaderboardHero')
        query_layout.setContentsMargins(18, 15, 18, 15)
        query_layout.setSpacing(10)
        query_top = QHBoxLayout()
        query_top.setSpacing(8)
        query_top.addWidget(icon_tile('chart', 'sectionIcon', '#bfe1ff', 15, 28))
        query_top.addWidget(label('榜单筛选', 'cardTitle'))
        query_top.addStretch(1)
        self.leaderboard_api_state = label('默认 API Token 已就绪。', 'cardHint')
        query_top.addWidget(self.leaderboard_api_state)
        query_layout.addLayout(query_top)

        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.leaderboard_mode_combo = ThemedComboBox()
        self.leaderboard_mode_combo.setObjectName('leaderboardFilter')
        self.leaderboard_mode_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.leaderboard_mode_combo.setMinimumWidth(190)
        for title, prefix in [
            ('全部模式', 'bw'),
            ('经验 4 队 8 人', 'bwxp32'),
            ('4 队 4 人', 'bw16'),
            ('4 队 3 人', 'bw12'),
            ('双人', 'bw8'),
            ('单人', 'bw1'),
            ('无限火力', 'bw999'),
        ]:
            self.leaderboard_mode_combo.addItem(title, prefix)
        filters.addWidget(self.leaderboard_mode_combo, 1)
        self.leaderboard_metric_combo = ThemedComboBox()
        self.leaderboard_metric_combo.setObjectName('leaderboardFilter')
        self.leaderboard_metric_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.leaderboard_metric_combo.setMinimumWidth(160)
        for title, suffix in [('最终击杀', 'final_kills'), ('胜利', 'win'), ('拆床', 'destory_bed')]:
            self.leaderboard_metric_combo.addItem(title, suffix)
        filters.addWidget(self.leaderboard_metric_combo, 1)
        self.leaderboard_period_combo = ThemedComboBox()
        self.leaderboard_period_combo.setObjectName('leaderboardFilter')
        self.leaderboard_period_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.leaderboard_period_combo.setMinimumWidth(126)
        for title, suffix in [('日榜', 'daily'), ('周榜', 'weekly'), ('年榜', 'yearly'), ('总榜', 'all')]:
            self.leaderboard_period_combo.addItem(title, suffix)
        filters.addWidget(self.leaderboard_period_combo, 1)
        self.leaderboard_platform_combo = ThemedComboBox()
        self.leaderboard_platform_combo.setObjectName('leaderboardFilter')
        self.leaderboard_platform_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.leaderboard_platform_combo.setMinimumWidth(104)
        self.leaderboard_platform_combo.addItem('端游', 'java')
        self.leaderboard_platform_combo.addItem('手游', 'bedrock')
        filters.addWidget(self.leaderboard_platform_combo, 1)
        self.leaderboard_query_button = self.button('查询排行榜', self.query_leaderboard, True)
        self.leaderboard_query_button.setMinimumWidth(120)
        self.leaderboard_query_button.setProperty('themeIconName', 'chart')
        self.leaderboard_query_button.setIcon(line_icon(
            'chart', normal=primary_icon_color(self.current_theme),
            active=primary_icon_color(self.current_theme), size=17))
        self.leaderboard_query_button.setIconSize(QSize(17, 17))
        filters.addWidget(self.leaderboard_query_button)
        query_layout.addLayout(filters)
        hint = label('榜单周期由官方接口结算；年榜使用当前年度榜单标识。', 'cardHint')
        hint.setWordWrap(True)
        query_layout.addWidget(hint)
        layout.addWidget(query_card)

        result_card, result_layout = panel()
        result_card.setObjectName('leaderboardCard')
        result_layout.setContentsMargins(18, 15, 18, 15)
        result_layout.setSpacing(9)
        result_header = QHBoxLayout()
        result_header.setSpacing(8)
        result_header.addWidget(icon_tile('layers', 'sectionIcon', '#bfe1ff', 15, 28))
        self.leaderboard_result_title = label('排行榜结果', 'section')
        result_header.addWidget(self.leaderboard_result_title)
        result_header.addStretch(1)
        self.leaderboard_result_meta = label('尚未查询', 'cardHint')
        result_header.addWidget(self.leaderboard_result_meta)
        result_layout.addLayout(result_header)
        self.leaderboard_status = label('选择条件后点击“查询排行榜”。', 'muted')
        self.leaderboard_status.setWordWrap(True)
        result_layout.addWidget(self.leaderboard_status)

        self.leaderboard_scroll = QScrollArea()
        self.leaderboard_scroll.setWidgetResizable(True)
        self.leaderboard_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.leaderboard_rows_widget = QWidget()
        self.leaderboard_rows_widget.setObjectName('leaderboardRows')
        self.leaderboard_rows_widget.setStyleSheet('QWidget#leaderboardRows { background: transparent; }')
        self.leaderboard_rows_layout = QVBoxLayout(self.leaderboard_rows_widget)
        self.leaderboard_rows_layout.setContentsMargins(0, 0, 4, 0)
        self.leaderboard_rows_layout.setSpacing(7)
        self.leaderboard_rows_layout.addStretch(1)
        self.leaderboard_scroll.setWidget(self.leaderboard_rows_widget)
        result_layout.addWidget(self.leaderboard_scroll, 1)
        layout.addWidget(result_card, 1)
        return page

    def _leaderboard_request_key(self):
        mode = self.leaderboard_mode_combo.currentData() or 'bw'
        metric = self.leaderboard_metric_combo.currentData() or 'final_kills'
        # The official legacy enum keeps the all-mode final-kills key as `fk`;
        # mode-specific boards use the explicit `final_kills` segment.
        if mode == 'bw' and metric == 'final_kills':
            metric = 'fk'
        period = self.leaderboard_period_combo.currentData() or 'daily'
        platform = self.leaderboard_platform_combo.currentData() or 'java'
        return f'{mode}_{metric}_{period}_{platform}'

    def query_leaderboard(self):
        if not self._api_token:
            self.leaderboard_status.setText('尚未设置 API Token，正在打开 API 令牌设置。')
            self._open_api_settings()
            return
        key = self._leaderboard_request_key()
        self._leaderboard_pending_key = key
        self._leaderboard_last_key = key
        self.leaderboard_status.setText('正在读取排行榜……')
        self.leaderboard_result_meta.setText('请求中')
        self._render_leaderboard_rows([])
        cached = self._leaderboard_cache.get(key)
        if cached is not None:
            self._render_leaderboard_result(key, cached, from_cache=True)
            return
        self._start_api_request('leaderboard', bugland_api.LEADERBOARD_ENDPOINT,
                                {'lb_type': key}, priority=True)

    @staticmethod
    def _leaderboard_entries(result):
        payload = result
        if isinstance(payload, dict):
            for key in ('data', 'result', 'leaderboard', 'rows', 'items'):
                candidate = payload.get(key)
                if isinstance(candidate, (list, tuple)):
                    payload = candidate
                    break
                if isinstance(candidate, dict):
                    for nested in ('data', 'result', 'rows', 'items'):
                        values = candidate.get(nested)
                        if isinstance(values, (list, tuple)):
                            payload = values
                            break
                    if isinstance(payload, (list, tuple)):
                        break
        if not isinstance(payload, (list, tuple)):
            return []
        entries = []
        for index, item in enumerate(payload, 1):
            if isinstance(item, dict):
                rank = item.get('top', item.get('rank', item.get('ranking', index)))
                name = item.get('name', item.get('player_name', item.get('username', item.get('player', '未知玩家'))))
                score = item.get('score', item.get('points', item.get('value', item.get('count', '—'))))
                title = item.get('cn_type') or item.get('title') or item.get('type') or ''
            elif isinstance(item, (list, tuple)):
                rank, name, score, title = (list(item) + [None] * 4)[:4]
            else:
                rank, name, score, title = index, str(item), '—', ''
            if isinstance(name, dict):
                name = name.get('name') or name.get('username') or name.get('player_name') or '未知玩家'
            entries.append({'rank': str(rank or index), 'name': str(name or '未知玩家'),
                            'score': str(score if score is not None else '—'), 'title': str(title or '')})
        return entries

    def _render_leaderboard_result(self, key, result, from_cache=False):
        entries = self._leaderboard_entries(result)
        labels = {
            'daily': '日榜', 'weekly': '周榜', 'yearly': '年榜', 'all': '总榜',
        }
        period = key.split('_')[-2] if key.count('_') >= 3 else ''
        self.leaderboard_result_title.setText(f'排行榜 · {labels.get(period, period)}')
        self.leaderboard_result_meta.setText(f'{len(entries)} 条' + (' · 缓存' if from_cache else ''))
        if not entries:
            self.leaderboard_status.setText('接口没有返回可展示的榜单条目，请切换条件后重试。')
            self._render_leaderboard_rows([])
            return
        self.leaderboard_status.setText('榜单数据已更新，排名仅供参考。')
        self._render_leaderboard_rows(entries)

    def _render_leaderboard_rows(self, entries):
        rows_widget = getattr(self, 'leaderboard_rows_widget', None)
        if rows_widget is not None:
            rows_widget.setUpdatesEnabled(False)
        try:
            while self.leaderboard_rows_layout.count():
                item = self.leaderboard_rows_layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
            for index, entry in enumerate(entries[:100]):
                row = QFrame()
                row.setObjectName('leaderboardRow')
                # 将官方名次归一为五档视觉层级：1/2/3 使用金银铜高光，
                # 4 与 5+ 逐渐降低对比度，确保榜单从上往下自然收束。
                try:
                    rank_number = int(float(str(entry.get('rank') or index + 1).strip()))
                except (TypeError, ValueError):
                    rank_number = index + 1
                rank_level = str(max(1, min(rank_number, 5)))
                row.setProperty('rankLevel', rank_level)
                # 动态属性写入后立即刷新 QSS，确保高亮层级在首次展示时就生效。
                row.style().unpolish(row)
                row.style().polish(row)
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(12, 9, 14, 9)
                row_layout.setSpacing(12)
                rank = label(str(entry.get('rank') or index + 1), 'leaderboardRank')
                rank.setFixedWidth(44)
                rank.setAlignment(Qt.AlignmentFlag.AlignCenter)
                row_layout.addWidget(rank)
                copy = QVBoxLayout()
                copy.setSpacing(2)
                name = label(entry.get('name') or '未知玩家', 'leaderboardName')
                copy.addWidget(name)
                meta = entry.get('title') or '布吉岛排行榜'
                copy.addWidget(label(meta, 'leaderboardMeta'))
                row_layout.addLayout(copy, 1)
                score = label(entry.get('score') or '—', 'leaderboardScore')
                score.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                score.setMinimumWidth(86)
                row_layout.addWidget(score)
                self.leaderboard_rows_layout.addWidget(row)
            self.leaderboard_rows_layout.addStretch(1)
        finally:
            if rows_widget is not None:
                rows_widget.setUpdatesEnabled(True)
                rows_widget.update()

    def _build_match_data_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(14)

        heading_host = QWidget()
        heading_host.setObjectName('matchHeading')
        heading = QVBoxLayout(heading_host)
        heading.setSpacing(4)
        heading.addWidget(label('BEDWARS / MATCH HISTORY', 'eyebrow'))
        heading.addWidget(label('对局数据', 'pageTitle'))
        layout.addWidget(heading_host)
        self.match_heading_host = heading_host

        search_card, search_layout = panel()
        search_card.setObjectName('featureCard')
        search_layout.setContentsMargins(18, 13, 18, 13)
        search_layout.setSpacing(7)
        query_header = QHBoxLayout()
        query_header.addWidget(icon_tile('chart', 'sectionIcon', '#bfe1ff', 14, 24))
        query_header.addWidget(label('查询玩家', 'cardTitle'))
        query_header.addStretch(1)
        self.match_api_state = label('', 'cardHint')
        query_header.addWidget(self.match_api_state)
        search_layout.addLayout(query_header)
        query_row = QHBoxLayout()
        query_row.setSpacing(10)
        self.match_player_input = QLineEdit()
        self.match_player_input.setObjectName('themedInput')
        self.match_player_input.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.match_player_input.setAutoFillBackground(False)
        self.match_player_input.setPlaceholderText('玩家昵称，例如 Steve')
        self.match_player_input.setClearButtonEnabled(True)
        self.match_player_input.setMaxLength(64)
        self.match_player_input.setMinimumHeight(44)
        self.match_player_input.returnPressed.connect(lambda: self.query_match_history(True))
        query_row.addWidget(self.match_player_input, 1)
        self.match_query_button = self.button('查询对局', lambda: self.query_match_history(True), True)
        self.match_query_button.setProperty('themeIconName', 'chart')
        self.match_query_button.setIcon(line_icon(
            'chart', normal=primary_icon_color(self.current_theme),
            active=primary_icon_color(self.current_theme), size=17))
        self.match_query_button.setIconSize(QSize(17, 17))
        query_row.addWidget(self.match_query_button)
        self.match_token_button = self.button('API 令牌设置', self._open_api_settings)
        query_row.addWidget(self.match_token_button)
        search_layout.addLayout(query_row)
        recent_row = QHBoxLayout()
        recent_row.setSpacing(7)
        recent_row.addWidget(label('最近查询', 'cardHint'))
        self.recent_queries_empty = label('暂无记录', 'cardHint')
        recent_row.addWidget(self.recent_queries_empty)
        recent_grid_host = QWidget()
        recent_grid = QGridLayout(recent_grid_host)
        recent_grid.setContentsMargins(0, 0, 0, 0)
        recent_grid.setHorizontalSpacing(6)
        recent_grid.setVerticalSpacing(5)
        self.recent_queries_grid = recent_grid
        self.recent_query_buttons = []
        for index in range(5):
            recent_button = QPushButton()
            recent_button.setObjectName('recentQueryButton')
            recent_button.setCursor(Qt.CursorShape.PointingHandCursor)
            recent_button.setMinimumHeight(34)
            recent_button.setMinimumWidth(82)
            recent_button.setMaximumWidth(170)
            recent_button.setVisible(False)
            recent_button.clicked.connect(
                lambda checked=False, position=index: self._query_recent_player(position))
            recent_grid.addWidget(recent_button, index // 3, index % 3)
            self.recent_query_buttons.append(recent_button)
        recent_row.addWidget(recent_grid_host, 1)
        search_layout.addLayout(recent_row)
        self._refresh_match_api_state()
        self._refresh_recent_query_buttons()
        layout.addWidget(search_card)
        self.match_search_card = search_card

        result_panel, result_layout = panel()
        result_panel.setObjectName('glass')
        # Keep the history card compact enough to show five or more rows on
        # the default window while preserving readable spacing.
        result_layout.setContentsMargins(14, 10, 14, 10)
        result_layout.setSpacing(6)
        result_panel.setMinimumWidth(330)
        result_header = QHBoxLayout()
        result_header.setSpacing(6)
        result_header.addWidget(icon_tile('layers', 'sectionIcon', '#bfe1ff', 15, 28))
        result_header.addWidget(label('最近对局', 'section'))
        result_header.addStretch(1)
        self.match_previous_button = self.button('‹', lambda: self._change_match_page(-1))
        self.match_previous_button.setFixedWidth(38)
        self.match_previous_button.setToolTip('上一页')
        self.match_previous_button.setEnabled(False)
        self.match_previous_button.setVisible(False)
        result_header.addWidget(self.match_previous_button)
        self.match_page_label = label('第 1 页', 'muted')
        self.match_page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.match_page_label.setMinimumWidth(46)
        self.match_page_label.setVisible(False)
        result_header.addWidget(self.match_page_label)
        self.match_next_button = self.button('›', lambda: self._change_match_page(1))
        self.match_next_button.setFixedWidth(38)
        self.match_next_button.setToolTip('下一页')
        self.match_next_button.setEnabled(False)
        self.match_next_button.setVisible(False)
        result_header.addWidget(self.match_next_button)
        result_layout.addLayout(result_header)
        self.match_status = label('输入玩家昵称后开始查询。双击对局查看详情。', 'muted')
        self.match_status.setWordWrap(True)
        result_layout.addWidget(self.match_status)

        from PySide6.QtWidgets import QScrollArea
        self.match_scroll = QScrollArea()
        self.match_scroll.setWidgetResizable(True)
        self.match_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # The default Qt wheel step is deliberately conservative.  A match
        # card is compact, so a larger step keeps the list responsive without
        # making it jump an entire page at a time.
        # One wheel notch should clear roughly one compact card.  This keeps
        # the history page responsive on high-resolution mouse wheels while
        # leaving the page step large enough for keyboard navigation.
        self.match_scroll.verticalScrollBar().setSingleStep(56)
        self.match_scroll.verticalScrollBar().setPageStep(260)
        # A wider handle gives the history list a usable drag target.  The
        # list itself remains theme styled through the global QSS.
        self.match_scroll.verticalScrollBar().setMinimumWidth(12)
        self.match_scroll.verticalScrollBar().valueChanged.connect(
            lambda _value: self._maybe_load_next_match_page())
        self.match_scroll.verticalScrollBar().rangeChanged.connect(
            lambda _minimum, _maximum: self._maybe_load_next_match_page())
        self.match_rows_widget = QWidget()
        self.match_rows_widget.setObjectName('matchRows')
        self.match_rows_widget.setStyleSheet('QWidget#matchRows { background: transparent; }')
        self.match_rows_layout = QVBoxLayout(self.match_rows_widget)
        self.match_rows_layout.setContentsMargins(0, 0, 4, 0)
        self.match_rows_layout.setSpacing(5)
        self.match_pull_hint = QLabel('')
        self.match_pull_hint.setObjectName('matchPullHint')
        self.match_pull_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.match_pull_hint.setMinimumHeight(0)
        self.match_pull_hint.setMaximumHeight(0)
        self.match_pull_hint.setVisible(False)
        self.match_pull_hint_effect = QGraphicsOpacityEffect(self.match_pull_hint)
        self.match_pull_hint_effect.setOpacity(0.0)
        self.match_pull_hint.setGraphicsEffect(self.match_pull_hint_effect)
        self._match_pull_animation = None
        self._match_pull_fade_animation = None
        self._match_pull_collapse_animation = None
        self.match_scroll.viewport().installEventFilter(self)
        self.match_scroll.verticalScrollBar().installEventFilter(self)
        self.match_rows_layout.addWidget(self.match_pull_hint)
        self.match_rows_layout.addStretch(1)
        self.match_scroll.setWidget(self.match_rows_widget)
        result_layout.addWidget(self.match_scroll, 1)

        daily_summary = QFrame()
        daily_summary.setObjectName('dailySummary')
        daily_summary_layout = QVBoxLayout(daily_summary)
        daily_summary_layout.setContentsMargins(0, 2, 0, 0)
        daily_summary_layout.setSpacing(4)
        daily_header = QHBoxLayout()
        daily_header.addWidget(label('今日汇总', 'cardTitle'))
        daily_header.addStretch(1)
        self.daily_summary_hint = label('查询后自动汇总', 'cardHint')
        daily_header.addWidget(self.daily_summary_hint)
        daily_summary_layout.addLayout(daily_header)
        daily_stats_row = QHBoxLayout()
        daily_stats_row.setSpacing(7)
        self.daily_stat_values = {}
        for key, title in (
                ('win_rate', '胜率'), ('final_kills', '最终击杀'),
                ('beds', '最终拆床'), ('final_deaths', '最终死亡')):
            chip = QFrame()
            chip.setObjectName('dailyStatChip')
            chip.setMinimumHeight(42)
            chip_layout = QVBoxLayout(chip)
            chip_layout.setContentsMargins(8, 4, 8, 4)
            chip_layout.setSpacing(2)
            chip_layout.addWidget(label(title, 'statLabel'))
            value_label = label('—', 'dailyStatValue')
            value_label.setProperty('semantic', 'neutral')
            chip_layout.addWidget(value_label)
            daily_stats_row.addWidget(chip, 1)
            self.daily_stat_values[key] = value_label
        daily_summary_layout.addLayout(daily_stats_row)
        result_layout.addWidget(daily_summary)

        layout.addWidget(result_panel, 1)
        self.match_result_panel = result_panel
        self.match_daily_summary = daily_summary

        # Details stay inside the selected page so the main GUI does not
        # spawn a separate window.  The panel is populated lazily on demand.
        self.match_detail_panel = QFrame(page)
        self.match_detail_panel.setObjectName('matchDetailPanel')
        self.match_detail_panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.match_detail_panel.setVisible(False)
        layout.addWidget(self.match_detail_panel, 1)
        self.match_detail_panel_layout = QVBoxLayout(self.match_detail_panel)
        self.match_detail_panel_layout.setContentsMargins(0, 0, 0, 0)
        self.match_detail_panel_layout.setSpacing(8)
        self._match_records = []
        self._match_record_widgets = []
        self._selected_match_row = None
        self._match_detail_record = None
        self._detail_animations = []
        self.match_detail_dialog = None
        self._api_pump_timer = QTimer(self)
        self._api_pump_timer.setSingleShot(True)
        self._api_pump_timer.timeout.connect(self._pump_api_queue)
        return page

    def _ensure_detail_dialog(self):
        if self.match_detail_dialog is not None and hasattr(self, 'match_detail_layout'):
            return
        # Keep details in the match page.  This used to be a separate QDialog,
        # which made the visual hierarchy feel disconnected from the selected
        # page and allowed a second window to fall behind the main GUI.
        panel = self.match_detail_panel
        self.match_detail_dialog = panel  # compatibility alias for old cleanup/tests
        detail_layout = self.match_detail_panel_layout
        detail_layout.setContentsMargins(18, 14, 18, 14)
        detail_layout.setSpacing(10)
        detail_header = QHBoxLayout()
        detail_header.addWidget(icon_tile('chart', 'sectionIcon', '#bfe1ff', 15, 28))
        self.match_detail_title = label('单局详情', 'section')
        detail_header.addWidget(self.match_detail_title)
        detail_header.addStretch(1)
        self.match_detail_back = self.button('返回战局', self._close_match_detail)
        self.match_detail_back.setObjectName('settingsBack')
        detail_header.addWidget(self.match_detail_back)
        detail_layout.addLayout(detail_header)
        self.match_detail_state = label('战局概览 · 玩家表现 · MVP', 'muted')
        self.match_detail_state.setWordWrap(True)
        detail_layout.addWidget(self.match_detail_state)
        self.match_detail_scroll = QScrollArea()
        self.match_detail_scroll.setWidgetResizable(True)
        self.match_detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.match_detail_scroll.verticalScrollBar().setMinimumWidth(12)
        self.match_detail_content = QWidget()
        self.match_detail_content.setObjectName('matchDetailContent')
        self.match_detail_content.setStyleSheet('QWidget#matchDetailContent { background: transparent; }')
        self.match_detail_layout = QVBoxLayout(self.match_detail_content)
        self.match_detail_layout.setContentsMargins(0, 4, 2, 0)
        self.match_detail_layout.setSpacing(10)
        self.match_detail_scroll.setWidget(self.match_detail_content)
        detail_layout.addWidget(self.match_detail_scroll, 1)
        panel.hide()

    def _enter_match_detail(self):
        """Switch the existing match page to its embedded detail surface."""
        self.match_heading_host.hide()
        self.match_search_card.hide()
        self.match_result_panel.hide()
        self.match_detail_panel.show()

    def _close_match_detail(self):
        self._stop_detail_animations()
        self._match_detail_record = None
        if hasattr(self, 'match_detail_panel'):
            self.match_detail_panel.hide()
        if hasattr(self, 'match_heading_host'):
            self.match_heading_host.show()
        if hasattr(self, 'match_search_card'):
            self.match_search_card.show()
        if hasattr(self, 'match_result_panel'):
            self.match_result_panel.show()

    def _open_match_detail(self, record):
        self._ensure_detail_dialog()
        self._match_detail_record = record
        self._enter_match_detail()
        self._request_match_detail(record)

    @staticmethod
    def _load_recent_queries():
        settings = QSettings('DEV King', '逐渐工具箱')
        recent = settings.value('matchHistory/recentQueries', [])
        if not isinstance(recent, (list, tuple)):
            recent = [recent] if recent else []
        names = []
        for name in recent:
            normalized = str(name).strip()
            if normalized and normalized.casefold() not in {item.casefold() for item in names}:
                names.append(normalized)
            if len(names) == 5:
                break
        return names

    def _refresh_recent_query_buttons(self):
        if not hasattr(self, 'recent_query_buttons'):
            return
        self.recent_queries_empty.setVisible(not self._recent_queries)
        for index, button in enumerate(self.recent_query_buttons):
            if index < len(self._recent_queries):
                name = self._recent_queries[index]
                # Keep the label readable across themes and DPI settings.  The
                # button grows to the measured text width instead of replacing
                # a player name with an unexplained ellipsis.
                # Long player names keep their full value in the tooltip and
                # accessibility name, while the compact two-row layout uses
                # an explicit elision width so text cannot paint over a
                # neighbouring button.
                button.setText(button.fontMetrics().elidedText(
                    name, Qt.TextElideMode.ElideRight, 138))
                measured = button.fontMetrics().horizontalAdvance(name) + 28
                button.setMinimumWidth(max(82, min(170, measured)))
                button.setToolTip(f'点击查询 {name}')
                button.setAccessibleName(f'查询最近玩家 {name}')
                button.setVisible(True)
            else:
                button.hide()

    def _remember_recent_query(self, player_name):
        player_name = player_name.strip()
        self._recent_queries = [
            name for name in self._recent_queries
            if name.casefold() != player_name.casefold()]
        self._recent_queries.insert(0, player_name)
        self._recent_queries = self._recent_queries[:5]
        QSettings('DEV King', '逐渐工具箱').setValue(
            'matchHistory/recentQueries', self._recent_queries)
        self._refresh_recent_query_buttons()

    def _query_recent_player(self, position):
        if position < 0 or position >= len(self._recent_queries):
            return
        self.match_player_input.setText(self._recent_queries[position])
        self.query_match_history(True)

    @staticmethod
    def _match_detail_key(record):
        match_id = str(record.get('matchId') or record.get('_id') or record.get('match_id') or '')
        match_date = str(record.get('date') or record.get('end_time') or record.get('endTime') or '')
        return (match_id, match_date)

    @staticmethod
    def _format_summary_number(value):
        if value is None:
            return '—'
        return str(int(value)) if float(value).is_integer() else str(round(value, 1))

    def _refresh_daily_match_summary(self, records):
        summary = match_data.day_summary(records)
        synced = summary['total'] - summary['missing_stats']
        known = summary['wins'] + summary['losses']
        complete = self._today_scan_complete and not self._today_scan_error
        rate = summary['wins'] / known * 100 if known else None
        # Avoid forcing a style unpolish/repolish on every API completion when
        # the displayed value did not actually change.  Metadata hydration can
        # complete several times in quick succession; keeping the existing
        # widget state avoids needless synchronous repaints on the GUI thread.
        win_rate_label = self.daily_stat_values['win_rate']
        win_rate_text = f'{rate:.0f}%' if rate is not None else '—'
        win_rate_semantic = 'neutral' if rate is None else ('positive' if rate >= 50 else 'negative')
        if win_rate_label.text() != win_rate_text:
            win_rate_label.setText(win_rate_text)
        if win_rate_label.property('semantic') != win_rate_semantic:
            win_rate_label.setProperty('semantic', win_rate_semantic)
            win_rate_label.style().unpolish(win_rate_label)
            win_rate_label.style().polish(win_rate_label)
            win_rate_label.update()
        for key in ('final_kills', 'beds', 'final_deaths'):
            value = summary[key]
            if summary['total'] == 0:
                text = '0' if complete else '—'
            elif synced == 0:
                text = '—'
            else:
                text = self._format_summary_number(value)
                if summary['missing_stats']:
                    text += '+'
            value_label = self.daily_stat_values[key]
            tooltip = ('已同步部分的合计，仍有对局尚未返回个人统计'
                       if summary['missing_stats'] else '今日已同步对局的合计')
            semantic = 'neutral' if text == '—' else ('negative' if key == 'final_deaths' else 'positive')
            if value_label.text() != text:
                value_label.setText(text)
            if value_label.toolTip() != tooltip:
                value_label.setToolTip(tooltip)
            if value_label.property('semantic') != semantic:
                value_label.setProperty('semantic', semantic)
                value_label.style().unpolish(value_label)
                value_label.style().polish(value_label)
                value_label.update()
        if not self._match_player_name:
            hint = '查询后自动汇总'
        elif self._today_scan_error:
            hint = self._today_scan_error
        elif not complete:
            hint = f"正在同步今日对局 · 已找到 {summary['total']} 场"
        elif summary['missing_stats']:
            jobs_pending = any(job['action'] in ('hydrate', 'detail') for job in self._api_queue)
            jobs_pending = jobs_pending or self._api_action in ('hydrate', 'detail')
            hint = (f"战斗数据 {synced}/{summary['total']} 场 · "
                    + ('同步中' if jobs_pending else '部分数据不可用'))
        else:
            hint = f"{summary['wins']} 胜 {summary['losses']} 负 · 今日 {summary['total']} 场"
        if self.daily_summary_hint.text() != hint:
            self.daily_summary_hint.setText(hint)


    @staticmethod
    def _record_duration_text(record):
        value = next((record.get(key) for key in (
            'duration', 'game_duration', 'gameDuration', 'play_time', 'playTime',
            'playtime', 'elapsed', 'elapsed_seconds') if record.get(key) is not None), None)
        if value is None:
            start = record.get('start_time') or record.get('startTime')
            end = record.get('end_time') or record.get('endTime')
            try:
                start_dt = datetime.fromisoformat(str(start).replace('Z', '+00:00'))
                end_dt = datetime.fromisoformat(str(end).replace('Z', '+00:00'))
                value = max(0, (end_dt - start_dt).total_seconds())
            except (TypeError, ValueError):
                return '时长未知'
        text = str(value).strip()
        if ':' in text:
            parts = text.split(':')
            if len(parts) in (2, 3) and all(part.isdigit() for part in parts):
                numbers = [int(part) for part in parts]
                seconds = numbers[-1] + numbers[-2] * 60
                if len(numbers) == 3:
                    seconds += numbers[0] * 3600
            else:
                return text
        else:
            try:
                seconds = max(0, int(float(text)))
            except ValueError:
                return text
        if seconds >= 3600:
            return f'{seconds // 3600}小时{(seconds % 3600) // 60}分'
        if seconds >= 60:
            return f'{seconds // 60}分{seconds % 60:02d}秒'
        return f'{seconds}秒'

    @staticmethod
    def _record_map_name(record):
        for key in ('map', 'map_name', 'mapName', 'arena', 'mapname'):
            value = record.get(key)
            if isinstance(value, dict):
                value = value.get('name') or value.get('display_name') or value.get('id')
            if value:
                return str(value)
        return '地图未知'

    @staticmethod
    def _record_date_display(record):
        """Use a compact date in the card; the full API value stays in tooltip."""
        raw = str(record.get('date') or record.get('end_time')
                   or record.get('endTime') or '时间未知')
        try:
            parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            return parsed.astimezone().strftime('%m-%d %H:%M')
        except (TypeError, ValueError, OverflowError):
            # Keep a readable prefix when an upstream API returns a custom
            # timestamp or an unusually long identifier.
            return raw if len(raw) <= 19 else f'{raw[:18]}…'

    @staticmethod
    def _compact_card_text(value, limit=18):
        value = str(value or '')
        return value if len(value) <= limit else f'{value[:max(1, limit - 1)]}…'

    def query_match_history(self, reset_page=False):
        player_name = self.match_player_input.text().strip()
        if not player_name:
            self.match_status.setText('请先输入布吉岛游戏昵称。')
            self.match_player_input.setFocus()
            return
        if not self._api_token:
            self.match_status.setText('尚未设置 API Token，正在打开 API 令牌设置。')
            self._open_api_settings()
            return
        if reset_page or player_name != self._match_player_name:
            self._api_generation += 1
            self._api_queue.clear()
            self._hydrating_keys.clear()
            self._history_loading_pages.clear()
            self._match_player_name = player_name
            self._match_player_uuid = ''
            self._match_page = 1
            # Mark page 1 before clearing the visible list.  The scroll area's
            # rangeChanged signal can fire during that clear; without this
            # guard it could enqueue a second page-1 request before the normal
            # query request is submitted below.
            self._history_loading_pages.add(1)
            self._match_records_accumulated = []
            self._history_pages = {}
            self._history_has_more = True
            self._today_scan_complete = False
            self._today_scan_error = ''
            self._history_scan_last = None
            self._history_sorted = True
            if self.match_detail_dialog is not None:
                self._close_match_detail()
            self._render_match_records([])
            self._refresh_daily_match_summary([])
            if hasattr(self, 'match_scroll'):
                self.match_scroll.verticalScrollBar().setValue(0)
        self._remember_recent_query(player_name)
        if hasattr(self, 'match_page_label'):
            self.match_page_label.setText(f'已载入 {len(self._match_records_accumulated)} 场')
        self.match_status.setText('正在查询对局……')
        payload = {'username': self._match_player_name, 'page': str(self._match_page)}
        self._history_loading_pages.add(self._match_page)
        self._start_api_request('history', bugland_api.MATCH_HISTORY_ENDPOINT, payload, priority=True)

    def _change_match_page(self, offset):
        next_page = max(1, self._match_page + offset)
        if next_page == self._match_page:
            return
        self._match_page = next_page
        if next_page in self._history_pages:
            self._show_history_page(next_page)
        else:
            self.query_match_history(False)

    def eventFilter(self, watched, event):
        """Give the match list a small pull-to-refresh affordance.

        Qt clamps a scroll area at its bottom edge, so an extra wheel tick
        normally produces no visual feedback.  When the user keeps scrolling
        down at that edge we briefly expand a footer, show the refresh time,
        then fade it away.  The normal scrollbar and API page loader continue
        to receive their events unchanged.
        """
        if (hasattr(self, 'match_scroll') and watched in (
                self.match_scroll.viewport(), self.match_scroll.verticalScrollBar())
                and event.type() == QEvent.Type.Wheel):
            delta = event.angleDelta().y()
            scrollbar = self.match_scroll.verticalScrollBar()
            at_bottom = scrollbar.maximum() > 0 and scrollbar.value() >= scrollbar.maximum() - 2
            if delta < 0 and at_bottom:
                self._show_match_pull_feedback()
                self._maybe_load_next_match_page()
        return super().eventFilter(watched, event)

    def _show_match_pull_feedback(self):
        """Animate the compact bottom footer and let it settle invisibly."""
        hint = getattr(self, 'match_pull_hint', None)
        if hint is None:
            return
        now_text = datetime.now().astimezone().strftime('%H:%M:%S')
        hint.setText(f'请等待（刷新时间 {now_text}）')
        hint.setVisible(True)
        # Stop a previous collapse/fade before restarting the pull gesture.
        for animation in (self._match_pull_animation,
                          self._match_pull_fade_animation,
                          self._match_pull_collapse_animation):
            if animation is not None:
                animation.stop()
        self.match_pull_hint_effect.setOpacity(1.0)
        hint.setMinimumHeight(0)
        hint.setMaximumHeight(0)
        expand = QPropertyAnimation(hint, b'maximumHeight', self)
        expand.setDuration(180)
        expand.setStartValue(0)
        expand.setEndValue(28)
        expand.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._match_pull_animation = expand
        expand.start(QPropertyAnimation.DeletionPolicy.KeepWhenStopped)

        # Keep the message visible for a short moment, then fade and collapse
        # the empty space so the list returns exactly to its original height.
        QTimer.singleShot(520, self._fade_match_pull_feedback)

    def _fade_match_pull_feedback(self):
        hint = getattr(self, 'match_pull_hint', None)
        effect = getattr(self, 'match_pull_hint_effect', None)
        if hint is None or effect is None or not hint.isVisible():
            return
        fade = QPropertyAnimation(effect, b'opacity', self)
        fade.setDuration(260)
        fade.setStartValue(effect.opacity())
        fade.setEndValue(0.0)
        fade.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._match_pull_fade_animation = fade

        def collapse():
            collapse_animation = QPropertyAnimation(hint, b'maximumHeight', self)
            collapse_animation.setDuration(180)
            collapse_animation.setStartValue(hint.maximumHeight())
            collapse_animation.setEndValue(0)
            collapse_animation.setEasingCurve(QEasingCurve.Type.InCubic)
            self._match_pull_collapse_animation = collapse_animation

            def hidden():
                hint.setVisible(False)
                hint.setMinimumHeight(0)
                hint.setMaximumHeight(0)
                hint.clearFocus()

            collapse_animation.finished.connect(hidden)
            collapse_animation.start(QPropertyAnimation.DeletionPolicy.KeepWhenStopped)

        fade.finished.connect(collapse)
        fade.start(QPropertyAnimation.DeletionPolicy.KeepWhenStopped)

    def _maybe_load_next_match_page(self):
        """Load the next API page when the continuous history list nears its end."""
        if not hasattr(self, 'match_scroll') or not self._match_player_name:
            return
        if not self._history_has_more or self._api_generation <= 0:
            return
        scrollbar = self.match_scroll.verticalScrollBar()
        # With an empty/short first page max() is zero; loading once here also
        # makes the list fill the viewport without requiring a hidden page tap.
        if scrollbar.maximum() > 0 and scrollbar.value() < scrollbar.maximum() - 24:
            return
        loaded_pages = [page for page in self._history_pages if isinstance(page, int)]
        next_page = max(loaded_pages or [0]) + 1
        if next_page in self._history_loading_pages or next_page in self._history_pages:
            return
        # The daily summary scanner can already be requesting this same page.
        # Reuse that response instead of queuing a duplicate request while the
        # user is dragging the scrollbar at the end of the list.
        queued_same_page = any(
            item.get('action') in ('history', 'today')
            and str(item.get('payload', {}).get('page')) == str(next_page)
            for item in self._api_queue)
        active = self._api_context
        active_same_page = bool(
            active and active.get('action') in ('history', 'today')
            and str(active.get('payload', {}).get('page')) == str(next_page))
        if queued_same_page or active_same_page:
            return
        self._match_page = next_page
        self._history_loading_pages.add(next_page)
        self.match_status.setText('继续下滑以加载更多对局……')
        self._start_api_request(
            'history', bugland_api.MATCH_HISTORY_ENDPOINT,
            {'username': self._match_player_name, 'page': str(next_page)},
            priority=False)

    def _start_api_request(self, action, endpoint, payload, record=None, priority=False):
        job = {'action': action, 'endpoint': endpoint, 'payload': payload,
               'record': record, 'generation': self._api_generation}
        if action == 'history':
            self._api_queue = deque(item for item in self._api_queue if item['action'] != 'history')
        identity = (action, payload)
        if any((item['action'], item['payload']) == identity for item in self._api_queue):
            return
        if priority:
            self._api_queue.appendleft(job)
        elif action == 'today':
            # Put day scanning before metadata, but behind explicit user actions.
            index = next((i for i, item in enumerate(self._api_queue)
                          if item['action'] not in ('history', 'detail')), len(self._api_queue))
            self._api_queue.insert(index, job)
        else:
            self._api_queue.append(job)
        self._pump_api_queue()

    def _pump_api_queue(self):
        if self._closing or self._api_worker is not None or not self._api_queue:
            return
        now = time.monotonic()
        while self._api_request_times and now - self._api_request_times[0] >= 60:
            self._api_request_times.popleft()
        wait = max(0, 2.2 - (now - self._api_request_times[-1])) if self._api_request_times else 0
        if len(self._api_request_times) >= 28:
            wait = max(wait, 60.1 - (now - self._api_request_times[0]))
        if wait > 0:
            self._api_pump_timer.start(int(wait * 1000) + 1)
            return
        job = self._api_queue.popleft()
        if job['generation'] != self._api_generation:
            self._api_pump_timer.start(0)
            return
        self._api_request_times.append(now)
        self._api_action = job['action']
        self._api_context = job
        worker = BuglandRequestWorker(job['action'], job['endpoint'], job['payload'], self._api_token, self)
        worker.completed.connect(self._on_api_request_completed)
        worker.finished.connect(self._on_api_worker_finished)
        self._api_worker = worker
        worker.start()

    def _on_api_request_completed(self, action, result, error):
        job = self._api_context
        if action == 'history' and job is not None:
            try:
                self._history_loading_pages.discard(int(job.get('payload', {}).get('page', 0)))
            except (TypeError, ValueError):
                pass
        if self._closing or job is None or job['generation'] != self._api_generation:
            return
        record = job.get('record')
        if error:
            if any(word in error for word in ('Token', '连接', '超时', '频率')):
                self._api_queue.clear()
                self._today_scan_error = '连接中断，可重新查询'
                self.match_status.setText(error)
                if action == 'leaderboard' and hasattr(self, 'leaderboard_status'):
                    self.leaderboard_status.setText(error)
            if action in ('history', 'today'):
                self._today_scan_error = '同步中断，可重新查询'
                self.match_status.setText(error)
            elif action == 'leaderboard':
                self.leaderboard_result_meta.setText('请求失败')
                self.leaderboard_status.setText(error)
            elif record is not None:
                record['_detail_error'] = error
                if self._match_detail_record is record:
                    self._set_match_detail_placeholder('详情加载失败', error)
            self._refresh_record_metadata()
            self._refresh_daily_match_summary(self._match_records_accumulated)
            return
        if action in ('history', 'today'):
            try:
                records, page, page_size, player_uuid = match_data.unpack_history(result)
            except ValueError as exc:
                self._today_scan_error = '数据格式异常，可重新查询'
                self.match_status.setText(str(exc))
                self._refresh_daily_match_summary(self._match_records_accumulated)
                return
            # The requested page is authoritative for legacy responses that omit page.
            page = int(job['payload']['page'])
            self._page_size = page_size
            # A short/empty response is the end of the continuous history
            # stream.  A full page remains eligible for one more lazy load.
            if action == 'history':
                self._history_has_more = bool(records) and len(records) >= max(1, page_size)
            if player_uuid:
                self._match_player_uuid = player_uuid
            known = {match_data.record_key(item): item for item in self._match_records_accumulated}
            added = 0
            merged = []
            for item in records:
                key = match_data.record_key(item)
                if key in known:
                    current = known[key]
                    current.update(item)
                else:
                    current = dict(item)
                    self._match_records_accumulated.append(current)
                    known[key] = current
                    added += 1
                merged.append(current)
            already_loaded = page in self._history_pages
            self._history_pages[page] = merged
            if action == 'history':
                # Render every page received so scrolling feels like one list;
                # this also fixes the old hidden "page 2" state not appearing
                # until a pager button was pressed.
                self._show_history_page(page)
                if records and added == 0 and not already_loaded:
                    # Avoid spinning through the API if it repeats a page.
                    self._history_has_more = False
                    self.match_status.setText('接口返回重复页，已停止继续加载。')
            if action == 'today' and records and added == 0 and not already_loaded:
                self._today_scan_error = '接口返回重复页，统计未完成'
            elif (page == 1 or action == 'today') and not self._today_scan_complete:
                dates = [match_data.parse_time(item.get('date') or item.get('end_time')) for item in merged]
                today = match_data.parse_time(datetime.now().astimezone()).date()
                if any(value is None for value in dates):
                    self._history_sorted = False
                valid_dates = [value for value in dates if value is not None]
                chain = ([self._history_scan_last] if self._history_scan_last else []) + valid_dates
                if any(left < right for left, right in zip(chain, chain[1:])):
                    self._history_sorted = False
                if valid_dates:
                    self._history_scan_last = valid_dates[-1]
                crossed_day = self._history_sorted and any(value.date() < today for value in valid_dates)
                if not records or crossed_day:
                    self._today_scan_complete = True
                elif not self._today_scan_error:
                    self._start_api_request('today', bugland_api.MATCH_HISTORY_ENDPOINT,
                                            {'username': self._match_player_name, 'page': str(page + 1)})
            today = match_data.parse_time(datetime.now().astimezone()).date()
            for current in merged:
                played = match_data.parse_time(current.get('date') or current.get('end_time'))
                if action == 'history' or (played and played.date() == today):
                    self._queue_record_metadata(current, refresh=False)
            # A page can contain several cached records.  Refresh the visible
            # labels once after all records have been merged instead of doing
            # a full row walk for every cache hit.
            self._refresh_record_metadata()
            self._refresh_daily_match_summary(self._match_records_accumulated)
            return
        if action == 'leaderboard':
            key = self._leaderboard_pending_key or self._leaderboard_last_key
            self._leaderboard_cache[key] = result
            self._render_leaderboard_result(key, result)
            return
        if action in ('detail', 'hydrate') and record is not None:
            details = result.get('data', result) if isinstance(result, dict) else result
            if not isinstance(details, dict) or not any(key in details for key in ('teams', 'map', 'end_time')):
                record['_detail_error'] = '此对局未返回可用详情'
                if self._match_detail_record is record:
                    self._set_match_detail_placeholder('暂无详情', record['_detail_error'])
            else:
                self._match_detail_cache[self._match_detail_key(record)] = details
                record.update(match_data.enrich_record(record, details, self._match_player_name, self._match_player_uuid))
                record.pop('_detail_error', None)
                if self._match_detail_record is record and self.match_detail_dialog is not None:
                    self._show_match_detail(record, details)
            self._refresh_record_metadata()
            self._refresh_daily_match_summary(self._match_records_accumulated)

    def _on_api_worker_finished(self):
        worker = self._api_worker
        self._api_worker = None
        self._api_action = None
        self._api_context = None
        if worker is not None:
            worker.deleteLater()
        self._refresh_daily_match_summary(self._match_records_accumulated)
        self._api_pump_timer.start(0)

    def _show_history_page(self, page):
        self._match_page = page
        self._match_records = list(self._match_records_accumulated)
        self.match_page_label.setText(f'已载入 {len(self._match_records)} 场')
        self._render_match_records(self._match_records)
        if self._match_records:
            more_hint = ' · 下滑加载更多' if self._history_has_more else ''
            self.match_status.setText(
                f'已载入 {len(self._match_records)} 场{more_hint} · 双击查看详情，地图和时长会自动补齐。')
        else:
            self.match_status.setText('这一页没有对局记录。')
        self.match_previous_button.setEnabled(page > 1)
        self.match_next_button.setEnabled(len(self._match_records) >= self._page_size)
        for record in self._match_records:
            self._queue_record_metadata(record, refresh=False)
        self._refresh_record_metadata()

    def _queue_record_metadata(self, record, refresh=True):
        key = self._match_detail_key(record)
        if key in self._match_detail_cache:
            record.update(match_data.enrich_record(record, self._match_detail_cache[key],
                                                  self._match_player_name, self._match_player_uuid))
            if refresh:
                self._refresh_record_metadata()
            return
        if key in self._hydrating_keys or not all(key):
            return
        self._hydrating_keys.add(key)
        self._start_api_request('hydrate', bugland_api.MATCH_DETAIL_ENDPOINT,
                                {'match': key[0], 'date': key[1]}, record=record)

    def _refresh_record_metadata(self):
        for row, record in self._match_record_widgets:
            map_name = self._record_map_name(record)
            duration = self._record_duration_text(record)
            tooltip = record.get('_detail_error') or '双击打开完整对局 · Enter 也可打开'
            # Hydration may revisit every visible row.  Only touch labels and
            # tooltips whose value changed so Qt does not relayout/repaint the
            # entire card list for an unchanged record.
            compact_map = self._compact_card_text(map_name)
            if row._map_label.text() != compact_map:
                row._map_label.setText(compact_map)
            if row._map_label.toolTip() != map_name:
                row._map_label.setToolTip(map_name)
            if row._duration_label.text() != duration:
                row._duration_label.setText(duration)
            if row.toolTip() != tooltip:
                row.setToolTip(tooltip)


    def _render_match_records(self, records):
        rows_widget = getattr(self, 'match_rows_widget', None)
        if rows_widget is not None:
            rows_widget.setUpdatesEnabled(False)
        try:
            while self.match_rows_layout.count():
                item = self.match_rows_layout.takeAt(0)
                widget = item.widget()
                # Keep the pull-to-refresh footer alive between renders so
                # its opacity/height animations are not interrupted when a
                # new API page arrives.
                if widget is self.match_pull_hint:
                    continue
                if widget is not None:
                    widget.deleteLater()
            self._match_record_widgets = []
            self._selected_match_row = None
            for record in records:
                if not isinstance(record, dict):
                    continue
                row = MatchRecordCard()
                row.setObjectName('matchRecord')
                row.setProperty('selected', False)
                row.setCursor(Qt.CursorShape.PointingHandCursor)
                row_layout = QHBoxLayout(row)
                # Compact cards keep five or more matches visible in the
                # default window while leaving the two-line metadata readable.
                row.setMinimumHeight(60)
                row.setAccessibleName(f"{record.get('type', '对局')} {record.get('date', '')}")
                row_layout.setContentsMargins(16, 8, 16, 8)
                row_layout.setSpacing(12)
                outcome = self._match_outcome(record.get('win')) or 'unknown'
                row.setProperty('recordOutcome', outcome)
                result_text = {'win': '胜利', 'loss': '失败', 'unknown': '未知'}[outcome]
                result_label = label(result_text, 'matchRowOutcome')
                result_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                result_label.setMinimumWidth(68)
                result_label.setMaximumWidth(78)
                row_layout.addWidget(result_label)
                copy = QVBoxLayout()
                copy.setSpacing(4)
                mode = str(record.get('type') or record.get('gamemode') or '对局')
                date = str(record.get('date') or record.get('end_time') or record.get('endTime') or '时间未知')
                title = label(self._compact_card_text(mode), 'matchRowTitle')
                title.setToolTip(mode)
                copy.addWidget(title)
                date_label = label(self._record_date_display(record), 'matchRowMeta')
                date_label.setToolTip(date)
                copy.addWidget(date_label)
                row_layout.addLayout(copy, 1)
                metadata = QVBoxLayout()
                metadata.setSpacing(4)
                map_name = self._record_map_name(record)
                row._map_label = label(self._compact_card_text(map_name), 'matchRowTitle')
                row._map_label.setToolTip(map_name)
                row._map_label.setAlignment(Qt.AlignmentFlag.AlignRight)
                row._duration_label = label(self._record_duration_text(record), 'matchRowMeta')
                row._duration_label.setAlignment(Qt.AlignmentFlag.AlignRight)
                metadata.addWidget(row._map_label)
                metadata.addWidget(row._duration_label)
                row_layout.addLayout(metadata, 1)
                row_layout.addWidget(label('›', 'matchRowTitle'))
                row.pressed.connect(lambda current=row, item=record: self._select_match_record(current, item))
                row.doubleClicked.connect(lambda item=record: self._open_match_detail(item))
                for child in row.findChildren(QWidget):
                    child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                self.match_rows_layout.addWidget(row)
                self._match_record_widgets.append((row, record))
            self.match_rows_layout.addWidget(self.match_pull_hint)
            self.match_rows_layout.addStretch(1)
            self._refresh_record_metadata()
        finally:
            if rows_widget is not None:
                rows_widget.setUpdatesEnabled(True)
                rows_widget.update()

    def _select_match_record(self, row, record):
        previous = self._selected_match_row
        if previous is not None and previous is not row:
            previous.setProperty('selected', False)
            previous.style().unpolish(previous)
            previous.style().polish(previous)
            previous.update()
        row.setProperty('selected', True)
        row.style().unpolish(row)
        row.style().polish(row)
        row.update()
        self._selected_match_row = row

    def _request_match_detail(self, record):
        cache_key = self._match_detail_key(record)
        if cache_key in self._match_detail_cache:
            self._show_match_detail(record, self._match_detail_cache[cache_key])
            return
        match_id, match_date = cache_key
        if not match_id or not match_date:
            self.match_status.setText('此条记录缺少单局详情所需的对局 ID 或时间。')
            self._set_match_detail_placeholder('详情暂不可用', '这条记录没有返回详情接口需要的对局 ID 或时间。')
            return
        self._pending_match_record = record
        self.match_detail_title.setText('正在加载详情')
        self.match_status.setText('正在加载这场对局……')
        self._set_match_detail_placeholder('正在加载', '正在整理地图、队伍和玩家数据。')
        # Reuse an active metadata request and promote an already queued one.
        if (self._api_context is not None and self._api_context.get('record') is record
                and self._api_context['generation'] == self._api_generation):
            return
        self._api_queue = deque(
            job for job in self._api_queue
            if job.get('record') is None or self._match_detail_key(job['record']) != cache_key)
        self._start_api_request(
            'detail', bugland_api.MATCH_DETAIL_ENDPOINT,
            {'match': match_id, 'date': match_date}, record=record, priority=True)

    def _show_match_detail(self, record, details):
        if not isinstance(details, dict):
            self.match_status.setText('API 返回的单局详情格式无法显示。')
            self._set_match_detail_placeholder('详情暂不可读', 'API 返回的内容格式与当前展示不兼容。')
            return
        self._clear_detail_layout()
        map_name = str(details.get('map') or '地图未知')
        mode = str(details.get('gamemode') or record.get('type') or '模式未知')
        match_date = str(details.get('end_time') or record.get('date') or '时间未知')
        teams = details.get('teams') or {}
        if not isinstance(teams, dict):
            teams = {}
        winner_value = details.get('winning_team') or details.get('winner_team') or details.get('win') or details.get('winner')
        if isinstance(winner_value, dict):
            winner_value = winner_value.get('name') or winner_value.get('team') or winner_value.get('team_name')
        winner_name = str(winner_value) if winner_value is not None and self._match_outcome(winner_value) is None else ''
        winner_team = self._find_winner_team(winner_name, teams)
        outcome = self._match_outcome(record.get('win'))

        players = []
        for team_name, team_players in teams.items():
            if isinstance(team_players, list):
                for player in team_players:
                    if isinstance(player, dict):
                        players.append((str(team_name), player))
        explicit_mvps = [(team, player) for team, player in players if self._has_explicit_mvp(player)]
        estimated_mvp = False
        if explicit_mvps:
            mvps = explicit_mvps
        else:
            candidates = [(team, player) for team, player in players if winner_team is None or team == winner_team]
            candidates.sort(key=lambda pair: self._mvp_score(pair[1]), reverse=True)
            if candidates and self._mvp_score(candidates[0][1]) > 0:
                mvps = [candidates[0]]
                estimated_mvp = True
            else:
                mvps = []
        mvp_keys = {self._player_identity(player) for _, player in mvps}

        self.match_detail_title.setText(map_name)
        summary_bits = [mode, match_date]
        if winner_team:
            summary_bits.append(f'{winner_team} 获胜')
        elif winner_name:
            summary_bits.append(f'胜方：{winner_name}')
        self.match_detail_state.setText('  ·  '.join(summary_bits))

        hero = QFrame()
        hero.setObjectName('matchDetailHero')
        hero.setProperty('recordOutcome', outcome or 'unknown')
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(16, 14, 16, 14)
        hero_layout.setSpacing(10)
        hero_top = QHBoxLayout()
        hero_copy = QVBoxLayout()
        hero_copy.setSpacing(3)
        hero_copy.addWidget(label(mode, 'cardTitle'))
        hero_copy.addWidget(label(match_date, 'cardHint'))
        hero_top.addLayout(hero_copy, 1)
        if outcome == 'win':
            hero_top.addWidget(label('胜利', 'matchWinBadge'))
        elif outcome == 'loss':
            hero_top.addWidget(label('失败', 'matchLossBadge'))
        else:
            hero_top.addWidget(label('已结束', 'matchUnknownBadge'))
        hero_layout.addLayout(hero_top)
        summary_row = QHBoxLayout()
        summary_row.setSpacing(8)
        summary_row.addWidget(self._stat_chip('队伍', str(len(teams))))
        summary_row.addWidget(self._stat_chip('玩家', str(len(players))))
        summary_row.addWidget(self._stat_chip('地图', map_name))
        summary_row.addWidget(self._stat_chip('游玩时长', self._record_duration_text(record)))
        hero_layout.addLayout(summary_row)
        if mvps:
            _, mvp_player = mvps[0]
            mvp_name = str(mvp_player.get('player_name') or mvp_player.get('player_uuid') or '未知玩家')
            mvp_note = '数据估算' if estimated_mvp else 'API 标记'
            mvp_row = QHBoxLayout()
            mvp_row.addWidget(label('本场高光', 'muted'))
            mvp_row.addWidget(label('MVP', 'mvpBadge'))
            mvp_row.addWidget(label(mvp_name, 'cardTitle'))
            mvp_row.addStretch(1)
            mvp_row.addWidget(label(mvp_note, 'cardHint'))
            hero_layout.addLayout(mvp_row)
        self.match_detail_layout.addWidget(hero)
        self._fade_detail_widget(hero)

        queried_player = None
        for team_name, player in players:
            name = str(player.get('player_name') or player.get('player_uuid') or '')
            if name.casefold() == self._match_player_name.casefold():
                queried_player = player
                break
        if queried_player is None and self._match_player_uuid:
            for _team_name, player in players:
                if str(player.get('player_uuid') or '').casefold() == self._match_player_uuid.casefold():
                    queried_player = player
                    break
        # The queried player's summary follows the same fixed-field contract
        # as the metric grid below.  An absent player field is a real zero for
        # display purposes, rather than an em dash that makes the card look
        # incomplete.
        query_final = self._display_stat_value(self._numeric_stat(
            queried_player or {}, 'final_kill', 'finalKill'))
        query_beds = self._display_stat_value(self._numeric_stat(
            queried_player or {}, 'bed_destory', 'bed_destroy', 'bedDestroy'))
        query_final_deaths = self._display_stat_value(self._numeric_stat(
            queried_player or {}, 'final_death', 'finalDeath'))
        ribbon = QFrame()
        ribbon.setObjectName('matchDetailRibbon')
        ribbon.setProperty('recordOutcome', outcome or 'unknown')
        ribbon_layout = QVBoxLayout(ribbon)
        ribbon_layout.setContentsMargins(17, 14, 17, 14)
        ribbon_layout.setSpacing(8)
        ribbon_top = QHBoxLayout()
        copy = QVBoxLayout()
        copy.setSpacing(3)
        copy.addWidget(label('MATCH MOMENT', 'detailKicker'))
        if outcome == 'win':
            highlight_text = f'胜利锁定 · {query_final} 次最终击杀'
        elif outcome == 'loss':
            highlight_text = f'战局复盘 · {query_beds} 次拆床制造机会'
        else:
            highlight_text = '战局高光 · 正在整理个人表现'
        copy.addWidget(label(highlight_text, 'detailHighlight'))
        mvp_text = (f'MVP：{mvps[0][1].get("player_name") or "本场高光"}' if mvps
                    else 'MVP：根据当前返回数据暂未判定')
        copy.addWidget(label(f'{mvp_text}  ·  {map_name}  ·  {self._record_duration_text(record)}', 'detailSubline'))
        ribbon_top.addLayout(copy, 1)
        ribbon_top.addWidget(label('查询的人', 'queryTargetBadge'), 0, Qt.AlignmentFlag.AlignTop)
        pulse = QProgressBar()
        pulse.setObjectName('detailPulse')
        pulse.setRange(0, 100)
        pulse.setValue(min(100, 48 + int(self._numeric_stat(queried_player or {}, 'kill', 'kills') * 8)))
        pulse.setTextVisible(False)
        pulse.setFixedWidth(115)
        ribbon_top.addWidget(pulse, 0, Qt.AlignmentFlag.AlignVCenter)
        ribbon_layout.addLayout(ribbon_top)
        stats = QHBoxLayout()
        stats.setSpacing(7)
        for caption, value in (('最终击杀', query_final), ('拆床', query_beds), ('最终死亡', query_final_deaths)):
            stat = QVBoxLayout()
            stat.setSpacing(1)
            stat.addWidget(label(value, 'detailStatValue'))
            stat.addWidget(label(caption, 'detailStatCaption'))
            stats.addLayout(stat, 1)
        ribbon_layout.addLayout(stats)
        self.match_detail_layout.addWidget(ribbon)
        self._fade_detail_widget(ribbon, duration=270)

        ordered_teams = list(teams.items())
        ordered_teams.sort(key=lambda item: 0 if winner_team and str(item[0]) == winner_team else 1)

        # A compact team overview gives the detail view a clear second level:
        # team identity and aggregate performance are separated from the
        # player-level positive/negative metric colors below.
        team_overview = QFrame()
        team_overview.setObjectName('teamOverviewCard')
        team_overview_layout = QVBoxLayout(team_overview)
        team_overview_layout.setContentsMargins(13, 12, 13, 13)
        team_overview_layout.setSpacing(8)
        team_overview_header = QHBoxLayout()
        team_overview_header.addWidget(label('队伍总览', 'section'))
        team_overview_header.addStretch(1)
        team_overview_header.addWidget(label('队伍颜色独立于玩家数据', 'cardHint'))
        team_overview_layout.addLayout(team_overview_header)
        team_grid = QGridLayout()
        team_grid.setContentsMargins(0, 0, 0, 0)
        team_grid.setHorizontalSpacing(8)
        team_grid.setVerticalSpacing(8)
        team_tints = ('blue', 'gold', 'violet', 'teal')
        for team_index, (team_name, team_players) in enumerate(ordered_teams):
            if not isinstance(team_players, list):
                continue
            team_name = str(team_name)
            team_item = QFrame()
            team_item.setObjectName('teamOverviewItem')
            team_item.setProperty('teamTint', team_tints[team_index % len(team_tints)])
            team_item.setProperty('teamResult', 'win' if winner_team and team_name == winner_team
                                   else 'loss' if winner_team else 'neutral')
            team_item_layout = QVBoxLayout(team_item)
            team_item_layout.setContentsMargins(11, 9, 11, 9)
            team_item_layout.setSpacing(5)
            team_title = QHBoxLayout()
            team_title.addWidget(label(team_name, 'cardTitle'))
            team_title.addStretch(1)
            result = '胜方' if winner_team and team_name == winner_team else '对手' if winner_team else '未知'
            team_title.addWidget(label(result, 'cardHint'))
            team_item_layout.addLayout(team_title)
            totals = {
                '击杀': sum(self._numeric_stat(item, 'kill', 'kills') for item in team_players if isinstance(item, dict)),
                '最终击杀': sum(self._numeric_stat(item, 'final_kill', 'finalKill') for item in team_players if isinstance(item, dict)),
                '拆床': sum(self._numeric_stat(item, 'bed_destroy', 'bed_destory', 'bedDestroy') for item in team_players if isinstance(item, dict)),
                '死亡': sum(self._numeric_stat(item, 'death', 'deaths') for item in team_players if isinstance(item, dict)),
            }
            total_line = QHBoxLayout()
            total_line.setSpacing(5)
            total_line.addWidget(label(f'{len(team_players)} 人', 'cardHint'))
            totals_label = label(' · '.join(
                f'{name} {self._display_stat_value(value)}'
                for name, value in totals.items()), 'cardHint')
            totals_label.setWordWrap(True)
            totals_label.setMinimumWidth(0)
            totals_label.setToolTip(' · '.join(
                f'{name} {self._display_stat_value(value)}'
                for name, value in totals.items()))
            total_line.addWidget(totals_label, 1)
            total_line.addStretch(1)
            team_item_layout.addLayout(total_line)
            team_grid.addWidget(team_item, team_index // 2, team_index % 2)
        team_overview_layout.addLayout(team_grid)
        self.match_detail_layout.addWidget(team_overview)
        self._fade_detail_widget(team_overview, duration=245)

        for team_index, (team_name, team_players) in enumerate(ordered_teams):
            if not isinstance(team_players, list):
                continue
            team_name = str(team_name)
            team_tint = self._team_tint(team_name, team_index)
            team_card, team_layout = panel()
            team_card.setObjectName('featureCard')
            team_card_layout = team_layout
            team_card_layout.setContentsMargins(13, 12, 13, 13)
            team_card_layout.setSpacing(9)
            team_header = QHBoxLayout()
            team_header.addWidget(icon_tile('shield', 'sectionIcon', '#bfe1ff', 14, 27))
            team_header.addWidget(label(team_name, 'section'))
            team_badge = label(team_tint.upper(), 'teamTintBadge')
            team_badge.setProperty('tint', team_tint)
            team_header.addWidget(team_badge)
            team_header.addWidget(label(f'{len(team_players)} 位玩家', 'cardHint'))
            team_header.addStretch(1)
            if winner_team and team_name == winner_team:
                team_header.addWidget(label('获胜队伍', 'matchWinBadge'))
            team_card_layout.addLayout(team_header)

            for player in team_players:
                if not isinstance(player, dict):
                    continue
                player_name = str(player.get('player_name') or player.get('player_uuid') or '未知玩家')
                player_uuid = str(player.get('player_uuid') or '')
                player_is_target = bool(
                    (self._match_player_name and player_name.casefold() == self._match_player_name.casefold())
                    or (self._match_player_uuid and player_uuid.casefold() == self._match_player_uuid.casefold())
                )
                player_row = QFrame()
                player_row.setObjectName('featureRow')
                player_row.setProperty('teamTint', team_tint)
                player_row.setProperty('queryTarget', 'true' if player_is_target else 'false')
                player_row.setProperty(
                    'playerOutcome',
                    'win' if winner_team and team_name == winner_team else
                    'loss' if winner_team else 'neutral')
                player_layout = QVBoxLayout(player_row)
                player_layout.setContentsMargins(11, 9, 11, 10)
                player_layout.setSpacing(8)
                player_top = QHBoxLayout()
                player_name_label = label(player_name, 'cardTitle')
                player_name_label.setObjectName('playerName')
                player_top.addWidget(player_name_label)
                identity = self._player_identity(player)
                if identity in mvp_keys:
                    mvp_badge = label('MVP', 'mvpBadge')
                    mvp_badge.setToolTip('由 API 标记' if not estimated_mvp else '根据击杀、最终击杀和拆床等战斗数据估算')
                    player_top.addWidget(mvp_badge)
                if player_is_target:
                    player_top.addWidget(label('查询的人', 'queryTargetBadge'))
                elimination = self._player_elimination_state(player)
                elimination_badge = label(
                    '已淘汰' if elimination == 'eliminated' else '未淘汰',
                    'eliminationBadge')
                elimination_badge.setProperty('elimination', elimination)
                elimination_badge.setToolTip(
                    '该玩家已经发生最终死亡。' if elimination == 'eliminated'
                    else '该玩家没有最终死亡记录。')
                player_top.addWidget(elimination_badge)
                player_top.addStretch(1)
                player_layout.addLayout(player_top)

                stat_specs = [
                    ('最终击杀', ('final_kill', 'finalKill', 'final_kills', 'finalKills')),
                    ('拆床', ('bed_destroy', 'bed_destory', 'bedDestroy', 'beds_destroyed')),
                    ('击杀', ('kill', 'kills', 'total_kills', 'totalKills')),
                    ('死亡', ('death', 'deaths')),
                    ('最终死亡', ('final_death', 'finalDeath', 'final_deaths', 'finalDeaths')),
                    ('伤害', ('damage', 'damage_dealt', 'damageDealt', 'total_damage', 'totalDamage')),
                    ('绿宝石', ('emerald', 'emeralds', 'emerald_collected', 'emeralds_collected', 'emeraldsPicked')),
                    ('钻石', ('diamond', 'diamonds', 'diamond_collected', 'diamonds_collected', 'diamondsPicked')),
                    ('放置', ('place', 'blocks_placed')),
                    ('破坏', ('break', 'blocks_broken')),
                    ('承伤', ('interception', 'damage_taken')),
                ]
                # The detail view is a fixed metric grid.  The API omits
                # counters whose value is zero, so filtering out ``None``
                # here made each player's card change shape and hid metrics
                # that were actually known to be zero.  Normalize every
                # metric to a numeric value and keep all chips visible;
                # malformed/absent counters are presented as 0 as requested.
                available_stats = []
                for stat_name, keys in stat_specs:
                    raw_value = self._player_stat_value(player, stat_name, keys)
                    if raw_value is None:
                        raw_value = 0
                    try:
                        numeric_value = float(str(raw_value).replace(',', '').strip())
                        if numeric_value != numeric_value or numeric_value < 0:
                            numeric_value = 0.0
                    except (TypeError, ValueError, OverflowError):
                        numeric_value = 0.0
                    if numeric_value.is_integer():
                        numeric_value = int(numeric_value)
                    available_stats.append((stat_name, numeric_value,
                                            self._display_stat_value(numeric_value)))
                stats_grid = QGridLayout()
                stats_grid.setContentsMargins(0, 0, 0, 0)
                stats_grid.setHorizontalSpacing(6)
                stats_grid.setVerticalSpacing(6)
                for column in range(4):
                    stats_grid.setColumnStretch(column, 1)
                for index, (stat_name, raw_value, value) in enumerate(available_stats):
                    threshold_hit = self._threshold_hit(stat_name, raw_value)
                    chip = self._stat_chip(stat_name, value)
                    chip.setProperty('thresholdHit', 'true' if threshold_hit else 'false')
                    metric_tone = 'highlight' if threshold_hit else self._stat_tone(stat_name)
                    chip.setProperty('metricTone', metric_tone)
                    metric_icon = chip.findChild(QLabel, 'statMetricIcon')
                    if metric_icon is not None:
                        metric_icon.setProperty('metricTone', metric_tone)
                    self._refresh_stat_chip_icon(chip)
                    if threshold_hit:
                        threshold = DETAIL_METRIC_THRESHOLDS[stat_name][0]
                        chip.setToolTip(f'{stat_name} 超过高光阈值 {threshold}')
                    stats_grid.addWidget(chip, index // 4, index % 4)
                player_layout.addLayout(stats_grid)
                resources = player.get('pick_up')
                if isinstance(resources, dict) and resources:
                    resource_text = self._format_player_stats({'pick_up': resources})
                    player_layout.addWidget(label(resource_text, 'cardHint'))
                team_card_layout.addWidget(player_row)

            self.match_detail_layout.addWidget(team_card)
            self._fade_detail_widget(team_card, duration=260 + team_index * 35)

        if not teams or not players:
            self.match_detail_layout.addWidget(label('这场对局没有返回可展示的队伍统计。', 'muted'))
        self.match_detail_layout.addStretch(1)
        self.match_status.setText('单局详情已加载。')

    def _set_match_detail_placeholder(self, title='正在加载', message='正在整理本场对局。'):
        if not hasattr(self, 'match_detail_layout'):
            return
        self._clear_detail_layout()
        self.match_detail_title.setText(title)
        placeholder = QFrame()
        placeholder.setObjectName('featureCard')
        placeholder_layout = QVBoxLayout(placeholder)
        placeholder_layout.setContentsMargins(20, 24, 20, 24)
        placeholder_layout.setSpacing(10)
        placeholder_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder_layout.addWidget(icon_tile('layers', 'heroIconTile', '#bfe1ff', 25, 54), 0, Qt.AlignmentFlag.AlignCenter)
        placeholder_layout.addWidget(label(title, 'cardTitle'), 0, Qt.AlignmentFlag.AlignCenter)
        hint = label(message, 'muted')
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder_layout.addWidget(hint)
        self.match_detail_layout.addStretch(1)
        self.match_detail_layout.addWidget(placeholder)
        self.match_detail_layout.addStretch(1)
        self.match_detail_state.setText('战局概览 · 玩家表现 · MVP')

    @staticmethod
    def _match_outcome(value):
        if isinstance(value, bool):
            return 'win' if value else 'loss'
        if isinstance(value, (int, float)):
            if value == 1:
                return 'win'
            if value == 0:
                return 'loss'
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized in {'true', '1', 'win', 'won', 'victory', '胜利', '获胜', '胜', '是'}:
                return 'win'
            if normalized in {'false', '0', 'loss', 'lost', 'defeat', '失败', '未获胜', '负', '否'}:
                return 'loss'
        return None

    @staticmethod
    def _find_winner_team(winner_name, teams):
        if not winner_name:
            return None
        normalized = winner_name.strip().casefold()
        for team_name in teams:
            candidate = str(team_name).strip().casefold()
            if candidate == normalized or normalized in candidate or candidate in normalized:
                return str(team_name)
        return None

    @staticmethod
    def _team_tint(team_name, index=0):
        """Resolve API team labels to a stable visual identity."""
        normalized = str(team_name or '').strip().casefold()
        aliases = (
            ('red', ('红', 'red', '红队', 'team red')),
            ('blue', ('蓝', 'blue', '蓝队', 'team blue')),
            ('yellow', ('黄', 'yellow', '黄队', 'gold', 'team yellow')),
            ('green', ('绿', 'green', '绿队', 'team green')),
        )
        for tint, words in aliases:
            if any(word in normalized for word in words):
                return tint
        return ('red', 'blue', 'yellow', 'green')[index % 4]

    @classmethod
    def _player_elimination_state(cls, player):
        """Return active/eliminated for the explicit player status fields."""
        for key in ('eliminated', 'is_eliminated', 'isEliminated'):
            if key not in player:
                continue
            value = player.get(key)
            if isinstance(value, bool):
                return 'eliminated' if value else 'active'
            if isinstance(value, (int, float)):
                return 'eliminated' if value else 'active'
            normalized = str(value).strip().casefold()
            if normalized in {'true', '1', 'yes', 'dead', 'eliminated', '淘汰', '已淘汰'}:
                return 'eliminated'
            if normalized in {'false', '0', 'no', 'alive', 'active', '存活', '未淘汰'}:
                return 'active'
        status = str(player.get('status') or player.get('state') or '').strip().casefold()
        if any(word in status for word in ('eliminated', 'dead', '淘汰', '死亡')):
            return 'eliminated'
        if any(word in status for word in ('alive', 'active', '存活', '在场')):
            return 'active'
        # BedWars APIs commonly expose final_death as the only elimination
        # signal. Keep the badge deterministic when other fields are absent.
        return 'eliminated' if cls._numeric_stat(player, 'final_death', 'finalDeath') > 0 else 'active'

    @staticmethod
    def _player_identity(player):
        return str(player.get('player_uuid') or player.get('player_name') or id(player)).casefold()

    @staticmethod
    def _has_explicit_mvp(player):
        for key in ('mvp', 'is_mvp', 'isMvp', 'best_player'):
            value = player.get(key)
            if isinstance(value, bool) and value:
                return True
            if isinstance(value, (int, float)) and value == 1:
                return True
            if isinstance(value, str) and value.strip().casefold() in {'true', '1', 'yes', 'mvp'}:
                return True
        return False

    @staticmethod
    def _normalized_field_key(value):
        """Normalize API field names without relying on one provider spelling."""
        return ''.join(char for char in str(value).casefold() if char.isalnum())

    @classmethod
    def _player_stat_value(cls, player, title, keys):
        """Read a player metric from flat fields or the API's resource map."""
        if not isinstance(player, dict):
            return None
        normalized_aliases = {cls._normalized_field_key(key) for key in keys}
        for key, value in player.items():
            if cls._normalized_field_key(key) in normalized_aliases and value is not None:
                return value
        if title in {'绿宝石', '钻石'}:
            containers = ('pick_up', 'pickup', 'pickUp', 'resources', 'resource', 'items', 'collected')
            for container_name in containers:
                container = next(
                    (value for key, value in player.items()
                     if cls._normalized_field_key(key) == cls._normalized_field_key(container_name)),
                    None,
                )
                if not isinstance(container, dict):
                    continue
                for key, value in container.items():
                    if cls._normalized_field_key(key) in normalized_aliases and value is not None:
                        if isinstance(value, dict):
                            value = next(
                                (nested for nested_key, nested in value.items()
                                 if cls._normalized_field_key(nested_key) in {'amount', 'count', 'total', 'value'}),
                                value,
                            )
                        return value
        return None

    @classmethod
    def _threshold_hit(cls, title, value):
        threshold_info = DETAIL_METRIC_THRESHOLDS.get(title)
        if threshold_info is None:
            return False
        try:
            number = float(str(value).replace(',', '').strip())
        except (TypeError, ValueError):
            return False
        return number == number and number > threshold_info[0]

    @classmethod
    def _numeric_stat(cls, player, *keys):
        value = cls._player_stat_value(player, '', keys)
        try:
            number = float(str(value).replace(',', '').strip())
        except (TypeError, ValueError):
            return 0.0
        return number if number == number else 0.0

    @classmethod
    def _mvp_score(cls, player):
        return (
            cls._numeric_stat(player, 'final_kill', 'finalKill') * 5
            + cls._numeric_stat(player, 'bed_destroy', 'bed_destory', 'bedDestroy') * 5
            + cls._numeric_stat(player, 'kill', 'kills') * 2
            + cls._numeric_stat(player, 'damage', 'damage_dealt') * 0.01
            - cls._numeric_stat(player, 'death', 'deaths')
            - cls._numeric_stat(player, 'final_death', 'finalDeath') * 3
        )

    @staticmethod
    def _display_stat_value(value):
        if isinstance(value, float):
            return str(int(value)) if value.is_integer() else str(round(value, 1))
        return str(value)

    def _stat_chip(self, title, value):
        chip = QFrame()
        chip.setObjectName('statChip')
        chip_layout = QVBoxLayout(chip)
        chip_layout.setContentsMargins(9, 7, 9, 7)
        chip_layout.setSpacing(2)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(5)
        icon_name = DETAIL_METRIC_ICONS.get(title)
        if icon_name:
            icon_label = QLabel()
            icon_label.setObjectName('statMetricIcon')
            icon_label.setFixedSize(24, 24)
            icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon_label.setProperty('metricIconName', icon_name)
            icon_label.setProperty('metricTitle', title)
            icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            title_row.addWidget(icon_label)
        title_label = label(title, 'statLabel')
        title_row.addWidget(title_label)
        title_row.addStretch(1)
        value_label = label(str(value), 'statValue')
        value_label.setWordWrap(True)
        chip_layout.addLayout(title_row)
        chip_layout.addWidget(value_label)
        chip.setMinimumHeight(54)
        self._refresh_stat_chip_icon(chip)
        return chip

    def _metric_icon_color(self, tone='neutral'):
        theme = getattr(self, 'current_theme', 'light')
        colors = MATCH_ROW_PALETTES.get(theme, MATCH_ROW_PALETTES['light'])
        if tone == 'highlight':
            return LEADERBOARD_RANK_PALETTES.get(
                theme, LEADERBOARD_RANK_PALETTES['light'])['1']['border']
        return colors.get(tone if tone in {'positive', 'negative'} else 'neutral',
                          colors['neutral'])

    def _refresh_stat_chip_icon(self, chip):
        icon_label = chip.findChild(QLabel, 'statMetricIcon') if chip is not None else None
        if icon_label is None:
            return
        icon_name = icon_label.property('metricIconName') or 'command'
        tone = icon_label.property('metricTone') or chip.property('metricTone') or 'neutral'
        icon_label.setPixmap(_render_svg_icon(
            icon_name, self._metric_icon_color(str(tone)), 20, 2.15))

    @staticmethod
    def _stat_tone(title):
        """Map player metrics to a restrained semantic color layer."""
        if title in {'死亡', '最终死亡', '承伤'}:
            return 'negative'
        if title in {'击杀', '最终击杀', '拆床', '伤害', '放置', '破坏'}:
            return 'positive'
        return 'neutral'

    def _clear_detail_layout(self):
        self._stop_detail_animations()
        # Detail pages contain many nested cards and SVG metric icons.  Keep
        # the content viewport frozen while the old tree is removed; the
        # single queued update below paints the completed replacement once,
        # instead of exposing every intermediate layout pass to the window.
        content = getattr(self, 'match_detail_content', None)
        if content is not None:
            content.setUpdatesEnabled(False)
        while self.match_detail_layout.count():
            item = self.match_detail_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if content is not None:
            QTimer.singleShot(0, self._restore_detail_updates)

    def _restore_detail_updates(self):
        content = getattr(self, 'match_detail_content', None)
        if content is not None:
            content.setUpdatesEnabled(True)
            content.update()

    def _fade_detail_widget(self, widget, duration=220):
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(0.0)
        widget.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b'opacity', self)
        animation.setDuration(self._animation_duration(duration))
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        entry = (animation, widget, effect)
        self._detail_animations.append(entry)

        def finish():
            try:
                if widget.graphicsEffect() is effect:
                    widget.setGraphicsEffect(None)
            except RuntimeError:
                pass
            if entry in self._detail_animations:
                self._detail_animations.remove(entry)
            animation.deleteLater()

        animation.finished.connect(finish)
        animation.start()

    def _stop_detail_animations(self):
        for animation, widget, effect in list(self._detail_animations):
            animation.stop()
            try:
                if widget.graphicsEffect() is effect:
                    widget.setGraphicsEffect(None)
            except RuntimeError:
                pass
            animation.deleteLater()
        self._detail_animations.clear()

    @staticmethod
    def _format_player_stats(player):
        fields = [
            ('kill', '击杀'), ('final_kill', '最终击杀'), ('death', '死亡'),
            ('final_death', '最终死亡'), ('bed_destory', '拆床'),
            ('bed_destroy', '拆床'), ('damage', '伤害'), ('place', '放置'),
            ('break', '破坏'), ('interception', '承伤'),
        ]
        values = []
        for key, title in fields:
            if key in player:
                value = player[key]
                if isinstance(value, (int, float)) and float(value).is_integer():
                    value = int(value)
                elif isinstance(value, float):
                    value = round(value, 1)
                values.append(f'{title} {value}')
        resources = player.get('pick_up')
        if isinstance(resources, dict) and resources:
            resource_names = {
                'IRON_INGOT': '铁', 'GOLD_INGOT': '金',
                'DIAMOND': '钻石', 'EMERALD': '绿宝石',
            }
            resource_text = ' / '.join(
                f'{resource_names.get(name, name)} {int(amount) if isinstance(amount, (int, float)) and float(amount).is_integer() else amount}'
                for name, amount in resources.items())
            values.append(f'资源 {resource_text}')
        return '  ·  '.join(values)

    def _progress_bar(self):
        from PySide6.QtWidgets import QProgressBar
        bar = QProgressBar()
        bar.setTextVisible(False)
        bar.setFixedHeight(5)
        return bar

    # ------------------------------------------------------------------
    # 大厅聊天
    # ------------------------------------------------------------------
    def _build_lobby_chat_page(self):
        """创建主题自适应的公共大厅聊天页。

        消息气泡使用普通 QWidget 绘制，避免引入 websocket/第三方依赖；
        后端可以先用 Render 的免费 Web Service 提供三个 JSON 接口，后续
        也可以在设置中替换为自托管地址。
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(14)

        heading = QVBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(label('BEDWARS / LOBBY CHAT', 'eyebrow'))
        heading.addWidget(label('大厅聊天', 'pageTitle'))
        heading.addWidget(label('公共大厅频道 · 仅使用登录游戏 ID 发言，没有私聊。', 'pageHint'))
        layout.addLayout(heading)

        overview = QFrame()
        overview.setObjectName('chatOverviewCard')
        overview_layout = QHBoxLayout(overview)
        overview_layout.setContentsMargins(16, 12, 16, 12)
        overview_layout.setSpacing(12)
        overview_layout.addWidget(icon_tile('chat', 'sectionIcon', '#bfe1ff', 15, 30))
        status_copy = QVBoxLayout()
        status_copy.setSpacing(2)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(label('公共大厅', 'cardTitle'))
        self.chat_connection_badge = label('准备连接', 'chatConnectionBadge')
        self.chat_connection_badge.setObjectName('chatConnectionBadge')
        title_row.addWidget(self.chat_connection_badge)
        title_row.addStretch(1)
        status_copy.addLayout(title_row)
        self.chat_server_hint = label('', 'cardHint')
        self.chat_server_hint.setObjectName('chatServerHint')
        status_copy.addWidget(self.chat_server_hint)
        overview_layout.addLayout(status_copy, 1)
        online_box = QFrame()
        online_box.setObjectName('chatOnlineCard')
        online_layout = QVBoxLayout(online_box)
        online_layout.setContentsMargins(12, 7, 12, 7)
        online_layout.setSpacing(0)
        online_layout.addWidget(label('实时在线', 'chatOnlineCaption'))
        self.chat_online_count = label('—', 'chatOnlineCount')
        self.chat_online_count.setObjectName('chatOnlineCount')
        online_layout.addWidget(self.chat_online_count)
        overview_layout.addWidget(online_box)
        # 在线人数是大厅状态的第一视觉锚点，固定在卡片左上起始位置。
        overview_layout.insertWidget(0, online_box)
        refresh = self.button('刷新', self._poll_lobby_chat)
        refresh.setObjectName('chatRefreshButton')
        refresh.setProperty('themeIconName', 'refresh')
        overview_layout.addWidget(refresh)
        layout.addWidget(overview)

        chat_card = QFrame()
        chat_card.setObjectName('chatCard')
        chat_layout = QVBoxLayout(chat_card)
        chat_layout.setContentsMargins(14, 13, 14, 14)
        chat_layout.setSpacing(10)
        message_header = QHBoxLayout()
        message_header.setSpacing(7)
        message_header.addWidget(icon_tile('layers', 'sectionIcon', '#bfe1ff', 13, 24))
        message_header.addWidget(label('大厅消息', 'cardTitle'))
        message_header.addStretch(1)
        self.chat_status = label('输入登录 ID 后即可加入大厅。', 'cardHint')
        self.chat_status.setObjectName('chatStatus')
        message_header.addWidget(self.chat_status)
        chat_layout.addLayout(message_header)

        self.chat_scroll = QScrollArea()
        self.chat_scroll.setObjectName('chatScroll')
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chat_messages_host = QWidget()
        self.chat_messages_host.setObjectName('chatMessagesHost')
        self.chat_messages_layout = QVBoxLayout(self.chat_messages_host)
        self.chat_messages_layout.setContentsMargins(3, 3, 6, 3)
        self.chat_messages_layout.setSpacing(7)
        self.chat_empty_label = label('还没有消息，成为第一个打招呼的人吧。', 'chatEmpty')
        self.chat_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.chat_messages_layout.addWidget(self.chat_empty_label)
        self.chat_messages_layout.addStretch(1)
        self.chat_scroll.setWidget(self.chat_messages_host)
        chat_layout.addWidget(self.chat_scroll, 1)

        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        self.chat_emoji_button = QPushButton('😊')
        self.chat_emoji_button.setObjectName('chatEmojiButton')
        self.chat_emoji_button.setFixedSize(42, 36)
        self.chat_emoji_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chat_emoji_button.setToolTip('插入表情')
        self.chat_emoji_button.clicked.connect(self._toggle_chat_emoji_panel)
        input_row.addWidget(self.chat_emoji_button)
        self.chat_input = QLineEdit()
        self.chat_input.setObjectName('themedInput')
        self.chat_input.setPlaceholderText('输入大厅消息（最多 240 字）')
        self.chat_input.setMaxLength(240)
        self.chat_input.returnPressed.connect(self._send_lobby_chat)
        input_row.addWidget(self.chat_input, 1)
        self.chat_send_button = self.button('发送', self._send_lobby_chat, True)
        self.chat_send_button.setObjectName('chatSendButton')
        self.chat_send_button.setProperty('themeIconName', 'send')
        input_row.addWidget(self.chat_send_button)
        chat_layout.addLayout(input_row)
        layout.addWidget(chat_card, 1)

        self._chat_poll_timer = QTimer(self)
        self._chat_poll_timer.setInterval(7000)
        self._chat_poll_timer.timeout.connect(self._poll_lobby_chat)
        self._refresh_chat_identity()
        self._render_lobby_messages([])
        return page

    def _ensure_chat_emoji_panel(self):
        """Create the small built-in emoji picker once, without dependencies."""
        popup = getattr(self, 'chat_emoji_popup', None)
        if popup is not None:
            return popup
        popup = QFrame(self, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        popup.setObjectName('chatEmojiPopup')
        popup.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        popup_layout = QGridLayout(popup)
        popup_layout.setContentsMargins(9, 9, 9, 9)
        popup_layout.setHorizontalSpacing(3)
        popup_layout.setVerticalSpacing(3)
        # Keep this intentionally small and platform independent.  These are
        # regular Unicode characters, so the picker works in a packaged EXE
        # without downloading image assets or installing an emoji library.
        emojis = (
            '😀', '😃', '😄', '😁', '😆', '😅',
            '😂', '🙂', '🙃', '😉', '😊', '😎',
            '🤝', '🎉', '🔥', '💯', '👍', '👏',
            '❤️', '✨', '😭', '😡', '😴', '🤔',
        )
        columns = 6
        for index, emoji in enumerate(emojis):
            button = QPushButton(emoji, popup)
            button.setObjectName('chatEmojiItem')
            button.setFixedSize(34, 34)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(f'插入 {emoji}')
            button.clicked.connect(
                lambda checked=False, value=emoji: self._insert_chat_emoji(value))
            popup_layout.addWidget(button, index // columns, index % columns)
        self.chat_emoji_popup = popup
        return popup

    def _toggle_chat_emoji_panel(self):
        popup = self._ensure_chat_emoji_panel()
        if popup.isVisible():
            popup.hide()
            return
        button = getattr(self, 'chat_emoji_button', None)
        if button is None:
            return
        popup.adjustSize()
        origin = button.mapToGlobal(QPoint(0, 0))
        x = origin.x()
        y = origin.y() - popup.height() - 8
        screen = QApplication.screenAt(origin) or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            if y < available.top():
                y = origin.y() + button.height() + 8
            if x + popup.width() > available.right():
                x = max(available.left(), available.right() - popup.width())
        popup.move(x, y)
        popup.show()
        popup.raise_()

    def _insert_chat_emoji(self, emoji):
        if not hasattr(self, 'chat_input'):
            return
        self.chat_input.insert(str(emoji or ''))
        self.chat_input.setFocus(Qt.FocusReason.PopupFocusReason)
        popup = getattr(self, 'chat_emoji_popup', None)
        if popup is not None:
            popup.hide()

    def _initialize_chat_sound(self):
        """Prepare a tiny Codex-style chime without adding a binary asset."""
        try:
            path = os.path.join(tempfile.gettempdir(), 'devking-lobby-message.wav')
            sample_rate = 24000
            duration = 0.22
            frames = int(sample_rate * duration)
            with wave.open(path, 'wb') as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(sample_rate)
                data = bytearray()
                for index in range(frames):
                    seconds = index / sample_rate
                    frequency = 660.0 if seconds < 0.105 else 880.0
                    local = seconds if seconds < 0.105 else seconds - 0.105
                    envelope = min(1.0, local / 0.012, max(0.0, (0.105 - local) / 0.035))
                    envelope *= max(0.0, min(1.0, (duration - seconds) / 0.025))
                    value = int(0.28 * envelope * math.sin(2 * math.pi * frequency * seconds) * 32767)
                    data.extend(struct.pack('<h', value))
                stream.writeframes(bytes(data))
            self._chat_sound_path = path
            if QSoundEffect is not None:
                effect = QSoundEffect(self)
                effect.setLoopCount(1)
                effect.setVolume(self.chat_sound_volume / 100.0)
                effect.setSource(QUrl.fromLocalFile(path))
                self._chat_sound_effect = effect
        except Exception:
            self._chat_sound_path = ''
            self._chat_sound_effect = None

    def _set_chat_sound_enabled(self, enabled):
        self.chat_sound_enabled = bool(enabled)
        self._settings.setValue(CHAT_SOUND_ENABLED_KEY, self.chat_sound_enabled)
        self._settings.sync()
        if hasattr(self, 'chat_sound_hint'):
            self.chat_sound_hint.setText(
                '已开启，新消息会播放提示音。' if self.chat_sound_enabled
                else '已关闭，不会播放大厅消息提示音。')

    def _set_chat_sound_volume(self, value):
        try:
            self.chat_sound_volume = max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return
        self._settings.setValue(CHAT_SOUND_VOLUME_KEY, self.chat_sound_volume)
        self._settings.sync()
        if self._chat_sound_effect is not None:
            self._chat_sound_effect.setVolume(self.chat_sound_volume / 100.0)
        if hasattr(self, 'chat_sound_volume_value'):
            self.chat_sound_volume_value.setText(f'{self.chat_sound_volume}%')

    def _play_chat_sound(self):
        if self._closing or not self.chat_sound_enabled or self.chat_sound_volume <= 0:
            return
        effect = self._chat_sound_effect
        if effect is not None:
            effect.setVolume(self.chat_sound_volume / 100.0)
            effect.stop()
            effect.play()
            return
        # Fallback for a build without Qt Multimedia.  The generated WAV is
        # still a system-local asset and PlaySound is asynchronous on Windows.
        if self._chat_sound_path and sys.platform.startswith('win'):
            try:
                import winsound
                winsound.PlaySound(
                    self._chat_sound_path,
                    winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            except Exception:
                QApplication.beep()
        else:
            QApplication.beep()

    def _chat_bound_game_id(self):
        value = str(self._settings.value(GAME_ID_SETTINGS_KEY, self._game_id) or '').strip()
        # Keep the legacy chat key in lockstep with the verified login key.
        # Older builds could leave these two settings different after a
        # rebind, which made the visible identity and WebSocket identity drift.
        if value and str(self._settings.value('chat/gameId', '') or '').strip() != value:
            self._settings.setValue('chat/gameId', value)
            self._settings.sync()
        return value

    def _restore_chat_session_cache(self):
        raw = self._settings.value(CHAT_SESSION_CACHE_KEY, '')
        if isinstance(raw, QByteArray):
            raw = bytes(raw).decode('utf-8', errors='ignore')
        if not raw:
            return
        try:
            payload = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        if not isinstance(payload, list):
            return
        messages = [item for item in payload[-80:] if isinstance(item, dict)]
        if not messages:
            return
        self._chat_messages = messages
        self._chat_message_keys = {
            self._chat_message_key(item) for item in messages}
        self._render_lobby_messages(messages)

    def _save_chat_session_cache(self):
        if not self._chat_messages:
            self._settings.remove(CHAT_SESSION_CACHE_KEY)
            return
        try:
            raw = json.dumps(self._chat_messages[-80:], ensure_ascii=False,
                             separators=(',', ':'))
        except (TypeError, ValueError):
            return
        self._settings.setValue(CHAT_SESSION_CACHE_KEY, raw)
        self._settings.sync()

    def _clear_chat_session_cache(self):
        self._settings.remove(CHAT_SESSION_CACHE_KEY)
        self._settings.sync()

    def game_id(self):
        """返回当前绑定的布吉岛游戏 ID，供启动登录流程复用。"""
        return self._chat_bound_game_id()

    def set_game_id(self, game_id):
        """统一写入启动登录和大厅聊天共用的游戏 ID。"""
        game_id = str(game_id or '').strip()
        identity_changed = game_id != self._game_id
        self._game_id = game_id
        if hasattr(self, '_player_id'):
            self._player_id = game_id
        self._chat_session_id = ''
        self._settings.setValue(GAME_ID_SETTINGS_KEY, game_id)
        self._settings.setValue('chat/gameId', game_id)
        self._settings.sync()
        if hasattr(self, 'match_player_input'):
            self.match_player_input.setText(game_id)
        if hasattr(self, 'chat_account_input'):
            self.chat_account_input.setText(game_id)
        if identity_changed and hasattr(self, '_chat_client'):
            self._chat_client.stop()
            self._chat_pending_message = ''
            self._chat_sending_message = ''
            if hasattr(self, 'chat_input'):
                self.chat_input.clear()
            self._chat_messages = []
            self._chat_message_keys.clear()
            self._clear_chat_session_cache()
        self._refresh_chat_identity()
        if identity_changed and game_id and hasattr(self, 'chat_input'):
            self._poll_lobby_chat()

    def _refresh_chat_identity(self, preserve_status=False):
        if not hasattr(self, 'chat_input'):
            return
        player_id = self._chat_bound_game_id()
        if player_id != self._game_id:
            self._game_id = player_id
            self._chat_session_id = ''
            self._chat_client.stop()
            # A message typed under the previous identity must never be sent
            # after a rebind.  The transport stop above may synchronously
            # report a failure; clear the editor after that callback returns.
            self._chat_pending_message = ''
            self._chat_sending_message = ''
            self.chat_input.clear()
            self._chat_messages = []
            self._chat_message_keys.clear()
            self._clear_chat_session_cache()
        configured = bool(player_id)
        busy = bool(self._chat_sending_message or self._chat_pending_message)
        self.chat_input.setEnabled(configured and not busy)
        self.chat_send_button.setEnabled(configured and not busy)
        if hasattr(self, 'chat_emoji_button'):
            self.chat_emoji_button.setEnabled(configured and not busy)
        connection_state = self.chat_connection_badge.text()
        if configured:
            self.chat_input.setPlaceholderText(f'以 {player_id} 发言 · 输入大厅消息')
            if not preserve_status:
                self.chat_status.setText(f'当前身份：{player_id} · 公共频道')
            if not preserve_status or connection_state in {'未登录', '准备连接', '已绑定 ID'}:
                self.chat_connection_badge.setText('已绑定 ID')
        else:
            self.chat_input.setPlaceholderText('请先登录游戏 ID，再输入大厅消息')
            self.chat_status.setText('请先登录游戏 ID 后加入大厅。')
            self.chat_connection_badge.setText('未登录')
        self.chat_server_hint.setText(
            'Supabase Realtime 公共频道 · 本次运行缓存，关闭软件后清除')

    @staticmethod
    def _chat_payload_messages(payload):
        if isinstance(payload, list):
            return payload, None
        if not isinstance(payload, dict):
            return [], None
        messages = payload.get('messages')
        if not isinstance(messages, (list, tuple)):
            for key in ('items', 'data', 'results'):
                candidate = payload.get(key)
                if isinstance(candidate, dict):
                    candidate = candidate.get('messages') or candidate.get('items')
                if isinstance(candidate, (list, tuple)):
                    messages = candidate
                    break
        online = payload.get('online', payload.get('online_count', payload.get('onlinePlayers')))
        try:
            online = int(online) if online is not None else None
        except (TypeError, ValueError):
            online = None
        return list(messages or []), online

    @staticmethod
    def _chat_message_key(item):
        if not isinstance(item, dict):
            return str(item)
        identifier = item.get('id') or item.get('message_id') or item.get('uuid')
        if identifier:
            return str(identifier)
        return '|'.join(str(item.get(key) or '') for key in ('player_id', 'username', 'message', 'created_at'))

    def _poll_lobby_chat(self):
        if self._closing:
            return
        self._refresh_chat_identity(preserve_status=True)
        player_id = self._chat_bound_game_id()
        if not player_id:
            self.chat_connection_badge.setText('未登录')
            self._chat_client.stop()
            return
        if self._chat_client.player_id != player_id or not self._chat_client.joined:
            self._chat_client.start(player_id)

    def _send_lobby_chat(self):
        if self._closing:
            return
        player_id = self._chat_bound_game_id()
        message = self.chat_input.text().strip()
        if not player_id:
            self.chat_status.setText('请先登录游戏 ID，聊天身份会与查询玩家同步。')
            return
        if not message:
            self.chat_input.setFocus()
            return
        if self._chat_sending_message:
            return
        self._refresh_chat_identity(preserve_status=True)
        # A rebind can happen while the old socket is still connected. Never
        # let that socket publish under the previous player's ID.
        if self._chat_client.player_id != player_id:
            self._chat_pending_message = message
            self.chat_status.setText('正在切换大厅身份…')
            self._chat_client.start(player_id)
            return
        if not self._chat_client.joined:
            self._chat_pending_message = message
            self._refresh_chat_identity(preserve_status=True)
            self.chat_status.setText('正在加入公共大厅…')
            self._poll_lobby_chat()
            return
        self._begin_chat_message_send(message)

    def _begin_chat_message_send(self, message):
        message = str(message or '').strip()
        if not message or self._chat_sending_message:
            return False
        self._chat_sending_message = message
        self._refresh_chat_identity(preserve_status=True)
        self.chat_status.setText('正在发送消息……')
        if self._chat_client.send_message(message):
            return True
        self._chat_sending_message = ''
        self._refresh_chat_identity(preserve_status=True)
        self.chat_status.setText('发送失败：当前大厅连接尚未就绪。')
        return False

    def _on_supabase_chat_status(self, status):
        if self._closing or not hasattr(self, 'chat_connection_badge'):
            return
        self.chat_connection_badge.setText(str(status))
        if status == '在线':
            self.chat_status.setText('已连接 Supabase Realtime · 公共频道')
            pending = self._chat_pending_message
            self._chat_pending_message = ''
            if pending:
                self._begin_chat_message_send(pending)
        elif status == '连接中…':
            self.chat_status.setText('正在连接 Supabase Realtime…')
        elif '重连' in status:
            self.chat_status.setText('大厅连接暂时中断，正在自动重连…')

    def _on_supabase_chat_error(self, message):
        if self._closing or not hasattr(self, 'chat_status'):
            return
        self.chat_connection_badge.setText('离线')
        if self._chat_pending_message and not self._chat_sending_message:
            pending = self._chat_pending_message
            self._chat_pending_message = ''
            self.chat_input.setText(pending)
            self._refresh_chat_identity(preserve_status=True)
        self.chat_status.setText(str(message or '暂时无法连接大厅。')[:180])

    def _on_supabase_message_sent(self, payload):
        if self._closing or not isinstance(payload, dict):
            return
        text = str(payload.get('message') or payload.get('text') or '').strip()
        if not self._chat_sending_message or text != self._chat_sending_message:
            return
        self._chat_sending_message = ''
        self.chat_input.clear()
        self._refresh_chat_identity(preserve_status=True)
        self.chat_status.setText('消息已发送 · 公共大厅可见')

    def _on_supabase_message_failed(self, payload, reason):
        if self._closing:
            return
        text = ''
        if isinstance(payload, dict):
            text = str(payload.get('message') or payload.get('text') or '').strip()
        if ((self._chat_sending_message or self._chat_pending_message) and text and
                text != (self._chat_sending_message or self._chat_pending_message)):
            return
        restore = text or self._chat_sending_message or self._chat_pending_message
        self._chat_sending_message = ''
        self._chat_pending_message = ''
        if restore:
            self.chat_input.setText(restore)
        self._refresh_chat_identity(preserve_status=True)
        self.chat_status.setText(f'发送失败：{str(reason or "消息未确认。")[:150]}')

    def _on_supabase_presence_changed(self, count):
        if hasattr(self, 'chat_online_count'):
            self.chat_online_count.setText(str(max(0, int(count))))

    def _on_supabase_chat_message(self, payload):
        if self._closing or not isinstance(payload, dict):
            return
        # Realtime 没有历史消息；把当前运行收到的消息写入本机临时缓存，
        # 方便页面重绘或切换后恢复，关闭软件时会主动清除。
        item = dict(payload)
        item.setdefault('message', item.get('text', ''))
        item.setdefault('player_id', item.get('username', '未知玩家'))
        item.setdefault('created_at', datetime.now().astimezone().isoformat(timespec='seconds'))
        sender = str(item.get('player_id') or item.get('username') or '').strip()
        message_text = str(item.get('message') or item.get('text') or '').strip()
        # The Realtime broadcast includes our own message before the transport
        # emits its ACK.  Do not turn that self echo into a second notification.
        own_echo = (
            sender and sender == self._chat_bound_game_id() and
            bool(self._chat_sending_message) and
            message_text == self._chat_sending_message)
        key = self._chat_message_key(item)
        if key in self._chat_message_keys:
            return
        self._chat_message_keys.add(key)
        self._chat_messages.append(item)
        # Keep the canonical payloads intact.  Rendering must not replace
        # these with presentation-only dictionaries and lose IDs/timestamps.
        self._chat_messages = self._chat_messages[-80:]
        self._chat_message_keys = {
            self._chat_message_key(message) for message in self._chat_messages}
        self._save_chat_session_cache()
        self._render_lobby_messages(self._chat_messages)
        if not own_echo:
            self._play_chat_sound()
        self.chat_status.setText('已同步实时消息 · 公共大厅可见')

    def _render_lobby_messages(self, messages):
        if not hasattr(self, 'chat_messages_layout'):
            return
        normalized = []
        for item in list(messages or [])[-80:]:
            if isinstance(item, dict):
                text = str(item.get('message') or item.get('text') or '').strip()
                sender = str(item.get('player_id') or item.get('username') or item.get('sender') or '未知玩家').strip()
                stamp = str(item.get('created_at') or item.get('timestamp') or item.get('time') or '').strip()
                key = self._chat_message_key(item)
            else:
                text, sender, stamp, key = str(item).strip(), '未知玩家', '', str(item)
            if text:
                normalized.append({'text': text, 'sender': sender or '未知玩家', 'stamp': stamp, 'key': key})
        # The empty-state label is reused; deleting it here makes the next
        # message fail once Qt processes DeferredDelete after startup.
        self.chat_messages_host.setUpdatesEnabled(False)
        while self.chat_messages_layout.count():
            item = self.chat_messages_layout.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self.chat_empty_label:
                widget.hide()
                widget.deleteLater()
        self.chat_empty_label.setVisible(not normalized)
        if not normalized:
            self.chat_messages_layout.addWidget(self.chat_empty_label)
        for item in normalized:
            bubble = QFrame()
            bubble.setObjectName('chatMessageBubble')
            bubble.setProperty(
                'mine', 'true' if str(item['sender']).casefold() ==
                self._chat_bound_game_id().casefold() else 'false')
            bubble.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            bubble_layout = QVBoxLayout(bubble)
            bubble_layout.setContentsMargins(12, 8, 12, 8)
            bubble_layout.setSpacing(3)
            meta = QHBoxLayout()
            meta.setSpacing(7)
            author = label(item['sender'], 'chatMessageAuthor')
            author.setTextFormat(Qt.TextFormat.PlainText)
            author.setObjectName('chatMessageAuthor')
            meta.addWidget(author)
            if item['stamp']:
                stamp = item['stamp']
                try:
                    stamp = datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone().strftime('%H:%M')
                except (ValueError, TypeError, OverflowError):
                    stamp = stamp[:16]
                meta.addWidget(label(stamp, 'chatMessageTime'))
            meta.addStretch(1)
            bubble_layout.addLayout(meta)
            body = label(item['text'], 'chatMessageText')
            body.setTextFormat(Qt.TextFormat.PlainText)
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            bubble_layout.addWidget(body)
            self.chat_messages_layout.addWidget(bubble)
        self.chat_messages_layout.addStretch(1)
        self.chat_messages_host.setUpdatesEnabled(True)
        QTimer.singleShot(0, lambda: self.chat_scroll.verticalScrollBar().setValue(
            self.chat_scroll.verticalScrollBar().maximum()))

    def _build_placeholder(self, title, description, icon, bullets):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(14)
        heading = QVBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(label('TOOL MODULE', 'eyebrow'))
        heading.addWidget(label(title, 'pageTitle'))
        heading.addWidget(label(description, 'pageHint'))
        layout.addLayout(heading)

        card = QFrame()
        card.setObjectName('placeholder')
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(30, 28, 30, 28)
        card_layout.setSpacing(13)
        card_layout.addWidget(icon_tile(icon, 'moduleIconTile', '#bfe1ff', 28, 50))
        card_layout.addWidget(label('功能分区已预留', 'section'))
        copy = label('这个页面已经接入主界面，后续可以在这里继续加入具体工具。', 'placeholderText')
        copy.setWordWrap(True)
        card_layout.addWidget(copy)
        card_layout.addSpacing(5)
        for bullet in bullets:
            card_layout.addWidget(label(f'  ·  {bullet}', 'muted'))
        card_layout.addStretch()
        card_layout.addWidget(self.button('返回总览', lambda: self.switch_section('overview')))
        layout.addWidget(card, 1)
        return page

    def _build_taskbar_switcher_page(self):
        """把 TaskbarSwitcher 的窗口列表和热键切换接入快捷工具分区。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(14)

        heading = QVBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(label('QUICK TOOLS / TASKBAR SWITCHER', 'eyebrow'))
        heading.addWidget(label('快捷工具', 'pageTitle'))
        heading.addWidget(label('按主任务栏从右往左列出运行中的窗口，双击即可快速切换。', 'pageHint'))
        layout.addLayout(heading)

        info_card = QFrame()
        info_card.setObjectName('taskbarInfoCard')
        info_layout = QVBoxLayout(info_card)
        info_layout.setContentsMargins(18, 15, 18, 15)
        info_layout.setSpacing(9)
        info_header = QHBoxLayout()
        info_header.addWidget(icon_tile('command', 'sectionIcon', '#bfe1ff', 16, 30))
        info_header.addWidget(label('任务栏快速切换', 'section'))
        info_header.addStretch(1)
        self.taskbar_status = label('正在注册全局快捷键…', 'taskbarStatus')
        self.taskbar_status.setObjectName('taskbarStatus')
        info_header.addWidget(self.taskbar_status)
        info_layout.addLayout(info_header)
        self.taskbar_hotkey_hint = label(
            'Alt+1 到 Alt+0 对应列表从右往左的位置；若快捷键被占用会尝试 Ctrl+Alt 组合。',
            'taskbarHotkeyHint')
        self.taskbar_hotkey_hint.setObjectName('taskbarHotkeyHint')
        self.taskbar_hotkey_hint.setWordWrap(True)
        info_layout.addWidget(self.taskbar_hotkey_hint)
        layout.addWidget(info_card)

        list_card = QFrame()
        list_card.setObjectName('taskbarListCard')
        list_layout = QVBoxLayout(list_card)
        list_layout.setContentsMargins(18, 15, 18, 15)
        list_layout.setSpacing(10)
        toolbar = QHBoxLayout()
        toolbar.addWidget(label('正在运行的窗口', 'section'))
        toolbar.addStretch(1)
        self.taskbar_search = QLineEdit()
        self.taskbar_search.setObjectName('themedInput')
        self.taskbar_search.setPlaceholderText('筛选窗口名称')
        self.taskbar_search.setClearButtonEnabled(True)
        self.taskbar_search.setFixedWidth(220)
        toolbar.addWidget(self.taskbar_search)
        self.taskbar_refresh_button = QPushButton('刷新')
        self.taskbar_refresh_button.setObjectName('taskbarRefresh')
        self.taskbar_refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.taskbar_refresh_button.setMinimumHeight(38)
        self.taskbar_refresh_button.clicked.connect(self._refresh_taskbar_apps)
        toolbar.addWidget(self.taskbar_refresh_button)
        list_layout.addLayout(toolbar)

        self.taskbar_table = QTableWidget(0, 3)
        self.taskbar_table.setObjectName('taskbarTable')
        self.taskbar_table.setHorizontalHeaderLabels(['快捷键', '应用 / 窗口', '任务栏位置'])
        self.taskbar_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.taskbar_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.taskbar_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.taskbar_table.setShowGrid(False)
        self.taskbar_table.setAlternatingRowColors(True)
        self.taskbar_table.setSortingEnabled(False)
        self.taskbar_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.taskbar_table.verticalHeader().setVisible(False)
        self.taskbar_table.verticalHeader().setDefaultSectionSize(42)
        header = self.taskbar_table.horizontalHeader()
        header.setObjectName('taskbarHeader')
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.taskbar_table.itemSelectionChanged.connect(self._sync_taskbar_selection)
        self.taskbar_table.itemDoubleClicked.connect(
            lambda _item: self._switch_selected_taskbar_app())
        list_layout.addWidget(self.taskbar_table, 1)

        actions = QHBoxLayout()
        self.taskbar_notice = label('刷新列表以读取当前任务栏窗口。', 'taskbarNotice')
        self.taskbar_notice.setObjectName('taskbarNotice')
        self.taskbar_notice.setWordWrap(True)
        actions.addWidget(self.taskbar_notice, 1)
        self.taskbar_switch_button = QPushButton('切换到选中窗口')
        self.taskbar_switch_button.setObjectName('taskbarAction')
        self.taskbar_switch_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.taskbar_switch_button.setMinimumHeight(40)
        self.taskbar_switch_button.setEnabled(False)
        self.taskbar_switch_button.clicked.connect(self._switch_selected_taskbar_app)
        actions.addWidget(self.taskbar_switch_button)
        list_layout.addLayout(actions)
        layout.addWidget(list_card, 1)

        self.taskbar_search.textChanged.connect(self._populate_taskbar_table)
        return page

    def _start_taskbar_action(self, action, index=None, expected_title=''):
        if self._closing:
            return
        worker = self._taskbar_action_worker
        if worker is not None and worker.isRunning():
            self._set_taskbar_notice('正在读取任务栏，请稍候。')
            return
        worker = TaskbarActionWorker(action, index, expected_title, self)
        worker.completedAction.connect(self._on_taskbar_action_completed)
        worker.finished.connect(self._on_taskbar_action_finished)
        self._taskbar_action_worker = worker
        self._taskbar_action_kind = action
        if hasattr(self, 'taskbar_refresh_button'):
            self.taskbar_refresh_button.setEnabled(False)
        if action == 'refresh':
            self._set_taskbar_notice('正在读取主任务栏窗口…')
        else:
            self._set_taskbar_notice('正在切换窗口…')
        worker.start()

    def _refresh_taskbar_apps(self):
        self._start_taskbar_action('refresh')

    def _refresh_taskbar_if_visible(self):
        if self._active_section == 'startup':
            worker = self._taskbar_action_worker
            if worker is None or not worker.isRunning():
                self._refresh_taskbar_apps()

    def _on_taskbar_action_completed(self, payload):
        if self._closing or not isinstance(payload, dict):
            return
        action = payload.get('action')
        error = str(payload.get('error') or '')
        if error:
            self._set_taskbar_notice(error, error=True)
            if action == 'refresh':
                self.taskbar_status.setText('读取失败')
            return
        if action == 'refresh':
            self._taskbar_items = list(payload.get('items') or [])
            self._populate_taskbar_table()
            count = len(self._taskbar_items)
            self.taskbar_status.setText(f'已读取 {count} 个窗口')
            self._set_taskbar_notice(
                '双击一行、使用下方按钮，或按对应全局快捷键切换窗口。'
                if count else '当前任务栏没有可切换的运行窗口。')
            return
        title = str(payload.get('title') or '目标窗口')
        if self._active_section == 'startup':
            self._set_taskbar_notice(f'已切换到：{title}')
        else:
            self.status.setText(f'快捷切换完成 · {title}')

    def _on_taskbar_action_finished(self):
        worker = self._taskbar_action_worker
        if worker is not None:
            worker.deleteLater()
        self._taskbar_action_worker = None
        if hasattr(self, 'taskbar_refresh_button') and not self._closing:
            self.taskbar_refresh_button.setEnabled(True)
            self._sync_taskbar_selection()

    def _set_taskbar_notice(self, message, error=False):
        if not hasattr(self, 'taskbar_notice'):
            return
        self.taskbar_notice.setText(str(message))
        self.taskbar_notice.setProperty('noticeLevel', 'error' if error else 'normal')
        style = self.taskbar_notice.style()
        style.unpolish(self.taskbar_notice)
        style.polish(self.taskbar_notice)
        self.taskbar_notice.update()

    def _populate_taskbar_table(self):
        if not hasattr(self, 'taskbar_table'):
            return
        table = self.taskbar_table
        selected_index = None
        current_row = table.currentRow()
        if current_row >= 0:
            key_item = table.item(current_row, 0)
            if key_item is not None:
                value = key_item.data(Qt.ItemDataRole.UserRole)
                selected_index = int(value) if value is not None else None
        query = self.taskbar_search.text().strip().casefold()
        rows = []
        for index, item in enumerate(self._taskbar_items[:10]):
            name = str(item.get('name') or item.get('title') or '未命名窗口')
            title = str(item.get('title') or name)
            hotkey = self._taskbar_hotkeys.get(index)
            if hotkey is None:
                configured = self._taskbar_bindings.get(index, '')
                hotkey = ('录入中' if self._taskbar_hotkeys_paused else
                          f'{configured} · 占用' if configured else '未设置')
            position = f'右数第 {index + 1} 个'
            if not query or query in f'{name} {title} {hotkey} {position}'.casefold():
                rows.append((index, name, title, hotkey, position))
        table.setUpdatesEnabled(False)
        table.setRowCount(len(rows))
        for row, (index, name, title, hotkey, position) in enumerate(rows):
            key_cell = QTableWidgetItem(hotkey)
            key_cell.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
            key_cell.setData(Qt.ItemDataRole.UserRole, index)
            key_cell.setToolTip('按任务栏从右往左排序')
            name_cell = QTableWidgetItem(name)
            name_cell.setToolTip(title)
            position_cell = QTableWidgetItem(position)
            position_cell.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
            table.setItem(row, 0, key_cell)
            table.setItem(row, 1, name_cell)
            table.setItem(row, 2, position_cell)
            table.setRowHeight(row, 42)
            if index == selected_index:
                table.selectRow(row)
        table.setUpdatesEnabled(True)
        table.viewport().update()
        if self._taskbar_items and not rows:
            self._set_taskbar_notice('没有名称与筛选内容相符的窗口。')
        self._sync_taskbar_selection()

    def _sync_taskbar_selection(self):
        if not hasattr(self, 'taskbar_switch_button'):
            return
        table = getattr(self, 'taskbar_table', None)
        row = table.currentRow() if table is not None else -1
        self.taskbar_switch_button.setEnabled(row >= 0 and table.item(row, 0) is not None)

    def _switch_selected_taskbar_app(self):
        table = getattr(self, 'taskbar_table', None)
        if table is None:
            return
        row = table.currentRow()
        key_cell = table.item(row, 0) if row >= 0 else None
        name_cell = table.item(row, 1) if row >= 0 else None
        if key_cell is None or name_cell is None:
            self._set_taskbar_notice('请先选择要切换的窗口。')
            return
        index = key_cell.data(Qt.ItemDataRole.UserRole)
        if index is None:
            return
        index = int(index)
        expected_title = ''
        if 0 <= index < len(self._taskbar_items):
            item = self._taskbar_items[index]
            expected_title = str(item.get('title') or item.get('name') or '')
        self._start_taskbar_action('activate', index, expected_title)

    def _on_taskbar_hotkeys_ready(self, mapping):
        if (self.sender() is not self._taskbar_hotkey_worker
                or self._taskbar_hotkeys_paused or self._closing):
            return
        self._taskbar_hotkeys = dict(mapping or {})
        if not hasattr(self, 'taskbar_status'):
            return
        count = len(self._taskbar_hotkeys)
        if count == 10:
            self.taskbar_status.setText('全局快捷键已就绪')
            self.taskbar_hotkey_hint.setText(
                '快捷键按任务栏右侧第 1–10 位排列；可在设置 → 快捷工具插件中修改。')
        elif count:
            self.taskbar_status.setText(f'快捷键已就绪 {count}/10')
            self.taskbar_hotkey_hint.setText(
                '部分按键重复或被系统占用；可在设置 → 快捷工具插件中换绑。')
        else:
            self.taskbar_status.setText('全局快捷键不可用')
            self.taskbar_hotkey_hint.setText(
                '当前没有按键注册成功；请到设置 → 快捷工具插件调整，或双击表格切换。')
        self._populate_taskbar_table()

    def _on_taskbar_hotkey_pressed(self, index):
        if self.sender() is not self._taskbar_hotkey_worker or self._closing:
            return
        self._start_taskbar_action('activate', int(index))

    def _build_settings_page(self, include_heading=True):
        """设置页使用分类导航，主题分类可立即切换，其余分类先预留位置。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(14)

        if include_heading:
            heading = QVBoxLayout()
            heading.setSpacing(4)
            heading.addWidget(label('DEV KING / PREFERENCES', 'eyebrow'))
            heading.addWidget(label('设置', 'pageTitle'))
            heading.addWidget(label('调整 ZJ HUB 的主题和后续功能偏好。', 'pageHint'))
            layout.addLayout(heading)

        body = QHBoxLayout()
        body.setSpacing(14)
        layout.addLayout(body, 1)

        category_panel = QFrame()
        category_panel.setObjectName('settingsSidebar')
        category_panel.setMinimumWidth(218)
        category_panel.setMaximumWidth(244)
        category_layout = QVBoxLayout(category_panel)
        category_layout.setContentsMargins(10, 12, 10, 12)
        category_layout.setSpacing(5)
        self._section_header(category_layout, '设置分类', 'settings')
        category_layout.addSpacing(5)
        self.settings_category_buttons = {}
        self.settings_category_indices = {}
        category_specs = [
            ('theme', '主题', '切换外观主题', 'sparkles'),
            ('general', '常规', '通用行为设置', 'settings'),
            ('performance', '性能', '显示与性能选项', 'chart'),
            ('api', 'API 连接', '管理对局数据查询令牌', 'shield'),
            ('chat-account', '大厅身份', '登录 ID 与大厅聊天身份', 'user'),
            ('taskbar-hotkeys', '快捷工具插件', 'TaskbarSwitcher 按键设置', 'command'),
        ]
        for key, title, hint, icon_name in category_specs:
            button = QPushButton(title)
            button.setObjectName('settingsCategory')
            button.setCheckable(True)
            button.setProperty('iconName', icon_name)
            button.setProperty('iconRole', 'settings')
            button.setIcon(line_icon(icon_name))
            button.setIconSize(QSize(19, 19))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(hint)
            button.clicked.connect(lambda checked=False, name=key: self.switch_settings_category(name))
            category_layout.addWidget(button)
            self.settings_category_buttons[key] = button
        category_layout.addStretch(1)
        category_layout.addWidget(label('左侧分类可以继续扩展。', 'brandHint'))
        body.addWidget(category_panel)

        self.settings_stack = QStackedWidget()
        body.addWidget(self.settings_stack, 1)

        theme_page = self._build_theme_settings()
        self.settings_category_indices['theme'] = self.settings_stack.addWidget(theme_page)
        self.settings_category_indices['general'] = self.settings_stack.addWidget(
            self._build_general_settings())
        self.settings_category_indices['performance'] = self.settings_stack.addWidget(
            self._build_animation_settings())
        self.settings_category_indices['api'] = self.settings_stack.addWidget(self._build_api_settings())
        self.settings_category_indices['chat-account'] = self.settings_stack.addWidget(
            self._build_chat_account_settings())
        self.settings_category_indices['taskbar-hotkeys'] = self.settings_stack.addWidget(
            self._build_taskbar_hotkey_settings())
        self.switch_settings_category('theme')
        return page

    def _build_general_settings(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        card = QFrame()
        card.setObjectName('settingsCard')
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 20, 22, 18)
        card_layout.setSpacing(11)
        self._section_header(card_layout, '常规设置', 'settings')
        card_layout.addWidget(label('调整消息反馈和常用行为。', 'muted'))

        sound_row = QFrame()
        sound_row.setObjectName('featureRow')
        sound_layout = QHBoxLayout(sound_row)
        sound_layout.setContentsMargins(14, 11, 14, 11)
        sound_layout.setSpacing(10)
        sound_copy = QVBoxLayout()
        sound_copy.setSpacing(3)
        sound_copy.addWidget(label('大厅消息提示音', 'cardTitle'))
        self.chat_sound_hint = label('', 'cardHint')
        sound_copy.addWidget(self.chat_sound_hint)
        sound_layout.addLayout(sound_copy, 1)
        from PySide6.QtWidgets import QCheckBox
        toggle = QCheckBox('开启')
        toggle.setObjectName('chatSoundEnabled')
        toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle.setChecked(self.chat_sound_enabled)
        toggle.toggled.connect(self._set_chat_sound_enabled)
        self.chat_sound_toggle = toggle
        sound_layout.addWidget(toggle)
        card_layout.addWidget(sound_row)

        volume_row = QFrame()
        volume_row.setObjectName('featureRow')
        volume_layout = QHBoxLayout(volume_row)
        volume_layout.setContentsMargins(14, 11, 14, 11)
        volume_layout.setSpacing(10)
        volume_copy = QVBoxLayout()
        volume_copy.setSpacing(3)
        volume_copy.addWidget(label('提示音音量', 'cardTitle'))
        volume_copy.addWidget(label('使用 Codex 任务完成提示音风格的短促提示。', 'cardHint'))
        volume_layout.addLayout(volume_copy, 1)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setObjectName('chatSoundVolume')
        slider.setRange(0, 100)
        slider.setSingleStep(5)
        slider.setPageStep(10)
        slider.setValue(self.chat_sound_volume)
        slider.setMinimumWidth(180)
        slider.setCursor(Qt.CursorShape.PointingHandCursor)
        slider.valueChanged.connect(self._set_chat_sound_volume)
        self.chat_sound_volume_slider = slider
        volume_layout.addWidget(slider)
        self.chat_sound_volume_value = label(f'{self.chat_sound_volume}%', 'cardTitle')
        self.chat_sound_volume_value.setMinimumWidth(42)
        self.chat_sound_volume_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        volume_layout.addWidget(self.chat_sound_volume_value)
        card_layout.addWidget(volume_row)
        self._set_chat_sound_enabled(self.chat_sound_enabled)
        self._set_chat_sound_volume(self.chat_sound_volume)
        card_layout.addWidget(label('关闭提示音不会影响大厅消息接收。', 'muted'))
        card_layout.addStretch(1)
        layout.addWidget(card, 1)
        return page

    def _build_chat_account_settings(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        card = QFrame()
        card.setObjectName('settingsCard')
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 20, 22, 18)
        card_layout.setSpacing(11)
        self._section_header(card_layout, '大厅身份', 'user')
        hint = label('登录游戏 ID 后，战局查询和大厅聊天会共用这个身份。换绑后下次发送消息立即生效。', 'muted')
        hint.setWordWrap(True)
        card_layout.addWidget(hint)
        row = QFrame()
        row.setObjectName('featureRow')
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(14, 11, 14, 11)
        row_layout.setSpacing(9)
        copy = QVBoxLayout()
        copy.setSpacing(3)
        copy.addWidget(label('游戏 ID', 'cardTitle'))
        self.chat_account_hint = label('', 'cardHint')
        copy.addWidget(self.chat_account_hint)
        row_layout.addLayout(copy, 1)
        self.chat_account_input = QLineEdit()
        self.chat_account_input.setObjectName('themedInput')
        self.chat_account_input.setPlaceholderText('输入布吉岛游戏 ID')
        self.chat_account_input.setMaxLength(64)
        self.chat_account_input.setMinimumWidth(220)
        self.chat_account_input.setText(self._chat_bound_game_id())
        row_layout.addWidget(self.chat_account_input)
        save = self.button('保存身份', self._save_chat_account, True)
        save.setObjectName('chatSaveIdentity')
        row_layout.addWidget(save)
        card_layout.addWidget(row)
        server_row = QFrame()
        server_row.setObjectName('featureRow')
        server_layout = QHBoxLayout(server_row)
        server_layout.setContentsMargins(14, 11, 14, 11)
        server_layout.setSpacing(9)
        server_copy = QVBoxLayout()
        server_copy.setSpacing(3)
        server_copy.addWidget(label('大厅服务器', 'cardTitle'))
        server_hint = label('默认使用公共大厅地址，也可填入自托管服务。', 'cardHint')
        server_hint.setWordWrap(True)
        server_copy.addWidget(server_hint)
        server_layout.addLayout(server_copy, 1)
        self.chat_server_url_input = QLineEdit()
        self.chat_server_url_input.setObjectName('themedInput')
        self.chat_server_url_input.setPlaceholderText('Supabase Realtime 地址')
        self.chat_server_url_input.setText(SUPABASE_REALTIME_URL)
        self.chat_server_url_input.setReadOnly(True)
        self.chat_server_url_input.setMinimumWidth(300)
        self.chat_server_url_input.setClearButtonEnabled(True)
        self.chat_server_url_input.returnPressed.connect(self._save_chat_server_url)
        server_layout.addWidget(self.chat_server_url_input)
        server_save = self.button('重新连接', self._save_chat_server_url, True)
        server_save.setObjectName('chatSaveServer')
        server_layout.addWidget(server_save)
        card_layout.addWidget(server_row)
        sound_row = QFrame()
        sound_row.setObjectName('featureRow')
        sound_layout = QHBoxLayout(sound_row)
        sound_layout.setContentsMargins(14, 10, 14, 10)
        sound_layout.setSpacing(10)
        sound_copy = QVBoxLayout()
        sound_copy.setSpacing(3)
        sound_copy.addWidget(label('大厅消息提示音', 'cardTitle'))
        self.chat_sound_hint = label('', 'cardHint')
        self.chat_sound_hint.setWordWrap(True)
        sound_copy.addWidget(self.chat_sound_hint)
        sound_layout.addLayout(sound_copy, 1)
        self.chat_sound_toggle = QCheckBox('启用')
        self.chat_sound_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chat_sound_toggle.setChecked(self.chat_sound_enabled)
        self.chat_sound_toggle.toggled.connect(self._set_chat_sound_enabled)
        sound_layout.addWidget(self.chat_sound_toggle)
        sound_layout.addWidget(label('音量', 'cardHint'))
        self.chat_sound_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.chat_sound_volume_slider.setRange(0, 100)
        self.chat_sound_volume_slider.setValue(self.chat_sound_volume)
        self.chat_sound_volume_slider.setSingleStep(5)
        self.chat_sound_volume_slider.setPageStep(10)
        self.chat_sound_volume_slider.setFixedWidth(150)
        self.chat_sound_volume_slider.setToolTip('调整大厅消息提示音音量')
        self.chat_sound_volume_slider.valueChanged.connect(self._set_chat_sound_volume)
        sound_layout.addWidget(self.chat_sound_volume_slider)
        self.chat_sound_volume_value = label(f'{self.chat_sound_volume}%', 'cardHint')
        self.chat_sound_volume_value.setMinimumWidth(38)
        self.chat_sound_volume_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        sound_layout.addWidget(self.chat_sound_volume_value)
        card_layout.addWidget(sound_row)
        self._set_chat_sound_enabled(self.chat_sound_enabled)
        card_layout.addWidget(label('大厅消息会公开显示给所有在线玩家，请勿填写隐私信息。', 'muted'))
        card_layout.addStretch(1)
        layout.addWidget(card, 1)
        return page

    def _save_chat_account(self):
        value = self.chat_account_input.text().strip()
        if not value:
            self.chat_account_hint.setText('请输入有效的游戏 ID。')
            self.chat_account_input.setFocus()
            return
        self._open_player_login_dialog(value)

    def _save_chat_server_url(self):
        self._chat_server_url = SUPABASE_REALTIME_URL
        self.chat_server_url_input.setText(SUPABASE_REALTIME_URL)
        self._chat_client.stop()
        self.chat_connection_badge.setText('准备连接')
        self._refresh_chat_identity()
        self.chat_status.setText('正在重新连接 Supabase Realtime 公共大厅……')
        self._poll_lobby_chat()

    def _build_taskbar_hotkey_settings(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        card = QFrame()
        card.setObjectName('settingsCard')
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 20, 22, 18)
        card_layout.setSpacing(10)
        self._section_header(card_layout, 'TaskbarSwitcher 快捷键', 'command')
        description = label(
            '点击按键框后按下一个组合键。支持 Ctrl、Alt 或 Win 加字母、数字或 F1–F12；'
            '录入时会暂时释放全局快捷键，离开按键框后自动重新注册。', 'muted')
        description.setWordWrap(True)
        card_layout.addWidget(description)

        bindings_grid = QGridLayout()
        bindings_grid.setContentsMargins(0, 2, 0, 0)
        bindings_grid.setHorizontalSpacing(10)
        bindings_grid.setVerticalSpacing(8)
        self._taskbar_hotkey_edits = {}
        for index in range(10):
            row = QFrame()
            row.setObjectName('featureRow')
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(11, 7, 11, 7)
            row_layout.setSpacing(8)
            title = label(f'任务栏右侧第 {index + 1} 位', 'cardTitle')
            row_layout.addWidget(title, 1)
            edit = HotkeyCaptureEdit(self._taskbar_bindings.get(index, ''), row)
            edit.setObjectName('themedInput')
            # Ctrl+Alt+Shift+F12 is a valid binding and needs roughly 234px
            # with Microsoft YaHei UI.  Leave room for it so the capture field
            # never hides the value after a theme or DPI change.
            edit.setMinimumWidth(236)
            edit.setMaximumWidth(260)
            edit.captureStarted.connect(self._pause_taskbar_hotkeys)
            edit.captureFinished.connect(self._resume_taskbar_hotkeys)
            edit.sequenceChanged.connect(
                lambda sequence, slot=index: self._set_taskbar_hotkey_binding(slot, sequence))
            row_layout.addWidget(edit)
            bindings_grid.addWidget(row, index // 2, index % 2)
            self._taskbar_hotkey_edits[index] = edit
        card_layout.addLayout(bindings_grid)

        actions = QHBoxLayout()
        actions.addWidget(label('重复按键或被系统占用的按键不会注册。', 'muted'), 1)
        reset = QPushButton('恢复默认按键')
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.setMinimumHeight(38)
        reset.clicked.connect(self._reset_taskbar_hotkey_bindings)
        actions.addWidget(reset)
        card_layout.addLayout(actions)
        card_layout.addStretch(1)
        layout.addWidget(card, 1)
        return page

    def _set_taskbar_hotkey_binding(self, index, sequence):
        index = int(index)
        sequence = str(sequence or '')
        if _windows_hotkey_parts(sequence) is None and sequence:
            return
        if self._taskbar_bindings.get(index, '') == sequence:
            return
        self._taskbar_bindings[index] = sequence
        self._settings.setValue(f'plugins/taskbar_switcher/hotkeys/{index}', sequence)
        self._settings.sync()
        if not self._taskbar_hotkeys_paused:
            self._restart_taskbar_hotkeys()

    def _reset_taskbar_hotkey_bindings(self):
        for index, sequence in TASKBAR_DEFAULT_BINDINGS.items():
            self._taskbar_bindings[index] = sequence
            self._settings.setValue(f'plugins/taskbar_switcher/hotkeys/{index}', sequence)
            edit = getattr(self, '_taskbar_hotkey_edits', {}).get(index)
            if edit is not None:
                edit.setSequence(sequence)
        self._settings.sync()
        self._restart_taskbar_hotkeys()
        if hasattr(self, 'taskbar_notice'):
            self._set_taskbar_notice('已恢复 Alt+1 到 Alt+0 默认按键。')

    def _pause_taskbar_hotkeys(self):
        if self._taskbar_hotkeys_paused:
            return
        self._taskbar_hotkeys_paused = True
        worker = self._taskbar_hotkey_worker
        if worker is not None and worker.isRunning():
            worker.stop()
            worker.wait(1200)
        if worker is not None and not worker.isRunning():
            worker.deleteLater()
            self._taskbar_hotkey_worker = None
        self._taskbar_hotkeys = {}
        if hasattr(self, 'taskbar_status'):
            self.taskbar_status.setText('按键录入中')
            self._populate_taskbar_table()

    def _resume_taskbar_hotkeys(self):
        if not self._taskbar_hotkeys_paused:
            return
        self._taskbar_hotkeys_paused = False
        self._restart_taskbar_hotkeys()

    def _restart_taskbar_hotkeys(self):
        worker = self._taskbar_hotkey_worker
        if worker is not None and worker.isRunning():
            worker.stop()
            if not worker.wait(1200):
                return
        if worker is not None:
            worker.deleteLater()
        worker = TaskbarHotkeyWorker(self._taskbar_bindings, self)
        worker.hotkeysReady.connect(self._on_taskbar_hotkeys_ready)
        worker.pressed.connect(self._on_taskbar_hotkey_pressed)
        self._taskbar_hotkey_worker = worker
        worker.start()

    def _build_animation_settings(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        card = QFrame()
        card.setObjectName('settingsCard')
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(24, 22, 24, 22)
        card_layout.setSpacing(12)
        self._section_header(card_layout, '动画与反馈', 'sparkles')
        card_layout.addWidget(label('控制页面切换、按钮反馈和对局详情展开的过渡速度。', 'muted'))
        row = QFrame()
        row.setObjectName('featureRow')
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(14, 12, 14, 12)
        copy = QVBoxLayout()
        copy.setSpacing(3)
        copy.addWidget(label('界面动画速度', 'cardTitle'))
        self.animation_speed_hint = label('', 'cardHint')
        copy.addWidget(self.animation_speed_hint)
        row_layout.addLayout(copy, 1)
        speed = ThemedComboBox()
        speed.setObjectName('animationSpeed')
        speed.addItem('关闭', 'off')
        speed.addItem('快速', 'fast')
        speed.addItem('标准', 'standard')
        speed.addItem('慢速', 'slow')
        speed.setMinimumWidth(128)
        speed.setCursor(Qt.CursorShape.PointingHandCursor)
        speed.currentIndexChanged.connect(lambda _index: self._set_animation_speed(speed.currentData()))
        self.animation_speed_combo = speed
        speed.setCurrentIndex(max(0, speed.findData(self.animation_speed)))
        row_layout.addWidget(speed)
        card_layout.addWidget(row)
        self._set_animation_speed(self.animation_speed)
        card_layout.addWidget(label('推荐使用“标准”。关闭动画只会取消界面过渡，不会影响对局数据查询。', 'muted'))
        card_layout.addStretch(1)
        layout.addWidget(card, 1)
        return page

    def _build_settings_overlay(self, root):
        """创建覆盖整个客户端区域的设置层，背景使用不透明主题色。"""
        overlay = QFrame(root)
        overlay.setObjectName('settingsOverlay')
        overlay.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        overlay.setGeometry(root.rect())
        layout = QVBoxLayout(overlay)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(18)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        copy = QVBoxLayout()
        copy.setSpacing(3)
        copy.addWidget(label('DEV KING / SETTINGS', 'eyebrow'))
        copy.addWidget(label('设置', 'pageTitle'))
        copy.addWidget(label('ZJ HUB · 设置覆盖在主界面之上，背景不透明', 'pageHint'))
        header.addLayout(copy)
        header.addStretch()
        back = QPushButton('返回主界面')
        back.setObjectName('settingsBack')
        back.setIcon(line_icon('home', size=17))
        back.setIconSize(QSize(17, 17))
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.clicked.connect(self.close_settings_overlay)
        header.addWidget(back)
        layout.addLayout(header)

        # 设置页本身填满覆盖层，避免露出下面的主界面。
        layout.addWidget(self._build_settings_page(include_heading=False), 1)
        overlay.hide()
        return overlay

    def show_settings_overlay(self):
        if not hasattr(self, 'settings_overlay'):
            return
        self._settings_return_section = getattr(self, '_active_section', 'overview')
        self.settings_overlay.setGeometry(self.backdrop.rect())
        self.settings_overlay.show()
        self.settings_overlay.raise_()
        self.settings_button.setChecked(True)

    def close_settings_overlay(self):
        if hasattr(self, 'settings_overlay'):
            self.settings_overlay.hide()
        self.settings_button.setChecked(False)
        self.switch_section(getattr(self, '_settings_return_section', 'overview'))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'settings_overlay'):
            self.settings_overlay.setGeometry(self.backdrop.rect())

    def _build_theme_settings(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        card = QFrame()
        card.setObjectName('settingsCard')
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 20, 22, 20)
        card_layout.setSpacing(8)
        self._section_header(card_layout, '主题', 'sparkles')
        card_layout.addWidget(label('选择后会立即应用，明亮主题是默认外观。', 'muted'))
        self.settings_theme_label = label('当前主题：明亮', 'pageTitle')
        self.settings_theme_label.setStyleSheet('font-size: 17px;')
        card_layout.addWidget(self.settings_theme_label)
        self.settings_theme_hint = label(THEME_PALETTES['light']['description'], 'muted')
        card_layout.addWidget(self.settings_theme_hint)
        card_layout.addWidget(label('设置页覆盖整个 GUI，背景不透明，内容始终显示在最上层。', 'muted'))
        card_layout.addSpacing(8)

        choices = QWidget()
        choices_layout = QGridLayout(choices)
        choices_layout.setContentsMargins(0, 0, 0, 0)
        choices_layout.setHorizontalSpacing(10)
        choices_layout.setVerticalSpacing(10)
        self.theme_buttons = {}
        for index, (key, config) in enumerate(THEME_PALETTES.items()):
            button = QPushButton(f"{config['name']}\n{config['description']}")
            button.setObjectName('themeOption')
            button.setCheckable(True)
            button.setProperty('iconName', config['icon'])
            button.setProperty('iconRole', 'theme')
            button.setIcon(line_icon(config['icon'], size=20))
            button.setIconSize(QSize(20, 20))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAccessibleName(config['name'] + '主题')
            button.clicked.connect(lambda checked=False, name=key: self.apply_theme(name))
            choices_layout.addWidget(button, index // 2, index % 2)
            self.theme_buttons[key] = button
        card_layout.addWidget(choices)
        self._feature_row(card_layout, '主题预览', '四种主题共用一致的内容层级，只切换背景、卡片和强调色。', 'sparkles')
        card_layout.addStretch(1)
        layout.addWidget(card, 1)
        return page

    def _build_api_settings(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        card = QFrame()
        card.setObjectName('settingsCard')
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(24, 22, 24, 22)
        card_layout.setSpacing(10)
        self._section_header(card_layout, '布吉岛 API 连接', 'shield')
        description = label(
            '默认 API Token 已填入。你另行保存的令牌会由 Windows DPAPI 加密后保存在当前账户。',
            'muted')
        description.setWordWrap(True)
        card_layout.addWidget(description)
        self.api_token_input = QLineEdit()
        self.api_token_input.setObjectName('themedInput')
        self.api_token_input.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.api_token_input.setAutoFillBackground(False)
        self.api_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_token_input.setPlaceholderText('粘贴 API Token')
        self.api_token_input.setMinimumHeight(44)
        if self._api_token:
            self.api_token_input.setText(self._api_token)
        card_layout.addWidget(self.api_token_input)
        peak_note = label('如果遇到高峰期，可以在游戏内输入 /openapi 自行获取 API Token。', 'muted')
        peak_note.setWordWrap(True)
        card_layout.addWidget(peak_note)
        card_layout.addWidget(label('普通 Token 每分钟最多 30 次；应用会按 28 次/分钟进行本地保护。', 'muted'))
        self.api_token_status = label('', 'muted')
        card_layout.addWidget(self.api_token_status)
        actions = QHBoxLayout()
        actions.addStretch(1)
        clear_button = self.button('清除 Token', self._clear_api_token)
        actions.addWidget(clear_button)
        save_button = self.button('加密保存', self._save_api_token, True)
        actions.addWidget(save_button)
        card_layout.addLayout(actions)
        card_layout.addStretch(1)
        layout.addWidget(card, 1)
        self._refresh_api_token_status()
        return page

    def _refresh_api_token_status(self):
        if not hasattr(self, 'api_token_status'):
            return
        if self._api_token:
            if self._api_token_is_default and not bugland_api.token_is_saved():
                self.api_token_status.setText('默认 API Token 已就绪。')
            elif self._api_token_is_default:
                self.api_token_status.setText('默认 API Token 已就绪，并已加密保存在当前 Windows 账户。')
            else:
                self.api_token_status.setText('Token 已加密保存在当前 Windows 账户。')
        else:
            self.api_token_status.setText('尚未保存 Token。请粘贴后点击“加密保存”。')

    def _refresh_match_api_state(self):
        if not hasattr(self, 'match_api_state'):
            return
        if self._api_token:
            self.match_api_state.setText(
                '默认 API Token 已就绪。' if self._api_token_is_default
                else 'API Token 已在本机安全保存。')
        else:
            self.match_api_state.setText('尚未设置 API Token；首次查询时会提示配置。')

    def _save_api_token(self):
        try:
            bugland_api.save_token(self.api_token_input.text())
        except Exception as error:
            self.api_token_status.setText(f'保存失败：{error}')
            return
        self._api_token = self.api_token_input.text().strip()
        self._api_token_is_default = self._api_token == bugland_api.DEFAULT_API_TOKEN
        self.api_token_input.setText(self._api_token)
        self._refresh_api_token_status()
        self._refresh_match_api_state()

    def _clear_api_token(self):
        try:
            bugland_api.clear_token()
        except OSError as error:
            self.api_token_status.setText(f'清除失败：{error}')
            return
        self._api_token = ''
        self._api_token_is_default = False
        self.api_token_input.clear()
        self._refresh_api_token_status()
        self._refresh_match_api_state()

    def _open_api_settings(self):
        self.show_settings_overlay()
        self.switch_settings_category('api')

    def _build_settings_placeholder(self, title, description, icon, bullets):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        card = QFrame()
        card.setObjectName('settingsCard')
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(26, 24, 26, 24)
        card_layout.setSpacing(12)
        card_layout.addWidget(icon_tile(icon, 'moduleIconTile', '#bfe1ff', 28, 50))
        card_layout.addWidget(label(title, 'section'))
        text = label(description, 'placeholderText')
        text.setWordWrap(True)
        card_layout.addWidget(text)
        card_layout.addSpacing(5)
        for bullet in bullets:
            card_layout.addWidget(label(f'  ·  {bullet}', 'muted'))
        card_layout.addStretch(1)
        layout.addWidget(card, 1)
        return page

    def switch_settings_category(self, key):
        index = self.settings_category_indices.get(key)
        if index is None:
            return
        self.settings_stack.setCurrentIndex(index)
        self._animate_widget(self.settings_stack.currentWidget(), 180)
        for name, button in self.settings_category_buttons.items():
            button.setChecked(name == key)

    def apply_theme(self, key):
        if key not in THEME_PALETTES:
            key = 'light'
        self.current_theme = key
        if hasattr(self, 'backdrop'):
            self.backdrop.set_theme(key)
        app = QApplication.instance()
        if app is not None:
            # Private combo popup containers are top-level widgets; expose the
            # active theme so ThemedComboBox can repaint them directly when a
            # selector is not propagated by the platform style backend.
            app.setProperty('_themeKey', key)
            app.setStyleSheet(build_style(key))
        self._apply_opaque_input_surfaces()
        self._refresh_theme_icons()
        if hasattr(self, 'theme_buttons'):
            for name, button in self.theme_buttons.items():
                button.setChecked(name == key)
        if hasattr(self, 'settings_theme_label'):
            config = THEME_PALETTES[key]
            self.settings_theme_label.setText(f"当前主题：{config['name']}")
            self.settings_theme_hint.setText(config['description'])

    def _apply_opaque_input_surfaces(self):
        """Keep editable fields opaque through focus, hover and theme changes."""
        colors = INPUT_SURFACES[self.current_theme]
        fill = colors['fill']
        text = colors['text']
        border = colors['border']
        focus = colors['focus']
        selection = colors['selection']
        field_style = f"""
QLineEdit#themedInput,
QLineEdit#themedInput:hover,
QLineEdit#themedInput:focus,
QLineEdit#themedInput:read-only,
QLineEdit#themedInput:disabled {{
    color: {text};
    background-color: {fill};
    border: 1px solid {border};
    border-radius: 9px;
    padding: 10px 12px;
    selection-background-color: {selection};
    selection-color: {text};
}}
QLineEdit#themedInput:focus {{ border-color: {focus}; }}
"""
        for field in self.findChildren(QLineEdit):
            if field.objectName() != 'themedInput':
                continue
            field.setAutoFillBackground(False)
            field.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            field.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
            palette = field.palette()
            for group in (
                QPalette.ColorGroup.Active,
                QPalette.ColorGroup.Inactive,
                QPalette.ColorGroup.Disabled,
            ):
                palette.setColor(group, QPalette.ColorRole.Base, QColor(fill))
                palette.setColor(group, QPalette.ColorRole.Window, QColor(fill))
                palette.setColor(group, QPalette.ColorRole.Text, QColor(text))
                palette.setColor(group, QPalette.ColorRole.PlaceholderText, QColor(text))
                palette.setColor(group, QPalette.ColorRole.Highlight, QColor(selection))
                palette.setColor(group, QPalette.ColorRole.HighlightedText, QColor(text))
            field.setPalette(palette)
            field.setStyleSheet(field_style)

    def _refresh_theme_icons(self):
        """让各主题共用同一套图标几何，只切换对比度色，避免视觉漂移。"""
        if not hasattr(self, 'nav_buttons'):
            return
        if self.current_theme == 'light':
            normal, active, metric = '#536679', '#ffffff', '#3f79b4'
        elif self.current_theme == 'glass':
            normal, active, metric = '#c4c6ca', '#f7f7f8', '#d0d2d6'
        elif self.current_theme == 'liquid':
            normal, active, metric = '#c7d1df', '#ffffff', '#b7d3f5'
        else:
            normal, active, metric = '#b8c8dc', '#eff7ff', '#a9d4ff'
        for button in self.nav_buttons.values():
            button.setIcon(line_icon(button.property('iconName'), normal, active, 20, active))
        if hasattr(self, 'settings_button'):
            self.settings_button.setIcon(line_icon('settings', normal, active, 20, active))
        for button in getattr(self, 'settings_category_buttons', {}).values():
            name = button.property('iconName') or 'command'
            button.setIcon(line_icon(name, normal, active, 19, active))
        theme_active = (
            '#ffffff' if self.current_theme == 'light'
            else '#202124' if self.current_theme == 'glass'
            else '#18263b' if self.current_theme == 'liquid'
            else '#102640'
        )
        for key, button in getattr(self, 'theme_buttons', {}).items():
            name = THEME_PALETTES[key]['icon']
            button.setIcon(line_icon(name, normal, theme_active, 20, active))
        for button in getattr(self, 'window_controls', []):
            name = button.property('iconName') or 'command'
            button.setIcon(line_icon(name, normal, '#ffffff', 18, '#ffffff'))
        primary_color = primary_icon_color(self.current_theme)
        for button in self.findChildren(QPushButton):
            name = button.property('themeIconName')
            if name:
                button.setIcon(line_icon(name, primary_color, primary_color, 17))
        for icon_label in self._metric_icon_labels:
            name = icon_label.property('iconName') or 'command'
            icon_label.setPixmap(_render_svg_icon(name, metric, 20))
        for icon_label in self.findChildren(QLabel, 'statMetricIcon'):
            name = icon_label.property('metricIconName') or 'command'
            tone = str(icon_label.property('metricTone') or 'neutral')
            icon_label.setPixmap(_render_svg_icon(
                name, self._metric_icon_color(tone), 20, 2.15))
        tile_color = (
            '#3f79b4' if self.current_theme == 'light'
            else '#c9cbd0' if self.current_theme == 'glass'
            else '#b7d3f5' if self.current_theme == 'liquid'
            else '#bfe1ff'
        )
        status_color = '#287c68' if self.current_theme == 'light' else '#b9f1e4'
        for glyph in self.findChildren(QLabel):
            if not glyph.property('iconTileGlyph'):
                continue
            color = status_color if glyph.property('iconSemanticRole') == 'status' else tile_color
            glyph.setPixmap(_render_svg_icon(
                glyph.property('iconName') or 'command',
                color,
                int(glyph.property('iconPixelSize') or 20),
            ))

    def _build_footer(self, root_layout):
        footer = QHBoxLayout()
        self.status = label('就绪 · ZJ HUB 在本机运行', 'muted')
        footer.addWidget(self.status)
        footer.addStretch()
        if not optimizer.is_admin():
            footer.addWidget(self.button('以管理员身份重启', self.restart_admin))
        from PySide6.QtWidgets import QSizeGrip
        footer.addWidget(QSizeGrip(self))
        root_layout.addLayout(footer)

    def refresh(self):
        super().refresh()
        if self.isMinimized() or self._dragging:
            return
        self._set_if_changed(self.dashboard_memory, self.memory.text())
        self._set_if_changed(self.dashboard_uptime, self.uptime.text())
        cpu_text = f'{optimizer.get_cpu_percent():.0f}%'
        self._set_if_changed(self.dashboard_cpu, cpu_text)
        if self.scan_ready:
            junk_text = optimizer.format_bytes(sum(t.scanned for t in self.targets))
        elif self.busy:
            junk_text = '扫描中…'
        else:
            junk_text = '尚未扫描'
        self._set_if_changed(self.dashboard_junk, junk_text)
        self._set_if_changed(self.dashboard_state, self.status.text())

    @staticmethod
    def _set_if_changed(widget, text):
        if widget.text() != text:
            widget.setText(text)

    def set_busy(self, busy, text='就绪 · ZJ HUB 在本机运行'):
        super().set_busy(busy, text)
        if hasattr(self, 'dashboard_state'):
            self._set_if_changed(self.dashboard_state, text)

    def closeEvent(self, event):
        # The cache belongs to this process only. Clear it before handling
        # either the idle or busy shutdown path so a close request can never
        # resurrect the previous room on the next launch.
        self._closing = True
        self._clear_chat_session_cache()
        if self.busy:
            super().closeEvent(event)
            return
        if hasattr(self, '_taskbar_auto_refresh_timer'):
            self._taskbar_auto_refresh_timer.stop()
        self._api_queue.clear()
        self._api_pump_timer.stop()
        self._finish_window_drag()
        self._stop_ui_animations()
        self._stop_detail_animations()
        if hasattr(self, '_chat_client'):
            self._chat_client.stop()
        if self.match_detail_dialog is not None:
            self._close_match_detail()
        if self._taskbar_hotkey_worker is not None and self._taskbar_hotkey_worker.isRunning():
            self._taskbar_hotkey_worker.stop()
            if not self._taskbar_hotkey_worker.wait(1000):
                self.hide()
                event.ignore()
                self._taskbar_hotkey_worker.finished.connect(self.close)
                return
        if self._api_worker is not None and self._api_worker.isRunning():
            # Do not destroy a QThread during a bounded network request.
            # Hide immediately and finish closing when the request returns.
            self.hide()
            event.ignore()
            self._api_worker.finished.connect(self.close)
            return
        if self._chat_worker is not None and self._chat_worker.isRunning():
            # Lobby polling is also a bounded network request; wait for its
            # worker before Qt tears down the parent window.
            self.hide()
            event.ignore()
            self._chat_worker.finished.connect(self.close)
            return
        if self._update_worker is not None and self._update_worker.isRunning():
            self.hide()
            event.ignore()
            self._update_worker.finished.connect(self.close)
            return
        if self._taskbar_action_worker is not None and self._taskbar_action_worker.isRunning():
            self.hide()
            event.ignore()
            self._taskbar_action_worker.finished.connect(self.close)
            return
        super().closeEvent(event)


def _reveal_main_window(window):
    """Show the main window with a short, quiet expand/fade reveal.

    The old standalone ZJ splash made startup feel like a separate loading
    screen and also delayed the first usable frame.  The application now opens
    directly and animates its own surface for roughly half a second.  Keeping
    the two animations on the native window also avoids a transient black
    overlay on Windows' frameless translucent window.
    """
    final_geometry = window.geometry()
    screen = QApplication.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        if not available.contains(final_geometry.center()):
            final_geometry.moveCenter(available.center())
    window.setGeometry(final_geometry)

    # A small 6% scale gives the expand motion without making the first frame
    # look like a dialog popping in.  Respect the window's minimum dimensions.
    start_width = max(window.minimumWidth(), int(final_geometry.width() * 0.94))
    start_height = max(window.minimumHeight(), int(final_geometry.height() * 0.94))
    start_geometry = QRect(0, 0, start_width, start_height)
    start_geometry.moveCenter(final_geometry.center())

    window.setWindowOpacity(0.0)
    window.setGeometry(start_geometry)
    window.show()
    window.raise_()
    window.activateWindow()

    geometry_animation = QPropertyAnimation(window, b'geometry', window)
    geometry_animation.setDuration(500)
    geometry_animation.setStartValue(start_geometry)
    geometry_animation.setEndValue(final_geometry)
    geometry_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    opacity_animation = QPropertyAnimation(window, b'windowOpacity', window)
    opacity_animation.setDuration(460)
    opacity_animation.setStartValue(0.0)
    opacity_animation.setEndValue(1.0)
    opacity_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    # Store the animations on the window so they remain alive for the full
    # transition even when this helper returns.
    window._startup_reveal_animations = [geometry_animation, opacity_animation]

    def finish_reveal():
        window.setWindowOpacity(1.0)
        window.setGeometry(final_geometry)
        window._startup_reveal_animations = []

    geometry_animation.finished.connect(finish_reveal)
    geometry_animation.start()
    opacity_animation.start()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setStyleSheet(build_style('light'))
    # First launch is gated by a verified BuGLand game ID.  Keep the dialog
    # ahead of LargeApp construction so the main GUI never flashes before the
    # identity has been accepted.  Existing IDs are loaded directly and can be
    # changed later from Settings → 大厅身份.
    startup_settings = QSettings('DEV King', '逐渐工具箱')
    saved_game_id = str(startup_settings.value(GAME_ID_SETTINGS_KEY, '') or '').strip()
    if not saved_game_id:
        login_dialog = PlayerLoginDialog(
            bugland_api.load_token(), '', 'light')
        if login_dialog.exec() != QDialog.DialogCode.Accepted:
            sys.exit(0)
    window = LargeApp()
    _reveal_main_window(window)
    sys.exit(app.exec())










