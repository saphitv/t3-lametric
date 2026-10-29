import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

from fixtures import NOW
from test_bridge import FakeDevice

from t3_lametric.bridge import Bridge, Ledger
from t3_lametric.config import Config, ConfigError, RemoteConfig, load
from t3_lametric.display import frames, running_count
from t3_lametric.remote import (
    CombinedActivity,
    CombinedReader,
    CombinedUsage,
    RemoteActivity,
    input_message,
)
from t3_lametric.runtime import DisplayLoop
from t3_lametric.source import Agent, Snapshot, SourceError, Usage, iso
from t3_lametric.usage import UsageCache, UsageError, UsageInterrupted


def thread(id="chat", *, run="run", status="running", parent=None, relationship=None, request=None):
    return {
        "id": id,
        "latestRunId": run,
        "activeRunId": run if status == "running" else None,
        "status": status,
        "activityRunStatus": "running" if status == "running" else None,
        "latestRunCompletedAt": iso(NOW) if status in ("completed", "failed") else None,
        "providerInstanceId": "claude",
        "archivedAt": None,
        "lineage": {"relationshipToParent": relationship, "parentThreadId": parent},
        "pendingRuntimeRequest": request,
        "latestVisibleMessage": {"text": "private-message-must-not-be-retained"},
    }


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.config = RemoteConfig(
            "remote-env", "remote-workstation", "https://example.test", Path("remote-session")
        )
        self.remote = RemoteActivity(self.config, threading.Event())

    def seed(self, *threads):
        self.remote._event({"kind": "snapshot", "snapshot": {"threads": list(threads)}})
        self.remote._event({"kind": "synchronized"})

    def test_initial_snapshot_is_a_baseline_and_drops_message_bodies(self):
        self.seed(thread(), thread("old", status="completed"))
        snapshot = self.remote.snapshot(NOW)
        self.assertEqual(len(snapshot.agents), 1)
        self.assertEqual(snapshot.completions, ())
        self.assertNotIn("private-message", repr(self.remote.threads))
        self.assertEqual(snapshot.agents[0].thread_id, "remote-env:chat")

    def test_initial_remote_runs_are_quiet_but_later_runs_can_announce_their_start(self):
        self.seed(thread())
        self.assertFalse(self.remote.snapshot(NOW).agents[0].announce_start)
        self.remote._event({"kind": "thread.updated", "thread": thread(run="new-run")})
        self.assertTrue(self.remote.snapshot(NOW).agents[0].announce_start)

    def test_work_queued_at_connection_can_announce_when_it_actually_starts(self):
        queued = thread(run="queued-run", status="queued")
        queued["activeRunId"] = "queued-run"
        self.seed(queued)
        self.remote._event({"kind": "thread.updated", "thread": thread(run="queued-run")})
        self.assertTrue(self.remote.snapshot(NOW).agents[0].announce_start)

    def test_session_grouping_namespaces_children_but_preserves_forks(self):
        self.seed(
            thread(),
            thread("child", parent="chat", relationship="subagent"),
            thread("fork", parent="chat", relationship="fork"),
        )
        snapshot = self.remote.snapshot(NOW)
        self.assertEqual(running_count(snapshot), 2)
        self.assertEqual(
            {a.session_id for a in snapshot.agents}, {"remote-env:chat", "remote-env:fork"}
        )

    def test_live_completion_and_reconnect_catchup_are_delivered_once(self):
        self.seed(thread())
        self.remote._event({"kind": "thread.updated", "thread": thread(status="completed")})
        snapshot = self.remote.snapshot(NOW)
        self.assertEqual(len(snapshot.completions), 1)
        self.assertEqual(snapshot.completions[0].id, "remote-env:run")
        self.remote.healthy = False
        self.assertEqual(self.remote.snapshot(NOW).agents, ())
        self.assertEqual(self.remote.snapshot(NOW).completions, ())
        self.seed(thread(status="completed"))
        self.assertEqual(len(self.remote.snapshot(NOW).completions), 1)
        self.seed(thread(run="new", status="completed"))
        self.assertEqual(len(self.remote.snapshot(NOW).completions), 2)
        self.assertEqual(self.remote.snapshot(NOW + timedelta(minutes=3)).completions, ())

    def test_remote_questions_include_machine_name_and_dynamic_tools_stay_quiet(self):
        request = {"id": "ask", "kind": "user_input", "createdAt": iso(NOW)}
        self.seed(
            thread(request=request),
            thread("tool", request={"id": "internal", "kind": "dynamic_tool_call"}),
        )
        self.remote.messages["ask"] = "Which release?"
        snapshot = self.remote.snapshot(NOW)
        self.assertEqual(len(snapshot.requests), 1)
        self.assertEqual(snapshot.requests[0].id, "remote-env:ask")
        self.assertEqual(snapshot.requests[0].message, "remote-workstation: Which release?")

    def test_async_questions_do_not_remove_an_actively_working_session_from_the_count(self):
        self.seed(
            thread(
                request={
                    "id": "ask",
                    "kind": "user_input",
                    "blocking": False,
                    "createdAt": iso(NOW),
                }
            )
        )
        self.assertEqual(running_count(self.remote.snapshot(NOW)), 1)

    def test_old_async_requests_are_excluded_but_blocking_requests_and_live_questions_remain(self):
        request = {"id": "ask", "kind": "user_input", "blocking": False, "createdAt": iso(NOW)}
        self.seed(thread(request=request))
        self.assertEqual(len(self.remote.snapshot(NOW).requests), 1)
        for status in ("completed", "cancelled", "failed"):
            self.seed(thread(status=status, request=request))
            self.assertEqual(self.remote.snapshot(NOW).requests, ())
            self.assertEqual(self.remote._pending(), {})
        self.seed(thread(status="completed", request=dict(request, blocking=True)))
        self.assertEqual(len(self.remote.snapshot(NOW).requests), 1)

    def test_bounded_input_snapshot_extracts_only_the_requested_question_or_secret_label(self):
        projection = {
            "turnItems": [
                {
                    "type": "user_input_request",
                    "requestId": "ask",
                    "questions": [{"question": "Which release?"}],
                    "answers": {"secret": "private-answer"},
                },
                {
                    "type": "user_input_request",
                    "requestId": "other",
                    "questions": [{"question": "Unrelated"}],
                },
                {
                    "type": "secret_request",
                    "id": "key",
                    "label": "Service key",
                    "reason": "Connect it",
                    "value": "private-value",
                },
            ]
        }
        self.assertEqual(input_message(projection, "ask"), "Which release?")
        self.assertEqual(input_message(projection, "key"), "Service key: Connect it")
        self.assertEqual(input_message(projection, "unknown"), "")

    def test_wrong_server_identity_is_rejected_before_subscribing(self):
        connection = Mock()
        connection.recv.return_value = json.dumps(
            {
                "_tag": "Exit",
                "requestId": "identity",
                "exit": {"_tag": "Success", "value": {"environment": {"environmentId": "wrong"}}},
            }
        )
        with self.assertRaises(ValueError):
            self.remote._validate_connection(connection)
        sent = json.loads(connection.send.call_args.args[0])
        self.assertEqual(sent["tag"], "server.getConfig")

    def test_reader_combines_environments_without_id_collisions_or_offline_ghosts(self):
        self.seed(thread())
        local = Mock()
        local.read.return_value = Snapshot(
            NOW, True, (Agent("run", "chat", "codex", "running"),), (), (), Usage()
        )
        reader = CombinedReader(local, [self.remote])
        self.assertEqual(running_count(reader.read(NOW)), 2)
        self.remote.healthy = False
        self.assertEqual(running_count(reader.read(NOW)), 1)
        self.remote.healthy = True
        local.read.return_value = replace(local.read.return_value, online=False)
        self.assertEqual(running_count(reader.read(NOW)), 1)
        local.read.side_effect = SourceError("local unavailable")
        self.assertEqual(running_count(reader.read(NOW)), 1)
        self.assertEqual(reader.error, "local unavailable")

    def test_activity_group_consumes_all_sources_and_offline_peer_keeps_local_event_mode(self):
        local, remote = Mock(healthy=True), Mock(healthy=False)
        local.changed.return_value = remote.changed.return_value = True
        group = CombinedActivity(local, [remote])
        self.assertTrue(group.changed())
        local.changed.assert_called_once()
        remote.changed.assert_called_once()
        self.assertTrue(group.healthy)

    def test_remote_event_immediately_drives_native_done_then_remaining_local_count(self):
        self.seed(thread())
        local = Mock()
        local.read.side_effect = lambda now: Snapshot(
            now, True, (Agent("run", "chat", "codex", "running"),), (), (), Usage()
        )
        clock = [0.0]
        device = FakeDevice()
        bridge = Bridge(
            Config(Path("db"), None, Path("state"), push_url="http://clock/widget"),
            device,
            Ledger(None, "test"),
            clock=lambda: clock[0],
        )
        activity = CombinedActivity(
            Mock(healthy=True, changed=Mock(return_value=False)), [self.remote]
        )
        loop = DisplayLoop(
            CombinedReader(local, [self.remote]),
            bridge,
            activity,
            None,
            None,
            clock=lambda: clock[0],
        )
        loop.step(NOW)
        self.remote._event({"kind": "thread.updated", "thread": thread(status="completed")})
        clock[0] = 0.1
        self.assertEqual(loop.step(NOW)["state"], "done")
        self.assertEqual(device.popups[-1]["model"]["frames"][0]["text"], "Done")
        clock[0] = 0.9
        self.assertEqual(loop.step(NOW)["state"], "count")
        self.assertEqual(running_count(loop.snapshot), 1)


