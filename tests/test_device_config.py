import base64
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from t3_lametric.config import Config, ConfigError, load
from t3_lametric.device import DeviceError, LaMetric, WidgetVisibility


class DeviceTests(unittest.TestCase):
    def test_reel_sends_nine_raw_frames_and_input_stops_it_before_updating_widget(self):
        device = LaMetric(self.config)
        device._roll_supported = True
        device._request = Mock(
            side_effect=[
                {"status": "stopped"},
                {
                    "success": {
                        "data": {"session_id": "12345678-1234-5678-1234-567812345678", "port": 9999}
                    }
                },
                {},
                {},
            ]
        )
        with (
            patch("t3_lametric.device.socket.socket") as sender,
            patch("t3_lametric.device.threading.Timer") as timer,
            patch("t3_lametric.device.threading.Thread") as worker,
        ):
            self.assertTrue(device.roll_tokens("1.0M", "1.1M"))
            args = worker.call_args.kwargs["args"]
            with patch.object(args[-1], "wait", return_value=False):
                device._play_token_roll(*args)
            sent = sender.return_value.__enter__.return_value.sendto
            self.assertEqual(sent.call_count, 9)
            self.assertTrue(all(len(call.args[0]) == 924 for call in sent.call_args_list))
            self.assertEqual(timer.call_args.args[0], 0.5)
            device.dashboard([{"text": "Input"}])
            timer.return_value.cancel.assert_called_once()
        paths = [call.args[0] for call in device._request.call_args_list]
        self.assertTrue(paths[-2].endswith("/stream/stop"))
        self.assertEqual(paths[-1], self.config.push_url)
        self.assertIsNone(device._roll_timer)

    def test_expired_overlay_callback_cannot_stop_a_newer_animation(self):
        device = LaMetric(self.config)
        old, current = Mock(), Mock()
        device._roll_timer = current
        device._request = Mock()
        device._finish_token_roll(old)
        device._request.assert_not_called()
        self.assertIs(device._roll_timer, current)

    def test_reel_does_not_replace_an_existing_external_stream(self):
        device = LaMetric(self.config)
        device._roll_supported = True
        device._request = Mock(return_value={"status": "receiving"})
        self.assertFalse(device.roll_tokens("1.0M", "1.1M"))
        device._request.assert_called_once()
        self.assertIsNone(device._roll_timer)

    def test_uncertain_stream_start_schedules_cleanup_and_disables_reel(self):
        device = LaMetric(self.config)
        device._roll_supported = True
        device._request = Mock(side_effect=[{"status": "stopped"}, DeviceError("timeout")])
        with patch("t3_lametric.device.threading.Timer") as timer:
            self.assertFalse(device.roll_tokens("1.0M", "1.1M"))
            self.assertEqual(timer.call_args.args[0], 0)
            timer.return_value.start.assert_called_once()
        self.assertFalse(device.supports_token_roll)

    @classmethod
    def setUpClass(cls):
        cls.requests = []
        cls.response_status = 200
        cls.response_body = b'{"success":true}'

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def do_DELETE(self):
                self.respond()

            def respond(self):
                length = int(self.headers.get("Content-Length", 0))
                cls.requests.append(
                    (self.path, self.headers.get("Authorization"), self.rfile.read(length))
                )
                self.send_response(cls.response_status)
                if cls.response_status == 302:
                    self.send_header("Location", "/credential-leak")
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(cls.response_body)

            def log_message(self, *args):
                pass

        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        type(self).requests.clear()
        type(self).response_status = 200
        type(self).response_body = b'{"success":true}'
        url = f"http://127.0.0.1:{self.server.server_port}"
        self.config = Config(
            Path("unused"),
            None,
            Path("state"),
            device_url=url,
            push_url=url + "/api/v2/widget/update/com.lametric.diy.devwidget/1",
        )
        self.environment = patch.dict(os.environ, {"LAMETRIC_API_KEY": "fixture-device-key"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_authenticated_device_dashboard_and_notification_requests(self):
        device = LaMetric(self.config)
        device.check()
        device.dashboard([{"text": "T3 2 RUN"}, {"text": "TOK 1M"}])
        device.notify({"model": {"frames": [{"text": "CX DONE"}], "cycles": 1}})
        expected = "Basic " + base64.b64encode(b"dev:fixture-device-key").decode()
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(all(r[1] == expected for r in self.requests))
        self.assertEqual(self.requests[0][0], "/api/v2")
        self.assertEqual(self.requests[2][0], "/api/v2/device/notifications")
        payload = json.loads(self.requests[1][2])
        self.assertEqual([f["index"] for f in payload["frames"]], [0, 1])

    def test_redirects_do_not_forward_device_credentials(self):
        type(self).response_status = 302
        with self.assertRaisesRegex(DeviceError, "HTTP 302"):
            LaMetric(self.config).check()
        self.assertEqual(len(self.requests), 1)

    def test_native_cues_return_an_id_and_only_dismiss_that_id(self):
        type(self).response_body = b'{"success":{"id":"owned-cue"}}'
        device = LaMetric(self.config)
        self.assertEqual(device.notify({"model": {"frames": [{"text": "Done"}]}}), "owned-cue")
        device.dismiss("owned-cue")
        self.assertEqual(self.requests[-1][0], "/api/v2/device/notifications/owned-cue")
        type(self).response_status = 404
        device.dismiss("already-expired-owned-cue")

    def test_timed_dismissal_runs_independently_of_the_calling_thread(self):
        device = LaMetric(self.config)
        expiry = device.dismiss_after("owned-done", 0.05)
        self.assertTrue(expiry.completed.wait(1))
        self.assertTrue(expiry.succeeded)
        self.assertTrue(expiry.cancel())
        self.assertEqual([r[0] for r in self.requests], ["/api/v2/device/notifications/owned-done"])

    def test_device_errors_and_invalid_json_are_reported_without_response_secrets(self):
        type(self).response_body = b'{"errors":[{"message":"fixture-device-key"}]}'
        with self.assertRaises(DeviceError) as result:
            LaMetric(self.config).check()
        self.assertNotIn("fixture-device-key", str(result.exception))
        type(self).response_body = b"not json"
        with self.assertRaisesRegex(DeviceError, "invalid JSON"):
            LaMetric(self.config).check()

    def test_notifications_work_without_a_dashboard_widget(self):
        device = LaMetric(replace(self.config, push_url=""))
        device.dashboard([{"text": "T3 IDLE"}])
        self.assertEqual(self.requests, [])
        device.notify({"model": {"frames": [{"text": "T3 DONE"}]}})
        self.assertEqual(len(self.requests), 1)

    def test_visibility_matches_the_exact_widget_and_is_cached_for_five_seconds(self):
        type(self).response_body = json.dumps(
            {
                "com.lametric.diy.devwidget": {
                    "widgets": {"1": {"visible": False}, "other": {"visible": True}}
                }
            }
        ).encode()
        current = [0]
        visibility = WidgetVisibility(LaMetric(self.config), clock=lambda: current[0])
        self.assertFalse(visibility.read())
        current[0] = 4
        self.assertFalse(visibility.read())
        self.assertEqual(len(self.requests), 1)
        type(self).response_body = json.dumps(
            {"com.lametric.diy.devwidget": {"widgets": {"1": {"visible": True}}}}
        ).encode()
        current[0] = 5
        self.assertTrue(visibility.read())
        self.assertEqual(len(self.requests), 2)
        type(self).response_status = 500
        current[0] = 10
        self.assertFalse(visibility.read())
        self.assertIsNotNone(visibility.error)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def config(self, text):
        path = self.root / "config.toml"
        path.write_text(text)
        return load(path)

    def test_relative_paths_and_push_origin_resolve_without_network(self):
        config = self.config(
            '[t3]\ndatabase="data/statev2.sqlite"\ncheck_runtime=false\n[lametric]\npush_url="http://192.168.1.2:8080/api/v2/widget/update/widget/1"\n'
        )
        self.assertEqual(config.database, self.root / "data/statev2.sqlite")
        self.assertIsNone(config.runtime_file)
        self.assertEqual(config.device_url, "http://192.168.1.2:8080")
        self.assertFalse(config.database.exists())

    def test_typos_and_invalid_intervals_fail_clearly(self):
        for text in (
            "[bridge]\npoll_seconds=0\n",
            '[bridge]\nnotify_attention="false"\n',
            "[bridge]\npoll_seconds=nan\n",
            "[bridge]\ntoken_refresh_seconds=0.5\n",
            "[bridge]\ncompletion_seconds=0.49\n",
            "[bridge]\ncompletion_seconds=1.01\n",
            "[bridge]\nnotify_compeleted=true\n",
            '[t3]\ncheck_runtime="false"\n',
            "[unknown]\na=1\n",
        ):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                self.config(text)

    def test_credentials_cannot_be_embedded_in_a_device_url(self):
        for value in (
            "http://dev:secret@localhost:8080",
            "file:///tmp/device",
            "http://localhost:8080?key=secret",
            "http://localhost:bad",
        ):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                self.config(f'[lametric]\ndevice_url="{value}"\n')

    def test_secret_file_and_environment_override(self):
        key_file = self.root / "device.key"
        key_file.write_text("file-secret\n")
        config = self.config(
            '[lametric]\napi_key_env="T3_LAMETRIC_TEST_KEY"\napi_key_file="device.key"\n'
        )
        with patch.dict(os.environ, {"T3_LAMETRIC_TEST_KEY": ""}):
            self.assertEqual(config.key(), "file-secret")
        with patch.dict(os.environ, {"T3_LAMETRIC_TEST_KEY": "environment-secret"}):
            self.assertEqual(config.key(), "environment-secret")

    def test_an_explicit_missing_config_file_is_an_error(self):
        with self.assertRaisesRegex(ConfigError, "not found"):
            load(self.root / "missing.toml")

    def test_state_cannot_replace_the_t3_database(self):
        with self.assertRaises(ConfigError):
            self.config('[t3]\ndatabase="same"\n[bridge]\nstate_file="same"\n')

    def test_weekly_accounts_and_private_session_path(self):
        config = self.config(
            '[weekly]\nsession_file="weekly.json"\naccounts=[{label="CP",instance_id="personal"},{label="CL",source_id="hub",account_id="claude"}]\n'
        )
        self.assertEqual(config.weekly_session_file, self.root / "weekly.json")
        self.assertEqual([a.label for a in config.weekly_accounts], ["CP", "CL"])
        self.assertEqual(config.weekly_refresh_seconds, 120)
        for settings in (
            'accounts=[{label="TOOLONG",instance_id="codex"}]',
            'accounts=[{label="CP",source_id="hub"}]',
            'accounts=[{label="CP",instance_id="codex",source_id="hub",account_id="a"}]',
            'accounts=[{label="CP",instance_id="a"},{label="CP",instance_id="b"}]',
            "refresh_seconds=5",
            "refresh_seconds=119",
            "max_age_seconds=-1",
        ):
            with self.subTest(settings=settings), self.assertRaises(ConfigError):
                self.config('[weekly]\nsession_file="weekly.json"\n' + settings)


if __name__ == "__main__":
    unittest.main()
