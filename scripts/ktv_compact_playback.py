"""存量播放双轨迁移。默认只读计划，实机验收后显式准备、应用并可继续中断任务。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SHA = re.compile(r"[0-9a-f]{64}\Z")
ENCODING_REVISION = 1


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def atomic(path, value):
    temporary = path.with_name("." + path.name + ".partial")
    raw = json.dumps(value, ensure_ascii=False, indent=2).encode()
    with temporary.open("wb") as output:
        os.chmod(temporary, 0o600)
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def protected(db):
    result = set()
    for room in db.execute("SELECT id,current_song,played FROM rooms WHERE closed=0"):
        played = set(json.loads(room["played"]))
        for row in db.execute("SELECT id,song_id,state FROM songs WHERE room=? AND removed=0", (room["id"],)):
            if row["id"] == room["current_song"] or row["id"] not in played and row["state"] not in {"failed", "cancelled"}:
                result.add(row["song_id"])
    for row in db.execute("SELECT s.song_id FROM commands c JOIN songs s ON s.id=c.song "
                          "JOIN rooms r ON r.id=c.room WHERE r.closed=0 AND c.seq>r.last_command "
                          "AND c.action='play' AND c.created>?", (int(time.time()) - 20,)):
        result.add(row["song_id"])
    return result


def plan(state, legacy=None):
    """计划仅打开现有 SQLite 的只读连接，不初始化服务或修改保留期。"""
    with closing(sqlite3.connect((state / "state.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        reserved = protected(db)
        items = []
        for row in db.execute("SELECT * FROM managed_editions ORDER BY song_id,source,updated DESC"):
            job = json.loads(row["job"])
            media = job.get("media", {})
            if (set(media) != {"original", "instrumental"}
                    or any(item.get("encoding", "pcm_s16le_wav") != "pcm_s16le_wav" for item in media.values())):
                continue
            identity = {name: row[name] for name in ("song_id", "source", "version")}
            key = fingerprint(identity)
            files = {}
            for kind, item in media.items():
                sha = item.get("sha256", "")
                path = state / "coordinator-media" / sha
                if not SHA.fullmatch(sha) or path.is_symlink() or not path.is_file() or path.stat().st_size != item["bytes"]:
                    break
                files[kind] = {"sha256": sha, "bytes": item["bytes"]}
            if len(files) != 2:
                continue
            bitrate = 256000
            legacy_retire = []
            if legacy:
                catalog = legacy / "catalog" / (row["song_id"] + ".json")
                if catalog.is_file() and not catalog.is_symlink():
                    archived = json.loads(catalog.read_text())
                    metadata = archived.get("source_metadata") or {}
                    known = metadata.get("bitrate")
                    lossless = metadata.get("requested_level") in {"lossless", "hires", "jyeffect", "sky", "jymaster"}
                    original = archived.get("files", {}).get("source") or {}
                    if (SHA.fullmatch(original.get("sha256", ""))
                            and original.get("file") == original["sha256"] + ".audio"):
                        source = legacy / "blobs" / original["file"]
                        if source.is_file() and not source.is_symlink():
                            with source.open("rb") as stream:
                                header = stream.read(12)
                            lossless |= header.startswith(b"fLaC") or header[:4] == b"RIFF" and header[8:] == b"WAVE"
                    if (type(known) is int and 8000 <= known <= 1024000
                            and not lossless):
                        bitrate = known
                    if all(archived.get("files", {}).get(kind, {}).get("sha256") == spec["sha256"]
                           for kind, spec in files.items()):
                        originals = [archived.get("files", {}).get("source"), *archived.get("originals", [])]
                        legacy_retire = [original for original in originals if isinstance(original, dict)
                            and SHA.fullmatch(original.get("sha256", ""))
                            and original.get("file") == original["sha256"] + ".audio"
                            and type(original.get("bytes")) is int and original["bytes"] > 0]
            items.append({**identity, "id": key, "title": job.get("title", ""), "media": files,
                          "bitrate": bitrate, "legacy_retire": legacy_retire, "protected": row["song_id"] in reserved})
    return {"schema": 1, "items": items, "revision": fingerprint(items)}


def acceptance(path):
    value = json.loads(path.read_text())
    if (value.get("audio_encoding") != "aac_lc_m4a" or any(value.get(key) is not True
            for key in ("physical_tv", "original_backing_switch", "lyrics_in_sync"))):
        raise ValueError("physical_tv_acceptance_required")
    return fingerprint(value)


def prepare(state, stage, legacy=None, exported=None):
    """Mac 只写显式暂存目录，已完成的内容按原计划摘要续用。"""
    from homeplane.ktv_audio import pack_playback

    current = plan(state, legacy) if exported is None else json.loads(exported.read_text())
    if current.get("schema") != 1 or current.get("revision") != fingerprint(current.get("items")):
        raise ValueError("migration_plan_changed")
    stage.mkdir(mode=0o700, parents=True, exist_ok=True)
    variants = {}
    def variant(item):
        return fingerprint(["aac_lc_m4a", ENCODING_REVISION, item["media"], item["bitrate"]])
    for record in stage.glob("*/prepared.json"):
        existing = json.loads(record.read_text())
        if existing.get("encoding_revision") == ENCODING_REVISION:
            variants.setdefault(variant(existing["source"]), (record.parent, existing["media"]))
    for item in current["items"]:
        if item["protected"]:
            continue
        folder = stage / item["id"]
        folder.mkdir(mode=0o700, exist_ok=True)
        record = folder / "prepared.json"
        if record.is_file():
            existing = json.loads(record.read_text())
            if existing["source"] != item:
                raise ValueError("migration_source_changed")
            if (existing.get("encoding_revision") == ENCODING_REVISION
                    and all(checksum(folder / spec["filename"]) == spec["sha256"] for spec in existing["media"].values())):
                continue
        shared = variants.get(variant(item))
        if shared and shared[0] != folder and all(checksum(shared[0] / spec["filename"]) == spec["sha256"] for spec in shared[1].values()):
            for spec in shared[1].values():
                _legacy_link(shared[0] / spec["filename"], folder / spec["filename"])
            atomic(record, {"source": item, "media": shared[1], "encoding_revision": ENCODING_REVISION})
            continue
        paths = [state / "coordinator-media" / item["media"][kind]["sha256"] for kind in ("original", "instrumental")]
        if any(checksum(path) != item["media"][kind]["sha256"] for path, kind in zip(paths, ("original", "instrumental"))):
            raise ValueError("migration_source_changed")
        media = pack_playback(*paths, folder, threading.Event(), ffmpeg=shutil.which("ffmpeg"),
                              ffprobe=shutil.which("ffprobe"), bitrate=item["bitrate"])
        for path in folder.glob(".playback-*.wav"):
            path.unlink()
        atomic(record, {"source": item, "media": media, "encoding_revision": ENCODING_REVISION})
        variants[variant(item)] = (folder, media)
    return current


def _publish(source, destination, sha, size):
    if source.is_symlink() or source.stat().st_size != size or checksum(source) != sha:
        raise ValueError("migration_candidate_changed")
    if destination.is_file() and not destination.is_symlink() and destination.stat().st_size == size and checksum(destination) == sha:
        return
    temporary = destination.with_name(".compact-" + sha)
    try:
        with source.open("rb") as original, temporary.open("wb") as output:
            shutil.copyfileobj(original, output, 1048576)
            output.flush()
            os.fsync(output.fileno())
        if checksum(temporary) != sha:
            raise ValueError("migration_copy_changed")
        temporary.chmod(0o400)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _job(job, media):
    original = job["media"]["original"]["sha256"]
    result = dict(job)
    result["media"] = {kind: {**{k: v for k, v in spec.items() if k != "filename"},
                             "url": job["media"][kind]["url"]} for kind, spec in media.items()}
    video = job.get("video")
    if video and video.get("source_audio_sha256") == original:
        result["video"] = {**video, "source_audio_sha256": media["original"]["sha256"]}
    return result


def _legacy_link(source, destination):
    """同一 NAS 卷只增加硬链接，兼容未配置共享根目录的旧回档器。"""
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if source.is_symlink() or destination.is_symlink():
        raise ValueError("migration_legacy_source_changed")
    if destination.is_file() and os.path.samefile(source, destination):
        return
    temporary = destination.with_name(".compact-" + destination.name)
    try:
        if temporary.exists():
            temporary.unlink()
        os.link(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _legacy(legacy, item, media):
    catalog = legacy / "catalog" / (item["song_id"] + ".json")
    if not catalog.is_file() or catalog.is_symlink():
        return
    data = json.loads(catalog.read_text())
    old = {spec["sha256"] for spec in item["media"].values()}
    changed = False
    if all((data.get("files", {}).get(kind) or {}).get("sha256") == spec["sha256"] for kind, spec in item["media"].items()):
        data["media"] = media
        for kind, spec in media.items():
            data["files"][kind] = {"file": spec["sha256"] + ".m4a", "sha256": spec["sha256"], "bytes": spec["bytes"]}
        retiring = {value["file"] for value in item.get("legacy_retire", [])}
        if data.get("files", {}).get("source", {}).get("file") in retiring:
            del data["files"]["source"]
        data["originals"] = [value for value in data.get("originals", []) if value.get("file") not in retiring]
        changed = True
    for key in ("retained", "originals"):
        values = data.get(key, [])
        kept = [value for value in values if not (value.get("sha256") in old and value.get("file", "").endswith(".wav"))]
        if kept != values:
            data[key], changed = kept, True
    if changed:
        atomic(catalog, data)


def apply(state, stage, proof, legacy=None):
    """在 NAS 上运行，先发布新成品，再事务切换冷曲目，最后回收无引用旧副本。"""
    try:
        from integrations.ktv_public.audio_format import verify_compact
    except ImportError:
        from audio_format import verify_compact

    accepted = acceptance(proof)
    candidates = [json.loads(path.read_text()) for path in stage.glob("*/prepared.json")]
    completed, old_hashes = [], set()
    journal = stage / "applied.json"
    previous = json.loads(journal.read_text()) if journal.is_file() else {"completed": [], "old_hashes": []}
    completed.extend(previous["completed"])
    old_hashes.update(previous["old_hashes"])
    with closing(sqlite3.connect(str(state / "state.sqlite"), timeout=20)) as db:
        db.row_factory = sqlite3.Row
        for candidate in candidates:
            item, media = candidate["source"], candidate["media"]
            if item["id"] in completed:
                continue
            if item["song_id"] in protected(db):
                continue
            # 验证与复制在写事务外完成，手机控制不等待媒体 I/O。
            for spec in media.values():
                source = stage / item["id"] / spec["filename"]
                verify_compact(source, spec)
                _publish(source, state / "coordinator-media" / spec["sha256"], spec["sha256"], spec["bytes"])
                if legacy:
                    _legacy_link(state / "coordinator-media" / spec["sha256"], legacy / "blobs" / (spec["sha256"] + ".m4a"))
            db.execute("BEGIN IMMEDIATE")
            try:
                if item["song_id"] in protected(db):
                    db.rollback()
                    continue
                row = db.execute("SELECT * FROM managed_editions WHERE song_id=? AND source=? AND version=?",
                                 (item["song_id"], item["source"], item["version"])).fetchone()
                if row is None:
                    # 元数据已提交但日志尚未写入的中断可按新摘要继续回收。
                    rows = db.execute("SELECT job FROM managed_editions WHERE song_id=? AND source=?",
                                      (item["song_id"], item["source"])).fetchall()
                    if not any(all(json.loads(r["job"])["media"][kind]["sha256"] == media[kind]["sha256"]
                                   for kind in media) for r in rows):
                        raise ValueError("migration_source_changed")
                    db.rollback()
                else:
                    old_job = json.loads(row["job"])
                    if any(old_job["media"][kind]["sha256"] != spec["sha256"] for kind, spec in item["media"].items()):
                        raise ValueError("migration_source_changed")
                    for spec in media.values():
                        info = (state / "coordinator-media" / spec["sha256"]).stat()
                        db.execute("INSERT OR REPLACE INTO coordinator_cache(sha,bytes,state,attempts,updated,device,inode,mtime_ns,ctime_ns) "
                                   "VALUES(?,?,'ready',0,?,?,?,?,?)", (spec["sha256"], spec["bytes"], int(time.time()),
                                   info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns))
                        db.execute("INSERT OR REPLACE INTO managed_media VALUES(?,?,?,?,?)",
                                   (item["song_id"], spec["sha256"], item["title"], spec["bytes"], int(time.time())))
                    new_job = _job(old_job, media)
                    version = fingerprint([item["version"], new_job["media"]])
                    timeline = {"sample_rate": 48000, "frames": media["original"]["frames"],
                                "duration_ms": round(media["original"]["frames"] / 48)}
                    db.execute("UPDATE managed_editions SET version=?,job=?,timeline=?,updated=? "
                               "WHERE song_id=? AND source=? AND version=?",
                               (version, json.dumps(new_job, sort_keys=True), json.dumps(timeline, sort_keys=True),
                                int(time.time()), item["song_id"], item["source"], item["version"]))
                    rows = db.execute("SELECT m.* FROM coordinator_manifests m JOIN songs s ON s.id=m.song WHERE s.song_id=?",
                                      (item["song_id"],)).fetchall()
                    for current in rows:
                        job = json.loads(current["job"])
                        if not all(job.get("media", {}).get(kind, {}).get("sha256") == spec["sha256"] for kind, spec in item["media"].items()):
                            continue
                        job = _job(job, media)
                        db.execute("UPDATE coordinator_manifests SET version=?,job=?,timeline=?,instance='',origin='',ticket='',expires=0,updated=? WHERE song=?",
                                   (version, json.dumps(job, sort_keys=True), json.dumps(timeline, sort_keys=True), int(time.time()), current["song"]))
                        db.execute("INSERT OR IGNORE INTO coordinator_manifest_versions VALUES(?,?)", (current["song"], version))
                        db.execute("UPDATE coordinator_lyric_status SET version=? WHERE song=?", (version, current["song"]))
                    if legacy:
                        _legacy(legacy, item, media)
                    db.commit()
                completed.append(item["id"])
                old_hashes.update(spec["sha256"] for spec in item["media"].values())
                old_hashes.update(spec["sha256"] for spec in item.get("legacy_retire", []))
                atomic(journal, {"completed": completed, "old_hashes": sorted(old_hashes), "acceptance": accepted})
            except BaseException:
                db.rollback()
                raise
        # 同一摘要可能被其他版本或仍在使用的歌曲引用，最后统一核对。
        db.execute("BEGIN IMMEDIATE")
        referenced = set()
        for table in ("managed_editions", "coordinator_manifests"):
            for row in db.execute("SELECT job FROM " + table):
                job = json.loads(row["job"])
                referenced.update(spec["sha256"] for spec in job.get("media", {}).values())
        if legacy:
            for catalog in (legacy / "catalog").glob("*.json"):
                data = json.loads(catalog.read_text())
                referenced.update(value["sha256"] for value in [
                    *data.get("files", {}).values(), *data.get("retained", []), *data.get("originals", [])])
        removable = old_hashes - referenced
        for sha in removable:
            if not SHA.fullmatch(sha):
                raise ValueError("migration_invalid_hash")
            db.execute("DELETE FROM managed_media WHERE sha=?", (sha,))
            db.execute("DELETE FROM coordinator_cache WHERE sha=?", (sha,))
        db.commit()
        removed = 0
        for sha in removable:
            paths = [state / "coordinator-media" / sha]
            if legacy:
                paths.append(legacy / "blobs" / (sha + ".wav"))
                paths.append(legacy / "blobs" / (sha + ".audio"))
            for path in paths:
                if path.is_file() and not path.is_symlink() and checksum(path) == sha:
                    identity = path.stat()
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        still_used = any(db.execute("SELECT 1 FROM " + table + " WHERE job LIKE ? LIMIT 1",
                                                    ("%" + sha + "%",)).fetchone()
                                         for table in ("managed_editions", "coordinator_manifests"))
                        if not still_used and path.stat() == identity:
                            path.unlink()
                            removed += identity.st_size
                        db.commit()
                    except BaseException:
                        db.rollback()
                        raise
        return {"completed": len(completed), "removed_bytes": removed, "retained_hashes": len(old_hashes & referenced)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--action", choices=("plan", "prepare", "apply"), default="plan")
    parser.add_argument("--tv-acceptance", type=Path)
    parser.add_argument("--output", type=Path, help="在 NAS 本机把只读计划导出为私有 JSON")
    parser.add_argument("--plan-file", type=Path, help="Mac 准备时读取 NAS 本机导出的计划")
    args = parser.parse_args()
    state = args.state.absolute()
    legacy = args.legacy_root.absolute() if args.legacy_root else None
    if state.resolve() != state or legacy and legacy.resolve() != legacy:
        parser.error("canonical_private_storage_required")
    if args.action == "plan":
        result = plan(state, legacy)
        if args.output:
            atomic(args.output.absolute(), result)
        print(json.dumps({"candidates": len(result["items"]), "protected": sum(item["protected"] for item in result["items"]),
                          "revision": result["revision"]}))
        return
    if not args.stage or not args.tv_acceptance:
        parser.error("explicit_stage_and_physical_tv_acceptance_required")
    acceptance(args.tv_acceptance)
    stage = args.stage.absolute()
    if stage.resolve() != stage or stage == state:
        parser.error("independent_migration_stage_required")
    if args.action == "prepare":
        if not args.plan_file:
            parser.error("nas_exported_plan_file_required")
        result = prepare(state, stage, legacy, exported=args.plan_file)
        print(json.dumps({"prepared": len(list(stage.glob("*/prepared.json"))), "revision": result["revision"]}))
    else:
        print(json.dumps(apply(state, stage, args.tv_acceptance, legacy)))


if __name__ == "__main__":
    main()
