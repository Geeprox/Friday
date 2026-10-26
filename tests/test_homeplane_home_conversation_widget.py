"""Offline component consumes real RuleConversation responses over stdio.

The DOM harness proves request/lifecycle/escaping behavior. It is deliberately
not a browser, F5 HTTP router, visual QA, decoder or device qualification.
"""

import asyncio
import json
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_homeplane_rule_conversation import PHRASE, request
from test_homeplane_rule_conversation import rig as rig

from homeplane.action_contracts import LightExecutionTrace, LightPlanRequest
from homeplane.home_flow_contracts import FlowRequest, FlowResponse

NODE = shutil.which("node") or ("/opt/homebrew/bin/node" if Path("/opt/homebrew/bin/node").is_file() else None)

HARNESS = r"""
import fs from 'node:fs';
import assert from 'node:assert/strict';
import readline from 'node:readline';
import {webcrypto} from 'node:crypto';
globalThis.crypto ??= webcrypto;
let writes = 0;
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {};
    this.listeners = new Map(); this._text = ''; this.parentNode = null;
    this.disabled = false; this.value = ''; this.currentTime = 0; this.pauseCount = 0;
  }
  set textContent(value) { this._text = String(value); this.replaceChildren(); }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(_) { writes++; throw Error('Evidence HTML must not execute'); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  removeAttribute(key) { delete this.attributes[key]; }
  set src(value) { this.setAttribute('src', value); }
  get src() { return this.getAttribute('src'); }
  get lastElementChild() { return this.children.at(-1); }
  get isConnected() { return this === document.body || !!this.parentNode?.isConnected; }
  append(...children) { for (const child of children) { child.remove(); child.parentNode = this; this.children.push(child); } }
  replaceChildren(...children) { for (const child of this.children) child.parentNode = null; this.children = []; this.append(...children); }
  remove() { if (this.parentNode) { this.parentNode.children = this.parentNode.children.filter(child => child !== this); this.parentNode = null; } }
  addEventListener(type, listener) { const entries = this.listeners.get(type) ?? []; entries.push(listener); this.listeners.set(type, entries); }
  removeEventListener(type, listener) { this.listeners.set(type, (this.listeners.get(type) ?? []).filter(item => item !== listener)); }
  fire(type, fields = {}) {
    if (type === 'click' && this.disabled) return;
    const event = {preventDefault() {}, key: '', ctrlKey: false, metaKey: false, ...fields};
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
  focus() { document.activeElement = this; }
  pause() { this.pauseCount++; }
  load() { this.loadCount = (this.loadCount ?? 0) + 1; }
  querySelectorAll(selector) {
    const descendants = this.children.flatMap(child => [child, ...child.querySelectorAll('*')]);
    if (selector === '*') return descendants;
    const attr = selector.match(/^\[([^=]+)="([^"]+)"\]$/);
    return descendants.filter(child => attr ? child.getAttribute(attr[1]) === attr[2] : selector.startsWith('.') ? (child.getAttribute('class') ?? '').split(' ').includes(selector.slice(1)) : child.tagName === selector.toUpperCase());
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
}
globalThis.document = {createElement: tag => new Element(tag), activeElement: null};
document.body = new Element('body');
const {default:copyDOM} = await import('file://' + process.argv[2].replace(/homeplane\/static\/home-conversation.js$/, 'tests/copy_layout_dom.cjs'));
const copyInterfaces=copyDOM.install(Element,document,document.body);
Object.assign(globalThis,copyInterfaces);
await import('file://' + process.argv[2].replace('home-conversation.js','copy-layout.js'));
const source = fs.readFileSync(process.argv[2], 'utf8').replace(/^import '[^']*copy-layout.js';\n/m,'');
const {HomeConversation} = await import('file://' + process.argv[2]);
const config = JSON.parse(process.argv[3]);
const input = readline.createInterface({input: process.stdin});
const outstanding = new Map();
const requests = [];
let nextBridge = 0;
input.on('line', line => {
  const result = JSON.parse(line), receipt = outstanding.get(result.bridge_id);
  if (!receipt) throw Error('unknown bridge response');
  outstanding.delete(result.bridge_id);
  if (result.error) receipt.reject(Error(result.error)); else receipt.resolve(result.response);
});
const emit = message => process.stdout.write(JSON.stringify(message) + '\n');
function bridge(type, fields) {
  const bridge_id = ++nextBridge;
  emit({type, bridge_id, ...fields});
  return new Promise((resolve, reject) => outstanding.set(bridge_id, {resolve, reject}));
}
let hold = false;
const delayed = [];
const client = {
  async send(request, {signal}) {
    requests.push(request);
    // Withhold only the consumer delivery of an actual completed domain report.
    // This deliberately ignores AbortSignal to exercise the local late fence.
    const response = await bridge('request', {request});
    if (hold) return await new Promise(resolve => delayed.push(() => resolve(response)));
    return response;
  },
  async cancel(request) { requests.push(request); return await bridge('request', {request}); },
};
const settle = () => new Promise(resolve => setTimeout(resolve, 2));
async function until(predicate) {
  for (let attempt = 0; attempt < 500; attempt++) { if (predicate()) return; await settle(); }
  throw Error('component did not reach expected state');
}
const host = document.createElement('main'), originalAsk = document.createElement('section');
originalAsk.textContent = 'Existing structured Ask query'; host.append(originalAsk); document.body.append(host);
let widget = HomeConversation.mount(host, {sourceToken: 'SIMULATED-source-one', client});
const root = () => host.querySelector('.hc-root');
const field = () => root().querySelector('[data-hc="input"]');
const actionable = name => root().querySelectorAll(`[data-hc="${name}"]`).filter(node => !node.disabled);
const submit = text => { field().value = text; root().querySelector('form').fire('submit'); };
const currentPre = () => root().querySelectorAll('pre').at(-1)?.textContent ?? '';

try {
  assert.equal(root().querySelector('[data-hc="send"]').disabled, true, 'session is server-issued');
  widget.update({response: config.bootstrap[0]});
  assert.equal(root().querySelector('[data-hc="send"]').disabled, false);
  submit(config.phrase);
  await until(() => actionable('choice').length === 2);
  assert(!host.querySelector('img'), 'malicious original rule label is text');
  assert(host.textContent.includes(config.malicious));
  actionable('choice')[1].fire('click');
  await until(() => actionable('approve').length === 1);
  const preview = JSON.parse(currentPre());
  assert.equal(preview.plan.status, 'awaiting_approval');
  assert.equal(preview.plan.old.id, config.selected_rule);
  assert.deepEqual(preview.plan.new_rule.actions.map(action => action.id), ['hall-light-on']);
  assert(host.textContent.includes('保留') && host.textContent.includes('移除'));
  assert.equal(document.activeElement.tagName, 'ARTICLE', 'result receives keyboard focus');
  actionable('approve')[0].fire('click');
  await until(() => JSON.parse(currentPre() || '{}').status === 'verified');
  const applied = JSON.parse(currentPre());
  assert.equal(applied.simulated, true); assert.equal(applied.real_write_attempted, false);
  assert.equal(applied.physically_confirmed, false);
  assert(host.textContent.includes('NAS 关闭') && host.textContent.includes('房间输出 未接入'));
  assert(root().querySelectorAll('[data-hc="approve"]').every(node => node.disabled), 'old previews cannot approve');
  const approval = requests.find(request => request.kind === 'approve');
  assert.equal(approval.plan_id, preview.plan.id); assert.equal(approval.approval_digest, preview.plan.approval_digest);
  actionable('rollback')[0].fire('click');
  await until(() => JSON.parse(currentPre() || '{}').status === 'rolled_back');
  assert(host.textContent.includes('不撤销过去的物理动作'));

  root().querySelector('[data-hc="new"]').fire('click');
  hold = true; submit(config.phrase); await until(() => delayed.length === 1);
  const captured = requests.filter(request => request.kind === 'turn').at(-1);
  await bridge('invalidate', {session_id: captured.session_id});
  widget.update({sourceToken: 'SIMULATED-source-two', response: config.bootstrap[1]});
  assert.equal(root().querySelectorAll('pre').length, 0);
  assert.equal(field().value, config.phrase, 'source changes retain the local authored draft without resending it');
  delayed.shift()(); hold = false; await settle();
  assert.equal(root().querySelectorAll('pre').length, 0, 'late old source cannot paint or re-enable approval');
  assert(requests.some(request => request.kind === 'cancel' && request.operation_id === captured.operation_id && request.session_id === captured.session_id));

  // Esc stops a real pending result. The eventual result cannot replace stop.
  hold = true; submit(config.phrase); await until(() => delayed.length === 1);
  root().fire('keydown', {key: 'Escape'});
  await until(() => root().textContent.includes('本次请求已停止'));
  delayed.shift()(); hold = false; await settle();
  assert(root().textContent.includes('本次请求已停止'));
  assert.equal(actionable('approve').length, 0);

  // A disposed mount leaves the original structured query intact.
  root().querySelector('[data-hc="new"]').fire('click');
  hold = true; field().value = config.phrase;
  root().fire('keydown', {key: 'Enter', ctrlKey: true});
  await until(() => delayed.length === 1);
  widget.dispose(); delayed.shift()(); hold = false; await settle();
  assert.equal(host.children.length, 1); assert.equal(host.children[0], originalAsk);
  assert.equal(host.textContent, 'Existing structured Ask query');
  assert(requests.filter(request => request.kind === 'cancel').every(request => !('approval_digest' in request)), 'cancel is a valid F0 request even after approve');
  assert(requests.every(request => !['subject_ref', 'grant', 'authority', 'input_kind', 'source_revision', 'result', 'success'].some(field => field in request)));
  assert.equal(writes, 0);
  await until(() => outstanding.size === 0);
  emit({type: 'done', requests, html_writes: writes, checked: ['real_choice', 'real_exact_preview', 'real_apply_readback', 'real_rollback', 'keyboard_focus', 'source_late', 'escape_late', 'dispose_late', 'existing_ask_preserved', 'safe_text', 'cancel_contract']});
} catch (error) {
  widget.dispose(); emit({type: 'failure', error: error.stack}); process.exitCode = 1;
} finally { input.close(); }
"""


