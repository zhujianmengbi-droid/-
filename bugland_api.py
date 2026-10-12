# -*- coding: utf-8 -*-
"""BuGLand v2 match-history client with per-user DPAPI token storage."""
import ctypes
import json
import os
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_BASE_URL = 'https://api.mcbjd.net/v2'
MATCH_HISTORY_ENDPOINT = '/gamelog/user'
MATCH_DETAIL_ENDPOINT = '/gamelog/match'
LEADERBOARD_ENDPOINT = '/leaderboard'
PLAYER_INFO_ENDPOINT = '/player'
DEFAULT_API_TOKEN = 'eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJodHRwczovL2J1ZGxhbmQubWNkZXYudG9wLyIsInN1YiI6IjMyNjZjYTM2LWZiOWUtNGJkYS04MjBmLTJmNTdmNDkwNTZkOSIsImdyb3VwcyI6WyJwbGF5ZXIiXSwibGltaXQiOjMwLCJpYXQiOjE3OTA2NDQyMjMsImV4cCI6MTc5MzIzNjIyMywianRpIjoiYTNlN2E0M2QtMDE2Zi00MzFhLTg0NDMtMzIyNDc2NTMzZjEwIn0.cxRQ9GZzqW8Rh2JnyLW1E4nwt9ZI4BJn1k0YB2xFRbU'


class BuglandApiError(Exception):
    """A user-facing API/network error with no authorization data attached."""


def _token_path():
    appdata = os.environ.get('APPDATA')
    if appdata:
        root = Path(appdata)
    else:
        root = Path.home() / 'AppData' / 'Roaming'
    return root / 'DEV King' / '逐渐工具箱' / 'bugland-token.bin'


def _token_disabled_path():
    return _token_path().with_name('bugland-token-disabled')


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ('cbData', ctypes.c_uint32),
        ('pbData', ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _dpapi(data, protect):
    if os.name != 'nt':
        raise BuglandApiError('Token 加密存储仅支持 Windows。')

    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    blob_pointer = ctypes.POINTER(_DataBlob)
    if protect:
        transform = crypt32.CryptProtectData
        transform.argtypes = [
            blob_pointer, ctypes.c_wchar_p, blob_pointer, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_uint32, blob_pointer,
        ]
    else:
        transform = crypt32.CryptUnprotectData
        transform.argtypes = [
            blob_pointer, ctypes.POINTER(ctypes.c_wchar_p), blob_pointer,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, blob_pointer,
        ]
    transform.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(
        len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = _DataBlob()
    if not transform(ctypes.byref(source), None, None, None, None, 1,
                     ctypes.byref(destination)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(destination.pbData)


def load_token():
    path = _token_path()
    if _token_disabled_path().exists():
        return ''
    if not path.exists():
        try:
            save_token(DEFAULT_API_TOKEN)
        except (OSError, BuglandApiError, ctypes.ArgumentError):
            # Keep the bundled default usable if local DPAPI storage is unavailable.
            return DEFAULT_API_TOKEN
        return DEFAULT_API_TOKEN
    try:
        encrypted = path.read_bytes()
        return _dpapi(encrypted, protect=False).decode('utf-8').strip()
    except (OSError, UnicodeError, BuglandApiError, ctypes.ArgumentError):
        return ''


def token_is_saved():
    return _token_path().is_file()


def save_token(token):
    token = token.strip()
    if not token:
        raise BuglandApiError('请先粘贴 API Token。')
    if len(token) > 4096 or any(character.isspace() for character in token):
        raise BuglandApiError('Token 格式无效，请检查后重试。')
    encrypted = _dpapi(token.encode('utf-8'), protect=True)
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temporary_path = Path(file.name)
            file.write(encrypted)
        os.replace(temporary_path, path)
        _token_disabled_path().unlink(missing_ok=True)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def clear_token():
    path = _token_path()
    path.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    _token_disabled_path().write_bytes(b'1')


def post_json(endpoint, payload, token):
    if endpoint not in (
            MATCH_HISTORY_ENDPOINT, MATCH_DETAIL_ENDPOINT,
            LEADERBOARD_ENDPOINT, PLAYER_INFO_ENDPOINT):
        raise BuglandApiError('不支持的 API 请求。')
    token = token.strip()
    if not token:
        raise BuglandApiError('请先在设置中保存 API Token。')
    request = Request(
        API_BASE_URL + endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': 'DEVKing-Toolbox/1.0',
        },
        method='POST',
    )
    try:
        with urlopen(request, timeout=18) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
    except HTTPError as error:
        body = error.read(16 * 1024)
        message = _error_message(body)
        if error.code in (401, 403):
            raise BuglandApiError('Token 无效、已过期或没有查询权限。') from None
        if error.code == 429:
            raise BuglandApiError('API 请求频率已达上限，请稍后再试。') from None
        if error.code == 404:
            raise BuglandApiError('没有找到对应数据，请检查玩家昵称或对局。') from None
        detail = f'：{message}' if message else ''
        raise BuglandApiError(f'API 返回 HTTP {error.code}{detail}') from None
    except URLError as error:
        reason = str(getattr(error, 'reason', ''))
        if 'timed out' in reason.lower() or 'timeout' in reason.lower():
            raise BuglandApiError('API 请求超时，请稍后重试。') from None
        raise BuglandApiError('无法连接布吉岛 API，请检查网络后重试。') from None
    except TimeoutError:
        raise BuglandApiError('API 请求超时，请稍后重试。') from None

    if len(raw) > 4 * 1024 * 1024:
        raise BuglandApiError('API 响应过大，已停止读取。')
    try:
        result = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeError, json.JSONDecodeError):
        raise BuglandApiError('API 返回了无法识别的数据。') from None
    if isinstance(result, dict):
        message = result.get('detail') or result.get('message') or result.get('error')
        if isinstance(message, str) and result.get('success') is False:
            raise BuglandApiError(message[:240])
    return result


def _error_message(body):
    try:
        value = json.loads(body.decode('utf-8-sig'))
    except (UnicodeError, json.JSONDecodeError):
        return ''
    if isinstance(value, dict):
        for key in ('detail', 'message', 'error'):
            message = value.get(key)
            if isinstance(message, str):
                return message[:240]
    return ''

