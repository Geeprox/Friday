import hashlib,json,wave
import math
import struct
import subprocess
import shutil
import threading
import pytest
from pathlib import Path
from types import SimpleNamespace
from homeplane.ktv_guide import GuidePublisher


def test_local_worker_guide_does_not_require_nas_mount_and_is_hash_checked(tmp_path, monkeypatch):
    from concurrent.futures import Future
    jobs = tmp_path / "jobs"
    job = "11111111-1111-4111-8111-111111111111"
    folder = jobs / job
    folder.mkdir(parents=True)
    specs = {}
    for kind in ("original", "instrumental"):
        source = folder / (kind + ".wav")
        with wave.open(str(source), "wb") as stream:
            stream.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0" * 48000 * 4 * 2)
        specs[kind] = {"sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "duration_seconds": 2}
    def no_mount(_):
        raise AssertionError("NAS mount must not gate local guide preparation")
    monkeypatch.setattr(Path, "is_mount", no_mount)
    publisher = GuidePublisher(SimpleNamespace(jobs_dir=jobs, get=lambda _: {"media": specs}), local=True)
    try:
        result = publisher._prepare(job)
        future = Future()
        future.set_result(result)
        publisher.futures[job] = future
        for variant, spec in (("guide_original", result), ("guide_reference", result["reference"])):
            stream, observed = publisher.media(job, variant)
            with stream:
                data = stream.read()
            assert observed["url"] == f"/v1/jobs/{job}/media/{variant}"
            assert len(data) == spec["bytes"] and hashlib.sha256(data).hexdigest() == spec["sha256"]
        path = publisher.destination / (result["sha256"] + ".mp3")
        path.write_bytes(b"x" * result["bytes"])
        with pytest.raises(ValueError, match="guide_(changed|unavailable)"):
            publisher.media(job, "guide_original")
        publisher.futures[job].result(timeout=10)
        restored = publisher.get(job)
        assert restored == result
        with publisher.media(job, "guide_original")[0] as stream:
            assert hashlib.sha256(stream.read()).hexdigest() == result["sha256"]
    finally:
        publisher.close()


def synthetic_queue(tmp_path, names=("a",)):
    jobs = tmp_path / "jobs"
    media = {}
    for index, job in enumerate(names):
        folder = jobs / job
        folder.mkdir(parents=True)
        media[job] = {}
        for offset, kind in enumerate(("original", "instrumental")):
            source = folder / (kind + ".wav")
            frequency = 220 + index * 150 + offset * 80
            samples = [int(8000 * math.sin(2 * math.pi * frequency * i / 48000)) for i in range(96000)]
            with wave.open(str(source), "wb") as stream:
                stream.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                stream.writeframes(b"".join(struct.pack("<hh", value, value) for value in samples))
            media[job][kind] = {"sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "duration_seconds": 2}
    return SimpleNamespace(jobs_dir=jobs, get=lambda job: {"media": media[job]})


def ready(publisher, job):
    value = publisher.get(job)
    if value is None:
        publisher.futures[job].result(timeout=10)
        value = publisher.get(job)
    assert value is not None
    return value


def test_evicted_successful_guide_is_rebuilt_on_get(tmp_path, monkeypatch):
    from homeplane import ktv_guide
    monkeypatch.setattr(ktv_guide, "CACHE_BUDGET_BYTES", 1)
    publisher = GuidePublisher(synthetic_queue(tmp_path, ("a", "b")), local=True)
    try:
        first = ready(publisher, "a")
        publisher.protect(["b"])
        ready(publisher, "b")
        paths = [publisher.destination / (spec["sha256"] + ".mp3") for spec in (first, first["reference"])]
        assert all(not path.exists() for path in paths), "exercise the actual cache eviction path"
        assert publisher.get("a") is None, "evicted metadata must not remain ready"
        publisher.futures["a"].result(timeout=10)
        assert publisher.get("a") == first
        for variant, spec in (("guide_original", first), ("guide_reference", first["reference"])):
            with publisher.media("a", variant)[0] as stream:
                assert hashlib.sha256(stream.read()).hexdigest() == spec["sha256"]
    finally:
        publisher.close()


def test_get_uses_stat_only_and_repairs_corruption_on_background_lane(tmp_path, monkeypatch):
    publisher = GuidePublisher(synthetic_queue(tmp_path), local=True)
    release = threading.Event()
    entered = threading.Event()
    try:
        first = ready(publisher, "a")
        with monkeypatch.context() as fast:
            fast.setattr(hashlib, "file_digest", lambda *a, **k: pytest.fail("get must not synchronously hash"))
            for _ in range(20):
                assert publisher.get("a") == first
        original_prepare = publisher._prepare
        def paused_prepare(job):
            entered.set()
            assert release.wait(5)
            return original_prepare(job)
        monkeypatch.setattr(publisher, "_prepare", paused_prepare)
        path = publisher.destination / (first["sha256"] + ".mp3")
        path.write_bytes(b"x" * first["bytes"])
        assert publisher.get("a") is None
        assert entered.wait(2)
        assert not publisher.futures["a"].done()
        with pytest.raises(ValueError, match="guide_unavailable"):
            publisher.media("a", "guide_original")
        release.set()
        publisher.futures["a"].result(timeout=10)
        assert publisher.get("a") == first
        assert hashlib.sha256(path.read_bytes()).hexdigest() == first["sha256"]
    finally:
        release.set()
        publisher.close()


@pytest.mark.parametrize("restart", [False, True])
def test_corrupt_encoded_cache_cannot_become_a_new_ready_source(tmp_path, restart):
    queue = synthetic_queue(tmp_path)
    publisher = GuidePublisher(queue, local=True)
    try:
        first = ready(publisher, "a")
        source_sha = queue.get("a")["media"]["original"]["sha256"]
        encoded = queue.jobs_dir / "a" / (".guide-original-" + source_sha + ".mp3")
        encoded.write_bytes(b"wrong source" * 500)
        (publisher.destination / (first["sha256"] + ".mp3")).unlink()
        if restart:
            publisher.close()
            publisher = GuidePublisher(queue, local=True)
        assert publisher.get("a") is None
        publisher.futures["a"].result(timeout=10)
        assert publisher.get("a") == first
        assert hashlib.sha256(encoded.read_bytes()).hexdigest() == first["sha256"]
        # Even a previously valid compressed cache cannot excuse a changed WAV.
        source = queue.jobs_dir / "a" / "original.wav"
        source.write_bytes(b"wrong WAV")
        (publisher.destination / (first["sha256"] + ".mp3")).unlink()
        assert publisher.get("a") is None
        with pytest.raises(ValueError, match="source_changed"):
            publisher.futures["a"].result(timeout=10)
        assert publisher.get("a") is None
    finally:
        publisher.close()


def test_media_rechecks_bytes_changed_after_ready_lookup_and_starts_repair(tmp_path, monkeypatch):
    publisher = GuidePublisher(synthetic_queue(tmp_path), local=True)
    try:
        first = ready(publisher, "a")
        path = publisher.destination / (first["sha256"] + ".mp3")
        get = publisher.get
        def change_after_lookup(job):
            value = get(job)
            path.write_bytes(b"x" * first["bytes"])
            return value
        with monkeypatch.context() as reading:
            reading.setattr(publisher, "get", change_after_lookup)
            with pytest.raises(ValueError, match="guide_changed"):
                publisher.media("a", "guide_original")
        publisher.futures["a"].result(timeout=10)
        assert publisher.get("a") == first
        with publisher.media("a", "guide_original")[0] as stream:
            assert hashlib.sha256(stream.read()).hexdigest() == first["sha256"]
    finally:
        publisher.close()


def test_compressed_original_is_verified_and_published_privately(tmp_path,monkeypatch):
    mount=tmp_path/'mounted';mount.mkdir();destination=mount/'guides';jobs=tmp_path/'jobs';folder=jobs/'synthetic';folder.mkdir(parents=True)
    source=folder/'original.wav'
    with wave.open(str(source),'wb') as out:
        out.setnchannels(2);out.setsampwidth(2);out.setframerate(48000);out.writeframes(b'\0'*48000*4*2)
    spec={'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'duration_seconds':2}
    queue=SimpleNamespace(jobs_dir=jobs,get=lambda job:{'media':{'original':spec}})
    config=tmp_path/'guide.json';config.write_text(json.dumps({'mount':str(mount),'directory':str(destination)}))
    original=Path.is_mount;monkeypatch.setattr(Path,'is_mount',lambda p:p==mount or original(p))
    publisher=GuidePublisher(queue,config)
    try:
        result=publisher._prepare('synthetic');file=destination/(result['sha256']+'.mp3')
        assert file.is_file() and file.stat().st_size==result['bytes']<source.stat().st_size
        assert hashlib.sha256(file.read_bytes()).hexdigest()==result['sha256']
        assert file.stat().st_mode&0o077==0
    finally:publisher.close()


def test_reference_is_the_aligned_accompaniment_and_both_files_are_retained(tmp_path, monkeypatch):
    mount = tmp_path / 'mounted';mount.mkdir()
    destination = mount / 'guides'
    folder = tmp_path / 'jobs/synthetic';folder.mkdir(parents=True)
    specs = {}
    for name, voice in [('original', True), ('instrumental', False)]:
        samples = []
        for frame in range(48000 * 2):
            # A shared backing cue and an original-only vocal cue, on one timeline.
            amplitude = 15000 if 24000 <= frame < 26400 else 0
            value = int(amplitude * math.sin(2 * math.pi * 800 * frame / 48000))
            if voice and 48000 <= frame < 52800:
                value += int(6000 * math.sin(2 * math.pi * 350 * frame / 48000))
            samples.extend([value, value])
        source = folder / (name + '.wav')
        with wave.open(str(source), 'wb') as out:
            out.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
            out.writeframes(struct.pack('<' + 'h' * len(samples), *samples))
        specs[name] = {'sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'duration_seconds': 2}
    config = tmp_path / 'guide.json';config.write_text(json.dumps({'mount': str(mount), 'directory': str(destination)}))
    monkeypatch.setattr(Path, 'is_mount', lambda path: path == mount)
    publisher = GuidePublisher(SimpleNamespace(jobs_dir=folder.parent, get=lambda _: {'media': specs}), config)
    try:
        publisher.protect(['synthetic'])
        result = publisher._prepare('synthetic')
        reference = result['reference']
        assert reference['kind'] == 'accompaniment'
        assert result['sha256'] != reference['sha256']
        assert result['duration_ms'] == reference['duration_ms'] == 2000
        assert publisher.job_files['synthetic'] == {result['sha256'] + '.mp3', reference['sha256'] + '.mp3'}
        decoded = []
        for media in [result, reference]:
            path = destination / (media['sha256'] + '.mp3')
            assert hashlib.sha256(path.read_bytes()).hexdigest() == media['sha256']
            raw = subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-i', str(path),
                                  '-f', 's16le', '-ac', '1', '-ar', '48000', '-'],
                                 check=True, capture_output=True).stdout
            pcm = struct.unpack('<' + 'h' * (len(raw) // 2), raw)
            assert len(pcm) == 96000
            first_loud = next(i for i, value in enumerate(pcm) if abs(value) > 4000)
            assert abs(first_loud - 24000) < 240
            decoded.append(pcm)
        assert max(map(abs, decoded[0][48000:52800])) > 4000
        assert max(map(abs, decoded[1][48000:52800])) < 20
    finally:
        publisher.close()
