"""Run the opt-in private LAN KTV preparation service; no audio-device actions."""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import socket
import sys
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def acquire_source(queue, adapter, cloud_adapter, song_id, folder, event):
    """Acquire the requested audio edition; optional video is prepared separately."""
    from homeplane.ktv_service import requested_audio_origin
    from homeplane.ktv_source_api import SOURCE_ERRORS
    from homeplane.ktv_video import acquire_mv_source

    origin = requested_audio_origin(queue.jobs[folder.name].get("source_preference", "auto"))
    if origin in {"mv", "mv_direct"}:
        try:
            source = acquire_mv_source(song_id, folder, event,
                                       mv_id=song_id if origin == "mv_direct" else None)
        except Exception:
            raise ValueError("cancelled" if event.is_set() else "source_unavailable") from None
    else:
        selected = cloud_adapter if origin == "cloud" else adapter
        result = selected.acquire(song_id, timeout=180, cancel=event,
                                  progress=lambda value: queue.report_source(folder.name, value))
        if result.get("status") != "ready":
            raw = result.get("code")
            raise ValueError(raw if raw in SOURCE_ERRORS | {
                "source_changed", "decode_failed", "download_timeout", "download_identity_mismatch",
                "download_cancelled",
            } else "download_unavailable")
        source = Path(result["source_path"])
    with queue.lock:
        queue.jobs[folder.name]["audio_origin"] = origin
    return source


