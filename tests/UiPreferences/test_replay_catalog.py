"""Replay scans stay off the UI thread and never publish superseded work."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from replay_catalog import ReplayCatalog, read_replay_info, scan_replays
import test_ui_preferences as fixture
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel


def wait_result(catalog):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = catalog.poll()
        if result is not None:
            return result
        time.sleep(0.005)
    raise AssertionError('catalog did not finish')


class ReplayCatalogTests(unittest.TestCase):
    def test_bounded_session_layout_metadata_and_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / 'direct.flashback.zip',
                     root / '.flashback-runtime/session-a/first.flashback.zip',
                     root / '.flashback-runtime/session-b/export/second.flashback.zip']
            ignored = root / '.flashback-runtime/session-b/export/deep/ignored.flashback.zip'
            for index, path in enumerate(paths + [ignored]):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'x' * (index + 1))
                os.utime(path, ns=(1_000_000_000 * (index + 1), 1_000_000_000 * (index + 1)))
            entries = scan_replays((root, root))
            self.assertEqual([item.path for item in entries], paths[::-1])
            self.assertEqual([item.size for item in entries], [3, 2, 1])
            self.assertEqual([item.mtime_ns for item in entries], [3_000_000_000, 2_000_000_000, 1_000_000_000])

    def test_never_traverse_linked_session_or_zip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'output'
            outside = root / 'outside'
            outside.mkdir()
            runtime = output / '.flashback-runtime'
            runtime.mkdir(parents=True)
            replay = outside / 'external.flashback.zip'
            replay.write_bytes(b'outside')
            try:
                (runtime / 'session-linked').symlink_to(outside, target_is_directory=True)
                (output / 'linked.flashback.zip').symlink_to(replay)
            except OSError as error:
                if sys.platform != 'win32':
                    raise
                # Windows directory junctions need no symlink privilege and
                # exercise the same reparse-point traversal boundary.
                import _winapi
                junction = runtime / 'session-linked'
                if not junction.exists():
                    _winapi.CreateJunction(str(outside), str(junction))
            self.assertEqual(scan_replays((output,)), ())

    def test_coalesced_refresh_finds_new_file_after_inflight_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started, release = threading.Event(), threading.Event()
            calls = []

            def slow_scan(directories, cancelled):
                calls.append(directories)
                snapshot = scan_replays(directories, cancelled)
                if len(calls) == 1:
                    started.set()
                    release.wait(3)
                return snapshot

            catalog = ReplayCatalog(scanner=slow_scan)
            try:
                catalog.request((root,))
                self.assertTrue(started.wait(1))
                replay = root / 'new.flashback.zip'
                replay.write_bytes(b'export completed')
                for _ in range(20):
                    catalog.request((root,), force=True)
                release.set()
                result = wait_result(catalog)
                self.assertEqual([item.path for item in result.entries], [replay])
                self.assertEqual(len(calls), 2)
                self.assertFalse(catalog.busy)
            finally:
                release.set()
                catalog.close()

    def test_switch_directory_rejects_old_result_and_close_never_waits(self):
        started, release = threading.Event(), threading.Event()

        def slow_scan(directories, cancelled):
            if directories[0].name == 'old':
                started.set()
                release.wait(3)
            return (directories[0].name,)

        catalog = ReplayCatalog(scanner=slow_scan)
        try:
            catalog.request((Path('old'),))
            self.assertTrue(started.wait(1))
            catalog.request((Path('new'),))
            release.set()
            self.assertEqual(wait_result(catalog).entries, ('new',))
            release.clear()
            started.clear()
            catalog.request((Path('old'),))
            self.assertTrue(started.wait(1))
            begin = time.perf_counter()
            catalog.close()
            self.assertLess(time.perf_counter() - begin, 0.1)
            self.assertFalse(catalog.busy)
            catalog.request((Path('new'),))
            self.assertIsNone(catalog.poll())
        finally:
            release.set()
            catalog.close()

    def test_information_preserves_summary_and_error_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replay = root / 'valid.flashback.zip'
            with zipfile.ZipFile(replay, 'w') as archive:
                archive.writestr('metadata.json', json.dumps({
                    'name': '测试回放', 'version_string': '1.21.10', 'total_ticks': 300,
                    'flashback_recorder': {'source_file': 'capture.bin'}}))
                archive.writestr('chunks/test', b'example')
            summary = read_replay_info(replay)
            for text in ('名称：测试回放', '版本：1.21.10', '总 tick：300',
                         '录制源：capture.bin', '文件：2 项', '· chunks/test'):
                self.assertIn(text, summary)
            with zipfile.ZipFile(replay, 'w') as archive:
                archive.writestr('metadata.json', '[]')
            self.assertIn('metadata.json 不是对象（压缩包目录仍可读取）', read_replay_info(replay))
            replay.write_bytes(b'broken')
            self.assertIn('回放压缩包尚未写完或已损坏', read_replay_info(replay))
            replay.unlink()
            self.assertIn('回放文件已被移动或删除', read_replay_info(replay))


class ReplayUiTests(unittest.TestCase):
    setUpClass = classmethod(fixture.UiPreferencesTests.setUpClass.__func__)
    setUp = fixture.UiPreferencesTests.setUp
    tearDown = fixture.UiPreferencesTests.tearDown

    def test_slow_scan_does_not_block_ui_or_repeat_ui_filesystem_reads(self):
        root = self.directory / 'replays'
        root.mkdir()
        replay = root / 'recent.flashback.zip'
        replay.write_bytes(b'replay')
        owner = threading.get_ident()
        reads = []
        originals = {name: getattr(Path, name) for name in ('stat', 'resolve')}
        original_scandir = os.scandir
        actual_directories = self.window._flashback_replay_dirs

        def guarded(name):
            def read(path, *args, **kwargs):
                self.assertNotEqual(threading.get_ident(), owner, f'UI performed Path.{name}')
                reads.append(name)
                return originals[name](path, *args, **kwargs)
            return read

        def guarded_scandir(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), owner, 'UI performed os.scandir')
            return original_scandir(*args, **kwargs)

        def slow_scan(directories, cancelled):
            time.sleep(0.35)
            return scan_replays(directories, cancelled)

        self.window._flashback_replay_catalog.close()
        self.window._flashback_replay_catalog = ReplayCatalog(scanner=slow_scan)
        self.window._flashback_replay_dirs = lambda: [root]
        pulses = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: pulses.append(time.perf_counter()))
        timer.start()
        try:
            with (patch.object(Path, 'stat', guarded('stat')),
                  patch.object(Path, 'resolve', guarded('resolve')),
                  patch.object(os, 'scandir', guarded_scandir)):
                self.assertTrue(actual_directories())
                begin = time.perf_counter()
                self.window._flashback_refresh_replay_list(force=True)
                elapsed = time.perf_counter() - begin
                self.assertLess(elapsed, 0.15)
                QTest.qWait(500)
            self.assertGreater(len(pulses), 10)
            self.assertTrue(reads)
            self.assertEqual(self.window._flashback_replay_cache, [replay])
            self.assertFalse(self.window._flashback_replay_scan_timer.isActive())
            texts = [item.text() for item in self.window.flashback_replay_host.findChildren(QLabel)]
            self.assertIn('recent', texts)
        finally:
            timer.stop()

    def test_information_selection_discards_old_summary(self):
        started, release = threading.Event(), threading.Event()

        def slow_info(paths, cancelled):
            if paths[0].name == 'old.flashback.zip':
                started.set()
                release.wait(2)
            return (f'回放：{paths[0].name}',)

        self.window._flashback_replay_info_reader.close()
        self.window._flashback_replay_info_reader = ReplayCatalog(scanner=slow_info)
        try:
            begin = time.perf_counter()
            self.window._flashback_show_replay_info('old.flashback.zip')
            self.assertLess(time.perf_counter() - begin, 0.1)
            self.assertTrue(started.wait(1))
            self.window._flashback_show_replay_info('new.flashback.zip')
            release.set()
            QTest.qWait(150)
            self.assertEqual(self.window.flashback_replay_info.toPlainText(), '回放：new.flashback.zip')
            self.assertFalse(self.window._flashback_replay_scan_timer.isActive())
        finally:
            release.set()


if __name__ == '__main__':
    unittest.main(verbosity=2)
