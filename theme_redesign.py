"""ZJ HUB's retained redesigned materials. The light theme is untouched.

The persisted ``blue`` key is intentionally retained for upgrade compatibility;
its presentation is now warm sand. All tokens are usable without importing Qt.
"""
from copy import deepcopy
import colorsys
import re


THEMES = {
    'dark': {
        'name': '暗色',
        'description': '中性炭黑 · 清晰灰阶',
        'icon': 'moon',
        'start': (20, 20, 20, 255),
        'end': (13, 13, 13, 255),
        'glow1': (85, 85, 85, 16),
        'glow2': (50, 50, 50, 10),
    },
    'blue': {
        'name': '暖砂',
        'description': '温润砂色 · 陶土铜棕',
        'icon': 'sun',
        'start': (239, 232, 220, 255),
        'end': (226, 213, 194, 255),
        'glow1': (255, 249, 234, 100),
        'glow2': (194, 155, 109, 22),
    },
}


# These have exactly the same keys as ui_controls.SURFACES['light'].
# Editors remain opaque so the caret and selections have consistent contrast.
SURFACES = {
    'dark': dict(fill='#252525', text='#efefef', muted='#a5a5a5', border='#444444',
                 focus='#c7c7c7', hover='#333333', selection='#565656',
                 track='#424242', on='#dedede', divider='#343434', page='#151515', card='#202020'),
    'blue': dict(fill='#fffaf1', text='#3e3127', muted='#786553', border='#cbbba5',
                 focus='#94633f', hover='#f1e4d0', selection='#e1c5a0',
                 track='#d1c0a9', on='#865936', divider='#e2d5c2', page='#eee5d7', card='#faf4e9'),
}


MATERIALS = {
    'dark': dict(
        sidebar='#181818', surface='#202020', hero='#282828', raised='#292929', inset='#1b1b1b',
        border='#373737', edge='#4a4a4a', button='#292929', hover='#353535',
        pressed='#404040', accent='#e2e2e2', accent_text='#1d1d1d',
        primary_hover='#f5f5f5', primary_down='#c6c6c6',
        selected='#333333', popup='#252525', overlay='#151515',
        community='#3e3529', community_border='#6c5940',
        community_text='#d8bd95', community_number='#f3dbb8',
    ),
    'blue': dict(
        sidebar='#e6dbc9', surface='#faf4e9', hero='#fff9ee', raised='#f2e7d7', inset='#eee2d0',
        border='#dfd0bb', edge='#c4ad8f', button='#f7efdf', hover='#ecdbc1',
        pressed='#dfc8a8', accent='#865936', accent_text='#fffaf1',
        primary_hover='#73492c', primary_down='#613d25',
        selected='#dac3a5', popup='#fffaf1', overlay='#eee5d7',
        community='#f3d7aa', community_border='#c79d64',
        community_text='#79552f', community_number='#50361e',
    ),
}


def _quad(bg, hover, border, text):
    return dict(bg=bg, hover=hover, border=border, text=text)