class CombinedUsageTests(unittest.TestCase):
    def setUp(self):
        self.local, self.client = Mock(), Mock()
        self.local.read.return_value = Usage(input_tokens=100, output_tokens=20, source="t3_usage")
        self.client.read.return_value = Usage(input_tokens=200, output_tokens=30, source="t3_usage")
        self.remote = Mock(healthy=True, config=Mock(environment_id="remote-env"))
        self.reader = CombinedUsage(self.local, [(self.remote, self.client)])

    def test_totals_add_once_per_environment_and_offline_preserves_last_known_daily_sum(self):
        self.assertEqual(self.reader.read(NOW).total, 350)
        self.remote.healthy = False
        result = self.reader.read(NOW)
        self.assertEqual(result.total, 350)
        self.assertTrue(result.incomplete)
        self.assertEqual(self.client.read.call_count, 1)
        self.assertFalse(
            frames(Snapshot(NOW, True, (Agent("r", "t", "c", "running"),), (), (), result))[0][
                "text"
            ].endswith("+")
        )

    def test_cost_combines_environments_and_clears_at_midnight(self):
        self.local.read.return_value.cost_usd = 1.25
        self.client.read.return_value.cost_usd = 2.50
        self.assertEqual(self.reader.read(NOW).cost_usd, 3.75)
        self.remote.healthy = False
        self.assertEqual(self.reader.read(NOW).cost_usd, 3.75)
        self.assertIsNone(self.reader.read(NOW + timedelta(days=1)).cost_usd)
        self.remote.healthy = True
        self.client.read.return_value.cost_usd = None
        self.assertIsNone(self.reader.read(NOW + timedelta(days=1)).cost_usd)

    def test_midnight_does_not_carry_over_yesterdays_offline_tokens(self):
        self.reader.read(NOW)
        self.remote.healthy = False
        result = self.reader.read(NOW + timedelta(days=1))
        self.assertEqual(result.total, 120)
        self.assertTrue(result.incomplete)

    def test_hidden_view_makes_no_usage_requests_and_activity_interrupts_all_readers(self):
        cache = UsageCache(self.reader)
        cache.read(NOW, displayed=False)
        self.local.read.assert_not_called()
        self.client.read.assert_not_called()
        self.reader.interrupt()
        self.local.interrupt.assert_called_once()
        self.client.interrupt.assert_called_once()
        self.client.read.side_effect = UsageInterrupted("activity changed")
        with self.assertRaises(UsageInterrupted):
            self.reader.read(NOW)

    def test_offline_local_server_does_not_block_remote_total(self):
        reader = CombinedUsage(self.local, [(self.remote, self.client)], local_online=lambda: False)
        result = reader.read(NOW)
        self.assertEqual(result.total, 230)
        self.local.read.assert_not_called()
        self.assertTrue(result.incomplete)

    def test_both_apis_failing_do_not_become_a_known_zero(self):
        self.local.read.side_effect = self.client.read.side_effect = UsageError("offline")
        with self.assertRaises(UsageError):
            self.reader.read(NOW)


class RemoteConfigTests(unittest.TestCase):
    def test_secure_unique_remotes_and_private_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            text = '[[remotes]]\nenvironment_id="remote"\nlabel="omy"\nurl="https://example.test"\nsession_file="secret.json"\n'
            path.write_text(text)
            config = load(path)
            self.assertEqual(
                config.remotes[0].session_file, (Path(temporary) / "secret.json").resolve()
            )
            for invalid in (
                text.replace("https://example.test", "http://public.test"),
                text.replace("https://example.test", "https://user:secret@example.test"),
                text + text,
                text.replace('session_file="secret.json"\n', ""),
            ):
                path.write_text(invalid)
                with self.subTest(invalid=invalid), self.assertRaises(ConfigError):
                    load(path)


if __name__ == "__main__":
    unittest.main()
