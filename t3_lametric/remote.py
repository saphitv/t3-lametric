"""Combine independently authenticated T3 environments without remote agents."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta

import websocket

from .activity import ActivityStream
from .config import RemoteConfig
from .source import (
    ACTIVE_RUNS,
    Agent,
    Completion,
    Request,
    Snapshot,
    SourceError,
    Usage,
    iso,
    parse_time,
)
from .usage import UsageError, UsageInterrupted


def input_message(projection: dict, request_id: str) -> str:
    items = list(projection.get("turnItems", []))
    items.extend(row.get("item", {}) for row in projection.get("visibleTurnItems", []))
    for item in reversed(items):
        kind = item.get("type")
        if item.get("requestId") == request_id and kind in (
            "user_input_request",
            "approval_request",
        ):
            text = " / ".join(
                q["question"]
                for q in item.get("questions", [])
                if isinstance(q, dict) and isinstance(q.get("question"), str)
            )
            return text or (item.get("prompt") if isinstance(item.get("prompt"), str) else "")
        if item.get("id") == request_id and kind == "secret_request":
            return ": ".join(item[k] for k in ("label", "reason") if isinstance(item.get(k), str))
    return ""


def actionable_request(thread: dict) -> dict | None:
    request = thread.get("pendingRuntimeRequest")
    if not request or request.get("kind") == "dynamic_tool_call":
        return None
    active = (
        thread.get("activeRunId")
        and (thread.get("activityRunStatus") or thread.get("status")) in ACTIVE_RUNS
    )
    if request.get("blocking") is False and not active:
        return None
    return request


class RemoteActivity(ActivityStream):
    def __init__(
        self, config: RemoteConfig, wakeup: threading.Event, *, interrupt=None, lookback: int = 120
    ):
        super().__init__(config.session_file, None, wakeup, interrupt=interrupt, origin=config.url)
        self.config, self.lookback = config, lookback
        self.data_lock = threading.Lock()
        self.threads: dict[str, dict] = {}
        self.finished: dict[str, Completion] = {}
        self.terminal_seen: set[str] = set()
        self.baselined = False
        self.baseline_runs: set[str] = set()
        self.messages: dict[str, str] = {}
        self.inputs_changed = threading.Event()
        self.input_connection = None
        self.input_worker = threading.Thread(
            target=self._inputs, name="t3-remote-input", daemon=True
        )

    def _validate_connection(self, connection):
        connection.send(
            json.dumps(
                {
                    "_tag": "Request",
                    "id": "identity",
                    "tag": "server.getConfig",
                    "headers": [],
                    "payload": {},
                }
            )
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            raw = json.loads(connection.recv())
            for message in raw if isinstance(raw, list) else [raw]:
                if message.get("_tag") == "Ping":
                    connection.send(json.dumps({"_tag": "Pong"}))
                elif message.get("_tag") == "Exit" and message.get("requestId") == "identity":
                    result = message.get("exit", {})
                    if (
                        result.get("_tag") != "Success"
                        or result.get("value", {}).get("environment", {}).get("environmentId")
                        != self.config.environment_id
                    ):
                        raise ValueError("Remote environment identity did not match")
                    return
        raise OSError("Remote identity check timed out")

    def start(self):
        self.input_worker.start()
        super().start()

    def _store(self, thread: dict, *, baseline: bool = False):
        # Drop message bodies, models, paths, and all unrelated shell metadata.
        keys = (
            "id",
            "latestRunId",
            "activeRunId",
            "status",
            "activityRunStatus",
            "latestRunCompletedAt",
            "providerInstanceId",
            "archivedAt",
        )
        slim = {k: thread.get(k) for k in keys}
        slim["lineage"] = {
            k: (thread.get("lineage") or {}).get(k)
            for k in ("relationshipToParent", "parentThreadId", "rootThreadId")
        }
        request = thread.get("pendingRuntimeRequest")
        slim["pendingRuntimeRequest"] = (
            {k: request.get(k) for k in ("id", "kind", "createdAt", "blocking")}
            if request
            else None
        )
        self.threads[thread["id"]] = slim
        if (
            baseline
            and slim.get("activeRunId")
            and (slim.get("activityRunStatus") or slim.get("status"))
            not in ("queued", "pending", "idle")
        ):
            self.baseline_runs.add(slim["activeRunId"])
        run_id = slim.get("latestRunId")
        status, completed = slim.get("status"), slim.get("latestRunCompletedAt")
        if run_id and status in ("completed", "failed") and completed:
            key = f"{run_id}:{status}:{completed}"
            if not baseline and key not in self.terminal_seen:
                self.finished[key] = Completion(
                    run_id,
                    thread["id"],
                    slim.get("providerInstanceId") or "remote",
                    status,
                    completed,
                    slim["lineage"].get("relationshipToParent") == "subagent",
                )
            self.terminal_seen.add(key)

    def _event(self, event: dict):
        kind = event.get("kind")
        with self.data_lock:
            if kind == "snapshot" and "resolvedRepositoryIdentityRoots" not in event:
                self.threads = {}
                for thread in event["snapshot"]["threads"]:
                    self._store(thread, baseline=not self.baselined)
                self.baselined = True
            elif kind == "thread.updated":
                self._store(event["thread"])
            elif kind == "thread.removed":
                self.threads.pop(event["threadId"], None)
            pending = self._pending()
            self.baseline_runs.intersection_update(
                t["activeRunId"] for t in self.threads.values() if t.get("activeRunId")
            )
            self.messages = {k: v for k, v in self.messages.items() if k in pending}
            needs_input = any(k not in self.messages for k in pending)
        super()._event(event)
        if needs_input:
            self.inputs_changed.set()

    def _pending(self) -> dict:
        return {
            r["id"]: (thread["id"], r)
            for thread in self.threads.values()
            if not thread.get("archivedAt") and (r := actionable_request(thread))
        }

    def _fetch_input(self, thread_id: str, request_id: str) -> str:
        config = self.config
        credential = json.loads(config.session_file.read_text())
        c = websocket.create_connection(
            config.url.replace("https:", "wss:").replace("http:", "ws:")
            + "/ws?orchestrationProtocol=2",
            header={"Authorization": "Bearer " + credential["token"]},
            origin=config.url,
            timeout=3,
            http_no_proxy=["127.0.0.1"],
        )
        self.input_connection = c
        try:
            c.send(
                json.dumps(
                    {
                        "_tag": "Request",
                        "id": "input",
                        "tag": "orchestration.subscribeThread",
                        "headers": [],
                        "payload": {
                            "threadId": thread_id,
                            "acceptBoundedSnapshot": True,
                            "acceptCompactTurnItems": False,
                        },
                    }
                )
            )
            deadline = time.monotonic() + 3
            while not self.stopping.is_set() and time.monotonic() < deadline:
                raw = json.loads(c.recv())
                for message in raw if isinstance(raw, list) else [raw]:
                    if message.get("_tag") == "Ping":
                        c.send(json.dumps({"_tag": "Pong"}))
                    elif message.get("_tag") == "Chunk":
                        c.send(json.dumps({"_tag": "Ack", "requestId": "input"}))
                        for event in message.get("values", []):
                            if event.get("kind") == "snapshot":
                                return input_message(event["projection"], request_id)
                    elif message.get("_tag") == "Exit":
                        return ""
            return ""
        finally:
            self.input_connection = None
            c.close()

    def _inputs(self):
        while not self.stopping.is_set():
            self.inputs_changed.wait(30)
            self.inputs_changed.clear()
            with self.data_lock:
                pending = list(self._pending().items())[:20]
                needed = [
                    (key, thread_id)
                    for key, (thread_id, _) in pending
                    if not self.messages.get(key)
                ]
            for key, thread_id in needed:
                if self.stopping.is_set():
                    return
                try:
                    text = self._fetch_input(thread_id, key)
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    AttributeError,
                    websocket.WebSocketException,
                ):
                    text = ""
                with self.data_lock:
                    current = self._pending()
                    changed = key in current and self.messages.get(key) != text
                    if key in current:
                        self.messages[key] = text
                if changed:
                    self._wake()

    def snapshot(self, now: datetime) -> Snapshot:
        if not self.healthy:
            return Snapshot(now, False, (), (), (), Usage())
        with self.data_lock:
            threads = dict(self.threads)
            baseline_runs = set(self.baseline_runs)
            messages = dict(self.messages)
            cutoff = now - timedelta(seconds=self.lookback)
            self.finished = {
                k: v
                for k, v in self.finished.items()
                if (parse_time(v.completed_at) or cutoff) >= cutoff
            }
            self.terminal_seen = set(self.finished) | {
                f"{t['latestRunId']}:{t['status']}:{t['latestRunCompletedAt']}"
                for t in self.threads.values()
                if t.get("latestRunId") and t.get("latestRunCompletedAt")
            }
            completions = tuple(self.finished.values())
        prefix = self.config.environment_id + ":"

        def root(thread_id):
            seen = set()
            while thread_id not in seen:
                seen.add(thread_id)
                lineage = threads.get(thread_id, {}).get("lineage", {})
                if lineage.get("relationshipToParent") != "subagent":
                    break
                parent = lineage.get("parentThreadId") or lineage.get("rootThreadId")
                if not parent:
                    break
                thread_id = parent
            return prefix + thread_id

        agents, requests = [], []
        for thread in threads.values():
            if thread.get("archivedAt"):
                continue
            thread_id = thread["id"]
            run_id = thread.get("activeRunId")
            status = thread.get("activityRunStatus") or thread.get("status")
            if run_id and status in ACTIVE_RUNS:
                agents.append(
                    Agent(
                        prefix + run_id,
                        prefix + thread_id,
                        thread.get("providerInstanceId") or "remote",
                        status,
                        thread["lineage"].get("relationshipToParent") == "subagent",
                        session_id=root(thread_id),
                        announce_start=run_id not in baseline_runs,
                    )
                )
            request = actionable_request(thread)
            if request:
                text = messages.get(request["id"]) or "Input needed in T3 Code"
                requests.append(
                    Request(
                        prefix + request["id"],
                        prefix + thread_id,
                        "remote",
                        request["kind"],
                        request.get("createdAt") or iso(now),
                        self.config.label + ": " + text,
                        blocking=request.get("blocking") is not False,
                    )
                )
        completions = tuple(
            replace(c, id=prefix + c.id, thread_id=prefix + c.thread_id) for c in completions
        )
        return Snapshot(now, True, tuple(agents), tuple(requests), completions, Usage())

    def close(self):
        self.stopping.set()
        self.inputs_changed.set()
        if self.input_connection is not None:
            self.input_connection.abort()
        super().close()
        if self.input_worker.is_alive():
            self.input_worker.join(timeout=4)


class CombinedReader:
    def __init__(self, local, remotes):
        self.local, self.remotes = local, remotes
        self.error = None

    def read(self, now):
        try:
            local = self.local.read(now)
            self.error = None
        except SourceError as error:
            self.error = str(error)
            local = Snapshot(now, False, (), (), (), Usage())
        snapshots = [local, *(remote.snapshot(now) for remote in self.remotes)]
        live = [s for s in snapshots if s.online is not False]
        return Snapshot(
            now,
            bool(live),
            tuple(a for s in live for a in s.agents),
            tuple(r for s in live for r in s.requests),
            tuple(c for s in live for c in s.completions),
            snapshots[0].usage,
            {k: v for s in live for k, v in s.titles.items()},
        )


class CombinedActivity:
    def __init__(self, local, remotes):
        self.local, self.remotes = local, remotes
        self.streams = ([local] if local else []) + list(remotes)

    @property
    def healthy(self):
        return bool(self.local and self.local.healthy)

    @property
    def error(self):
        return self.local.error if self.local else None

    @property
    def description(self):
        local = "local live" if self.healthy else "local polling"
        return "; ".join(
            [
                local,
                *(r.config.label + (" live" if r.healthy else " offline") for r in self.remotes),
            ]
        )

    def changed(self):
        return any([stream.changed() for stream in self.streams])

    def start(self):
        for stream in self.streams:
            stream.start()

    def close(self):
        for stream in self.streams:
            stream.close()


class CombinedUsage:
    def __init__(self, local, remotes, *, local_online=lambda: True):
        self.local, self.remotes = local, remotes
        self.local_online = local_online
        self.values = {}
        self.day = None

    def read(self, now):
        if self.day != now.date():
            self.values, self.day = {}, now.date()
        partial = 0
        clients = [("local", self.local, self.local_online())] if self.local else []
        clients += [
            (remote.config.environment_id, client, remote.healthy)
            for remote, client in self.remotes
        ]
        for key, client, online in clients:
            if online:
                try:
                    self.values[key] = client.read(now)
                    continue
                except UsageInterrupted:
                    raise
                except UsageError:
                    pass
            partial += 1
        if not self.values:
            raise UsageError(
                "No environment returned a daily usage total; keeping the last known total."
            )
        return Usage(
            input_tokens=sum(v.input_tokens for v in self.values.values()),
            output_tokens=sum(v.output_tokens for v in self.values.values()),
            unavailable_turns=partial,
            source="t3_usage",
        )

    def close(self):
        for client in ([self.local] if self.local else []) + [c for _, c in self.remotes]:
            client.close()

    def interrupt(self):
        for client in ([self.local] if self.local else []) + [c for _, c in self.remotes]:
            client.interrupt()
