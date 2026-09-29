# Temporary Chat Relay

An in-memory WebSocket relay for small game chat rooms.

- `GET /health` checks that the service is running.
- WebSocket endpoint: `/ws?room=ROOM_CODE&name=PLAYER_NAME`.
- Send JSON: `{ "text": "hello" }`.
- The server keeps only active WebSocket connections in RAM. It does not write messages to a database or file and does not print message content.
- Restarting or sleeping the service clears all rooms and messages.

This is a demo relay. Add authentication and rate limits before exposing it to a larger audience.