@pytest.mark.asyncio
@pytest.mark.skipif(NODE is None, reason="Existing Node unavailable for offline DOM contract")
async def test_component_consumes_original_rule_domain_and_fences_source_stop_dispose(rig, tmp_path):
    _, manager, _, conversation, context, _ = rig
    old = manager.backend.records()[0]
    malicious = '<img src=x onerror="SIMULATED-untrusted-label">'
    extra = await manager.backend.create_disabled("SIMULATED-choice-two", old.rule.model_copy(update={
        "id": "SIMULATED-choice-two", "intent": malicious,
    }))
    await manager.backend.toggle(extra.id, extra.fingerprint, True)
    newer = replace(context, session_id="SIMULATED-session-two", generation=2)
    contexts = {value.session_id: value for value in (context, newer)}
    bootstrap = [FlowResponse(session_id=value.session_id, turn_id="bootstrap:1", operation_id="bootstrap:1",
                              phase="result", code="session_bootstrap", input_kind="console_text").model_dump(mode="json")
                 for value in (context, newer)]
    config = dict(bootstrap=bootstrap, phrase=PHRASE, malicious=malicious, selected_rule=extra.id)
    script = tmp_path / "offline-component.mjs"
    script.write_text(HARNESS)
    process = await asyncio.create_subprocess_exec(
        NODE, str(script), str(Path(__file__).parents[1] / "homeplane/static/home-conversation.js"), json.dumps(config),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={"PATH": "/opt/homebrew/bin:/usr/bin:/bin", "TMPDIR": "/tmp"},
    )
    reports, result = [], None
    try:
        async with asyncio.timeout(12):
            while line := await process.stdout.readline():
                message = json.loads(line)
                if message["type"] == "failure":
                    pytest.fail(message["error"])
                if message["type"] == "done":
                    result = message
                    break
                if message["type"] == "invalidate":
                    conversation.invalidate(message["session_id"])
                    response = None
                else:
                    actual_request = FlowRequest.model_validate(message["request"])
                    trusted = contexts[actual_request.session_id]  # private pre-issued fixture context only
                    actual = await conversation.turn(actual_request, trusted)
                    response = actual.model_dump(mode="json")
                    reports.append((actual_request, actual))
                process.stdin.write((json.dumps({"bridge_id": message["bridge_id"], "response": response}) + "\n").encode())
                await process.stdin.drain()
            assert result is not None
            assert await process.wait() == 0, (await process.stderr.read()).decode()
        assert result["html_writes"] == 0 and "dispose_late" in result["checked"]
        approvals = [(request, response) for request, response in reports if request.kind == "approve"]
        assert len(approvals) == 1 and approvals[0][1].domain["status"] == "verified"
        assert approvals[0][1].domain["real_write_attempted"] is False
        assert manager.get(approvals[0][0].plan_id).status == "rolled_back"
        assert manager.backend.read(extra.id).enabled and manager.backend.read(old.id).enabled
        (tmp_path / "actual-rule-widget-receipt.json").write_text(json.dumps({
            "qualification": "isolated Simulator domain; not F5 HTTP/browser/device acceptance",
            "transport": "real RuleConversation/RuleTextChanges/RuleManager responses over stdio to DOM contract harness",
            "html_writes": result["html_writes"], "checked": result["checked"],
            "requests_responses": [{"request": request.model_dump(mode="json"), "response": response.model_dump(mode="json")}
                                   for request, response in reports],
            "final_rule_records": [record.model_dump(mode="json") for record in manager.backend.records()],
        }, ensure_ascii=False, indent=2) + "\n")
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()
        await conversation.close()


