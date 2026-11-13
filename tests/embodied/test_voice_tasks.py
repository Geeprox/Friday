"""Original admitted voice -> pending task -> Console approval -> original rules.

Inputs and model receipts are explicitly synthetic; no real device or model I/O.
"""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from homeplane.embodied.camera_provenance import camera_source_context
from homeplane.embodied.home_service import HomeService
from homeplane.embodied.voice_preferences import VoiceEdit, VoicePreferencesStore
from homeplane.runtime_log import RuntimeLog
from tests.embodied.test_voice_questions import CAMERA, KEY, after_standalone_wake, transcript
from tests.test_homeplane_core_rules import event
from tests.test_homeplane_home_flow import bootstrap, domain, request, turn
from tests.test_homeplane_task_composition import TEXT, TaskRuntime, task_app


@asynccontextmanager
async def setup(tmp_path, monkeypatch, model=None):
    import homeplane.embodied.api as api
    import homeplane.embodied.bridge as bridge
    import homeplane.embodied.home_service as home
    import homeplane.embodied.paths as paths

    private = tmp_path / "voice"
    private.mkdir()
    selected = dict(
        id=CAMERA,
        room="客厅",
        name="合成摄像头",
        model="SYNTHETIC",
        region="cn",
        device={"did": "SYNTHETIC-task-camera"},
    )
    config = private / "config.json"
    config.write_text(
        json.dumps(
            dict(version=1, main_origin="http://127.0.0.1:18743", default_camera=CAMERA, cameras=[selected])
        )
    )
    config.chmod(0o600)
    (private / "control-key").write_text(KEY)
    monkeypatch.setattr(api, "PRIVATE", private)
    monkeypatch.setattr(paths, "PRIVATE", private)
    monkeypatch.setattr(bridge, "PRIVATE", private)
    monkeypatch.setattr(home, "VOICE_INPUT_PAUSED", private / "voice-input-paused")
    preferences = VoicePreferencesStore(private)
    model = model or TaskRuntime()
    async with task_app(tmp_path, model) as (app, client, calls, session):
        app.state.embodied.process = SimpleNamespace(returncode=None)
        app.state.embodied.capture_enabled = app.state.embodied.interaction_enabled = True
        app.state.infrastructure.policy = app.state.infrastructure.policy.model_copy(update={"enabled": True})
        client_type = httpx.AsyncClient
        monkeypatch.setattr(
            bridge,
            "httpx",
            SimpleNamespace(
                AsyncClient=lambda **kwargs: client_type(transport=httpx.ASGITransport(app=app), **kwargs)
            ),
        )
        voice = HomeService(camera_id=CAMERA, voice_preferences=preferences.read)
        voice.selected = voice.armed = True
        voice.last_video = time.monotonic()
        voice.runtime_log = RuntimeLog(tmp_path / "logs")
        voice.speaker.say = AsyncMock()
        voice.command = AsyncMock(side_effect=AssertionError("Task draft cannot actuate"))
        effect = AsyncMock(side_effect=AssertionError("Task draft cannot actuate"))
        monkeypatch.setattr(bridge, "command", effect)
        try:
            yield SimpleNamespace(
                app=app,
                client=client,
                model=model,
                home=voice,
                private=private,
                selected=selected,
                preferences=preferences,
                calls=calls,
                session=session,
                effect=effect,
            )
        finally:
            app.state.embodied.process = None


def voice_request(o, **changes):
    return dict(
        request_id="c" * 64,
        camera=CAMERA,
        camera_source_context=camera_source_context(o.selected),
        text="Friday，" + TEXT,
        attending=False,
        captured_mono=time.monotonic(),
        voice_revision=0,
        **changes,
    )


async def post(o, body):
    return await o.client.post("/api/embodied/task", json=body, headers={"X-Friday-Worker": KEY})


