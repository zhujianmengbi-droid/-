"""Exercise custom controls with Qt events and inspect their actual rendering."""
import unittest

import test_ui_preferences as shared
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from ui_controls import NumberField, SettingsRow, surface_for


class ControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shared.UiPreferencesTests.setUpClass()

    def setUp(self):
        self.fixture = shared.UiPreferencesTests()
        self.fixture.setUp()
        self.window = self.fixture.window
        self.window.resize(1180, 800)
        self.window.show()
        self.window.activateWindow()
        self.window.show_settings_overlay()
        self.window.switch_settings_category('flashback')
        self.fixture.app.processEvents()

    def tearDown(self):
        self.fixture.tearDown()

    def test_step_buttons_keyboard_and_bounds_keep_native_values(self):
        field = self.window._preference_controls['flashback/logFontSize']
        self.assertIsInstance(field, NumberField)
        field.increase_button.click()
        self.assertEqual(field.value(), 12)
        self.assertEqual(self.window._ui_preferences['flashback/logFontSize'], 12)
        field.decrease_button.click()
        self.assertEqual(field.value(), 11)
        field.setValue(field.minimum())
        self.assertFalse(field.decrease_button.isEnabled())
        field.setValue(field.maximum())
        self.assertFalse(field.increase_button.isEnabled())
        field.lineEdit().setFocus()
        field.lineEdit().selectAll()
        QTest.keyClicks(field.lineEdit(), '15')
        QTest.keyClick(field.lineEdit(), Qt.Key.Key_Return)
        self.assertEqual(field.value(), 15)
        self.assertEqual(self.window._ui_preferences['flashback/logFontSize'], 15)
        rate = self.window.flashback_rate
        rate.increase_button.click()
        self.assertEqual(rate.value(), 30)
        self.assertTrue(self.window._flashback_rate_dirty)

    def test_surfaces_and_height_match_inside_and_outside_settings(self):
        inside = self.window._preference_controls['flashback/logLines']
        outside = self.window.flashback_rate
        for theme in ('light', 'dark', 'blue'):
            self.window.apply_theme(theme)
            self.fixture.app.processEvents()
            for field in (inside, outside):
                with self.subTest(theme=theme, field=field.objectName()):
                    self.assertEqual(field.height(), 38)
                    self.assertGreaterEqual(field.lineEdit().height(), 30)
                    self.assertLessEqual(field.increase_button.geometry().bottom(), field.height())
                    image = field.grab().toImage()
                    self.assertEqual(image.pixelColor(40, 6), QColor(surface_for(theme)['fill']))
                    field.setFocus()
                    self.fixture.app.processEvents()
                    self.assertEqual(field.grab().toImage().pixelColor(40, 6), QColor(surface_for(theme)['fill']))
                    field.setEnabled(False)
                    self.assertEqual(field.grab().toImage().pixelColor(40, 6), QColor(surface_for(theme)['fill']))
                    field.setEnabled(True)
            combo = self.window._preference_controls['flashback/statusInterval']
            combo.setFocus()
            self.fixture.app.processEvents()
            self.assertEqual(combo.height(), 38)
            self.assertEqual(combo.grab().toImage().pixelColor(40, 6), QColor(surface_for(theme)['fill']))

    def test_unfocused_wheel_does_not_change_a_setting(self):
        field = self.window._preference_controls['flashback/logLines']
        other = self.window._preference_controls['flashback/logFontSize']
        other.setFocus()
        self.fixture.app.processEvents()
        old = field.value()
        event = QWheelEvent(QPointF(20, 20), QPointF(20, 20), QPoint(), QPoint(0, 120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(field, event)
        self.assertEqual(field.value(), old)
        field.setFocus()
        self.fixture.app.processEvents()
        event = QWheelEvent(QPointF(20, 20), QPointF(20, 20), QPoint(), QPoint(0, 120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(field, event)
        self.assertGreater(field.value(), old)

    def test_switch_uses_its_whole_hit_area_and_supports_keyboard(self):
        toggle = self.window._preference_controls['flashback/followLog']
        self.assertEqual(toggle.size().width(), 44)
        QTest.mouseClick(toggle, Qt.MouseButton.LeftButton, pos=QPoint(42, 13))
        self.assertFalse(toggle.isChecked())
        self.assertFalse(self.window._ui_preferences['flashback/followLog'])
        toggle.setFocus()
        QTest.keyClick(toggle, Qt.Key.Key_Space)
        self.assertTrue(toggle.isChecked())
        self.assertTrue(self.window._ui_preferences['flashback/followLog'])

    def test_narrow_settings_stack_controls_without_clipping(self):
        self.window.setMinimumSize(320, 240)
        self.window.resize(640, 560)
        self.fixture.app.processEvents()
        field = self.window._preference_controls['flashback/logLines']
        row = field.parentWidget()
        self.assertIsInstance(row, SettingsRow)
        self.assertTrue(row._stacked)
        description = row.findChild(shared.ui.QLabel, 'cardHint')
        description_bottom = description.mapTo(row, QPoint()).y() + description.height()
        self.assertGreaterEqual(field.y(), description_bottom)
        self.assertEqual(field.height(), 38)
        self.window.resize(1180, 800)
        self.fixture.app.processEvents()
        self.assertFalse(row._stacked)
        self.assertEqual(field.height(), 38)


if __name__ == '__main__':
    unittest.main(verbosity=2)
