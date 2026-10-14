import msal

from app.outlook import auth


def _changed_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    cache.deserialize('{"AccessToken": {}}')
    cache.has_state_changed = True
    return cache


def test_save_cache_survives_chmod_denied(tmp_path, monkeypatch):
    """Docker Desktop bind mounts refuse chmod; the refreshed token must still be saved."""

    def deny(path, mode):
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(auth.os, "chmod", deny)
    auth.save_cache(tmp_path, _changed_cache())
    assert "AccessToken" in auth.cache_path(tmp_path).read_text()


def test_save_cache_skips_unchanged(tmp_path):
    auth.save_cache(tmp_path, msal.SerializableTokenCache())
    assert not auth.cache_path(tmp_path).exists()
