"""Shared desktop controls with explicit surfaces and native value semantics."""
from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QApplication, QBoxLayout, QCheckBox, QFrame, QSlider, QSpinBox, QToolButton
from theme_redesign import SURFACES as REDESIGNED_SURFACES


SURFACES = {
    'light': dict(fill='#ffffff', text='#252b33', muted='#69727e', border='#d3d9e0',
                  focus='#9b8060', hover='#f0f2f5', selection='#ddc9af',
                  track='#d9dee4', on='#343b43', divider='#e7eaee', page='#f3f4f6', card='#ffffff'),
    'dark': dict(fill='#20262e', text='#edf0f4', muted='#a3acb8', border='#424b57',
                 focus='#c7aa84', hover='#303944', selection='#65543f',
                 track='#414b58', on='#c8d0d9', divider='#333d49', page='#151c25', card='#1c2530'),
    'blue': dict(fill='#102840', text='#edf3fa', muted='#a9bbce', border='#3f5872',
                 focus='#d0b58f', hover='#203b56', selection='#655741',
                 track='#38516d', on='#d0deed', divider='#2c4662', page='#0b2038', card='#112d49'),
}


SURFACES.update(REDESIGNED_SURFACES)


def surface_for(theme=None):
    if theme is None:
        app = QApplication.instance()
        theme = app.property('_themeKey') if app is not None else 'light'
    return SURFACES.get(theme, SURFACES['light'])


class SettingsRow(QFrame):
    """Place controls below their descriptions when the settings pane is narrow."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._stacked = None
        self.setMinimumHeight(80)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        layout = self.layout()
        stacked = self.width() < 400
        if isinstance(layout, QBoxLayout) and stacked != self._stacked:
            self._stacked = stacked
            layout.setDirection(QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight)
            layout.setSpacing(10 if stacked else 16)
            self.updateGeometry()


class StepButton(QToolButton):
    def __init__(self, direction, parent):
        super().__init__(parent)
        self.direction = direction
        self.setObjectName('numberStepButton')
        self.setAccessibleName('增加数值' if direction > 0 else '减少数值')
        self.setToolTip('增加' if direction > 0 else '减少')
        self.setAutoRepeat(True)
        self.setAutoRepeatDelay(350)
        self.setAutoRepeatInterval(80)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def paintEvent(self, event):
        colors = surface_for()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.isEnabled() and (self.underMouse() or self.isDown()):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colors['border'] if self.isDown() else colors['hover']))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 6, 6)
        painter.setPen(QPen(QColor(colors['text'] if self.isEnabled() else colors['border']), 1.6,
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        x, y = self.width() / 2, self.height() / 2
        painter.drawLine(QPointF(x - 4, y), QPointF(x + 4, y))
        if self.direction > 0:
            painter.drawLine(QPointF(x, y - 4), QPointF(x, y + 4))
        painter.end()


class NumberField(QSpinBox):
    """QSpinBox behavior with balanced horizontal step buttons and one surface."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedHeight(38)
        self.setMinimumWidth(140)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.decrease_button = StepButton(-1, self)
        self.increase_button = StepButton(1, self)
        self.decrease_button.clicked.connect(self.stepDown)
        self.increase_button.clicked.connect(self.stepUp)
        self.valueChanged.connect(self._sync_buttons)
        self.lineEdit().setObjectName('numberEditor')
        self.lineEdit().setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lineEdit().setFrame(False)
        self.refresh_theme()
        self._sync_buttons()

    def sizeHint(self):
        return QSize(140, 38)

    def minimumSizeHint(self):
        return QSize(140, 38)

    def refresh_theme(self):
        colors = surface_for()
        editor = self.lineEdit()
        palette = editor.palette()
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
            palette.setColor(group, QPalette.ColorRole.Base, QColor(colors['fill']))
            palette.setColor(group, QPalette.ColorRole.Text, QColor(colors['muted'] if group == QPalette.ColorGroup.Disabled else colors['text']))
        editor.setPalette(palette)
        editor.setStyleSheet(f"""
QLineEdit#numberEditor {{ background: {colors['fill']}; color: {colors['text']};
    border: none; border-radius: 0; padding: 0; min-height: 0;
    font-size: 13px; font-weight: 600; selection-background-color: {colors['selection']}; }}
QLineEdit#numberEditor:disabled {{ color: {colors['muted']}; }}
""")
        self.update()
        self.decrease_button.update()
        self.increase_button.update()

    def _sync_buttons(self, *_args):
        if not hasattr(self, 'decrease_button'):
            return
        self.decrease_button.setEnabled(not self.isReadOnly() and (self.wrapping() or self.value() > self.minimum()))
        self.increase_button.setEnabled(not self.isReadOnly() and (self.wrapping() or self.value() < self.maximum()))

    def setRange(self, minimum, maximum):
        super().setRange(minimum, maximum)
        self._sync_buttons()

    def setMinimum(self, value):
        super().setMinimum(value)
        self._sync_buttons()

    def setMaximum(self, value):
        super().setMaximum(value)
        self._sync_buttons()

    def setReadOnly(self, enabled):
        super().setReadOnly(enabled)
        self._sync_buttons()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'decrease_button'):
            self.decrease_button.setGeometry(4, 5, 28, 28)
            self.increase_button.setGeometry(self.width() - 32, 5, 28, 28)
            self.lineEdit().setGeometry(34, 2, max(1, self.width() - 68), self.height() - 4)

    def paintEvent(self, event):
        colors = surface_for()
        focused = self.hasFocus() or self.lineEdit().hasFocus()
        edge = colors['focus'] if focused else colors['border']
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(edge), 1.5 if focused else 1))
        painter.setBrush(QColor(colors['fill']))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.8, .8, -.8, -.8), 9, 9)
        painter.end()

    def event(self, event):
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut, QEvent.Type.Enter,
                            QEvent.Type.Leave, QEvent.Type.EnabledChange):
            self.update()
        return super().event(event)

    def wheelEvent(self, event):
        if self.hasFocus() or self.lineEdit().hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class ToggleSwitch(QCheckBox):
    def __init__(self, text='', parent=None):
        super().__init__(text, parent)
        self.setFixedSize(44, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.toggled.connect(self.update)

    def sizeHint(self):
        return QSize(44, 26)

    def hitButton(self, position):
        return self.rect().contains(position)

    def paintEvent(self, event):
        colors = surface_for()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(.45)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colors['on'] if self.isChecked() else colors['track']))
        painter.drawRoundedRect(QRectF(1, 2, 42, 22), 11, 11)
        knob = '#ffffff' if not self.isChecked() or surface_for() is SURFACES['light'] else colors['card']
        painter.setBrush(QColor(knob))
        painter.drawEllipse(QPointF(32 if self.isChecked() else 12, 13), 8, 8)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(colors['focus']), 1.2))
            painter.drawRoundedRect(QRectF(.6, .6, 42.8, 24.8), 12, 12)
        painter.end()


