import hashlib
import threading

import numpy as np
import pytest

from homeplane.ktv_service import valid_video
from homeplane.ktv_video import align_samples, cdn_url, download


def test_constant_offset_alignment_and_edited_mv_rejection():
    rate = 1000
    song = np.random.default_rng(12).normal(0, .15, 90 * rate).astype(np.float32)
    video = np.concatenate((np.zeros(2 * rate), song))
    value = align_samples(song, video, sample_rate=rate)
    assert value["video_offset_ms"] == 2000
    assert value["window_count"] == 7 and value["score_min"] > .99
    assert value["offset_drift_ms"] == 0
    edited = np.concatenate((video[:45 * rate], video[47 * rate:]))
    with pytest.raises(ValueError, match="different_mv_arrangement"):
        align_samples(song, edited, sample_rate=rate)
    with pytest.raises(ValueError):
        align_samples(song, np.zeros_like(song), sample_rate=rate)
    with pytest.raises(ValueError):
        align_samples(np.zeros_like(song), video, sample_rate=rate)


SYNTHETIC_USERINFO = "synthetic-test-user"  # URL credentials, never a real mailbox.


@pytest.mark.parametrize("value", ["http://127.0.0.1/a", "https://evil.test/a", "file:///tmp/x",
                                  "https://a.vod.126.net.evil.test/a", f"https://{SYNTHETIC_USERINFO}@a.vod.126.net/a",
                                  "https://a.vod.126.net:1234/a"])
def test_cdn_rejects_untrusted_hosts(value):
    with pytest.raises(ValueError):
        cdn_url(value)


def test_cdn_only_upgrades_the_exact_official_host():
    assert cdn_url("http://a.vod.126.net/video.mp4?token=opaque") == "https://a.vod.126.net/video.mp4?token=opaque"


def test_video_contract_rejects_source_mismatch_nan_and_drift():
    source = hashlib.sha256(b"original").hexdigest()
    value = {"status": "ready", "encoding": "h264_mp4_video_only", "bytes": 1000,
             "sha256": "1" * 64, "source_audio_sha256": source, "duration_seconds": 120,
             "video_offset_ms": 2000, "score_min": .999, "window_count": 7, "offset_drift_ms": 0}
    assert valid_video(value, source)
    for change in ({"source_audio_sha256": "2" * 64}, {"score_min": float("nan")},
                   {"offset_drift_ms": 26}, {"window_count": 1}, {"video_offset_ms": 20000}):
        assert not valid_video({**value, **change}, source)


def test_download_budget_rejected_before_network(tmp_path):
    with pytest.raises(ValueError, match="video_budget_exceeded"):
        download("https://a.vod.126.net/a", 2 ** 30, tmp_path / "clip.mp4", threading.Event())
    assert not list(tmp_path.iterdir())


def test_normalized_delivery_uses_picture_duration_and_short_gop(tmp_path):
    import json
    import shutil
    import subprocess
    import threading

    from homeplane.ktv_video import normalize_video
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.skip('local media tools unavailable')
    source, output = tmp_path/'synthetic.mp4', tmp_path/'delivery.mp4'
    subprocess.run([ffmpeg,'-nostdin','-v','error','-f','lavfi','-i','testsrc=size=64x64:rate=24',
                    '-t','3','-c:v','libx264','-pix_fmt','yuv420p','-g','240',str(source)],check=True)
    delivery=normalize_video(source,output,threading.Event(),ffmpeg=ffmpeg,ffprobe=ffprobe,duration=3.6)
    assert abs(delivery['duration_seconds']-3)<.15
    raw=subprocess.check_output([ffprobe,'-v','error','-select_streams','v:0','-skip_frame','nokey',
        '-show_frames','-show_entries','frame=best_effort_timestamp_time','-of','json',str(output)])
    frames=json.loads(raw)['frames']
    stamps=[float(f['best_effort_timestamp_time']) for f in frames]
    assert len(stamps)>=3 and max(b-a for a,b in zip(stamps,stamps[1:]))<=1.05

    assert delivery['encoding']=='h264_mp4_silent_clock'
    pcm=subprocess.check_output([ffmpeg,'-nostdin','-v','error','-i',str(output),'-map','0:a:0','-f','s16le','-'])
    assert len(pcm)>48000*2*2*2
    assert np.max(np.abs(np.frombuffer(pcm,dtype='<i2').astype(np.int32)))==0


def test_same_mv_audio_proof_does_not_require_album_duration_or_waveform_offset():
    source='a'*64
    value={'status':'ready','encoding':'h264_mp4_silent_clock','bytes':2000,'sha256':'b'*64,
        'duration_seconds':310,'source_audio_sha256':source,'video_offset_ms':0,
        'method':'same_mv_audio','derived_from_video_sha256':'c'*64}
    assert valid_video(value,source)
    assert not valid_video({**value,'video_offset_ms':1500},source)
    assert not valid_video({**value,'derived_from_video_sha256':'invalid'},source)
    assert not valid_video(value,'d'*64)


def test_mv_audio_extraction_uses_movie_timeline_with_intro_and_padding(tmp_path):
    import shutil,subprocess,wave
    from homeplane.ktv_video import extract_mv_audio,prepare_same_mv
    from homeplane.ktv_service import transcode_pcm,digest
    ffmpeg,ffprobe=shutil.which('ffmpeg'),shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:pytest.skip('local media tools unavailable')
    movie=tmp_path/'.mv-source.mp4';audio=tmp_path/'.mv-audio.wav'
    # Synthetic picture is longer than its audio: edited silent material must remain in the song timeline.
    subprocess.run([ffmpeg,'-nostdin','-v','error','-f','lavfi','-i','testsrc=size=64x64:rate=24:duration=4',
        '-f','lavfi','-i','sine=frequency=300:sample_rate=48000:duration=2','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(movie)],check=True)
    duration=extract_mv_audio(movie,audio,threading.Event(),ffmpeg=ffmpeg,ffprobe=ffprobe)
    with wave.open(str(audio)) as w:assert abs(w.getnframes()/w.getframerate()-4)<.03
    import json
    (tmp_path/'mv-source.json').write_text(json.dumps({'song_id':'123','mv_id':'456','mv_sha256':digest(movie),'duration_seconds':duration}))
    original=tmp_path/'original.wav';transcode_pcm(audio,original,threading.Event(),ffmpeg=ffmpeg)
    result=prepare_same_mv('123',original,tmp_path,threading.Event())
    assert result['method']=='same_mv_audio' and result['video_offset_ms']==0
    assert valid_video(result,digest(original))
    assert abs(result['duration_seconds']-duration)<.15
