"""One-shot migration: move account e-mails/passwords from config.yaml to Infisical.

    uv run python scripts/migrate_to_infisical.py --project-id <id> [--environment prod]
        uploads every account's email/password as <NAME>_EMAIL / <NAME>_PASSWORD
        and reads them back to verify.
    ... --rewrite-config
        additionally strips email:/password: from the accounts section of
        config.yaml (backup written next to it) and adds a secrets: block.

Needs INFISICAL_CLIENT_ID and INFISICAL_CLIENT_SECRET in the environment.
No secret value is ever printed.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import _interpolate  # noqa: E402
from app.secrets import InfisicalClient, InfisicalConfig, secret_key  # noqa: E402


def rewrite_config(path: Path, secrets_block: str) -> int:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out, in_accounts, removed = [], False, 0
    for line in lines:
        top = re.match(r"^[A-Za-z_]\w*\s*:", line)
        if top:
            in_accounts = line.startswith("accounts:")
        if in_accounts and re.match(r"^\s+-\s+(email|password)\s*:", line):
            raise SystemExit("an account list item starts with email/password; "
                             "put `name:` first and re-run")
        if in_accounts and re.match(r"^\s+(email|password)\s*:", line):
            removed += 1
            continue
        out.append(line)
    text = "".join(out)
    if not re.search(r"^secrets\s*:", text, re.M):
        text = re.sub(r"^accounts\s*:", secrets_block + "\naccounts:", text, count=1, flags=re.M)
    path.write_text(text, encoding="utf-8")
    return removed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--environment", default="prod")
    ap.add_argument("--site-url", default="https://us.infisical.com")
    ap.add_argument("--rewrite-config", action="store_true")
    args = ap.parse_args()

    path = Path(args.config)
    raw = _interpolate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    secrets_raw = {"provider": "infisical", "project_id": args.project_id,
                   "environment": args.environment, "site_url": args.site_url}
    client = InfisicalClient(InfisicalConfig.from_raw(secrets_raw))

    wanted = {}
    for acc in raw.get("accounts") or []:
        name = str(acc.get("name") or "").strip()
        if not name:
            raise SystemExit("every account needs a name before migrating")
        for field in ("email", "password"):
            value = str(acc.get(field) or "")
            if value:
                wanted[secret_key(name, field)] = value
    if not wanted:
        print("config.yaml holds no inline credentials; nothing to upload")
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
        backup = path.with_name(path.name + ".pre-infisical.bak")
        shutil.copy2(path, backup)
        block = (
            "# Account e-mails/passwords are fetched from Infisical at startup as\n"
            "# <NAME>_EMAIL / <NAME>_PASSWORD. Credentials: INFISICAL_CLIENT_ID /\n"
            "# INFISICAL_CLIENT_SECRET in the environment.\n"
            "secrets:\n"
            "  provider: infisical\n"
            f"  project_id: {args.project_id}\n"
            f"  environment: {args.environment}\n"
            f"  site_url: {args.site_url}\n"
        )
        removed = rewrite_config(path, block)
        print(f"removed {removed} credential line(s) from {path}; backup at {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