def main(argv=None):
    # Runtime/dependency imports stay inside the outer startup admission guard.
    from homeplane.ktv_service import (
        Queue,
        atomic_json,
        create_app,
        local_host,
        private_directory,
        separator,
        transcode_pcm,
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "serve", "check"])
    parser.add_argument("--config", type=Path, default=Path("data/ktv/config.json"))
    parser.add_argument("--host", type=local_host, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--download-dir", type=Path)
    parser.add_argument("--launchd-sockets", action="store_true", help="Use system-owned LAN and loopback listeners")
    parser.add_argument("--public-config", type=Path, help="Private NAS endpoint and agent configuration")
    parser.add_argument("--reset-startup-budget", action="store_true", help="Explicit local budget reset; does not start the service")
    parser.add_argument(
        "--allow-download", action="store_true", help="Allow authenticated local API downloads; never opens client windows"
    )
    args = parser.parse_args(argv)
    config_path = args.config.absolute()
    root = Path(__file__).resolve().parents[1]
    if config_path.resolve() != config_path or not any(
        config_path.is_relative_to(root / p) for p in ("data", ".artifacts")
    ):
        parser.error("config_must_be_private_workspace_file")
    if args.action == "init":
        if config_path.exists():
            parser.error("existing_config_preserved")
        if args.download_dir is None or args.download_dir.resolve() != args.download_dir.absolute():
            parser.error("explicit_download_directory_required")
        private_directory(config_path.parent)
        atomic_json(
            config_path,
            {
                "protocol": 1,
                "token": secrets.token_urlsafe(32),
                "download_dir": str(args.download_dir.absolute()),
                "root": str((config_path.parent / "runtime").absolute()),
                "separation_cache": str(root / ".artifacts/ktv-audio/separation/cache"),
            },
        )
        print(json.dumps({"status": "configured", "token_printed": False, "audio_started": False}))
        return
    if not config_path.is_file() or config_path.stat().st_mode & 0o077:
        parser.error("private_config_required")
    config = json.loads(config_path.read_text())
    from homeplane.ktv_download import NetEaseDownloadAdapter
    from homeplane.ktv_resources import available_memory, machine
    from homeplane.ktv_source_api import APIAudioAdapter, LocalSourceAPI
    from homeplane.ktv_video import prepare_video
    legacy = NetEaseDownloadAdapter(
        Path(config["download_dir"]), Path(config["root"]) / "downloads", allow_launch=False
    )
    source_api = LocalSourceAPI(Path(config["root"]) / "source-account",
        root / "data/ktv/provider/node_modules/@neteasecloudmusicapienhanced/api")
    adapter = APIAudioAdapter(source_api, Path(config["root"]) / "api-downloads",
                              legacy=legacy, enabled=args.allow_download)
    cloud_adapter = APIAudioAdapter(source_api, Path(config["root"]) / "cloud-downloads",
                                    enabled=args.allow_download, source="cloud")
    ffmpeg = shutil.which("ffmpeg")
    if args.action == "check":
        print(
            json.dumps(
                {
                    "status": "configured",
                    "ffmpeg_available": bool(ffmpeg),
                    "download_launch_enabled": False,
                    "source_api": source_api.status(),
                    "audio_started": False,
                    "catalog_count": len(adapter.catalog()),
                }
            )
        )
        return
    if not ffmpeg or not 1024 <= args.port <= 65535:
        parser.error("runtime_or_port_unavailable")

    def acquire(song_id, folder, event):
        return acquire_source(queue, adapter, cloud_adapter, song_id, folder, event)

    queue = Queue(
        Path(config["root"]),
        acquire=acquire,
        separate=separator(Path(config["separation_cache"])),
        transcode=partial(transcode_pcm, ffmpeg=ffmpeg),
        video=lambda song_id, original, folder, event: {"status": "unavailable"} if queue.jobs[folder.name].get("audio_origin") == "cloud" else prepare_video(song_id, original, folder, event),
        hardware=machine(),
        memory_available=available_memory,
    )
    import uvicorn

    from homeplane.ktv_cache import CacheManager
    from homeplane.ktv_nas_bridge import NasBridge
    from homeplane.ktv_search import search_songs, song_lyrics
    from homeplane.ktv_service import prepared_lyrics
    from homeplane.ktv_worker import PreparationWorker, WorkerCapabilities, media_origin

    public_config = json.loads(args.public_config.read_text()) if args.public_config else {}
    if public_config.get("coordination") not in (None, "nas"):
        raise ValueError("invalid_coordination_role")
    coordinator = public_config.get("coordination") == "nas"
    if coordinator and media_origin(public_config.get("media_origin", "")) != "http://" + args.host + ":" + str(args.port):
        raise ValueError("worker_media_listener_mismatch")

    guide = None
    if coordinator or (config_path.parent / "guide.json").is_file():
        from homeplane.ktv_guide import GuidePublisher
        guide = GuidePublisher(queue, config_path.parent / "guide.json", local=coordinator)
    if coordinator:
        bridge = PreparationWorker(queue, args.public_config, signing_key=config["token"],
            health=WorkerCapabilities(source_api, Path(config["separation_cache"]), download_enabled=args.allow_download),
            lyrics=lambda job_id: prepared_lyrics(queue, job_id, song_lyrics, source_api),
            cloud_catalog=source_api.cloud_catalog, guide=guide, cloud_lyrics=source_api.prepare_cloud_lyrics)
    else:
        bridge = NasBridge(queue, args.public_config, cloud_catalog=source_api.cloud_catalog, guide=guide,
                           cloud_lyrics=source_api.prepare_cloud_lyrics) if args.public_config else None
    archive = None
    archive_config = config_path.parent / "archive.json"
    if archive_config.is_file():
        from homeplane.ktv_archive import MediaArchive
        archive = MediaArchive(queue, archive_config)
        adapter.archive = archive
        cloud_adapter.archive = archive
        queue.archive = archive

    def protected_cache():
        archiving = set(archive.active) if archive else set()
        if bridge is None:
            return archiving
        if isinstance(bridge, PreparationWorker):
            return archiving | bridge.protected_jobs()
        party = bridge.current()
        active = set(party.get("order", []))
        report = bridge.playback or {}
        active -= set(report.get("played", []))
        if report.get("current"):
            active.add(report["current"])
        return active | archiving

    cache = CacheManager(
        queue, Path(config["separation_cache"]), protected_cache, bridge.lock if bridge else None
    )
    cache.archive = archive
    if archive:
        archive.cache_manager = cache
        if guide:
            guide.exclude({job for jobs in archive.exclusions.values() for job in jobs})
            archive.on_exclude = guide.exclude
    from homeplane.ktv_library import SongLibrary
    library = SongLibrary(queue, Path(config["separation_cache"]), bridge=bridge, source_api=source_api, archive=archive)
    queue.storage_manager = library
    # No access log: song/job paths and authorization headers are private operational data.
    app = create_app(
        queue,
        config["token"],
        adapter.catalog,
        search_songs,
        song_lyrics,
        bridge,
        cache_manager=cache,
        console_origin=f"http://127.0.0.1:{args.port}",
        source_api=source_api,
        archive_manager=archive,
        library=library,
    )
    listeners = []
    try:
        if args.launchd_sockets:
            from homeplane.ktv_startup import activate
            listeners = activate(args.host, args.port)
        for host in ([] if args.launchd_sockets else dict.fromkeys([args.host, "127.0.0.1"])):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, args.port))
            listener.listen(128)
            listeners.append(listener)
        atomic_json(config_path.parent / "startup.json", {"state": "ready", "system_sockets": args.launchd_sockets})
        from homeplane.ktv_startup import runtime_ready
        class BudgetedServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                if self.started:
                    runtime_ready(lambda: setattr(self, "should_exit", True))
        BudgetedServer(uvicorn.Config(app, access_log=False, log_config=None)).run(sockets=listeners)
    finally:
        for listener in listeners:
            listener.close()


if __name__ == "__main__":
    from homeplane.ktv_startup import guarded_run
    sys.exit(guarded_run(main, sys.argv[1:]))
