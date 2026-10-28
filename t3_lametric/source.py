"""Read the small v2 projections, never the transcript or provider event logs."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

ACTIVE_RUNS = ("queued", "preparing", "starting", "running", "waiting")
PREFIX = "orchestration_v2_projection_"


class SourceError(Exception):
    pass


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def canonical_provider(driver: str) -> str:
    return {"claudeAgent": "claude", "claudeCode": "claude"}.get(driver, driver)


@dataclass(frozen=True)
class Agent:
    id: str
    thread_id: str
    provider: str
    status: str
    subagent: bool = False
    context_used: int | None = None
    context_max: int | None = None
    provider_thread_id: str | None = None
    session_id: str | None = None
    announce_start: bool = True


@dataclass(frozen=True)
class Request:
    id: str
    thread_id: str
    provider: str
    kind: str
    created_at: str
    message: str = ""
    blocking: bool = True


@dataclass(frozen=True)
class Completion:
    id: str
    thread_id: str
    provider: str
    status: str
    completed_at: str
    subagent: bool = False


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    complete_turns: int = 0
    partial_turns: int = 0
    unavailable_turns: int = 0
    by_provider: dict[str, int] = field(default_factory=dict)
    source: str = "turns"
    read_at: str | None = None

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def available(self) -> bool:
        return self.source == "t3_usage" or self.complete_turns + self.partial_turns > 0

    @property
    def incomplete(self) -> bool:
        return self.partial_turns + self.unavailable_turns > 0


@dataclass(frozen=True)
class WeeklyQuota:
    label: str
    used_percent: float | None = None
    checked_at: str | None = None
    resets_at: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True)
class Snapshot:
    captured_at: datetime
    online: bool | None
    agents: tuple[Agent, ...]
    requests: tuple[Request, ...]
    completions: tuple[Completion, ...]
    usage: Usage
    titles: dict[str, str] = field(default_factory=dict)
    weekly: tuple[WeeklyQuota, ...] = ()


def server_online(runtime_path: Path | None) -> bool | None:
    if runtime_path is None:
        return None
    try:
        runtime = json.loads(runtime_path.read_text())
        pid = runtime.get("pid")
        if type(pid) is not int or pid <= 0:
            return False
        if os.name == "nt":
            # Windows kill(pid, 0) is not a safe process-existence probe.
            return None
        os.kill(pid, 0)
        return True
    except PermissionError:
        return None
    except (OSError, ValueError, AttributeError):
        return False


def token_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


class T3Reader:
    """Each poll is a short consistent transaction opened with SQLite mode=ro."""

    REQUIRED: ClassVar[dict[str, set[str]]] = {
        "threads": {
            "thread_id",
            "title",
            "default_provider",
            "deleted_at",
            "archived_at",
            "payload_json",
        },
        "runs": {
            "run_id",
            "thread_id",
            "provider",
            "provider_thread_id",
            "status",
            "completed_at",
            "payload_json",
        },
        "provider_threads": {"provider_thread_id", "provider", "driver"},
        "provider_turns": {
            "provider_turn_id",
            "thread_id",
            "provider_thread_id",
            "status",
            "started_at",
            "completed_at",
            "payload_json",
        },
        "runtime_requests": {
            "runtime_request_id",
            "thread_id",
            "node_id",
            "kind",
            "status",
            "created_at",
            "payload_json",
        },
        "nodes": {"node_id", "provider_thread_id"},
        "turn_items": {
            "thread_id",
            "node_id",
            "type",
            "status",
            "payload_json",
            "updated_at",
            "turn_item_id",
        },
        "subagents": {
            "subagent_id",
            "thread_id",
            "run_id",
            "provider",
            "provider_thread_id",
            "child_thread_id",
            "origin",
            "status",
            "payload_json",
        },
    }

    def __init__(
        self,
        database: Path,
        runtime_path: Path | None,
        lookback: int = 120,
        *,
        include_usage: bool = True,
    ):
        self.database = database.expanduser().resolve()
        self.runtime_path = runtime_path
        self.lookback = lookback
        self.include_usage = include_usage
        self._validated_identity: tuple[int, int] | None = None

    def _validate(self, connection: sqlite3.Connection) -> None:
        stat = self.database.stat()
        identity = (stat.st_dev, stat.st_ino)
        if self._validated_identity == identity:
            return
        for table, required in self.REQUIRED.items():
            columns = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({PREFIX}{table})")
            }
            missing = required - columns
            if missing:
                raise SourceError(
                    f"Unsupported T3 database: {PREFIX}{table} is missing {', '.join(sorted(missing))}. "
                    "This bridge requires T3 Code's v2 projections (0.1.x); check the database path or update the bridge."
                )
        self._validated_identity = identity

    def read(self, now: datetime | None = None) -> Snapshot:
        now = now or datetime.now().astimezone()
        if now.tzinfo is None:
            raise ValueError("A timezone-aware time is required")
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        cutoff = iso(now - timedelta(seconds=self.lookback))
        connection = None
        try:
            connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=1)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            deadline = time.monotonic() + 3
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            self._validate(connection)
            connection.execute("BEGIN")
            result = self._read(connection, now, iso(day_start), iso(day_end), cutoff)
            connection.rollback()
            return result
        except (sqlite3.Error, OSError, json.JSONDecodeError) as error:
            # Do not propagate SQL payloads, transcripts, or provider credentials.
            raise SourceError(f"Cannot read T3 database {self.database}: {error}") from error
        finally:
            if connection is not None:
                connection.close()

    def _read(
        self, c: sqlite3.Connection, now: datetime, day_start: str, day_end: str, cutoff: str
    ) -> Snapshot:
        threads = {
            row["thread_id"]: dict(row)
            for row in c.execute(f"""
                SELECT thread_id, title, default_provider, archived_at,
                       json_extract(payload_json, '$.lineage.relationshipToParent') AS relationship,
                       json_extract(payload_json, '$.lineage.rootThreadId') AS root_thread_id,
                       json_extract(payload_json, '$.lineage.parentThreadId') AS parent_thread_id
                FROM {PREFIX}threads WHERE deleted_at IS NULL
            """)
        }

        def session_id(thread_id: str) -> str:
            seen = set()
            while thread_id not in seen:
                seen.add(thread_id)
                thread = threads.get(thread_id)
                if thread is None or thread["relationship"] != "subagent":
                    break
                parent = thread["parent_thread_id"] or thread["root_thread_id"]
                if not parent:
                    break
                thread_id = parent
            return thread_id

        runs = list(
            c.execute(
                f"""
            SELECT r.run_id, r.thread_id, r.status, r.completed_at,
                   r.provider_thread_id, COALESCE(p.driver, p.provider, r.provider) AS driver
            FROM {PREFIX}runs r
            LEFT JOIN {PREFIX}provider_threads p ON p.provider_thread_id = r.provider_thread_id
            WHERE r.status IN ('queued', 'preparing', 'starting', 'running', 'waiting')
               OR r.completed_at >= ?
        """,
                (cutoff,),
            )
        )
        active_runs = {r["run_id"] for r in runs if r["status"] in ACTIVE_RUNS}
        agents: dict[str, Agent] = {}
        completions = []
        for run in runs:
            thread = threads.get(run["thread_id"])
            if thread is None or thread["archived_at"] is not None:
                continue
            driver = canonical_provider(run["driver"])
            child = thread["relationship"] == "subagent"
            if run["status"] in ACTIVE_RUNS:
                agents[run["thread_id"]] = Agent(
                    run["run_id"],
                    run["thread_id"],
                    driver,
                    run["status"],
                    child,
                    provider_thread_id=run["provider_thread_id"],
                    session_id=session_id(run["thread_id"]),
                )
            elif run["completed_at"]:
                completions.append(
                    Completion(
                        run["run_id"],
                        run["thread_id"],
                        driver,
                        run["status"],
                        run["completed_at"],
                        child,
                    )
                )

        # App-owned delegates have a child run; native delegates sometimes only
        # have a subagent row. Count either representation once.
        provider_thread_ids = {
            r["provider_thread_id"]
            for r in runs
            if r["thread_id"] in agents and r["status"] in ACTIVE_RUNS
        }
        provider_thread_ids.discard(None)
        for row in c.execute(f"""
            SELECT s.subagent_id, s.thread_id, s.child_thread_id, s.run_id, s.origin,
                   s.provider_thread_id, s.status,
                   COALESCE(p.driver, json_extract(s.payload_json, '$.driver'), s.provider) AS driver
            FROM {PREFIX}subagents s
            LEFT JOIN {PREFIX}provider_threads p ON p.provider_thread_id = s.provider_thread_id
            WHERE s.status IN ('pending', 'running', 'waiting')
        """):
            parent = threads.get(row["thread_id"])
            if parent is None or parent["archived_at"] is not None:
                continue
            if row["child_thread_id"] in agents or row["provider_thread_id"] in provider_thread_ids:
                continue
            if row["origin"] == "provider_native" and row["run_id"] not in active_runs:
                continue
            child_id = row["child_thread_id"] or row["thread_id"]
            agents[row["subagent_id"]] = Agent(
                row["subagent_id"],
                child_id,
                canonical_provider(row["driver"]),
                row["status"],
                True,
                provider_thread_id=row["provider_thread_id"],
                session_id=session_id(row["thread_id"]),
            )
            if row["provider_thread_id"]:
                provider_thread_ids.add(row["provider_thread_id"])

        requests = []
        active_thread_ids = {agent.thread_id for agent in agents.values()}
        for row in c.execute(f"""
            SELECT q.runtime_request_id, q.thread_id, q.node_id, q.kind, q.created_at,
                   json_extract(q.payload_json, '$.responseCapability.type') AS response_type,
                   COALESCE(p.driver, p.provider, t.default_provider) AS driver
            FROM {PREFIX}runtime_requests q
            JOIN {PREFIX}threads t ON t.thread_id = q.thread_id
            LEFT JOIN {PREFIX}nodes n ON n.node_id = q.node_id
            LEFT JOIN {PREFIX}provider_threads p ON p.provider_thread_id = n.provider_thread_id
            WHERE q.status = 'pending' AND t.deleted_at IS NULL AND t.archived_at IS NULL
                  AND q.kind <> 'dynamic_tool_call'
        """):
            # Async questions can remain pending after a run finishes or is
            # cancelled. They must not pin the clock's attention indicator.
            if row["response_type"] == "message" and row["thread_id"] not in active_thread_ids:
                continue
            item = c.execute(
                f"""
                SELECT json_extract(payload_json, '$.questions') AS questions,
                       json_extract(payload_json, '$.prompt') AS prompt
                FROM {PREFIX}turn_items
                WHERE thread_id = ? AND node_id = ?
                  AND type IN ('user_input_request', 'approval_request')
                  AND json_extract(payload_json, '$.requestId') = ?
                ORDER BY rowid DESC LIMIT 1
            """,
                (row["thread_id"], row["node_id"], row["runtime_request_id"]),
            ).fetchone()
            message = ""
            if item:
                questions = json.loads(item["questions"]) if item["questions"] else []
                message = " / ".join(
                    q["question"]
                    for q in questions
                    if isinstance(q, dict) and isinstance(q.get("question"), str)
                )
                if not message and isinstance(item["prompt"], str):
                    message = item["prompt"]
            requests.append(
                Request(
                    row["runtime_request_id"],
                    row["thread_id"],
                    canonical_provider(row["driver"]),
                    row["kind"],
                    row["created_at"],
                    message,
                    blocking=row["response_type"] != "message",
                )
            )

        # Secret cards are not runtime requests. Read only their public label
        # and reason on active threads; never read the secret value or answers.
        active_threads = sorted({r["thread_id"] for r in runs if r["status"] in ACTIVE_RUNS})
        if active_threads:
            placeholders = ",".join("?" for _ in active_threads)
            for row in c.execute(
                f"""
                SELECT turn_item_id, thread_id, updated_at,
                       json_extract(payload_json, '$.label') AS label,
                       json_extract(payload_json, '$.reason') AS reason
                FROM {PREFIX}turn_items
                WHERE thread_id IN ({placeholders})
                  AND type = 'secret_request' AND status IN ('pending', 'waiting', 'running')
                  AND json_extract(payload_json, '$.secretStatus') = 'pending'
            """,
                active_threads,
            ):
                thread = threads.get(row["thread_id"])
                if thread and thread["archived_at"] is None:
                    requests.append(
                        Request(
                            row["turn_item_id"],
                            row["thread_id"],
                            canonical_provider(thread["default_provider"]),
                            "secret",
                            row["updated_at"],
                            ": ".join(s for s in (row["label"], row["reason"]) if s),
                        )
                    )

        usage = Usage()
        contexts: dict[str, tuple[str, int, int | None]] = {}
        # The projection contains one row per native turn. turnTokenUsage is
        # normalized work for that turn; tokenUsage is context size and must
        # never be summed as spend. Cache and reasoning are already included.
        for row in (
            c.execute(
                f"""
            SELECT t.thread_id, t.provider_thread_id, t.status, t.started_at, t.completed_at,
                   COALESCE(p.driver, p.provider, h.default_provider) AS driver,
                   json_extract(t.payload_json, '$.turnTokenUsage') AS work,
                   json_extract(t.payload_json, '$.tokenUsage') AS context
            FROM {PREFIX}provider_turns t
            JOIN {PREFIX}threads h ON h.thread_id = t.thread_id
            LEFT JOIN {PREFIX}provider_threads p ON p.provider_thread_id = t.provider_thread_id
            WHERE h.deleted_at IS NULL AND (
                (t.completed_at >= ? AND t.completed_at < ?)
                OR t.status IN ('pending', 'running')
            )
        """,
                (day_start, day_end),
            )
            if self.include_usage
            else ()
        ):
            context = json.loads(row["context"]) if row["context"] else {}
            used = token_count(context.get("usedTokens"))
            if used is not None and row["provider_thread_id"] in provider_thread_ids:
                stamp = context.get("updatedAt") or row["completed_at"] or row["started_at"] or ""
                previous = contexts.get(row["provider_thread_id"])
                if previous is None or stamp > previous[0]:
                    contexts[row["provider_thread_id"]] = (
                        stamp,
                        used,
                        token_count(context.get("maxTokens")),
                    )
            # Work is normally only available when a provider turn finishes.
            if not row["completed_at"] or not (day_start <= row["completed_at"] < day_end):
                continue
            work = json.loads(row["work"]) if row["work"] else {}
            if work.get("usageScope") != "main_agent" or work.get("usageStatus") == "unavailable":
                usage.unavailable_turns += 1
                continue
            incoming, outgoing = (
                token_count(work.get("inputTokens")),
                token_count(work.get("outputTokens")),
            )
            if incoming is None and outgoing is None:
                usage.unavailable_turns += 1
                continue
            if (
                work.get("usageStatus") == "complete"
                and incoming is not None
                and outgoing is not None
            ):
                usage.complete_turns += 1
            else:
                usage.partial_turns += 1
            usage.input_tokens += incoming or 0
            usage.output_tokens += outgoing or 0
            driver = canonical_provider(row["driver"])
            usage.by_provider[driver] = (
                usage.by_provider.get(driver, 0) + (incoming or 0) + (outgoing or 0)
            )

        for key, agent in agents.items():
            if agent.provider_thread_id in contexts:
                _, used, maximum = contexts[agent.provider_thread_id]
                agents[key] = replace(agent, context_used=used, context_max=maximum)
        return Snapshot(
            now,
            server_online(self.runtime_path),
            tuple(agents.values()),
            tuple(requests),
            tuple(completions),
            usage,
            {key: value["title"] for key, value in threads.items()},
        )
