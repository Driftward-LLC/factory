"""Small client-credentials provider used only by the Factory token service."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


TOKEN_ENDPOINT = "https://api.linear.app/oauth/token"


class IdentityError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AppTokenProvider:
    def __init__(self, client_id: str, client_secret: str, cache: Path, *, scopes: str = "read,write", skew: int = 3600):
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.cache = cache
        self.scopes = ",".join(part.strip() for part in scopes.split(",") if part.strip())
        self.skew = skew
        if not self.client_id or not self.client_secret or not self.scopes:
            raise IdentityError("app_credentials_invalid")

    @property
    def credential_id(self) -> str:
        return hashlib.sha256(f"{self.client_id}\0{self.scopes}".encode()).hexdigest()

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.cache.parent.chmod(0o700)
        path = self.cache.with_suffix(".lock")
        with path.open("a+", encoding="utf-8") as handle:
            path.chmod(0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def token(self, *, force: bool = False) -> str:
        with self._lock():
            if not force:
                cached = self._cached()
                if cached:
                    return cached
            token, expires_in = self._fetch()
            self._store(token, expires_in)
            return token

    def invalidate(self) -> None:
        with self._lock():
            try:
                self.cache.unlink()
            except FileNotFoundError:
                pass

    def _cached(self) -> str | None:
        try:
            value = json.loads(self.cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("credential_id") != self.credential_id:
            return None
        token, expiry = value.get("access_token"), value.get("expires_at")
        if not isinstance(token, str) or not token or not isinstance(expiry, (int, float)) or expiry <= time.time() + self.skew:
            return None
        return token

    def _fetch(self) -> tuple[str, int]:
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": self.scopes}).encode()
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        request = urllib.request.Request(TOKEN_ENDPOINT, data=body, headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                value = json.load(response)
        except urllib.error.HTTPError as exc:
            raise IdentityError("app_auth_failure" if exc.code in {400, 401, 403} else "linear_api_failure") from exc
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise IdentityError("linear_api_failure") from exc
        if not isinstance(value, dict) or not isinstance(value.get("access_token"), str) or not value["access_token"] or str(value.get("token_type", "")).lower() != "bearer" or not isinstance(value.get("expires_in"), int) or value["expires_in"] <= self.skew:
            raise IdentityError("app_token_invalid")
        return value["access_token"], value["expires_in"]

    def _store(self, token: str, expires_in: int) -> None:
        value = {"schema_version": 1, "credential_id": self.credential_id, "access_token": token, "expires_at": int(time.time()) + expires_in}
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.cache.parent, delete=False) as handle:
            json.dump(value, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.chmod(0o600)
        os.replace(temporary, self.cache)
