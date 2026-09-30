"""
Fake rep broadcaster for teammates (no camera needed)
=====================================================
Same websocket server as main.py (ws://localhost:8765), sends one valid
rep_counted event every 3 seconds. Reps increment; depth_ok is true for most
reps and false every 4th rep, so form_score moves around realistically.

INSTALL:
    py -m pip install websockets

RUN:
    py replay.py          # Ctrl+C to stop
    (do NOT run at the same time as main.py - same port)

TEST THE WEBSOCKET:
    npx wscat -c ws://localhost:8765     # or point the dashboard at it

EVENT FORMAT (identical to main.py):
    {"event": "rep_counted", "reps": 7, "depth_ok": true, "form_score": 85.7,
     "exercise": "squat", "state": "standing"}
"""

import asyncio
import json

import websockets

HOST = "localhost"
PORT = 8765
EXERCISE = "bicep_curl"
INTERVAL_SECONDS = 3
BAD_REP_EVERY = 4        # every 4th rep is shallow (depth_ok = false)

clients = set()


async def handler(ws):
    clients.add(ws)
    try:
        async for _ in ws:      # ignore incoming messages
            pass
    except Exception:
        pass
    finally:
        clients.discard(ws)


async def send_all(message):
    current = list(clients)
    if not current:
        return
    results = await asyncio.gather(*(c.send(message) for c in current), return_exceptions=True)
    for c, r in zip(current, results):
        if isinstance(r, Exception):
            clients.discard(c)


async def main():
    async with websockets.serve(handler, HOST, PORT):
        print(f"websocket on {HOST}:{PORT}  (replay mode, one event every {INTERVAL_SECONDS}s)")
        reps = 0
        good = 0
        while True:
            await asyncio.sleep(INTERVAL_SECONDS)
            reps += 1
            depth_ok = (reps % BAD_REP_EVERY) != 0
            if depth_ok:
                good += 1
            event = {
                "event": "rep_counted",
                "reps": reps,
                "depth_ok": depth_ok,
                "form_score": round(100.0 * good / reps, 1),
                "exercise": EXERCISE,
                "state": "standing",
            }
            message = json.dumps(event)
            await send_all(message)
            print(f"[{len(clients)} client(s)] {message}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("bye")
    except OSError as e:
        print(f"Could not start on {HOST}:{PORT} ({e}). Is main.py already running?")
