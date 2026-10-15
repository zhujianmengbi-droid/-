"""Own one disposable skin cache per ZJ HUB process; never own player assets."""
import atexit
import os
from pathlib import Path
import shutil
import tempfile
import threading
import uuid
from itertools import islice
import psutil


class SessionSkinCache:
    @staticmethod
    def _cleanup_stale(base):
        for folder in islice(base.glob('session-*'), 64):
            try:
                if folder.is_symlink() or folder.is_junction() or folder.resolve().parent != base:
                    continue
                marker = folder / '.zj-cache-owner'
                if marker.is_symlink():
                    continue
                data = marker.read_text(encoding='ascii').splitlines()
                if len(data) != 3 or data[0] != 'ZJHUB-SKIN-CACHE' or not data[1].isdigit():
                    continue
                pid = int(data[1])
                if pid <= 0 or not folder.name.startswith(f'session-{pid}-') or psutil.pid_exists(pid):
                    continue
                if len(data[2]) == 32 and all(character in '0123456789abcdef' for character in data[2]):
                    shutil.rmtree(folder)
            except (OSError, UnicodeError):
                continue

    def __init__(self, roots=None):
        local = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
        self.roots = tuple(roots) if roots is not None else (
            local / 'ZJ HUB' / 'cache' / 'skins', Path(tempfile.gettempdir()) / 'ZJ-HUB' / 'skins')
        self.token = uuid.uuid4().hex
        self.directory = None
        self.base = None
        self.lock = threading.RLock()

    def path(self):
        with self.lock:
            if self.directory is not None and self.directory.is_dir():
                return self.directory
            failure = None
            for root in self.roots:
                try:
                    base = Path(root).resolve()
                    base.mkdir(parents=True, exist_ok=True)
                    self._cleanup_stale(base)
                    directory = Path(tempfile.mkdtemp(prefix=f'session-{os.getpid()}-', dir=base))
                    (directory / '.zj-cache-owner').write_text(f'ZJHUB-SKIN-CACHE\n{os.getpid()}\n{self.token}', encoding='ascii')
                    self.base, self.directory = base, directory
                    return directory
                except OSError as error:
                    failure = error
            raise OSError('无法创建 ZJ HUB 临时皮肤缓存，请检查当前用户临时目录权限。') from failure

    def cleanup(self):
        with self.lock:
            root, base = self.directory, self.base
            if root is None or not root.exists():
                return
            marker = root / '.zj-cache-owner'
            if (base is None or root.is_symlink() or root.is_junction()
                    or root.resolve().parent != base.resolve() or not root.name.startswith(f'session-{os.getpid()}-')
                    or marker.is_symlink() or marker.read_text(encoding='ascii') != f'ZJHUB-SKIN-CACHE\n{os.getpid()}\n{self.token}'):
                raise OSError('临时缓存归属校验失败，已停止清理。')
            shutil.rmtree(root)
            self.directory = None


_session = SessionSkinCache()


def skin_cache_directory():
    return _session.path()


def cleanup_skin_cache():
    _session.cleanup()


def _exit_cleanup():
    try:
        cleanup_skin_cache()
    except OSError:
        pass


atexit.register(_exit_cleanup)