def semantic_palettes(key):
    """Return fresh ``flashback``, ``match``, ``team`` and ``rank`` dictionaries.

    Win/loss and actual team identifiers retain their meaning. The blue team's
    *label* is unchanged while its decoration becomes neutral silver. Red is
    rust, green is leaf/olive, and podium silver is genuinely neutral gray.
    """
    if key not in SURFACES:
        return {}
    c, m = SURFACES[key], MATERIALS[key]
    if key == 'blue':
        win = dict(bg='#e7ead6', hover='#dce2c5', text='#4b5d2f', meta='#647444', border='#afbd8d')
        loss = dict(bg='#f3dfd2', hover='#eccdb9', text='#8a4930', meta='#9a6448', border='#d0a084')
        ranks = {
            '1': _quad('#f1e0b5', '#ead39a', '#bb9753', '#735420'),
            '2': _quad('#e8e3dc', '#ded7ce', '#aaa094', '#61574b'),
            '3': _quad('#eed8c5', '#e6c7ac', '#bc8b65', '#805638'),
            '4': _quad('#f3ebdf', '#e9dece', '#cfc0aa', '#776650'),
            '5': _quad('#f7f0e5', '#eee3d3', '#ddd0bd', '#8b7761'),
        }
        teams = {
            'red': _quad('#f2dfd4', '#eacdbb', '#c28a68', '#8b4c31'),
            'blue': _quad('#e9e3da', '#ddd5c8', '#a99d8b', '#625647'),
            'yellow': _quad('#f4e7bd', '#edda9e', '#c3a351', '#816326'),
            'green': _quad('#e5e8d4', '#d7dfbf', '#9eae78', '#566834'),
        }
    else:
        win = dict(bg='#273021', hover='#3b4630', text='#dce8c6',
                   meta='#b8c79e', border='#66794a')
        loss = dict(bg='#37291f', hover='#523c2c', text='#f1d0b8',
                    meta='#d1aa8e', border='#8e6548')
        ranks = {
            '1': _quad('#493d26', '#5a4a2c', '#b6985b', '#ecd29d'),
            '2': _quad('#3a3a3a', '#484848', '#989898', '#e2e2e2'),
            '3': _quad('#443426', '#564230', '#ae8055', '#e1bd97'),
            '4': _quad('#303030', '#3c3c3c', '#686868', '#bfbfbf'),
            '5': _quad('#292929', '#353535', '#525252', '#ababab'),
        }
        teams = {
            'red': _quad('#463127', '#543a2c', '#b78363', '#edc3a5'),
            'blue': _quad('#373737', '#454545', '#a5a5a5', '#dedede'),
            'yellow': _quad('#443b25', '#55482b', '#bca05b', '#e8d49e'),
            'green': _quad('#303927', '#3c482f', '#8fa56a', '#d2e0b7'),
        }
    match = {
        **{'win_' + name: value for name, value in win.items()},
        **{'loss_' + name: value for name, value in loss.items()},
        'unknown_bg': c['card'], 'unknown_hover': c['hover'],
        'unknown_text': c['text'], 'unknown_meta': c['muted'],
        'daily_bg': m['raised'], 'recent_bg': m['raised'], 'recent_hover': c['hover'],
        'recent_text': c['text'], 'positive': win['text'], 'negative': loss['text'],
        'neutral': c['text'], 'outline': c['focus'],
    }
    flashback = dict(card=m['surface'], raised=m['raised'], border=m['border'],
                     input=c['fill'], text=c['text'], muted=c['muted'],
                     accent=m['accent'], error=loss['text'], check=m['accent'], check_border=c['focus'])
    return deepcopy(dict(flashback=flashback, match=match, team=teams, rank=ranks))


