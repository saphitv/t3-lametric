"""Brief count-up transitions over known values; never query T3 for animation."""

from __future__ import annotations

from datetime import date

from .display import compact


class TokenCounter:
    def __init__(self):
        self.pause()

    def pause(self) -> None:
        self.target: int | None = None
        self.day: date | None = None
        self.text = ""
        self.suffix = ""
        self.pending: list[tuple[float, str]] = []

    @property
    def deadline(self) -> float | None:
        return self.pending[0][0] if self.pending else None

    def render(self, total: int, suffix: str, day: date, now: float) -> str:
        if (total, suffix, day) != (self.target, self.suffix, self.day):
            previous = self.target
            target_text = compact(total) + suffix
            steps = [target_text]
            if (
                previous is not None
                and total > previous
                and day == self.day
                and suffix == self.suffix
                and target_text != self.text
            ):
                # Three bounded pushes at most. Dedupe rounded values: small
                # changes go straight to the real total instead of flickering.
                steps = list(
                    dict.fromkeys(
                        compact(previous + (total - previous) * part // 3) + suffix
                        for part in (1, 2, 3)
                    )
                )
                steps = [text for text in steps if text != self.text] or [target_text]
            self.text = steps[0]
            self.pending = [(now + 0.18 * i, text) for i, text in enumerate(steps) if i]
            self.target, self.day, self.suffix = total, day, suffix
        while self.pending and now >= self.pending[0][0]:
            _, self.text = self.pending.pop(0)
        return self.text
