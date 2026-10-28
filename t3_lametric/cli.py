from __future__ import annotations

import argparse
import json
import plistlib
import signal
import sys
import threading
import time
from contextlib import nullcontext
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .activity import ActivityStream
from .bridge import Bridge, Ledger, StateError, single_instance
from .config import ConfigError, config_path, load
from .device import DeviceError, LaMetric, WidgetVisibility
from .display import Alert, frames, running_count
from .icons import idle_dots, working_spinner
from .remote import CombinedActivity, CombinedReader, CombinedUsage, RemoteActivity
from .runtime import DisplayLoop
from .source import SourceError, T3Reader, Usage, iso, server_online
from .usage import T3Usage, UsageCache, UsageError
from .weekly import T3Weekly, WeeklyCache, remaining


def local_timezone():
    # astimezone().tzinfo is a fixed offset, which gets midnight wrong on
    # daylight-saving transitions. Use the system's full timezone rules.
    try:
        with open("/etc/localtime", "rb") as localtime:
            resolved = str(Path("/etc/localtime").resolve())
            key = resolved.split("/zoneinfo/")[-1] if "/zoneinfo/" in resolved else None
            return ZoneInfo.from_file(localtime, key=key)
    except OSError:
        return datetime.now().astimezone().tzinfo


def summary(snapshot) -> dict:
    return {
        "captured_at": iso(snapshot.captured_at),
        "online": snapshot.online,
        "agents": [asdict(agent) for agent in snapshot.agents],
        "pending_requests": len(snapshot.requests),
        "weekly_accounts": [
            {**asdict(account), "remaining_percent": remaining(account, snapshot.captured_at)}
            for account in snapshot.weekly
        ],
        "usage_today": {
            **asdict(snapshot.usage),
            "total_tokens": snapshot.usage.total,
            "incomplete": snapshot.usage.incomplete,
            "available": snapshot.usage.available,
        },
        "frames": frames(snapshot),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Show T3 Code activity and usage on a LaMetric Time."
    )
    result.add_argument(
        "--config", type=Path, help=f"TOML configuration (default: {config_path()})"
    )
    commands = result.add_subparsers(dest="command", required=True)
    status = commands.add_parser(
        "status", help="Read T3 without contacting the clock or writing state"
    )
    status.add_argument(
        "--json", action="store_true", help="Print normalized activity and LaMetric frames"
    )
    commands.add_parser("frames", help="Print the exact dashboard JSON without sending it")
    run = commands.add_parser("run", help="Run the bridge until Ctrl-C")
    run.add_argument("--once", action="store_true", help="Read and deliver one update, then exit")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Print updates; no clock writes or state-file writes",
    )
    doctor = commands.add_parser("doctor", help="Check T3 compatibility and device configuration")
    doctor.add_argument(
        "--device", action="store_true", help="Also make an authenticated read-only device request"
    )
    commands.add_parser("test-device", help="Send sample dashboard frames and a test notification")
    service = commands.add_parser(
        "service-file", help="Write a macOS launchd plist without enabling it"
    )
    service.add_argument("--output", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    usage_client = None
    activity_client = None
    bridge = None
    try:
        config = load(args.config)
        zone = ZoneInfo(config.time_zone) if config.time_zone else local_timezone()
        wakeup = threading.Event()
        remote_streams = [
            RemoteActivity(remote, wakeup, lookback=config.max_alert_age_seconds)
            for remote in config.remotes
        ]
        if config.usage_session_file and config.runtime_file:
            usage_client = T3Usage(
                config.usage_session_file,
                config.runtime_file,
                config.time_zone or getattr(zone, "key", None) or "UTC",
            )
        if remote_streams:
            remote_usage = [
                (
                    remote,
                    T3Usage(
                        remote.config.session_file,
                        None,
                        config.time_zone or getattr(zone, "key", None) or "UTC",
                        origin=remote.config.url,
                    ),
                )
                for remote in remote_streams
            ]
            usage_client = CombinedUsage(
                usage_client,
                remote_usage,
                local_online=lambda: server_online(config.runtime_file) is not False,
            )
        reader = T3Reader(
            config.database,
            config.runtime_file,
            config.max_alert_age_seconds,
            include_usage=usage_client is None,
        )
        if remote_streams:
            reader = CombinedReader(reader, remote_streams)
        last_usage = Usage()
        last_usage_day = None
        last_usage_error = None
        weekly = (
            WeeklyCache(
                T3Weekly(
                    config.weekly_session_file,
                    config.runtime_file,
                    config.weekly_accounts,
                    config.weekly_max_age_seconds,
                ),
                config.weekly_refresh_seconds,
            )
            if config.weekly_accounts and config.weekly_session_file and config.runtime_file
            else None
        )
        last_weekly_error = None

        def read_snapshot():
            nonlocal last_usage, last_usage_day, last_usage_error, last_weekly_error
            current = reader.read(datetime.now(zone))
            if usage_client:
                day = current.captured_at.date()
                if last_usage_day != day:
                    last_usage, last_usage_day = Usage(), day
                if current.online is False:
                    usage_client.close()
                elif (running_count(current) and not current.requests) or args.command != "run":
                    try:
                        last_usage = usage_client.read(current.captured_at)
                        last_usage_error = None
                    except UsageError as error:
                        if str(error) != last_usage_error:
                            print(error, file=sys.stderr, flush=True)
                            last_usage_error = str(error)
                current = replace(current, usage=last_usage)
            if weekly:
                can_refresh = current.online is not False and (
                    (not running_count(current) and not current.requests) or args.command != "run"
                )
                current = replace(current, weekly=weekly.read(refresh=can_refresh))
                if weekly.error != last_weekly_error:
                    if weekly.error:
                        print(weekly.error, file=sys.stderr, flush=True)
                    last_weekly_error = weekly.error
            return current

        if args.command == "service-file":
            output = args.output.expanduser().resolve()
            config.state_file.parent.mkdir(parents=True, exist_ok=True)
            executable = str(Path(sys.executable).absolute())
            document = {
                "Label": "local.t3-lametric",
                "ProgramArguments": [
                    executable,
                    "-m",
                    "t3_lametric",
                    "--config",
                    str((args.config or config_path()).expanduser().resolve()),
                    "run",
                ],
                "WorkingDirectory": str(Path(__file__).resolve().parent.parent),
                "RunAtLoad": True,
                "KeepAlive": True,
                "ThrottleInterval": 15,
                "StandardOutPath": str(config.state_file.parent / "bridge.log"),
                "StandardErrorPath": str(config.state_file.parent / "bridge.log"),
                "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("xb") as handle:
                plistlib.dump(document, handle)
            print(f"Wrote {output}. The service has not been enabled.")
            return 0
        if args.command == "test-device":
            device = LaMetric(config)
            device.check()
            device.dashboard(
                [
                    {"text": "T3", "icon": idle_dots(), "duration": 3000},
                    {"text": "2", "icon": working_spinner(), "duration": 1000},
                ]
            )
            device.notify(
                Alert("test", "done", "T3 CONNECTED", iso(datetime.now(zone))).payload(config.sound)
            )
            print(
                "Sent the test notification"
                + (
                    " and dashboard."
                    if config.push_url
                    else ". Add push_url for a persistent dashboard."
                )
            )
            return 0
        if args.command in ("status", "frames", "doctor"):
            if remote_streams:
                activity_client = CombinedActivity(None, remote_streams)
                activity_client.start()
                ready_by = time.monotonic() + 3
                while not all(r.healthy for r in remote_streams) and time.monotonic() < ready_by:
                    wakeup.wait(min(0.1, max(0, ready_by - time.monotonic())))
                    wakeup.clear()
            snapshot = read_snapshot()
            if args.command == "frames":
                print(
                    json.dumps(
                        {"frames": frames(snapshot)},
                        indent=2,
                    )
                )
            elif args.command == "doctor":
                print(f"T3 v2 projections: OK ({config.database})")
                print(
                    f"T3 process: {'running' if snapshot.online else 'offline' if snapshot.online is False else 'unchecked'}"
                )
                print(
                    f"Activity: {len(snapshot.agents)} agents, {len(snapshot.requests)} pending requests"
                )
                print(f"Today's total tokens: {snapshot.usage.total:,} ({snapshot.usage.source})")
                for remote in remote_streams:
                    print(
                        f"Remote {remote.config.label}: {'live' if remote.healthy else 'offline'}"
                    )
                for account in snapshot.weekly:
                    left = remaining(account, snapshot.captured_at)
                    print(f"Weekly {account.label}: {left if left is not None else '?'}% remaining")
                print(
                    f"Dashboard: {'configured' if config.push_url else 'not configured (set lametric.push_url)'}"
                )
                print(
                    f"Notifications: {'configured' if config.device_url else 'not configured (set lametric.device_url)'}"
                )
                if args.device:
                    LaMetric(config).check()
                    print("LaMetric device API: OK")
            elif args.json:
                print(json.dumps(summary(snapshot), indent=2))
            else:
                print(" | ".join(frame["text"] for frame in frames(snapshot)))
                if snapshot.usage.incomplete:
                    print("Some usage readings are incomplete or unavailable.")
            return 0

        device = None if args.dry_run else LaMetric(config)
        with nullcontext() if args.dry_run else single_instance(config.state_file):
            ledger = Ledger(None if args.dry_run else config.state_file, str(config.database))
            bridge = Bridge(config, device, ledger)
            stopping = threading.Event()

            def stop(_signum, _frame):
                stopping.set()
                wakeup.set()

            signal.signal(signal.SIGTERM, stop)

            def interrupt_usage():
                if usage_client:
                    usage_client.interrupt()
                if weekly:
                    weekly.reader.interrupt()

            activity_session = config.activity_session_file or config.weekly_session_file
            if activity_session and config.runtime_file and not args.once:
                activity_client = ActivityStream(
                    activity_session, config.runtime_file, wakeup, interrupt=interrupt_usage
                )
            if remote_streams:
                for remote in remote_streams:
                    remote.interrupt = interrupt_usage
                activity_client = CombinedActivity(activity_client, remote_streams)
            if activity_client:
                activity_client.start()
            usage_cache = (
                UsageCache(usage_client, config.token_refresh_seconds) if usage_client else None
            )
            visibility = WidgetVisibility(device) if device and config.push_url else None
            loop = DisplayLoop(
                reader, bridge, activity_client, usage_cache, weekly, visibility=visibility
            )
            previous = None
            previous_stream = None
            previous_usage_error = None
            previous_weekly_error = None
            previous_visibility_error = None
            previous_reader_error = None
            last_data_error_push = 0.0
            while not stopping.is_set():
                wakeup.clear()
                retry_at = None
                try:
                    update = loop.step(datetime.now(zone))
                    snapshot = loop.snapshot
                    if isinstance(reader, CombinedReader) and reader.error != previous_reader_error:
                        if reader.error:
                            print(reader.error, file=sys.stderr, flush=True)
                        previous_reader_error = reader.error
                    if activity_client:
                        stream_state = (
                            activity_client.description
                            if isinstance(activity_client, CombinedActivity)
                            else (
                                "live WebSocket"
                                if activity_client.healthy
                                else activity_client.error
                            )
                        )
                        if stream_state and stream_state != previous_stream:
                            print(f"Activity: {stream_state}", flush=True)
                            previous_stream = stream_state
                    for cache, old_error in (
                        (usage_cache, previous_usage_error),
                        (weekly, previous_weekly_error),
                        (visibility, previous_visibility_error),
                    ):
                        if cache and cache.error and cache.error != old_error:
                            print(cache.error, file=sys.stderr, flush=True)
                    previous_usage_error = usage_cache.error if usage_cache else None
                    previous_weekly_error = weekly.error if weekly else None
                    previous_visibility_error = visibility.error if visibility else None
                    if args.dry_run:
                        content = json.dumps(update, sort_keys=True)
                        if content != previous:
                            print(content, flush=True)
                            previous = content
                    else:
                        status = (
                            "INPUT (" + str(len(snapshot.requests)) + " pending)"
                            if update["state"] == "input"
                            else " | ".join(frame["text"] for frame in update["frames"])
                        )
                        if status != previous:
                            print(f"{iso(datetime.now(zone))} {status}", flush=True)
                            previous = status
                    for error in update["errors"]:
                        print(error, file=sys.stderr, flush=True)
                    if update["dropped_events"]:
                        print(
                            f"Dropped {update['dropped_events']} events (five-event queue full).",
                            flush=True,
                        )
                    if args.once:
                        return 1 if update["errors"] else 0
                except SourceError as error:
                    print(error, file=sys.stderr, flush=True)
                    if args.once:
                        return 1
                    loop.snapshot = None
                    loop.next_activity_read = time.monotonic() + config.poll_seconds
                    retry_at = loop.next_activity_read
                    bridge.phase, bridge.next_deadline = None, None
                    bridge.current_event = None
                    bridge.close()
                    if device and time.monotonic() - last_data_error_push >= config.refresh_seconds:
                        try:
                            device.dashboard([{"text": "T3 DATA?", "duration": 5000}])
                            bridge.last_dashboard = None
                            last_data_error_push = time.monotonic()
                        except DeviceError as device_error:
                            print(device_error, file=sys.stderr, flush=True)
                wake = retry_at if retry_at is not None else loop.deadline()
                wakeup.wait(max(0, wake - time.monotonic()))
        return 0
    except KeyboardInterrupt:
        return 0
    except (ConfigError, SourceError, StateError, DeviceError, OSError) as error:
        print(f"t3-lametric: {error}", file=sys.stderr)
        return 1
    finally:
        if activity_client:
            activity_client.close()
        if bridge:
            try:
                bridge.close()
            except DeviceError as error:
                print(error, file=sys.stderr, flush=True)
        if usage_client:
            usage_client.close()


if __name__ == "__main__":
    raise SystemExit(main())
