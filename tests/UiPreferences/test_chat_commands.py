"""Local lobby commands and presence snapshots; never connect or broadcast."""
import json
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent

import test_ui_preferences as shared

ui = shared.ui


def meta(name, ref, **extra):
    return dict(user_id=name, phx_ref=ref, **extra)


def frame(client, event, payload, **overrides):
    message = dict(topic=client._topic, join_ref=client._join_ref,
                   event=event, payload=payload)
    message.update(overrides)
    client._on_message(json.dumps(message))


class PresenceCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shared.UiPreferencesTests.setUpClass()

    def setUp(self):
        self.client = ui.SupabaseRealtimeChatClient()
        self.client._player_id = 'Viewer'
        self.client._joined = True
        self.client._join_ref = '42'

    def tearDown(self):
        self.client.stop()
        self.client.deleteLater()
        shared.UiPreferencesTests.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_snapshot_merges_player_ids_but_preserves_connection_counts(self):
        frame(self.client, 'presence_state', {
            'private-a': {'metas': [meta('Alice', 'a'), meta('Alice', 'a')]},
            'private-b': {'metas': [meta('alice', 'b')]},
            'private-c': {'metas': [meta('Bob', 'c')]},
            'never-display-this-key': {'metas': [{'phx_ref': 'secret-ref', 'token': 'secret-token'}]},
        })
        self.assertEqual(self.client.online_players(), (('Alice', 2), ('Bob', 1)))

    def test_joins_merge_and_leave_removes_only_the_matching_connection(self):
        frame(self.client, 'presence_state', {'one-key': {'metas': [meta('Alice', 'a')]}})
        joins = {'one-key': {'metas': [meta('Alice', 'b'), meta('Bob', 'c')]}}
        frame(self.client, 'presence_diff', {'joins': joins})
        frame(self.client, 'presence_diff', {'joins': joins})
        self.assertEqual(self.client.online_players(), (('Alice', 2), ('Bob', 1)))
        frame(self.client, 'presence_diff', {'leaves': {'one-key': {'metas': [meta('Alice', 'a')]}}})
        self.assertEqual(self.client.online_players(), (('Alice', 1), ('Bob', 1)))
        frame(self.client, 'presence_diff', {'joins': {
            'one-key': {'metas': [meta('Alice', 'b-new', phx_ref_prev='b')]}}})
        self.assertEqual(self.client.online_players(), (('Alice', 1), ('Bob', 1)))
        frame(self.client, 'presence_diff', {'leaves': {
            'one-key': {'metas': [meta('Alice', 'b-new')]}}})
        self.assertEqual(self.client.online_players(), (('Bob', 1),))

    def test_missing_state_is_not_zero_players_and_foreign_frames_are_ignored(self):
        self.assertIsNone(self.client.online_players())
        frame(self.client, 'presence_diff', {'joins': {'a': {'metas': [meta('Alice', 'a')]}}})
        self.assertIsNone(self.client.online_players())
        frame(self.client, 'presence_state', {}, topic='realtime:another-room')
        frame(self.client, 'presence_state', {}, join_ref='old-join')
        self.assertIsNone(self.client.online_players())
        frame(self.client, 'presence_state', {})
        self.assertEqual(self.client.online_players(), (('Alice', 1),))
        frame(self.client, 'presence_state', {})
        self.assertEqual(self.client.online_players(), ())

    def test_initial_diffs_replay_in_order_after_snapshot_and_clear_on_reset(self):
        frame(self.client, 'presence_diff', {'joins': {'b': {'metas': [meta('Bob', 'b')]}}})
        frame(self.client, 'presence_diff', {'leaves': {'a': {'metas': [meta('Alice', 'a')]}}})
        self.assertIsNone(self.client.online_players())
        frame(self.client, 'presence_state', {'a': {'metas': [meta('Alice', 'a')]}})
        self.assertEqual(self.client.online_players(), (('Bob', 1),))
        self.assertEqual(self.client._presence_pending_diffs, [])
        self.client._reset_presence()
        frame(self.client, 'presence_diff', {'joins': {'b': {'metas': [meta('Bob', 'b')]}}})
        self.client._on_disconnected()
        self.assertEqual(self.client._presence_pending_diffs, [])

    def test_disconnect_stop_and_new_socket_clear_previous_roster(self):
        frame(self.client, 'presence_state', {'a': {'metas': [meta('Alice', 'a')]}})
        self.client._on_disconnected()
        self.assertIsNone(self.client.online_players())
        self.assertEqual(self.client._presence_state, {})
        frame(self.client, 'presence_state', {'a': {'metas': [meta('Alice', 'a')]}})
        self.assertEqual(self.client._presence_state, {})
        self.client._wanted = True
        with patch.object(self.client._socket, 'open') as open_socket:
            self.client._open_socket()
            open_socket.assert_called_once()
        self.assertFalse(self.client.presence_synced)
        self.client.stop()
        self.assertEqual(self.client._presence_state, {})
        self.assertFalse(self.client.presence_synced)

    def test_malformed_metadata_and_missing_leave_ref_do_not_erase_players(self):
        frame(self.client, 'presence_state', {
            'a': {'metas': [meta('Alice', 'a'), None, {'user_id': {'token': 'x'}},
                            {'name': '<b>Bob</b>', 'phx_ref': {'bad': 'ref'}}]},
            'b': {'metas': 'invalid'}, 'c': None,
        })
        original = self.client.online_players()
        frame(self.client, 'presence_diff', {'joins': {'a': {'metas': [meta('Cara', 'c')]}},
                                            'leaves': {'a': {'metas': [{'user_id': 'Alice'}]}}})
        self.assertIn(('Alice', 1), self.client.online_players())
        self.assertIn(('<b>Bob</b>', 1), self.client.online_players())
        self.assertIn(('Cara', 1), self.client.online_players())
        self.assertEqual(len(original), 2)

    def test_transport_cannot_broadcast_any_slash_command(self):
        with patch.object(self.client, '_send') as send:
            for text in ('/help', '/player', ' /unknown ', '/help extra'):
                self.assertFalse(self.client.send_message(text))
            send.assert_not_called()
        self.assertEqual(self.client._pending_messages, {})


class ChatCommandUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shared.UiPreferencesTests.setUpClass()

    def setUp(self):
        self.fixture = shared.UiPreferencesTests()
        self.fixture.setUp()
        self.window = self.fixture.window
        self.window.refresh_timer.stop()
        self.network_patches = [patch.object(self.window._chat_client, 'start'),
                                patch.object(self.window._chat_client, 'send_message')]
        self.start, self.send = [item.start() for item in self.network_patches]

    def tearDown(self):
        for item in reversed(self.network_patches):
            item.stop()
        self.fixture.tearDown()

    def test_help_button_is_available_without_login_and_keeps_chat_gate(self):
        self.assertFalse(self.window.chat_input.isEnabled())
        self.assertFalse(self.window.chat_send_button.isEnabled())
        self.assertTrue(self.window.chat_help_button.isEnabled())
        self.window.chat_help_button.click()
        text = self.window.chat_command_output.toPlainText()
        self.assertIn('/help', text)
        self.assertIn('/player', text)
        self.assertTrue(self.window.chat_command_output.isReadOnly())
        self.assertEqual(self.window._chat_messages, [])
        self.assertEqual(self.window._chat_pending_message, '')
        self.assertEqual(self.window._chat_sending_message, '')
        self.window.chat_input.setText('ordinary message')
        self.window._send_lobby_chat()
        self.start.assert_not_called()
        self.send.assert_not_called()
        self.assertEqual(self.window._chat_pending_message, '')

    def test_input_commands_and_unknown_commands_never_enter_public_queue(self):
        for command, expected in (('/HELP', '全部'), ('/player', '尚未登录'),
                                  ('/no-such-command', '未知指令'), ('/player extra', '用法')):
            self.window.chat_input.setText(command)
            self.window._send_lobby_chat()
            self.assertIn(expected, self.window.chat_command_output.toPlainText())
            self.assertEqual(self.window.chat_input.text(), '')
            self.assertEqual(self.window._chat_pending_message, '')
            self.assertEqual(self.window._chat_sending_message, '')
        self.assertFalse(self.window._begin_chat_message_send('/unknown'))
        self.start.assert_not_called()
        self.send.assert_not_called()
        self.assertEqual(self.window._chat_messages, [])

    def test_player_result_tracks_sync_and_clears_names_on_disconnect(self):
        client = self.window._chat_client
        client._joined, client._join_ref = True, '42'
        with patch.object(self.window, '_chat_bound_game_id', return_value='Viewer'):
            self.window._run_lobby_command('/player')
            self.assertIn('等待', self.window.chat_command_output.toPlainText())
            self.assertEqual(self.window.chat_online_count.text(), '—')
            frame(client, 'presence_state', {
                'private-a': {'metas': [meta('<b>Alice</b>', 'a')]},
                'private-b': {'metas': [meta('<b>alice</b>', 'b')]},
                'private-c': {'metas': [meta('Bob', 'c')]},
            })
            text = self.window.chat_command_output.toPlainText()
            self.assertIn('在线玩家：2 人 · 在线连接：3 个', text)
            self.assertIn('<b>Alice</b>（2 个连接）', text)
            self.assertIn('不是 Minecraft 服务器', text)
            self.assertNotIn('private-', text)
            self.assertEqual(self.window.chat_online_count.text(), '3')
            client._on_disconnected()
            text = self.window.chat_command_output.toPlainText()
            self.assertIn('旧名单已清除', text)
            self.assertNotIn('Alice', text)
            self.assertNotIn('Bob', text)
            self.assertEqual(self.window.chat_online_count.text(), '—')
        self.start.assert_not_called()
        self.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
