"""Offscreen behavior checks; no game, network requests, or global hotkeys."""
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QAbstractAnimation, QEvent, QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QFontDatabase, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QScrollArea
import main_large as ui
from ui_preferences import DEFAULTS, normalize


class UiPreferencesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        # Windows' offscreen platform starts with an empty font database.
        # Load installed fonts for realistic glyphs and layout measurements.
        for name in ('msyh.ttc', 'msyhbd.ttc', 'segoeui.ttf', 'seguisym.ttf', 'segmdl2.ttf'):
            font = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'Fonts' / name
            if font.is_file():
                QFontDatabase.addApplicationFont(str(font))
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='zj-ui-check-')
        self.directory = Path(self.scratch.name)
        self.ini = self.directory / 'settings.ini'
        self.patches = [
            patch.object(ui, 'QSettings', lambda *_args: QSettings(str(self.ini), QSettings.Format.IniFormat)),
            patch.object(ui.bugland_api, 'load_token', lambda: ''),
            patch.object(ui.TaskbarHotkeyWorker, 'start', lambda _self: None),
            patch.object(ui.LargeApp, '_start_update_check', lambda _self: None),
            patch.object(ui.LargeApp, '_flashback_refresh', lambda _self: None),
            patch.object(ui, 'APP_LOG_ROOT', self.directory / 'logs'),
        ]
        for item in self.patches:
            item.start()
        self.window = ui.LargeApp()
        self.window._scroll_motion._system_enabled = True
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.scratch.cleanup()

    def show_match_list(self):
        self.window.resize(1180, 800)
        self.window.page_stack.setCurrentIndex(self.window._section_page_indices['memory'])
        self.window._render_match_records([
            {'type': f'测试对局 {index}', 'date': '2026-10-16 09:00:00', 'win': True}
            for index in range(30)
        ])
        self.window.show()
        self.app.processEvents()
        return self.window.match_scroll.verticalScrollBar()

    def wheel(self, delta):
        viewport = self.window.match_scroll.viewport()
        event = QWheelEvent(QPointF(20, 20), QPointF(20, 20), QPoint(0, 0), QPoint(0, delta),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(viewport, event)

    def test_brand_and_log_order(self):
        labels = self.window.findChildren(QLabel)
        self.assertTrue(any(label.text() == 'OOPZ 交流频道' for label in labels))
        self.assertTrue(any(label.text() == '881695' for label in labels))
        self.assertFalse(any(label.text().lower() == 'dev king' for label in labels))
        content = self.window.findChild(ui.QWidget, 'flashbackPageContent')
        layout = content.layout()
        log = self.window.findChild(QFrame, 'flashbackLogCard')
        replay = self.window.findChild(QFrame, 'flashbackReplayCard')
        replay_row = next(index for index in range(layout.count())
                          if layout.itemAt(index).layout() and layout.itemAt(index).layout().indexOf(replay) >= 0)
        self.assertLess(layout.indexOf(log), replay_row)

    def test_preferences_save_reload_and_restore(self):
        controls = self.window._preference_controls
        controls['match/recentLimit'].setValue(8)
        controls['match/cardGap'].setValue(14)
        controls['flashback/logFontSize'].setValue(16)
        controls['flashback/statusInterval'].setCurrentIndex(3)
        controls['flashback/followLog'].setChecked(False)
        for index in range(12):
            self.window._remember_recent_query(f'玩家{index}')
        self.assertEqual(len(self.window._recent_queries), 8)
        self.assertEqual(sum(not item.isHidden() for item in self.window.recent_query_buttons), 8)
        self.assertEqual(self.window.match_rows_layout.spacing(), 14)
        self.assertEqual(self.window._flashback_status_timer.interval(), 5000)
        self.assertIn('font-size: 16px', self.window.flashback_log.styleSheet())
        self.window._settings.sync()
        other = ui.LargeApp()
        try:
            self.assertEqual(other._ui_preferences, self.window._ui_preferences)
            self.assertEqual(other._recent_queries, self.window._recent_queries)
            self.assertEqual(other.match_rows_layout.spacing(), 14)
        finally:
            other.close(); other.deleteLater()
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.window._reset_ui_preferences(list(DEFAULTS))
        self.assertEqual(self.window._ui_preferences, DEFAULTS)
        self.assertEqual(self.window.match_rows_layout.spacing(), 5)
        self.assertEqual(controls['flashback/logFontSize'].value(), 11)
        self.assertEqual(self.window._flashback_status_timer.interval(), 1500)

    def test_manual_paging_remains_available_with_autoload_off(self):
        self.window._preference_controls['match/autoLoad'].setChecked(False)
        self.assertFalse(self.window.match_next_button.isHidden())
        self.window._match_player_name = '测试玩家'
        self.window._api_generation = 1
        self.window._history_pages = {1: []}
        calls = []
        self.window._start_api_request = lambda *args, **kwargs: calls.append((args, kwargs))
        self.window._match_records_accumulated = [{'type': '测试', 'win': True} for _ in range(10)]
        self.window._show_history_page(1)
        self.assertTrue(self.window.match_next_button.isEnabled())
        self.window._maybe_load_next_match_page()
        self.assertEqual(calls, [])
        self.window.match_next_button.click()
        self.window.match_next_button.click()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][2]['page'], '2')
        self.window._history_has_more = False
        self.window._show_history_page(2)
        self.assertFalse(self.window.match_next_button.isEnabled())

    def test_log_retention_follow_and_reading_position(self):
        self.window.page_stack.setCurrentIndex(self.window._section_page_indices['flashback'])
        self.window.show()
        self.app.processEvents()
        self.window._preference_controls['flashback/logLines'].setValue(50)
        for index in range(100):
            self.window._flashback_append_log(f'测试日志 {index}')
        log = self.window.flashback_log
        self.assertEqual(log.document().blockCount(), 50)
        bar = log.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        self.window._preference_controls['flashback/followLog'].setChecked(False)
        bar.setValue(3)
        self.window._flashback_append_log('关闭跟随后的新日志')
        self.assertEqual(bar.value(), 3)
        self.window._preference_controls['flashback/followLog'].setChecked(True)
        self.window._flashback_append_log('重新开启跟随后的新日志')
        self.assertEqual(bar.value(), bar.maximum())

    def test_replay_limit_changes_only_displayed_files(self):
        folder = self.directory / 'replays'
        folder.mkdir()
        for index in range(35):
            path = folder / f'{index:02}.flashback.zip'
            path.write_bytes(b'fixture')
            os.utime(path, (1000 + index, 1000 + index))
        self.window._flashback_replay_dirs = lambda: [folder]
        self.window._preference_controls['flashback/replayLimit'].setValue(5)
        self.assertEqual(len(self.window._flashback_replays()), 5)
        self.assertEqual(self.window._flashback_replays()[0].name, '34.flashback.zip')
        self.window._preference_controls['flashback/replayLimit'].setValue(30)
        self.assertEqual(len(self.window._flashback_replays()), 30)
        self.assertEqual(len(list(folder.iterdir())), 35)

    def test_invalid_persisted_preferences_are_bounded(self):
        self.assertEqual(normalize('flashback/logLines', 999999), 2000)
        self.assertEqual(normalize('flashback/logFontSize', 'broken'), 11)
        self.assertEqual(normalize('flashback/statusInterval', 2222), 1500)
        self.assertFalse(normalize('match/autoLoad', 'false'))
        self.assertEqual(normalize('match/recentLimit', -10), 1)

    def test_wheel_can_accumulate_reverse_and_release_control(self):
        bar = self.show_match_list()
        self.assertGreater(bar.maximum(), 300)
        self.wheel(-120)
        QTest.qWait(50)
        self.assertGreater(bar.value(), 0)
        first_position = bar.value()
        self.wheel(-120)
        QTest.qWait(240)
        self.assertGreater(bar.value(), first_position)
        self.assertLess(self.window.backdrop._parallax_offset, 0)
        cache_key = self.window.backdrop._background_cache.cacheKey()
        self.wheel(120)
        QTest.qWait(240)
        self.assertLess(bar.value(), first_position + 168)
        self.assertEqual(self.window.backdrop._background_cache.cacheKey(), cache_key)
        self.wheel(-120)
        QTest.qWait(30)
        bar.sliderPressed.emit()
        position = bar.value()
        QTest.qWait(240)
        self.assertEqual(bar.value(), position)

    def test_motion_off_is_native_and_leaves_no_running_animation(self):
        bar = self.show_match_list()
        self.wheel(-120)
        QTest.qWait(30)
        self.window._preference_controls['ui/parallaxScrolling'].setChecked(False)
        self.assertEqual(self.window.backdrop._parallax_offset, 0)
        before = bar.value()
        self.wheel(-120)
        self.assertGreater(bar.value(), before)
        self.assertTrue(all(state['animation'].state() == QAbstractAnimation.State.Stopped
                            for state in self.window._scroll_motion._states.values()))
        self.window._preference_controls['ui/parallaxScrolling'].setChecked(True)
        self.window._set_animation_speed('off')
        before = bar.value()
        self.wheel(-120)
        self.assertGreater(bar.value(), before)
        self.assertEqual(self.window.backdrop._parallax_offset, 0)

    def test_touchpad_pixels_and_windows_reduced_motion(self):
        bar = self.show_match_list()
        event = QWheelEvent(QPointF(20, 20), QPointF(20, 20), QPoint(0, -70), QPoint(0, 0),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.ScrollUpdate, False)
        QApplication.sendEvent(self.window.match_scroll.viewport(), event)
        self.assertEqual(bar.value(), 70)
        self.assertTrue(all(state['animation'].state() == QAbstractAnimation.State.Stopped
                            for state in self.window._scroll_motion._states.values()))
        self.window._scroll_motion._system_enabled = False
        before = bar.value()
        self.wheel(-120)
        self.assertGreater(bar.value(), before)
        self.assertEqual(self.window.backdrop._parallax_offset, 0)

    def test_destroying_a_scroller_stops_its_animation(self):
        area = QScrollArea(self.window.centralWidget())
        content = ui.QWidget()
        content.setMinimumSize(180, 2500)
        area.setWidget(content)
        area.resize(250, 200)
        self.window.show()
        area.show()
        self.app.processEvents()
        event = QWheelEvent(QPointF(20, 20), QPointF(20, 20), QPoint(0, 0), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(area.viewport(), event)
        self.assertIn(area, self.window._scroll_motion._states)
        area.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QTest.qWait(220)
        self.assertNotIn(area, self.window._scroll_motion._states)
        self.assertEqual(self.window.backdrop._parallax_offset, 0)

    def test_window_close_releases_running_scroll_controller(self):
        self.show_match_list()
        self.wheel(-120)
        controller = self.window._scroll_motion
        self.window.close()
        self.assertIsNone(self.window._scroll_motion)
        self.assertIsNone(controller.window)
        self.assertFalse(controller._states)

    def test_fresh_interpreter_exits_normally_after_widget_cleanup(self):
        destination = self.directory / 'fresh-startup.json'
        env = os.environ.copy()
        for name in ('LOCALAPPDATA', 'APPDATA', 'USERPROFILE', 'TEMP', 'TMP'):
            folder = self.directory / name
            folder.mkdir(exist_ok=True)
            env[name] = str(folder)
        process = subprocess.run(
            [sys.executable, '-X', 'faulthandler', str(ROOT / 'main_large.py'), '--check-new-pc', str(destination)],
            env=env, capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr.decode('utf-8', errors='replace'))
        import json
        self.assertTrue(json.loads(destination.read_text(encoding='utf-8'))['ok'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
