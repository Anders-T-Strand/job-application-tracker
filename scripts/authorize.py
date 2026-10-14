"""One-time interactive device-code login. Creates secrets/msal_token_cache.json.

Run locally: python scripts/authorize.py
Then copy secrets/ to the VM (or run this on the VM — it only needs a browser somewhere).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.outlook import auth  # noqa: E402


def main() -> None:
    settings = load_settings()
    if not settings.ms_client_id:
        sys.exit("MS_CLIENT_ID is not set — put it in .env (see README 'Azure setup').")
    cache = auth.load_cache(settings.secrets_dir)
    app = auth.build_app(settings.ms_client_id, settings.ms_authority, cache)
    flow = app.initiate_device_flow(scopes=auth.SCOPES)
    if "user_code" not in flow:
        sys.exit(f"could not start device flow: {flow.get('error_description', flow)}")
    print(flow["message"])  # "To sign in, use a web browser to open ... and enter the code ..."
    result = app.acquire_token_by_device_flow(flow)  # blocks until you finish in the browser
    if "access_token" not in result:
        sys.exit(f"login failed: {result.get('error_description', result)}")
    auth.save_cache(settings.secrets_dir, cache)
    account = (app.get_accounts() or [{}])[0]
    print(f"Signed in as {account.get('username', 'unknown')}.")
    print(f"Token cache written to {auth.cache_path(settings.secrets_dir)} (chmod 600).")


if __name__ == "__main__":
    main()
