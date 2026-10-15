"""Runtime secret fetch from Infisical (AWS-Secrets-Manager style).

Account e-mails and passwords live in Infisical, keyed by the account's
``name`` in config.yaml: account ``Canary-Alton`` reads ``CANARY_ALTON_EMAIL``
and ``CANARY_ALTON_PASSWORD``. Everything else (canary flag, visa_type,
dates, consulates) stays local.

Authentication is a Universal Auth machine identity: ``INFISICAL_CLIENT_ID``
and ``INFISICAL_CLIENT_SECRET`` come from the environment (``.env`` / a Docker
secret), never from config.yaml. They are exchanged for a short-lived access
token on every load -- the fetch happens once at startup.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, Optional

from . import httpclient as http


class SecretsError(RuntimeError):
    """Infisical could not be reached or refused us; startup must stop."""


def secret_key(account_name: str, field: str) -> str:
    """``Canary-Alton`` + ``email`` -> ``CANARY_ALTON_EMAIL``."""
    base = re.sub(r"[^A-Za-z0-9]+", "_", account_name).strip("_").upper()
    if not base:
        raise SecretsError(f"account name {account_name!r} has no usable characters")
    return f"{base}_{field.upper()}"


@dataclass
class InfisicalConfig:
    project_id: str
    environment: str = "prod"
    secret_path: str = "/"
    site_url: str = "https://us.infisical.com"
    client_id: str = ""
    client_secret: str = ""

    @classmethod
    def from_raw(cls, raw: Dict) -> "InfisicalConfig":
        project_id = str(raw.get("project_id") or "").strip()
        if not project_id:
            raise SecretsError("secrets.project_id is required")
        return cls(
            project_id=project_id,
            environment=str(raw.get("environment") or "prod").strip(),
            secret_path=str(raw.get("secret_path") or "/").strip(),
            site_url=str(
                raw.get("site_url") or os.getenv("INFISICAL_SITE_URL")
                or "https://us.infisical.com"
            ).strip().rstrip("/"),
            client_id=os.getenv("INFISICAL_CLIENT_ID", "").strip(),
            client_secret=os.getenv("INFISICAL_CLIENT_SECRET", "").strip(),
        )


class InfisicalClient:
    def __init__(self, config: InfisicalConfig, timeout: float = 20) -> None:
        self.config = config
        self.timeout = timeout
        self._token: Optional[str] = None

    # -- auth --------------------------------------------------------------

    def _login(self) -> str:
        cfg = self.config
        if not (cfg.client_id and cfg.client_secret):
            raise SecretsError(
                "secrets.provider is infisical but INFISICAL_CLIENT_ID / "
                "INFISICAL_CLIENT_SECRET are not set in the environment"
            )
        resp = self._call("POST", "/api/v1/auth/universal-auth/login", auth=False,
                          json={"clientId": cfg.client_id,
                                "clientSecret": cfg.client_secret})
        token = resp.get("accessToken")
        if not token:
            raise SecretsError("Infisical login returned no accessToken")
        return token

    def _call(self, method: str, path: str, auth: bool = True, **kwargs) -> Dict:
        headers = {}
        if auth:
            if self._token is None:
                self._token = self._login()
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            resp = http.request(method, self.config.site_url + path,
                                headers=headers, timeout=self.timeout, **kwargs)
        except http.RequestException as exc:
            raise SecretsError(f"Infisical unreachable: {exc}") from exc
        if resp.status_code >= 400:
            # Never echo the request body: it can hold a secret value.
            raise SecretsError(
                f"Infisical {method} {path.split('?')[0]} -> "
                f"{resp.status_code}: {resp.text[:300]}"
            )
        return resp.json() if resp.content else {}

    # -- secrets -----------------------------------------------------------

    def _scope(self) -> Dict[str, str]:
        return {"workspaceId": self.config.project_id,
                "environment": self.config.environment,
                "secretPath": self.config.secret_path}

    def fetch_all(self) -> Dict[str, str]:
        data = self._call("GET", "/api/v3/secrets/raw", params=self._scope())
        return {s["secretKey"]: s.get("secretValue", "")
                for s in data.get("secrets", [])}

    def put(self, key: str, value: str, exists: bool) -> None:
        body = dict(self._scope(), secretValue=value, type="shared")
        self._call("PATCH" if exists else "POST", f"/api/v3/secrets/raw/{key}", json=body)


def fill_account_credentials(raw_accounts, secrets_raw: Dict,
                             client: Optional[InfisicalClient] = None) -> None:
    """Fill missing ``email`` / ``password`` on each raw account, in place.

    A value written inline in config.yaml still wins, so an account can be
    migrated one at a time. A missing secret is left empty and reported by the
    normal config validation with the account's name.
    """
    provider = str(secrets_raw.get("provider") or "infisical").strip().lower()
    if provider != "infisical":
        raise SecretsError(f"secrets.provider {provider!r} is not supported")
    needed = [a for a in raw_accounts if isinstance(a, dict)
              and (not a.get("email") or not a.get("password"))]
    if not needed:
        return
    client = client or InfisicalClient(InfisicalConfig.from_raw(secrets_raw))
    values = client.fetch_all()
    for acc in needed:
        name = str(acc.get("name") or "").strip()
        if not name:
            raise SecretsError("an account without a name cannot be looked up in Infisical")
        for field in ("email", "password"):
            if not acc.get(field):
                acc[field] = values.get(secret_key(name, field), "")
