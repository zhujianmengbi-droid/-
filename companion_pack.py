"""Save the bundled companion modpack without blocking on a network service."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile


PACK_FILENAME = 'ZJ-HUB-modpack.zip'
PACK_SHA256 = 'b2b5054e6656da423abd8a67ef66875f821e4534d66c48239d84764f8a12dd25'


def bundled_pack_path():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    return root / 'companions' / PACK_FILENAME


def save_companion_pack(destination):
    """Verify a complete temporary copy, then atomically replace the chosen file."""
    source = bundled_pack_path()
    destination = Path(destination).absolute()
    temporary = None
    try:
        with source.open('rb') as original:
            with tempfile.NamedTemporaryFile(
                    mode='wb', prefix='.zj-modpack-', suffix='.tmp',
                    dir=destination.parent, delete=False) as output:
                temporary = Path(output.name)
                digest = hashlib.sha256()
                while chunk := original.read(256 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
        if digest.hexdigest() != PACK_SHA256:
            raise ValueError('配套整合包校验失败，请重新下载 ZJ HUB。')
        os.replace(temporary, destination)
        temporary = None
        return str(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
