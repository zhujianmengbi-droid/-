"""Interruptible wheel scrolling and a bounded, cached backdrop parallax."""
import ctypes
import os
import weakref

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QEvent, QObject, QPropertyAnimation, Qt
from PySide6.QtWidgets import QApplication, QAbstractItemView, QAbstractScrollArea, QScrollArea


# This controller receives the application's paint/layout/timer events too.
# Reject those before traversing widgets or rebuilding an event-type tuple.
MOTION_EVENTS = frozenset((
    QEvent.Type.WindowDeactivate, QEvent.Type.Wheel,
    QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress, QEvent.Type.Hide,
))


def system_motion_enabled():
    if os.name != 'nt':
        return True
    enabled = ctypes.c_int(1)
    try:
        result = ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0)
        return not result or bool(enabled.value)
    except (AttributeError, OSError):
        return True


class ScrollMotion(QObject):
    def __init__(self, window, backdrop, enabled, duration):
        super().__init__(window)
        self.window = window
        self.backdrop = backdrop
        self.enabled = enabled
        self.duration = duration
        self._states = weakref.WeakKeyDictionary()
        self._active = None
        self._system_enabled = system_motion_enabled()

    def _area(self, watched):
        if not hasattr(watched, 'parentWidget'):
            return None
        parent = watched.parentWidget()
        # A scrollbar has an intermediate Qt container. Do not intercept
        # children such as spin boxes, editable fields or combo popups.
        for _ in range(3):
            if isinstance(parent, QAbstractScrollArea):
                if (watched is parent.viewport() or watched is parent.verticalScrollBar()) and parent.window() is self.window:
                    return parent
                return None
            if parent is None:
                break
            parent = parent.parentWidget()
        return None

    def _state(self, area):
        state = self._states.get(area)
        if state is not None:
            return state
        if isinstance(area, QAbstractItemView):
            area.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        bar = area.verticalScrollBar()
        animation = QPropertyAnimation(bar, b'value', self)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        state = {'animation': animation, 'target': float(bar.value()), 'direction': 0}
        self._states[area] = state
        reference = weakref.ref(area)
        state['connections'] = [
            bar.sliderPressed.connect(lambda: self.stop_area(reference())),
            bar.rangeChanged.connect(lambda _low, _high: self.stop_area(reference())),
            bar.valueChanged.connect(lambda value: self._progress(reference(), value)),
            area.destroyed.connect(lambda: self._dispose(state, reference)),
        ]
        return state

    def _dispose(self, state, reference):
        if state.get('disposed'):
            return
        state['disposed'] = True
        for connection in state.pop('connections', []):
            try:
                QObject.disconnect(connection)
            except (RuntimeError, TypeError):
                pass
        area = reference()
        if area is not None:
            self._states.pop(area, None)
        if self._active is not None and self._active() is area:
            self._active = None
            try:
                self.backdrop.set_parallax_offset(0.0)
            except RuntimeError:
                pass
        try:
            state['animation'].stop()
            state['animation'].deleteLater()
        except RuntimeError:
            pass

    def _progress(self, area, value):
        if area is None or self._active is None or self._active() is not area:
            return
        if not self.enabled() or not self._system_enabled:
            return
        bar = area.verticalScrollBar()
        distance = max(1, bar.maximum() - bar.minimum())
        self.backdrop.set_parallax_offset(-24.0 * (value - bar.minimum()) / distance)

    def stop_area(self, area):
        state = self._states.get(area) if area is not None else None
        if state is not None:
            state['animation'].stop()
            state['target'] = float(area.verticalScrollBar().value())
            state['direction'] = 0

    def reset(self):
        for area in list(self._states):
            try:
                self.stop_area(area)
            except RuntimeError:
                continue
        self._active = None
        self.backdrop.set_parallax_offset(0.0)

    def shutdown(self):
        if self.window is None:
            return
        self.reset()
        for area, state in list(self._states.items()):
            self._dispose(state, weakref.ref(area))
        self._states.clear()
        # Release Python callbacks and the owner reference before Qt destroys
        # its children. Leaving this QObject/owner cycle to interpreter GC can
        # enter deleted PySide wrappers during Python 3.14 shutdown.
        self.enabled = None
        self.duration = None
        self.backdrop = None
        self.window = None
        self.deleteLater()

    def handle_event(self, watched, event, event_type=None):
        if event_type is None:
            event_type = event.type()
        if event_type not in MOTION_EVENTS:
            return False
        if event_type == QEvent.Type.WindowDeactivate and watched is self.window:
            self.reset()
            return False
        if event_type == QEvent.Type.WindowDeactivate:
            return False
        if event_type == QEvent.Type.Hide and isinstance(watched, QAbstractScrollArea):
            self.stop_area(watched)
            if self._active is not None and self._active() is watched:
                self._active = None
                self.backdrop.set_parallax_offset(0.0)
            return False
        area = self._area(watched)
        if area is None:
            return False
        if event_type != QEvent.Type.Wheel:
            self.stop_area(area)
            # Keyboard and scrollbar dragging retain their native response.
            self._active = None
            self.backdrop.set_parallax_offset(0.0)
            return False
        if not self.enabled() or not self._system_enabled:
            self.reset()
            return False
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            self.stop_area(area)
            return False
        # Text editors keep native text/line scrolling; only page and item
        # viewports use pixel interpolation, without changing editable input.
        if not isinstance(area, (QScrollArea, QAbstractItemView)):
            return False
        pixel_delta = event.pixelDelta()
        angle_delta = event.angleDelta()
        if abs(angle_delta.x()) > abs(angle_delta.y()) or abs(pixel_delta.x()) > abs(pixel_delta.y()):
            return False
        delta = pixel_delta.y() or angle_delta.y()
        if not delta:
            return False
        state = self._state(area)
        bar = area.verticalScrollBar()
        direction = 1 if delta < 0 else -1
        running = state['animation'].state() == QAbstractAnimation.State.Running
        origin = state['target'] if running and direction == state['direction'] else float(bar.value())
        step = -pixel_delta.y() if pixel_delta.y() else -angle_delta.y() / 120.0 * QApplication.wheelScrollLines() * max(1, bar.singleStep())
        target = max(bar.minimum(), min(bar.maximum(), origin + step))
        state['animation'].stop()
        state['target'] = target
        state['direction'] = direction
        if round(target) == bar.value():
            # Let an enclosing scroller receive a wheel gesture at the edge.
            return False
        self._active = weakref.ref(area)
        if pixel_delta.y():
            # High-resolution touchpads already supply continuous pixels.
            bar.setValue(round(target))
        else:
            state['animation'].setDuration(max(70, min(220, self.duration(160))))
            state['animation'].setStartValue(bar.value())
            state['animation'].setEndValue(round(target))
            state['animation'].start()
        event.accept()
        return True