async def receive(o, *, followup=False):
    if followup:
        await after_standalone_wake(o.home, TEXT)
    else:
        o.home.request = AsyncMock(return_value=transcript("Friday，" + TEXT))
        await o.home.transcribe(b"SYNTHETIC task audio", None, time.monotonic(), False)
    command = o.home.state["voice_command"]
    assert command["state"] == "awaiting_confirmation", o.home.state.get("last_result", o.home.state)
    reply = await o.client.get("/api/home-flow/task-proposals")
    assert reply.status_code == 200, reply.text
    return reply.json()["proposals"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("followup", [False, True])
async def test_spoken_task_is_only_a_draft_then_console_adopts_same_plan_without_second_model(
    tmp_path, monkeypatch, followup
):
    async with setup(tmp_path, monkeypatch) as o:
        proposal = await receive(o, followup=followup)
        assert len(o.model.calls) == 1 and not o.calls
        assert (
            o.app.state.core.store.connection.execute("SELECT count(*) FROM core_rule_plans").fetchone()[0]
            == 0
        )
        o.home.command.assert_not_awaited()
        o.effect.assert_not_awaited()
        assert "核对" in o.home.speaker.say.call_args.args[0]
        shown = await turn(
            o.client,
            request(o.session, operation="voice-task:one", lane="tasks", proposal_ref=proposal["id"]),
        )
        assert shown["phase"] == "preview", shown
        assert domain(shown)["task_review"] == proposal["task_review"] and len(o.model.calls) == 1
        done = await turn(
            o.client,
            request(
                o.session,
                operation="voice-task:one",
                kind="approve",
                lane="tasks",
                plan_id=shown["plan_id"],
                approval_digest=shown["approval_digest"],
                expected_context=shown["context_token"],
            ),
        )
        assert domain(done)["plan"]["status"] == "verified" and not o.calls
        await o.client.post("/api/home-flow/dispose", json={"session_id": o.session})
        await o.app.state.home_experience.household_mode.select("awake")
        runtime = o.app.state.core_rule_runtime
        await runtime.ingest(o.app.state.core.adapter, event(o.app.state.core))
        for _ in range(30):
            await asyncio.sleep(0.01)
            await runtime.tick()
            if not runtime.pending:
                break
        assert [c.url.path for c in o.calls] == [
            "/api/services/light/turn_on",
            "/api/services/switch/turn_on",
        ]


@pytest.mark.asyncio
async def test_duplicate_intake_and_second_console_cannot_create_another_model_or_rule(tmp_path, monkeypatch):
    async with setup(tmp_path, monkeypatch) as o:
        body = voice_request(o)
        first = await post(o, body)
        assert first.status_code == 200, first.text
        second = await post(o, body)
        assert second.status_code == 200, second.text
        assert first.json()["proposal_ref"] == second.json()["proposal_ref"] and len(o.model.calls) == 1
        ref = first.json()["proposal_ref"]
        shown = await turn(
            o.client, request(o.session, operation="voice-task:one", lane="tasks", proposal_ref=ref)
        )
        assert shown["phase"] == "preview", shown
        ready = await bootstrap(o.client)
        duplicate = await turn(
            o.client, request(ready["session_id"], operation="voice-task:two", lane="tasks", proposal_ref=ref)
        )
        assert duplicate["phase"] in {"refused", "unavailable", "cancelled"}, duplicate
        assert (
            o.app.state.core.store.connection.execute("SELECT count(*) FROM core_rule_plans").fetchone()[0]
            == 1
        )
        assert not o.app.state.core_rules.records() and not o.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["wake", "camera", "worker", "policy", "paused", "authority"])
