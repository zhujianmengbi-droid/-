"""Message updates preserve existing selectable UI and avoid redundant work."""
import copy
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QLabel
from shiboken6 import isValid

import test_ui_preferences as shared


class ChatPerformanceTests(unittest.TestCase):
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

    @staticmethod
    def message(index, sender='player', text=None):
        return {'id': str(index), 'player_id': sender,
                'message': text or f'Message {index}',
                'created_at': '2026-10-16T09:00:00+08:00'}

    def bubbles(self):
        layout = self.window.chat_messages_layout
        return [layout.itemAt(index).widget() for index in range(layout.count())
                if layout.itemAt(index).widget() is not None
                and layout.itemAt(index).widget().objectName() == 'chatMessageBubble']

    def test_append_reuses_visible_messages_and_preserves_text_selection(self):
        messages = [self.message(index) for index in range(80)]
        self.window._render_lobby_messages(messages)
        before = self.bubbles()
        body = before[10].findChild(QLabel, 'chatMessageText')
        body.setSelection(0, 7)
        incoming = messages + [self.message(80)]
        original = copy.deepcopy(incoming)
        with patch.object(self.window, '_create_chat_bubble',
                          wraps=self.window._create_chat_bubble) as create:
            self.window._render_lobby_messages(incoming)
            self.assertEqual(create.call_count, 1)
        after = self.bubbles()
        self.assertEqual(len(after), 80)
        self.assertTrue(all(old is new for old, new in zip(before[1:], after[:-1])))
        self.assertEqual(body.selectedText(), 'Message')
        self.assertEqual(body.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(incoming, original)
        self.fixture.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(before[0]))

    def test_duplicate_ids_content_edits_reordering_and_empty_state(self):
        first = self.message('same', text='First')
        second = self.message('same', text='Second')
        self.window._render_lobby_messages([first, second, first])
        old = self.bubbles()
        self.assertEqual(len({id(widget) for widget in old}), 3)
        self.window._render_lobby_messages([second, first, first])
        reordered = self.bubbles()
        self.assertIs(reordered[0], old[1])
        self.assertIs(reordered[1], old[0])
        self.assertIs(reordered[2], old[2])
        edited = dict(second, message='Edited')
        self.window._render_lobby_messages([edited, first, first])
        changed = self.bubbles()
        self.assertIsNot(changed[0], old[1])
        self.assertEqual(changed[0].findChild(QLabel, 'chatMessageText').text(), 'Edited')
        self.assertIs(changed[1], old[0])
        self.window._render_lobby_messages([])
        self.fixture.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertTrue(all(not isValid(widget) for widget in changed))
        self.assertFalse(self.window.chat_empty_label.isHidden())
        self.assertEqual(self.window.chat_messages_layout.count(), 2)
        self.window._render_lobby_messages([first])
        self.assertEqual(len(self.bubbles()), 1)
        self.assertTrue(self.window.chat_empty_label.isHidden())

    def test_identity_change_refreshes_mine_style_and_keeps_scroll_behavior(self):
        messages = [self.message('a', sender='Alice'), self.message('b', sender='Bob')]
        with patch.object(self.window, '_chat_bound_game_id', return_value='alice'):
            self.window._render_lobby_messages(messages)
        before = self.bubbles()
        self.assertEqual([widget.property('mine') for widget in before], ['true', 'false'])
        with patch.object(self.window, '_chat_bound_game_id', return_value='bob'), \
                patch.object(self.window, '_schedule_chat_scroll_to_latest') as scroll:
            self.window._render_lobby_messages(messages)
            self.assertEqual(scroll.call_count, 1)
            self.window._render_lobby_messages(messages)
            self.assertEqual(scroll.call_count, 1)
        self.assertTrue(all(old is new for old, new in zip(before, self.bubbles())))
        self.assertEqual([widget.property('mine') for widget in before], ['false', 'true'])

    def test_unchanged_taskbar_results_keep_selection_and_updates_still_apply(self):
        payload = {'action': 'refresh', 'items': [
            {'name': 'Editor', 'title': 'First document'},
            {'name': 'Browser', 'title': 'Second window'}]}
        self.window._on_taskbar_action_completed(payload)
        self.window.taskbar_table.selectRow(1)
        cell = self.window.taskbar_table.item(1, 1)
        with patch.object(self.window, '_populate_taskbar_table',
                          wraps=self.window._populate_taskbar_table) as populate:
            self.window._on_taskbar_action_completed(copy.deepcopy(payload))
            self.assertEqual(populate.call_count, 0)
            self.assertIs(self.window.taskbar_table.item(1, 1), cell)
            changed = copy.deepcopy(payload)
            changed['items'][1]['title'] = 'Updated title'
            self.window._on_taskbar_action_completed(changed)
            self.assertEqual(populate.call_count, 1)
        self.assertEqual(self.window.taskbar_table.currentRow(), 1)
        self.assertEqual(self.window.taskbar_table.item(1, 1).toolTip(), 'Updated title')
        self.window.taskbar_search.setText('Editor')
        self.assertEqual(self.window.taskbar_table.rowCount(), 1)
        self.window._on_taskbar_action_completed({'action': 'refresh', 'items': []})
        self.assertEqual(self.window.taskbar_table.rowCount(), 0)


if __name__ == '__main__':
    unittest.main()
