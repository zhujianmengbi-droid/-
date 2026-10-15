"""A short native-window entrance, with no widget resizing or splash screen."""
import ctypes
import sys

from PySide6.QtCore import (
    QEasingCurve, QEvent, QObject, QParallelAnimationGroup, QPoint,
    QPointF, QPropertyAnimation,
)
from PySide6.QtWidgets import QApplication, QWidget


_ENTRANCE_EVENTS = frozenset((
    QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress,
    QEvent.Type.Close, QEvent.Type.Hide,
))


def system_motion_enabled():
    if sys.platform != 'win32':
        return True
    enabled = ctypes.c_int(1)
    try:
        if ctypes.windll.user32.SystemParametersInfoW(
                0x1042, 0, ctypes.byref(enabled), 0):  # SPI_GETCLIENTAREAANIMATION
            return bool(enabled.value)
    except (AttributeError, OSError):
        pass
    return True


class WindowEntrance(QObject):
    """Lift the existing surface into place; hand control over on any input."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.group = QParallelAnimationGroup(self)
        self._finished = False
        self._target = QPoint(window.pos())

    def start(self):
        window = self.window
        speed = getattr(window, 'animation_speed', 'standard')
        if speed == 'off' or not system_motion_enabled():
            window.setWindowOpacity(1.0)
            window.show()
            self.finish()
            return

        duration = 140 if speed == 'fast' else 180
        # Native window translation does not reflow or rescale child widgets.
        # The restrained lift reads as one surface settling onto the desktop.
        origin = self._target + QPoint(0, 8)
        window.move(origin)
        window.setWindowOpacity(0.0)
        window.show()
        window.raise_()
        window.activateWindow()

        curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
        curve.addCubicBezierSegment(
            QPointF(0.23, 1.0), QPointF(0.32, 1.0), QPointF(1.0, 1.0))
        for name, start, end, length in (
            (b'pos', origin, self._target, duration),
            (b'windowOpacity', 0.0, 1.0, min(140, duration)),
        ):
            animation = QPropertyAnimation(window, name, self.group)
            animation.setStartValue(start)
            animation.setEndValue(end)
            animation.setDuration(length)
            animation.setEasingCurve(curve)
            self.group.addAnimation(animation)
        QApplication.instance().installEventFilter(self)
        self.group.finished.connect(self.finish)
        self.group.start()

    def eventFilter(self, watched, event):
        kind = event.type()
        # Showing the window sends many child layout/paint/style events. Only
        # user input and closing/hiding can interrupt the entrance.
        if kind not in _ENTRANCE_EVENTS:
            return False
        if kind == QEvent.Type.Hide:
            if watched is self.window:
                self.finish()
        elif isinstance(watched, QWidget) and watched.window() is self.window:
            self.finish()
        return False

    def finish(self):
        if self._finished:
            return
        self._finished = True
        self.group.stop()
        QApplication.instance().removeEventFilter(self)
        # Do not reset position/geometry here: a click, drag, or maximize can
        # interrupt the entrance and must keep the user's new window position.
        self.window.setWindowOpacity(1.0)
        self.window._startup_entrance = None
        self.deleteLater()


def reveal_main_window(window):
    previous = getattr(window, '_startup_entrance', None)
    if previous is not None:
        previous.finish()
    screen = window.screen() or QApplication.primaryScreen()
    if screen is not None:
        geometry = window.geometry()
        available = screen.availableGeometry()
        if not available.contains(geometry.center()):
            geometry.moveCenter(available.center())
            window.setGeometry(geometry)
    entrance = WindowEntrance(window)
    window._startup_entrance = entrance
    entrance.start()
