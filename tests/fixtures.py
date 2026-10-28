import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from t3_lametric.source import PREFIX, iso

NOW = datetime(2026, 10, 28, 20, 0, tzinfo=ZoneInfo("Europe/Zurich"))
NOW_ISO = iso(NOW)


class Database:
    def __init__(self, directory):
        self.path = Path(directory) / "statev2.sqlite"
        self.c = sqlite3.connect(self.path)
        self.c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE orchestration_v2_projection_threads (
                thread_id TEXT PRIMARY KEY, title TEXT, default_provider TEXT,
                deleted_at TEXT, archived_at TEXT, payload_json TEXT);
            CREATE TABLE orchestration_v2_projection_runs (
                run_id TEXT PRIMARY KEY, thread_id TEXT, provider TEXT,
                provider_thread_id TEXT, status TEXT, completed_at TEXT, payload_json TEXT);
            CREATE TABLE orchestration_v2_projection_provider_threads (
                provider_thread_id TEXT PRIMARY KEY, provider TEXT, driver TEXT);
            CREATE TABLE orchestration_v2_projection_provider_turns (
                provider_turn_id TEXT PRIMARY KEY, thread_id TEXT, provider_thread_id TEXT,
                status TEXT, started_at TEXT, completed_at TEXT, payload_json TEXT);
            CREATE TABLE orchestration_v2_projection_runtime_requests (
                runtime_request_id TEXT PRIMARY KEY, thread_id TEXT, node_id TEXT,
                kind TEXT, status TEXT, created_at TEXT, payload_json TEXT);
            CREATE TABLE orchestration_v2_projection_nodes (node_id TEXT PRIMARY KEY, provider_thread_id TEXT);
            CREATE TABLE orchestration_v2_projection_turn_items (
                turn_item_id TEXT PRIMARY KEY, thread_id TEXT, node_id TEXT,
                type TEXT, status TEXT, payload_json TEXT, updated_at TEXT);
            CREATE TABLE orchestration_v2_projection_subagents (
                subagent_id TEXT PRIMARY KEY, thread_id TEXT, run_id TEXT, provider TEXT,
                provider_thread_id TEXT, child_thread_id TEXT, origin TEXT, status TEXT, payload_json TEXT);
        """)

    def row(self, table, **data):
        self.c.execute(
            f"INSERT OR REPLACE INTO {PREFIX}{table} ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",
            list(data.values()),
        )
        self.c.commit()

    def thread(self, id="main", driver="codex", child=False, **overrides):
        self.row(
            "threads",
            **dict(
                thread_id=id,
                title="Private project title",
                default_provider=driver,
                deleted_at=None,
                archived_at=None,
                payload_json=json.dumps(
                    {
                        "lineage": {
                            "relationshipToParent": "subagent" if child else None,
                            "rootThreadId": "main" if child else id,
                        }
                    }
                ),
                **overrides,
            ),
        )
        self.row("provider_threads", provider_thread_id="p:" + id, provider=driver, driver=driver)

    def run(self, id="run:main", thread="main", status="running", completed_at=None):
        self.row(
            "runs",
            run_id=id,
            thread_id=thread,
            provider="alias:work",
            provider_thread_id="p:" + thread,
            status=status,
            completed_at=completed_at,
            payload_json="{}",
        )

    def turn(
        self,
        id="turn:main",
        thread="main",
        status="completed",
        work=None,
        context=None,
        completed_at=NOW_ISO,
        started_at=None,
    ):
        self.row(
            "provider_turns",
            provider_turn_id=id,
            thread_id=thread,
            provider_thread_id="p:" + thread,
            status=status,
            started_at=started_at or iso(NOW),
            completed_at=completed_at,
            payload_json=json.dumps({"turnTokenUsage": work, "tokenUsage": context}),
        )

    def request(
        self,
        id="request:main",
        thread="main",
        kind="user_input",
        status="pending",
        created_at=NOW_ISO,
        message=None,
    ):
        node = "node:" + id
        self.row("nodes", node_id=node, provider_thread_id="p:" + thread)
        self.row(
            "runtime_requests",
            runtime_request_id=id,
            thread_id=thread,
            node_id=node,
            kind=kind,
            status=status,
            created_at=created_at,
        )
        if message:
            item = (
                {"requestId": id, "questions": [{"question": message}]}
                if kind == "user_input"
                else {"requestId": id, "prompt": message}
            )
            self.row(
                "turn_items",
                turn_item_id="item:" + id,
                thread_id=thread,
                node_id=node,
                type="user_input_request" if kind == "user_input" else "approval_request",
                status="waiting" if status == "pending" else "completed",
                payload_json=json.dumps(item),
                updated_at=created_at,
            )

    def subagent(
        self,
        id,
        parent="main",
        child=None,
        provider_thread_id=None,
        origin="app_owned",
        run="run:main",
    ):
        self.row(
            "subagents",
            subagent_id=id,
            thread_id=parent,
            run_id=run,
            provider="claudeAgent",
            provider_thread_id=provider_thread_id,
            child_thread_id=child,
            origin=origin,
            status="running",
            payload_json='{"driver":"claudeAgent"}',
        )

    def close(self):
        self.c.close()


def work(incoming=100, outgoing=20, status="complete", **extras):
    return {
        "usageScope": "main_agent",
        "usageStatus": status,
        "inputTokens": incoming,
        "outputTokens": outgoing,
        "cachedInputTokens": 80,
        "cacheCreationTokens": 10,
        "reasoningTokens": 5,
        "hasSubagents": False,
        **extras,
    }