PROJECTION_HARNESS = HARNESS.split("const config = JSON.parse(process.argv[3]);")[0] + r"""
const config = JSON.parse(process.argv[3]);
const root = document.createElement('main'); document.body.append(root);
let sequence = 0, projected = null, calls = [];
const client = {
  async send(request) { calls.push(request); return {...projected, session_id: request.session_id, turn_id: request.turn_id, operation_id: request.operation_id}; },
  async cancel(request) { calls.push(request); return {...config.bootstrap, session_id: request.session_id, turn_id: request.turn_id, operation_id: request.operation_id, phase: 'cancelled', code: 'projection_cancelled'}; },
};
const widget = HomeConversation.mount(root, {sourceToken: 'SIMULATED-projection:0', client});
const settle = () => new Promise(resolve => setImmediate(resolve));
async function show(response) {
  projected = response;
  widget.update({sourceToken: `SIMULATED-projection:${++sequence}`, response: config.bootstrap});
  root.querySelector('[data-hc="input"]').value = 'SIMULATED renderer projection only';
  root.querySelector('form').fire('submit'); await settle();
}
try {
  await show(config.expired_rule);
  assert(root.querySelector('[data-hc="approve"]').disabled, 'effective expiry disables exact old approval');
  assert(root.textContent.includes('已过期'));
  await show(config.action);
  const approve = root.querySelector('[data-hc="approve"]'); assert(approve && !approve.disabled);
  assert(root.textContent.includes('light.set') && root.textContent.includes('"brightness":8'));
  assert(!root.textContent.includes('undefined'), 'original light desired fields are consumed');
  approve.fire('click'); await settle();
  const click = calls.find(request => request.kind === 'approve');
  assert.equal(click.plan_id, config.action.plan_id); assert.equal(click.approval_digest, config.action.approval_digest);
  assert.equal(click.expected_context, config.action.context_token);
  await show(config.mode);
  assert(root.textContent.includes('当前模式需核对，请查看完整说明'));
  assert(root.textContent.includes('适用范围以执行服务和有效时间为准'));
  assert(!root.textContent.includes('删除范围'));
  assert(root.textContent.includes('配置、调用与可听见状态分别显示'));
  assert(root.textContent.includes('已过期') && root.textContent.includes('剩余 0 秒'));
  assert(root.textContent.includes('可听见 unverified') && root.textContent.includes('模拟 true'));
  assert(root.querySelector('pre').textContent.includes('"value": true'), 'raw expired original lease is retained');
  await show(config.held_archive);
  assert.equal(root.querySelector('video'), null, 'missing content guard never loads an unqualified route');
  assert(root.textContent.includes('scoped_action_trace_unavailable'));
  await show(config.playback);
  const video = root.querySelector('video'); assert(video && video.muted === true);
  assert.equal(video.getAttribute('autoplay'), null); assert.equal(video.getAttribute('preload'), 'none');
  video.fire('loadedmetadata'); assert.equal(video.currentTime, 30.3);
  video.currentTime = 20; video.fire('seeking'); assert.equal(video.currentTime, 30.3);
  video.currentTime = 41; video.fire('timeupdate'); assert(video.pauseCount > 0 && video.currentTime < 40);
  await show(config.foreign_route); assert.equal(root.querySelector('video'), null);
  await show(config.invalid_qualifier); assert(root.textContent.includes('请求结果尚未确认'));
  assert.equal(root.querySelectorAll('[data-hc="approve"]').length, 0);
  assert.equal(writes, 0);
} finally { widget.dispose(); }
"""


