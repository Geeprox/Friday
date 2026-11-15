import pytest
from homeplane.embodied import interaction
from homeplane.embodied.interaction import Binding, Configuration, InteractionEngine
from homeplane.embodied.interaction_fleet import InteractionFleet

CAMERAS=['cam_1111111111','cam_2222222222','cam_3333333333']

def people(person=None,track=7):
    return [dict(id=track,joints={},identity=dict(state='matched' if person else 'unknown',person_id=person))]

def hand(gesture,track=7):
    return [dict(person_id=track,gesture=gesture)]

@pytest.fixture
def configured(monkeypatch):
    config=Configuration(revision=1,enabled=True,bindings=[Binding(id='a'*32,camera=CAMERAS[0],gesture='hand.open_palm',target='lamp',action='on',enabled=True),Binding(id='b'*32,gesture='hand.ok',target='lamp',action='off',enabled=True)])
    monkeypatch.setattr(interaction,'load',lambda:config)
    import homeplane.embodied.interaction_fleet as fleet_module
    monkeypatch.setattr(fleet_module,'load',lambda:config,raising=False)
    return config

def update(fleet,cam,t,gesture=None,person=None,track=7,session='session',state='locked'):
    return fleet.update(cam,session,people(person,track),hand(gesture,track) if gesture else [],{'status':state},True,t)

def bind(fleet,cam,person=None,track=7):
    assert not update(fleet,cam,10,'ok',person,track)
    assert not update(fleet,cam,10.8,'ok',person,track)
    assert not update(fleet,cam,11,'ok',person,track)
    assert not update(fleet,cam,11.2,None,person,track)

@pytest.mark.parametrize('camera',CAMERAS)
def test_one_legacy_camera_binding_works_in_every_camera(configured,camera):
    fleet=InteractionFleet();bind(fleet,camera)
    assert not update(fleet,camera,11.3,'open_palm')
    result=update(fleet,camera,12.05,'open_palm')
    assert len(result)==1 and result[0]['camera']==camera and result[0]['target']=='lamp'
    assert fleet.current(result[0],12.1)
    assert not fleet.current(result[0],14)

def test_binding_ok_is_consumed_and_a_held_gesture_needs_release(configured):
    fleet=InteractionFleet()
    for n in range(100,123):
        result=update(fleet,CAMERAS[0],n/10,'ok')
        assert not result
    # Start a new hold after the binding OK was released.
    update(fleet,CAMERAS[0],12.3,None);update(fleet,CAMERAS[0],12.6,None)
    update(fleet,CAMERAS[0],12.7,'ok')
    assert len(update(fleet,CAMERAS[0],13.5,'ok'))==1
    for n in range(136,200):
        assert not update(fleet,CAMERAS[0],n/10,'ok')
    update(fleet,CAMERAS[0],20,None);update(fleet,CAMERAS[0],20.3,None)
    update(fleet,CAMERAS[0],20.4,'ok')
    assert len(update(fleet,CAMERAS[0],21.2,'ok'))==1


@pytest.mark.parametrize('camera', CAMERAS)
def test_unique_ok_can_take_over_existing_operator_without_dispatching_selection(configured, camera):
    fleet = InteractionFleet(); bind(fleet, camera)
    crowd = people(track=7) + people(track=8)
    for at in [11.3, 11.7, 12.1]:
        assert not fleet.update(camera, 'session', crowd, hand('ok', 8), {'status': 'locked'}, True, at)
        if at < 12.1:
            assert fleet.cameras[camera]['owner'] == 7
    assert fleet.cameras[camera]['owner'] == 8
    # Selection is not a configured OK action, even if the new owner keeps holding it.
    assert not fleet.update(camera, 'session', crowd, hand('ok', 8), {'status': 'locked'}, True, 12.9)
    for at in [13., 13.3]:
        assert not fleet.update(camera, 'session', crowd, [], {'status': 'locked'}, True, at)
    assert not fleet.update(camera, 'session', crowd, hand('ok', 8), {'status': 'locked'}, True, 13.4)
    result = fleet.update(camera, 'session', crowd, hand('ok', 8), {'status': 'locked'}, True, 14.2)
    assert len(result) == 1 and result[0]['track'] == 8 and result[0]['action'] == 'off'


