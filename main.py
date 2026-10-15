# -*- coding: utf-8 -*-
"""半透明玻璃桌面界面；清理逻辑由 optimizer 提供。"""
import ctypes
import queue
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QIcon, QLinearGradient, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import (
    QApplication, QAbstractButton, QAbstractScrollArea, QAbstractSlider,
    QAbstractSpinBox, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QVBoxLayout, QWidget, QSizeGrip,
)
import optimizer

STYLE = """
QWidget { color: #eff5ff; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
QMessageBox { background: #17263b; }
QToolTip { color: #eff5ff; background: #17263b; border: 1px solid #65809f; border-radius: 8px; padding: 6px; }
QLabel { background: transparent; }
QLabel#title { font-size: 26px; font-weight: 600; }
QLabel#section { font-size: 16px; font-weight: 600; }
QLabel#metric { font-size: 32px; font-weight: 600; }
QLabel#muted { color: #b2c2d7; }
QLabel#badge { color: #b8e9df; background: rgba(99,190,170,25); border-radius: 10px; padding: 7px 12px; }
QFrame#glass { background: rgba(20,35,57,72); border: 1px solid rgba(202,224,255,42); border-radius: 14px; }
QFrame#row { background: transparent; border-bottom: 1px solid rgba(210,230,255,18); }
QPushButton { background: rgba(182,213,255,20); border: 1px solid rgba(202,224,255,40); border-radius: 9px; padding: 10px 16px; }
QPushButton:hover { background: rgba(182,213,255,38); border-color: #7495b9; }
QPushButton:pressed { background: rgba(182,213,255,55); }
QPushButton:focus { border: 1px solid #a3ceff; }
QPushButton:disabled { color: #7e90a6; background: rgba(100,120,150,15); }
QPushButton#primary { background: #b7d7ff; color: #102640; border-color: #d0e5ff; font-weight: 600; }
QPushButton#primary:hover { background: #d0e5ff; }
QPushButton#primary:disabled { background: #5c7089; color: #bac5d3; }
QCheckBox { spacing: 12px; background: transparent; }
QCheckBox::indicator { width: 17px; height: 17px; border-radius: 4px; }
QPlainTextEdit { background: transparent; border: none; color: #b2c2d7; font-size: 12px; }
QScrollArea { background: transparent; border: none; }
QProgressBar { background: rgba(193,216,255,18); border: none; border-radius: 3px; height: 5px; }
QProgressBar::chunk { background: #9cc7ff; border-radius: 3px; }
"""


class _Accent(ctypes.Structure):
    _fields_ = [
        ('state', ctypes.c_int), ('flags', ctypes.c_int),
        ('color', ctypes.c_uint), ('animation', ctypes.c_int),
    ]


class _Composition(ctypes.Structure):
    _fields_ = [
        ('attribute', ctypes.c_int), ('data', ctypes.c_void_p),
        ('size', ctypes.c_size_t),
    ]


