"""Privacy policy and real codecs using generated, non-private patterns only."""

import hashlib
import io
import json
import shutil
import subprocess

import numpy as np
import pytest

from homeplane.archive_privacy import FrameProtection
from homeplane.archive_privacy_range import serve
from homeplane.archive_privacy_worker import render
from homeplane.archive_worker import signature


class Positive:
    def __init__(self, *_):
        pass

    def detect(self, image):
        return {
            "parts": [{"class": "MALE_GENITALIA_EXPOSED", "score": 0.9, "box": [170, 100, 20, 20]}],
            "people": [[140, 20, 100, 150]],
        }


def patterned():
    return np.random.default_rng(419).integers(0, 256, (180, 320, 3), dtype=np.uint8)


def test_localized_person_mosaic_preserves_source_and_unaffected_area():
    image = patterned()
    original = image.copy()
    result = FrameProtection(Positive()).apply(image, 0)
    assert np.array_equal(image, original)
    assert np.array_equal(result[:, :100], image[:, :100])
    assert not np.array_equal(result[80:130, 160:210], image[80:130, 160:210])
    assert result[90:97, 172:178].std() < image[90:97, 172:178].std()


@pytest.mark.parametrize(
    "analysis",
    [
        None,
        {},
        {"parts": [], "people": "invalid"},
        {
            "parts": [{"class": "MALE_GENITALIA_EXPOSED", "score": float("nan"), "box": [1, 2, 3, 4]}],
            "people": [],
        },
        {
            "parts": [{"class": "MALE_GENITALIA_EXPOSED", "score": 0.9, "box": [-100, -100, 1, 1]}],
            "people": [],
        },
        {"parts": [{"class": "MALE_GENITALIA_EXPOSED", "score": 0.22, "box": [1, 2, 3, 4]}], "people": []},
        {"parts": [{"class": "MALE_GENITALIA_EXPOSED", "score": 0.9, "box": [1, 2, 3, 4]}], "people": []},
    ],
)
def test_unknown_or_ambiguous_is_whole_frame_protection(analysis):
    assert FrameProtection().regions(analysis, 320, 180, 0) == [(0, 0, 320, 180)]


def test_detector_missing_or_throwing_never_returns_source_pixels():
    class Broken:
        def detect(self, _):
            raise RuntimeError("SIMULATED-private-content")

    source = patterned()
    for detector in (None, Broken()):
        result = FrameProtection(detector).apply(source, 0)
        assert not np.array_equal(result, source)
        assert len(np.unique(result.reshape(-1, 3), axis=0)) <= 8 * 12


def test_one_second_guard_prevents_drop_flicker():
    policy = FrameProtection()
    assert policy.regions(Positive().detect(None), 320, 180, 1)
    assert policy.regions({"parts": [], "people": []}, 320, 180, 1.5) == [(0, 0, 320, 180)]
    assert policy.regions({"parts": [], "people": []}, 320, 180, 2.1) == []


@pytest.fixture
def source(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("Installed ffmpeg required for synthetic codec validation")
    folder, cache = tmp_path.resolve() / "original", tmp_path.resolve() / "protected"
    folder.mkdir()
    cache.mkdir(mode=0o700)
    path = folder / "synthetic.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=8",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=550:sample_rate=16000",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
    )
    return folder, cache, path


def job(path):
    return {
        "path": path.name,
        "signature": signature(path.stat()),
        "output": "1" * 32 + ".mp4",
        "model": str(path.parent / "SIMULATED-unconfigured.onnx"),
    }


def test_real_encode_decode_audio_range_and_source_integrity(source):
    import av

    folder, cache, path = source
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    request = job(path)
    result = render(str(folder), str(cache), request, detector_factory=Positive)
    target = cache / request["output"]
    subprocess.run(
        [shutil.which("ffmpeg"), "-v", "error", "-xerror", "-i", str(target), "-f", "null", "-"], check=True
    )
    with av.open(str(target)) as video:
        assert len(video.streams.audio) == 1
        assert abs(video.duration / av.time_base - 2.0) < 0.2
        frames = list(video.decode(video=0))
        assert len(frames) == 16 and abs(float(frames[0].pts * frames[0].time_base)) < 0.01
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert target.stat().st_mode & 0o777 == 0o600
    output = io.BytesIO()
    range_request = {
        "path": request["output"],
        "signature": result["signature"],
        "start": 0,
        "length": 64,
        "source_path": path.name,
        "source_signature": request["signature"],
    }
    assert serve(str(cache), str(folder), range_request, output)
    header, body = output.getvalue().split(b"\n", 1)
    assert json.loads(header)["status"] == "readable" and body == target.read_bytes()[:64]
    path.touch()
    output = io.BytesIO()
    with pytest.raises(ValueError, match="source_changed"):
        serve(str(cache), str(folder), range_request, output)
    assert not output.getvalue()


def test_missing_model_still_produces_only_protected_video(source):
    import av
    import cv2

    folder, cache, path = source
    before = path.read_bytes()
    result = render(
        str(folder), str(cache), job(path), detector_factory=lambda _: (_ for _ in ()).throw(ValueError())
    )
    assert result["privacy"] == "local_full_frame_mosaic" and path.read_bytes() == before
    with av.open(str(path)) as original, av.open(str(cache / job(path)["output"])) as preview:
        raw = next(original.decode(video=0)).to_ndarray(format="gray")
        protected = next(preview.decode(video=0)).to_ndarray(format="gray")
        assert (
            np.abs(cv2.Laplacian(protected, cv2.CV_32F)).mean()
            < np.abs(cv2.Laplacian(raw, cv2.CV_32F)).mean() * 0.5
        )


def test_source_replacement_and_symlink_destination_fail_before_publish(source):
    folder, cache, path = source
    request = job(path)
    request["signature"] = "0" * 64
    with pytest.raises(ValueError, match="source_changed"):
        render(str(folder), str(cache), request, detector_factory=Positive)
    assert not list(cache.iterdir())
    request = job(path)
    (cache / request["output"]).symlink_to(path)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        render(str(folder), str(cache), request, detector_factory=Positive)
    assert path.read_bytes() == before