@pytest.mark.parametrize('interruption', ['released', 'ambiguous', 'foreign_track'])
def test_interrupted_or_ambiguous_ok_cannot_finish_operator_takeover(configured, interruption):
    fleet = InteractionFleet(); bind(fleet, CAMERAS[0])
    crowd = people(track=7) + people(track=8) + people(track=9)
    assert fleet.operator(CAMERAS[0], 'session', crowd, hand('ok', 8), 11.3) == 7
    hands = {'released': [], 'ambiguous': hand('ok', 8) + hand('ok', 9),
             'foreign_track': hand('ok', 99)}[interruption]
    assert fleet.operator(CAMERAS[0], 'session', crowd, hands, 11.8) == 7
    assert fleet.operator(CAMERAS[0], 'session', crowd, hand('ok', 8), 12.2) == 7
    assert fleet.operator(CAMERAS[0], 'session', crowd, hand('ok', 8), 13.) == 8


def test_takeover_fences_previous_operator_events_and_keeps_other_camera_owner(configured):
    fleet = InteractionFleet(); bind(fleet, CAMERAS[0]); bind(fleet, CAMERAS[1])
    update(fleet, CAMERAS[0], 11.3, 'open_palm')
    event = update(fleet, CAMERAS[0], 12.05, 'open_palm')[0]
    assert fleet.current(event, 12.1)
    crowd = people(track=7) + people(track=8)
    for at in [12.1, 12.5, 12.85]:
        assert not fleet.update(CAMERAS[0], 'session', crowd, hand('ok', 8), {'status': 'locked'}, True, at)
    assert fleet.cameras[CAMERAS[0]]['owner'] == 8
    assert not fleet.current(event, 12.9)
    assert fleet.cameras[CAMERAS[1]]['owner'] == 7

def test_enrolled_operator_continues_and_overlapping_views_execute_once(configured):
    fleet=InteractionFleet();bind(fleet,CAMERAS[0],'member')
    update(fleet,CAMERAS[0],11.3,'open_palm','member')
    assert len(update(fleet,CAMERAS[0],12.05,'open_palm','member'))==1
    update(fleet,CAMERAS[1],12.1,'open_palm','member',track=99)
    assert fleet.cameras[CAMERAS[1]]['owner']==99
    assert not update(fleet,CAMERAS[1],12.9,'open_palm','member',track=99)
    for n in range(130,190):
        assert not update(fleet,CAMERAS[1],n/10,'open_palm','member',track=99)

def test_equal_track_numbers_do_not_skip_camera_local_activation(configured):
    fleet=InteractionFleet();bind(fleet,CAMERAS[0])
    assert not update(fleet,CAMERAS[1],11.3,'open_palm')
    assert fleet.cameras[CAMERAS[1]]['owner'] is None
    assert not update(fleet,CAMERAS[1],11.8,'open_palm')
    assert fleet.cameras[CAMERAS[1]]['owner'] is None
    assert not update(fleet,CAMERAS[1],12.2,'open_palm')
    assert fleet.cameras[CAMERAS[1]]['owner'] == 7
    assert not fleet.operators, 'Single-body selection must not invent cross-room identity'


@pytest.mark.parametrize('camera', CAMERAS)
def test_single_person_without_enrollment_can_use_gesture(configured, camera):
    fleet = InteractionFleet()
    for at in [100., 100.4, 100.8]:
        assert not update(fleet, camera, at)
    assert fleet.cameras[camera]['owner'] == 7
    assert not update(fleet, camera, 101.2, 'open_palm')
    assert len(update(fleet, camera, 102., 'open_palm')) == 1


def test_duplicate_tracks_revoke_existing_operator_even_with_ok(configured):
    fleet = InteractionFleet(); bind(fleet, CAMERAS[0], 'member')
    duplicated = people('member') + people('member')
    assert fleet.operator(CAMERAS[0], 'session', duplicated, hand('ok'), 11.4) is None
    assert fleet.cameras[CAMERAS[0]]['owner'] is None
    assert not fleet.current(dict(camera=CAMERAS[0],session='session',track=7,
        binding='a'*32,target='lamp',action='on',revision=1,observed_mono=11.4), 11.4)


@pytest.mark.parametrize('bad_time', [float('nan'), float('inf'), 9.])
def test_invalid_or_reversed_time_revokes_operator(configured, bad_time):
    fleet = InteractionFleet(); bind(fleet, CAMERAS[0])
    assert fleet.operator(CAMERAS[0], 'session', people(), [], bad_time) is None
    assert fleet.cameras[CAMERAS[0]]['owner'] is None

