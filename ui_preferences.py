"""Typed, bounded UI preferences shared by settings controls and live widgets."""

DEFAULTS = {
    'match/recentLimit': 5,
    'match/autoLoad': True,
    'match/cardGap': 5,
    'flashback/logLines': 120,
    'flashback/logFontSize': 11,
    'flashback/followLog': True,
    'flashback/statusInterval': 1500,
    'flashback/replayLimit': 24,
    'ui/parallaxScrolling': True,
}

LIMITS = {
    'match/recentLimit': (1, 9),
    'match/cardGap': (0, 20),
    'flashback/logLines': (50, 2000),
    'flashback/logFontSize': (10, 18),
    'flashback/replayLimit': (5, 100),
}

CHOICES = {'flashback/statusInterval': (1000, 1500, 3000, 5000)}


def normalize(key, value):
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, str):
            value = value.strip().lower()
            if value in {'true', '1', 'yes', 'on'}:
                return True
            if value in {'false', '0', 'no', 'off'}:
                return False
            return default
        return value if isinstance(value, bool) else default
    try:
        value = int(value)
    except (ValueError, TypeError, OverflowError):
        return default
    if key in CHOICES:
        return value if value in CHOICES[key] else default
    low, high = LIMITS[key]
    return max(low, min(high, value))


def read_preferences(settings):
    return {key: normalize(key, settings.value('preferences/' + key, default))
            for key, default in DEFAULTS.items()}


def save_preference(settings, key, value):
    value = normalize(key, value)
    settings.setValue('preferences/' + key, value)
    return value
