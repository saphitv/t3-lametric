import base64
import struct
import tempfile
import unittest
import zlib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

from fixtures import NOW

from t3_lametric.bridge import Bridge, Ledger, StateError, single_instance
from t3_lametric.config import Config
from t3_lametric.device import DeviceError
from t3_lametric.display import alerts, count_frame, frames, running_count
from t3_lametric.icons import GREEN, idle_dots, token_icon
from t3_lametric.source import Agent, Completion, Request, Snapshot, Usage, iso


def snapshot(**values):
    return replace(Snapshot(NOW, True, (), (), (), Usage()), **values)


def active(count=1):
    return tuple(Agent(str(i), str(i), "codex", "running") for i in range(count))


def icon_colors(icon):
    image = base64.b64decode(icon.split(",", 1)[1])
    if image.startswith(b"GIF"):
        size = 2 ** ((image[10] & 7) + 1)
        return {tuple(image[i : i + 3]) for i in range(13, 13 + size * 3, 3)}
    data, offset = b"", 8
    while offset < len(image):
        length = struct.unpack(">I", image[offset : offset + 4])[0]
        if image[offset + 4 : offset + 8] == b"IDAT":
            data += image[offset + 8 : offset + 8 + length]
        offset += length + 12
    pixels = zlib.decompress(data)
    return {
        tuple(pixels[y * 25 + 1 + x * 3 : y * 25 + 4 + x * 3]) for y in range(8) for x in range(8)
    }