def test_stale_moving_and_restarted_session_cannot_finish_pending_gesture(configured):
    fleet=InteractionFleet();bind(fleet,CAMERAS[0])
    update(fleet,CAMERAS[0],11.3,'open_palm')
    assert not update(fleet,CAMERAS[0],12,'open_palm',state='moving')
    assert not update(fleet,CAMERAS[0],12.8,'open_palm')
    bind(fleet,CAMERAS[1]);update(fleet,CAMERAS[1],11.3,'open_palm')
    assert not update(fleet,CAMERAS[1],15,'open_palm')
    assert not update(fleet,CAMERAS[1],15.8,'open_palm',session='new-session')

def test_body_binding_does_not_require_floor_registration(configured):
    engine=InteractionEngine(CAMERAS[2],'session')
    assert not engine.update(people(),hand('open_palm'),7,{'status':'unconfigured'},True,10)
    assert len(engine.update(people(),hand('open_palm'),7,{'status':'unconfigured'},True,10.8))==1

def test_point_targets_stay_camera_specific(configured):
    configured.bindings=[Binding(id='a'*32,camera=CAMERAS[0],gesture='hand.point',target='lamp',action='on',enabled=True,anchor=[.8,.5])]
    point=[dict(person_id=7,gesture='pointing',ray=dict(origin=[.1,.5],direction=[1,0]))]
    for cam in CAMERAS:
        engine=InteractionEngine(cam,'session')
        engine.update(people(),point,7,{'status':'locked'},True,10)
        engine.update(people(),point,7,{'status':'locked'},True,10.9)
        events=engine.update(people(),hand('ok'),7,{'status':'locked'},True,11)
        assert bool(events)==(cam==CAMERAS[0])

@pytest.mark.parametrize('gesture,changes',[
 ('body.hands_up',{'leftWrist':(.4,.1),'rightWrist':(.6,.1)}),
 ('body.left_hand_up',{'leftWrist':(.4,.1),'rightWrist':(.6,.65)}),
 ('body.right_hand_up',{'leftWrist':(.4,.65),'rightWrist':(.6,.1)}),
 ('body.lean_left',{'leftShoulder':(.2,.3),'rightShoulder':(.4,.3)}),
 ('body.lean_right',{'leftShoulder':(.6,.3),'rightShoulder':(.8,.3)}),
])
def test_body_bindings_require_confident_body_relative_observation(gesture,changes):
    from homeplane.embodied.interaction import body_gestures
    joints={name:dict(x=x,y=y,confidence=.95) for name,(x,y) in {
        'leftShoulder':(.4,.3),'rightShoulder':(.6,.3),'leftHip':(.4,.6),'rightHip':(.6,.6),
        'leftWrist':(.4,.65),'rightWrist':(.6,.65),**changes}.items()}
    assert gesture in body_gestures({'joints':joints})
    joints['leftHip']['confidence']=.1
    assert not body_gestures({'joints':joints})


def test_pointing_pauses_when_background_is_compensated(configured):
    configured.bindings=[Binding(id='a'*32,camera=CAMERAS[0],gesture='hand.point',target='lamp',action='on',enabled=True,anchor=[.8,.5])]
    engine=InteractionEngine(CAMERAS[0],'session')
    point=[dict(person_id=7,gesture='pointing',ray=dict(origin=[.1,.5],direction=[1,0]))]
    engine.update(people(),point,7,{'status':'locked'},True,10)
    engine.update(people(),point,7,{'status':'locked'},True,10.9)
    assert not engine.update(people(),hand('ok'),7,{'status':'locked','compensated':True},True,11)
    assert engine.pending is None


def test_discovered_cover_never_enables_gestures_or_overwrites_edits(tmp_path,monkeypatch):
    monkeypatch.setattr(interaction,'PRIVATE',tmp_path)
    targets={'dream':dict(id='dream',name='客厅梦幻帘',kind='cover',capabilities=['cover.position'])}
    migrated=interaction.ensure_defaults(targets,set(CAMERAS))
    assert not migrated.enabled and not migrated.bindings
    edited=interaction.save(migrated.model_copy(update={'enabled':False}),targets,set(CAMERAS))
    assert interaction.ensure_defaults(targets,set(CAMERAS))==edited

