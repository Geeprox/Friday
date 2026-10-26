import pytest
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeplane.embodied import airplay


@pytest.mark.asyncio
async def test_quiet_mode_stops_before_dependency_discovery_or_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(airplay, "PRIVATE", tmp_path)
    (tmp_path / "voice-paused").touch()
    monkeypatch.setattr(airplay, "status", lambda: pytest.fail("must not touch output while quiet"))
    assert await airplay.play(None, "测试") == {"status": "paused"}


@pytest.mark.asyncio
async def test_unconfigured_homepod_does_not_fall_back_to_other_speaker(tmp_path, monkeypatch):
    monkeypatch.setattr(airplay, "PRIVATE", tmp_path)
    monkeypatch.setattr(airplay, "status", lambda: dict(configured=False, dependency_available=True))
    assert (await airplay.play(None, "测试"))["reason"] == "homepod_setup_required"


@pytest.fixture
def receiver(tmp_path,monkeypatch):
    import pyatv
    from pyatv.const import DeviceState, FeatureState, PairingRequirement, Protocol
    from homeplane.embodied import synthesis
    monkeypatch.setattr(airplay,'PRIVATE',tmp_path)
    monkeypatch.setattr(airplay,'_play_lock',asyncio.Lock())
    target=tmp_path/'homepod.json'
    target.write_text(json.dumps(dict(host='192.168.50.123',identifier='HOME-POD')))
    target.chmod(0o600)
    service=SimpleNamespace(protocol=Protocol.AirPlay,properties={'model':'AudioAccessory5,1'},
                            identifier='HOME-POD',pairing=PairingRequirement.NotNeeded,credentials=None)
    config=SimpleNamespace(address='192.168.50.123',all_identifiers={'HOME-POD'},identifier='HOME-POD',
        name='客厅HomePod',services=[service],device_info=SimpleNamespace(model=SimpleNamespace(name='HomePodMini')),
        set_credentials=Mock())
    device=SimpleNamespace(
        metadata=SimpleNamespace(main_protocol=Protocol.MRP,playing=AsyncMock(return_value=SimpleNamespace(device_state=DeviceState.Idle))),
        features=SimpleNamespace(get_feature=Mock(return_value=SimpleNamespace(state=FeatureState.Available))),
        stream=SimpleNamespace(stream_file=AsyncMock()),close=Mock(return_value=set()),
        audio=SimpleNamespace(set_volume=Mock(side_effect=AssertionError('volume must stay unchanged'))))
    scan=AsyncMock(return_value=[config]);connect=AsyncMock(return_value=device)
    monkeypatch.setattr(pyatv,'scan',scan);monkeypatch.setattr(pyatv,'connect',connect)
    audio=tmp_path/'reply.wav'
    async def synthesize(text,*,file_format):
        assert file_format=='wav';audio.write_bytes(b'RIFF-SYNTHETIC-WAVE');return audio,'synthetic'
    synth=AsyncMock(side_effect=synthesize);monkeypatch.setattr(synthesis,'synthesize',synth)
    return SimpleNamespace(config=config,service=service,device=device,scan=scan,connect=connect,
                           synth=synth,audio=audio,private=tmp_path,target=target)


@pytest.mark.asyncio
async def test_friday_wav_uses_exact_receiver_without_volume_or_system_output(receiver):
    result=await airplay.play(None,'Friday 测试')
    assert result['status']=='accepted' and result['backend_completed']
    assert result['system_audio_redirected'] is False and result['physical_audio_confirmed'] is False
    receiver.device.stream.stream_file.assert_awaited_once_with(str(receiver.audio))
    receiver.device.audio.set_volume.assert_not_called()
    receiver.device.close.assert_called_once()
    assert not receiver.audio.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('state_name',['Playing','Loading','Seeking'])
async def test_existing_media_is_not_interrupted(receiver,state_name):
    from pyatv.const import DeviceState
    receiver.device.metadata.playing.return_value.device_state=getattr(DeviceState,state_name)
    assert (await airplay.play(None,'测试'))['status']=='busy'
    receiver.synth.assert_not_awaited();receiver.device.stream.stream_file.assert_not_awaited()
    receiver.device.close.assert_called_once()


@pytest.mark.asyncio
async def test_raop_default_idle_does_not_claim_receiver_is_free(receiver):
    from pyatv.const import Protocol
    receiver.device.metadata.main_protocol=Protocol.RAOP
    result=await airplay.play(None,'测试')
    assert result['reason']=='homepod_playback_state_unavailable'
    receiver.synth.assert_not_awaited();receiver.device.stream.stream_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_receiver_is_rechecked_after_synthesis(receiver):
    from pyatv.const import DeviceState
    receiver.device.metadata.playing.side_effect=[SimpleNamespace(device_state=DeviceState.Idle),
                                                 SimpleNamespace(device_state=DeviceState.Playing)]
    assert (await airplay.play(None,'测试'))['status']=='busy'
    receiver.device.stream.stream_file.assert_not_awaited()
    assert not receiver.audio.exists()


