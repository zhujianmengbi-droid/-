import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from runtime_paths import SessionSkinCache


class SessionCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_cache_is_reused_then_removed_and_recreated(self):
        cache = SessionSkinCache([self.root / 'cache'])
        first = cache.path()
        (first / 'skin.png').write_bytes(b'fixture')
        self.assertEqual(first, cache.path())
        cache.cleanup()
        self.assertFalse(first.exists())
        second = cache.path()
        self.assertNotEqual(first, second)
        cache.cleanup()

    def test_other_instance_and_player_files_survive(self):
        one = SessionSkinCache([self.root / 'cache'])
        two = SessionSkinCache([self.root / 'cache'])
        first, second = one.path(), two.path()
        player = self.root / 'player/flashback/recorded_skins/skin.png'
        player.parent.mkdir(parents=True)
        player.write_bytes(b'player asset')
        one.cleanup()
        self.assertFalse(first.exists())
        self.assertTrue(second.is_dir())
        self.assertEqual(player.read_bytes(), b'player asset')
        two.cleanup()

    def test_invalid_first_location_uses_fallback(self):
        blocked = self.root / 'not-a-directory'
        blocked.write_bytes(b'x')
        cache = SessionSkinCache([blocked / 'cache', self.root / 'fallback'])
        self.assertEqual(cache.path().parent, self.root / 'fallback')
        cache.cleanup()

    def test_all_locations_invalid_is_clear_error(self):
        blocked = self.root / 'not-a-directory'; blocked.write_bytes(b'x')
        with self.assertRaisesRegex(OSError, '临时皮肤缓存'):
            SessionSkinCache([blocked / 'cache']).path()

    def test_foreign_ownership_marker_prevents_deletion(self):
        cache = SessionSkinCache([self.root / 'cache'])
        folder = cache.path()
        (folder / '.zj-cache-owner').write_text('different-owner', encoding='ascii')
        with self.assertRaisesRegex(OSError, '归属'):
            cache.cleanup()
        self.assertTrue(folder.exists())

    def test_repeated_cleanup_is_safe(self):
        cache = SessionSkinCache([self.root / 'cache'])
        cache.path(); cache.cleanup(); cache.cleanup()

    def test_stale_owned_cache_is_removed_but_unknown_directory_survives(self):
        base = self.root / 'cache'; base.mkdir()
        stale = base / 'session-99999999-stale'; stale.mkdir()
        (stale / '.zj-cache-owner').write_text('ZJHUB-SKIN-CACHE\n99999999\n' + 'a' * 32, encoding='ascii')
        foreign = base / 'session-99999999-foreign'; foreign.mkdir()
        (foreign / '.zj-cache-owner').write_text('not our cache', encoding='ascii')
        cache = SessionSkinCache([base]); cache.path()
        self.assertFalse(stale.exists())
        self.assertTrue(foreign.exists())
        cache.cleanup()


if __name__ == '__main__':
    unittest.main()
