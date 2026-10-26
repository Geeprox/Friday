"""Version-bound NetEase control. Preparing a request is silent; execution is explicit.

Encoding was recovered from the installed 3.1.12 client, not a public API.
Launching an URI never proves the song or playback state changed.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess

BUNDLE_ID = "com.netease.163music"
OPEN = "/usr/bin/open"
ACTIONS = frozenset({"pause", "resume", "play"})
SONG_ID = re.compile(r"[1-9][0-9]{0,18}\Z", re.ASCII)


def message(action: str, song_id: str | None = None) -> dict[str, str]:
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError("unsupported_command")
    if action == "play":
        if not isinstance(song_id, str) or not SONG_ID.fullmatch(song_id) or int(song_id) > 2**63 - 1:
            raise ValueError("numeric_song_id_required")
        return {"cmd": "play", "type": "song", "id": song_id}
    if song_id is not None:
        raise ValueError("song_id_not_allowed")
    return {"cmd": action}


def build_uri(action: str, song_id: str | None = None) -> str:
    payload = json.dumps(message(action, song_id), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "orpheus://" + base64.b64encode(payload).decode("ascii")


def control(
    action: str, song_id: str | None = None, *, execute: bool = False, title: str | None = None
) -> dict:
    uri = build_uri(action, song_id)
    if type(execute) is not bool:
        raise ValueError("explicit_execution_flag_required")
    if title is not None and (
        not isinstance(title, str) or not 1 <= len(title) <= 200 or any(ord(c) < 32 or ord(c) == 127 for c in title)
    ):
        raise ValueError("invalid_display_title")
    result = {
        "schema_version": 1,
        "action": action,
        "song_selected": song_id is not None,
        "execution_requested": execute,
        "playback_confirmed": False,
    }
    if not execute:
        # Only the explicit inspect result contains song identity or optional display title.
        # Execution results remain fixed operational metadata; nothing is logged to disk.
        result.update(status="prepared", message=message(action, song_id), uri=uri)
        if title is not None:
            result["display_title"] = title
        return result
    try:
        completed = subprocess.run(
            [OPEN, "-b", BUNDLE_ID, uri],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {**result, "status": "unknown", "code": "launch_timeout", "launch_accepted": None}
    except OSError:
        return {**result, "status": "unavailable", "code": "launcher_unavailable", "launch_accepted": False}
    if completed.returncode != 0:
        return {**result, "status": "unknown", "code": "launcher_failed", "launch_accepted": False}
    return {**result, "status": "requested", "code": "none", "launch_accepted": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=sorted(ACTIONS))
    parser.add_argument("--song-id", help="Positive numeric NetEase song ID, required for play")
    parser.add_argument("--title", help="Optional local display title; excluded from execution results and URI")
    parser.add_argument("--execute", action="store_true", help="Explicitly send the command to the installed client")
    args = parser.parse_args(argv)
    try:
        result = control(args.action, args.song_id, execute=args.execute, title=args.title)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"prepared", "requested"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
