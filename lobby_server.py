# -*- coding: utf-8 -*-
"""Small stateless lobby chat service for Render.

The free Render instance keeps a short in-memory message window.  This is
intentional: the client treats the lobby as a live room, and no private
messages or long-term chat history are stored.
"""
from __future__ import annotations

import os
import ipaddress
import threading
import time
import uuid
from collections import deque

from flask import Flask, jsonify, request


app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024
_lock = threading.RLock()
_messages = deque(maxlen=160)
_sessions = {}
_rate_windows = {}
_next_message_id = 1
_SESSION_TTL = 45
_MAX_TEXT = 240
_MAX_SESSIONS = 2048
_RATE_WINDOW = 10
_RATE_LIMIT = 20
_MAX_RATE_WINDOWS = 8192
# Empty by default: only explicitly configured reverse proxies may supply IPs.
app.config['TRUSTED_PROXY_NETWORKS'] = tuple(
    ipaddress.ip_network(value.strip(), strict=False)
    for value in os.environ.get('LOBBY_TRUSTED_PROXY_CIDRS', '').split(',') if value.strip()
)


def _clean(value, limit):
    value = str(value or '').replace('\x00', '').strip()
    return value[:limit]


def _cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response


def _client_key():
    def address(value):
        parsed = ipaddress.ip_address(value.strip())
        return parsed.ipv4_mapped if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped else parsed

    try:
        peer = address(request.remote_addr or '')
    except ValueError:
        return 'unknown'
    trusted = app.config['TRUSTED_PROXY_NETWORKS']
    forwarded = request.headers.get('X-Forwarded-For', '')
    if not trusted or len(forwarded) > 2048:
        return str(peer)
    try:
        hops = forwarded.split(',') if forwarded else []
        if len(hops) > 32:
            return str(peer)
        chain = [address(value) for value in hops]
    except ValueError:
        return str(peer)
    # Stop at the first untrusted hop, walking from the known socket peer.
    for previous in reversed(chain):
        if not any(peer in network for network in trusted):
            break
        peer = previous
    return str(peer)


def _allow_request(key, limit=_RATE_LIMIT):
    now = time.monotonic()
    with _lock:
        if key not in _rate_windows and len(_rate_windows) >= _MAX_RATE_WINDOWS:
            expired_keys = [
                k for k, q in list(_rate_windows.items())
                if not q or now - q[-1] >= _RATE_WINDOW
            ]
            for k in expired_keys:
                _rate_windows.pop(k, None)
        if key not in _rate_windows:
            if len(_rate_windows) >= _MAX_RATE_WINDOWS:
                return False
            _rate_windows[key] = deque()
        window = _rate_windows[key]
        while window and now - window[0] >= _RATE_WINDOW:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        return True


@app.after_request
def _after_request(response):
    return _cors(response)


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'ok': True, 'service': 'DEV King lobby', 'version': 1})


@app.route('/api/join', methods=['POST', 'OPTIONS'])
def join():
    if request.method == 'OPTIONS':
        return ('', 204)
    payload = request.get_json(silent=True) or {}
    if not _allow_request('join:' + _client_key()):
        return jsonify({'ok': False, 'message': '请求过于频繁，请稍后再试'}), 429
    player_id = _clean(payload.get('player_id') or payload.get('playerId'), 32)
    if not player_id:
        return jsonify({'ok': False, 'message': '缺少游戏 ID'}), 400
    session_id = uuid.uuid4().hex
    with _lock:
        _online_count_locked()
        if len(_sessions) >= _MAX_SESSIONS:
            return jsonify({'ok': False, 'message': '大厅暂时已满，请稍后再试'}), 503
        _sessions[session_id] = {'player_id': player_id, 'seen': time.time()}
        online = _online_count_locked()
    return jsonify({'ok': True, 'session_id': session_id, 'online': online})


@app.route('/api/online', methods=['GET', 'POST', 'OPTIONS'])
@app.route('/api/chat/presence', methods=['GET', 'POST', 'OPTIONS'])
def online():
    if request.method == 'OPTIONS':
        return ('', 204)
    payload = request.get_json(silent=True) or {}
    if not _allow_request('online:' + _client_key()):
        return jsonify({'ok': False, 'message': '请求过于频繁，请稍后再试'}), 429
    session_id = _clean(payload.get('session_id') or request.args.get('session_id'), 64)
    with _lock:
        if session_id in _sessions:
            _sessions[session_id]['seen'] = time.time()
        count = _online_count_locked()
    return jsonify({'ok': True, 'online': count, 'online_count': count})


@app.route('/api/messages', methods=['GET', 'POST', 'OPTIONS'])
@app.route('/api/chat/messages', methods=['GET', 'POST', 'OPTIONS'])
def messages():
    if request.method == 'OPTIONS':
        return ('', 204)
    if request.method == 'POST':
        return _post_message()
    if not _allow_request('read:' + _client_key(), limit=45):
        return jsonify({'ok': False, 'message': '请求过于频繁，请稍后再试'}), 429
    session_id = _clean(request.args.get('session_id'), 64)
    with _lock:
        if session_id in _sessions:
            _sessions[session_id]['seen'] = time.time()
        online_count = _online_count_locked()
        items = list(_messages)
    return jsonify({
        'ok': True,
        'online': online_count,
        'online_count': online_count,
        'messages': items,
    })


@app.route('/api/send', methods=['POST', 'OPTIONS'])
def send():
    if request.method == 'OPTIONS':
        return ('', 204)
    return _post_message()


def _post_message():
    global _next_message_id
    payload = request.get_json(silent=True) or {}
    if not _allow_request('send:' + _client_key(), limit=8):
        return jsonify({'ok': False, 'message': '发送过快，请稍后再试'}), 429
    session_id = _clean(payload.get('session_id'), 64)
    player_id = _clean(payload.get('player_id') or payload.get('username'), 32)
    text = _clean(payload.get('message') or payload.get('text'), _MAX_TEXT)
    if not player_id:
        return jsonify({'ok': False, 'message': '请先设置游戏 ID'}), 400
    if not text:
        return jsonify({'ok': False, 'message': '消息不能为空'}), 400
    now = time.strftime('%H:%M:%S')
    with _lock:
        if session_id in _sessions:
            _sessions[session_id]['seen'] = time.time()
            _sessions[session_id]['player_id'] = player_id
        elif len(_sessions) < _MAX_SESSIONS:
            _sessions[session_id or uuid.uuid4().hex] = {'player_id': player_id, 'seen': time.time()}
        item = {
            'id': _next_message_id,
            'player_id': player_id,
            'message': text,
            'created_at': now,
        }
        _next_message_id += 1
        _messages.append(item)
        online_count = _online_count_locked()
        items = list(_messages)
    return jsonify({
        'ok': True,
        'online': online_count,
        'online_count': online_count,
        'message': item,
        'messages': items,
    })


def _online_count_locked():
    cutoff = time.time() - _SESSION_TTL
    expired = [key for key, item in _sessions.items() if item.get('seen', 0) < cutoff]
    for key in expired:
        _sessions.pop(key, None)
    players = {
        item.get('player_id')
        for item in _sessions.values()
        if item.get('player_id')
    }
    return len(players)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    app.run(host='0.0.0.0', port=port)
