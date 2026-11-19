"""One-shot migration: move service secrets from config.yaml to Infisical.

    uv run python scripts/migrate_service_secrets.py [--rewrite-config]

Uploads, from the current config.yaml (the `secrets:` block names the project):

    network.proxy                 -> AIS_PROXY
    capsolver_api_key             -> CAPSOLVER_API_KEY
    newrelic.license_key          -> NEW_RELIC_LICENSE_KEY
    $STARLINK_GATEWAY_URL / _KEY  -> STARLINK_GATEWAY_URL / STARLINK_GATEWAY_KEY
                                     (from the environment, if set)

reads them back to verify, and with --rewrite-config replaces each inline
value in config.yaml by its switch (use_proxy / use_gateway / enabled: true),
writing a backup next to the file first.

Needs INFISICAL_CLIENT_ID and INFISICAL_CLIENT_SECRET in the environment.
No secret value is ever printed.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import (CAPSOLVER_SECRET, GATEWAY_KEY_SECRET,  # noqa: E402
                        GATEWAY_URL_SECRET, NEW_RELIC_SECRET, PROXY_SECRET,
                        _interpolate)
from app.secrets import InfisicalClient, InfisicalConfig  # noqa: E402


def wanted_secrets(raw: dict) -> dict:
    net = raw.get("network") or {}
    nr = raw.get("newrelic") or {}
    found = {
        PROXY_SECRET: net.get("proxy"),
        CAPSOLVER_SECRET: raw.get("capsolver_api_key"),
        NEW_RELIC_SECRET: nr.get("license_key"),
        GATEWAY_URL_SECRET: net.get("gateway_url") or os.getenv(GATEWAY_URL_SECRET),
        GATEWAY_KEY_SECRET: net.get("gateway_key") or os.getenv(GATEWAY_KEY_SECRET),
    }
    return {k: str(v).strip() for k, v in found.items() if v and str(v).strip()}


def rewrite_config(path: Path, uploaded: set) -> list:
    """Swap each inline secret line for its switch; returns what changed."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out, section, changed, gateway_done = [], "", [], False
    for line in lines:
        top = re.match(r"^([A-Za-z_]\w*)\s*:", line)
        if top:
            section = top.group(1)
        m = re.match(r"^(\s*)(\w+)\s*:", line)
        indent, key = (m.group(1), m.group(2)) if m else ("", "")
        if section == "capsolver_api_key" and top and CAPSOLVER_SECRET in uploaded:
            out.append("capsolver:\n  enabled: true\n")
            changed.append("capsolver_api_key -> capsolver.enabled: true")
            continue
        if section == "network" and indent and key == "proxy" and PROXY_SECRET in uploaded:
            out.append(f"{indent}use_proxy: true\n")
            changed.append("network.proxy -> network.use_proxy: true")
            continue
        if section == "network" and indent and key in ("gateway_url", "gateway_key"):
            if not gateway_done:
                out.append(f"{indent}use_gateway: false\n")
                gateway_done = True
            changed.append(f"network.{key} removed")
            continue
        if section == "newrelic" and indent and key == "license_key" \
                and NEW_RELIC_SECRET in uploaded:
            out.append(f"{indent}enabled: true\n")
            changed.append("newrelic.license_key -> newrelic.enabled: true")
            continue
        out.append(line)
    path.write_text("".join(out), encoding="utf-8")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--rewrite-config", action="store_true")
    args = ap.parse_args()

    path = Path(args.config)
    raw = _interpolate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    secrets_raw = raw.get("secrets")
    if not secrets_raw:
        raise SystemExit("config.yaml has no secrets: block; run migrate_to_infisical.py first")
    client = InfisicalClient(InfisicalConfig.from_raw(secrets_raw))

    wanted = wanted_secrets(raw)
    existing = client.fetch_all()
    for key, value in wanted.items():
        state = "unchanged" if existing.get(key) == value else (
            "updated" if key in existing else "created")
        if state != "unchanged":
            client.put(key, value, exists=key in existing)
        print(f"  {key}: {state}")
    after = client.fetch_all()
    bad = [k for k, v in wanted.items() if after.get(k) != v]
    if bad:
        print(f"VERIFY FAILED for: {', '.join(bad)}; config.yaml left untouched")
        return 1
    print(f"verified {len(wanted)} secret(s) read back identical")

    if args.rewrite_config:
        backup = path.with_name(path.name + ".pre-service-secrets.bak")
        shutil.copy2(path, backup)
        for change in rewrite_config(path, set(wanted)):
            print(f"  {change}")
        print(f"backup with the old values at {backup}; delete it once things run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
