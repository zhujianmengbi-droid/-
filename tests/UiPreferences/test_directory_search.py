"""Slow launcher discovery never blocks Qt or applies stale UI results."""
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QEvent, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

import test_ui_preferences as shared
from onboarding import DirectorySearch


class DirectorySearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shared.UiPreferencesTests.setUpClass()

    def setUp(self):
        self.fixture = shared.UiPreferencesTests()
        self.fixture.setUp()
        self.window = self.fixture.window
        self.window.refresh_timer.stop()
        self.release = threading.Event()

    def tearDown(self):
        self.release.set()
        self.window._quick_start_dialog = None
        self.window._cancel_directory_search('_quick_start_search')
        self.window._cancel_directory_search('_flashback_directory_search')
        self.fixture.tearDown()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(5)
        self.assertTrue(predicate(), 'directory worker did not finish')

    def test_find_runs_off_gui_and_preserves_result_save(self):
        gui_thread = threading.get_ident()
        called_threads = []
        fired = []
        target = self.fixture.directory / 'game'

        def finder(cancelled):
            called_threads.append(threading.get_ident())
            self.release.wait(2)
            return target

        with patch.object(self.window, '_find_flashback_game_directory', finder), \
                patch.object(self.window, '_flashback_save_config') as save:
            self.window._flashback_find_target()
            QTimer.singleShot(0, lambda: fired.append(True))
            self.wait_for(lambda: bool(fired))
            self.assertFalse(self.release.is_set())
            save.assert_not_called()
            self.release.set()
            self.wait_for(lambda: self.window._flashback_directory_search is None)
            self.assertEqual(self.window.flashback_target.text(), str(target))
            self.assertTrue(self.window._flashback_target_dirty)
            save.assert_called_once_with()
        self.assertTrue(called_threads)
        self.assertNotEqual(called_threads[0], gui_thread)

    def test_late_result_keeps_manual_edit_and_repeated_request_shares_worker(self):
        finder = Mock(side_effect=lambda cancelled: (
            self.release.wait(2), self.fixture.directory / 'found')[1])
        with patch.object(self.window, '_find_flashback_game_directory', finder), \
                patch.object(self.window, '_flashback_save_config') as save:
            self.window._flashback_find_target()
            worker = self.window._flashback_directory_search
            self.window._flashback_find_target()
            self.assertIs(self.window._flashback_directory_search, worker)
            self.window.flashback_target.setText('manually chosen directory')
            self.release.set()
            self.wait_for(lambda: self.window._flashback_directory_search is None)
            self.assertEqual(self.window.flashback_target.text(), 'manually chosen directory')
            save.assert_not_called()
            finder.assert_called_once()

    def test_manual_picker_opens_without_discovery_or_initial_filesystem_probe(self):
        self.window.flashback_target.setText('current chosen path')
        with patch.object(self.window, '_find_flashback_game_directory') as find, \
                patch.object(self.window, '_is_flashback_game_directory') as valid, \
                patch.object(shared.ui.QFileDialog, 'getExistingDirectory', return_value='') as choose:
            self.window._flashback_choose_target()
            find.assert_not_called()
            valid.assert_not_called()
            self.assertEqual(choose.call_args.args[2], 'current chosen path')

    def test_guide_can_search_again_after_finished_worker_is_deleted(self):
        dialog = SimpleNamespace(directory_found=Mock())
        self.window._quick_start_dialog = dialog
        target = str(self.fixture.directory / 'game')
        with patch.object(self.window, '_find_flashback_game_directory',
                          side_effect=lambda cancelled: target):
            for _ in range(2):
                self.window._quick_start_find_directory()
                self.wait_for(lambda: self.window._quick_start_search is None)
                self.fixture.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(dialog.directory_found.call_count, 2)
        dialog.directory_found.assert_called_with(target, '')

    def test_closing_guide_discards_slow_result_without_waiting(self):
        dialog = SimpleNamespace(directory_found=Mock())
        self.window._quick_start_dialog = dialog
        with patch.object(self.window, '_find_flashback_game_directory',
                          side_effect=lambda cancelled: (self.release.wait(2), 'late path')[1]):
            self.window._quick_start_find_directory()
            self.window._quick_start_dialog_finished(0)
            self.assertIsNone(self.window._quick_start_search)
            self.assertFalse(self.release.is_set())
            self.fixture.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.release.set()
            QTest.qWait(80)
        dialog.directory_found.assert_not_called()

    def test_owner_destruction_cancels_worker_without_waiting_on_disk(self):
        owner = QWidget()
        finished = Mock()
        worker = DirectorySearch(lambda: (self.release.wait(2), 'late path')[1], owner)
        cancelled = worker._cancelled
        worker.finished.connect(finished)
        worker.start()
        owner.deleteLater()
        self.fixture.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.release.is_set())
        self.release.set()
        QTest.qWait(80)
        finished.assert_not_called()

    def test_missing_directory_and_search_failure_do_not_save(self):
        with patch.object(self.window, '_flashback_save_config') as save:
            for result, error in ((None, ''), (None, 'disk unavailable')):
                self.window._flashback_directory_found(self.window.flashback_target.text(), result, error)
            save.assert_not_called()

    def test_import_validates_and_finds_target_off_gui_without_extra_config_save(self):
        source = self.fixture.directory / 'source.flashback.zip'
        source.touch()
        target = str(self.fixture.directory / 'game')
        called_threads = []

        def validate(path):
            called_threads.append(threading.get_ident())
            return False

        with patch.object(shared.ui.QFileDialog, 'getOpenFileName', return_value=(str(source), '')), \
                patch.object(self.window, '_is_flashback_game_directory', validate), \
                patch.object(self.window, '_find_flashback_game_directory', return_value=target) as find, \
                patch.object(self.window, '_flashback_request', return_value=True) as request, \
                patch.object(self.window, '_flashback_save_config') as save:
            self.window._flashback_import()
            self.wait_for(lambda: self.window._flashback_directory_search is None)
            request.assert_called_once_with('import', path=str(source), target=target,
                                            uiImportGeneration=1, openAfterImport=False)
            find.assert_called_once()
            save.assert_not_called()
        self.assertNotEqual(called_threads[0], threading.get_ident())
        self.assertIsNone(self.window._flashback_pending_open_path)

    def test_new_install_intent_replaces_old_scan_and_opens_only_matching_response(self):
        first = self.fixture.directory / 'first.flashback.zip'
        second = self.fixture.directory / 'second.flashback.zip'
        first.touch(); second.touch()
        target = str(self.fixture.directory / 'game')
        self.window.flashback_target.setText(target)

        def validate(path):
            self.release.wait(2)
            return True

        with patch.object(self.window, '_is_flashback_game_directory', validate), \
                patch.object(self.window, '_find_flashback_game_directory') as find, \
                patch.object(self.window, '_flashback_request', return_value=True) as request, \
                patch.object(self.window, '_open_flashback_replay') as open_replay:
            self.window._install_and_open_flashback_replay(first)
            self.window._install_and_open_flashback_replay(second)
            self.release.set()
            self.wait_for(lambda: self.window._flashback_directory_search is None)
            request.assert_called_once_with('import', path=str(second), target=target,
                                            uiImportGeneration=2, openAfterImport=True)
            find.assert_not_called()
            current = {'command': 'import', **request.call_args.kwargs}
            stale = dict(current, path=str(first), uiImportGeneration=1)
            self.window._flashback_open_import_result(stale, {
                'command': 'import', 'importedReplay': 'old-installed.zip'})
            self.window._flashback_open_import_result({'command': 'status'}, {
                'command': 'status', 'importedReplay': 'old-installed.zip'})
            QTest.qWait(140)
            open_replay.assert_not_called()
            self.assertEqual(self.window._flashback_pending_open_path, str(second))
            self.window._flashback_open_import_result(current, {
                'command': 'import', 'importedReplay': 'correct-installed.zip'})
            QTest.qWait(140)
            open_replay.assert_called_once_with('correct-installed.zip')
            self.assertIsNone(self.window._flashback_pending_open_path)

    def test_import_scan_manual_target_change_cancels_action(self):
        source = self.fixture.directory / 'source.flashback.zip'
        source.touch()
        with patch.object(self.window, '_is_flashback_game_directory',
                          side_effect=lambda path: (self.release.wait(2), True)[1]), \
                patch.object(self.window, '_flashback_request', return_value=True) as request:
            self.window._begin_flashback_import(source, open_after=True)
            self.window.flashback_target.setText('new manual destination')
            self.release.set()
            self.wait_for(lambda: self.window._flashback_directory_search is None)
            request.assert_not_called()
            self.assertIsNone(self.window._flashback_pending_open_path)

    def test_import_failure_does_not_leave_open_intent_or_busy_state(self):
        source = self.fixture.directory / 'source.flashback.zip'
        source.touch()
        self.window.flashback_target.setText('chosen target')
        with patch.object(self.window, '_is_flashback_game_directory', return_value=True), \
                patch.object(self.window, '_flashback_request', return_value=False):
            self.window._begin_flashback_import(source, open_after=True)
            self.wait_for(lambda: self.window._flashback_directory_search is None)
            self.assertIsNone(self.window._flashback_pending_open_path)
            self.assertFalse(self.window._flashback_busy)


if __name__ == '__main__':
    unittest.main()
