import ipaddress
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import lobby_server as lobby


class LobbySecurityTests(unittest.TestCase):
    def setUp(self):
        lobby._rate_windows.clear()
        lobby._sessions.clear()
        lobby._messages.clear()
        lobby.app.config['TRUSTED_PROXY_NETWORKS'] = ()
        self.client = lobby.app.test_client()

    def key(self, peer, forwarded):
        with lobby.app.test_request_context('/', headers={'X-Forwarded-For': forwarded},
                                            environ_base={'REMOTE_ADDR': peer}):
            return lobby._client_key()

    def test_rotating_header_does_not_bypass_send_limit(self):
        codes = [self.client.post('/api/send', json={'player_id': 'Fixture', 'message': 'test'},
                                  headers={'X-Forwarded-For': f'198.51.100.{i}'},
                                  environ_base={'REMOTE_ADDR': '203.0.113.4'}).status_code
                 for i in range(12)]
        self.assertEqual(codes, [200] * 8 + [429] * 4)
        self.assertEqual(len(lobby._rate_windows), 1)

    def test_untrusted_peer_cannot_supply_forwarding_header(self):
        lobby.app.config['TRUSTED_PROXY_NETWORKS'] = (ipaddress.ip_network('10.0.0.0/8'),)
        self.assertEqual(self.key('203.0.113.4', '198.51.100.9'), '203.0.113.4')

    def test_trusted_proxy_stops_before_forged_leftmost(self):
        lobby.app.config['TRUSTED_PROXY_NETWORKS'] = (ipaddress.ip_network('10.0.0.0/8'),)
        self.assertEqual(self.key('10.0.0.2', '192.0.2.123, 198.51.100.9'), '198.51.100.9')

    def test_multiple_trusted_hops(self):
        lobby.app.config['TRUSTED_PROXY_NETWORKS'] = tuple(map(ipaddress.ip_network, ('10.0.0.0/8', '192.0.2.0/24')))
        self.assertEqual(self.key('10.0.0.2', '203.0.113.6, 198.51.100.9, 192.0.2.2'), '198.51.100.9')

    def test_malformed_and_excessive_header_fall_back_to_peer(self):
        lobby.app.config['TRUSTED_PROXY_NETWORKS'] = (ipaddress.ip_network('10.0.0.0/8'),)
        for header in ('not-an-ip', ',198.51.100.1', 'a' * 4096, ','.join(['198.51.100.1'] * 33)):
            self.assertEqual(self.key('10.0.0.2', header), '10.0.0.2')

    def test_ipv6_mapped_ipv4_has_one_identity(self):
        self.assertEqual(self.key('::ffff:203.0.113.4', '198.51.100.9'), '203.0.113.4')

    def test_rate_table_has_hard_bound_and_recovers_after_expiry(self):
        with patch.object(lobby, '_MAX_RATE_WINDOWS', 3), patch.object(lobby.time, 'monotonic', return_value=100):
            for key in ('a', 'b', 'c'):
                self.assertTrue(lobby._allow_request(key))
            self.assertFalse(lobby._allow_request('d'))
            self.assertEqual(len(lobby._rate_windows), 3)
        with patch.object(lobby, '_MAX_RATE_WINDOWS', 3), patch.object(lobby.time, 'monotonic', return_value=111):
            self.assertTrue(lobby._allow_request('d'))
            self.assertEqual(set(lobby._rate_windows), {'d'})

    def test_clock_changes_do_not_reset_rate_limit(self):
        with patch.object(lobby.time, 'monotonic', return_value=100):
            self.assertTrue(lobby._allow_request('a', limit=1))
            with patch.object(lobby.time, 'time', return_value=-100000):
                self.assertFalse(lobby._allow_request('a', limit=1))


if __name__ == '__main__':
    unittest.main()
