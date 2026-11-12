"""Original production display contract and game engine; no worker/hardware import."""
import ast
from pathlib import Path
import pytest
from pydantic import ValidationError
from homeplane.embodied.game_presentation import GameViewReport
from homeplane.embodied.motion_games import MotionGames


def engine():
    now=[0.0];tree=ast.parse(Path('homeplane/embodied/worker.py').read_text())
    assignment=next(node for node in tree.body if isinstance(node,ast.Assign)and any(isinstance(t,ast.Name)and t.id=='games'for t in node.targets))
    scope={'MotionGames':lambda **kwargs:MotionGames(clock=lambda:now[0],**kwargs)}
    exec(compile(ast.Module(body=[assignment],type_ignores=[]),'worker-game-construction','exec'),scope)
    return scope['games'],now


def view(game,**changes):
    presentation=game.presentation.public(game.presentation_scope())
    data=dict(expected_context=game.control_context,expected_round=game.round_id,mode=game.selected_mode,
              expected_view=presentation['context'],view_id='1'*32,sequence=0,window=presentation['window'],visible=True,ready=True)
    data.update(changes);return data


def test_real_worker_requires_presentation_without_importing_it():
    game,_=engine();assert game.presentation is not None


def test_round_context_sequence_and_expiry_cannot_revive_a_closed_display():
    game,now=engine();body=view(game)
    assert game.observe_presentation(body)['ready']
    now[0]=1;assert game.observe_presentation(body)['ready']
    now[0]=2.6
    with pytest.raises(ValueError):game.observe_presentation({**body,'sequence':1})
    fresh=view(game,view_id='2'*32);game.observe_presentation(fresh)
    game.observe_presentation({**fresh,'sequence':2,'visible':False,'ready':False})
    with pytest.raises(ValueError):game.observe_presentation({**fresh,'sequence':1})
    assert not game.presentation.ready(game.presentation_scope())
    game.select('ski')
    with pytest.raises(ValueError):game.observe_presentation(fresh)


def test_one_hidden_or_unready_view_does_not_pause_another_ready_screen():
    game,_=engine();one=view(game);two=view(game,view_id='2'*32)
    game.observe_presentation(one);game.observe_presentation(two)
    assert game.observe_presentation({**one,'sequence':1,'visible':False,'ready':False})['ready']
    assert not game.observe_presentation({**two,'sequence':1,'ready':False})['ready']
    assert game.observe_presentation({**two,'sequence':2})['ready']


def test_loading_never_advances_or_accepts_a_held_start_gesture():
    game,now=engine();game.select('ski')
    for index in range(30):
        now[0]=index*.1;game.update([],{'status':'locked'},now[0],[])
    assert game.elapsed==0 and game.distance==0 and game.mode=='off'
    with pytest.raises(ValueError,match='game_presentation_not_ready'):game.require_game_view('ski')
    game.observe_presentation(view(game));game.require_game_view('ski');game.reset('ski')
    game.update([],{'status':'locked'},now[0]);assert game.elapsed==0 and game.presentation_blocked


def test_losing_all_views_preserves_progress_and_requires_explicit_fresh_resume():
    game,now=engine();game.reset('ski');game.observe_presentation(view(game))
    game.elapsed=5;game.distance=123;game.gates_passed=2
    now[0]=3;game.update([],{'status':'locked'},now[0])
    assert game.distance==123 and game.gates_passed==2 and game.elapsed==5
    assert game.public['source_recovery']['phase']=='display' and game.manually_paused
    with pytest.raises(ValueError,match='game_presentation_not_ready'):game.set_paused(False)
    game.observe_presentation(view(game,view_id='2'*32));game.update([],{'status':'locked'},now[0])
    assert game.source_resume_required
    with pytest.raises(ValueError,match='game_input_not_ready'):game.set_paused(False)


@pytest.mark.parametrize('change',[{'ready':1},{'sequence':True},{'visible':False},{'mode':'home'},{'expected_round':0},{'raw_frame':'never accepted'}])
def test_view_messages_do_not_accept_source_data_or_ambiguous_types(change):
    game,_=engine()
    with pytest.raises(ValidationError):GameViewReport.model_validate(view(game,**change))


def test_an_unready_report_overtaking_registration_rejects_the_late_positive():
    game,_=engine();old=view(game)
    game.observe_presentation({**old,'sequence':1,'ready':False})
    with pytest.raises(ValueError):game.observe_presentation(old)
    assert not game.presentation.ready(game.presentation_scope())
    game.observe_presentation({**old,'sequence':2});assert game.presentation.ready(game.presentation_scope())


