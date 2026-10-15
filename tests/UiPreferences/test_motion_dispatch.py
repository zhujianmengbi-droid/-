"""Keep entrance interruption and native input unchanged when filtering events."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QWidget
from startup_motion import WindowEntrance


class CountingWidget(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.owner_queries = 0

    def window(self):
        self.owner_queries += 1
        return super().window()


class EntranceDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QWidget()
        self.child = CountingWidget(self.window)
        self.entrance = WindowEntrance(self.window)
        self.entrance.finish = Mock()

    def tearDown(self):
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_layout_and_paint_do_not_traverse_or_interrupt(self):
        for kind in (QEvent.Type.Paint, QEvent.Type.UpdateRequest,
                     QEvent.Type.LayoutRequest, QEvent.Type.Timer,
                     QEvent.Type.Resize, QEvent.Type.Show):
            self.assertFalse(self.entrance.eventFilter(self.child, QEvent(kind)))
        self.assertEqual(self.child.owner_queries, 0)
        self.entrance.finish.assert_not_called()

    def test_input_and_close_keep_native_handling_and_interrupt(self):
        for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress,
                     QEvent.Type.Close):
            self.entrance.finish.reset_mock()
            self.assertFalse(self.entrance.eventFilter(self.child, QEvent(kind)))
            self.entrance.finish.assert_called_once_with()

    def test_only_own_window_hide_interrupts(self):
        other = QWidget()
        try:
            for watched in (other, self.child):
                self.assertFalse(self.entrance.eventFilter(watched, QEvent(QEvent.Type.Hide)))
            self.assertFalse(self.entrance.eventFilter(other, QEvent(QEvent.Type.KeyPress)))
            self.entrance.finish.assert_not_called()
            self.assertFalse(self.entrance.eventFilter(self.window, QEvent(QEvent.Type.Hide)))
            self.entrance.finish.assert_called_once_with()
        finally:
            other.deleteLater()


if __name__ == '__main__':
    unittest.main(verbosity=2)
