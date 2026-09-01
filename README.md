# US Visa Appointment Watcher (Canada)

Watches [ais.usvisa-info.com/en-ca](https://ais.usvisa-info.com/en-ca) for US visa
interview slots and books them. Multiple accounts, multiple applications per
account, multiple consulates per application, a web dashboard, and Telegram
notifications. Deploys with Docker Compose.

Handles **both** AIS flows:

| Flow | Site offers | Supported |
|---|---|---|
| First-time booking (never had an appointment) | "Schedule Appointment" | yes |
| Rescheduling to an earlier date | "Reschedule Appointment" | yes |

## What the dashboard shows

Top-line counters: **accounts**, **applications**, how many are being watched,
how many currently have a matching slot, how many are booked.

Then one table per account listing, for every application:

| Column | Content |
|---|---|
| Application | label, `schedule_id`, and whether it is a first-time booking, a reschedule, or has no action left |
| Status | `watching` / `match` / `booked` / `error` / `inactive` |
| Target consulates | in preference order, e.g. `Toronto, Vancouver, Calgary` |
| Target date | the window, e.g. `2026-09-01 → 2028-12-31` or `before 2026-12-30` |
| Current appointment | what the site shows today, or the slot just booked |
| Availability per consulate | latest poll per consulate: matching date, out-of-window date, `no availability`, or `request failed` |
| Note | last action taken and when it was checked |

Plus a rolling activity log. `/api/state` returns the same data as JSON, and
`/healthz` is an unauthenticated liveness probe that exposes nothing else.

Configured targets appear immediately, before and independently of any sign-in,
so the page is still readable when an account is failing to authenticate.

## Quick start

```sh
cp config.example.yaml config.yaml
$EDITOR config.yaml          # accounts, targets, tokens -- everything
docker compose up -d
docker compose logs -f
```

Dashboard: <http://127.0.0.1:8000>

`test_mode` defaults to **true**: it polls, reports matches and sends
notifications, but never submits a booking. Watch a few sweeps, confirm the
output looks right, then set `test_mode: false`.

## Where things live

| Path | Purpose | Committed? |
|---|---|---|
| `config.yaml` | **all** application config — accounts, credentials, targets, Telegram token, dashboard auth | no, gitignored |
| `.env` | Docker knobs only — published address/port and `TZ`, substituted into `docker-compose.yml` by Compose | no, gitignored |
| `data/store.json` | the JSON store the app writes: discovered applications, last availability per consulate, bookings, activity log | no, gitignored |

Only `config.yaml` needs editing to run. `.env` is optional; without it Compose
falls back to `127.0.0.1:8000` and `TZ=UTC`. Nothing in `.env` affects
application behaviour.

`uv run usvisa --check` validates `config.yaml` and tells you which values are
still shipped placeholders.

## Configuration

Targets cascade through three levels, each overriding the one before:

```yaml
defaults:                     # applies to every account
  consulates: [TRT]
  latest_acceptable_date: 2026-12-30

accounts:
  - name: Primary
    email: you@example.com
    password: your-password
    consulates: [TRT, VAN]    # applies to every application on this account

    applications:
      - schedule_id: "72856817"
        label: Applicant A
        consulates: [TRT, VAN, CAL]      # applies to this application only
        latest_acceptable_date: 2027-06-30
      - schedule_id: "72856546"
        label: Applicant B               # inherits the account settings
```

Omit `applications` entirely and the account is auto-discovered: every
application still offering a scheduling action is watched with the account's
target.

`schedule_id` is the number in the AIS URL:

```
https://ais.usvisa-info.com/en-ca/niv/schedule/72856817/continue_actions
                                               ^^^^^^^^
```

If you would rather not write secrets into the file, any value may be a
`${VAR}` placeholder and will be read from the environment instead — but no
part of the configuration requires it.

### Consulates

Names, consular post codes, airport codes and raw facility ids are all
accepted — `[Toronto, Vancouver]`, `[TRT, VAN]`, `[YYZ, YVR]` and `[94, 95]` are
identical. Order is the preference order.

| Consulate | Code | id |
|---|---|---|
| Calgary | CAL | 89 |
| Halifax | HAL | 90 |
| Montreal | MTL | 91 |
| Ottawa | OTT | 92 |
| Quebec City | QUB | 93 |
| Toronto | TRT | 94 |
| Vancouver | VAN | 95 |

All seven ids were read from the live `facility_id` dropdown on the appointment
form, so unlike earlier versions of this project none of them are guesses.

`prefer_earliest: true` (the default) polls every listed consulate and takes the
globally earliest acceptable slot, breaking ties by list order.
`prefer_earliest: false` walks the list in order and takes the first match.

### Date windows and exclusions

```yaml
earliest_acceptable_date: 2026-09-01
latest_acceptable_date: 2026-12-30
exclusions:
  - [2026-10-01, 2026-10-15]
  - { from: 2026-11-20, to: 2026-11-25 }
```

Both bounds are inclusive. A date inside any exclusion range is refused even if
offered.

### Telegram

Put both values in `config.yaml`:

```yaml
telegram:
  bot_token: "123456789:AAEhBOweik6ad9r_QwerTyUiOpAsDfGhJkL"
  chat_id: "987654321"
  notify_on_no_availability: false
```

Notifications switch on as soon as both are non-empty.

To get them:

1. `/newbot` to [@BotFather](https://t.me/BotFather) and copy the token it
   replies with.
2. **Send your new bot a message first** — it cannot see you otherwise. Then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id` out of the
   JSON. A direct chat id is positive; for a group, add the bot, post a message,
   and use the negative id.

Verify it before relying on it:

```sh
uv run usvisa --test-telegram                      # local
docker compose run --rm watcher --test-telegram    # in the container
```

It checks the token with `getMe`, sends one message, and tells you which of the
two values is wrong if it fails.

You get pinged on startup, when a slot matches, when a booking succeeds or
fails, and when an account has repeated failures. Set
`notify_on_no_availability: true` to also get a message after empty sweeps
(noisy). Notification failures are logged and ignored — they never interrupt
polling.

### CapSolver (optional)

The login page loads reCAPTCHA but leaves it **dormant** on a clean session: the
container is empty and no challenge renders. When the server does decide to arm
it, it injects `data-sitekey` and a v3 `data-action` at request time — so nothing
is hardcoded here, both are read from the live page and handed to
[CapSolver](https://docs.capsolver.com/en/api/). Set `capsolver_api_key` in
`config.yaml` to enable it; without a key an armed challenge fails the login for
that account and the bot retries next sweep.

## The store

`data/store.json` holds everything that is not configuration: which
applications were discovered, what each is targeting, the last availability seen
per consulate, what has been booked, and the activity log. Writes are atomic
(temp file plus rename) and the whole document is restored at startup, so a
restart shows the last known state immediately instead of a blank page until the
first sweep finishes — the sweep counter and log carry over too.

Configuration always wins over the store: labels, consulates and date windows
are re-applied from `config.yaml` on every boot, and accounts or applications you
remove from the config are pruned. Credentials are never written to it, and
e-mail addresses are stored masked.

Mount it on a volume (Compose does this as `watcher-data`) to keep it across
container replacements. Deleting it is safe — you only lose history.

## Metrics

Metrics are pushed to **New Relic** after every sweep — a single HTTPS POST to the
[Metric API](https://docs.newrelic.com/docs/data-apis/ingest-apis/metric-api/report-metrics-metric-api/).
No agent, no sidecar, no scraper. The free tier is far more than this volume
needs (roughly 50 data points per sweep, ~15k a day).

```yaml
newrelic:
  license_key: "eu01xx…NRAL"   # Account settings -> API keys -> "INGEST - LICENSE"
  region: us                   # us | eu | jp -- must match your data centre
  environment: home            # optional, attached to every data point
```

Verify before relying on it:

```sh
uv run usvisa --test-newrelic                      # local
docker compose run --rm watcher --test-newrelic    # in the container
```

Then query it:

```sql
FROM Metric SELECT latest(usvisa.earliest_slot_days)
FACET account, application, consulate TIMESERIES
```

The headline metric answers "how far out is the earliest slot":

| Metric | Meaning |
|---|---|
| `usvisa.earliest_slot_days` | days from today to the earliest slot, ignoring your window |
| `usvisa.earliest_acceptable_slot_days` | same, but only slots inside your window |
| `usvisa.available_days` / `usvisa.acceptable_days` | how many days are offered / acceptable |
| `usvisa.target_deadline_days` | days until `latest_acceptable_date`, negative once it passes |
| `usvisa.booked_slot_days` | days to the appointment actually booked |
| `usvisa.consulate_poll_ok` | 0 when the last request to that consulate failed |
| `usvisa.consulate_status` | always 1; read the `status` attribute (`match` / `none` / `out-of-window` / `error`) |
| `usvisa.application_state` | always 1; read the `state` attribute (`watching` / `match` / `booked` / `error` / `inactive`) |
| `usvisa.account_login_ok` | 0 when the last sign-in failed |
| `usvisa.seconds_since_check` | staleness, useful for an alert if polling silently stops |
| `usvisa.up`, `usvisa.worker_running`, `usvisa.sweeps_total`, `usvisa.test_mode` | process health |

Your dimensions are the attributes: `account`, `application` (plus `schedule_id`
as a stable identity) and `consulate`.

Two behaviours worth knowing:

- **Day counts are computed at emit time**, not when the slot was found, so they
  decay correctly between polls rather than freezing for a whole `poll_interval`.
- **Absent rather than zero.** A consulate with no availability emits no
  `earliest_slot_days` data point at all, because `0` would read as "a slot is
  free today". Use `usvisa.consulate_status` with `status='none'` to detect
  "nothing free", and `usvisa.consulate_poll_ok` for a failed request.

A push failure is logged and ignored — monitoring never interrupts polling, and
repeated failures go quiet after the first few so an outage cannot flood the log.
Note that a `202` only means accepted; if a metric never shows up, query
`NrIntegrationError` in the same account.

`GET /metrics` also returns the same gauges in Prometheus text format. Nothing
scrapes it; it is there so you can `curl` the current values without waiting for
ingestion. It sits behind the same auth as the dashboard.

### A ready-made New Relic dashboard

`monitoring/newrelic-dashboard.json` defines a two-page dashboard (slot trends,
movement per day, target windows, plus a diagnostics page). Stamp your account ID
into it and import:

```sh
python monitoring/render-dashboard.py YOUR_ACCOUNT_ID
```

Then **Dashboards → Import dashboard** and paste the generated
`newrelic-dashboard-<id>.json`.

The account ID must be substituted — the import dialog asks which account to
*save* the dashboard under, but it does **not** rewrite the `accountIds` inside
each widget. Leave the placeholder in and every chart reports *"This widget was
added from an account you don't have access to"*. Your ID is the number in any
New Relic URL (`.../accounts/<id>/...` or `?account=<id>`) and is shown beside the
name in the account switcher.

Two conventions in these queries are deliberate:

- **No query pins its own time range.** New Relic's time picker
  [supersedes any `SINCE`/`UNTIL`](https://docs.newrelic.com/docs/nrql/using-nrql/query-time-range/)
  unless a widget sets *Ignore time picker*, so a hardcoded `SINCE` is either dead
  or quietly makes the widget lie when you change the picker. Every chart follows
  the picker; **set it to `Last 7 days`** or the trend and movement charts have
  nothing to plot.
- **Aggregates use `min()`, not `latest()`.** `earliest_slot_days` is sampled once
  per `poll_interval`, and a chart bucket holds several samples. `latest()` keeps
  only the last one, so a slot that appeared for a single sweep and was taken
  before the next would vanish from the graph — which is exactly the event worth
  seeing. `latest()` is used only on the "right now" tile, where current state is
  the question. The **Now vs best seen** table shows both side by side: a large
  gap means an earlier slot came and went inside the window.

The **In window (d)** column stays blank until a slot actually falls inside your
configured date range, and the **Movement** chart needs more than a day of history
before `derivative` has anything to plot. Both are expected, not missing data.

### If the push fails with "connection refused"

New Relic is an analytics product, so its domains sit on standard tracker
blocklists. An ad blocker on the host — AdGuard, Pi-hole, a hosts entry —
resolves `metric-api.newrelic.com` to `0.0.0.0`, and connecting there reports
`[Errno 111] Connection refused`, which looks like a New Relic outage rather than
local filtering.

**This is handled automatically.** With `dns_mode: auto` (the default) the client
notices the blackholed answer and re-resolves over DNS-over-HTTPS, which runs on
port 443 where a port-53 blocker cannot see it:

```
new relic: metric-api.newrelic.com is blackholed by a DNS-level blocker;
           re-resolving over DNS-over-HTTPS
new relic: resolved metric-api.newrelic.com to 162.247.241.10 over DoH
pushed 29 data point(s) to New Relic
```

TLS is untouched: SNI, the `Host` header and certificate verification all still
use the real hostname, so only address resolution is bypassed — it is not a trust
bypass. Cloudflare is tried first, then Google. The address is cached for five
minutes because New Relic rotates it, which is also why pinning it in
`extra_hosts` is a bad idea.

`dns_mode` options:

| Value | Behaviour |
|---|---|
| `auto` | system DNS, falling back to DoH when the answer is blackholed (default) |
| `doh` | always resolve over DoH, skipping system DNS entirely |
| `system` | system DNS only; fail if it is blocked |

Note that changing the container's `dns:` does **not** help, because these
blockers intercept port 53 at the network level regardless of which upstream
resolver is configured. The lasting fix on the host side is to allowlist
`newrelic.com` in the blocker, or exclude the Docker/WSL processes from its
filtering.

## Running without Docker

```sh
uv sync
uv run usvisa --check           # validate config and exit
uv run usvisa --test-telegram   # send one test notification and exit
uv run usvisa --test-newrelic   # push metrics once and exit
uv run usvisa --once            # one sweep, no web server
uv run usvisa                   # poll + serve the dashboard
uv run pytest
```

## Security

- **The dashboard has no authentication unless you configure it.** Set
  `dashboard.username` / `dashboard.password` in `config.yaml`; the page shows a
  warning banner and the log prints a warning while they are unset.
- Compose publishes to `127.0.0.1:8000` only. Set `DASHBOARD_BIND=0.0.0.0` in
  `.env` *after* enabling auth, and prefer a reverse proxy with TLS over exposing
  it directly.
- The dashboard masks account e-mails and never renders passwords or CSRF
  tokens. The store on disk contains no credentials. Container stdout prints the
  configured e-mail once at startup.
- The container runs as uid 10001 with a read-only root filesystem, all
  capabilities dropped and `no-new-privileges`. Only `/app/data` (the store
  volume) and `/tmp` are writable.
- `config.yaml`, `.env` and `data/` are gitignored. `config.yaml` holds your
  password in plaintext — treat the file accordingly and do not commit it.

## How it works

Everything is plain HTTP; there is no browser and no Selenium.

| Step | Request |
|---|---|
| sign in | `POST /en-ca/niv/users/sign_in` — `user[email]`, `user[password]`, `policy_confirmed=1`, CSRF from `<meta name="csrf-token">` |
| verify session | `GET /en-ca/niv` — redirects to `/niv/groups/{group_id}` when signed in |
| list applications | parse `/niv/schedule/{id}/continue_actions` links off the group page |
| classify | `GET .../continue_actions` — a `.../continue` link labelled "Schedule Appointment" or "Reschedule Appointment" means actionable; no such link means completed |
| availability | `GET .../appointment/days/{facility}.json` → `[{"date":"YYYY-MM-DD","business_day":true}]` |
| times | `GET .../appointment/times/{facility}.json?date=…` → `{"available_times":[…]}` |
| book | `POST .../appointment` — `authenticity_token`, `confirmed_limit_message`, `use_consulate_appointment_capacity`, `appointments[consulate_appointment][facility_id]`, `[date]`, `[time]` |

Two details worth knowing if you modify this:

- `facility_id` is **required**. The date field stays empty until it is set,
  which is why simply posting a date does nothing.
- The sign-in form is `data-remote="true"`, so the controller answers
  `text/javascript`. Requesting `text/html` makes Rails raise `UnknownFormat` and
  return a 404 page, which looks nothing like an auth failure. Session validity
  is therefore confirmed with a separate `GET /en-ca/niv` rather than by parsing
  the POST response.

An empty availability list is a normal answer, not an error — several Canadian
posts routinely return zero days — and the dashboard reports "no availability"
and "request failed" as distinct states.

## Caution

Rescheduling is limited to a small number of attempts per application. Keep
`test_mode: true` until you are sure your date window is one you actually want,
because the bot will take the earliest acceptable slot it sees.

## Disclaimer

Provided as-is, with no warranty of any kind. You use it at your own risk, and
the authors accept no liability for any consequence of running it. Understand
what it does before pointing it at a real account, and consider the terms of
service of the system you are automating.

## History

Version 2 is a rewrite. The original Selenium implementation, the Gmail
notifier and the CLI-only interface are gone; `requests`, Telegram and the
dashboard replace them.

Thanks to [@jywyq](https://github.com/jywyq) for the original notification
feature, [@bsingh-kpt](https://github.com/bsingh-kpt) for fixing the legacy
rescheduler in 2025, and [@trungnguyen21](https://github.com/trungnguyen21) and
[@saroopskesav](https://github.com/saroopskesav) for the original consulate
numbers. The Gmail helper was a vendored copy of
[paulc/gmail-sender](https://github.com/paulc/gmail-sender).
