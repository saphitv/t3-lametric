import threading
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

from fixtures import NOW
from test_bridge import FakeDevice

from t3_lametric.activity import ActivityStream
from t3_lametric.bridge import Bridge, Ledger
from t3_lametric.config import Config
from t3_lametric.runtime import DisplayLoop
from t3_lametric.source import Agent, Completion, Request, Snapshot, Usage, iso
from t3_lametric.usage import UsageCache, UsageError


class RuntimeTests(unittest.TestCase):
    def test_reel_plays_from_cached_values_without_extra_t3_reads(self):
        self.bridge.config = replace(self.config, token_reel=True)
        self.device.supports_token_roll = True
        self.device.roll_tokens = Mock(return_value=True)
        self.usage_reader.read.return_value = Usage(input_tokens=1000, source="t3_usage")
        self.step(0)
        self.usage_reader.read.return_value = Usage(input_tokens=1900, source="t3_usage")
        self.assertEqual(self.step(5)["frames"][0]["text"], "1.9k")
        self.device.roll_tokens.assert_called_once_with("1.0k", "1.9k")
        self.step(5.25)
        self.assertEqual(self.usage_reader.read.call_count, 2)
        self.assertEqual(self.reader.read.call_count, 1)
        self.assertIsNone(self.bridge.counter.deadline)
        self.device.roll_tokens.assert_called_once()

    def setUp(self):
        self.time = 0.0
        self.config = Config(Path("db"), None, Path("state"), push_url="http://clock/widget")
        self.snapshot = Snapshot(
            NOW, True, (Agent("run", "chat", "codex", "running"),), (), (), Usage()
        )
        self.reader = Mock()
        self.reader.read.side_effect = lambda now: replace(self.snapshot, captured_at=now)
        self.activity = Mock(healthy=True)
        self.activity.changed.return_value = False
        self.device = FakeDevice()
        self.bridge = Bridge(
            self.config, self.device, Ledger(None, "test"), clock=lambda: self.time
        )
        self.usage_reader = Mock()
        self.usage_reader.read.return_value = Usage(input_tokens=100, source="t3_usage")
        self.usage = UsageCache(self.usage_reader, 5, clock=lambda: self.time)
        self.loop = DisplayLoop(
            self.reader, self.bridge, self.activity, self.usage, None, clock=lambda: self.time
        )

    def step(self, second, *, event=False):
        self.time = second
        self.activity.changed.return_value = event
        return self.loop.step(NOW + timedelta(seconds=second))

    def test_visible_tokens_refresh_every_five_seconds_without_reading_activity_again(self):
        self.step(0)
        self.assertEqual(self.loop.deadline(), 5)
        self.step(1, event=True)
        self.step(2, event=True)
        self.assertEqual(self.usage_reader.read.call_count, 1)
        self.step(5)
        self.assertEqual(self.usage_reader.read.call_count, 2)
        self.assertEqual(self.reader.read.call_count, 3)
        self.assertNotIn("N/A", [f["text"] for d in self.device.dashboards[2:] for f in d])
        self.assertEqual(self.loop.deadline(), 10)

    def test_completion_events_push_done_before_any_usage_call_and_pause_tokens_through_count(self):
        self.step(0)
        done = Completion("finished", "other", "codex", "completed", iso(NOW))
        self.snapshot = replace(self.snapshot, completions=(done,))
        self.usage_reader.read.side_effect = AssertionError(
            "Token read while completion/count is visible"
        )
        self.assertEqual(self.step(0.1, event=True)["state"], "done")
        self.assertEqual(self.loop.deadline(), 0.9)
        self.assertEqual(self.step(0.9)["state"], "count")
        self.assertEqual(self.step(2.4)["state"], "tokens")
        self.assertEqual(self.usage_reader.read.call_count, 1)
        self.assertEqual(self.device.dismissed, ["1", "2"])
        self.usage_reader.read.side_effect = None
        self.step(5)
        self.assertEqual(self.usage_reader.read.call_count, 2)

    def test_input_and_idle_have_no_token_reads_even_when_a_refresh_is_overdue(self):
        self.step(0)
        self.snapshot = replace(
            self.snapshot,
            requests=(Request("q", "other", "codex", "user_input", iso(NOW), "Question"),),
        )
        self.assertEqual(self.step(6, event=True)["state"], "input")
        self.step(20)
        self.snapshot = replace(self.snapshot, agents=(), requests=())
        self.assertEqual(self.step(21, event=True)["state"], "idle")
        self.step(40)
        self.assertEqual(self.usage_reader.read.call_count, 1)
        self.snapshot = replace(self.snapshot, agents=(Agent("new", "new", "codex", "running"),))
        self.assertEqual(self.step(41, event=True)["state"], "count")
        self.assertEqual(self.loop.deadline(), 42)
        self.assertEqual(self.usage_reader.read.call_count, 1)
        self.assertEqual(self.step(42)["state"], "tokens")
        self.assertEqual(self.usage_reader.read.call_count, 2)

    def test_input_arriving_during_count_pins_widget_and_pauses_usage_until_answered(self):
        self.step(0)
        self.snapshot = replace(
            self.snapshot,
            agents=self.snapshot.agents + (Agent("new", "new", "claude", "running"),),
        )
        self.assertEqual(self.step(0.1, event=True)["state"], "count")
        self.snapshot = replace(
            self.snapshot,
            requests=(Request("q", "other", "codex", "user_input", iso(NOW)),),
        )
        self.assertEqual(self.step(0.2, event=True)["state"], "count")
        self.assertEqual(self.device.dashboards[-1][0]["text"], "Input")
        for second in (1.1, 10, 70, 180):
            self.assertEqual(self.step(second)["state"], "input")
        self.assertEqual(self.usage_reader.read.call_count, 1)
        self.snapshot = replace(self.snapshot, requests=())
        self.assertEqual(self.step(181, event=True)["state"], "tokens")
        self.assertEqual(self.usage_reader.read.call_count, 2)

    def test_activity_reads_wait_until_done_has_had_its_full_interval(self):
        self.step(0)
        self.snapshot = replace(
            self.snapshot,
            completions=(Completion("finished", "other", "codex", "completed", iso(NOW)),),
        )
        self.assertEqual(self.step(0.1, event=True)["state"], "done")
        reads = self.reader.read.call_count
        self.assertEqual(self.step(0.2, event=True)["state"], "done")
        self.assertEqual(self.step(0.89)["state"], "done")
        self.assertEqual(self.reader.read.call_count, reads)
        self.assertTrue(self.loop.activity_dirty)
        self.assertEqual(self.loop.deadline(), 0.9)
        self.step(0.9)
        self.assertEqual(self.reader.read.call_count, reads + 1)
        self.assertFalse(self.loop.activity_dirty)

    def test_disconnected_stream_falls_back_to_five_second_activity_reads(self):
        self.activity.healthy = False
        self.step(0)
        self.step(5)
        self.assertEqual(self.reader.read.call_count, 2)

    def test_overflow_report_survives_a_second_render_when_the_usage_cache_resets(self):
        self.step(0)
        self.usage.day = NOW.date() - timedelta(days=1)
        self.snapshot = replace(
            self.snapshot,
            completions=tuple(
                Completion(str(i), "other", "codex", "completed", iso(NOW)) for i in range(10)
            ),
        )
        result = self.step(0.1, event=True)
        self.assertEqual(result["queued_events"], 5)
        self.assertEqual(result["dropped_events"], 4)
        self.assertEqual(self.usage_reader.read.call_count, 1)

    def test_one_second_usage_refresh_and_animation_do_not_repeat_activity_or_usage_reads(self):
        self.usage.interval = 1
        self.usage_reader.read.return_value = Usage(input_tokens=1000, source="t3_usage")
        self.step(0)
        self.usage_reader.read.return_value = Usage(input_tokens=1900, source="t3_usage")
        self.assertEqual(self.step(1)["frames"][0]["text"], "1.3k")
        self.assertAlmostEqual(self.loop.deadline(), 1.18)
        self.assertEqual(self.step(1.18)["frames"][0]["text"], "1.6k")
        self.assertEqual(self.step(1.36)["frames"][0]["text"], "1.9k")
        self.assertEqual(self.usage_reader.read.call_count, 2)
        self.assertEqual(self.reader.read.call_count, 1)
        self.assertEqual(self.loop.deadline(), 2)

    def test_completion_interrupts_an_unfinished_countup(self):
        self.usage.interval = 1
        self.usage_reader.read.return_value = Usage(input_tokens=1000, source="t3_usage")
        self.step(0)
        self.usage_reader.read.return_value = Usage(input_tokens=1900, source="t3_usage")
        self.step(1)
        self.snapshot = replace(
            self.snapshot,
            completions=(Completion("finished", "other", "codex", "completed", iso(NOW)),),
        )
        self.assertEqual(self.step(1.05, event=True)["state"], "done")
        self.assertIsNone(self.bridge.counter.deadline)
        self.assertEqual(self.usage_reader.read.call_count, 2)
        self.assertAlmostEqual(self.loop.deadline(), 1.85)

    def test_hidden_clock_app_skips_usage_until_visibility_check_reports_it_back(self):
        visibility = Mock(next_read=5)
        visibility.read.return_value = False
        self.loop.visibility = visibility
        self.step(0)
        self.assertEqual(self.usage_reader.read.call_count, 0)
        self.assertEqual(self.loop.deadline(), 5)
        visibility.read.return_value = True
        visibility.next_read = 10
        self.step(5)
        self.assertEqual(self.usage_reader.read.call_count, 1)
        self.snapshot = replace(
            self.snapshot, requests=(Request("q", "other", "codex", "user_input", iso(NOW)),)
        )
        self.step(6, event=True)
        self.assertEqual(visibility.read.call_count, 2)

    def test_device_failure_backs_off_instead_of_spinning_on_expired_heartbeat(self):
        self.step(0)
        self.device.offline = True
        result = self.step(61)
        self.assertTrue(result["errors"])
        self.assertEqual(self.loop.deadline(), 66)
        self.assertEqual(self.usage_reader.read.call_count, 1)

    def test_hidden_token_usage_resets_at_midnight_and_errors_back_off(self):
        self.usage.read(NOW, displayed=True)
        tomorrow = NOW + timedelta(days=1)
        self.assertFalse(self.usage.read(tomorrow, displayed=False).available)
        self.usage_reader.read.side_effect = UsageError("offline")
        self.usage.read(tomorrow, displayed=True)
        self.assertEqual(self.usage.error, "offline")
        self.usage.read(tomorrow, displayed=True)
        self.assertEqual(self.usage_reader.read.call_count, 2)


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.wakeup = threading.Event()
        self.stream = ActivityStream(Path("session"), Path("runtime"), self.wakeup)
        self.thread = {
            "id": "chat",
            "status": "running",
            "latestRunId": "run",
            "activeRunId": "run",
            "latestVisibleMessage": {"text": "private"},
        }
        self.stream._event({"kind": "snapshot", "snapshot": {"threads": [self.thread]}})
        self.stream.changed()
        self.wakeup.clear()

    def test_stream_filters_text_updates_but_wakes_immediately_on_completion_and_input(self):
        self.stream._event(
            {
                "kind": "thread.updated",
                "thread": {**self.thread, "latestVisibleMessage": {"text": "more text"}},
            }
        )
        self.assertFalse(self.stream.changed())
        self.stream._event(
            {
                "kind": "thread.updated",
                "thread": {
                    **self.thread,
                    "status": "completed",
                    "activeRunId": None,
                    "latestRunCompletedAt": iso(NOW),
                },
            }
        )
        self.assertTrue(self.wakeup.is_set())
        self.assertTrue(self.stream.changed())
        self.stream._event(
            {
                "kind": "thread.updated",
                "thread": {
                    **self.thread,
                    "pendingRuntimeRequest": {"id": "q", "kind": "user_input"},
                },
            }
        )
        self.assertTrue(self.stream.changed())
        self.assertNotIn("private", repr(self.stream.states))

    def test_enrichment_only_snapshots_preserve_activity_and_reconnect_snapshots_resync_it(self):
        self.stream._event(
            {
                "kind": "snapshot",
                "resolvedRepositoryIdentityRoots": ["project"],
                "snapshot": {"threads": []},
            }
        )
        self.assertIn("chat", self.stream.states)
        self.assertFalse(self.stream.changed())
        self.stream._event({"kind": "synchronized"})
        self.assertTrue(self.stream.healthy)
        self.assertTrue(self.stream.changed())
        self.stream._event({"kind": "snapshot", "snapshot": {"threads": []}})
        self.assertEqual(self.stream.states, {})
        self.assertTrue(self.stream.changed())


if __name__ == "__main__":
    unittest.main()