@pytest.mark.asyncio
async def test_device_identity_never_falls_back_to_another_speaker(receiver):
    receiver.config.all_identifiers={'OTHER'}
    result=await airplay.play(None,'测试')
    assert result['reason']=='homepod_not_found' and receiver.scan.await_count==2
    receiver.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_even_matching_name_and_identifier_require_actual_homepod_model(receiver):
    receiver.service.properties={'model':'已选择的媒体播放器'}
    assert (await airplay.play(None,'测试'))['reason']=='homepod_identity_mismatch'
    receiver.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_identifier_is_not_resolved_by_array_order(receiver):
    receiver.scan.return_value=[receiver.config,receiver.config]
    assert (await airplay.play(None,'测试'))['reason']=='homepod_identity_ambiguous'
    receiver.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_dhcp_address_change_recovers_only_the_pinned_identity(receiver):
    before=receiver.target.read_bytes();receiver.scan.side_effect=[[],[receiver.config]]
    receiver.config.address='192.168.50.124'
    assert (await airplay.play(None,'测试'))['status']=='accepted'
    assert receiver.scan.await_args_list[1].kwargs['identifier']=='HOME-POD'
    assert receiver.target.read_bytes()==before


@pytest.mark.asyncio
async def test_pairing_requirement_does_not_automatically_prompt_or_pair(receiver):
    from pyatv.const import PairingRequirement
    receiver.service.pairing=PairingRequirement.Mandatory
    assert (await airplay.play(None,'测试'))['reason']=='homepod_pairing_required'
    receiver.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_capability_is_checked_and_connection_closed(receiver):
    from pyatv.const import FeatureState
    receiver.device.features.get_feature.return_value.state=FeatureState.Unavailable
    assert (await airplay.play(None,'测试'))['reason']=='homepod_stream_unavailable'
    receiver.device.close.assert_called_once();receiver.synth.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_can_run_while_quiet_without_synthesis_or_streaming(receiver):
    (receiver.private/'voice-paused').touch()
    result=await airplay.probe()
    assert result['connected'] and result['status']=='ready' and result['quiet']
    receiver.synth.assert_not_awaited();receiver.device.stream.stream_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_connection_errors_do_not_leak_credentials_or_message_content(receiver):
    receiver.connect.side_effect=RuntimeError('SYNTHETIC-SECRET-credential')
    result=await airplay.play(None,'SYNTHETIC-PRIVATE-MESSAGE')
    assert result['error_class']=='RuntimeError'
    assert 'SYNTHETIC' not in json.dumps(result)


@pytest.mark.parametrize('host',['127.0.0.1','0.0.0.0','8.8.8.8','169.254.1.2','::1','fe80::1','224.0.0.1'])
def test_target_must_be_reachable_home_ipv4_not_loopback_or_public(receiver,host):
    receiver.target.write_text(json.dumps(dict(host=host,identifier='HOME-POD')))
    assert not airplay.status()['configured']


@pytest.mark.asyncio
async def test_quiet_enabled_during_stream_cancels_only_owned_stream_and_removes_file(receiver):
    entered,cancelled=asyncio.Event(),asyncio.Event()
    async def stream(_):
        entered.set()
        try:await asyncio.Event().wait()
        finally:cancelled.set()
    receiver.device.stream.stream_file.side_effect=stream
    task=asyncio.create_task(airplay.play(None,'测试'))
    await asyncio.wait_for(entered.wait(),1)
    assert (await airplay.play(None,'并发回复'))['status']=='busy'
    assert receiver.connect.await_count==1
    (receiver.private/'voice-paused').touch()
    result=await asyncio.wait_for(task,1)
    assert result['status']=='paused' and not result['backend_completed'] and cancelled.is_set()
    assert not receiver.audio.exists();receiver.device.close.assert_called_once()


@pytest.mark.asyncio
async def test_task_cancellation_closes_owned_connection_and_removes_file(receiver):
    entered=asyncio.Event()
    async def stream(_):entered.set();await asyncio.Event().wait()
    receiver.device.stream.stream_file.side_effect=stream
    task=asyncio.create_task(airplay.play(None,'测试'));await asyncio.wait_for(entered.wait(),1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    receiver.device.close.assert_called_once();assert not receiver.audio.exists()


@pytest.mark.asyncio
async def test_selection_uses_discovered_identity_and_keeps_quiet_and_existing_pairing(receiver,monkeypatch):
    saved=json.loads(receiver.target.read_text());saved['credentials']={'AirPlay':'SYNTHETIC'}
    receiver.target.write_text(json.dumps(saved));(receiver.private/'voice-paused').touch()
    monkeypatch.setattr(airplay,'configuration',lambda:{'version':1,'speech_output':'panel','cameras':['SYNTHETIC']})
    result=await airplay.select('HOME-POD',host='192.168.50.123')
    stored=json.loads(receiver.target.read_text())
    assert stored['identifier']=='HOME-POD' and stored['credentials']==saved['credentials']
    assert receiver.target.stat().st_mode & 0o777 == 0o600
    config=json.loads((receiver.private/'config.json').read_text())
    assert config['speech_output']=='homepod' and config['cameras']==['SYNTHETIC'] and result['quiet']
    receiver.connect.assert_not_awaited();receiver.device.stream.stream_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_selection_rejects_other_speakers_and_keeps_existing_configuration(receiver):
    before=receiver.target.read_bytes();receiver.service.properties={'model':'已选择的媒体播放器'}
    with pytest.raises(airplay.Unavailable,match='homepod_not_discovered'):
        await airplay.select('HOME-POD')
    assert receiver.target.read_bytes()==before


@pytest.mark.asyncio
async def test_discovery_merges_protocols_using_actual_pyatv_configuration(receiver):
    result=await airplay.discover(6,host='192.168.50.123')
    assert result==[dict(name='客厅HomePod',model='HomePodMini',homepod=True,identifier='HOME-POD',
                        addresses=['192.168.50.123'],protocols=['AirPlay'])]
    assert receiver.scan.await_args.kwargs['hosts']==['192.168.50.123']
    receiver.connect.assert_not_awaited()