async def test_pending_source_changes_retire_inbox_and_block_already_displayed_approval(
    tmp_path, monkeypatch, gate
):
    async with setup(tmp_path, monkeypatch) as o:
        proposal = await receive(o)
        shown = await turn(
            o.client,
            request(o.session, operation="voice-task:one", lane="tasks", proposal_ref=proposal["id"]),
        )
        assert shown["phase"] == "preview", shown
        if gate == "wake":
            o.preferences.save(VoiceEdit(expected_revision=0, wake_word="小福"))
        elif gate == "camera":
            value = json.loads((o.private / "config.json").read_text())
            value["cameras"][0]["device"]["did"] = "SYNTHETIC-replaced"
            (o.private / "config.json").write_text(json.dumps(value))
        elif gate == "worker":
            o.app.state.embodied.process = SimpleNamespace(returncode=None)
        elif gate == "policy":
            o.app.state.infrastructure.policy = o.app.state.infrastructure.policy.model_copy(
                update={"interaction": False}
            )
        elif gate == "paused":
            (o.private / "voice-input-paused").touch()
        elif gate == "authority":
            o.app.state.core.actions.pause()
        inbox = await o.client.get("/api/home-flow/task-proposals")
        assert inbox.json()["proposals"] == []
        done = await turn(
            o.client,
            request(
                o.session,
                operation="voice-task:one",
                kind="approve",
                lane="tasks",
                plan_id=shown["plan_id"],
                approval_digest=shown["approval_digest"],
                expected_context=shown["context_token"],
            ),
        )
        assert done["phase"] in {"refused", "unavailable", "cancelled"}, done
        assert not o.app.state.core_rules.records() and not o.calls


