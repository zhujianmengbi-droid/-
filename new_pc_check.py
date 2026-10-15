"""Isolated startup diagnostic for the exact packaged ZJ HUB executable."""
import ctypes
import json
import os
from pathlib import Path
import ssl
import struct
import tempfile

from PySide6.QtCore import QEvent, QSettings, qVersion
from PySide6.QtNetwork import QSslSocket
from PySide6.QtWidgets import QDialog


def run_check(app, ui, destination):
    root = Path(tempfile.mkdtemp(prefix='zj-hub-new-pc-'))
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(root / 'settings'))
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.SystemScope, str(root / 'system-settings'))
    # The explicit (organization, application) overload uses NativeFormat even
    # after setDefaultFormat. Force the format for every UI settings instance.
    original_settings = ui.QSettings
    class IsolatedSettings(QSettings):
        def __init__(self, organization, application):
            super().__init__(QSettings.Format.IniFormat, QSettings.Scope.UserScope, organization, application)
    ui.QSettings = IsolatedSettings
    app.setQuitOnLastWindowClosed(False)
    checks = {}
    settings = IsolatedSettings('DEV King', '逐渐工具箱')
    checks['freshSettings'] = not settings.allKeys()
    checks['settingsUseIsolatedIni'] = settings.format() == QSettings.Format.IniFormat
    checks['pythonTls'] = bool(ssl.OPENSSL_VERSION)
    checks['qtTls'] = QSslSocket.supportsSsl()
    checks['x64'] = struct.calcsize('P') == 8
    # The diagnostic builds the real widgets but never registers global
    # hotkeys, opens a network session, starts the recorder, or shows windows.
    hotkey_start = ui.TaskbarHotkeyWorker.start
    updater = ui.LargeApp._start_update_check
    ui.TaskbarHotkeyWorker.start = lambda self: None
    ui.LargeApp._start_update_check = lambda self: None
    window = None
    cache = None
    try:
        dialog = ui.PlayerLoginDialog('', '', 'light')
        dialog.skip_button.click()
        checks['offlineSkipLogin'] = dialog.result() == QDialog.DialogCode.Accepted and dialog.skipped_login
        checks['skipDoesNotVerifyOverNetwork'] = dialog._worker is None
        dialog.deleteLater()
        window = ui.LargeApp()
        app.processEvents()
        checks['mainWindowBuilt'] = window.windowTitle() == 'ZJ HUB'
        checks['recorderBundled'] = bool(window._find_flashback_bridge())
        checks['mainWindowRemainsHidden'] = not window.isVisible()
        # Exercise every retained theme in the real frozen executable without
        # showing a UI or modifying the user's normal settings.
        material_checks = {}
        for theme in ('dark', 'blue', 'light'):
            window.apply_theme(theme)
            window.backdrop._rebuild_cache()
            material_checks[theme] = (not window.backdrop._background_cache.isNull()
                                      and not window.backdrop._glow_cache.isNull())
        checks['themeMaterialsReady'] = all(material_checks.values())
        screen = window.screen()
        area = screen.availableGeometry() if screen else None
        checks['fitsAvailableScreen'] = area is None or (window.width() <= area.width() and window.height() <= area.height())
        cache = ui._program_skin_cache_directory()
        checks['persistentSkinCache'] = '_MEI' not in str(cache)
        with tempfile.TemporaryFile(dir=cache) as stream:
            stream.write(b'test'); stream.flush()
        checks['skinCacheWritable'] = True
        from companion_pack import PACK_SHA256, bundled_pack_path
        import hashlib
        checks['companionPackIntact'] = hashlib.sha256(bundled_pack_path().read_bytes()).hexdigest() == PACK_SHA256
        details = {'qtVersion': qVersion(), 'qtTlsBackends': QSslSocket.availableBackends(),
                   'themeMaterials': material_checks,
                   'qtTlsActiveBackend': QSslSocket.activeBackend(),
                   'screen': None if area is None else [area.width(), area.height()],
                   'window': [window.width(), window.height()], 'skinCache': str(cache),
                   'isolatedSettings': str(root / 'settings'),
                   'scope': 'isolated INI, offscreen widgets and bundled resources; not a clean Windows VM'}
        # Missing-WMI/JDK cases are checked separately by the engine tests.
        if os.name == 'nt':
            details['isAdministrator'] = bool(ctypes.windll.shell32.IsUserAnAdmin())
        result = {'ok': all(checks.values()), 'checks': checks, 'details': details}
    except Exception as error:
        result = {'ok': False, 'checks': checks, 'error': str(error)}
    finally:
        if window is not None:
            window.close()
            window.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        ui.TaskbarHotkeyWorker.start = hotkey_start
        ui.LargeApp._start_update_check = updater
        settings.sync()
        ui.QSettings = original_settings
    result['checks']['temporarySkinCacheRemovedAfterClose'] = cache is None or not cache.exists()
    result['ok'] = result.get('ok', False) and result['checks']['temporarySkinCacheRemovedAfterClose']
    target = Path(destination).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if result['ok'] else 1
