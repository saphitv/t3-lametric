"""Read T3's published limits, including Hub accounts; never probe a provider."""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import websocket

from .config import WeeklyAccount
from .source import WeeklyQuota, iso, parse_time


class WeeklyError(Exception):
    pass


class WeeklyInterrupted(WeeklyError):
    pass


def quota(account: WeeklyAccount, limits: dict, max_age_seconds: float) -> WeeklyQuota:
    empty = WeeklyQuota(account.label)
    if not isinstance(limits, dict) or limits.get("unavailable"):
        return empty
    windows = limits.get("windows", [])
    if not isinstance(windows, list):
        return empty
    # Claude's model-specific weekly windows overlap the overall allowance.
    # Only use the overall window; never add windows or substitute a model cap.
    window = next(
        (
            w
            for w in windows
            if isinstance(w, dict)
            and w.get("kind") == "weekly"
            and (
                w.get("id") in ("seven_day", "primary", "secondary")
                or w.get("label", "").casefold() == "weekly"
            )
        ),
        None,
    )
    if window is None:
        return empty
    used = window.get("usedPercent")
    checked = parse_time(limits.get("checkedAt"))
    reset = parse_time(window.get("resetsAt"))
    if (
        type(used) not in (int, float)
        or not math.isfinite(used)
        or not 0 <= used <= 100
        or checked is None
    ):
        return empty
    expiry = checked + timedelta(seconds=max_age_seconds)
    if reset is not None:
        expiry = min(expiry, reset)
    return WeeklyQuota(
        account.label, used, iso(checked), iso(reset) if reset else None, iso(expiry)
    )


def remaining(quota: WeeklyQuota, now: datetime) -> int | None:
    expiry, checked = parse_time(quota.expires_at), parse_time(quota.checked_at)
    if quota.used_percent is None or expiry is None or checked is None:
        return None
    if now >= expiry or checked > now + timedelta(minutes=5):
        return None
    return max(0, min(100, math.floor(100 - quota.used_percent + 1e-9)))


class T3Weekly:
    def __init__(
        self,
        session_file: Path,
        runtime_file: Path,
        accounts: tuple[WeeklyAccount, ...],
        max_age_seconds: float = 1800,
    ):
        self.session_file, self.runtime_file = session_file, runtime_file
        self.accounts, self.max_age_seconds = accounts, max_age_seconds
        self.connection = None
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

    def read(self) -> tuple[WeeklyQuota, ...]:
        connection = None
        self.cancelled.clear()
        self.reading.set()
        try:
            runtime = json.loads(self.runtime_file.read_text())
            credential = json.loads(self.session_file.read_text())
            origin = f"http://127.0.0.1:{int(runtime['port'])}"
            connection = websocket.create_connection(
                origin.replace("http:", "ws:") + "/ws?orchestrationProtocol=2",
                header={"Authorization": "Bearer " + credential["token"]},
                origin=origin,
                timeout=3,
                http_no_proxy=["127.0.0.1"],
            )
            self.connection = connection
            if self.cancelled.is_set():
                raise WeeklyInterrupted("Activity changed during weekly refresh")
            connection.send(
                json.dumps(
                    {
                        "_tag": "Request",
                        "id": "weekly",
                        "tag": "subscribeServerConfig",
                        "headers": [],
                        "payload": {"usageLimitSources": True},
                    }
                )
            )
            providers, sources = None, None
            needs_sources = any(a.source_id for a in self.accounts)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                raw = json.loads(connection.recv())
                for message in raw if isinstance(raw, list) else [raw]:
                    if message.get("_tag") == "Ping":
                        connection.send(json.dumps({"_tag": "Pong"}))
                    elif message.get("_tag") == "Exit":
                        raise WeeklyError("T3 rejected the read-only weekly-limits subscription.")
                    elif message.get("_tag") == "Chunk" and message.get("requestId") == "weekly":
                        connection.send(json.dumps({"_tag": "Ack", "requestId": "weekly"}))
                        for event in message.get("values", []):
                            if event.get("type") == "snapshot":
                                providers = event["config"]["providers"]
                            elif event.get("type") == "usageLimitSourcesUpdated":
                                sources = event["payload"]["sources"]
                if providers is not None and (sources is not None or not needs_sources):
                    return self._select(providers, sources or [])
            raise WeeklyError("T3's cached weekly limits did not arrive in time.")
        except WeeklyError:
            raise
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            websocket.WebSocketException,
        ):
            if self.cancelled.is_set():
                raise WeeklyInterrupted("Activity changed during weekly refresh") from None
            raise WeeklyError(
                "Cannot read T3's cached weekly limits; keeping recent readings."
            ) from None
        finally:
            self.reading.clear()
            self.connection = None
            if connection is not None:
                connection.close()

    def _select(self, providers: list, sources: list) -> tuple[WeeklyQuota, ...]:
        result = []
        for account in self.accounts:
            limits = {}
            if account.instance_id:
                provider = next(
                    (
                        p
                        for p in providers
                        if p.get("instanceId") == account.instance_id and p.get("enabled")
                    ),
                    {},
                )
                limits = provider.get("usageLimits", {})
            else:
                source = next(
                    (s for s in sources if s.get("id") == account.source_id and not s.get("error")),
                    {},
                )
                item = next(
                    (a for a in source.get("accounts", []) if a.get("id") == account.account_id), {}
                )
                limits = item.get("usageLimits", {})
            result.append(quota(account, limits, self.max_age_seconds))
        return tuple(result)


class WeeklyCache:
    def __init__(self, reader: T3Weekly, refresh_seconds: float = 120, *, clock=time.monotonic):
        self.reader, self.refresh_seconds, self.clock = reader, max(120, refresh_seconds), clock
        self.next_read = 0.0
        self.values = tuple(WeeklyQuota(a.label) for a in reader.accounts)
        self.error: str | None = None

    def read(self, *, refresh: bool = True) -> tuple[WeeklyQuota, ...]:
        if refresh and self.clock() >= self.next_read:
            try:
                previous = {q.label: q for q in self.values}
                self.values = tuple(
                    q if q.used_percent is not None else previous.get(q.label, q)
                    for q in self.reader.read()
                )
                self.error = None
            except WeeklyInterrupted:
                pass
            except WeeklyError as error:
                self.error = str(error)
            finally:
                # Completed, failed, and interrupted attempts all share the
                # two-minute floor. Activity never causes an immediate retry.
                self.next_read = self.clock() + self.refresh_seconds
        return self.values
