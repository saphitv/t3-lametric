import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from fixtures import NOW

from t3_lametric.bridge import Bridge, Ledger
from t3_lametric.config import Config, WeeklyAccount
from t3_lametric.display import frames
from t3_lametric.icons import token_icon
from t3_lametric.source import Agent, Request, Snapshot, Usage, iso
from t3_lametric.weekly import (
    T3Weekly,
    WeeklyCache,
    WeeklyError,
    WeeklyInterrupted,
    quota,
    remaining,
)


def limits(used=27, *, checked=NOW, reset=None):
    return {
        "checkedAt": iso(checked),
        "windows": [
            {"id": "five_hour", "kind": "session", "usedPercent": 99},
            {
                "id": "seven_day_fable",
                "kind": "weekly",
                "label": "Weekly · Fable",
                "usedPercent": 90,
            },
            {
                "id": "seven_day",
                "kind": "weekly",
                "usedPercent": used,
                "resetsAt": iso(reset or NOW + timedelta(days=2)),
            },
        ],
    }


class WeeklyTests(unittest.TestCase):
    def test_activity_interruption_does_not_delay_weekly_refresh_by_five_minutes(self):
        account = WeeklyAccount("CL", source_id="hub", account_id="claude")
        value = quota(account, limits(), 1800)
        reader = Mock(accounts=(account,))
        reader.read.side_effect = [WeeklyInterrupted("activity changed"), (value,)]
        cache = WeeklyCache(reader, clock=lambda: 0)
        cache.read()
        self.assertIsNone(cache.error)
        self.assertEqual(cache.next_read, 0)
        self.assertEqual(cache.read(), (value,))

    def test_only_overall_weekly_window_is_used(self):
        reading = quota(WeeklyAccount("CL", instance_id="claude"), limits(), 1800)
        self.assertEqual(remaining(reading, NOW), 73)
        self.assertEqual(reading.used_percent, 27)
        scoped_only = limits()
        scoped_only["windows"].pop()
        self.assertIsNone(quota(WeeklyAccount("CL"), scoped_only, 1800).used_percent)

    def test_stale_or_reset_windows_are_unknown_without_inventing_a_fresh_allowance(self):
        account = WeeklyAccount("CP", instance_id="personal")
        stale = quota(account, limits(checked=NOW - timedelta(minutes=30)), 1800)
        reset = quota(account, limits(reset=NOW + timedelta(seconds=5)), 1800)
        future = quota(account, limits(checked=NOW + timedelta(minutes=10)), 1800)
        self.assertIsNone(remaining(stale, NOW))
        self.assertEqual(remaining(reset, NOW), 73)
        self.assertIsNone(remaining(reset, NOW + timedelta(seconds=5)))
        self.assertIsNone(remaining(future, NOW))

    def test_missing_invalid_or_unsupported_values_never_look_like_zero_usage(self):
        account = WeeklyAccount("CL")
        for data in (
            {},
            {"unavailable": {"reason": "unsupported"}},
            *[limits(n) for n in (True, -1, 101, float("nan"), "27")],
        ):
            with self.subTest(data=data):
                self.assertIsNone(remaining(quota(account, data, 1800), NOW))
        self.assertEqual(remaining(quota(account, limits(0), 1800), NOW), 100)
        self.assertEqual(remaining(quota(account, limits(100), 1800), NOW), 0)
        self.assertEqual(remaining(quota(account, limits(0.1), 1800), NOW), 99)

    def test_exact_account_selection_ignores_other_codex_accounts_and_hub_duplicates(self):
        accounts = (
            WeeklyAccount("CP", instance_id="personal"),
            WeeklyAccount("CW", instance_id="work"),
            WeeklyAccount("CL", source_id="hub", account_id="claude-account"),
        )
        reader = T3Weekly(Path("session"), Path("runtime"), accounts)
        providers = [
            {"instanceId": name, "enabled": True, "usageLimits": limits(value)}
            for name, value in (("personal", 0), ("work", 18), ("other", 60))
        ]
        sources = [
            {
                "id": "hub",
                "accounts": [
                    {"id": "claude-account", "usageLimits": limits(27)},
                    {"id": "codex-duplicate", "usageLimits": limits(99)},
                ],
            }
        ]
        selected = reader._select(providers, sources)
        self.assertEqual(
            [(q.label, remaining(q, NOW)) for q in selected], [("CP", 100), ("CW", 82), ("CL", 73)]
        )

    def test_missing_or_disabled_accounts_keep_their_labels_and_show_unknown(self):
        reader = T3Weekly(
            Path("session"),
            Path("runtime"),
            (
                WeeklyAccount("CP", instance_id="personal"),
                WeeklyAccount("CL", source_id="hub", account_id="claude"),
            ),
        )
        values = reader._select(
            [{"instanceId": "personal", "enabled": False, "usageLimits": limits(0)}],
            [{"id": "hub", "error": "offline", "accounts": []}],
        )
        self.assertEqual([q.label for q in values], ["CP", "CL"])
        self.assertTrue(all(remaining(q, NOW) is None for q in values))

    def test_cached_reads_back_off_after_failure_and_skip_refresh_while_busy(self):
        account = WeeklyAccount("CL", source_id="hub", account_id="claude")
        value = quota(account, limits(), 1800)
        reader = Mock(accounts=(account,))
        reader.read.side_effect = [(value,), WeeklyError("offline"), (value,)]
        now = [0]
        cache = WeeklyCache(reader, 300, clock=lambda: now[0])
        cache.read(refresh=False)
        reader.read.assert_not_called()
        self.assertEqual(cache.read(), (value,))
        for second in range(5, 300, 5):
            now[0] = second
            self.assertEqual(cache.read(), (value,))
        self.assertEqual(reader.read.call_count, 1)
        now[0] = 300
        self.assertEqual(cache.read(), (value,))
        self.assertEqual(cache.error, "offline")
        now[0] = 305
        self.assertEqual(cache.read(), (value,))
        self.assertEqual(reader.read.call_count, 2)
        now[0] = 600
        cache.read(refresh=False)
        self.assertEqual(reader.read.call_count, 2)
        cache.read()
        self.assertIsNone(cache.error)

    def test_sleep_cycle_is_mostly_dots_and_live_activity_or_input_preempts_it(self):
        readings = tuple(
            quota(WeeklyAccount(label), limits(used), 1800)
            for label, used in (("CP", 0), ("CW", 18), ("CL", 27))
        )
        state = Snapshot(NOW, True, (), (), (), Usage(source="t3_usage"), weekly=readings)
        sleeping = frames(state)
        self.assertEqual(
            [f["text"] for f in sleeping], ["T3", "CP100%", "T3", "CW82%", "T3", "CL73%"]
        )
        self.assertEqual(sum(f["duration"] for f in sleeping), 30000)
        self.assertTrue(all(f["duration"] <= 10000 for f in sleeping))
        self.assertTrue(all(f["icon"].startswith("data:image/gif;base64,") for f in sleeping))
        active = replace(state, agents=(Agent("run", "chat", "codex", "running"),))
        self.assertEqual(frames(active), [{"text": "0.0", "icon": token_icon(), "duration": 5000}])
        pending = replace(
            state,
            requests=(
                Request("request", "chat", "codex", "user_input", iso(NOW), "Choose a release"),
            ),
        )
        self.assertEqual([f["text"] for f in frames(pending)], ["Input"])
        self.assertEqual([f["text"] for f in frames(replace(state, online=False))], ["T3 OFFLINE"])
        stale = frames(replace(state, captured_at=NOW + timedelta(hours=1)))
        self.assertEqual([f["text"] for f in stale][1::2], ["CP ?", "CW ?", "CL ?"])

    def test_sleep_cycle_does_not_cause_five_second_clock_uploads(self):
        reading = quota(WeeklyAccount("CP"), limits(0), 1800)
        state = Snapshot(NOW, True, (), (), (), Usage(), weekly=(reading,))
        now = [0]
        device = Mock()
        bridge = Bridge(
            Config(Path("db"), None, Path("state"), push_url="http://clock/widget"),
            device,
            Ledger(None, "test"),
            clock=lambda: now[0],
        )
        for second in range(0, 60, 5):
            now[0] = second
            bridge.tick(replace(state, captured_at=NOW + timedelta(seconds=second)))
        self.assertEqual(device.dashboard.call_count, 1)

    def test_read_only_stream_acknowledges_snapshot_and_reads_hub_then_closes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session, runtime = root / "session.json", root / "runtime.json"
            session.write_text('{"token":"fixture-secret"}')
            runtime.write_text('{"port":3773}')
            reader = T3Weekly(
                session, runtime, (WeeklyAccount("CL", source_id="hub", account_id="claude"),)
            )
            socket = Mock()
            socket.recv.side_effect = [
                json.dumps({"_tag": "Ping"}),
                json.dumps(
                    {
                        "_tag": "Chunk",
                        "requestId": "weekly",
                        "values": [{"type": "snapshot", "config": {"providers": []}}],
                    }
                ),
                json.dumps(
                    [
                        {
                            "_tag": "Chunk",
                            "requestId": "weekly",
                            "values": [
                                {
                                    "type": "usageLimitSourcesUpdated",
                                    "payload": {
                                        "sources": [
                                            {
                                                "id": "hub",
                                                "accounts": [
                                                    {"id": "claude", "usageLimits": limits()}
                                                ],
                                            }
                                        ]
                                    },
                                }
                            ],
                        }
                    ]
                ),
            ]
            with patch("t3_lametric.weekly.websocket.create_connection", return_value=socket):
                self.assertEqual(remaining(reader.read()[0], NOW), 73)
            sent = [json.loads(c.args[0]) for c in socket.send.call_args_list]
            self.assertEqual(
                [m["tag"] for m in sent if m["_tag"] == "Request"], ["subscribeServerConfig"]
            )
            self.assertEqual(len([m for m in sent if m["_tag"] == "Ack"]), 2)
            self.assertTrue(any(m["_tag"] == "Pong" for m in sent))
            socket.close.assert_called_once()
            socket.recv.side_effect = ValueError("fixture-secret")
            with (
                patch("t3_lametric.weekly.websocket.create_connection", return_value=socket),
                self.assertRaises(WeeklyError) as error,
            ):
                reader.read()
            self.assertNotIn("fixture-secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
