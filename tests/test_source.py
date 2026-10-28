import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fixtures import NOW, Database, work

from t3_lametric.source import SourceError, T3Reader, iso, server_online


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Database(self.temporary.name)
        self.addCleanup(self.db.close)
        self.reader = T3Reader(self.db.path, None)
        self.db.thread()

    def test_normalized_work_is_counted_once_without_cache_or_reasoning_duplication(self):
        self.db.turn(work=work(), context={"usedTokens": 9999999, "maxTokens": 10000000})
        self.db.thread("claude", driver="claudeAgent")
        self.db.turn(id="turn:claude", thread="claude", work=work(200, 40))
        first = self.reader.read(NOW)
        self.assertEqual(first.usage.total, 360)
        self.assertEqual(first.usage.by_provider, {"codex": 120, "claude": 240})
        self.assertEqual(self.reader.read(NOW).usage.total, 360)

    def test_missing_counts_remain_unknown_and_partial_counts_are_marked(self):
        self.db.turn(work=None)
        self.db.turn(id="partial", work=work(incoming=None, outgoing=17, status="partial"))
        self.db.turn(id="bad", work=work(incoming=-10, outgoing=True))
        usage = self.reader.read(NOW).usage
        self.assertEqual(usage.total, 17)
        self.assertEqual(usage.partial_turns, 1)
        self.assertEqual(usage.unavailable_turns, 2)
        self.assertTrue(usage.incomplete)

    def test_live_context_is_separate_from_finished_usage(self):
        self.db.run()
        self.db.turn(
            status="running",
            completed_at=None,
            context={"usedTokens": 300, "maxTokens": 1000, "updatedAt": iso(NOW)},
        )
        snapshot = self.reader.read(NOW)
        self.assertEqual(snapshot.agents[0].context_used, 300)
        self.assertEqual(snapshot.agents[0].context_max, 1000)
        self.assertFalse(snapshot.usage.available)

    def test_context_from_a_previous_provider_is_not_assigned_to_new_provider(self):
        self.db.run()
        self.db.turn(
            status="running", completed_at=None, context={"usedTokens": 500, "maxTokens": 1000}
        )
        self.db.row(
            "provider_threads",
            provider_thread_id="new-provider",
            provider="claudeAgent",
            driver="claudeAgent",
        )
        self.db.c.execute(
            "UPDATE orchestration_v2_projection_runs SET provider_thread_id = 'new-provider'"
        )
        self.db.c.commit()
        snapshot = self.reader.read(NOW)
        self.assertEqual(snapshot.agents[0].provider, "claude")
        self.assertIsNone(snapshot.agents[0].context_used)

    def test_child_run_and_subagent_projection_count_as_one_agent(self):
        self.db.run()
        self.db.thread("child", driver="claudeAgent", child=True)
        self.db.run("run:child", "child")
        self.db.subagent("delegate", child="child", provider_thread_id="p:child")
        self.db.subagent("native", provider_thread_id="p:native", origin="provider_native")
        self.db.subagent(
            "duplicate-native", provider_thread_id="p:native", origin="provider_native"
        )
        snapshot = self.reader.read(NOW)
        self.assertEqual(len(snapshot.agents), 3)
        self.assertEqual(sum(a.subagent for a in snapshot.agents), 2)

    def test_native_subagents_with_finished_parents_do_not_stay_running(self):
        self.db.run(status="completed", completed_at=iso(NOW))
        self.db.subagent("stale-native", origin="provider_native")
        self.assertEqual(self.reader.read(NOW).agents, ())

    def test_pending_human_requests_exclude_internal_tool_requests(self):
        self.db.request("input")
        self.db.request("approval", kind="command")
        self.db.request("tool", kind="dynamic_tool_call")
        self.db.request("old", status="resolved")
        self.assertEqual({r.id for r in self.reader.read(NOW).requests}, {"input", "approval"})

    def test_async_requests_on_finished_or_cancelled_chats_do_not_pin_input(self):
        self.db.request("async")
        self.db.request("blocking", kind="command")
        self.db.c.execute(
            "UPDATE orchestration_v2_projection_runtime_requests "
            "SET payload_json=json_set(COALESCE(payload_json, '{}'), "
            "'$.responseCapability.type', 'message') "
            "WHERE runtime_request_id='async'"
        )
        self.db.c.commit()
        self.db.run()
        self.assertEqual({r.id for r in self.reader.read(NOW).requests}, {"async", "blocking"})
        for status in ("completed", "cancelled", "failed"):
            self.db.run(status=status, completed_at=iso(NOW))
            self.assertEqual({r.id for r in self.reader.read(NOW).requests}, {"blocking"})

    def test_pending_question_and_approval_messages_are_read_without_answers(self):
        self.db.request("input", message="Which branch should I use?")
        self.db.request("approve", kind="command", message="May I run the build?")
        self.db.c.execute(
            "UPDATE orchestration_v2_projection_turn_items SET payload_json = json_set(payload_json, '$.questionAnswer', 'DO NOT DISPLAY THIS ANSWER')"
        )
        self.db.c.commit()
        messages = {r.id: r.message for r in self.reader.read(NOW).requests}
        self.assertEqual(
            messages, {"input": "Which branch should I use?", "approve": "May I run the build?"}
        )

    def test_secret_card_displays_only_the_label_and_reason(self):
        self.db.run()
        self.db.row(
            "turn_items",
            turn_item_id="secret",
            thread_id="main",
            node_id="secret-node",
            type="secret_request",
            status="waiting",
            updated_at=iso(NOW),
            payload_json='{"label":"Device key","reason":"Connect the clock","secretStatus":"pending","value":"NEVER DISPLAY"}',
        )
        requests = self.reader.read(NOW).requests
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].message, "Device key: Connect the clock")

    def test_ui_usage_mode_skips_provider_turn_history(self):
        self.db.turn(work=work())
        snapshot = T3Reader(self.db.path, None, include_usage=False).read(NOW)
        self.assertEqual(snapshot.usage.total, 0)
        self.assertFalse(snapshot.usage.available)

    def test_forked_chats_are_distinct_sessions_while_delegates_share_their_parent(self):
        self.db.run()
        self.db.thread("fork")
        self.db.c.execute(
            "UPDATE orchestration_v2_projection_threads SET payload_json=? WHERE thread_id='fork'",
            (
                '{"lineage":{"relationshipToParent":"fork","rootThreadId":"main","parentThreadId":"main"}}',
            ),
        )
        self.db.c.commit()
        self.db.run("run:fork", "fork")
        self.db.thread("child", child=True)
        self.db.run("run:child", "child")
        sessions = {a.thread_id: a.session_id for a in self.reader.read(NOW).agents}
        self.assertEqual(sessions, {"main": "main", "fork": "fork", "child": "main"})

    def test_short_runs_are_visible_even_if_never_seen_running(self):
        self.db.run(status="completed", completed_at=iso(NOW - timedelta(seconds=1)))
        self.db.run("old", status="completed", completed_at=iso(NOW - timedelta(hours=1)))
        snapshot = self.reader.read(NOW)
        self.assertEqual([r.id for r in snapshot.completions], ["run:main"])

    def test_archived_threads_keep_usage_but_have_no_activity_or_alerts(self):
        self.db.c.execute(
            "UPDATE orchestration_v2_projection_threads SET archived_at=?", (iso(NOW),)
        )
        self.db.c.commit()
        self.db.run()
        self.db.request()
        self.db.turn(work=work())
        snapshot = self.reader.read(NOW)
        self.assertEqual(snapshot.agents, ())
        self.assertEqual(snapshot.requests, ())
        self.assertEqual(snapshot.usage.total, 120)

    def test_local_day_boundary_on_daylight_saving_transition(self):
        zone = ZoneInfo("Europe/Zurich")
        now = datetime(2026, 10, 25, 20, 0, tzinfo=zone)
        before = datetime(2026, 10, 24, 23, 59, tzinfo=zone)
        midnight = datetime(2026, 10, 25, 0, 0, tzinfo=zone)
        next_day = datetime(2026, 10, 26, 0, 0, tzinfo=zone)
        for id, stamp in (("before", before), ("today", midnight), ("tomorrow", next_day)):
            self.db.turn(id=id, completed_at=iso(stamp), work=work())
        self.assertEqual(self.reader.read(now).usage.total, 120)

    def test_read_is_consistent_during_a_writer_transaction_and_does_not_mutate(self):
        self.db.turn(work=work())
        self.db.c.execute("BEGIN IMMEDIATE")
        self.db.c.execute("UPDATE orchestration_v2_projection_provider_turns SET payload_json='{}'")
        self.assertEqual(self.reader.read(NOW).usage.total, 120)
        self.db.c.rollback()
        self.assertEqual(self.reader.read(NOW).usage.total, 120)

    def test_missing_database_is_never_created_and_bad_schema_is_explained(self):
        missing = self.db.path.parent / "missing.sqlite"
        with self.assertRaises(SourceError):
            T3Reader(missing, None).read(NOW)
        self.assertFalse(missing.exists())
        empty = self.db.path.parent / "empty.sqlite"
        sqlite3.connect(empty).close()
        with self.assertRaisesRegex(SourceError, "Unsupported T3 database"):
            T3Reader(empty, None).read(NOW)

    def test_missing_runtime_record_is_offline_not_idle(self):
        self.assertFalse(server_online(Path(self.temporary.name) / "server-runtime.json"))


if __name__ == "__main__":
    unittest.main()