def theme_style(key):
    """Purpose-designed materials appended after shared geometry/control rules."""
    if key not in SURFACES:
        return ''
    c, m = SURFACES[key], MATERIALS[key]
    sem = semantic_palettes(key)
    p = sem['match']
    return f'''
/* Theme materials: editorial sand, matte graphite, or smoke liquid glass. */
QWidget {{ color: {c['text']}; }}
QDialog, QMessageBox {{ color: {c['text']}; background: {c['page']}; }}
QToolTip {{ color: {c['text']}; background: {m['popup']}; border: 1px solid {c['border']}; padding: 7px 10px; }}
QFrame#sidebar {{ background: {m['sidebar']}; border: 1px solid {m['border']}; border-radius: 20px; }}
QFrame#glass, QFrame#featureCard, QFrame#placeholder,
QFrame#leaderboardCard, QFrame#teamOverviewCard,
QFrame#chatOverviewCard, QFrame#chatCard,
QFrame#taskbarInfoCard, QFrame#taskbarListCard {{
    color: {c['text']}; background: {m['surface']}; border: 1px solid {m['border']}; border-radius: 18px;
}}
QFrame#hero, QFrame#leaderboardHero, QFrame#flashbackHero {{
    background: {m['hero']}; border: 1px solid {m['edge']}; border-left: 1px solid {m['edge']}; border-radius: 20px;
}}
QFrame#metricCard, QFrame#featureRow, QFrame#teamOverviewItem {{
    background: {m['raised']}; border: 1px solid {m['border']}; border-radius: 13px;
}}
QFrame#teamOverviewItem[teamTint="blue"] {{ border-top: 3px solid {sem['team']['blue']['border']}; }}
QFrame#teamOverviewItem[teamTint="gold"] {{ border-top: 3px solid {sem['team']['yellow']['border']}; }}
QFrame#teamOverviewItem[teamTint="violet"] {{ border-top: 3px solid {sem['rank']['3']['border']}; }}
QFrame#teamOverviewItem[teamTint="teal"] {{ border-top: 3px solid {sem['team']['green']['border']}; }}
QFrame#matchDetailPanel {{ background: {m['surface']}; border: 1px solid {m['border']}; border-radius: 18px; }}
QFrame#matchDetailHero, QFrame#matchDetailRibbon, QFrame#dailyStatChip {{ background: {m['raised']}; }}
QFrame#statChip {{ background: {m['inset']}; }}
QFrame#navLine {{ background: {m['border']}; }}
QFrame#row {{ background: transparent; border-bottom: 1px solid {m['border']}; }}
QFrame#heroIconTile, QFrame#moduleIconTile, QFrame#sectionIcon, QFrame#rowGlyph,
QLabel#metricIcon, QLabel#statMetricIcon {{ background: {m['raised']}; border: 1px solid {m['border']}; }}
QLabel#brand, QLabel#title, QLabel#pageTitle, QLabel#heroTitle, QLabel#section,
QLabel#cardTitle, QLabel#metric, QLabel#metricSmall, QLabel#metricSmallCompact,
QLabel#statValue, QLabel#playerName, QLabel#placeholderIcon, QLabel#placeholderText,
QLabel#leaderboardName, QLabel#leaderboardScore, QLabel#updateTitle {{ color: {c['text']}; }}
QLabel#brandHint, QLabel#pageHint, QLabel#muted, QLabel#cardHint, QLabel#metricLabel,
QLabel#statLabel, QLabel#eyebrow, QLabel#leaderboardMeta, QLabel#updateHint,
QLabel#detailStatCaption {{ color: {c['muted']}; }}
QLabel#brandMark {{ background: {m['accent']}; color: {m['accent_text']}; border: none; }}
QFrame#communityChannel {{ background: {m['community']}; border: 1px solid {m['community_border']}; border-radius: 9px; }}
QLabel#communityCaption {{ color: {m['community_text']}; }}
QLabel#communityNumber {{ color: {m['community_number']}; }}

QPushButton {{ color: {c['text']}; background: {m['button']}; border: 1px solid {m['border']}; border-radius: 10px; }}
QPushButton:hover {{ color: {c['text']}; background: {m['hover']}; border-color: {m['edge']}; }}
QPushButton:pressed {{ color: {c['text']}; background: {m['pressed']}; }}
QPushButton:disabled {{ color: {c['muted']}; background: {m['inset']}; border-color: {m['border']}; }}
QPushButton#primary, QPushButton#updateOpen {{ color: {m['accent_text']}; background: {m['accent']}; border-color: {m['accent']}; }}
QPushButton#primary:hover, QPushButton#updateOpen:hover {{ color: {m['accent_text']}; background: {m['primary_hover']}; border-color: {m['primary_hover']}; }}
QPushButton#primary:pressed, QPushButton#updateOpen:pressed {{ background: {m['primary_down']}; }}
QPushButton#primary:disabled, QPushButton#updateOpen:disabled {{ color: {c['muted']}; background: {m['inset']}; border-color: {m['border']}; }}
QPushButton#navButton, QPushButton#settingsButton {{ color: {c['muted']}; background: transparent; border: 1px solid transparent; border-radius: 11px; }}
QPushButton#navButton:hover, QPushButton#settingsButton:hover {{ color: {c['text']}; background: {m['hover']}; border-color: transparent; }}
QPushButton#navButton:checked, QPushButton#settingsButton:checked {{ color: {c['text']}; background: {m['selected']}; border: 1px solid {m['edge']}; font-weight: 600; }}
QPushButton#windowControl {{ background: transparent; color: {c['muted']}; border: none; }}
QPushButton#windowControl:hover {{ background: {m['hover']}; color: {c['text']}; }}
QPushButton#windowControl[controlRole="close"]:hover {{ background: #a75c3e; color: #ffffff; }}
QPushButton#updateDismiss {{ color: {c['muted']}; background: transparent; border: none; }}
QPushButton#updateDismiss:hover {{ color: {c['text']}; background: {m['hover']}; }}
QFrame#updateBanner {{ background: {m['raised']}; border-color: {m['border']}; }}

QLineEdit {{ color: {c['text']}; background: {c['fill']}; border: 1px solid {c['border']}; selection-background-color: {c['selection']}; selection-color: {c['text']}; }}
QLineEdit:focus {{ color: {c['text']}; background: {c['fill']}; border: 1px solid {c['focus']}; }}
QLineEdit#numberEditor, QLineEdit#numberEditor:focus {{ color: {c['text']}; background: {c['fill']}; border: none; }}
QPlainTextEdit, QTextEdit {{ color: {c['text']}; background: {c['fill']}; border-color: {c['border']}; selection-background-color: {c['selection']}; }}
QComboBox QAbstractItemView {{ color: {c['text']}; background: {m['popup']}; border: 1px solid {c['border']}; selection-background-color: {c['selection']}; selection-color: {c['text']}; outline: none; }}
QFrame#comboPopup, QFrame#chatEmojiPopup {{ color: {c['text']}; background: {m['popup']}; border: 1px solid {c['border']}; }}
QCheckBox {{ color: {c['text']}; }}
QCheckBox::indicator:unchecked {{ background: {c['fill']}; border: 1px solid {c['border']}; }}
QCheckBox::indicator:checked {{ background: {c['on']}; border: 1px solid {c['focus']}; }}
QProgressBar {{ color: {c['text']}; background: {c['track']}; border: none; }}
QProgressBar::chunk {{ background: {c['on']}; }}

QFrame#settingsOverlay {{ background: {m['overlay']}; border: none; }}
QFrame#settingsSidebar {{ background: transparent; border: none; }}
QFrame#settingsCard {{ background: {m['surface']}; border: 1px solid {m['border']}; border-radius: 18px; }}
QFrame#preferenceRow, QFrame#settingsCard QFrame#featureRow {{ background: transparent; border: none; border-bottom: 1px solid {m['border']}; border-radius: 0; }}
QPushButton#settingsCategory {{ color: {c['muted']}; background: transparent; border: 1px solid transparent; }}
QPushButton#settingsCategory:hover:!checked {{ color: {c['text']}; background: {m['hover']}; }}
QPushButton#settingsCategory:checked {{ color: {c['fill']}; background: {c['on']}; border: 1px solid {c['on']}; }}
QPushButton#themeOption {{ color: {c['text']}; background: {m['raised']}; border: 1px solid {m['border']}; border-radius: 13px; }}
QPushButton#themeOption:hover {{ color: {c['text']}; background: {m['hover']}; border-color: {m['edge']}; }}
QPushButton#themeOption:checked {{ color: {c['text']}; background: {m['selected']}; border: 1px solid {c['focus']}; }}
QPushButton#settingsReset {{ color: {c['text']}; background: {c['fill']}; border-color: {c['border']}; }}
QPushButton#settingsReset:hover {{ background: {c['hover']}; }}

QLabel#badge, QLabel#statusPill, QLabel#matchWinBadge {{ color: {p['win_text']}; background: {p['win_bg']}; border-color: {p['win_border']}; }}
QLabel#statusDot {{ color: {p['positive']}; }}
QFrame#statusGlyph {{ background: {p['win_bg']}; }}
QLabel#matchLossBadge {{ color: {p['loss_text']}; background: {p['loss_bg']}; }}
QLabel#matchUnknownBadge {{ color: {c['muted']}; background: {m['raised']}; }}
QLabel#mvpBadge {{ color: {sem['rank']['1']['text']}; background: {sem['rank']['1']['bg']}; }}

QFrame#chatOnlineCard {{ background: {m['raised']}; border-color: {m['border']}; }}
QLabel#chatOnlineCount {{ color: {p['positive']}; }}
QLabel#chatOnlineCaption, QLabel#chatServerHint, QLabel#chatStatus, QLabel#chatEmpty,
QLabel#chatMessageTime {{ color: {c['muted']}; }}
QLabel#chatMessageAuthor, QLabel#chatMessageText {{ color: {c['text']}; }}
QFrame#chatMessageBubble {{ background: {m['raised']}; border-color: {m['border']}; }}
QFrame#chatMessageBubble[mine="true"] {{ background: {m['selected']}; border-color: {m['edge']}; }}
QPushButton#chatEmojiButton {{ color: {c['text']}; background: {m['raised']}; border-color: {m['border']}; }}
QPushButton#chatEmojiButton:hover, QPushButton#chatEmojiItem:hover {{ background: {m['hover']}; }}
QPushButton#chatEmojiItem {{ color: {c['text']}; }}
QLabel#chatUnreadDot {{ background: #b97851; border-color: {c['page']}; }}

QTableWidget#taskbarTable {{ color: {c['text']}; background: {m['inset']}; alternate-background-color: {m['raised']}; border-color: {m['border']}; gridline-color: {m['border']}; selection-background-color: {m['selected']}; selection-color: {c['text']}; }}
QTableWidget#taskbarTable::item:selected {{ background: {m['selected']}; color: {c['text']}; }}
QTableWidget#taskbarTable::item:hover {{ background: {m['hover']}; }}
QHeaderView#taskbarHeader::section {{ color: {c['muted']}; background: {m['raised']}; border-bottom-color: {m['border']}; }}
QLabel#taskbarStatus {{ color: {c['text']}; }}
QLabel#taskbarNotice, QLabel#taskbarHotkeyHint {{ color: {c['muted']}; }}
QLabel#taskbarNotice[noticeLevel="error"] {{ color: {p['negative']}; }}
QPushButton#taskbarRefresh {{ color: {m['accent_text']}; background: {m['accent']}; border-color: {m['accent']}; }}
'''


