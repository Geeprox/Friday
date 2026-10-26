#!/usr/bin/env python3
"""Explicitly authorized SMB metadata probe; bounded entries, one media sample per folder."""
import argparse
import hashlib
import itertools
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

MEDIA = {".mp4", ".mkv", ".mov", ".ts", ".avi"}


def probe(root, private_path):
    root = Path(root).resolve(strict=True)
    rows, mapping = [], []
    with os.scandir(root) as entries:
        folders = [Path(e.path) for e in itertools.islice(entries, 20)
                   if e.is_dir(follow_symlinks=False) and not e.name.startswith('.')]
    for folder in folders:
        sample = None
        with os.scandir(folder) as entries:
            for entry in itertools.islice(entries, 80):
                if entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in MEDIA:
                    sample = Path(entry.path)
                    break
        if sample is None:
            continue
        identity = 'archive-camera-' + str(len(rows) + 1)
        before = sample.stat()
        record = {"camera_ref": identity, "simulated": False,
                  "folder_fingerprint": hashlib.sha256(folder.name.encode()).hexdigest(),
                  "file_fingerprint": hashlib.sha256(sample.name.encode()).hexdigest(),
                  "file_size_bytes": before.st_size, "selection": "first_media_in_bounded_directory_iteration",
                  "sample_suffix": sample.suffix, "recording_time_verified": False,
                  "filename_time_mapping_verified": False, "retention_policy_verified": False}
        command = ['ffprobe', '-v', 'error', '-probesize', '1048576', '-analyzeduration', '1000000',
                   '-show_entries', 'stream=index,codec_type,codec_name,width,height,sample_rate,channels,time_base,duration:format=format_name,duration,size',
                   '-of', 'json', str(sample)]
        try:
            process = subprocess.run(command, capture_output=True, timeout=12, check=False)
            if len(process.stdout) > 65536:
                record['status'] = 'OUTPUT_LIMIT'
            elif process.returncode:
                record.update(status='FFPROBE_FAILED', returncode=process.returncode)
            else:
                record.update(status='PASS_MEDIA_METADATA', media=json.loads(process.stdout))
        except subprocess.TimeoutExpired:
            record['status'] = 'TIMEOUT'
        except (OSError, ValueError) as exc:
            record.update(status='FAILED', error_class=type(exc).__name__)
        after = sample.stat()
        record['metadata_unchanged_during_probe'] = (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
        record['completion_verified'] = False  # stable metadata alone is not completion proof
        rows.append(record)
        mapping.append({'camera_ref': identity, 'folder': str(folder), 'sample': str(sample)})
        if len(rows) >= 3:
            break
    target = Path(private_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'sensitivity':'private-household-paths','root':str(root),'mapping':mapping},stream,indent=2)
        stream.write('\n')
    return {'observed_at':datetime.now(timezone.utc).isoformat(), 'simulated':False,
            'read_authorized':True,'transport':'existing_authenticated_smb_mount',
            'limits':{'top_entries':20,'entries_per_folder':80,'samples':3,'probe_timeout_seconds':12},
            'media_copied':False,'audio_video_decoded':False,'media_modified':False,
            'recording_pipeline_changed':False,'sample_count':len(rows),'samples':rows}


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True)
    parser.add_argument('--read-authorized',action='store_true')
    parser.add_argument('--private-map',default='data/homeplane/nas-private-paths.json')
    parser.add_argument('--output',default='docs/evidence/nas-read-probe.json')
    args=parser.parse_args()
    if not args.read_authorized:
        parser.error('Explicit read authorization is required')
    result=probe(args.root,args.private_map)
    Path(args.output).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