class VolumeSlider(QSlider):
    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedHeight(28)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


def paint_combo_arrow(widget):
    colors = surface_for()
    painter = QPainter(widget)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(colors['text'] if widget.isEnabled() else colors['muted']), 1.5,
                        Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    x, y = widget.width() - 19, widget.height() / 2
    path = QPainterPath()
    path.moveTo(x - 3.5, y - 1.5)
    path.lineTo(x, y + 2)
    path.lineTo(x + 3.5, y - 1.5)
    painter.drawPath(path)
    painter.end()


def scrollbar_style(theme):
    """Style every subcontrol so Windows cannot supply native arrow buttons.

    Keep the native QScrollBar for drag, keyboard, page clicks and smooth
    wheel scrolling. Its 12px lane contains an inset 8px rounded thumb.
    """
    c = surface_for(theme)
    return f"""
QScrollBar:vertical {{ background: transparent; border: none; width: 12px; margin: 0 2px; }}
QScrollBar:horizontal {{ background: transparent; border: none; height: 12px; margin: 2px 0; }}
QScrollBar::handle:vertical {{ background: {c['muted']}; border: none; border-radius: 4px; min-height: 32px; }}
QScrollBar::handle:horizontal {{ background: {c['muted']}; border: none; border-radius: 4px; min-width: 32px; }}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{ background: {c['text']}; }}
QScrollBar::handle:vertical:pressed, QScrollBar::handle:horizontal:pressed {{ background: {c['on']}; }}
QScrollBar::handle:vertical:disabled, QScrollBar::handle:horizontal:disabled {{ background: {c['track']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0; height: 0; border: none; background: transparent; }}
QScrollBar::up-arrow, QScrollBar::down-arrow, QScrollBar::left-arrow, QScrollBar::right-arrow {{
    image: none; width: 0; height: 0; border: none; background: transparent; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; border: none; }}
QAbstractScrollArea::corner {{ background: transparent; border: none; }}
"""


