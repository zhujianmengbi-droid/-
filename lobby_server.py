# -*- coding: utf-8 -*-
"""Small stateless lobby chat service for Render.

The free Render instance keeps a short in-memory message window.  This is
intentional: the client treats the lobby as a live room, and no private
messages or long-term chat history are stored.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from collections import deque

from flask import Flask, jsonify, request


app = Flask(__name__)
_lock = threading.RLock()
_messages = deque(maxlen=160)
_sessions = {}
_next_message_id = 1
_SESSION_TTL = 45
_MAX_TEXT = 240


def _clean(value, limit):
    value = str(value or '').replace('\x00', '').strip()
    return value[:limit]


def _cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response


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
    player_id = _clean(payload.get('player_id') or payload.get('playerId'), 32)
    if not player_id:
        return jsonify({'ok': False, 'message': '缺少游戏 ID'}), 400
    session_id = uuid.uuid4().hex
    with _lock:
        _sessions[session_id] = {'player_id': player_id, 'seen': time.time()}
        online = _online_count_locked()
    return jsonify({'ok': True, 'session_id': session_id, 'online': online})


@app.route('/api/online', methods=['GET', 'POST', 'OPTIONS'])
@app.route('/api/chat/presence', methods=['GET', 'POST', 'OPTIONS'])
def online():
    if request.method == 'OPTIONS':
        return ('', 204)
    payload = request.get_json(silent=True) or {}
    session_id = _clean(payload.get('session_id') or request.args.get('session_id'), 64)
    with _lock:
        if session_id in _sessions:
            _sessions[session_id]['seen'] = time.time()
        count = _online_count_locked()
    return jsonify({'ok': True, 'online': count})


@app.route('/api/messages', methods=['GET', 'POST', 'OPTIONS'])
@app.route('/api/chat/messages', methods=['GET', 'POST', 'OPTIONS'])
def messages():
    if request.method == 'OPTIONS':
        return ('', 204)
    if request.method == 'POST':
        payload = request.get_json(silent=True) or {}
        session_id = _clean(payload.get('session_id'), 64)
        player_id = _clean(payload.get('player_id') or payload.get('playerId'), 32)
        text = _clean(payload.get('text') or payload.get('message'), _MAX_TEXT)
        with _lock:
            session = _sessions.get(session_id)
            # Keep the original lightweight client contract usable while the
            # newer client uses an explicit join session.
            if session is None and player_id:
                session_id = uuid.uuid4().hex
                session = {'player_id': player_id, 'seen': time.time()}
                _sessions[session_id] = session
            if session is None:
                return jsonify({'ok': False, 'message': '聊天会话已过期，请重新进入大厅'}), 401
            session['seen'] = time.time()
            if not text:
                return jsonify({'ok': False, 'message': '消息不能为空'}), 400
            global _next_message_id
            item = {
                'id': _next_message_id,
                'player_id': session['player_id'],
                'text': text,
                'message': text,
                'created_at': int(time.time()),
            }
            _next_message_id += 1
            _messages.append(item)
        return jsonify({'ok': True, 'message': item})

    try:
        after = int(request.args.get('after', '0'))
    except ValueError:
        after = 0
    session_id = _clean(request.args.get('session_id'), 64)
    player_id = _clean(request.args.get('player_id') or request.args.get('playerId'), 32)
    with _lock:
        session = _sessions.get(session_id)
        if session is None and player_id:
            session_id = uuid.uuid4().hex
            session = {'player_id': player_id, 'seen': time.time()}
            _sessions[session_id] = session
        if session is not None:
            session['seen'] = time.time()
        items = [item for item in _messages if item['id'] > after]
        online_count = _online_count_locked()
    return jsonify({'ok': True, 'online': online_count, 'messages': items[-80:]})


def _online_count_locked():
    cutoff = time.time() - _SESSION_TTL
    expired = [key for key, value in _sessions.items() if value['seen'] < cutoff]
    for key in expired:
        _sessions.pop(key, None)
    return len(_sessions)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '10000'))
    app.run(host='0.0.0.0', port=port)
