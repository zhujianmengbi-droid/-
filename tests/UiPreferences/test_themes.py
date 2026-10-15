"""Theme switching must preserve the accepted light UI and readable controls."""
import hashlib
import unittest

import test_ui_preferences as shared
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QLabel, QWidget
from theme_redesign import _COLOR, _parse_color


def is_cool(color):
    hue = color.hsvHue()
    return ((155 <= hue <= 325 and color.hsvSaturationF() > .025)
            or (max(color.red(), color.green(), color.blue()) - min(color.red(), color.green(), color.blue()) < 40
                and color.blue() > color.red()))


class ThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shared.UiPreferencesTests.setUpClass()

    def setUp(self):
        self.fixture = shared.UiPreferencesTests()
        self.fixture.setUp()
        self.window = self.fixture.window
        self.window.refresh_timer.stop()
        self.window.resize(1180, 800)
        self.window.show()
        self.fixture.app.processEvents()

    def tearDown(self):
        self.fixture.tearDown()

    def retained_style(self, theme):
        # Only the new command editor and help button extend the stylesheet.
        # Keep the historical hash for every existing control instead of
        # accepting a new full hash that could hide a visual regression.
        lines = shared.ui.build_style(theme).splitlines(keepends=True)
        added = [line for line in lines if line.startswith('QPlainTextEdit#chatCommandOutput ')]
        self.assertEqual(len(added), 1)
        retained = ''.join(line for line in lines if line not in added)
        self.assertEqual(retained.count(', QPushButton#chatHelpButton'), 1)
        return retained.replace(', QPushButton#chatHelpButton', '')

    def test_light_restores_exact_accepted_style_and_log_selection(self):
        # Captured from the backed-up v1.6.24 runtime, not from the new theme.
        self.assertEqual(hashlib.sha256(self.retained_style('light').encode()).hexdigest(),
                         '27cce4836067c082b12e039b0ea6f23b0b2dbb120d22819d402e300758a31a5b')
        roles = (QPalette.ColorRole.Base, QPalette.ColorRole.Text, QPalette.ColorRole.Highlight,
                 QPalette.ColorRole.HighlightedText)
        logs = (self.window.log_text, self.window.flashback_log, self.window.flashback_replay_info)
        before = [[editor.palette().color(role) for role in roles] for editor in logs]
        for theme in ('dark', 'blue'):
            self.window.apply_theme(theme)
            for editor in logs:
                self.assertFalse(is_cool(editor.palette().color(QPalette.ColorRole.Highlight)))
            self.window.apply_theme('light')
            self.fixture.app.processEvents()
            self.assertEqual([[editor.palette().color(role) for role in roles] for editor in logs], before)

    def test_removed_theme_falls_back_and_retained_visuals_are_unchanged(self):
        self.assertEqual(set(self.window.theme_buttons), {'light', 'dark', 'blue'})
        self.window.apply_theme('glass')
        self.assertEqual(self.window.current_theme, 'light')
        self.assertEqual(self.window.backdrop.theme, 'light')
        # Captured from the delivered v1.6.25 before removing liquid glass.
        expected = {
            'dark': 'c633cca7ed10150bfab3fbc357669b8bc7a971455d94046480617122714db088',
            'blue': '0ead9a150d64bcd9f6f580827129549f2efecca651c797bc13fbb91298d01ecc',
        }
        for theme, digest in expected.items():
            self.assertEqual(hashlib.sha256(self.retained_style(theme).encode()).hexdigest(), digest)
            self.window.apply_theme(theme)
            self.fixture.app.processEvents()
            cache = self.window.backdrop._background_cache.cacheKey()
            glow = self.window.backdrop._glow_cache.cacheKey()
            self.window.backdrop.set_parallax_offset(-12)
            self.fixture.app.processEvents()
            self.assertEqual(self.window.backdrop._background_cache.cacheKey(), cache)
            self.assertEqual(self.window.backdrop._glow_cache.cacheKey(), glow)

    def test_redesigned_dialogs_styles_and_dynamic_icons_have_no_cool_accents(self):
        for theme in ('dark', 'blue'):
            self.window.apply_theme(theme)
            login = shared.ui.PlayerLoginDialog('', '', theme)
            guide = shared.ui.QuickStartDialog(self.window, theme)
            tile = shared.ui.icon_tile('shield', color='#bfe1ff')
            try:
                self.window.leaderboard_mode_combo._apply_popup_style()
                sheets = [self.fixture.app.styleSheet(), login.styleSheet(), guide.styleSheet()]
                sheets.extend(widget.styleSheet() for widget in self.window.findChildren(QWidget))
                for sheet in sheets:
                    for match in _COLOR.finditer(sheet):
                        rgb, _alpha = _parse_color(match.group())
                        self.assertFalse(is_cool(QColor(*rgb)), (theme, match.group()))
                pixmap = tile.findChild(QLabel).pixmap().toImage()
                for x in range(pixmap.width()):
                    for y in range(pixmap.height()):
                        pixel = pixmap.pixelColor(x, y)
                        if pixel.alpha() > 128:
                            self.assertFalse(is_cool(pixel), (theme, pixel.name()))
                if theme == 'blue':
                    self.assertIn('暖砂', self.window.theme_buttons[theme].text())
                    self.assertNotIn('深海蓝', self.window.theme_buttons[theme].text())
            finally:
                login.close(); guide.close(); tile.close()
                login.deleteLater(); guide.deleteLater(); tile.deleteLater()
                self.fixture.app.sendPostedEvents(None, shared.QEvent.Type.DeferredDelete)


if __name__ == '__main__':
    unittest.main(verbosity=2)