@pytest.mark.asyncio
async def test_original_worker_routes_gate_start_and_scope_the_new_round_without_home_effects():
    from tests.embodied.test_game_controls import handler,request,game_handler
    from fastapi import HTTPException
    game,now=engine();game.select('ski');control,start,home=game_handler(game)
    display=handler('homeplane/embodied/worker.py','game_presentation',games=game)
    with pytest.raises(HTTPException)as error:await control(request({'action':'start','mode':'ski'},game=game))
    assert error.value.status_code==409;start.assert_not_awaited()
    import json,subprocess
    def client(**extra):
        result=subprocess.run(['node','tests/game_presentation_packets.cjs'],input=json.dumps(dict(game=game.public,**extra)),text=True,capture_output=True,timeout=15)
        assert result.returncode==0,result.stdout+result.stderr
        return json.loads(result.stdout)
    [first]=client();assert first['path']=='/api/game/presentation'
    first=first['body'];response=await display(request(first));assert response['view_ready']
    with pytest.raises(HTTPException)as error:await display(request(first,origin='https://example.invalid'))
    assert error.value.status_code==403
    requests=client(ack=response,start=True);command=next(call['body']for call in requests if call['path']=='/api/game')
    old_round=game.round_id;result=await control(request(command))
    assert result['round_id']>old_round and result['presentation']['required'] and not result['presentation']['ready']
    start.assert_awaited_once();home.assert_not_awaited()
    with pytest.raises(HTTPException):await display(request(first))
    await display(request(view(game)));game.update([],{'status':'locked'},now[0]);assert game.elapsed==0