class Backdrop(QWidget):
    """静态渐变背景，缓存后只在窗口尺寸变化时重新绘制。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._background_cache = None
        self._cache_key = None

    def _rebuild_cache(self):
        width, height = self.width(), self.height()
        if width <= 0 or height <= 0:
            self._background_cache = None
            self._cache_key = None
            return

        cache = QPixmap(width, height)
        cache.fill(Qt.GlobalColor.transparent)
        painter = QPainter(cache)
        # 这里只绘制填充渐变，不需要抗锯齿；减少每次重建的 CPU 开销。
        base = QLinearGradient(0, 0, width, height)
        base.setColorAt(0, QColor(17, 30, 51, 105))
        base.setColorAt(1, QColor(12, 21, 37, 140))
        painter.fillRect(0, 0, width, height, base)
        for x, y, radius, color in [
            (.05, .0, .65, QColor(86, 139, 220, 70)),
            (.95, .65, .5, QColor(73, 139, 139, 38)),
        ]:
            glow = QRadialGradient(width * x, height * y, width * radius)
            glow.setColorAt(0, color)
            glow.setColorAt(1, QColor(0, 0, 0, 0))
            painter.fillRect(0, 0, width, height, glow)
        painter.end()
        self._background_cache = cache
        self._cache_key = (width, height, round(self.devicePixelRatioF(), 2))

    def resizeEvent(self, event):
        self._background_cache = None
        self._cache_key = None
        super().resizeEvent(event)

    def paintEvent(self, event):
        cache_key = (self.width(), self.height(), round(self.devicePixelRatioF(), 2))
        if self._background_cache is None or self._cache_key != cache_key:
            self._rebuild_cache()
        painter = QPainter(self)
        # The cache is already a full-size pixmap, but Qt may request only a
        # small exposed region after a child update or window uncover. Clip the
        # blit to that region so the translucent root does not copy the whole
        # 1180x800 surface for a tiny repaint.
        painter.setClipRegion(event.region())
        if self._background_cache is not None:
            painter.drawPixmap(0, 0, self._background_cache)
        else:
            painter.fillRect(self.rect(), QColor(12, 21, 37, 140))
        painter.end()


def label(text, role=None):
    widget = QLabel(text)
    if role:
        widget.setObjectName(role)
    return widget


def panel():
    widget = QFrame()
    widget.setObjectName('glass')
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(22, 20, 22, 20)
    layout.setSpacing(12)
    return widget, layout


class TitleBar(QWidget):
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.window().begin_window_drag(event.globalPosition().toPoint())

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.window().toggle_maximized()


class App(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('ZJ HUB')
        # Windows 上 Qt 的逐像素透明窗口必须同时使用无边框标志。
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._restore_geometry = None
        self.resize(1020, 660)
        self.setMinimumSize(820, 570)
        icon = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'app.ico'
        self.setWindowIcon(QIcon(str(icon)))
        self.targets = optimizer.build_targets()
        self.events = queue.Queue()
        self.busy = False
        self.scan_ready = False
        self.scan_completed = 0
        self.checks, self.sizes = {}, {}
        self.controls = []
        self._last_memory_state = None
        self._last_uptime_text = None
        self._glass_ready = False
        self._dragging = False
        self._native_move_pending = False
        self._drag_updates_suspended = False
        self._drag_offset = None
        self._pending_drag_pos = None
        self._drag_move_timer = QTimer(self)
        self._drag_move_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._drag_move_timer.setInterval(16)
        self._drag_move_timer.timeout.connect(self._flush_drag_move)
        self._drag_watchdog_timer = QTimer(self)
        self._drag_watchdog_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._drag_watchdog_timer.setInterval(80)
        self._drag_watchdog_timer.timeout.connect(self._check_window_drag_release)
        self._build_ui()
        # 无边框窗口没有原生标题栏；监听本窗口的空白区域，让面板边缘、标题
        # 文字和顶部留白都可以拖动，同时把按钮和滚动控件留给原有交互。
        QApplication.instance().installEventFilter(self)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self.refresh_timer.start(2000)
        self.event_timer = QTimer(self)
        self.event_timer.timeout.connect(self.drain)
        self.event_timer.setInterval(80)
        self.event_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self.refresh()
        QTimer.singleShot(100, self.native_glass)

    def native_glass(self):
        hwnd = ctypes.c_void_p(int(self.winId()))
        dwm = ctypes.windll.dwmapi
        # 关闭系统窗口过渡动画，减少拖动时的额外合成工作。 之前同时启用
        # Acrylic backdrop (38=3) 和旧式 Accent blur，导致普通明亮/深色主题
        # 也要持续经过整窗模糊合成。只有玻璃主题需要桌面模糊；其余主题的
        # 背景缓存已经足够提供材质感，使用 NONE 可显著降低重绘和窗口移动的
        # DWM 合成开销。
        for attribute, value in [(3, 1), (20, 1), (33, 2), (38, 1)]:
            data = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), 4)
        # ACCENT_ENABLE_BLURBEHIND 在 Windows 11 22000 上可用；只给玻璃主题
        # 开启，避免把每一次普通页面更新都交给模糊合成器处理。
        self._glass_ready = True
        self.blur_enabled = self._apply_blur(
            getattr(self, 'current_theme', 'light') == 'glass')

    def _apply_blur(self, enabled):
        """设置 DWM 模糊状态；仅玻璃主题需要持续模糊。"""
        hwnd = ctypes.c_void_p(int(self.winId()))
        accent = _Accent(3 if enabled else 0, 0, 0, 0)
        composition = _Composition(19, ctypes.addressof(accent), ctypes.sizeof(accent))
        apply = ctypes.windll.user32.SetWindowCompositionAttribute
        apply.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Composition)]
        apply.restype = ctypes.c_int
        try:
            return bool(apply(hwnd, ctypes.byref(composition)))
        except (AttributeError, OSError):
            return False

    def _set_drag_updates(self, enabled):
        """窗口移动期间持续绘制内容，防止拖动时留下空白/镂空帧。"""
        central = self.centralWidget()
        if central is None:
            return
        # 旧实现拖动时调用 setUpdatesEnabled(False)，带透明背景的窗口会
        # 把未重绘区域显示成黑块或空白层。鼠标移动已由 16ms 定时器合并，
        # 因此不再冻结绘制；始终恢复绘制以覆盖此前可能残留的禁用状态。
        self._drag_updates_suspended = False
        if not central.updatesEnabled():
            central.setUpdatesEnabled(True)
        central.update()

    def begin_window_drag(self, global_pos=None):
        if self.windowHandle() is None or self._dragging:
            return
        # The application event filter sees the same press before TitleBar's
        # mousePressEvent. Guard the second callback when native moving starts.
        if self._native_move_pending:
            return
        # Let Windows own the non-client move loop whenever possible.  The
        # previous Python-side grab + 16 ms timer added a visible input delay
        # and forced every translucent frame through the Qt event loop.
        if sys.platform == 'win32':
            self._native_move_pending = True
            QTimer.singleShot(0, lambda: setattr(self, '_native_move_pending', False))
            try:
                if self.windowHandle().startSystemMove():
                    return
            except (AttributeError, RuntimeError):
                self._native_move_pending = False
            else:
                self._native_move_pending = False
        global_pos = global_pos or QCursor.pos()
        self._drag_offset = global_pos - self.frameGeometry().topLeft()
        self._pending_drag_pos = self.pos()
        self._dragging = True
        self._set_drag_updates(False)
        # 自己以 60Hz 合并鼠标移动，避免系统移动循环把带模糊的窗口推到
        # 几百次/秒的重合成，同时保留模糊效果和窗口内容。
        self.grabMouse()
        self._drag_watchdog_timer.start()

    @staticmethod
    def _event_global_pos(event):
        if hasattr(event, 'globalPosition'):
            return event.globalPosition().toPoint()
        return event.globalPos()

    def _flush_drag_move(self):
        if self._dragging and self._pending_drag_pos is not None:
            self.move(self._pending_drag_pos)

    def _check_window_drag_release(self):
        if (self._dragging
                and not (QApplication.mouseButtons() & Qt.MouseButton.LeftButton)):
            self._finish_window_drag()

    def _finish_window_drag(self):
        if self._dragging:
            self._flush_drag_move()
            self._drag_move_timer.stop()
            self._drag_watchdog_timer.stop()
            self._dragging = False
            self._drag_offset = None
            self._pending_drag_pos = None
            if QWidget.mouseGrabber() is self:
                self.releaseMouse()
            self._set_drag_updates(True)

    def moveEvent(self, event):
        super().moveEvent(event)

    def _is_drag_source(self, widget):
        if not isinstance(widget, QWidget) or widget.window() is not self:
            return False
        interactive_types = (
            QAbstractButton, QAbstractScrollArea, QAbstractSlider, QSizeGrip,
            QLineEdit, QComboBox, QAbstractSpinBox,
        )
        # 子控件也继承其交互容器的鼠标行为，例如输入框内部按钮、滚动区里的
        # 对局卡片和文本视口。不能让这些按下事件启动窗口拖动。
        parent = widget
        while parent is not None and parent is not self:
            if isinstance(parent, interactive_types) or parent.property('noWindowDrag'):
                # A viewport can ignore a mouse press and propagate it past its
                # scroll area to a draggable panel. Stop at the interactive root.
                parent.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)
                return False
            if isinstance(parent, QLabel):
                flags = parent.textInteractionFlags()
                if (flags & Qt.TextInteractionFlag.TextSelectableByMouse or
                        (flags & Qt.TextInteractionFlag.LinksAccessibleByMouse and
                         '<a ' in parent.text().lower())):
                    parent.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)
                    return False
            parent = parent.parentWidget()
        return True

    def eventFilter(self, watched, event):
        event_type = event.type()
        if not self._dragging and event_type not in (
                QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            return False
        if self._dragging:
            if ((event_type == QEvent.Type.ApplicationDeactivate
                 and watched is QApplication.instance())
                    or (event_type in (QEvent.Type.WindowDeactivate, QEvent.Type.UngrabMouse,
                                       QEvent.Type.Hide)
                        and watched is self)):
                self._finish_window_drag()
        if (event_type == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
                and self._dragging):
            self._finish_window_drag()
            return True
        if self._dragging and event_type == QEvent.Type.MouseMove:
            self._pending_drag_pos = self._event_global_pos(event) - self._drag_offset
            if not self._drag_move_timer.isActive():
                self._drag_move_timer.start()
            return True
        if (event_type in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)
                and self._is_drag_source(watched)):
            if event_type == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
                self._finish_window_drag()
                self.toggle_maximized()
                return True
            if event_type == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self.begin_window_drag(self._event_global_pos(event))
                return True
        return super().eventFilter(watched, event)

    def button(self, text, action, primary=False):
        widget = QPushButton(text)
        widget.setMinimumHeight(38)
        widget.setMinimumWidth(96)
        if primary:
            widget.setObjectName('primary')
        widget.setCursor(Qt.CursorShape.PointingHandCursor)
        widget.clicked.connect(action)
        self.controls.append(widget)
        return widget

    def _build_ui(self):
        root = Backdrop()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(20)
        title_bar = TitleBar()
        header = QHBoxLayout(title_bar)
        header.setContentsMargins(0, 0, 0, 0)
        title = QVBoxLayout()
        title.setSpacing(5)
        title.addWidget(label('ZJ HUB', 'title'))
        title.addWidget(label('看清系统状态，按需整理空间。', 'muted'))
        header.addLayout(title)
        header.addStretch()
        header.addWidget(label('管理员模式' if optimizer.is_admin() else '普通模式', 'badge'))
        for text, tip, action in [('−', '最小化', self.showMinimized),
                                  ('□', '最大化 / 还原', self.toggle_maximized),
                                  ('×', '关闭', self.close)]:
            control = QPushButton(text)
            control.setFixedSize(38, 34)
            control.setStyleSheet('padding: 0; font-size: 18px;')
            control.setToolTip(tip)
            control.setAccessibleName(tip)
            control.clicked.connect(action)
            header.addWidget(control)
        # 标题文字不拦截鼠标，留出原生拖动区域。
        for text_widget in title_bar.findChildren(QLabel):
            text_widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(title_bar)
        body = QHBoxLayout()
        body.setSpacing(18)
        layout.addLayout(body, 1)
        left = QVBoxLayout()
        left.setSpacing(16)
        body.addLayout(left, 3)
        status, status_layout = panel()
        left.addWidget(status)
        status_layout.addWidget(label('系统概览', 'section'))
        status_layout.addWidget(label('内存使用', 'muted'))
        self.memory = label('—', 'metric')
        status_layout.addWidget(self.memory)
        self.memory_bar = QProgressBar()
        self.memory_bar.setTextVisible(False)
        self.memory_bar.setFixedHeight(5)
        status_layout.addWidget(self.memory_bar)
        self.memory_hint = label('读取中', 'muted')
        status_layout.addWidget(self.memory_hint)
        status_layout.addSpacing(8)
        status_layout.addWidget(label('连续运行', 'muted'))
        self.uptime = label('—', 'section')
        status_layout.addWidget(self.uptime)
        self.memory_button = self.button('释放内存', self.free_memory)
        self.memory_button.setToolTip('压缩工作集不等于解决内存泄漏，应用再次访问页面时可能暂时变慢。')
        status_layout.addWidget(self.memory_button)
        activity, activity_layout = panel()
        left.addWidget(activity, 1)
        activity_layout.addWidget(label('操作记录', 'section'))
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
        summary.addWidget(label('空间清理', 'section'))
        summary.addStretch()
        self.scan_button = self.button('扫描文件', self.scan)
        summary.addWidget(self.scan_button)
        clean_layout.addLayout(summary)
        self.junk = label('先扫描，了解可清理空间', 'muted')
        self.junk.setWordWrap(True)
        clean_layout.addWidget(self.junk)
        self.scan_progress = QProgressBar()
        self.scan_progress.setRange(0, len(self.targets))
        self.scan_progress.setTextVisible(False)
        self.scan_progress.setFixedHeight(5)
        self.scan_progress.hide()
        clean_layout.addWidget(self.scan_progress)
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
        actions.addWidget(self.button('重置选择', self.reset_selection))
        actions.addStretch()
        self.optimize_button = self.button('一键优化', self.optimize, True)
        actions.addWidget(self.optimize_button)
        clean_layout.addLayout(actions)
        footer = QHBoxLayout()
        self.status = label('就绪 · 所有操作在本机执行', 'muted')
        footer.addWidget(self.status)
        footer.addStretch()
        if not optimizer.is_admin():
            footer.addWidget(self.button('以管理员身份重启', self.restart_admin))
        footer.addWidget(QSizeGrip(self))
        layout.addLayout(footer)

    def toggle_maximized(self):
        # 无边框 + Accent 会被 Windows 错判为全屏；按工作区几何缩放保留任务栏。
        if self._restore_geometry is None:
            self._restore_geometry = self.geometry()
            self.setGeometry(self.screen().availableGeometry())
        else:
            self.setGeometry(self._restore_geometry)
            self._restore_geometry = None

    def refresh(self):
        # Memory polling is cheap, but updating several labels while the
        # translucent window is being moved competes with DWM composition.
        # Defer one refresh tick and keep drag input at the display cadence.
        if self.isMinimized() or self._dragging:
            return
        mem = optimizer.get_mem_info()
        percent = round(mem['percent'])
        memory_text = f"{mem['percent']:.0f}%"
        memory_hint = f"{optimizer.format_bytes(mem['used'])} / {optimizer.format_bytes(mem['total'])}"
        memory_state = (memory_text, memory_hint, percent)
        if memory_state != self._last_memory_state:
            if memory_text != self.memory.text():
                self.memory.setText(memory_text)
            if memory_hint != self.memory_hint.text():
                self.memory_hint.setText(memory_hint)
            if percent != self.memory_bar.value():
                self.memory_bar.setValue(percent)
            self._last_memory_state = memory_state

        uptime_text = optimizer.format_uptime(optimizer.get_uptime_seconds())
        if uptime_text != self._last_uptime_text:
            self.uptime.setText(uptime_text)
            self._last_uptime_text = uptime_text

    def log(self, text):
        self.log_text.appendPlainText(str(text))

    def set_busy(self, busy, text='就绪 · 所有操作在本机执行'):
        self.busy = busy
        if busy:
            self.event_timer.start()
        else:
            self.event_timer.stop()
        self.status.setText(text)
        for widget in self.controls + list(self.checks.values()):
            widget.setEnabled(not busy)

    def update_selection(self):
        if self.scan_ready:
            selected = [t for t in self.targets if self.checks[t.key].isChecked()]
            total = sum(t.scanned for t in selected)
            self.junk.setText(f'已选 {len(selected)} 项 · 预计 {optimizer.format_bytes(total)} · 实际删除量可能更少')

    def reset_selection(self):
        for target in self.targets:
            self.checks[target.key].setChecked(target.default_on)

    def run(self, operation, selected=None):
        self.set_busy(True, {'scan': '正在扫描文件…', 'memory': '正在释放内存…', 'clean': '正在优化…'}[operation])
        def worker():
            try:
                if operation == 'scan':
                    optimizer.scan_all(self.targets, lambda t: self.events.put(('scan_item', (t.key, t.scanned))))
                else:
                    result = optimizer.free_memory()
                    self.events.put(('log', f"工作集减少 {optimizer.format_bytes(result['freed'])}；不代表等量可用内存增加。"))
                    if operation == 'clean':
                        optimizer.clean_selected(selected, lambda t: self.events.put(('clean_item', (t.key, t.cleaned, t.skipped, t.kind))))
                self.events.put(('done', operation))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def scan(self):
        if self.busy:
            return
        self.scan_ready = False
        self.scan_completed = 0
        self.scan_progress.setValue(0)
        self.scan_progress.show()
        self.junk.setText('正在统计文件，扫描不会删除内容…')
        for target in self.targets:
            target.clear()
            self.sizes[target.key].setText('扫描中')
        self.run('scan')

    def free_memory(self):
        if not self.busy and QMessageBox.question(self, '释放内存', '压缩工作集可能让应用短暂变慢，不能修复内存泄漏。继续？') == QMessageBox.StandardButton.Yes:
            self.run('memory')

    def optimize(self):
        if self.busy:
            return
        selected = [t for t in self.targets if self.checks[t.key].isChecked()]
        if not selected:
            self.status.setText('请先勾选需要清理的项目。')
            return
        if not optimizer.is_admin() and any(t.requires_admin for t in selected):
            self.status.setText('所选项目需要管理员权限，请先以管理员身份重启。')
            return
        names = '、'.join(t.name for t in selected)
        if QMessageBox.question(self, '确认优化', f'将释放内存并删除以下项目中的文件：\n{names}\n\n删除无法撤销；释放内存可能导致应用短暂变慢。继续？') != QMessageBox.StandardButton.Yes:
            return
        for target in selected:
            target.clear()
        self.run('clean', selected)

    def drain(self):
        # 限制单次处理量，避免大量后台事件挤占绘制和鼠标事件。
        for _ in range(32):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'scan_item':
                key, size = value
                self.scan_completed += 1
                self.scan_progress.setValue(self.scan_completed)
                self.status.setText(f'正在扫描 · {self.scan_completed}/{len(self.targets)} 项已完成')
                self.sizes[key].setText(optimizer.format_bytes(size) if key != 'dns' else '刷新解析')
            elif kind == 'clean_item':
                key, size, skipped, target_kind = value
                text = '已刷新' if target_kind == 'command' and not skipped else optimizer.format_bytes(size)
                self.sizes[key].setText('部分跳过' if skipped else text)
                self.log(f'{self.checks[key].text()}：{text}，跳过 {skipped} 项。')
            elif kind == 'log':
                self.log(value)
            elif kind == 'error':
                self.log('操作失败：' + value)
                self.scan_progress.hide()
                self.set_busy(False, '操作失败，详情见操作记录')
            elif kind == 'done':
                if value == 'scan':
                    self.scan_ready = True
                    self.scan_progress.hide()
                    self.update_selection()
                elif value == 'clean':
                    self.scan_ready = False
                    self.junk.setText('本轮已处理 · 可重新扫描查看剩余空间')
                self.log('本轮操作结束。')
                self.set_busy(False)
                self.refresh()

    def restart_admin(self):
        if optimizer.restart_as_admin():
            self.status.setText('已启动管理员窗口，正在关闭当前普通窗口…')
            self.close()
        else:
            self.status.setText('已取消管理员授权，当前仍为普通模式。')

    def closeEvent(self, event):
        if self.busy:
            self.status.setText('操作进行中，请完成后再关闭。')
            event.ignore()
        else:
            event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    window = App()
    window.show()
    sys.exit(app.exec())
