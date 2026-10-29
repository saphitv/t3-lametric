from __future__ import annotations

import base64
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote, unquote, urlsplit

from .config import Config, ConfigError
from .reel import ROLL_SECONDS, roll_images, stream_packet


class DeviceError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Device credentials must never follow a redirect to another origin.
        return None


class TimedDismissal:
    """Dismiss one owned cue even if the display loop is busy reading T3."""

    def __init__(self, device: LaMetric, identifier: str, seconds: float):
        self.device, self.identifier = device, identifier
        self.completed = threading.Event()
        self.succeeded = False
        self.error: str | None = None
        self.timer = threading.Timer(seconds, self._expire)
        self.timer.daemon = True
        self.timer.start()

    def _expire(self):
        try:
            self.device.dismiss(self.identifier)
            self.succeeded = True
        except DeviceError as error:
            self.error = str(error)
        finally:
            self.completed.set()

    def cancel(self) -> bool:
        self.timer.cancel()
        return self.succeeded


class LaMetric:
    def __init__(self, config: Config):
        if not config.device_url:
            raise ConfigError(
                "Set lametric.device_url or lametric.push_url before sending to the clock."
            )
        self.config = config
        self._authorization = "Basic " + base64.b64encode(f"dev:{config.key()}".encode()).decode(
            "ascii"
        )
        context = ssl.create_default_context()
        if not config.verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect(),
            urllib.request.HTTPSHandler(context=context),
        )
        self._roll_lock = threading.RLock()
        self._roll_timer: threading.Timer | None = None
        self._roll_cancel: threading.Event | None = None
        self._roll_supported: bool | None = None
        self.roll_error: str | None = None

    @property
    def supports_token_roll(self) -> bool:
        if self._roll_supported is None:
            try:
                status = self._request(self.config.device_url.rstrip("/") + "/api/v2/device/stream")
                size = status.get("canvas", {}).get("pixel", {}).get("size", {})
                self._roll_supported = status.get("protocol") == "lmsp" and size == {
                    "width": 37,
                    "height": 8,
                }
            except DeviceError as error:
                self.roll_error, self._roll_supported = str(error), False
        return self._roll_supported

    def stop_token_roll(self) -> None:
        with self._roll_lock:
            if self._roll_timer is None:
                return
            self._roll_timer.cancel()
            if self._roll_cancel:
                self._roll_cancel.set()
            self._request(
                self.config.device_url.rstrip("/") + "/api/v2/device/stream/stop", {}, method="PUT"
            )
            self._roll_timer = None
            self._roll_cancel = None

    def _schedule_roll_stop(self, seconds: float) -> threading.Timer:
        timer = threading.Timer(seconds, lambda: self._finish_token_roll(timer))
        self._roll_timer = timer
        timer.daemon = True
        timer.start()
        return timer

    def _finish_token_roll(self, expected: threading.Timer) -> None:
        with self._roll_lock:
            if self._roll_timer is not expected:
                return
            try:
                self.stop_token_roll()
            except DeviceError as error:
                # Retain ownership so the next normal update retries stop
                # before sending a question or completion cue.
                self.roll_error = str(error)

    def roll_tokens(self, previous: str, current: str) -> bool:
        images = roll_images(previous, current)
        if not images or not self.supports_token_roll:
            return False
        base = self.config.device_url.rstrip("/") + "/api/v2/device/stream"
        with self._roll_lock:
            self.stop_token_roll()
            started = False
            try:
                # Do not replace another streaming client's display.
                if self._request(base).get("status") != "stopped":
                    return False
                started = True
                result = self._request(
                    base + "/start",
                    {
                        "canvas": {
                            "fill_type": "scale",
                            "render_mode": "pixel",
                            "post_process": {"type": "none"},
                        }
                    },
                    method="PUT",
                )
                data = result["success"]["data"]
                packets = [stream_packet(data["session_id"], pixels) for pixels in images]
                target = (urlsplit(base).hostname, int(data["port"]))
                self._roll_cancel = threading.Event()
                timer = self._schedule_roll_stop(ROLL_SECONDS)
                worker = threading.Thread(
                    target=self._play_token_roll,
                    args=(packets, target, timer, self._roll_cancel),
                    daemon=True,
                )
                worker.start()
                self.roll_error = None
                return True
            except (DeviceError, OSError, ValueError, KeyError, TypeError) as error:
                self.roll_error = str(error)
                self._roll_supported = False
                if started:
                    # Schedule a stop even when a malformed response or UDP
                    # failure prevented playback from being set up normally.
                    self._schedule_roll_stop(0)
                return False

    def _play_token_roll(self, packets, target, expected, cancelled) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                for packet in packets:
                    with self._roll_lock:
                        if self._roll_timer is not expected or cancelled.is_set():
                            return
                        sender.sendto(packet, target)
                    if cancelled.wait(0.05):
                        return
        except OSError as error:
            self.roll_error, self._roll_supported = str(error), False
            self._finish_token_roll(expected)

    def _request(
        self,
        url: str,
        payload: dict | None = None,
        *,
        method: str | None = None,
        missing_ok: bool = False,
    ) -> dict:
        request = urllib.request.Request(
            url,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={
                "Authorization": self._authorization,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self.config.timeout_seconds) as response:
                body = response.read(65537)
            if len(body) > 65536:
                raise DeviceError("LaMetric response exceeded 64 KiB")
            result = json.loads(body) if body.strip() else {}
            if isinstance(result, dict) and (
                result.get("errors") or result.get("success") is False
            ):
                raise DeviceError(
                    "LaMetric rejected the update; check the widget URL and device key."
                )
            return result
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if missing_ok and code == 404:
                return {}
            raise DeviceError(
                f"LaMetric returned HTTP {code}; check the URL, push mode, and device key."
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise DeviceError(
                "Cannot reach LaMetric; check its address and network connection."
            ) from None
        except (ValueError, UnicodeError):
            raise DeviceError("LaMetric returned an invalid JSON response") from None

    def check(self) -> None:
        self._request(self.config.device_url.rstrip("/") + "/api/v2")

    def dashboard(self, frames: list[dict]) -> None:
        self.stop_token_roll()
        if self.config.push_url:
            self._request(
                self.config.push_url,
                {"frames": [dict(frame, index=i) for i, frame in enumerate(frames)]},
            )

    def dashboard_visible(self) -> bool:
        if self._roll_timer is not None:
            # The owned half-second overlay temporarily hides the same widget.
            return True
        parts = urlsplit(self.config.push_url).path.rstrip("/").split("/")
        if len(parts) < 2:
            return False
        package, widget_id = map(unquote, parts[-2:])
        apps = self._request(self.config.device_url.rstrip("/") + "/api/v2/device/apps")
        if not isinstance(apps, dict):
            raise DeviceError("LaMetric returned unsupported app visibility data")
        app = apps.get(package, {})
        widgets = app.get("widgets", {}) if isinstance(app, dict) else {}
        widget = widgets.get(widget_id, {}) if isinstance(widgets, dict) else {}
        return isinstance(widget, dict) and widget.get("visible") is True

    def notify(self, payload: dict) -> str | None:
        self.stop_token_roll()
        result = self._request(
            self.config.device_url.rstrip("/") + "/api/v2/device/notifications", payload
        )
        identifier = (
            result.get("success", {}).get("id") if isinstance(result.get("success"), dict) else None
        )
        return str(identifier) if identifier is not None else None

    def dismiss(self, notification_id: str) -> None:
        self._request(
            self.config.device_url.rstrip("/")
            + "/api/v2/device/notifications/"
            + quote(notification_id, safe=""),
            method="DELETE",
            missing_ok=True,
        )

    def dismiss_after(self, notification_id: str, seconds: float) -> TimedDismissal:
        return TimedDismissal(self, notification_id, seconds)


class WidgetVisibility:
    """Check the selected clock app at most every five seconds, only when asked."""

    def __init__(self, device: LaMetric, *, clock=time.monotonic):
        self.device, self.clock = device, clock
        self.next_read = 0.0
        self.visible = False
        self.error: str | None = None

    def read(self) -> bool:
        if self.clock() >= self.next_read:
            try:
                self.visible = self.device.dashboard_visible()
                self.error = None
            except DeviceError as error:
                self.visible, self.error = False, str(error)
            self.next_read = self.clock() + 5
        return self.visible