class FakeDevice:
    def __init__(self):
        self.supports_token_roll = False
        self.popups = []
        self.dashboards = []
        self.dismissed = []
        self.offline = False
        self.timed_dismissals = []

    def stop_token_roll(self):
        pass

    def notify(self, payload):
        if self.offline:
            raise DeviceError("offline")
        self.popups.append(payload)
        return str(len(self.popups))

    def dismiss(self, identifier):
        if self.offline:
            raise DeviceError("offline")
        self.dismissed.append(identifier)

    def dashboard(self, payload):
        if self.offline:
            raise DeviceError("offline")
        self.dashboards.append(payload)

    def dismiss_after(self, identifier, seconds):
        expiry = Mock()
        expiry.cancel.return_value = False
        self.timed_dismissals.append((identifier, seconds, expiry))
        return expiry


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.config = Config(
            root / "t3.sqlite", None, root / "state.json", push_url="http://fake/widget"
        )
        self.ledger = Ledger(self.config.state_file, "test-t3")
        self.device = FakeDevice()
        self.time = 0.0
        self.bridge = Bridge(self.config, self.device, self.ledger, clock=lambda: self.time)
        self.done = Completion("run:1", "thread:1", "codex", "completed", iso(NOW))

    def test_completion_shows_green_done_then_blue_count_then_only_total(self):
        state = snapshot(
            agents=active(2), usage=Usage(input_tokens=1200, output_tokens=300, complete_turns=1)
        )
        self.bridge.tick(state)
        self.assertEqual(self.device.dashboards[-1][0]["text"], "1.5k")
        finished = replace(state, completions=(self.done,))
        result = self.bridge.tick(finished)
        self.assertEqual(result["frames"][0]["text"], "Done")
        self.assertEqual(self.device.popups[-1]["model"]["frames"][0]["text"], "Done")
        self.assertEqual(icon_colors(result["frames"][0]["icon"]), {(0, 0, 0), GREEN})
        self.assertEqual(self.bridge.next_deadline, 0.8)
        self.time = 0.79
        self.assertEqual(self.bridge.tick(finished, observe=False)["state"], "done")
        self.time = 0.8
        result = self.bridge.tick(finished, observe=False)
        self.assertEqual(result["state"], "count")
        self.assertIn((0, 128, 255), icon_colors(result["frames"][0]["icon"]))
        self.assertEqual(result["frames"][0]["text"], "2")
        self.time = 2.3
        result = self.bridge.tick(finished, observe=False)
        self.assertEqual(
            result["frames"], [{"text": "1.5k", "icon": token_icon(), "duration": 5000}]
        )
        self.assertIsNone(self.bridge.next_deadline)
        self.assertEqual(len(self.device.popups), 2)
        self.assertEqual(self.device.dismissed, ["1", "2"])

    def test_last_chat_finishing_skips_zero_count_and_returns_to_native_animation(self):
        self.bridge.tick(snapshot(agents=active()))
        finished = snapshot(completions=(self.done,))
        self.bridge.tick(finished)
        self.time = 0.8
        result = self.bridge.tick(finished, observe=False)
        self.assertEqual(result["frames"][0]["text"], "T3")
        self.assertTrue(result["frames"][0]["icon"].startswith("data:image/gif;base64,"))
        self.assertNotIn("0", [f["text"] for f in result["frames"]])

    def test_done_interval_is_protected_from_failure_input_new_work_and_disconnect(self):
        self.bridge.tick(snapshot(agents=active()))
        state = snapshot(agents=active(), completions=(self.done,))
        self.bridge.tick(state)
        self.assertEqual(self.device.timed_dismissals[0][:2], ("1", 0.8))
        self.assertEqual(self.device.popups[0]["model"]["frames"][0]["duration"], 1000)
        failed = replace(self.done, id="failure", status="failed")
        changed = snapshot(
            agents=active(2),
            completions=(self.done, failed),
            requests=(Request("q", "other", "codex", "user_input", iso(NOW)),),
        )
        for second, update in ((0.1, changed), (0.49, snapshot(online=False)), (0.79, changed)):
            self.time = second
            self.assertEqual(self.bridge.tick(update)["state"], "done")
            self.assertEqual(self.bridge.next_deadline, 0.8)
            self.assertEqual(self.device.dismissed, [])
            self.assertEqual(len(self.device.timed_dismissals), 1)
        self.time = 0.8
        self.assertEqual(self.bridge.tick(changed)["state"], "count")
        self.assertEqual(self.device.dismissed, ["1"])
        self.time = 1.8
        self.assertEqual(self.bridge.tick(changed)["state"], "failed")

    def test_fresh_completions_during_done_do_not_restart_or_extend_the_timer(self):
        self.bridge.tick(snapshot())
        self.bridge.tick(snapshot(completions=(self.done,)))
        for second in (0.2, 0.49, 0.79):
            self.time = second
            self.bridge.tick(snapshot(completions=(replace(self.done, id=str(second)),)))
            self.assertEqual(self.bridge.next_deadline, 0.8)
        self.assertEqual(len(self.device.popups), 1)
        self.assertEqual(len(self.device.timed_dismissals), 1)
        self.time = 0.8
        self.assertEqual(self.bridge.tick(snapshot())["state"], "done")
        self.assertEqual(self.device.dismissed, ["1"])
        self.assertEqual(len(self.device.timed_dismissals), 2)
        self.assertEqual(self.bridge.next_deadline, 1.6)

    def test_successful_independent_dismissal_is_not_sent_twice(self):
        self.bridge.tick(snapshot())
        self.bridge.tick(snapshot(completions=(self.done,)))
        self.device.timed_dismissals[0][2].cancel.return_value = True
        self.time = 0.8
        self.bridge.tick(snapshot())
        self.assertIsNone(self.bridge.cue_id)
        self.assertEqual(self.device.dismissed, [])

    def test_new_chat_shows_working_for_one_second_then_total_and_new_turn_retriggers(self):
        self.bridge.tick(snapshot())
        self.time = 0.1
        state = snapshot(agents=active(), usage=Usage(input_tokens=1500, source="t3_usage"))
        result = self.bridge.tick(state)
        self.assertEqual(result["state"], "count")
        self.assertEqual(result["frames"][0]["text"], "1")
        self.assertTrue(result["frames"][0]["icon"].startswith("data:image/gif;base64,"))
        self.assertAlmostEqual(self.bridge.next_deadline, 1.1)
        self.time = 0.9
        self.bridge.tick(state)
        self.assertEqual(len(self.device.popups), 1)
        self.time = 1.1
        self.assertEqual(self.bridge.tick(state)["frames"][0]["text"], "1.5k")
        self.assertEqual(self.device.dismissed, ["1"])
        self.time = 2
        self.assertEqual(self.bridge.tick(state)["state"], "tokens")
        new_turn = replace(state, agents=(replace(state.agents[0], id="new-turn"),))
        self.assertEqual(self.bridge.tick(new_turn)["state"], "count")
        self.assertEqual(len(self.device.popups), 2)

    def test_queued_work_waits_until_running_and_delegates_do_not_trigger_working(self):
        self.bridge.tick(snapshot())
        queued = Agent("run", "chat", "codex", "queued", session_id="chat")
        self.assertEqual(self.bridge.tick(snapshot(agents=(queued,)))["state"], "idle")
        parent = replace(queued, status="running")
        self.assertEqual(self.bridge.tick(snapshot(agents=(parent,)))["state"], "count")
        self.time = 1
        self.bridge.tick(snapshot(agents=(parent,)))
        child = Agent("delegate", "child", "claude", "running", True, session_id="chat")
        self.assertEqual(self.bridge.tick(snapshot(agents=(parent, child)))["state"], "tokens")
        self.assertEqual(len(self.device.popups), 1)

    def test_burst_updates_the_visible_count_and_queues_new_starts_and_completions(self):
        self.bridge.tick(snapshot())
        self.bridge.tick(snapshot(agents=active()))
        original_deadline = self.bridge.next_deadline
        self.time = 0.2
        self.bridge.tick(snapshot(agents=active(2)))
        self.assertEqual(len(self.device.popups), 2)
        self.assertEqual(self.device.dismissed, ["1"])
        self.assertEqual(self.bridge.next_deadline, original_deadline)
        self.assertEqual(
            self.device.popups[-1]["model"]["frames"],
            [count_frame(snapshot(agents=active(2)))],
        )
        self.time = 0.3
        state = snapshot(agents=active(2), completions=(self.done,))
        self.assertEqual(self.bridge.tick(state)["state"], "count")
        self.assertEqual(len(self.bridge.events), 2)
        self.assertEqual(self.bridge.next_deadline, original_deadline)
        self.time = 1
        self.assertEqual(self.bridge.tick(state)["state"], "count")
        self.time = 2
        self.assertEqual(self.bridge.tick(state)["state"], "done")
        self.assertAlmostEqual(self.bridge.next_deadline, 2.8)

    def test_existing_runs_do_not_flash_on_startup_or_when_the_connection_returns(self):
        self.assertEqual(self.bridge.tick(snapshot(agents=active()))["state"], "tokens")
        self.bridge.tick(snapshot(online=False))
        self.assertEqual(self.bridge.tick(snapshot(agents=active()))["state"], "tokens")
        self.assertEqual(self.device.popups, [])

    def test_a_remote_initial_snapshot_does_not_fill_the_event_queue_with_old_starts(self):
        self.bridge.tick(snapshot())
        remote = Agent("remote:old", "remote:chat", "codex", "running", announce_start=False)
        self.assertEqual(self.bridge.tick(snapshot(agents=(remote,)))["state"], "tokens")
        self.assertFalse(self.bridge.events)
        new = replace(remote, id="remote:new", announce_start=True)
        self.assertEqual(self.bridge.tick(snapshot(agents=(new,)))["state"], "count")

    def test_completion_briefly_interrupts_input_then_the_generic_indicator_resumes(self):
        self.bridge.tick(snapshot(agents=active()))
        request = Request(
            "ask", "0", "codex", "user_input", iso(NOW), "Which release should I deploy?"
        )
        state = snapshot(agents=active(), requests=(request,), completions=(self.done,))
        self.assertEqual(self.bridge.tick(state)["state"], "done")
        self.time = 0.8
        self.assertEqual(self.bridge.tick(state, observe=False)["state"], "input")
        self.assertEqual(self.device.dashboards[-1][0]["text"], "Input")
        self.time = 10
        self.bridge.tick(state)
        self.assertEqual(len(self.device.dashboards), 2)
        answered = replace(state, requests=())
        self.assertEqual(self.bridge.tick(answered)["state"], "tokens")

    def test_input_replaces_backing_stats_during_events_until_every_request_is_resolved(self):
        state = snapshot(agents=active(2), usage=Usage(input_tokens=1500, source="t3_usage"))
        self.bridge.tick(state)
        requests = (
            Request("first", "0", "codex", "user_input", iso(NOW)),
            Request("second", "other", "claude", "user_input", iso(NOW)),
        )
        pending = replace(state, requests=requests, completions=(self.done,))
        self.assertEqual(self.bridge.tick(pending)["state"], "done")
        # Native playback can end before the next bridge tick. Its backing
        # widget must already say Input, never the previous token total.
        self.assertEqual(self.device.dashboards[-1][0]["text"], "Input")
        self.time = 0.8
        self.assertEqual(self.bridge.tick(pending)["state"], "count")
        self.assertEqual(self.device.dashboards[-1][0]["text"], "Input")
        self.time = 1.8
        self.assertEqual(self.bridge.tick(pending)["state"], "input")
        pending = replace(pending, requests=requests[1:])
        for second in (2, 30, 90, 180):
            self.time = second
            self.assertEqual(self.bridge.tick(pending)["state"], "input")
            self.assertEqual(self.device.dashboards[-1][0]["text"], "Input")
        self.assertEqual(
            [dashboard[0]["text"] for dashboard in self.device.dashboards[1:]],
            ["Input"] * (len(self.device.dashboards) - 1),
        )
        answered = replace(pending, requests=())
        self.assertEqual(self.bridge.tick(answered)["state"], "tokens")
        self.assertEqual(self.device.dashboards[-1][0]["text"], "1.5k")

    def test_questions_and_approvals_share_one_indicator_and_never_send_their_text(self):
        requests = (
            Request("q", "local", "codex", "user_input", iso(NOW), "PRIVATE QUESTION"),
            Request("a", "omy", "claude", "command", iso(NOW), "PRIVATE APPROVAL"),
        )
        result = self.bridge.tick(snapshot(requests=requests))
        self.assertEqual(len(result["frames"]), 1)
        self.assertEqual(result["frames"][0]["text"], "Input")
        self.assertNotIn("PRIVATE", repr(self.device.dashboards))
        # Notification-only installs must use the same generic wording.
        self.bridge.config = replace(self.config, push_url="")
        self.bridge.ledger = Ledger(None, "notification-only")
        self.bridge.tick(snapshot(requests=requests))
        self.assertEqual(self.device.popups[-1]["model"]["frames"][0]["text"], "Input")
        self.assertNotIn("PRIVATE", repr(self.device.popups))

    def test_old_completions_never_replay_on_start_or_after_restart(self):
        self.bridge.tick(snapshot(completions=(self.done,)))
        self.assertNotEqual(self.device.dashboards[-1][0]["text"], "Done")
        second = replace(self.done, id="run:2")
        self.bridge.tick(snapshot(completions=(self.done, second)))
        self.assertEqual(self.device.popups[-1]["model"]["frames"][0]["text"], "Done")
        restarted = Bridge(self.config, self.device, Ledger(self.config.state_file, "test-t3"))
        self.assertNotEqual(restarted.tick(snapshot(completions=(second,)))["state"], "done")

    def test_offline_delivery_retries_without_losing_the_completion(self):
        self.bridge.tick(snapshot())
        self.device.offline = True
        state = snapshot(completions=(self.done,))
        self.assertTrue(self.bridge.tick(state)["errors"])
        self.assertIsNone(self.bridge.next_deadline)
        self.device.offline = False
        self.assertEqual(self.bridge.tick(state)["state"], "done")
        self.assertEqual(self.bridge.next_deadline, 0.8)

    def test_completion_that_expires_while_device_offline_is_dropped(self):
        self.bridge.tick(snapshot())
        self.device.offline = True
        self.bridge.tick(snapshot(completions=(self.done,)))
        self.device.offline = False
        result = self.bridge.tick(
            snapshot(captured_at=NOW + timedelta(minutes=5), completions=(self.done,))
        )
        self.assertNotEqual(result["state"], "done")

    def test_a_burst_plays_each_completion_once_with_its_own_full_interval(self):
        self.bridge.tick(snapshot())
        state = snapshot(completions=tuple(replace(self.done, id=str(i)) for i in range(5)))
        self.bridge.tick(state)
        self.assertEqual(len(self.device.popups), 1)
        self.assertEqual(len(self.bridge.events), 4)
        for index in range(2, 6):
            self.time = self.bridge.next_deadline
            self.assertEqual(self.bridge.tick(state, observe=False)["state"], "done")
            self.assertEqual(len(self.device.popups), index)
            self.assertAlmostEqual(self.bridge.next_deadline - self.time, 0.8)
        self.time = self.bridge.next_deadline
        self.assertEqual(self.bridge.tick(state)["state"], "idle")
        self.assertEqual(self.device.dismissed, ["1", "2", "3", "4", "5"])

    def test_queue_holds_five_waiting_events_and_rejects_overflow_permanently(self):
        self.bridge.tick(snapshot())
        state = snapshot(
            completions=tuple(replace(self.done, id=f"event-{i:02}") for i in range(10))
        )
        result = self.bridge.tick(state)
        self.assertEqual(result["queued_events"], 5)
        self.assertEqual(result["dropped_events"], 4)
        self.assertIn("event-00", self.bridge.current_event.key)
        played = []
        for index in range(6):
            played.append(self.bridge.current_event.key)
            self.assertIn(f"event-{index:02}", played[-1])
            self.assertLessEqual(len(self.bridge.events), 5)
            self.time = self.bridge.next_deadline
            self.bridge.tick(state)
        self.assertIsNone(self.bridge.current_event)
        self.assertEqual(len(self.device.popups), 6)
        self.assertFalse(self.bridge.events)
        # Re-reading the same history cannot refill newly freed slots.
        result = self.bridge.tick(state)
        self.assertEqual(result["state"], "idle")
        self.assertEqual(result["dropped_events"], 0)
        self.assertEqual(len(self.device.popups), 6)
        restarted = Bridge(self.config, self.device, Ledger(self.config.state_file, "test-t3"))
        self.assertEqual(restarted.tick(state)["state"], "idle")

    def test_queue_is_fifo_across_start_done_and_failure_events(self):
        self.bridge.tick(snapshot())
        self.bridge.tick(snapshot(agents=active()))
        done = replace(self.done, completed_at=iso(NOW + timedelta(milliseconds=100)))
        self.time = 0.1
        self.bridge.tick(snapshot(completions=(done,)))
        failed = replace(
            done, id="failed", status="failed", completed_at=iso(NOW + timedelta(milliseconds=200))
        )
        self.time = 0.2
        self.bridge.tick(snapshot(completions=(done, failed)))
        self.assertEqual([event.kind for event in self.bridge.events], ["done", "failed"])
        self.assertEqual(self.device.popups[-1]["model"]["frames"][0]["text"], "1")
        self.time = 1
        self.assertEqual(self.bridge.tick(snapshot())["state"], "done")
        self.time = self.bridge.next_deadline
        self.assertEqual(self.bridge.tick(snapshot())["state"], "failed")

    def test_undelivered_event_is_not_acknowledged_while_t3_is_offline(self):
        self.bridge.tick(snapshot())
        self.device.offline = True
        state = snapshot(completions=(self.done,))
        self.bridge.tick(state)
        self.device.offline = False
        self.assertEqual(self.bridge.tick(snapshot(online=False))["state"], "offline")
        self.assertNotIn(self.bridge.current_event.key, self.ledger.seen)
        self.assertIsNone(self.bridge.next_deadline)
        self.assertEqual(self.bridge.tick(state)["state"], "done")

    def test_answered_input_is_not_replayed_from_the_event_queue(self):
        self.bridge.tick(snapshot(agents=active()))
        pending = snapshot(
            agents=active(),
            completions=(self.done,),
            requests=(Request("q", "other", "codex", "user_input", iso(NOW)),),
        )
        self.bridge.tick(pending)
        self.assertFalse(self.bridge.events)
        self.time = self.bridge.next_deadline
        answered = replace(pending, requests=())
        self.assertEqual(self.bridge.tick(answered)["state"], "count")
        self.time = self.bridge.next_deadline
        self.assertEqual(self.bridge.tick(answered)["state"], "tokens")

    def test_idle_animation_is_uploaded_once_and_does_not_restart_every_five_seconds(self):
        self.bridge.tick(snapshot())
        for now in range(5, 60, 5):
            self.time = now
            self.bridge.tick(snapshot(captured_at=NOW + timedelta(seconds=now)))
        self.assertEqual(len(self.device.dashboards), 1)
        self.assertEqual(idle_dots(), idle_dots())

    def test_changed_token_total_refreshes_without_provider_output_or_context_frames(self):
        state = snapshot(
            agents=active(),
            usage=Usage(input_tokens=1_000_000_000, output_tokens=10, source="t3_usage"),
        )
        self.bridge.tick(state)
        self.time = 5
        self.bridge.tick(replace(state, usage=replace(state.usage, input_tokens=2_000_000_000)))
        self.time = 5.5
        self.bridge.tick(
            replace(state, usage=replace(state.usage, input_tokens=2_000_000_000)), observe=False
        )
        self.assertEqual(
            self.device.dashboards[-1], [{"text": "2.0B", "icon": token_icon(), "duration": 5000}]
        )

    def test_count_is_sessions_not_provider_subagents_and_excludes_queued_or_input_blocked(self):
        parent = Agent("main", "main", "codex", "running", session_id="main")
        child = Agent("child", "child", "claude", "running", True, session_id="main")
        queued = Agent("next", "next", "codex", "queued")
        state = snapshot(agents=(parent, child, queued))
        self.assertEqual(running_count(state), 1)
        blocked = Request("q", "main", "codex", "user_input", iso(NOW))
        self.assertEqual(
            running_count(replace(state, agents=(parent, queued), requests=(blocked,))), 0
        )
        self.assertIsNone(count_frame(snapshot()))

    def test_offline_snapshot_never_consumes_first_start_baseline(self):
        self.bridge.tick(snapshot(online=False))
        self.assertNotEqual(self.bridge.tick(snapshot(completions=(self.done,)))["state"], "done")
        self.assertEqual(frames(snapshot(online=False))[0]["text"], "T3 OFFLINE")

    def test_only_one_live_process_can_use_a_ledger(self):
        with single_instance(self.config.state_file), self.assertRaises(StateError):
            with single_instance(self.config.state_file):
                pass

    def test_dry_ledger_does_not_write(self):
        Bridge(self.config, None, Ledger(None, "test")).tick(snapshot())
        self.assertFalse(self.config.state_file.exists())

    def test_subagent_completion_is_quiet(self):
        self.assertEqual(alerts(snapshot(completions=(replace(self.done, subagent=True),))), [])

    def test_disabled_completion_cannot_replay_after_enabling(self):
        disabled = Bridge(replace(self.config, notify_completed=False), self.device, self.ledger)
        disabled.tick(snapshot())
        disabled.tick(snapshot(completions=(self.done,)))
        self.assertNotEqual(self.bridge.tick(snapshot(completions=(self.done,)))["state"], "done")


if __name__ == "__main__":
    unittest.main()
