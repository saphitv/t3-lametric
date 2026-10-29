from __future__ import annotations

import json
import os
import tempfile
import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .counter import TokenCounter
from .device import DeviceError, LaMetric, TimedDismissal
from .display import Alert, alerts, count_frame, frames, running_count, working_agents
from .icons import symbol
from .source import Snapshot, iso, parse_time


class StateError(Exception):
    pass


class Ledger:
    """Remember delivered and deliberately dropped IDs; delivery failures retry."""

    def __init__(self, path: Path | None, source: str):
        self.path = path
        self.seen: dict[str, str] = {}
        self.initialized = False
        self.source = source
        if path and path.exists():
            try:
                data = json.loads(path.read_text())
                if data.get("version") != 1 or not isinstance(data.get("seen"), dict):
                    raise ValueError("Unsupported bridge state")
                if data.get("source") == source:
                    self.seen = {
                        k: v
                        for k, v in data["seen"].items()
                        if isinstance(k, str) and isinstance(v, str)
                    }
                    self.initialized = True
            except (OSError, ValueError, AttributeError) as error:
                raise StateError(
                    f"Cannot load bridge state {path}; move it aside to start with a fresh baseline."
                ) from error
        self._saved = None

    def prepare(self, snapshot: Snapshot, config: Config) -> list[Alert]:
        if snapshot.online is False:
            return []
        candidates = alerts(
            snapshot,
            include_titles=config.show_titles,
            subagent_completion=config.subagent_completions,
        )
        now = snapshot.captured_at
        expiry = now - timedelta(days=1)
        active_requests = {a.key for a in candidates if a.kind == "attention"}
        self.seen = {
            k: value
            for k, value in self.seen.items()
            if k in active_requests or (parse_time(value) or expiry) > expiry
        }
        fresh = not self.initialized
        self.initialized = True
        pending = []
        for alert in candidates:
            occurred = parse_time(alert.occurred_at)
            enabled = {
                "attention": config.notify_attention,
                "done": config.notify_completed,
                "failed": config.notify_failed,
            }[alert.kind]
            too_old = (
                occurred is None or (now - occurred).total_seconds() > config.max_alert_age_seconds
            )
            if alert.key in self.seen:
                continue
            if (
                not enabled
                or (fresh and alert.kind != "attention")
                or (too_old and alert.kind != "attention")
            ):
                self.seen[alert.key] = iso(now)
                continue
            # Pending input/approvals remain useful even if they were created
            # before startup. Completed runs are only useful while recent.
            pending.append(alert)
        pending.sort(
            key=lambda a: ({"attention": 0, "failed": 1, "done": 2}[a.kind], a.occurred_at, a.key)
        )
        self.save()
        return pending

    def delivered(self, items: list[Alert], now: datetime) -> None:
        for alert in items:
            self.seen[alert.key] = iso(now)
        self.save()

    def discarded(self, items: list[Alert], now: datetime) -> None:
        # Overflow must remain rejected after a slot opens or the bridge restarts.
        self.delivered(items, now)

    def save(self) -> None:
        if self.path is None:
            return
        content = json.dumps(
            {"version": 1, "source": self.source, "seen": self.seen}, sort_keys=True
        )
        if content == self._saved:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".state-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as output:
                    output.write(content + "\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        except OSError as error:
            raise StateError(f"Cannot save bridge state: {error.strerror}") from error
        self._saved = content


@contextmanager
def single_instance(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise StateError(
                "A bridge already owns this state file. Stop it before starting another."
            ) from None
        yield


class Bridge:
    MAX_PENDING_EVENTS = 5

    def __init__(
        self, config: Config, device: LaMetric | None, ledger: Ledger, *, clock=time.monotonic
    ):
        self.config, self.device, self.ledger = config, device, ledger
        self.clock = clock
        self.last_dashboard: list[dict] | None = None
        self.last_widget_dashboard: list[dict] | None = None
        self.last_push: float | None = None
        self.phase: str | None = None
        self.next_deadline: float | None = None
        self.cue_id: str | None = None
        self.cue_state: str | None = None
        self.cue_event_key: str | None = None
        self.cue_expiry: TimedDismissal | None = None
        self.counter = TokenCounter()
        self.started_runs: set[str] = set()
        self.start_order: deque[str] = deque()
        self.starts_initialized = False
        self.events: deque[Alert] = deque()
        self.current_event: Alert | None = None

    def close(self) -> None:
        if self.device:
            self.device.stop_token_roll()
        if self.device and self.cue_id:
            expired = self.cue_expiry.cancel() if self.cue_expiry else False
            self.cue_expiry = None
            if not expired:
                self.device.dismiss(self.cue_id)
            self.cue_id, self.cue_state = None, None
            self.cue_event_key = None

    def _expired(self, event: Alert, snapshot: Snapshot) -> bool:
        occurred = parse_time(event.occurred_at)
        return (
            occurred is None
            or (snapshot.captured_at - occurred).total_seconds() > self.config.max_alert_age_seconds
        )

    def _finish_event(self) -> None:
        self.current_event, self.phase, self.next_deadline = None, None, None

    def _activate_next(self, snapshot: Snapshot) -> None:
        while self.current_event is None and self.events and snapshot.online is not False:
            event = self.events.popleft()
            if self._expired(event, snapshot) or (
                event.kind == "count" and not running_count(snapshot)
            ):
                self.ledger.discarded([event], snapshot.captured_at)
                continue
            self.current_event, self.phase = event, event.kind
            self.next_deadline = None

    def _advance(self, snapshot: Snapshot, now: float) -> None:
        if self.current_event:
            if self.next_deadline is not None and now >= self.next_deadline:
                self.next_deadline = None
                if self.phase in ("done", "failed") and count_frame(snapshot):
                    self.phase = "count"
                else:
                    self._finish_event()
            elif self.next_deadline is None and self._expired(self.current_event, snapshot):
                self.ledger.discarded([self.current_event], snapshot.captured_at)
                self._finish_event()
        if (
            self.current_event
            and self.phase == "count"
            and self.next_deadline is None
            and snapshot.online is not False
            and not running_count(snapshot)
        ):
            self.ledger.discarded([self.current_event], snapshot.captured_at)
            self._finish_event()
        self._activate_next(snapshot)

    def _enqueue(self, candidates: list[Alert], snapshot: Snapshot) -> int:
        known = {event.key for event in self.events}
        if self.current_event:
            known.add(self.current_event.key)
        dropped = []
        for event in sorted(candidates, key=lambda event: (event.occurred_at, event.key)):
            if event.key in known or event.key in self.ledger.seen:
                continue
            known.add(event.key)
            if self.current_event is None:
                self.events.append(event)
                self._activate_next(snapshot)
            elif len(self.events) < self.MAX_PENDING_EVENTS:
                self.events.append(event)
            else:
                dropped.append(event)
        if dropped:
            self.ledger.discarded(dropped, snapshot.captured_at)
        return len(dropped)

    def tick(
        self,
        snapshot: Snapshot,
        *,
        observe: bool = True,
        allow_token_roll: bool | Callable[[], bool] = False,
    ) -> dict:
        now = self.clock()
        self._advance(snapshot, now)

        pending = self.ledger.prepare(snapshot, self.config) if observe or snapshot.requests else []
        candidates = [event for event in pending if event.kind in ("done", "failed")]
        if observe and snapshot.online is not False:
            roots = {a.id: a for a in working_agents(snapshot) if not a.subagent}
            unseen = roots.keys() - self.started_runs
            if self.starts_initialized:
                start_count = str(running_count(snapshot))
                candidates.extend(
                    Alert(
                        "start:" + run_id,
                        "count",
                        start_count,
                        iso(snapshot.captured_at),
                    )
                    for run_id in unseen
                    if roots[run_id].announce_start
                )
            self.starts_initialized = True
            for run_id in unseen:
                self.started_runs.add(run_id)
                self.start_order.append(run_id)
            while len(self.start_order) > 4096:
                self.started_runs.discard(self.start_order.popleft())
        dropped = self._enqueue(candidates, snapshot)
        holding = self.next_deadline is not None and now < self.next_deadline
        selected = (
            [self.current_event]
            if self.current_event
            and self.phase == self.current_event.kind
            and self.current_event.key not in self.ledger.seen
            else []
        )

        dashboard = frames(snapshot)
        state = (
            "offline"
            if snapshot.online is False
            else "input"
            if snapshot.requests
            else "tokens"
            if running_count(snapshot)
            else "idle"
        )
        if snapshot.online is not False or holding:
            if self.phase in ("done", "failed"):
                state = self.phase
                dashboard = [
                    {
                        "text": "Done" if self.phase == "done" else "Failed",
                        "icon": symbol(self.phase),
                        "duration": round(self.config.completion_seconds * 1000),
                    }
                ]
            elif self.phase == "count":
                current = count_frame(snapshot, round(self.config.count_seconds * 1000))
                if current:
                    dashboard, state = [current], "count"
                elif holding and self.cue_state == "count" and self.last_dashboard:
                    dashboard, state = self.last_dashboard, "count"
                elif snapshot.online is not False:
                    self.ledger.discarded([self.current_event], snapshot.captured_at)
                    self._finish_event()
        if state == "input":
            selected = [a for a in pending if a.kind == "attention"]
        elif state not in ("done", "failed", "count"):
            selected = []
        rolling = bool(
            state == "tokens"
            and snapshot.usage.available
            and self.config.token_reel
            and self.device
            and (allow_token_roll() if callable(allow_token_roll) else allow_token_roll)
            and self.device.supports_token_roll
        )
        if state == "tokens" and snapshot.usage.available:
            dashboard = [
                dict(
                    dashboard[0],
                    text=self.counter.render(
                        snapshot.usage.total,
                        "",
                        snapshot.captured_at.date(),
                        now,
                        rolling=rolling,
                    ),
                )
            ]
        else:
            self.counter.pause()
        output = {
            "frames": dashboard,
            "notifications": [],
            "errors": [],
            "state": state,
            "queued_events": len(self.events),
            "dropped_events": dropped,
        }
        event_key = self.current_event.key if self.current_event else None
        new_event = state in ("done", "failed", "count") and event_key != self.cue_event_key
        due = self.last_push is None or now - self.last_push >= self.config.refresh_seconds
        input_dashboard = (
            frames(snapshot) if snapshot.requests and snapshot.online is not False else None
        )
        input_due = input_dashboard is not None and (
            input_dashboard != self.last_widget_dashboard or due
        )
        if dashboard != self.last_dashboard or due or selected or new_event or input_due:
            try:
                if self.device:
                    # Native cues temporarily cover the DIY widget. Replace its
                    # backing page immediately so cue expiry cannot reveal old
                    # token statistics while a request is still pending.
                    if input_due:
                        self.device.dashboard(input_dashboard)
                        self.last_widget_dashboard = input_dashboard
                    if self.cue_id and (self.cue_state != state or new_event):
                        self.close()
                    if state in ("done", "failed", "count") and (
                        self.cue_state != state or dashboard != self.last_dashboard or new_event
                    ):
                        if self.cue_id:
                            self.close()
                        payload = {
                            "priority": "warning",
                            "icon_type": "none",
                            "lifeTime": 1000,
                            "model": {
                                # Give native playback more room than our
                                # explicit timer, so one short frame cycle
                                # cannot finish the Done cue early.
                                "frames": [
                                    dict(frame, duration=1000) if state == "done" else frame
                                    for frame in dashboard
                                ],
                                "cycles": 1,
                            },
                        }
                        identifier = self.device.notify(payload)
                        if identifier is None:
                            raise DeviceError(
                                "LaMetric accepted no notification ID for the timed cue"
                            )
                        self.cue_id, self.cue_state = identifier, state
                        self.cue_event_key = event_key
                        if state == "done":
                            self.cue_expiry = self.device.dismiss_after(
                                identifier, self.config.completion_seconds
                            )
                        output["notifications"].append(payload)
                    elif state not in ("done", "failed", "count"):
                        if input_dashboard is None:
                            self.device.dashboard(dashboard)
                            self.last_widget_dashboard = dashboard
                            if rolling and self.counter.roll_from:
                                self.device.roll_tokens(
                                    self.counter.roll_from, dashboard[0]["text"]
                                )
                        if not self.config.push_url and selected and state == "input":
                            payload = selected[0].payload(self.config.sound)
                            self.device.notify(payload)
                            output["notifications"].append(payload)
                self.last_dashboard, self.last_push = dashboard, self.clock()
                if selected:
                    self.ledger.delivered(selected, snapshot.captured_at)
                if self.next_deadline is None and state in ("done", "failed", "count"):
                    if self.phase in ("done", "failed"):
                        self.next_deadline = self.clock() + self.config.completion_seconds
                    elif self.phase == "count":
                        self.next_deadline = self.clock() + self.config.count_seconds
            except DeviceError as error:
                output["errors"].append(str(error))
        return output
