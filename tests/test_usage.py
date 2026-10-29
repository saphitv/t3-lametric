import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import websocket
from fixtures import NOW

from t3_lametric.activity import ActivityStream
from t3_lametric.usage import T3Usage, UsageCache, UsageError, summarize


def summary_value():
    return {
        "readAt": "2026-10-28T19:00:00Z",
        "buckets": [
            {
                "provider": "claude",
                "costUsd": 1.23,
                "totals": {
                    "uncachedInputTokens": 10,
                    "cachedInputTokens": 80,
                    "cacheCreationTokens": 10,
                    "outputTokens": 20,
                    "reasoningTokens": 15,
                },
            },
            {
                "provider": "codex",
                "costUsd": 4.56,
                "totals": {
                    "uncachedInputTokens": 5,
                    "cachedInputTokens": 20,
                    "cacheCreationTokens": 0,
                    "outputTokens": 10,
                    "reasoningTokens": 5,
                },
            },
        ],
        "scanDurationMs": 3.5,
    }


class Socket:
    def __init__(self):
        self.sent = []
        self.responses = []
        self.closed = False

    def send(self, text):
        message = json.loads(text)
        self.sent.append(message)
        if message["_tag"] == "Request":
            self.responses.extend(
                [
                    {"_tag": "Ping"},
                    {
                        "_tag": "Exit",
                        "requestId": message["id"],
                        "exit": {"_tag": "Success", "value": summary_value()},
                    },
                ]
            )

    def recv(self):
        return json.dumps(self.responses.pop(0))

    def close(self):
        self.closed = True


class UsageTests(unittest.TestCase):
    def test_slow_reads_and_failures_back_off_a_one_second_refresh(self):
        from unittest.mock import Mock

        current = [0.0]

        def slow_read(_now):
            current[0] += 0.25
            return summarize(summary_value())

        reader = Mock()
        reader.read.side_effect = slow_read
        cache = UsageCache(reader, interval=1, clock=lambda: current[0])
        cache.read(NOW, displayed=True)
        self.assertEqual(cache.next_read, 2.75)
        current[0] = 1
        cache.read(NOW, displayed=True)
        self.assertEqual(reader.read.call_count, 1)
        current[0] = 3
        reader.read.side_effect = UsageError("offline")
        cache.read(NOW, displayed=True)
        self.assertEqual(cache.next_read, 8)
        self.assertTrue(cache.value.available)

    def test_total_matches_t3_formula_without_counting_reasoning_twice(self):
        usage = summarize(summary_value())
        self.assertEqual(usage.total, 155)
        self.assertEqual(usage.by_provider, {"claude": 120, "codex": 35})
        self.assertEqual(usage.source, "t3_usage")
        self.assertFalse(usage.incomplete)
        self.assertAlmostEqual(usage.cost_usd, 5.79)

    def test_an_empty_usage_day_is_a_known_zero(self):
        usage = summarize({"buckets": []})
        self.assertEqual(usage.total, 0)
        self.assertTrue(usage.available)
        self.assertEqual(usage.cost_usd, 0)

    def test_missing_invalid_and_unpriced_costs_keep_tokens_without_guessing_cost(self):
        for cost in (None, -1, float("nan"), float("inf"), "1.23", True):
            with self.subTest(cost=cost):
                value = summary_value()
                value["buckets"][0]["costUsd"] = cost
                usage = summarize(value)
                self.assertEqual(usage.total, 155)
                self.assertIsNone(usage.cost_usd)
        for quality in ({"costSource": "unpriced"}, {"unpricedRecords": 1}):
            value = summary_value()
            value["buckets"][1].update(quality)
            self.assertIsNone(summarize(value).cost_usd)

    def test_invalid_totals_do_not_silently_turn_into_zero(self):
        value = summary_value()
        del value["buckets"][0]["totals"]["cachedInputTokens"]
        with self.assertRaises(UsageError):
            summarize(value)
        with self.assertRaises(UsageError):
            summarize({})

    def test_persistent_read_only_rpc_uses_local_day_and_handles_heartbeats(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session, runtime = root / "session.json", root / "runtime.json"
            session.write_text('{"token":"fixture-token"}')
            runtime.write_text('{"port":3773}')
            socket = Socket()
            with patch(
                "t3_lametric.usage.websocket.create_connection", return_value=socket
            ) as connect:
                reader = T3Usage(session, runtime, "Europe/Zurich")
                self.assertEqual(reader.read(NOW).total, 155)
                self.assertEqual(reader.read(NOW).total, 155)
                self.assertEqual(connect.call_count, 1)
                requests = [m for m in socket.sent if m["_tag"] == "Request"]
                self.assertTrue(all(m["tag"] == "server.getUsageSummary" for m in requests))
                self.assertEqual(
                    requests[0]["payload"],
                    {
                        "sinceDay": "2026-10-28",
                        "untilDay": "2026-10-28",
                        "timeZone": "Europe/Zurich",
                    },
                )
                self.assertEqual(len([m for m in socket.sent if m["_tag"] == "Pong"]), 2)
                reader.close()
                self.assertTrue(socket.closed)

    def test_activity_event_interrupts_an_inflight_token_read_without_logging_a_failure(self):
        class BlockingSocket(Socket):
            def __init__(self):
                super().__init__()
                self.entered = threading.Event()
                self.aborted = threading.Event()

            def recv(self):
                self.entered.set()
                self.aborted.wait(3)
                raise websocket.WebSocketConnectionClosedException("fixture-secret")

            def abort(self):
                self.aborted.set()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session, runtime = root / "session.json", root / "runtime.json"
            session.write_text('{"token":"fixture-secret"}')
            runtime.write_text('{"port":3773}')
            socket = BlockingSocket()
            reader = T3Usage(session, runtime, "Europe/Zurich")
            cache = UsageCache(reader, clock=lambda: 0)
            stream = ActivityStream(session, runtime, threading.Event(), interrupt=reader.interrupt)
            with patch("t3_lametric.usage.websocket.create_connection", return_value=socket):
                worker = threading.Thread(target=lambda: cache.read(NOW, displayed=True))
                worker.start()
                self.assertTrue(socket.entered.wait(1))
                stream._event(
                    {"kind": "thread.updated", "thread": {"id": "chat", "status": "completed"}}
                )
                worker.join(1)
                self.assertFalse(worker.is_alive())
                self.assertTrue(stream.changed())
                self.assertIsNone(cache.error)
                self.assertEqual(cache.next_read, 0)
                self.assertTrue(socket.closed)


if __name__ == "__main__":
    unittest.main()