@pytest.mark.asyncio
async def test_noncooperative_cancelled_voice_planner_cannot_publish_pending_task(tmp_path, monkeypatch):
    model = TaskRuntime()
    original = model.complete_scenario
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def held(*args, **kwargs):
        value = await original(*args, **kwargs)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return value

    model.complete_scenario = held
    async with setup(tmp_path, monkeypatch, model) as o:
        pending = asyncio.create_task(post(o, voice_request(o)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            pending.cancel()
            await asyncio.wait_for(cancelled.wait(), 2)
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            assert (await o.client.get("/api/home-flow/task-proposals")).json()["proposals"] == []
            assert not o.app.state.core_rules.records() and not o.calls
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_proposal_cannot_be_rewritten_in_the_public_turn(tmp_path, monkeypatch):
    async with setup(tmp_path, monkeypatch) as o:
        proposal = await receive(o)
        response = await o.client.post(
            "/api/home-flow/turn",
            json=request(
                o.session,
                operation="voice-task:one",
                lane="tasks",
                proposal_ref=proposal["id"],
                text="改成另一件事",
            ),
        )
        assert response.status_code == 422 and not o.calls


@pytest.mark.asyncio
async def test_worker_retires_accepted_draft_after_its_local_interaction_changes(tmp_path, monkeypatch):
    import homeplane.embodied.bridge as bridge

    original = bridge.task
    async with setup(tmp_path, monkeypatch) as o:

        async def late(*args, **kwargs):
            answer = await original(*args, **kwargs)
            o.home.epoch += 1
            return answer

        monkeypatch.setattr(bridge, "task", late)
        o.home.request = AsyncMock(return_value=transcript("Friday，" + TEXT))
        await o.home.transcribe(b"SYNTHETIC retired task", None, time.monotonic(), False)
        assert o.home.state["voice_command"]["state"] == "expired"
        assert (await o.client.get("/api/home-flow/task-proposals")).json()["proposals"] == []
        o.home.speaker.say.assert_not_awaited()
        assert not o.calls


@pytest.mark.asyncio
async def test_retired_intake_receipts_release_capacity_after_original_audio_expires(tmp_path, monkeypatch):
    async with setup(tmp_path, monkeypatch) as o:
        owner = o.app.state.home_flow.voice_tasks
        owner.receipts = {str(i): ("f" * 64, "e" * 64, time.monotonic() - 1) for i in range(64)}
        result = await post(o, voice_request(o))
        assert result.status_code == 200, result.text
        assert len(owner.receipts) == 1 and len(o.model.calls) == 1 and not o.calls


@pytest.mark.asyncio
async def test_pending_http_payload_drives_home_attention_review_and_widget_confirmation(
    tmp_path, monkeypatch
):
    import subprocess
    from pathlib import Path

    from tests.test_homeplane_home_conversation_widget import HARNESS, NODE

    if NODE is None:
        pytest.skip("Existing Node unavailable for offline DOM check")
    async with setup(tmp_path, monkeypatch) as o:
        proposal = await receive(o)
        inbox = (await o.client.get("/api/home-flow/task-proposals")).json()
        shown = await turn(
            o.client,
            request(o.session, operation="voice-task:one", lane="tasks", proposal_ref=proposal["id"]),
        )
        assert shown["phase"] == "preview", shown
        done = await turn(
            o.client,
            request(
                o.session,
                operation="voice-task:one",
                kind="approve",
                lane="tasks",
                plan_id=shown["plan_id"],
                approval_digest=shown["approval_digest"],
                expected_context=shown["context_token"],
            ),
        )
        assert domain(done)["plan"]["status"] == "verified", done
        ready = (await o.client.post("/api/home-flow/context", json={"session_id": o.session})).json()
        script = (
            HARNESS.split("const config = JSON.parse(process.argv[3]);")[0]
            + r"""
const config=JSON.parse(process.argv[3]);
Object.defineProperty(Element.prototype,'dataset',{get(){if(!this._dataset)this._dataset=new Proxy({}, {get:(_,k)=>this.getAttribute('data-'+String(k).replace(/[A-Z]/g,c=>'-'+c.toLowerCase())),set:(_,k,v)=>{this.setAttribute('data-'+String(k).replace(/[A-Z]/g,c=>'-'+c.toLowerCase()),v);return true;}});return this._dataset;}});
const {mountHomeStatus,homeAttention}=await import('file://'+process.argv[2].replace('home-conversation.js','home-status.js'));
const storage=new Map();globalThis.sessionStorage={getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
const flush=async()=>{for(let i=0;i<4;i++)await new Promise(r=>setImmediate(r));};
const root=document.createElement('main');document.body.append(root);
const notices=homeAttention([],[],[],null,config.inbox.proposals);
assert.equal(notices.length,1);assert.equal(notices[0].page,'Ask Home');
let destination;const home=mountHomeStatus(root,{api:async path=>path==='/api/home-flow/task-proposals'?config.inbox:null,go:page=>destination=page});
await flush();const entry=[...root.querySelectorAll('button')].find(e=>e.textContent.includes('语音任务待确认'));assert(entry);
entry.onclick();assert.equal(destination,'Ask Home');assert.equal(storage.get('Friday.taskReview'),config.inbox.proposals[0].id);home.dispose();
let projected=config.preview;const sent=[];
const client={taskProposals:async()=>config.inbox,discardTaskProposal:async()=>{},async send(r){sent.push(r);return {...projected,session_id:r.session_id,turn_id:r.turn_id,operation_id:r.operation_id};},async cancel(r){return {...config.bootstrap,...r,phase:'cancelled',code:'synthetic_cancel'};}};
const widget=HomeConversation.mount(root,{sourceToken:null,client,initialProposalRef:storage.get('Friday.taskReview')});
try{
 widget.update({sourceToken:config.bootstrap.session_id,response:config.bootstrap});await flush();
 assert.equal(sent.length,1);assert.equal(sent[0].proposal_ref,config.inbox.proposals[0].id);assert.equal(sent[0].text,undefined);
 assert(!sent.some(r=>r.kind==='approve'));
 assert(root.textContent.includes(config.preview.domain.report.task_review.when));
 const enable=root.querySelector('[data-hc="approve"]');assert(enable&&!enable.disabled);projected=config.done;enable.fire('click');await flush();
 assert(root.textContent.includes('后台任务已启用'));
}finally{widget.dispose();}
// Retired reads cannot populate a fresh source.
let releaseOld,reads=0;
const stale=HomeConversation.mount(root,{sourceToken:null,client:{...client,taskProposals:async()=>{reads++;return reads===1?await new Promise(r=>releaseOld=r):{proposals:[]};}}});
try{
 stale.update({sourceToken:'old-source',response:config.bootstrap});await flush();assert(releaseOld);
 stale.update({sourceToken:'new-source',response:{...config.bootstrap,session_id:'console:new'}});
 releaseOld(config.inbox);await flush();
 assert.equal(root.querySelector('[data-hc="review-task"]'),null);assert.equal(reads,2);
}finally{stale.dispose();}
// A pre-discard read cannot restore an ignored proposal.
let releaseBeforeDiscard,fetches=0,discarded=0;
const ignoring=HomeConversation.mount(root,{sourceToken:null,client:{...client,
 taskProposals:async()=>{fetches++;return fetches===1?config.inbox:fetches===2?await new Promise(r=>releaseBeforeDiscard=r):{proposals:[]};},
 discardTaskProposal:async()=>{discarded++;}}});
try{
 ignoring.update({sourceToken:'ignore-source',response:config.bootstrap});await flush();
 ignoring.update({response:config.bootstrap});await flush();assert(releaseBeforeDiscard);
 root.querySelector('[data-hc="discard-task"]').fire('click');await flush();
 releaseBeforeDiscard(config.inbox);await flush();
 assert.equal(discarded,1);assert.equal(fetches,3);assert.equal(root.querySelector('[data-hc="review-task"]'),null);
}finally{ignoring.dispose();}
"""
        )
        path = tmp_path / "voice-task-widget.mjs"
        path.write_text(script)
        config = dict(inbox=inbox, bootstrap=ready, preview=shown, done=done)
        result = subprocess.run(
            [
                NODE,
                str(path),
                str(Path(__file__).parents[2] / "homeplane/static/home-conversation.js"),
                json.dumps(config),
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
        assert result.returncode == 0, result.stderr
        assert len(o.model.calls) == 1 and not o.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gate", ["no_wake", "wrong_camera", "stale", "revision", "worker_key", "origin", "extra_scope"]
)
async def test_unqualified_voice_task_never_reaches_the_model_or_rule_owner(tmp_path, monkeypatch, gate):
    async with setup(tmp_path, monkeypatch) as o:
        body = voice_request(o)
        headers = {"X-Friday-Worker": KEY}
        if gate == "no_wake":
            body["text"] = TEXT
        elif gate == "wrong_camera":
            body["camera"] = "cam_2222222222"
        elif gate == "stale":
            body["captured_mono"] = time.monotonic() - 11
        elif gate == "revision":
            body["voice_revision"] = 1
        elif gate == "worker_key":
            headers["X-Friday-Worker"] = "wrong"
        elif gate == "origin":
            headers["Origin"] = "http://another.invalid"
        elif gate == "extra_scope":
            body["scope"] = {"all_home": True}
        response = await o.client.post("/api/embodied/task", json=body, headers=headers)
        assert response.status_code in {403, 409, 422}, response.text
        assert not o.model.calls and not o.calls and not o.app.state.core_rules.records()


@pytest.mark.asyncio
async def test_custom_wake_without_gaze_and_expired_proposal_are_kept_separate(tmp_path, monkeypatch):
    async with setup(tmp_path, monkeypatch) as o:
        o.preferences.save(VoiceEdit(expected_revision=0, wake_word="小福"))
        o.home.request = AsyncMock(return_value=transcript("小福，" + TEXT, 1))
        await o.home.transcribe(b"SYNTHETIC custom wake task", None, time.monotonic(), False)
        assert o.home.state["voice_command"]["state"] == "awaiting_confirmation"
        owner = o.app.state.home_flow.voice_tasks
        proposal = owner.listing()["proposals"][0]
        owner.pending[proposal["id"]].deadline = time.monotonic() - 1
        assert owner.listing()["proposals"] == []
        rejected = await turn(
            o.client,
            request(o.session, operation="voice-task:one", lane="tasks", proposal_ref=proposal["id"]),
        )
        assert rejected["phase"] in {"refused", "unavailable", "cancelled"}, rejected
        assert not o.app.state.core_rules.records() and not o.calls