def test_original_stage_presentation_lifecycle():
    import subprocess
    result=subprocess.run(['node','--test','tests/game_presentation.test.cjs'],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    result=subprocess.run(['node','tests/embodied_games_boot.cjs'],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr


def test_visible_display_cannot_replace_a_missing_player_or_camera():
    game,now=engine();game.reset('ski')
    for index in range(60):
        now[0]=index*.1;game.observe_presentation(view(game,sequence=index))
        game.update([],{'status':'locked'},now[0])
    assert game.elapsed==0 and game.distance==0 and game.public['status']=='waiting'


def test_original_idle_view_refreshes_the_display_window_without_claiming_camera_or_player_readiness(tmp_path):
    from types import SimpleNamespace
    game,now=engine();game.select('ski');cached=game.public['presentation']['window']
    camera={'id':'SYNTHETIC-camera'};home=SimpleNamespace(public=lambda:{})
    from homeplane.embodied.party_camera import PartyCamera
    scope=dict(games=game,party_camera=PartyCamera(),state={'status':'starting','processed_frames':0},recorder=home,home=home,
               safety=SimpleNamespace(recording_status=lambda:{}),SELECTED=camera,secondary_homes={},
               fleet=SimpleNamespace(public=lambda at:{}),PRIVATE=tmp_path,camera_planner=None,last_frame=None,
               time=SimpleNamespace(monotonic=lambda:now[0],time=lambda:100+now[0]),configuration=lambda:{'cameras':[camera]})
    node=next(node for node in ast.parse(Path('homeplane/embodied/worker.py').read_text()).body if isinstance(node,ast.FunctionDef)and node.name=='view')
    exec(compile(ast.Module(body=[node],type_ignores=[]),'original-worker-view','exec'),scope)
    now[0]=3;report=scope['view']()
    assert report['game']['presentation']['window']!=cached and report['status']=='starting'
    game.observe_presentation(view(game,window=report['game']['presentation']['window']))
    assert game.presentation.ready(game.presentation_scope()) and not report['game']['players'] and game.elapsed==0


def test_loading_the_next_tutorial_keeps_the_original_player_and_hiding_still_requires_resume():
    from tests.embodied.test_motion_games import human,LOCK
    game,now=engine();game.reset('quest');sequence=0
    for index in range(240):
        now[0]=index*.05
        if index%8==0:game.observe_presentation(view(game,sequence=sequence));sequence+=1
        game.update([human(1,arms='horizontal')],LOCK,now[0])
        if game.level>0 and game.quest_transition>0:break
    assert game.level>0 and game.quest_transition>0 and game.slots==[1]
    previous=game.quest_transition
    # 浏览器尚未来得及确认新站点，worker 已经处理下一帧。
    now[0]+=.05;game.update([human(2)],LOCK,now[0]);assert not game.source_resume_required
    assert game.quest_transition==previous and game.slots==[1]
    with pytest.raises(ValueError,match='game_presentation_not_ready'):game.require_game_view('quest')
    sequence=0;game.observe_presentation(view(game,sequence=sequence,ready=False));sequence+=1
    game.update([human(2)],LOCK,now[0]);assert not game.source_resume_required and game.slots==[1]
    game.observe_presentation(view(game,sequence=sequence));sequence+=1
    previous=game.quest_transition
    for index in range(8):
        now[0]+=.05;game.update([human(2)],LOCK,now[0])
    assert game.quest_transition==previous and game.slots==[1]
    game.observe_presentation(view(game,sequence=sequence,ready=False));sequence+=1
    game.update([human(2)],LOCK,now[0])
    game.observe_presentation(view(game,sequence=sequence,visible=False,ready=False))
    game.update([human(2)],LOCK,now[0]);assert game.source_resume_required


def test_missing_next_step_report_does_not_extend_the_previous_screen_deadline():
    from tests.embodied.test_motion_games import human,LOCK
    game,now=engine();game.reset('quest');game.elapsed=4;game.slots=[1]
    game.observe_presentation(view(game))
    now[0]=2;game.level=1;game.quest_transition=3
    game.update([human(2)],LOCK,now[0]);assert not game.source_resume_required
    assert game.slots==[1] and game.quest_transition==3
    now[0]=2.6;game.update([human(2)],LOCK,now[0])
    assert game.source_resume_required and game.quest_transition==3 and game.elapsed==4


def test_hidden_first_next_step_report_revokes_inherited_visibility():
    from tests.embodied.test_motion_games import human,LOCK
    game,now=engine();game.reset('quest');game.elapsed=4;game.slots=[1]
    game.observe_presentation(view(game));game.level=1;game.quest_transition=3
    pending=view(game,view_id='2'*32)
    game.observe_presentation({**pending,'sequence':1,'visible':False,'ready':False})
    with pytest.raises(ValueError):game.observe_presentation(pending)
    game.update([human(1)],LOCK,now[0])
    assert game.source_resume_required and game.quest_transition==3 and game.elapsed==4


def test_hidden_first_next_step_report_cannot_retire_another_current_ready_view():
    from tests.embodied.test_motion_games import human,LOCK
    game,now=engine();game.reset('quest');game.elapsed=4;game.slots=[1]
    game.observe_presentation(view(game));game.level=1;game.quest_transition=3
    game.observe_presentation(view(game,view_id='2'*32))
    game.observe_presentation(view(game,view_id='3'*32,sequence=1,visible=False,ready=False))
    game.update([human(1)],LOCK,now[0])
    assert not game.source_resume_required and game.slots==[1]
    assert game.presentation.ready(game.presentation_scope())


def test_a_previous_exercise_view_cannot_mark_the_next_instruction_ready():
    game,_=engine();game.reset('quest');old=view(game);game.observe_presentation(old)
    game.level=1;current=game.viewer_state()['presentation']
    assert current['context']!=old['expected_view'] and not current['ready']
    with pytest.raises(ValueError):game.observe_presentation({**old,'sequence':1})
    game.observe_presentation(view(game));assert game.presentation.ready(game.presentation_scope())


def test_real_body_progress_freezes_when_the_last_view_leaves_and_does_not_catch_up():
    from tests.embodied.test_motion_games import human,LOCK
    game,now=engine();game.reset('ski');sequence=0
    for index in range(180):
        now[0]=index*.05
        if index%8==0:
            game.observe_presentation(view(game,sequence=sequence));sequence+=1
        person=human(1,arms='down')
        for side,offset in [('left',0),('right',1)]:person['joints'][side+'Wrist']['y']=.3+(.15 if(index//8+offset)%2 else-.15)
        game.update([person],LOCK,now[0])
    assert game.distance>0
    before=(game.distance,game.elapsed,game.gates_passed,game.collisions)
    game.observe_presentation(view(game,sequence=sequence,visible=False,ready=False))
    for index in range(60):
        now[0]+=.1;game.update([human()],LOCK,now[0])
    assert (game.distance,game.elapsed,game.gates_passed,game.collisions)==before
    assert game.public['source_recovery']['phase']=='display'
    sequence=0
    for index in range(80):
        now[0]+=.05
        if index%8==0:game.observe_presentation(view(game,view_id='2'*32,sequence=sequence));sequence+=1
        game.update([human()],LOCK,now[0])
    assert game.recovery_public()['ready'] and game.distance==before[0]
    game.set_paused(False);game.update([human()],LOCK,now[0]);assert game.distance==before[0] and game.resume_seconds==3
