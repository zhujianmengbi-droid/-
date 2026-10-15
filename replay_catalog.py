"""Bounded replay discovery with metadata captured once, away from Qt's thread."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import stat
import threading
import zipfile


@dataclass(frozen=True)
class ReplayEntry:
    path: Path
    mtime_ns: int
    size: int
    identity: str


def directory_key(directories):
    # Lexical normalization only: resolving links can block on network shares.
    return tuple(os.path.normcase(os.path.abspath(directory)) for directory in directories)


def scan_replays(directories, cancelled=None):
    """Read direct exports and the recorder's two bounded session layouts.

    Resolve each explicitly selected root once. Never follow links/reparse
    points below it, including session directories and ZIPs.
    """
    cancelled = cancelled or threading.Event()
    files = []
    seen = set()
    seen_roots = set()

    def entries(folder):
        try:
            with os.scandir(folder) as stream:
                for entry in stream:
                    if cancelled.is_set():
                        return
                    try:
                        details = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if (stat.S_ISLNK(details.st_mode) or
                            getattr(details, 'st_file_attributes', 0) &
                            getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
                        continue
                    yield entry, details
        except OSError:
            return

    def collect(folder, resolved_folder):
        subfolders = []
        for entry, details in entries(folder):
            if stat.S_ISREG(details.st_mode) and entry.name.casefold().endswith('.flashback.zip'):
                identity = os.path.normcase(str(resolved_folder / entry.name))
                if identity not in seen:
                    seen.add(identity)
                    files.append(ReplayEntry(Path(entry.path), details.st_mtime_ns,
                                             details.st_size, identity))
            elif stat.S_ISDIR(details.st_mode):
                subfolders.append((Path(entry.path), resolved_folder / entry.name))
        return subfolders

    for directory in directories:
        if cancelled.is_set():
            break
        directory = Path(directory)
        try:
            resolved = directory.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        root_identity = os.path.normcase(str(resolved))
        if root_identity in seen_roots:
            continue
        seen_roots.add(root_identity)
        children = collect(directory, resolved)
        runtime = next((item for item in children if item[0].name.casefold() == '.flashback-runtime'), None)
        if runtime is None:
            continue
        for entry, details in entries(runtime[0]):
            if not stat.S_ISDIR(details.st_mode) or not entry.name.casefold().startswith('session-'):
                continue
            session = Path(entry.path)
            session_resolved = runtime[1] / entry.name
            for child, child_resolved in collect(session, session_resolved):
                collect(child, child_resolved)
    files.sort(key=lambda item: item.mtime_ns, reverse=True)
    return tuple(files[:100])


def read_replay_info(path):
    """Return the existing replay summary and error wording without Qt access."""
    path = Path(path)
    base_lines = [f'回放：{path.name}', f'位置：{path.parent}']
    try:
        stat_result = path.stat()
        if stat_result.st_size <= 0:
            return '\n'.join(base_lines + ['', '文件大小为 0，回放可能仍在写入。请停止录制后刷新。'])
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            metadata = {}
            metadata_error = ''
            metadata_name = next(
                (name for name in names if name.replace('\\', '/').casefold() == 'metadata.json'), None)
            if metadata_name:
                try:
                    info = archive.getinfo(metadata_name)
                    if info.file_size > 2 * 1024 * 1024:
                        raise ValueError('metadata.json 超过 2 MB')
                    raw = archive.read(info)
                    metadata = json.loads(raw.decode('utf-8-sig'))
                    if not isinstance(metadata, dict):
                        raise ValueError('metadata.json 不是对象')
                except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
                    metadata = {}
                    metadata_error = str(error)
        summary_lines = [*base_lines,
                         f'大小：{stat_result.st_size / (1024 * 1024):.1f} MB',
                         f'文件：{len(names)} 项']
        if metadata_error:
            summary_lines.append(f'元数据读取失败：{metadata_error}（压缩包目录仍可读取）')
        labels = {
            'name': '名称', 'version_string': '版本', 'total_ticks': '总 tick',
            'data_version': '数据版本', 'protocol_version': '协议版本', 'world_name': '世界',
        }
        for key, title in labels.items():
            if key in metadata:
                summary_lines.append(f'{title}：{metadata[key]}')
        recorder = metadata.get('flashback_recorder')
        if isinstance(recorder, dict) and recorder.get('source_file'):
            summary_lines.append(f'录制源：{recorder["source_file"]}')
        summary_lines.extend(('', '压缩包内容：'))
        summary_lines.extend(f'· {name}' for name in names[:40])
        if len(names) > 40:
            summary_lines.append(f'· …还有 {len(names) - 40} 项')
        return '\n'.join(summary_lines)
    except zipfile.BadZipFile:
        return '\n'.join(base_lines + ['', '回放压缩包尚未写完或已损坏。请停止录制，等待导出完成后点击刷新。'])
    except FileNotFoundError:
        return '\n'.join(base_lines + ['', '回放文件已被移动或删除，请点击刷新重新扫描。'])
    except PermissionError:
        return '\n'.join(base_lines + ['', '当前没有权限读取此回放文件，请关闭占用它的程序后重试。'])
    except Exception as error:
        return f'无法读取回放信息：{error}'


def read_replay_details(paths, cancelled):
    return () if cancelled.is_set() else (read_replay_info(paths[0]),)


@dataclass(frozen=True)
class CatalogResult:
    generation: int
    directories: tuple
    entries: tuple
    error: str = ''


def _scan_to_queue(scanner, generation, directories, cancelled, results):
    # No Qt objects or callbacks are captured by the daemon worker. A blocked
    # filesystem call cannot retain a window or hold up application shutdown.
    try:
        result = CatalogResult(generation, directories, scanner(directories, cancelled))
    except Exception as error:
        result = CatalogResult(generation, directories, (), str(error))
    if not cancelled.is_set():
        results.put(result)


class ReplayCatalog:
    """GUI-thread request/poll facade; at most one scan and one newer request."""
    def __init__(self, scanner=scan_replays):
        self._scanner = scanner
        self._results = queue.SimpleQueue()
        self._cancelled = threading.Event()
        self._generation = 0
        self._desired = None
        self._running = None

    @property
    def busy(self):
        return self._running is not None

    def request(self, directories, force=False):
        if self._cancelled.is_set():
            return
        directories = tuple(Path(directory) for directory in directories)
        key = directory_key(directories)
        if self._desired is not None and key == self._desired[2]:
            if not force or (self._running is not None and self._desired[0] != self._running):
                return
        self._generation += 1
        self._desired = (self._generation, directories, key)
        if self._running is None:
            self._start()

    def _start(self):
        generation, directories, _key = self._desired
        self._running = generation
        threading.Thread(target=_scan_to_queue,
                         args=(self._scanner, generation, directories,
                               self._cancelled, self._results),
                         name='ZJ-HUB-replay-catalog', daemon=True).start()

    def poll(self):
        if self._cancelled.is_set():
            return None
        try:
            result = self._results.get_nowait()
        except queue.Empty:
            return None
        self._running = None
        if self._desired is None or result.generation != self._desired[0]:
            if self._desired is not None:
                self._start()
            return None
        self._desired = None
        return result

    def close(self):
        self._cancelled.set()
        self._desired = None
        self._running = None
