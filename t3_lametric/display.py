from __future__ import annotations

from dataclasses import dataclass

from .icons import idle_dots, quota_battery, symbol, token_icon, working_spinner
from .source import Agent, Snapshot
from .weekly import remaining


def compact(count: int) -> str:
    units = ("", "k", "M", "B", "T")
    value = float(count)
    for unit in units:
        if value < 999.95 or unit == units[-1]:
            return f"{value:.1f}{unit}"
        value /= 1000
    raise AssertionError("Unreachable")


def clean_text(value: str, limit: int = 320) -> str:
    text = " ".join("".join(c for c in value if c.isprintable() or c.isspace()).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def working_agents(snapshot: Snapshot) -> tuple[Agent, ...]:
    blocked = {r.thread_id for r in snapshot.requests if r.blocking}
    return tuple(
        a
        for a in snapshot.agents
        if a.status not in ("queued", "pending", "idle") and a.thread_id not in blocked
    )


def running_count(snapshot: Snapshot) -> int:
    return len({a.session_id or a.thread_id for a in working_agents(snapshot)})


def count_frame(snapshot: Snapshot, duration: int = 1000) -> dict | None:
    count = running_count(snapshot)
    return {"text": str(count), "icon": working_spinner(), "duration": duration} if count else None


def frames(snapshot: Snapshot) -> list[dict]:
    if snapshot.online is False:
        return [{"text": "T3 OFFLINE", "duration": 5000}]
    if snapshot.requests:
        return [{"text": "Input", "icon": symbol("input"), "duration": 10000}]
    if running_count(snapshot):
        total = compact(snapshot.usage.total) if snapshot.usage.available else "N/A"
        return [{"text": total, "icon": token_icon(), "duration": 5000}]
    sleeping = {"text": "T3", "icon": idle_dots(), "duration": 10000}
    if not snapshot.weekly:
        return [sleeping]
    result = []
    for account in snapshot.weekly:
        left = remaining(account, snapshot.captured_at)
        result.extend(
            [
                dict(sleeping, duration=7500),
                {
                    "text": f"{account.label}{left}%" if left is not None else f"{account.label} ?",
                    "icon": quota_battery(left),
                    "duration": 2500,
                },
            ]
        )
    return result


@dataclass(frozen=True)
class Alert:
    key: str
    kind: str
    text: str
    occurred_at: str

    def payload(self, sound: str = "") -> dict:
        icon = symbol("input" if self.kind == "attention" else self.kind)
        model = {"frames": [{"text": self.text, "icon": icon}], "cycles": 1}
        if sound:
            model["sound"] = {"category": "notifications", "id": sound, "repeat": 1}
        return {
            "priority": "warning" if self.kind in ("attention", "failed") else "info",
            "icon_type": "none",
            "lifeTime": 10000,
            "model": model,
        }


def alerts(
    snapshot: Snapshot, *, include_titles: bool = False, subagent_completion: bool = False
) -> list[Alert]:
    if snapshot.online is False:
        return []
    result = [
        Alert(
            f"request:{r.id}",
            "attention",
            "Input",
            r.created_at,
        )
        for r in snapshot.requests
    ]
    for run in snapshot.completions:
        if run.subagent and not subagent_completion:
            continue
        if run.status in ("completed", "failed"):
            kind = "done" if run.status == "completed" else "failed"
            text = "Done" if kind == "done" else "Failed"
            if include_titles and snapshot.titles.get(run.thread_id):
                text += ": " + clean_text(snapshot.titles[run.thread_id], 60)
            result.append(
                Alert(f"run:{run.id}:{run.status}:{run.completed_at}", kind, text, run.completed_at)
            )
    return result
