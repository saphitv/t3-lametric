from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class WeeklyAccount:
    label: str
    instance_id: str = ""
    source_id: str = ""
    account_id: str = ""


@dataclass(frozen=True)
class RemoteConfig:
    environment_id: str
    label: str
    url: str
    session_file: Path


def config_path() -> Path:
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "t3-lametric/config.toml"
    )


@dataclass(frozen=True)
class Config:
    database: Path
    runtime_file: Path | None
    state_file: Path
    poll_seconds: float = 5
    token_refresh_seconds: float = 1
    refresh_seconds: float = 60
    completion_seconds: float = 0.8
    count_seconds: float = 1
    usage_session_file: Path | None = None
    activity_session_file: Path | None = None
    time_zone: str | None = None
    weekly_session_file: Path | None = None
    weekly_accounts: tuple[WeeklyAccount, ...] = ()
    weekly_refresh_seconds: float = 300
    weekly_max_age_seconds: float = 1800
    remotes: tuple[RemoteConfig, ...] = ()
    max_alert_age_seconds: int = 120
    device_url: str = ""
    push_url: str = ""
    api_key_env: str = "LAMETRIC_API_KEY"
    api_key_file: Path | None = None
    verify_tls: bool = True
    timeout_seconds: float = 3
    sound: str = ""
    notify_completed: bool = True
    notify_failed: bool = True
    notify_attention: bool = True
    subagent_completions: bool = False
    show_titles: bool = False

    def key(self) -> str:
        value = os.environ.get(self.api_key_env, "")
        if not value and self.api_key_file:
            try:
                value = self.api_key_file.read_text().strip()
            except OSError as error:
                raise ConfigError(f"Cannot read the LaMetric key file: {error.strerror}") from error
        if not value.strip():
            raise ConfigError(
                f"Set {self.api_key_env} or lametric.api_key_file before sending to the clock."
            )
        return value.strip()


