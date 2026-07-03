"""Background Home Assistant WebSocket client.

Runs on its own thread with its own asyncio event loop, keeps a local
mirror of the configured entities' states, and exposes thread-safe
`get_state()`/`call_service()` for the render/input loop in kiosk.py.
Reconnects automatically (with backoff) if the connection drops.
"""

import asyncio
import json
import logging
import threading

import websockets

log = logging.getLogger("ha_client")


class HAClient:
    def __init__(self, url, token, entity_ids, todo_entity_ids=None, on_update=None):
        self._ws_url = _to_ws_url(url)
        self._token = token
        self._entity_ids = set(entity_ids)
        self._todo_entity_ids = set(todo_entity_ids or [])
        self._on_update = on_update  # callback(entity_id), invoked from the asyncio thread

        self.lock = threading.Lock()
        self.states = {}  # entity_id -> HA state dict
        self.todo_items = {}  # entity_id -> list of {"uid", "summary", "status"} dicts
        self.connected = threading.Event()

        self._loop = None
        self._ws = None
        self._msg_id = 0
        self._pending = {}  # req_id -> asyncio.Future, for requests awaited during the event loop
        self._thread = threading.Thread(target=self._run_loop, name="ha-client", daemon=True)

    def start(self):
        self._thread.start()

    # -- public, thread-safe API -------------------------------------------------

    def get_state(self, entity_id):
        with self.lock:
            return self.states.get(entity_id)

    def get_todo_items(self, entity_id):
        with self.lock:
            return list(self.todo_items.get(entity_id, []))

    def call_service(self, domain, service, entity_id, service_data=None):
        if self._loop is None:
            log.warning("Cannot call %s.%s on %s: not connected yet", domain, service, entity_id)
            return
        asyncio.run_coroutine_threadsafe(
            self._call_service_async(domain, service, entity_id, service_data), self._loop
        )

    def set_todo_item_status(self, entity_id, uid, status):
        self.call_service("todo", "update_item", entity_id, {"item": uid, "status": status})

    # -- background thread / asyncio internals -----------------------------------

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self):
        backoff = 1
        while True:
            try:
                await self._connect_once()
            except Exception as exc:
                log.warning("Home Assistant websocket error: %s", exc)
            self.connected.clear()
            log.info("Reconnecting to Home Assistant in %ds...", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _connect_once(self):
        async with websockets.connect(
            self._ws_url, open_timeout=10, ping_interval=20, ping_timeout=20
        ) as ws:
            self._ws = ws
            self._msg_id = 0
            await self._authenticate(ws)
            await self._seed_states(ws)
            if self._todo_entity_ids:
                await self._seed_todo_items(ws)
            await ws.send(json.dumps({
                "id": self._next_id(),
                "type": "subscribe_events",
                "event_type": "state_changed",
            }))
            self.connected.set()
            log.info("Connected to Home Assistant websocket (%d entities tracked)", len(self._entity_ids))

            async for raw in ws:
                self._handle_message(json.loads(raw))

    async def _authenticate(self, ws):
        first = json.loads(await ws.recv())
        if first.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected first message from HA: {first}")

        await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        resp = json.loads(await ws.recv())
        if resp.get("type") != "auth_ok":
            raise RuntimeError(f"Home Assistant authentication failed: {resp}")

    async def _seed_states(self, ws):
        req_id = self._next_id()
        await ws.send(json.dumps({"id": req_id, "type": "get_states"}))
        while True:
            resp = json.loads(await ws.recv())
            if resp.get("id") != req_id:
                continue  # ignore unrelated messages that might arrive first
            if not resp.get("success"):
                raise RuntimeError(f"get_states failed: {resp}")
            with self.lock:
                for state in resp["result"]:
                    if state["entity_id"] in self._entity_ids:
                        self.states[state["entity_id"]] = state
            missing = self._entity_ids - self.states.keys()
            if missing:
                log.warning("Entities not found in Home Assistant: %s", ", ".join(sorted(missing)))
            return

    async def _seed_todo_items(self, ws):
        for entity_id in self._todo_entity_ids:
            req_id = self._next_id()
            await ws.send(json.dumps({"id": req_id, "type": "todo/item/list", "entity_id": entity_id}))
            while True:
                resp = json.loads(await ws.recv())
                if resp.get("id") != req_id:
                    continue  # ignore unrelated messages that might arrive first
                if not resp.get("success"):
                    log.warning("todo/item/list failed for %s: %s", entity_id, resp)
                    break
                with self.lock:
                    self.todo_items[entity_id] = resp["result"]["items"]
                break

    async def _refresh_todo_items(self, entity_id):
        if self._ws is None:
            return
        req_id = None
        try:
            req_id = self._next_id()
            fut = self._loop.create_future()
            self._pending[req_id] = fut
            await self._ws.send(json.dumps({
                "id": req_id, "type": "todo/item/list", "entity_id": entity_id,
            }))
            resp = await fut
            if not resp.get("success"):
                log.warning("todo/item/list failed for %s: %s", entity_id, resp)
                return
            with self.lock:
                self.todo_items[entity_id] = resp["result"]["items"]
            if self._on_update:
                try:
                    self._on_update(entity_id)
                except Exception:
                    log.exception("on_update callback failed for %s", entity_id)
        except Exception:
            log.exception("Failed to refresh todo items for %s", entity_id)
        finally:
            if req_id is not None:
                self._pending.pop(req_id, None)

    def _handle_message(self, msg):
        if msg.get("type") == "result":
            fut = self._pending.get(msg.get("id"))
            if fut and not fut.done():
                fut.set_result(msg)
            return
        if msg.get("type") != "event":
            return
        event = msg.get("event", {})
        if event.get("event_type") != "state_changed":
            return
        data = event.get("data", {})
        entity_id = data.get("entity_id")
        new_state = data.get("new_state")
        if entity_id in self._todo_entity_ids:
            # The todo entity's state is just an item count, not useful on its
            # own - refetch the full item list whenever it changes.
            asyncio.create_task(self._refresh_todo_items(entity_id))
            return
        if entity_id not in self._entity_ids or new_state is None:
            return
        with self.lock:
            self.states[entity_id] = new_state
        if self._on_update:
            try:
                self._on_update(entity_id)
            except Exception:
                log.exception("on_update callback failed for %s", entity_id)

    async def _call_service_async(self, domain, service, entity_id, service_data=None):
        if not self.connected.is_set() or self._ws is None:
            log.warning("Cannot call %s.%s on %s: not connected", domain, service, entity_id)
            return
        try:
            msg = {
                "id": self._next_id(),
                "type": "call_service",
                "domain": domain,
                "service": service,
                "target": {"entity_id": entity_id},
            }
            if service_data:
                msg["service_data"] = service_data
            await self._ws.send(json.dumps(msg))
        except Exception:
            log.exception("Failed to call %s.%s on %s", domain, service, entity_id)

    def _next_id(self):
        self._msg_id += 1
        return self._msg_id


def _to_ws_url(http_url):
    url = http_url.rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    return url + "/api/websocket"
