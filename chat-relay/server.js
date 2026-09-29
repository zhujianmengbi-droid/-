const http = require('http');
const { WebSocketServer, WebSocket } = require('ws');

const MAX_TEXT_BYTES = 2000;
const MAX_ROOM_BYTES = 100;
const rooms = new Map(); // room id -> Set<WebSocket>; never persisted

function clean(value, maxBytes) {
  if (typeof value !== 'string') return '';
  const trimmed = value.trim();
  return Buffer.byteLength(trimmed, 'utf8') <= maxBytes ? trimmed : '';
}

function send(socket, payload) {
  if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));
}

function leave(socket) {
  const room = socket.room;
  if (!room) return;
  room.delete(socket);
  if (room.size === 0) rooms.delete(socket.roomId);
  socket.room = null;
}

const server = http.createServer((req, res) => {
  if (req.url === '/health') {
    res.writeHead(200, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
    res.end(JSON.stringify({ ok: true, rooms: rooms.size }));
    return;
  }
  res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
  res.end('Not found');
});

const wss = new WebSocketServer({ server, path: '/ws', maxPayload: MAX_TEXT_BYTES + 512 });

wss.on('connection', (socket, req) => {
  const url = new URL(req.url, 'http://localhost');
  const roomId = clean(url.searchParams.get('room'), MAX_ROOM_BYTES);
  const name = clean(url.searchParams.get('name'), 64) || '玩家';

  if (!roomId) {
    send(socket, { type: 'error', code: 'room_required' });
    socket.close(1008, 'room required');
    return;
  }

  let room = rooms.get(roomId);
  if (!room) {
    room = new Set();
    rooms.set(roomId, room);
  }
  socket.roomId = roomId;
  socket.room = room;
  socket.name = name;
  room.add(socket);
  send(socket, { type: 'ready', room: roomId });

  socket.on('message', (raw) => {
    if (Buffer.byteLength(raw) > MAX_TEXT_BYTES + 512) return;
    let packet;
    try { packet = JSON.parse(raw.toString()); } catch { return; }
    const text = clean(packet && packet.text, MAX_TEXT_BYTES);
    if (!text) return;

    // Broadcast only to currently connected players. No database, file, or message logging.
    for (const peer of room) {
      send(peer, { type: 'message', from: socket.name, text, at: Date.now() });
    }
  });

  socket.on('close', () => leave(socket));
  socket.on('error', () => leave(socket));
});

setInterval(() => {
  for (const socket of wss.clients) {
    if (socket.isAlive === false) {
      socket.terminate();
      continue;
    }
    socket.isAlive = false;
    socket.ping();
  }
}, 30000).unref();

wss.on('connection', socket => socket.on('pong', () => { socket.isAlive = true; }));

const port = Number(process.env.PORT || 10000);
server.listen(port, '0.0.0.0', () => {
  // Deliberately do not log rooms or chat content.
  console.log(`temporary-chat-relay listening on ${port}`);
});
