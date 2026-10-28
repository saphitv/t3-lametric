"""Use T3's sidebar stream as a low-traffic wakeup for activity changes."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import websocket


def signature(thread: dict) -> tuple:
    request = thread.get("pendingRuntimeRequest") or {}
    lineage = thread.get("lineage") or {}
    return (
        thread.get("status"),
        thread.get("latestRunId"),
        thread.get("latestRunCompletedAt"),
        thread.get("activeRunId"),
        thread.get("activityRunStatus"),
        thread.get("archivedAt"),
        request.get("id"),
        request.get("kind"),
        request.get("blocking"),
        lineage.get("relationshipToParent"),
        lineage.get("parentThreadId"),
    )


class ActivityStream:
    def __init__(
        self,
        session_file: Path,
        runtime_file: Path | None,
        wakeup: threading.Event,
        *,
        interrupt=None,
        origin: str | None = None,
    ):
        self.session_file, self.runtime_file, self.wakeup = session_file, runtime_file, wakeup
        self.interrupt = interrupt
        self.origin = origin
        self.dirty = threading.Event()
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.healthy = False
        self.error: str | None = None
        self.connection = None
        self.states: dict[str, tuple] = {}
        self.worker = threading.Thread(target=self._run, name="t3-activity", daemon=True)

    def start(self):
        self.worker.start()

    def changed(self) -> bool:
        with self.lock:
            changed = self.dirty.is_set()
            self.dirty.clear()
        return changed

    def _wake(self):
        with self.lock:
            self.dirty.set()
        if self.interrupt:
            self.interrupt()
        self.wakeup.set()

    def _event(self, event: dict) -> None:
        kind = event.get("kind")
        if kind == "snapshot":
            # Enrichment-only snapshots carry no authoritative thread list.
            if "resolvedRepositoryIdentityRoots" not in event:
                self.states = {t["id"]: signature(t) for t in event["snapshot"]["threads"]}
                self._wake()
        elif kind == "thread.updated":
            thread = event["thread"]
            key, value = thread["id"], signature(thread)
            if self.states.get(key) != value:
                self.states[key] = value
                self._wake()
        elif kind == "thread.removed":
            self.states.pop(event["threadId"], None)
            self._wake()
        elif kind == "synchronized":
            self.healthy, self.error = True, None
            self._wake()

    def _validate_connection(self, connection):
        pass

    def _run(self):
        while not self.stopping.is_set():
            connection = None
            try:
                credential = json.loads(self.session_file.read_text())
                if self.origin:
                    origin = self.origin
                else:
                    runtime = json.loads(self.runtime_file.read_text())
                    origin = f"http://127.0.0.1:{int(runtime['port'])}"
                connection = websocket.create_connection(
                    origin.replace("https:", "wss:").replace("http:", "ws:")
                    + "/ws?orchestrationProtocol=2",
                    header={"Authorization": "Bearer " + credential["token"]},
                    origin=origin,
                    timeout=3,
                    http_no_proxy=["127.0.0.1"],
                )
                self.connection = connection
                self._validate_connection(connection)
                connection.send(
                    json.dumps(
                        {
                            "_tag": "Request",
                            "id": "activity",
                            "tag": "orchestration.subscribeShell",
                            "headers": [],
                            "payload": {"requestCompletionMarker": True},
                        }
                    )
                )
                connection.settimeout(15)
                waiting_for_pong = False
                while not self.stopping.is_set():
                    try:
                        raw = json.loads(connection.recv())
                    except websocket.WebSocketTimeoutException:
                        if waiting_for_pong:
                            raise OSError("Heartbeat timed out") from None
                        connection.send(json.dumps({"_tag": "Ping"}))
                        waiting_for_pong = True
                        continue
                    waiting_for_pong = False
                    for message in raw if isinstance(raw, list) else [raw]:
                        if message.get("_tag") == "Ping":
                            connection.send(json.dumps({"_tag": "Pong"}))
                        elif (
                            message.get("_tag") == "Chunk"
                            and message.get("requestId") == "activity"
                        ):
                            connection.send(json.dumps({"_tag": "Ack", "requestId": "activity"}))
                            for event in message.get("values", []):
                                self._event(event)
                        elif message.get("_tag") == "Exit":
                            raise OSError("Activity subscription ended")
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                websocket.WebSocketException,
            ):
                self.healthy = False
                self.error = (
                    "T3 activity stream disconnected; using activity polling until it reconnects."
                )
                self._wake()
            finally:
                self.healthy = False
                self.connection = None
                if connection is not None:
                    connection.close()
            self.stopping.wait(5)

    def close(self):
        self.stopping.set()
        if self.connection is not None:
            self.connection.abort()
        if self.worker.is_alive():
            self.worker.join(timeout=4)