def control_style(theme):
    c = surface_for(theme)
    combos = ', '.join('QComboBox' + name for name in ('', '#animationSpeed', '#leaderboardFilter', '#flashbackProcessCombo', '#preferenceControl'))
    return f"""
QLabel#title, QLabel#pageTitle {{ font-size: 24px; font-weight: 700; }}
QLabel#brand {{ font-size: 16px; font-weight: 600; }}
QLabel#heroTitle {{ font-size: 20px; font-weight: 600; }}
QLabel#section {{ font-size: 16px; font-weight: 600; }}
QLabel#cardTitle {{ font-size: 14px; font-weight: 600; }}
QLabel#cardHint, QLabel#pageHint, QLabel#muted, QLabel#brandHint {{ font-size: 12px; }}
QLabel#communityCaption {{ font-size: 12px; font-weight: 600; }}
QLabel#communityNumber {{ font-size: 16px; font-weight: 700; }}
QPushButton {{ padding: 8px 12px; }}
{combos} {{ color: {c['text']}; background: {c['fill']};
    border: 1px solid {c['border']}; border-radius: 9px;
    min-height: 20px; padding: 8px 34px 8px 12px; font-size: 13px; }}
QComboBox::drop-down, QComboBox#animationSpeed::drop-down,
QComboBox#leaderboardFilter::drop-down, QComboBox#flashbackProcessCombo::drop-down,
QComboBox#preferenceControl::drop-down {{ width: 28px; border: none; background: transparent; }}
QComboBox::down-arrow, QComboBox#animationSpeed::down-arrow,
QComboBox#leaderboardFilter::down-arrow, QComboBox#flashbackProcessCombo::down-arrow,
QComboBox#preferenceControl::down-arrow {{ image: none; width: 0; height: 0; }}
QComboBox:focus, QComboBox#animationSpeed:focus, QComboBox#leaderboardFilter:focus,
QComboBox#flashbackProcessCombo:focus, QComboBox#preferenceControl:focus {{
    border-color: {c['focus']}; background: {c['fill']}; padding: 8px 34px 8px 12px; }}
QComboBox#animationSpeed:hover, QComboBox#leaderboardFilter:hover,
QComboBox#flashbackProcessCombo:hover, QComboBox#preferenceControl:hover {{
    background: {c['fill']}; border-color: {c['focus']}; }}
QSpinBox, QSpinBox#preferenceControl, QSpinBox#flashbackRate {{ padding: 0; border: none; background: transparent; min-height: 38px; max-height: 38px; }}
QToolButton#numberStepButton {{ background: transparent; border: none; padding: 0; }}
QCheckBox#preferenceControl, QCheckBox#chatSoundEnabled {{ background: transparent; border: none; padding: 0; min-height: 0; }}
QSlider {{ background: transparent; border: none; }}
QSlider::groove:horizontal {{ background: {c['track']}; height: 4px; border: none; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {c['on']}; border-radius: 2px; }}
QSlider::add-page:horizontal {{ background: {c['track']}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {c['fill']}; border: 2px solid {c['on']};
    width: 14px; height: 14px; margin: -6px 0; border-radius: 8px; }}
QSlider::handle:horizontal:hover {{ border-color: {c['focus']}; }}
QFrame#settingsOverlay {{ background: {c['page']}; }}
QFrame#settingsSidebar {{ background: transparent; border: none; }}
QFrame#settingsCard {{ background: {c['card']}; border: 1px solid {c['divider']}; border-radius: 16px; }}
QFrame#settingsCard QLabel#section {{ font-size: 18px; font-weight: 600; }}
QFrame#settingsOverlay QLabel#eyebrow {{ color: {c['muted']}; font-size: 11px; font-weight: 600; }}
QFrame#preferenceRow, QFrame#settingsCard QFrame#featureRow {{
    background: transparent; border: none; border-bottom: 1px solid {c['divider']}; border-radius: 0; }}
QPushButton#settingsCategory {{ color: {c['muted']}; background: transparent; border: 1px solid transparent; padding: 9px 12px; }}
QPushButton#settingsCategory:checked {{ color: {c['fill']}; background: {c['on']}; border: 1px solid {c['on']}; }}
QPushButton#settingsCategory:hover:!checked {{ color: {c['text']}; background: {c['hover']}; }}
QPushButton#settingsReset {{ color: {c['text']}; background: {c['fill']}; border: 1px solid {c['border']};
    border-radius: 8px; min-height: 18px; padding: 7px 12px; font-size: 12px; }}
QPushButton#settingsReset:hover {{ background: {c['hover']}; }}
""" + scrollbar_style(theme)
