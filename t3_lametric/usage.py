"""Ask the same read-only usage RPC as T3's Usage page; no transcript scan here."""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path

import websocket

from .source import Usage, token_count


class UsageError(Exception):
    pass


class UsageInterrupted(UsageError):
    pass


def summarize(value: dict) -> Usage:
    if not isinstance(value.get("buckets"), list):
        raise UsageError("T3 returned an unsupported usage summary")
    result = Usage(source="t3_usage", read_at=value.get("readAt"), cost_usd=0.0)
    for bucket in value.get("buckets", []):
        totals = bucket.get("totals", {})
        counts = [
            token_count(totals.get(k))
            for k in (
                "uncachedInputTokens",
                "cachedInputTokens",
                "cacheCreationTokens",
                "outputTokens",
            )
        ]
        if any(n is None for n in counts):
            raise UsageError("T3 returned unsupported usage totals")
        incoming, outgoing = sum(counts[:3]), counts[3]
        result.input_tokens += incoming
        result.output_tokens += outgoing
        provider = bucket.get("provider", "unknown")
        result.by_provider[provider] = result.by_provider.get(provider, 0) + incoming + outgoing
        cost = bucket.get("costUsd")
        if (
            type(cost) not in (int, float)
            or not math.isfinite(cost)
            or cost < 0
            or bucket.get("costSource") == "unpriced"
            or bucket.get("unpricedRecords", 0) > 0
        ):
            result.cost_usd = None
        elif result.cost_usd is not None:
            result.cost_usd += cost
    return result


class T3Usage:
    def __init__(
        self,
        session_file: Path,
        runtime_file: Path | None,
        time_zone: str,
        *,
        origin: str | None = None,
    ):
        self.session_file, self.runtime_file, self.time_zone = session_file, runtime_file, time_zone
        self.connection = None
        self.origin = None
        self.fixed_origin = origin
        self.sequence = 0
        self.scan_duration_ms = None
        self.reading = threading.Event()
        self.cancelled = threading.Event()

    def interrupt(self):
        if self.reading.is_set():
            self.cancelled.set()
            connection = self.connection
            if connection is not None:
                try:
                    connection.abort()
                except (OSError, websocket.WebSocketException):
                    pass

    def close(self):
        if self.connection:
            self.connection.close()
        self.connection = None

    def read(self, now: datetime) -> Usage:
        self.cancelled.clear()
        self.reading.set()
        try:
            if self.fixed_origin:
                origin = self.fixed_origin
            else:
                runtime = json.loads(self.runtime_file.read_text())
                origin = f"http://127.0.0.1:{int(runtime['port'])}"
            if self.origin != origin:
                self.close()
            if self.connection is None:
                credential = json.loads(self.session_file.read_text())
                self.connection = websocket.create_connection(
                    origin.replace("https:", "wss:").replace("http:", "ws:")
                    + "/ws?orchestrationProtocol=2",
                    header={"Authorization": "Bearer " + credential["token"]},
                    origin=origin,
                    timeout=3,
                    http_no_proxy=["127.0.0.1"],
                )
                self.origin = origin
            if self.cancelled.is_set():
                raise UsageInterrupted("Activity changed during token refresh")
            self.sequence += 1
            request_id = str(self.sequence)
            day = now.strftime("%Y-%m-%d")
            self.connection.send(
                json.dumps(
                    {
                        "_tag": "Request",
                        "id": request_id,
                        "tag": "server.getUsageSummary",
                        "headers": [],
                        "payload": {"sinceDay": day, "untilDay": day, "timeZone": self.time_zone},
                    }
                )
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                raw = self.connection.recv()
                messages = json.loads(raw)
                for message in messages if isinstance(messages, list) else [messages]:
                    if message.get("_tag") == "Ping":
                        self.connection.send(json.dumps({"_tag": "Pong"}))
                    elif message.get("_tag") == "Exit" and message.get("requestId") == request_id:
                        result = message.get("exit", {})
                        if result.get("_tag") != "Success":
                            raise UsageError(
                                "T3 rejected the read-only usage request; check its session permissions."
                            )
                        value = result["value"]
                        self.scan_duration_ms = value.get("scanDurationMs")
                        return summarize(value)
            raise UsageError("T3 usage request timed out")
        except UsageError:
            self.close()
            raise
        except (OSError, ValueError, KeyError, TypeError, websocket.WebSocketException):
            self.close()
            if self.cancelled.is_set():
                raise UsageInterrupted("Activity changed during token refresh") from None
            raise UsageError(
                "Cannot refresh T3's usage total; keeping the last known total."
            ) from None
        finally:
            self.reading.clear()


class UsageCache:
    def __init__(self, reader: T3Usage, interval: float = 5, *, clock=time.monotonic):
        self.reader, self.interval, self.clock = reader, interval, clock
        self.value = Usage()
        self.day = None
        self.next_read = 0.0
        self.error: str | None = None

    def read(self, now: datetime, *, displayed: bool) -> Usage:
        if self.day != now.date():
            self.value, self.day = Usage(), now.date()
            self.next_read = 0.0
        if not displayed:
            self.reader.close()
            return self.value
        if self.clock() >= self.next_read:
            interrupted = False
            started = self.clock()
            try:
                self.value = self.reader.read(now)
                self.error = None
            except UsageInterrupted:
                interrupted = True
            except UsageError as error:
                self.error = str(error)
            finally:
                # Keep requests below roughly 10% of wall time, including relay
                # delays. A slow server backs off without delaying activity cues.
                elapsed = max(0, self.clock() - started)
                delay = max(self.interval, elapsed * 10, 5 if self.error else 0)
                self.next_read = self.clock() if interrupted else self.clock() + delay
        return self.value