_COLOR = re.compile(r'#[0-9a-fA-F]{8}\b|#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|rgba?\(\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+(?:\s*,\s*[\d.]+)?\s*\)', re.I)
_DECLARATION = re.compile(r'(?P<name>[\w-]+)\s*:(?P<value>[^;{}]+)(?=;|})')


def _parse_color(token):
    alpha = None
    if token.startswith('#'):
        digits = token[1:]
        if len(digits) == 3:
            digits = ''.join(value * 2 for value in digits)
        if len(digits) == 8:  # QColor/QSS uses #AARRGGBB.
            alpha, digits = int(digits[:2], 16), digits[2:]
        rgb = tuple(int(digits[index:index + 2], 16) for index in (0, 2, 4))
    else:
        values = [float(value) for value in re.findall(r'[\d.]+', token)]
        rgb = tuple(round(value) for value in values[:3])
        if len(values) > 3:
            alpha = values[3]
    return rgb, alpha


def _replace_color(token, key, prop):
    rgb, alpha = _parse_color(token)
    hue, saturation, _ = colorsys.rgb_to_hsv(*(value / 255 for value in rgb))
    r, g, b = rgb
    # Includes the old teal accent and imperceptibly blue-tinted neutral grays.
    cool = (155 <= hue * 360 <= 325 and saturation > .025) or (max(rgb) - min(rgb) < 40 and not r >= g >= b)
    if not cool:
        return token
    luminance = round(.2126 * r + .7152 * g + .0722 * b)
    if key == 'blue':
        c = SURFACES[key]
        if prop in ('color', 'selection-color'):
            replacement = c['text'] if luminance >= 175 else c['muted']
        elif 'border' in prop or prop == 'gridline-color':
            replacement = c['border']
        elif prop == 'selection-background-color':
            replacement = c['selection']
        else:
            replacement = c['card'] if luminance < 115 else c['hover']
        rgb = tuple(int(replacement[index:index + 2], 16) for index in (1, 3, 5))
    else:
        rgb = (luminance,) * 3
    if alpha is not None:
        return f'rgba({rgb[0]},{rgb[1]},{rgb[2]},{alpha:g})'
    return '#{:02x}{:02x}{:02x}'.format(*rgb)


def neutralize_legacy_css(css, key):
    """Remove cool legacy accents while preserving selectors and all geometry.

    This safety net only handles color declarations; it does not rename team
    identifiers, rewrite user content/URLs, or run for the original light theme.
    Core materials are explicitly designed in ``theme_style`` above, rather
    than relying on this conversion to manufacture a new theme.
    """
    if key not in SURFACES:
        return css

    def declaration(match):
        prop, value = match.group('name'), match.group('value')
        if prop not in {'color', 'background', 'background-color', 'alternate-background-color',
                        'selection-color', 'selection-background-color', 'gridline-color'} and not prop.startswith('border'):
            return match.group(0)
        value = _COLOR.sub(lambda found: _replace_color(found.group(0), key, prop), value)
        return f'{prop}:{value}'

    return _DECLARATION.sub(declaration, css)
