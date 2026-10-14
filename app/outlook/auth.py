"""MSAL token handling. The one-time interactive login lives in scripts/authorize.py."""

import contextlib
import os
from pathlib import Path

import msal

SCOPES = ["Mail.Read"]  # read-only, per SPEC.md — never widen this
CACHE_FILENAME = "msal_token_cache.json"


class AuthError(RuntimeError):
    pass


def cache_path(secrets_dir: Path) -> Path:
    return secrets_dir / CACHE_FILENAME


def load_cache(secrets_dir: Path) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    path = cache_path(secrets_dir)
    if path.exists():
        cache.deserialize(path.read_text())
    return cache


def save_cache(secrets_dir: Path, cache: msal.SerializableTokenCache) -> None:
    if not cache.has_state_changed:
        return
    secrets_dir.mkdir(parents=True, exist_ok=True)
    path = cache_path(secrets_dir)
    path.write_text(cache.serialize())
    # Best effort: a Windows folder bind-mounted by Docker Desktop shows up root-owned,
    # so chmod fails there even though the write succeeded — don't fail the poll over it.
    with contextlib.suppress(PermissionError):
        os.chmod(path, 0o600)


def build_app(
    client_id: str, authority: str, cache: msal.SerializableTokenCache
) -> msal.PublicClientApplication:
    return msal.PublicClientApplication(client_id, authority=authority, token_cache=cache)


def get_access_token(
    client_id: str, authority: str, secrets_dir: Path, force_refresh: bool = False
) -> str:
    """Return a valid access token from the cache, refreshing silently if needed.

    Blocking (MSAL is sync) — call via asyncio.to_thread from async code.
    """
    if not client_id:
        raise AuthError("MS_CLIENT_ID is not set — see README 'Azure setup'")
    cache = load_cache(secrets_dir)
    app = build_app(client_id, authority, cache)
    accounts = app.get_accounts()
    if not accounts:
        raise AuthError("no signed-in account in the token cache — run scripts/authorize.py")
    result = app.acquire_token_silent(SCOPES, account=accounts[0], force_refresh=force_refresh)
    save_cache(secrets_dir, cache)
    if not result or "access_token" not in result:
        raise AuthError("could not refresh the access token — re-run scripts/authorize.py")
    return result["access_token"]
