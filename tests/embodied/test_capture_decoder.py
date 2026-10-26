from types import SimpleNamespace
from unittest.mock import Mock

import av
import pytest
MIoTCameraCodec = pytest.importorskip('miot.types', reason='Optional operator-installed Miloco SDK').MIoTCameraCodec
from homeplane.embodied.capture_decoder import install_decoder


def setup(monkeypatch, *, failing=False):
    clock=SimpleNamespace(now=100.)
    image=object()
    frame=SimpleNamespace(to_ndarray=Mock(return_value=image),pts=None)
    def decode(encoded):
        if failing:raise ValueError
        frame.pts=encoded.pts
        return [frame]
    context=SimpleNamespace(decode=Mock(side_effect=decode),is_hwaccel=True)
    factory=Mock(return_value=context)
    monkeypatch.setattr(av,'CodecContext',SimpleNamespace(create=factory))
    class Base:
        def __init__(self):
            self._video_decoder=None;self._enable_hw_accel=True
            self._video_frame_callback=Mock(return_value=object())
            self._main_loop=SimpleNamespace(create_task=Mock(),call_soon_threadsafe=Mock())
        def _on_audio_callback(self,data):return 'audio'
        def stop(self):return 'joined'
    module=SimpleNamespace(MIoTMediaDecoder=Base)
    cls=install_decoder(module,clock=lambda:clock.now,wall=lambda:clock.now)
    return cls(),module,clock,frame,context,factory


def packet(at=100.,codec=MIoTCameraCodec.VIDEO_H265):
    return SimpleNamespace(codec_id=codec,data=b'packet',timestamp=123,channel=0,recv_unix_ms=at*1000)


def test_decode_every_packet_but_only_convert_at_five_fps(monkeypatch):
    d,_,clock,frame,context,_=setup(monkeypatch)
    for i in range(20):
        clock.now=100+i*.05;d._on_video_callback(packet(clock.now))
    assert context.decode.call_count==20
    assert frame.to_ndarray.call_count==5
    assert d._video_frame_callback.call_count==5
    assert d._video_frame_callback.call_args.args[3]==100800
    assert d.friday_status()==dict(hardware_decoding=True,decoded_fps=20.)
    assert d.stop()=='joined' and d._on_audio_callback(None)=='audio'


@pytest.mark.parametrize('age',[-.01,.501,100])
def test_future_or_old_packet_decodes_reference_but_never_converts(monkeypatch,age):
    d,_,clock,frame,context,_=setup(monkeypatch)
    d._on_video_callback(packet(clock.now-age))
    context.decode.assert_called_once();frame.to_ndarray.assert_not_called()
    d._video_frame_callback.assert_not_called()


def test_decoder_errors_switch_once_without_replaying_packet(monkeypatch):
    d,_,clock,frame,context,factory=setup(monkeypatch,failing=True)
    for i in range(5):d._on_video_callback(packet())
    assert context.decode.call_count==5
    assert factory.call_count==2 and d._friday_software
    assert factory.call_args.kwargs=={'hwaccel':None}
    frame.to_ndarray.assert_not_called()


def test_install_is_idempotent_and_unknown_codec_never_touches_context(monkeypatch):
    d,module,_,frame,context,factory=setup(monkeypatch)
    assert install_decoder(module) is type(d)
    d._on_video_callback(packet(codec=-1))
    factory.assert_not_called();context.decode.assert_not_called()


def test_multiple_frames_only_latest_is_converted_with_original_receipt(monkeypatch):
    d,_,_,frame,context,_=setup(monkeypatch)
    older=SimpleNamespace(to_ndarray=Mock(),pts=None)
    context.decode.side_effect=None
    frame.pts=1
    context.decode.return_value=[older,frame]
    d._on_video_callback(packet(99.9))
    older.to_ndarray.assert_not_called();frame.to_ndarray.assert_called_once_with(format='bgr24',threads=1)
    assert d._video_frame_callback.call_args.args[3:]==(99900,100000)


def test_delayed_frame_keeps_original_receipt_and_stale_delayed_frame_is_rejected(monkeypatch):
    d,_,clock,frame,context,_=setup(monkeypatch)
    context.decode.side_effect=None;context.decode.return_value=[]
    d._on_video_callback(packet(100))
    clock.now=100.3;frame.pts=1;context.decode.return_value=[frame]
    d._on_video_callback(packet(clock.now))
    assert d._video_frame_callback.call_args.args[3]==100000
    clock.now=101;frame.pts=2
    d._on_video_callback(packet(clock.now))
    assert d._video_frame_callback.call_count==1


def test_unknown_output_pts_and_duplicate_output_never_become_fresh(monkeypatch):
    d,_,clock,frame,context,_=setup(monkeypatch)
    context.decode.side_effect=None;context.decode.return_value=[frame];frame.pts=None
    d._on_video_callback(packet())
    frame.to_ndarray.assert_not_called()
    frame.pts=1;clock.now=100.2;d._on_video_callback(packet(clock.now))
    clock.now=100.4;d._on_video_callback(packet(clock.now))
    assert d._video_frame_callback.call_count==1


def test_pixel_download_failure_is_bounded_and_discards_old_context_receipts(monkeypatch):
    d,_,clock,frame,context,factory=setup(monkeypatch)
    frame.to_ndarray.side_effect=ValueError
    for i in range(3):
        clock.now=100+i*.25;d._on_video_callback(packet(clock.now))
    assert d._friday_software and d._video_decoder is None and not d._friday_receipts
    d._video_frame_callback.assert_not_called()
