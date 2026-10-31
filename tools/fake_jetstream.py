"""Local stand-in for Bluesky Jetstream, for testing the collector end to end.

Serves a scripted burst of events on ws://127.0.0.1:PORT/subscribe, records
the query string each client connected with (to check cursor resume), then
closes the socket so the collector reconnects.

    python tools/fake_jetstream.py 8765 /path/to/connections.log
"""
import asyncio
import json
import random
import sys
import time

from websockets.asyncio.server import serve

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
LOG = sys.argv[2] if len(sys.argv) > 2 else "connections.log"
BASE_US = 1_793_360_000_000_000


def events(n: int, offset: int):
    rng = random.Random(offset)
    for i in range(n):
        t = BASE_US + (offset + i) * 50_000
        did = f"did:plc:user{rng.randint(0, 400)}"
        r = rng.random()
        if r < 0.15:  # likes are filtered by wantedCollections in production; send some anyway
            yield {"did": did, "time_us": t, "kind": "commit",
                   "commit": {"rev": "r", "operation": "create", "collection": "app.bsky.feed.like", "rkey": f"l{i}",
                              "record": {"subject": {}}}}
        elif r < 0.2:
            yield {"did": did, "time_us": t, "kind": "commit",
                   "commit": {"rev": "r", "operation": "delete", "collection": "app.bsky.feed.post", "rkey": f"d{i}"}}
        elif r < 0.25:
            yield {"did": did, "time_us": t, "kind": "identity", "identity": {"did": did}}
        else:
            lang = "ja" if rng.random() < 0.1 else "en"
            text = rng.choice(["trying an analog weekend, no phone #AnalogWeekend",
                               "coffee this morning was great", "new music friday is out"])
            facets = []
            if "#" in text:
                facets = [{"index": {"byteStart": 0, "byteEnd": 1},
                           "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": "AnalogWeekend"}]}]
            yield {"did": did, "time_us": t, "kind": "commit",
                   "commit": {"rev": "r", "operation": "create", "collection": "app.bsky.feed.post",
                              "rkey": f"p{offset + i}", "cid": "c",
                              "record": {"$type": "app.bsky.feed.post", "text": text, "langs": [lang],
                                         "facets": facets, "createdAt": "2026-10-30T12:00:00Z"}}}


connections = 0


async def handler(ws):
    global connections
    connections += 1
    with open(LOG, "a") as f:
        f.write(ws.request.path + "\n")
    # first connection: 2,000 events; reconnects: replay the last 200 plus 300 new
    offset = 0 if connections == 1 else 1800
    n = 2000 if connections == 1 else 500
    for ev in events(n, offset):
        await ws.send(json.dumps(ev))
    await asyncio.sleep(3)  # let the collector's 2s flush fire
    await ws.close()


async def main():
    async with serve(handler, "127.0.0.1", PORT):
        await asyncio.sleep(float(sys.argv[3]) if len(sys.argv) > 3 else 30)


if __name__ == "__main__":
    asyncio.run(main())