@pytest.mark.parametrize('change',['disabled','deleted','revision','target'])
def test_dispatch_rechecks_current_binding_not_queued_configuration(configured,change):
    fleet=InteractionFleet();bind(fleet,CAMERAS[0]);update(fleet,CAMERAS[0],11.3,'open_palm')
    event=update(fleet,CAMERAS[0],12.05,'open_palm')[0]
    assert fleet.current(event,12.1)
    if change=='disabled':configured.enabled=False
    elif change=='deleted':configured.bindings=[]
    elif change=='revision':configured.revision+=1
    else:configured.bindings[0].target='another-target'
    assert not fleet.current(event,12.1)


@pytest.fixture
def microphones(configured):
    fleet=InteractionFleet()
    for camera in CAMERAS:fleet.session(camera,'session')
    return fleet


def voice(fleet, camera, at, *, enabled=True, session='session'):
    return fleet.voice(camera, at, enabled=enabled, session=session)


def test_occupied_view_and_empty_view_both_keep_current_microphones(microphones):
    empty, occupied, other = CAMERAS
    bind(microphones, occupied)
    for camera in (empty, occupied, other):
        assert voice(microphones, camera, 10)
    assert microphones.cameras[occupied]['owner'] == 7
    assert all(microphones.voice_current(camera, 10.1) for camera in CAMERAS)


def test_source_clear_and_session_restart_only_revoke_their_own_microphones(microphones):
    cleared, replaced, other = CAMERAS
    revoked = []
    microphones.voice_revoked = revoked.append
    for camera in CAMERAS:
        assert voice(microphones, camera, 10)
    microphones.clear(cleared)
    microphones.session(replaced, 'replacement')
    assert revoked == [cleared, replaced]
    assert not voice(microphones, replaced, 10.3)
    assert not microphones.voice_current(cleared, 10.3)
    assert not microphones.voice_current(replaced, 10.3)
    assert microphones.voice_current(other, 10.3)
    assert voice(microphones, replaced, 10.4, session='replacement')


def test_input_pause_revokes_speech_without_changing_gesture_operator(microphones):
    camera = CAMERAS[0]
    bind(microphones, camera)
    assert voice(microphones, camera, 12)
    assert not voice(microphones, camera, 12.2, enabled=False)
    assert not microphones.voice_current(camera, 12.3)
    assert microphones.cameras[camera]['owner'] == 7


def test_game_gesture_pause_preserves_current_microphone(microphones):
    camera, other = CAMERAS[:2]
    assert voice(microphones, camera, 10.1)
    assert not microphones.update(camera, 'session', people(), [], {}, False, 10.2, game_active=True)
    assert voice(microphones, other, 10.3)
    assert microphones.voice_current(camera, 10.3)
    assert microphones.voice_current(other, 10.3)


def test_uncalibrated_view_keeps_current_voice_until_the_source_closes(microphones):
    camera = CAMERAS[0]
    revoked = []
    microphones.voice_revoked = revoked.append
    assert voice(microphones, camera, 10.1)
    assert not microphones.update(camera, 'session', [], [], {'status': 'lost'}, True, 10.2)
    assert microphones.voice_current(camera, 10.2) and not revoked
    microphones.clear(camera)
    assert not microphones.voice_current(camera, 10.2) and revoked == [camera]


@pytest.mark.parametrize('invalid_time',[float('nan'),float('inf'),float('-inf'),9.9])
def test_invalid_or_old_callback_cannot_rewind_voice_lease(microphones,invalid_time):
    camera = CAMERAS[0]
    assert voice(microphones, camera, 10)
    assert not voice(microphones, camera, invalid_time, enabled=False)
    assert microphones.voice_current(camera, 10.1)
    assert microphones.cameras[camera]['voice_seen'] == 10


def test_unknown_or_late_old_session_cannot_extend_the_current_microphone(microphones):
    camera = CAMERAS[0]
    assert not voice(microphones, 'unknown', 10)
    assert voice(microphones, camera, 10)
    assert not voice(microphones, camera, 20, session='old')
    assert not microphones.voice_current(camera, 20)
    assert voice(microphones, camera, 10.1)


def test_audio_packets_do_not_renew_the_video_lease(microphones):
    camera, other = CAMERAS[:2]
    assert voice(microphones, camera, 10)
    assert voice(microphones, other, 11)
    for at in (10.1, 10.5, 11, 11.5):
        assert microphones.voice_current(camera, at)
    assert not microphones.voice_current(camera, 11.5001)
    assert not microphones.voice_current(camera, float('nan'))
    assert microphones.voice_current(other, 11.5001)
    assert microphones.cameras[camera]['voice_seen'] == 10
