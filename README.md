# T3 LaMetric

A local companion that puts T3 Code activity, daily tokens, and weekly account usage on a LaMetric TIME. It works across providers and explicitly configured machines, with no AI calls.

[GitCafe](https://git.cafe/saphitv/t3-lametric) · [GitHub](https://github.com/saphitv/t3-lametric)

**Machine coverage:** the bridge combines the host machine's T3 server with explicitly configured remote environments. Each remote has a live, authenticated T3 activity stream and contributes to completion notifications, pending questions, the running-session count, and the daily token sum. A sleeping or disconnected remote is excluded from live activity. Weekly quotas remain readings for the selected provider accounts and are not added again per machine.

## Display behavior

1. **Needs input:** a plain pale blue `?` icon sits beside `Input`. A single static indicator covers all pending questions, approvals, sign-ins, or secret requests, without showing their text or a count. It stays until the last actionable request is resolved, except for brief completion/count cues. The underlying widget also switches to Input during those cues, so their expiry cannot reveal old token statistics. Token refreshes pause throughout pending input. Nonblocking questions left over from completed, cancelled, or otherwise inactive chats are ignored; blocking requests remain visible even if their chat has ended.
2. **A chat finishes:** a native LaMetric notification shows `Done` with a green checkmark for **0.8 seconds**, then the remaining session count beside a blue spinner with one blank left column for **1 second**. No count frame is shown at zero. These cues briefly interrupt the input indicator, which resumes afterward. Each accepted completion gets its own sequence. Failed chats use a brief red failure cue. Native cues use warning priority and explicit dismissal of only their own notification IDs; their queue lifetime is not used as a display timer. Done uses a one-second native frame and an independent 0.8-second dismissal timer, so slow T3 reads do not prolong it. The interval is protected from new work, pending input, failure cues, and disconnection updates; those changes are processed afterward. New completions join the event queue without restarting the current timer. `completion_seconds` accepts only 0.5–1 second. One native cycle remains a fallback if the bridge stops before dismissal.
3. **Sessions running:** a pure white three-bar usage icon sits beside today's **combined token total**, with no `TOK` prefix, alternating every **5 seconds** with today's **combined USD cost** (for example, `$12.34`). Cost comes from T3's Usage summary, including its model pricing estimates; it is not a subscription charge. Missing or unpriced costs show `$ ?`. Both views use the same fetched daily summary and the same configured machines. Input and completion/count cues interrupt the rotation; it resumes with tokens. The stats PNG is 9×8: one blank left column, three two-pixel-wide bars, and one-column gaps. Its last white column extends into the usual spacer before the number. The device API accepts and retains this wider PNG; firmware rendering must be confirmed visually because the documented icon size is 8×8. Totals always keep one decimal, such as `512.0M`. Larger changes count up through at most three distinct rounded values over 0.36 seconds, then settle on the fetched total. Small changes go straight to the new value; identical rounded totals cause no update. No provider breakdown, separate output counter, or context frame.
4. **Idle:** a round character with smoothly rotating blue/purple/pink colors and two tiny white eyes glances left, right, up and down, gently bobs, and blinks beside `T3`. The 7.5-second loop leaves one blank pixel column on the left. With weekly accounts configured, 7.5 seconds of dots alternate with a 2.5-second account peek: `CP100%`, `CW82%`, `CL73%`, for example. These percentages mean **weekly quota remaining**. A little battery face blinks; its fill follows the remaining quota, with green at 30% or more, amber below 30%, and red below 10%. The three-account cycle lasts 30 seconds, mostly dots. The clock plays the uploaded frames and GIFs independently.

TIME's text pixels are white-only. The green completion checkmark, spinner, and dots use its 8×8 RGB icon area. The running-chat number uses the white text area.

Starting a new chat or a fresh turn briefly shows a blue rotating ring with one blank left column in the 8×8 icon area and just the running-chat number in the white text area for **1 second**, then returns to tokens (or pending input). There is no `Working` label. The spinner has a bright rotating head and a fading blue trail, and keeps the same padded size for all counts. The clock plays this GIF locally without extra polling. Queued work waits until it starts, and delegates do not trigger the cue. The displayed count follows the latest live total. New starts, completions, and failures join the same FIFO without interrupting the active event.

The event queue holds **at most five waiting events**, plus the event currently on screen. Events play first in, first out. A completion event includes its Done/Failed cue and the brief live count that follows, if any chats remain. New events arriving while all five waiting slots are occupied are **dropped permanently**, rather than retried after a slot opens or after a restart. The handled-ID ledger records these rejections. The bridge logs how many events were dropped. Queued events expire after the normal alert-age limit (120 seconds by default), so a disconnected clock cannot accumulate an old backlog. Continuous token, input, and idle views do not occupy queue slots; answered questions are never replayed from the queue. Only one bridge cue is sent to the clock at a time.

Active sessions are counted by their chat, so a chat and its delegates count once. Independent forked chats remain separate sessions. Queued work and sessions blocked on input are excluded from the count. Root chat completions trigger `Done`; individual subagents do not by default.

Activity changes arrive on T3's persistent **`orchestration.subscribeShell` WebSocket stream**, rather than waiting for a five-second poll. The listener keeps only status signatures, filters message-text updates, and wakes the bridge on run/request changes. A small read-only projection lookup supplies the current activity and question text. A once-per-minute activity read is a safety check; a disconnected stream reconnects automatically and falls back to five-second activity polling.

The **token total refreshes about once per second only while the token or cost view is selected and its clock widget is visible** (`bridge.token_refresh_seconds`, default 1). It pauses during completion/count cues, pending input, and idle animations. While the logical token or cost view is selected, the bridge checks which clock app is visible at most once every five seconds; switching apps can take up to five seconds to pause/resume usage reads. Usage failures back off at least five seconds, and a slow request increases the delay to ten times its elapsed duration, so usage requests occupy at most roughly 10% of wall time. This is a latency budget, not a measurement of T3 server CPU usage. Fallback activity polling remains five seconds independently.

Activity events interrupt in-flight usage requests, and completion/input cues cancel a count-up immediately. During Done, activity changes are retained and their projection reads wait until its display interval ends. Count-up steps reuse fetched totals and never read T3 again. My Data DIY's documented frame API does not expose a vertical digit-wheel transition, so the default remains a short stepped count-up. An experimental pixel reel is available below. The clock receives changed dashboard values plus a once-per-minute heartbeat; it plays idle frames locally. No browser or LLM runs as part of this automation.

Measured on October 28, 2026, using one cold and five warm requests per server: median warm `server.getUsageSummary` round-trip was **39.22 ms locally** (36 ms server-reported scan) and **29.09 ms on remote-workstation** (13 ms scan), with roughly 1–2 ms of bridge CPU time per call. Local and remote usage reads are sequential, so a combined warm refresh was approximately 70 ms. These timings are observations, not guarantees; backoff handles slower refreshes. No CLI process is launched for each update: the bridge reuses authenticated T3 WebSocket RPC connections.

## Install and configure

Python 3.11+ is required on the machine running T3 Code, together with a LaMetric TIME on the same reachable network.

```sh
git clone https://git.cafe/saphitv/t3-lametric.git
cd t3-lametric
python3 -m venv .venv
.venv/bin/python -m pip install -e .
mkdir -p ~/.config/t3-lametric
cp config.example.toml ~/.config/t3-lametric/config.toml
```

In the LaMetric mobile app, install **My Data DIY**, choose **HTTP / Local Push**, and copy its local widget URL into `lametric.push_url`. Set `lametric.device_url` to the clock's origin, such as `http://lametric.local:8080`.

Provide the device API key through `LAMETRIC_API_KEY` or `lametric.api_key_file`. A private key file (`chmod 600`) works best for a background service. The key is available under Device → Settings → Device API Key, or in the LaMetric developer portal under My Devices. Neither the key nor a T3 bearer token belongs in a URL or the repository.

### Try vertical rolling digits

**Experimental status:** on the tested TIME sa8 running OS 3.2.8, stream start/stop requests were accepted, but GIF and RAW trials did not produce a confirmed visible per-digit roll. The test installation has this option disabled. API acceptance and renderer tests alone do not establish that a firmware version can display the effect.

Set `token_reel = true` in `[bridge]` to try the experimental half-second digit reel on TIME firmware advertising a 37×8 LMSP pixel canvas. Only changing digits move vertically; the chart icon, unchanged digits, decimal point, and unit stay still. Unit or text-width changes jump straight to the new value.

The reel uses nine small RAW UDP frames, not a full-width icon GIF or scrolling text. A background worker sends about 8 KiB per changed displayed number. It reuses the fetched daily total, makes no extra T3 usage or provider calls, and runs only while the token view is visible. The exact final total is saved to the DIY widget first. An independent half-second timer stops the overlay; Input, Done, and other normal updates cancel it before taking over. The bridge does not replace an already-running external stream. Unsupported or failed streaming falls back to normal text updates. Set `token_reel = false` to keep the standard count-up; it is off by default.

This temporarily switches the device to streaming mode. Normal shutdown stops it. If the host stops abruptly during playback, the device may need its stream stopped before returning to its normal apps. The LaMetric streaming documentation is written for SKY; TIME behavior varies by firmware, so capability discovery and a physical display check are needed.

### Use T3's own total

For the same daily sum as the **local environment** in T3's Usage page, create a dedicated session with only `diagnostics:read`. With `t3` installed and running:

```sh
(
  umask 077
  t3 auth session issue --scope diagnostics:read --ttl 365d \
    --label 'T3 LaMetric read-only usage' --json \
    > ~/.config/t3-lametric/t3-usage-session.json
)
```

Set these in `[t3]`:

```toml
usage_session_file = "~/.config/t3-lametric/t3-usage-session.json"
time_zone = "Europe/Zurich" # use your local IANA time zone
```

This asks the same `server.getUsageSummary` RPC as T3's Usage page over one reusable, authenticated loopback connection. T3 performs the history scanning and deduplication. Input, cache reads/writes, and output are summed; reasoning is already part of output and is not added twice. Remote linked environments are outside this local sum. Compare with the same date, timezone, and environment filter in T3.

The bridge skips token requests while the token and cost views are hidden, including during completion/count cues. If usage refresh fails, it keeps the last known total and reports the problem in its log. A normal activity interruption is not logged as a usage failure. Renew the read-only session before its configured expiry. Without `usage_session_file`, a fallback sums normalized saved T3 turns; that narrower total can differ from the Usage page.

### Live completion and input events

The activity subscription needs a T3 session with **`orchestration:read`**. The weekly session below already has that scope and is reused automatically. To select a different private session, set this in `[t3]`:

```toml
activity_session_file = "~/.config/t3-lametric/t3-weekly-session.json"
```

No T3 server configuration, provider hook, or model invocation is required. The listener uses the existing local server runtime port, acknowledges stream chunks, answers heartbeats, and reconnects after disconnection. Completed runs still use the persistent delivery ledger, so reconnects and restarts do not replay already delivered cues. Without an activity credential, five-second activity polling remains available.

### Weekly account peeks while idle

Create a separate private T3 session for its published provider/Hub status:

```sh
(
  umask 077
  t3 auth session issue --scope orchestration:read --ttl 365d \
    --label 'T3 LaMetric read-only weekly limits' --json \
    > ~/.config/t3-lametric/t3-weekly-session.json
)
```

Add `[weekly]` settings like these, using exact instance IDs and Hub source/account IDs from T3:

```toml
[weekly]
session_file = "~/.config/t3-lametric/t3-weekly-session.json"
refresh_seconds = 120
max_age_seconds = 1800
accounts = [
  { label = "CP", instance_id = "codex" },
  { label = "CW", instance_id = "codex_work" },
  { label = "CL", source_id = "YOUR_HUB_SOURCE_ID", account_id = "YOUR_CLAUDE_ACCOUNT_ID" },
]
```

`CP`/`CW` can stand for Codex Personal/Work and `CL` for Claude. Labels support one or two uppercase letters/digits. Select up to six accounts, each by `instance_id` or by both `source_id` and `account_id`; selectors are exact so other accounts and Hub duplicates stay out of the cycle. A Claude instance that publishes limits directly can use `instance_id` instead.

The bridge briefly subscribes to T3's read-only `subscribeServerConfig` stream with `usageLimitSources` enabled. It reads the initial published provider/Hub snapshots and closes that subscription; the separate live activity subscription remains connected. It never starts a provider session, changes accounts, redeems credits, or reads provider credentials. Background quota reads occur only while idle, at most once every two minutes. Results are cached and reused between attempts; a temporarily unavailable account keeps its previous reading until that reading expires. Completed, failed, and interrupted attempts all wait at least two minutes before the next attempt. An activity event can cancel a read, but does not bypass that limit.

Only the overall **weekly** window is shown. Session limits and Claude's overlapping model-specific weekly windows are excluded. Missing, unsupported, expired, or older-than-30-minute readings show `?`, rather than a guessed zero or a presumed full allowance after reset. Temporary read failures retain recent readings until they expire. Freshness depends on T3's own provider/Hub refreshes. `CP ?`, for example, means Codex Personal's weekly reading is unavailable or stale; it does not mean that the account has no quota left. Fresh data returns automatically on the next idle refresh. Weekly peeks appear only while idle.

### Add another machine

The remote T3 server needs a dedicated bearer session with only `orchestration:read` and `diagnostics:read`. Issue it using `t3 auth session issue` on that machine, save the JSON privately on the bridge host (`chmod 600`), and add:

```toml
[[remotes]]
environment_id = "EXACT_REMOTE_ENVIRONMENT_ID"
label = "remote-workstation"
url = "https://YOUR_T3_CONNECT_HOST"
session_file = "~/.config/t3-lametric/remote-workstation-readonly.json"
```

The bridge verifies the environment identity before subscribing to its shell stream. Remote inputs fetch a bounded thread snapshot only when a question needs its text; only the matching question/prompt or secret label/reason is retained. Questions carry the machine label. No remote database mount, remote bridge daemon, SSH session, model invocation, or provider hooks are used during monitoring.

IDs are scoped by environment so unrelated chats cannot collide. Child chats are grouped into their session within each environment. The initial remote snapshot establishes a baseline; later completions are deduplicated by the existing delivery ledger. Connections reconnect automatically. A dropped connection clears that machine's live agents/questions immediately when the disconnect is detected; an unresponsive connection is detected by heartbeat timeout.

Daily totals use the same local-day/timezone request for every configured environment and add each environment once. A remote that is offline makes no token requests. Its last known total for the current day is retained in memory, and the display continues to show the last known sum without a `+` suffix. Diagnostics retain the incomplete-reading flag. At local midnight, yesterday's cached values are cleared. Histories independently copied into multiple environments are outside this environment-level deduplication.

Connecting another machine in T3's UI does not automatically add it here: configure its API endpoint and read-only bridge session once. A T3 peer link's agent credential is not reused by this daemon. The bridge session has its own expiry and can be revoked separately in that remote's Connections. This bridge still runs on its host machine; that machine must stay awake to update the physical clock.

### Check the connection

```sh
.venv/bin/t3-lametric doctor --device
.venv/bin/t3-lametric status
.venv/bin/t3-lametric run --dry-run --once
.venv/bin/t3-lametric run
```

`status`, `frames`, and dry runs never write to the clock. When configured, they can read T3's usage RPC. `run --dry-run` uses an in-memory alert ledger and prints proposed display updates; it does not save delivery state. `test-device` sends a sample dashboard and notification.

An alternate config goes before the command: `t3-lametric --config /absolute/path/config.toml status`. Relative paths resolve against the configuration's directory. `T3CODE_HOME` is honored when no database override is set. Local HTTP uses port 8080; HTTPS uses 4343, with `verify_tls = false` needed for a self-signed device certificate.

## Run automatically on macOS

Keep the installation at a stable location and configure `api_key_file`; launchd does not inherit your interactive shell environment.

```sh
.venv/bin/t3-lametric service-file --output ~/Library/LaunchAgents/local.t3-lametric.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/local.t3-lametric.plist
```

The generator writes the plist without enabling it. Logs default to `~/.local/state/t3-lametric/bridge.log`. Stop and disable it with:

```sh
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/local.t3-lametric.plist
```

Linux can run `t3-lametric run` under a user service manager.

## State and compatibility

The activity reader opens `statev2.sqlite` with **SQLite `mode=ro` and `query_only`**, closes its short transaction before contacting the clock, and reads only the projections needed for activity and pending requests. Pending-request reads select question/prompt fields, never answers or credential values. With usage RPC configured, the bridge skips provider-turn history queries entirely. It never starts a T3 server or writes to T3's application database directly.

Its own handled-event ledger lives outside T3's data directory. Initial startup establishes a baseline instead of replaying old completions, while an existing unanswered question is displayed immediately. Successful delivery is recorded only after the clock accepts the update; explicit queue overflow rejections are also remembered. Offline deliveries retry while recent. `T3 OFFLINE` and `T3 DATA?` replace stale data when needed. A stopped bridge cannot clear the clock, so keep the service running for offline detection.

The activity adapter uses T3 0.1.x's internal v2 projection schema and checks required fields. A future migration may require an adapter update. This is an unofficial companion, not a built-in T3 extension. The full display sequence needs My Data DIY; without a widget URL, only notification delivery is available.

## Development

Clone from GitCafe for development, or use the GitHub URL above. Personal configuration, device keys, T3 sessions, and runtime state belong outside this checkout.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/ruff check .
.venv/bin/ruff format --check t3_lametric tests
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip wheel --no-deps . --wheel-dir dist
```

Use the registered T3 project's Setup, Test, Lint, and Build scripts for the same commands. The live background service uses a separate stable installation; changing source files does not restart or reinstall it.

Tests cover timed native cues and dismissal, input indicator resumption and question-text suppression, event filtering, visible-token scheduling, interruption of in-flight token reads, session deduplication, native animation delivery, T3 usage accounting, weekly account selection and expiry, cached quota backoff, persistence, and a fake LaMetric HTTP device. No live database writes or real credentials are required.

References: [LaMetric authorization](https://lametric-documentation.readthedocs.io/en/latest/reference-docs/device-authorization.html), [notifications](https://lametric-documentation.readthedocs.io/en/latest/reference-docs/device-notifications.html), [DIY frames/GIFs](https://help.lametric.com/support/solutions/articles/6000225467-my-data-diy), [streaming protocol](https://docs.lametric.com/en/latest/reference-docs/device-stream.html), [T3 RPC contracts](https://github.com/pingdotgg/t3code/blob/main/packages/contracts/src/rpc.ts), [official OpenAI documentation for account rate-limit fields](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt).
