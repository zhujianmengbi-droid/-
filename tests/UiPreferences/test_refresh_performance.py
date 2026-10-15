"""Avoid redundant GUI work without removing theme, input or saved state."""
import unittest
from unittest.mock import patch

import test_ui_preferences as shared
from PySide6.QtCore import QEvent, QSettings


class RefreshPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shared.UiPreferencesTests.setUpClass()

    def setUp(self):
        self.fixture = shared.UiPreferencesTests()
        self.fixture.setUp()
        self.window = self.fixture.window
        self.window.refresh_timer.stop()

    def tearDown(self):
        self.fixture.tearDown()

    def test_reselecting_theme_is_idle_but_first_apply_and_switch_work(self):
        self.assertEqual(self.window._applied_theme, 'light')
        self.assertIn('QWidget', self.fixture.app.styleSheet())
        with patch.object(self.fixture.app, 'setStyleSheet', wraps=self.fixture.app.setStyleSheet) as apply:
            self.window.apply_theme('light')
            apply.assert_not_called()
            self.window.theme_buttons['light'].click()
            self.assertTrue(self.window.theme_buttons['light'].isChecked())
            self.window.theme_buttons['light'].click()
            self.assertTrue(self.window.theme_buttons['light'].isChecked())
            self.assertEqual(sum(button.isChecked() for button in self.window.theme_buttons.values()), 1)
            apply.assert_not_called()
            self.window.apply_theme('dark')
            self.assertEqual(apply.call_count, 1)
            self.window.theme_buttons['dark'].click()
            self.assertTrue(self.window.theme_buttons['dark'].isChecked())
            self.assertEqual(apply.call_count, 1)
            self.window.apply_theme('light')
            self.assertEqual(apply.call_count, 2)
        self.assertEqual(self.window.current_theme, 'light')

    def test_non_input_events_skip_scroll_filter_but_keys_still_arrive(self):
        motion = self.window._scroll_motion
        with patch.object(motion, 'handle_event', return_value=False) as handle:
            event = QEvent(QEvent.Type.UpdateRequest)
            self.assertFalse(self.window.eventFilter(self.window, event))
            handle.assert_not_called()
            event = QEvent(QEvent.Type.KeyPress)
            self.assertFalse(self.window.eventFilter(self.window, event))
            handle.assert_called_once_with(self.window, event, QEvent.Type.KeyPress)

    def test_unrelated_preferences_keep_status_timer_and_chips_untouched(self):
        window = self.window
        timer = window._flashback_status_timer
        timer.start()
        original_id = timer.timerId()
        with patch.object(window, '_refresh_recent_query_buttons') as recent, \
                patch.object(window, '_apply_log_surface') as logs:
            window._set_ui_preference('flashback/followLog', False)
            window._set_ui_preference('match/cardGap', 13)
            self.assertEqual(window.match_rows_layout.spacing(), 13)
            self.assertEqual(timer.timerId(), original_id)
            recent.assert_not_called()
            logs.assert_not_called()
            window._set_ui_preference('flashback/logFontSize', 15)
            logs.assert_called_once()
            window._set_ui_preference('match/recentLimit', 3)
            recent.assert_called_once()
            window._set_ui_preference('flashback/statusInterval', 3000)
            self.assertEqual(timer.interval(), 3000)
        timer.stop()

    def test_output_directory_typing_never_resolves_filesystem(self):
        from pathlib import Path
        with patch.object(Path, 'resolve', side_effect=AssertionError('UI resolved a path')):
            self.window.flashback_output.setText(str(self.fixture.directory / 'new recordings'))
        self.assertEqual(self.window._flashback_output_dir, self.fixture.directory / 'new recordings')
        self.assertTrue(self.window._flashback_output_dirty)

    def test_volume_drag_updates_live_and_flushes_final_value_on_close(self):
        settings = self.window._settings
        with patch.object(settings, 'sync', wraps=settings.sync) as sync:
            for volume in range(40, 51):
                self.window._set_chat_sound_volume(volume)
            sync.assert_not_called()
            self.assertEqual(self.window.chat_sound_volume, 50)
            self.assertTrue(all(slider.value() == 50 for slider in self.window._chat_sound_sliders))
            self.window.close()
            self.assertGreaterEqual(sync.call_count, 1)
        persisted = QSettings(str(self.fixture.ini), QSettings.Format.IniFormat)
        self.assertEqual(int(persisted.value(shared.ui.CHAT_SOUND_VOLUME_KEY)), 50)


if __name__ == '__main__':
    unittest.main()
