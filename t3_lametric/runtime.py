"""Keep activity events, cue deadlines, and visible-token refreshes independent."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime

from .activity import ActivityStream
from .bridge import Bridge
from .device import WidgetVisibility
from .source import T3Reader
from .usage import UsageCache
from .weekly import WeeklyCache


class DisplayLoop:
    def __init__(
        self,
        reader: T3Reader,
        bridge: Bridge,
        activity: ActivityStream | None,
        usage: UsageCache | None,
        weekly: WeeklyCache | None,
        *,
        clock=time.monotonic,
        visibility: WidgetVisibility | None = None,
    ):
        self.reader, self.bridge, self.activity = reader, bridge, activity
        self.usage, self.weekly, self.clock = usage, weekly, clock
        self.snapshot = None
        self.next_activity_read = 0.0
        self.state = None
        self.retry_at = None
        self.visibility = visibility
        self.tokens_visible = False
        self.activity_dirty = False

    def step(self, now: datetime) -> dict:
        changed = self.activity.changed() if self.activity else False
        self.activity_dirty = self.activity_dirty or changed
        holding_done = (
            self.bridge.phase == "done"
            and self.bridge.next_deadline is not None
            and self.clock() < self.bridge.next_deadline
        )
        observe = not holding_done and (
            self.snapshot is None
            or self.activity_dirty
            or self.clock() >= self.next_activity_read
            or (self.retry_at is not None and self.clock() >= self.retry_at)
        )
        if observe:
            self.snapshot = self.reader.read(now)
            self.activity_dirty = False
            interval = (
                60 if self.activity and self.activity.healthy else self.bridge.config.poll_seconds
            )
            self.next_activity_read = self.clock() + interval
        else:
            self.snapshot = replace(self.snapshot, captured_at=now)
        if self.usage:
            self.snapshot = replace(self.snapshot, usage=self.usage.value)
        if self.weekly:
            self.snapshot = replace(self.snapshot, weekly=self.weekly.values)
        # Commit a completion cue before making any potentially slow usage RPC.
        update = self.bridge.tick(self.snapshot, observe=observe)
        self.tokens_visible = (
            update["state"] == "tokens"
            and not update["errors"]
            and (self.visibility is None or self.visibility.read())
        )
        old_usage, old_weekly = self.snapshot.usage, self.snapshot.weekly
        if self.usage:
            self.snapshot = replace(
                self.snapshot,
                usage=self.usage.read(
                    now,
                    displayed=self.tokens_visible,
                ),
            )
        if self.weekly:
            self.snapshot = replace(
                self.snapshot,
                weekly=self.weekly.read(
                    refresh=update["state"] == "idle" and not update["errors"],
                ),
            )
        if old_usage != self.snapshot.usage or old_weekly != self.snapshot.weekly:
            refreshed = self.bridge.tick(self.snapshot, observe=False)
            refreshed["errors"] = update["errors"] + refreshed["errors"]
            refreshed["notifications"] = update["notifications"] + refreshed["notifications"]
            refreshed["dropped_events"] += update["dropped_events"]
            update = refreshed
        self.state = update["state"]
        self.retry_at = self.clock() + self.bridge.config.poll_seconds if update["errors"] else None
        return update

    def deadline(self) -> float:
        if (
            self.bridge.phase == "done"
            and self.bridge.next_deadline is not None
            and self.clock() < self.bridge.next_deadline
        ):
            return self.bridge.next_deadline
        if self.retry_at is not None:
            return min(self.next_activity_read, self.retry_at)
        deadlines = [self.next_activity_read]
        if self.bridge.last_push is not None:
            deadlines.append(self.bridge.last_push + self.bridge.config.refresh_seconds)
        if self.bridge.next_deadline is not None:
            deadlines.append(self.bridge.next_deadline)
        if self.usage and self.tokens_visible:
            deadlines.append(self.usage.next_read)
        if self.visibility and self.state == "tokens":
            deadlines.append(self.visibility.next_read)
        if self.bridge.counter.deadline is not None:
            deadlines.append(self.bridge.counter.deadline)
        if self.weekly and self.state == "idle":
            deadlines.append(self.weekly.next_read)
        return min(deadlines)
