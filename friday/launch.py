"""Local web launch. OAuth credentials stay in private files and never reach stdout."""

import json
import os
from pathlib import Path

import uvicorn

from .config import load_settings


def main():
    settings = load_settings()
    local_miloco = Path("data/miloco/config.json")
    if settings.mode == "live" and local_miloco.exists() and not os.environ.get(settings.miloco_token_env):
        token = json.loads(local_miloco.read_text()).get("server", {}).get("token")
        if token:
            os.environ[settings.miloco_token_env] = token
    uvicorn.run(
        "friday.app:app", host="127.0.0.1", port=int(os.environ.get("FRIDAY_PORT", "8730")), access_log=False
    )


if __name__ == "__main__":
    main()
