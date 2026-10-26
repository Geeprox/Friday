import asyncio,base64,struct,itertools
import pytest
from homeplane.embodied.video_relay import VideoRelay,HEADER,MAX_GOP

sequence=itertools.count(1)

def packet(nals,at=100.,codec='h264'):
    data=b''.join(b'\x00\x00\x00\x01'+n for n in nals)
    return dict(source_sequence=next(sequence),codec=codec,received_at=at,width=1920,height=1080,data=base64.b64encode(data).decode())

def key():return packet([b'\x67sps',b'\x68pps',b'\x65frame'])

def test_parameters_required_and_non_key_does_not_bootstrap():
    r=VideoRelay(clock=lambda:100.)
    r.offer(packet([b'\x41delta']));r.offer(packet([b'\x65frame']))
    assert not r.records
    r.offer(key());assert len(r.records)==1
    wire=r.records[0][2];size,codec,w,h,at,seq=HEADER.unpack(wire[:HEADER.size])
    assert (codec,w,h,at)==(1,1920,1080,100000) and len(wire)==HEADER.size+size
    r.offer(packet([b'\x41delta']));assert len(r.records)==2

def test_hevc_parameter_sets_and_epoch_revocation():
    r=VideoRelay(clock=lambda:100.)
    r.offer(packet([bytes([k<<1,1])+b'unit' for k in [32,33,34,19]],codec='hevc'))
    assert len(r.records)==1 and HEADER.unpack(r.records[0][2][:HEADER.size])[1]==2
    epoch=r.epoch;r.clear();assert not r.records and not r.parameters and r.epoch>epoch

@pytest.mark.parametrize('field,value',[('received_at',98),('received_at',101),('received_at',float('nan')),('width',999999),('codec','unknown'),('data','!bad!')])
def test_untrusted_packet_bounds(field,value):
    r=VideoRelay(clock=lambda:100.);p=key();p[field]=value;r.offer(p);assert not r.records

def test_memory_budget_discards_whole_gop_until_key():
    r=VideoRelay(clock=lambda:100.);r.offer(key())
    for _ in range(8):r.offer(packet([b'\x41'+b'x'*(1024*1024)]))
    assert not r.records and r.bytes==0
    r.offer(packet([b'\x41delta']));assert not r.records
    r.offer(key());assert r.records and r.bytes<MAX_GOP

def test_slow_viewer_disconnects_instead_of_skipping_reference_frame():
    async def run():
        r=VideoRelay(clock=lambda:100.);r.offer(key());stream=r.stream();await anext(stream)
        r.offer(packet([b'\x41missed']));r.offer(key())
        with pytest.raises(StopAsyncIteration):await anext(stream)
        assert r.subscribers==0
    asyncio.run(run())

def test_privacy_clear_ends_active_viewer():
    async def run():
        r=VideoRelay(clock=lambda:100.);r.offer(key());stream=r.stream();await anext(stream)
        r.clear()
        with pytest.raises(StopAsyncIteration):await anext(stream)
        assert r.subscribers==0
    asyncio.run(run())


def test_upstream_reference_gap_revokes_gop_and_waits_for_new_key():
    r=VideoRelay(clock=lambda:100.);first=key();r.offer(first)
    skipped=packet([b'\x41lost'])
    r.offer(packet([b'\x41after-loss']))
    assert not r.records and r.parameters
    r.offer(packet([b'\x65new-key']));assert len(r.records)==1