def load(path: Path | None = None) -> Config:
    requested_path = path
    path = (path or config_path()).expanduser().resolve()
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        if requested_path is not None:
            raise ConfigError(f"Configuration file not found: {path}") from None
        data = {}
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"Cannot read configuration: {error}") from error
    sections = {
        "t3": {
            "database",
            "runtime_file",
            "check_runtime",
            "usage_session_file",
            "activity_session_file",
            "time_zone",
        },
        "bridge": {
            "state_file",
            "poll_seconds",
            "token_refresh_seconds",
            "refresh_seconds",
            "max_alert_age_seconds",
            "notify_completed",
            "notify_failed",
            "notify_attention",
            "subagent_completions",
            "show_titles",
            "provider_counts",
            "context",
            "completion_seconds",
            "count_seconds",
        },
        "lametric": {
            "device_url",
            "push_url",
            "api_key_env",
            "api_key_file",
            "verify_tls",
            "timeout_seconds",
            "sound",
        },
        "weekly": {"session_file", "accounts", "refresh_seconds", "max_age_seconds"},
    }
    if data.keys() - (sections.keys() | {"remotes"}):
        raise ConfigError(
            f"Unknown configuration sections: {', '.join(sorted(data.keys() - (sections.keys() | {'remotes'})))}"
        )
    for name, keys in sections.items():
        section = data.get(name, {})
        if not isinstance(section, dict) or section.keys() - keys:
            raise ConfigError(f"Unknown or invalid settings in [{name}]")
    t3, bridge, device = (data.get(name, {}) for name in ("t3", "bridge", "lametric"))

    def file_path(value: object) -> Path:
        if not isinstance(value, str) or not value:
            raise ConfigError("File paths must be nonempty strings")
        result = Path(value).expanduser()
        return (path.parent / result).resolve() if not result.is_absolute() else result.resolve()

    t3_home = Path(os.environ.get("T3CODE_HOME", str(Path.home() / ".t3"))).expanduser()
    database = file_path(t3.get("database", str(t3_home / "userdata/statev2.sqlite")))
    runtime = file_path(t3.get("runtime_file", str(database.parent / "server-runtime.json")))
    state_home = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    values = {
        "database": database,
        "runtime_file": runtime,
        "state_file": file_path(
            bridge.get("state_file", str(state_home / "t3-lametric/state.json"))
        ),
    }
    for section in (bridge, device):
        values.update(
            {
                key: value
                for key, value in section.items()
                if key not in ("state_file", "api_key_file", "provider_counts", "context")
            }
        )
    if device.get("api_key_file"):
        values["api_key_file"] = file_path(device["api_key_file"])
    if t3.get("usage_session_file"):
        values["usage_session_file"] = file_path(t3["usage_session_file"])
    if t3.get("activity_session_file"):
        values["activity_session_file"] = file_path(t3["activity_session_file"])
    if t3.get("time_zone"):
        values["time_zone"] = t3["time_zone"]
    weekly = data.get("weekly", {})
    if weekly.get("session_file"):
        values["weekly_session_file"] = file_path(weekly["session_file"])
    for key in ("refresh_seconds", "max_age_seconds"):
        if key in weekly:
            values["weekly_" + key] = weekly[key]
    accounts = weekly.get("accounts", [])
    if not isinstance(accounts, list) or len(accounts) > 6:
        raise ConfigError("weekly.accounts must be a list of at most six accounts")
    parsed_accounts = []
    for account in accounts:
        if not isinstance(account, dict) or account.keys() - {
            "label",
            "instance_id",
            "source_id",
            "account_id",
        }:
            raise ConfigError("Invalid weekly account settings")
        if "label" not in account or any(not isinstance(v, str) or not v for v in account.values()):
            raise ConfigError("Weekly account labels and selectors must be nonempty strings")
        item = WeeklyAccount(**account)
        if not 1 <= len(item.label) <= 2 or any(
            c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for c in item.label
        ):
            raise ConfigError("Weekly labels must be one or two uppercase letters/digits")
        if bool(item.instance_id) == bool(item.source_id or item.account_id) or bool(
            item.source_id
        ) != bool(item.account_id):
            raise ConfigError(
                "Select a weekly account by instance_id or by source_id and account_id"
            )
        if item.label in {a.label for a in parsed_accounts}:
            raise ConfigError("Weekly account labels must be unique")
        parsed_accounts.append(item)
    values["weekly_accounts"] = tuple(parsed_accounts)
    remotes = data.get("remotes", [])
    if not isinstance(remotes, list) or len(remotes) > 8:
        raise ConfigError("remotes must be an array of at most eight environments")
    parsed_remotes = []
    for remote in remotes:
        required = {"environment_id", "label", "url", "session_file"}
        if (
            not isinstance(remote, dict)
            or remote.keys() != required
            or any(not isinstance(v, str) or not v for v in remote.values())
        ):
            raise ConfigError("Each remote requires environment_id, label, url, and session_file")
        try:
            parts = urlsplit(remote["url"])
            secure = parts.scheme == "https" or (
                parts.scheme == "http"
                and (
                    parts.hostname in ("localhost", "127.0.0.1", "::1")
                    or (parts.hostname or "").endswith(".ts.net")
                )
            )
            valid = (
                secure
                and parts.hostname
                and parts.path in ("", "/")
                and not (parts.username or parts.password or parts.query or parts.fragment)
                and parts.port != 0
            )
        except ValueError:
            valid = False
        if not valid:
            raise ConfigError(
                "Remote URLs must be HTTPS origins (or encrypted local/tailnet origins), without credentials"
            )
        if remote["environment_id"] in {r.environment_id for r in parsed_remotes}:
            raise ConfigError("Remote environment IDs must be unique")
        parsed_remotes.append(
            RemoteConfig(
                remote["environment_id"],
                remote["label"],
                remote["url"].rstrip("/"),
                file_path(remote["session_file"]),
            )
        )
    values["remotes"] = tuple(parsed_remotes)
    if parsed_remotes:
        try:
            local_id = (database.parent / "environment-id").read_text().strip()
        except OSError:
            local_id = None
        if any(r.environment_id == local_id for r in parsed_remotes):
            raise ConfigError("Do not add the local environment again as a remote")
    for key in ("check_runtime",):
        if key in t3 and type(t3[key]) is not bool:
            raise ConfigError(f"t3.{key} must be true or false")
    if t3.get("check_runtime", True) is False:
        values["runtime_file"] = None
    config = Config(**values)
    if config.weekly_accounts and (not config.weekly_session_file or not config.runtime_file):
        raise ConfigError("Weekly accounts require weekly.session_file and T3 runtime checking")
    if config.time_zone is not None:
        try:
            if not isinstance(config.time_zone, str):
                raise ValueError("Expected an IANA time zone")
            ZoneInfo(config.time_zone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ConfigError("t3.time_zone must be an IANA zone, such as Europe/Zurich") from None
    for key in (
        "verify_tls",
        "notify_completed",
        "notify_failed",
        "notify_attention",
        "subagent_completions",
        "show_titles",
    ):
        if type(getattr(config, key)) is not bool:
            raise ConfigError(f"{key} must be true or false")
    for key, minimum, maximum in (
        ("poll_seconds", 0.5, 60),
        ("token_refresh_seconds", 1, 60),
        ("refresh_seconds", 5, 3600),
        ("timeout_seconds", 0.1, 30),
        ("max_alert_age_seconds", 10, 3600),
        ("completion_seconds", 0.5, 1),
        ("count_seconds", 0.1, 5),
        ("weekly_refresh_seconds", 60, 3600),
        ("weekly_max_age_seconds", 600, 86400),
    ):
        value = getattr(config, key)
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not minimum <= value <= maximum
        ):
            raise ConfigError(f"{key} must be between {minimum} and {maximum}")
    for key in ("device_url", "push_url", "api_key_env", "sound"):
        if not isinstance(getattr(config, key), str):
            raise ConfigError(f"{key} must be a string")
    for key in ("device_url", "push_url"):
        value = getattr(config, key)
        if not value:
            continue
        try:
            parts = urlsplit(value)
            valid = parts.scheme in ("http", "https") and parts.hostname and parts.port != 0
        except ValueError:
            valid = False
        if not valid or parts.username or parts.password or parts.query or parts.fragment:
            raise ConfigError(
                f"lametric.{key} must be an HTTP(S) URL without credentials, query, or fragment"
            )
        if key == "device_url" and parts.path not in ("", "/"):
            raise ConfigError(
                "lametric.device_url must contain only the device origin (e.g. http://192.168.1.50:8080)"
            )
    if not config.device_url and config.push_url:
        from dataclasses import replace

        parts = urlsplit(config.push_url)
        config = replace(config, device_url=f"{parts.scheme}://{parts.netloc}")
    if config.state_file in (
        config.database,
        config.runtime_file,
        config.api_key_file,
        config.usage_session_file,
        config.activity_session_file,
        config.weekly_session_file,
        *(r.session_file for r in config.remotes),
    ):
        raise ConfigError("The bridge state file must be separate from T3's files")
    return config