@pytest.mark.asyncio
@pytest.mark.skipif(NODE is None, reason="Existing Node unavailable for offline DOM contract")
async def test_projection_only_expiry_independent_qualifiers_and_original_field_rendering(rig, tmp_path):
    """Presentation projections only: no F1/F3/F4 service or device acceptance."""
    _, _, _, conversation, context, _ = rig
    preview = await conversation.turn(request(context, text=PHRASE), context)
    now = datetime.now(UTC)
    bootstrap = FlowResponse(session_id="SIMULATED-projection", turn_id="bootstrap:1", operation_id="bootstrap:1",
                             phase="result", code="projection_bootstrap", input_kind="simulated_fixture")
    expired = preview.model_copy(update={"expires_at": now - timedelta(seconds=1)})
    action_request = LightPlanRequest(target="SIMULATED-controlled-lamp", desired={"power": "on", "brightness": 8},
                                     expected_state_version="SIMULATED-v1", expires_at=now + timedelta(seconds=30),
                                     idempotency_key="SIMULATED-key")
    trace = LightExecutionTrace(action_id="SIMULATED-action", correlation_id="SIMULATED-correlation", request=action_request,
                                created_at=now, boot_id="SIMULATED-boot", status="planned", code="planned", reasons=[])
    action = bootstrap.model_copy(update={"phase": "preview", "plan_id": trace.action_id, "approval_digest": "a" * 64,
                                          "context_token": "b" * 64, "expires_at": now + timedelta(seconds=30),
                                          "domain": trace.model_dump(mode="json")})
    mode = bootstrap.model_copy(update={"output_audibility": "unverified", "domain": {
        "report": {"runtime": {"modes": {"home.sleep": {"value": True}}}, "pause": None,
                   "effective": {"mode": "unknown", "status": "expired", "remaining_seconds": 0,
                                 "valid_until": (now - timedelta(seconds=1)).isoformat(), "backend": "simulator", "coverage": {}}},
        "feedback": {"kind": "statement", "room_id": None, "priority": "normal", "prompt_id": None,
                     "caption": "SIMULATED projection", "requires_attention": False},
        "output": {"status": "completed", "code": "projection_exit0", "endpoint_id": "SIMULATED-output", "configured": True,
                   "attempted": True, "completed": True, "simulated": True, "audibility": "unverified", "cleanup": "complete", "playback_window": None},
    }})
    held_archive = bootstrap.model_copy(update={"phase": "unavailable", "code": "archive_content_reader_guard_unavailable", "domain": {
        "query": None, "explanation": {"level": "history_gap", "statements": [], "complete_household_history": False},
        "archive": None, "archive_status": {"status": "held", "reason": "archive_content_reader_guard_unavailable", "choices": []},
        "trace_status": {"status": "held", "reason": "scoped_action_trace_unavailable"},
    }})
    playback = bootstrap.model_copy(update={"nas_lease": "content_qualified", "expires_at": now + timedelta(seconds=30), "domain": {
        "playback": {"stream_url": "/api/archive/media/" + "c" * 64, "simulated": True,
                     "expires_at": (now + timedelta(seconds=300)).isoformat(), "start_offset_seconds": 30.3,
                     "end_offset_seconds": 40, "clock_uncertainty_seconds": 0},
        "archive_status": {"status": "ready", "reason": "SIMULATED-projection-only"},
    }})
    foreign = playback.model_copy(deep=True)
    foreign.domain["playback"]["stream_url"] = "https://SIMULATED.invalid/media"
    config = {key: value.model_dump(mode="json") for key, value in {
        "bootstrap": bootstrap, "expired_rule": expired, "action": action, "mode": mode,
        "held_archive": held_archive, "playback": playback, "foreign_route": foreign,
    }.items()}
    config["invalid_qualifier"] = {**config["action"], "nas_lease": "SIMULATED-client-invented-grant"}
    script = tmp_path / "presentation-only.mjs"
    script.write_text(PROJECTION_HARNESS)
    try:
        result = subprocess.run([NODE, str(script), str(Path(__file__).parents[1] / "homeplane/static/home-conversation.js"),
                                 json.dumps(config)], text=True, capture_output=True, timeout=5,
                                env={"PATH": "/opt/homebrew/bin:/usr/bin:/bin", "TMPDIR": "/tmp"})
        assert result.returncode == 0, result.stderr
    finally:
        await conversation.close()
