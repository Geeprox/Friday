"""Explicit-config HA ingress with existing OAuth refresh; no legacy runtime startup."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect

from friday.home import Credentials

from .ha import HAReadOnlyAdapter

ROOT = Path(__file__).resolve().parents[1]
CREDENTIAL_PATH = ROOT / "data/credentials/home-assistant.json"


async def build(config_path: str | Path, *, client=None, transport=None, ws_connect=connect):
    """Load only the supplied config, refresh existing credentials, return read-only adapter.

    Raises fixed-code ValueError on configuration/authentication failures; never includes
    private URL, response body, credential, or recognized household content in errors.
    """
    try:
        config = json.loads(Path(config_path).read_text())
        config = config.get("config", config)
        url = config["ha_url"]
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment):
            raise ValueError()
        token_env = config.get("ha_token_env", "FRIDAY_HA_TOKEN")
        if not isinstance(token_env, str) or not token_env:
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("explicit_ha_config_invalid") from None
    owned = client is None
    auth_client = client or httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False, timeout=15)
    credentials = Credentials(SimpleNamespace(ha_url=url, ha_token_env=token_env), auth_client)
    credentials.path = CREDENTIAL_PATH
    try:
        if not credentials.connected():
            raise ValueError("ha_credentials_missing_or_mismatched")
        token = await credentials.token()
        if not isinstance(token, str) or not token:
            raise ValueError("invalid_token")
        instance = config.get("ha_instance") or "configured-" + hashlib.sha256(url.rstrip("/").encode()).hexdigest()[:16]
        adapter = HAReadOnlyAdapter(url, token, instance, client=auth_client, ws_connect=ws_connect,
                                    token_provider=credentials.token)
        adapter._owned_client = owned
        return adapter
    except Exception:
        if owned:
            await auth_client.aclose()
        raise ValueError("ha_authentication_unavailable") from None
