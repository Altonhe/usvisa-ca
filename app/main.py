"""Entry point: starts the polling worker and serves the dashboard.

    uv run usvisa                 # serve dashboard + poll
    uv run usvisa --check         # validate config and exit
    uv run usvisa --once          # one sweep, print results, exit (no server)
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path
from typing import List, Optional

import uvicorn

from .config import Config, ConfigError, load_config
from .dashboard import create_app
from .metrics import collect
from .newrelic import NewRelicClient
from .notifier import TelegramNotifier
from .store import Store
from .worker import Worker


def _print(message: str) -> None:
    print(message, flush=True)


PLACEHOLDERS = {
    "replace_me", "changeme", "change-me", "change_me", "todo", "xxx",
    "you@example.com", "other@example.com", "your-password",
    "change-me-before-exposing",
}


def _looks_unset(value: str) -> bool:
    return value.strip().lower() in PLACEHOLDERS


def warn_about_placeholders(config: Config) -> List[str]:
    """Values that are still the shipped placeholders, so nothing runs silently wrong."""
    warnings: List[str] = []
    for acc in config.accounts:
        if _looks_unset(acc.email):
            warnings.append(f"account {acc.name!r}: email is still a placeholder")
        if _looks_unset(acc.password):
            warnings.append(f"account {acc.name!r}: password is still a placeholder")
    if config.dashboard.username and _looks_unset(config.dashboard.password):
        warnings.append("dashboard.password is still a placeholder")
    return warnings


def describe(config: Config) -> None:
    _print(f"accounts:      {len(config.accounts)}")
    for acc in config.accounts:
        if acc.auto_discover:
            scope = "auto-discover every actionable application"
        else:
            scope = f"{len(acc.applications)} application(s)"
        _print(f"  - {acc.name} <{acc.email}>: {scope}")
        _print(
            f"      default target: {acc.target.describe_consulates()} "
            f"| {acc.target.describe_window()}"
        )
        for app in acc.applications:
            _print(
                f"      {app.schedule_id} {app.label or '(no label)'}: "
                f"{app.target.describe_consulates()} | {app.target.describe_window()}"
            )
            for start, end in app.target.exclusions:
                _print(f"          excluding {start} .. {end}")
    _print(f"poll interval: {config.poll_interval}s")
    _print(f"store:         {config.store_file}")
    _print(f"capsolver:     {'enabled' if config.capsolver_enabled else 'disabled'}")
    _print(f"telegram:      {'enabled' if config.telegram.enabled else 'disabled'}")
    _print(
        "new relic:     "
        + (f"enabled ({config.newrelic.region})"
           if config.newrelic.enabled else "disabled")
    )
    _print(f"dashboard:     http://{config.dashboard.host}:{config.dashboard.port}")
    _print(
        "               "
        + ("HTTP Basic auth enabled" if config.dashboard.auth_enabled else "NO AUTH")
    )
    _print(
        "mode:          "
        + ("TEST (nothing will be booked)" if config.test_mode else "LIVE (will book)")
    )


def build_newrelic(config: Config) -> Optional[NewRelicClient]:
    if not config.newrelic.enabled:
        return None
    common = {"service.name": "usvisa-watcher"}
    if config.newrelic.environment:
        common["environment"] = config.newrelic.environment
    return NewRelicClient(
        config.newrelic.license_key,
        region=config.newrelic.region,
        dns_mode=config.newrelic.dns_mode,
        common_attributes=common,
        logger=_print,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="usvisa", description=__doc__)
    parser.add_argument(
        "-c", "--config", type=Path, default=None,
        help="path to config.yaml (default: $USVISA_CONFIG or ./config.yaml)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="validate the configuration and exit",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="run a single sweep without starting the web server",
    )
    parser.add_argument(
        "--test-telegram", action="store_true",
        help="send one test message to Telegram and exit",
    )
    parser.add_argument(
        "--test-newrelic", action="store_true",
        help="push the current metrics to New Relic once and exit",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        _print(f"configuration error: {exc}")
        return 2

    describe(config)

    placeholders = warn_about_placeholders(config)
    if placeholders:
        _print("")
        for warning in placeholders:
            _print(f"WARNING: {warning}")
        _print("Edit config.yaml -- polling cannot succeed until these are real.")

    if args.check:
        _print("")
        if placeholders:
            _print("configuration parses, but placeholder values remain")
            return 1
        _print("configuration is valid")
        return 0

    if args.test_telegram:
        _print("")
        notifier = TelegramNotifier(config.telegram, logger=_print)
        if not notifier.enabled:
            _print(
                "Telegram is not configured. Set TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_CHAT_ID in .env (both must be non-empty)."
            )
            return 2
        if not notifier.verify():
            _print("the bot token was rejected; check TELEGRAM_BOT_TOKEN")
            return 1
        sent = notifier.send(
            "<b>Test message</b>\nUS visa watcher can reach this chat. "
            f"Accounts configured: {len(config.accounts)}, "
            f"applications: {config.total_applications()}."
        )
        if sent:
            _print("test message sent -- check your Telegram")
            return 0
        _print(
            "the token is valid but the message was rejected. TELEGRAM_CHAT_ID is "
            "probably wrong, or you have not sent your bot a message yet."
        )
        return 1

    if not config.dashboard.auth_enabled and not args.once:
        _print(
            "\nWARNING: the dashboard has no authentication. It exposes masked "
            "emails, schedule ids and appointment dates to anyone who can reach "
            "the port. Set dashboard.username/password in config.yaml, and keep "
            "the published port on a trusted network."
        )

    store = Store(path=config.store_file)
    if store.load():
        _print(f"\nrestored store from {config.store_file}")
    store.test_mode = config.test_mode
    notifier = TelegramNotifier(config.telegram, logger=_print)
    newrelic = build_newrelic(config)

    if args.test_newrelic:
        _print("")
        if newrelic is None:
            _print(
                "New Relic is not configured. Set newrelic.license_key in "
                "config.yaml (a licence/ingest key, not a user API key)."
            )
            return 2
        samples = collect(store.snapshot())
        if newrelic.send(samples):
            _print(f"pushed {len(samples)} data point(s) to New Relic")
            _print(
                "Query in New Relic:\n"
                "  FROM Metric SELECT latest(usvisa.earliest_slot_days)\n"
                "  FACET account, application, consulate TIMESERIES"
            )
            return 0
        _print("push was rejected; see the error above")
        return 1

    notifier.verify()
    if newrelic:
        newrelic.verify()
    worker = Worker(config, store, notifier, newrelic=newrelic)

    if args.once:
        _print("\nrunning a single sweep\n")
        worker._seed_state()  # noqa: SLF001 - deliberate single-shot use
        worker._sweep()       # noqa: SLF001
        store.save()
        _print(f"\nstore written to {config.store_file}")
        return 0

    worker.start()

    def shutdown(signum, _frame):
        _print(f"received signal {signum}, stopping worker")
        worker.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):
            # Not the main thread, or the platform lacks the signal.
            pass

    api = create_app(config, store, worker)
    _print("")
    try:
        uvicorn.run(
            api,
            host=config.dashboard.host,
            port=config.dashboard.port,
            log_level="info",
            access_log=False,
        )
    finally:
        worker.stop()
        store.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
