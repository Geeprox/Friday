"""Guest station contracts, with isolated storage and no physical devices/models."""

import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from homeplane.ktv_nas_bridge import NasBridge
from integrations.ktv_public.nas_server import NAMES, Refusal, Server, Store, make_handler

KEY = "a" * 43
ASSETS = Path(__file__).resolve().parents[1] / "integrations/ktv_public/web"


@pytest.fixture
def station(tmp_path, monkeypatch):
    # NasBridge also creates its own real HTTP pool. Keep every fixture request
    # on the owned loopback server even when macOS supplies a system proxy.
    for name in ('NO_PROXY', 'no_proxy'):
        monkeypatch.setenv(name, '127.0.0.1,localhost,::1')
    store = Store(tmp_path / "nas", "https://example.test")
    server = Server(("127.0.0.1", 0), make_handler(store, KEY, ASSETS))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with httpx.Client(base_url="http://127.0.0.1:" + str(server.server_port), timeout=3, trust_env=False) as client:
        yield store, client
    server.shutdown()
    server.server_close()
    thread.join(2)


def room(store):
    party = store.create()
    secret = parse_qs(urlsplit(party["join_url"]).fragment)["k"][0]
    return store.room(party["room_id"], secret), secret


def song():
    return {"song_id": "123", "title": "测试歌曲", "request_id": str(uuid.uuid4())}


def test_station_scopes_and_static_privacy(station):
    store, client = station
    value, secret = room(store)
    base = "/api/rooms/" + value["id"]
    guest = {"X-KTV-Room": secret}
    identity = client.post(base + "/join", headers=guest, json={}).json()
    guest["X-KTV-Guest"] = identity["key"]
    assert client.get("/").status_code == 200
    assert client.get("/app.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/add-queue.js").headers["content-type"].startswith("text/javascript")
    for name in ("sync.mjs", "sync-dsp.mjs", "sync-capture.mjs"):
        assert client.get("/" + name).headers["content-type"].startswith("text/javascript")
    policy = client.get("/").headers["permissions-policy"]
    assert "microphone=(self)" in policy and "camera=()" in policy and "geolocation=()" in policy
    assert client.get("/health").json()["protocol"] == 1
    assert client.get("/agent/tasks", headers=guest).status_code == 401
    assert client.get(base + "/status").status_code == 403
    assert client.get(base + "/status", headers=guest).status_code == 200
    assert identity["nickname"] and 0 <= identity["avatar"] < 12
    assert client.post(base + "/songs", headers={"X-KTV-Room": secret}, json=song()).status_code == 403
    assert client.post(base + "/theme", headers=guest, json={"theme": "festival"}).status_code == 200
    assert client.get(base + "/status", headers=guest).json()["theme"] == "festival"
    assert client.post(base + "/theme", headers=guest, json={"theme": []}).status_code == 400
    assert (
        client.post(base + "/profile", headers=guest, json={"nickname": "汽水朋友", "avatar": 3}).status_code
        == 200
    )
    assert client.get(base + "/status", headers={**guest, "Origin": "https://evil.test"}).status_code == 403
    assert client.get("/v1/jobs", headers=guest).status_code == 404
    assert client.get("/state.sqlite", headers=guest).status_code == 404
    assert client.post(base + "/songs", headers=guest, json=song()).status_code == 503
    store.poll()
    payload = song()
    first = client.post(base + "/songs", headers=guest, json=payload)
    assert first.status_code == 202
    chosen = client.get(base + "/status", headers=guest).json()["songs"][0]
    assert chosen["nickname"] == "汽水朋友" and chosen["avatar"] == 3
    assert client.post(base + "/songs", headers=guest, json=payload).json() == first.json()
    assert client.post(base + "/songs", headers=guest, json={**payload, "song_id": "124"}).status_code == 409
    assert (
        client.post(base + "/songs", headers=guest, json={**song(), "source_path": "/etc/passwd"}).status_code
        == 400
    )
    store.close(value["id"])
    assert client.get(base + "/status", headers=guest).status_code == 410
    assert secret not in store.db.read_bytes().decode(errors="ignore")
    assert identity["key"] not in store.db.read_bytes().decode(errors="ignore")


def test_leasing_expiry_and_cancellation_are_final(station):
    store, _ = station
    value, _ = room(store)
    store.poll()
    item = store.add(value, song())
    assert store.poll()["tasks"][0]["id"] == item["id"]
    assert store.poll()["tasks"] == []
    with store.connection() as db:
        db.execute("UPDATE songs SET lease=0")
    assert len(store.poll()["tasks"]) == 1
    store.close(value["id"])
    store.update({"updates": [{"id": item["id"], "state": "ready", "code": "none"}]})
    assert item["id"] in store.poll()["cancelled"]
    with pytest.raises(Refusal) as error:
        store.add(value, song())
    assert error.value.status == 410


def test_legacy_expiry_does_not_end_persistent_session_and_invalid_updates(station):
    store, _ = station
    value, secret = room(store)
    with store.connection() as db:
        db.execute("UPDATE rooms SET expires=0")
    assert not store.room(value["id"], secret)["closed"]
    store.close(value["id"])
    with pytest.raises(Refusal):
        store.room(value["id"], secret)
    for update in (None, [], {"id": "x", "state": [], "code": "none"}):
        with pytest.raises(Refusal):
            store.update({"updates": [update]})


class FakeQueue:
    def __init__(self):
        self.jobs = {}
        self.submissions = 0

    def submit(self, value):
        self.submissions += 1
        self.jobs.setdefault(value.request_id, {"id": value.request_id, "state": "queued", "code": "none"})
        return self.jobs[value.request_id]

    def get(self, key):
        return self.jobs[key]

    def cancel(self, key):
        self.jobs[key].update(state="cancelled", code="cancelled")


@pytest.mark.parametrize("source,preference", [("song", "song"), (None, "song"), ("mv", "mv_direct"), ("cloud", "cloud")])
def test_bridge_keeps_requested_audio_edition(tmp_path, source, preference):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"endpoint": "http://127.0.0.1:18769/ktv", "agent_key": KEY}))
    config.chmod(0o600)
    queue = FakeQueue()
    requests = []
    original = queue.submit

    def submit(value):
        requests.append(value)
        return original(value)

    queue.submit = submit
    bridge = NasBridge(queue, config, allow_loopback=True)
    item = {"id": str(uuid.uuid4()), "song_id": "123", "title": "Synthetic"}
    if source is not None:
        item["source"] = source
    bridge.pending_tasks[item["id"]] = item
    bridge.request = lambda *a: {"accepted": True}
    try:
        bridge._work_once()
        assert len(requests) == 1 and requests[0].source_preference == preference
        assert bridge.mapping[item["id"]] == requests[0].request_id
    finally:
        bridge.close()


def test_bridge_persistent_idempotency_state_and_close(station, tmp_path):
    store, client = station
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"endpoint": str(client.base_url).rstrip("/") + "/ktv", "agent_key": KEY}))
    config.chmod(0o600)
    queue = FakeQueue()
    bridge = NasBridge(queue, config, allow_loopback=True)
    # The real HTTP boundary is exercised; NAS reverse proxy strips /ktv in deployment.
    bridge.endpoint = str(client.base_url).rstrip("/")
    store.origin, store.base_path = bridge.endpoint, ""
    bridge.tick()
    party = bridge.create_party()
    assert party["active"]
    assert bridge.qr().startswith(b"\x89PNG\r\n\x1a\n")
    fragment = parse_qs(urlsplit(party["join_url"]).fragment)
    value = store.room(party["room_id"], fragment["k"][0])
    item = store.add(value, song())
    bridge.tick()
    assert len(queue.jobs) == 1
    queue.jobs[item["id"]].update(state="ready", code="none")
    bridge.tick()
    assert store.status(value)["songs"][0]["state"] == "ready"
    assert "source_path" not in str(store.status(value))
    bridge.close_party()
    assert not bridge.current()["active"]
    fresh = bridge.create_party()
    assert fresh["room_id"] != party["room_id"]
    assert fresh["order"] == [] and fresh["commands"] == []
    fragment2 = parse_qs(urlsplit(fresh["join_url"]).fragment)
    room2 = store.room(fresh["room_id"], fragment2["k"][0])
    assert store.status(room2)["songs"] == []
    assert len(queue.jobs) == 1  # reusable prepared media is preserved
    bridge.close()


def playback(current="", **changes):
    return {
        "current": current,
        "played": [],
        "state": "paused",
        "last_command": 0,
        "safety_latched": False,
        "applied_revision": 0,
        "mic_muted": True,
        **changes,
    }


def test_controls_queue_revision_and_commands(station):
    store, client = station
    value, secret = room(store)
    base = "/api/rooms/" + value["id"]
    headers = {"X-KTV-Room": secret}
    guest = client.post(base + "/join", headers=headers, json={}).json()
    headers["X-KTV-Guest"] = guest["key"]
    store.poll()
    a = store.add(value, song())["id"]
    b = store.add(value, {**song(), "song_id": "124"})["id"]
    command = {"action": "next", "song": a, "request_id": str(uuid.uuid4())}
    assert client.post(base + "/command", headers=headers, json=command).json()["code"] == "tv_offline"
    store.playback(value["id"], playback(a))
    store.update({"updates": [{"id": b, "state": "ready", "code": "none"}]})
    first = client.post(base + "/command", headers=headers, json=command)
    assert first.status_code == 200
    assert client.post(base + "/command", headers=headers, json=command).json() == first.json()
    assert (
        client.post(base + "/command", headers=headers, json={**command, "action": "pause"}).status_code
        == 409
    )
    assert (
        client.post(
            base + "/command", headers=headers, json={**command, "request_id": str(uuid.uuid4()), "song": b}
        ).json()["code"]
        == "playback_changed"
    )
    for invalid in ({"mic_gain": 101}, {"music_gain": -1}, {"mic_muted": False}, {"source": "anything"}):
        assert client.post(base + "/controls", headers=headers, json=invalid).status_code == 400
    assert (
        client.post(base + "/controls", headers={"X-KTV-Room": secret}, json={"mic_muted": 0}).status_code
        == 403
    )
    assert (
        client.post(base + "/controls", headers=headers, json={"music_gain": 25, "mic_muted": 1}).status_code
        == 200
    )
    state = store.status(value)
    assert state["controls"]["music_gain"] == 25
    pending_revision = state["controls"]["control_revision"]
    assert state["playback"]["applied_revision"] < pending_revision
    store.playback(value["id"], playback(a, last_command=first.json()["seq"], applied_revision=pending_revision))
    assert store.status(value)["playback"]["last_command"] == first.json()["seq"]
    assert store.status(value)["playback"]["applied_revision"] == pending_revision
    order = {"ids": [b, a], "revision": state["queue_revision"]}
    assert client.post(base + "/order", headers=headers, json=order).status_code == 200
    assert [s["id"] for s in store.status(value)["songs"]] == [b, a]
    assert client.post(base + "/order", headers=headers, json=order).json()["code"] == "queue_changed"
    fresh = store.status(value)["queue_revision"]
    assert (
        client.post(base + "/order", headers=headers, json={"ids": [a], "revision": fresh}).status_code == 409
    )
    other, _ = room(store)
    foreign = store.add(other, song())["id"]
    assert (
        client.post(
            base + "/command",
            headers=headers,
            json={"action": "play", "song": foreign, "request_id": str(uuid.uuid4())},
        ).status_code
        == 409
    )
    with store.connection() as db:
        db.execute("UPDATE commands SET created=0")
    assert store.poll()["rooms"][0]["commands"] == []


def test_safety_mute_wins_and_old_report_cannot_override_new_explicit_control(station):
    store, _ = station
    value, _ = room(store)
    store.controls(value, {"mic_muted": 0})
    revision = store.status(value)["controls"]["control_revision"]
    store.playback(value["id"], playback(safety_latched=True, applied_revision=revision))
    assert store.status(value)["controls"]["mic_muted"] == 1
    store.controls(value, {"mic_muted": 0})
    new_revision = store.status(value)["controls"]["control_revision"]
    store.playback(value["id"], playback(safety_latched=True, applied_revision=revision))
    assert store.status(value)["controls"]["control_revision"] == new_revision
    assert store.status(value)["controls"]["mic_muted"] == 0
    for bad in (playback(state=[]), playback(played=[str(uuid.uuid4())]), playback(last_command=True)):
        with pytest.raises(Refusal):
            store.playback(value["id"], bad)


def test_bridge_controls_mapping_and_playback_expiry(station, tmp_path):
    import time

    store, client = station
    config = tmp_path / "bridge.json"
    config.write_text(json.dumps({"endpoint": str(client.base_url).rstrip("/") + "/ktv", "agent_key": KEY}))
    config.chmod(0o600)
    queue = FakeQueue()
    bridge = NasBridge(queue, config, allow_loopback=True)
    bridge.endpoint = str(client.base_url).rstrip("/")
    store.origin, store.base_path = bridge.endpoint, ""
    party = bridge.create_party()
    fragment = parse_qs(urlsplit(party["join_url"]).fragment)
    value = store.room(party["room_id"], fragment["k"][0])
    store.poll()
    item = store.add(value, song())["id"]
    bridge.tick()
    bridge.tick()
    assert bridge.current()["order"] == [item]
    bridge.report_playback({"room_id": value["id"], **playback(item)})
    bridge.tick()
    assert store.status(value)["playback"]["online"]
    store.command(value, {"action": "pause", "song": item, "request_id": str(uuid.uuid4())})
    bridge.tick()
    assert bridge.current()["commands"][0]["song"] == item
    with store.connection() as db:
        db.execute("UPDATE rooms SET playback_at=0")
    bridge.playback_received = time.monotonic() - 10
    bridge.tick()
    assert not store.status(value)["playback"]["online"]
    with pytest.raises(ValueError):
        bridge.report_playback({"room_id": str(uuid.uuid4()), **playback()})
    bridge.close()


def test_retry_preserves_requester_and_progress_is_bounded(station):
    store, _ = station
    value, _ = room(store)
    store.poll()
    item = store.add(value, song())["id"]
    progress = {
        "stage": "acquiring",
        "completed_steps": 0,
        "total_steps": 4,
        "fraction": None,
        "source_phase": "official_request_sent",
        "source_bytes": 0,
        "stage_started": 1,
        "updated": 2,
        "failed_stage": "",
    }
    store.update(
        {
            "updates": [
                {
                    "id": item,
                    "state": "failed",
                    "code": "download_timeout",
                    "progress": progress,
                    "cache_hit": False,
                }
            ]
        }
    )
    assert store.status(value)["songs"][0]["progress"]["source_phase"] == "official_request_sent"
    payload = {"id": item, "request_id": str(uuid.uuid4())}
    store.retry(value, payload)
    store.retry(value, payload)
    task = next(t for t in store.poll()["tasks"] if t["id"] == item)
    assert task["attempt"] == 1 and task["requester"]["nickname"] == "朋友"
    with pytest.raises(Refusal):
        store.update(
            {
                "updates": [
                    {
                        "id": item,
                        "state": "acquiring",
                        "code": "none",
                        "progress": {**progress, "path": "/private/secret"},
                    }
                ]
            }
        )


def test_terminal_report_cannot_overwrite_retry_between_polls(station, tmp_path):
    store, client = station
    config = tmp_path / 'bridge.json'
    config.write_text(json.dumps({'endpoint': str(client.base_url).rstrip('/') + '/ktv', 'agent_key': KEY}))
    config.chmod(0o600)
    queue = FakeQueue()
    bridge = NasBridge(queue, config, allow_loopback=True)
    bridge.endpoint = str(client.base_url).rstrip('/')
    store.origin, store.base_path = bridge.endpoint, ''
    bridge.tick()
    party = bridge.create_party()
    fragment = parse_qs(urlsplit(party['join_url']).fragment)
    value = store.room(party['room_id'], fragment['k'][0])
    item = store.add(value, song())
    bridge.tick()
    queue.jobs[item['id']].update(state='failed', code='download_timeout')
    bridge.tick()
    assert store.status(value)['songs'][0]['state'] == 'failed'
    # Model an empty poll followed by a user retry before old local reports.
    request = bridge.request
    def interleaved(path, body=None):
        response = request(path, body)
        if path == '/agent/tasks':
            store.retry(value, {'id': item['id'], 'request_id': str(uuid.uuid4())})
        return response
    bridge.request = interleaved
    bridge.tick()
    assert store.status(value)['songs'][0]['state'] == 'queued'
    bridge.request = request
    bridge.tick()
    assert bridge.mapping[item['id']] != item['id']
    assert queue.submissions == 2
    bridge.close()


def test_room_song_is_unique_across_guests_and_request_ids(station):
    store, client = station
    value, secret = room(store)
    store.poll()
    one, two = store.join(value), store.join(value)
    first = store.add(value, song(), one)
    duplicate = store.add(value, {**song(), 'title':'同一歌曲的不同客户端标题'}, two)
    assert duplicate['id'] == first['id'] and duplicate['duplicate'] is True
    rows = store.status(value)['songs']
    assert len(rows) == 1 and rows[0]['guest_id'] == one['id']
    # Failure does not create a second row or bypass bounded explicit retries.
    store.update({'updates':[{'id':first['id'],'state':'failed','code':'source_rights_denied'}]})
    assert store.add(value, song(), two)['id'] == first['id']
    assert len(store.status(value)['songs']) == 1
    fresh, _ = room(store)
    assert store.add(fresh, song(), store.join(fresh))['id'] != first['id']

def test_completed_or_skipped_song_can_be_ordered_again(station):
    store, _ = station
    value, _ = room(store)
    store.poll()
    guest = store.join(value)
    first = store.add(value, song(), guest)
    store.update({'updates':[{'id':first['id'],'state':'ready','code':'none'}]})
    store.playback(value['id'], playback('', played=[first['id']]))
    second = store.add(value, song(), guest)
    assert second['id'] != first['id']
    assert store.add(value, song(), guest)['id'] == second['id']
    assert len(store.status(value)['songs']) == 2


def test_cloud_index_is_atomic_room_guest_only_and_source_identity_survives_queue(station):
    store, client = station
    value, secret = room(store);store.poll()
    headers = {'X-KTV-Room':secret}
    base='/api/rooms/'+value['id']
    guest=client.post(base+'/join',headers=headers,json={}).json()
    row={'song_id':'123','title':'云盘版本','artists':['测试歌手'],'source':'cloud'}
    packet={'revision':'b'*64,'offset':0,'total':2,'songs':[row]}
    assert client.post('/agent/cloud',json=packet).status_code==401
    assert client.post('/agent/cloud',headers={'Authorization':'Bearer '+KEY},json=packet).json()['complete'] is False
    assert store.search('云盘','cloud')['songs']==[]
    store.cloud_update({**packet,'offset':1,'songs':[{**row,'song_id':'124','title':'另一首云盘歌'}]})
    assert len(store.search('云盘','cloud')['songs'])==2
    assert client.get(base+'/search?source=cloud&q=云盘',headers=headers).status_code==403
    headers['X-KTV-Guest']=guest['key']
    found=client.get(base+'/search?source=cloud&q=云盘',headers=headers)
    assert found.status_code==200 and found.json()['catalog_count']==2
    cloud=client.post(base+'/songs',headers=headers,json={**song(),'source':'cloud'}).json()
    public=client.post(base+'/songs',headers=headers,json=song()).json()
    assert cloud['id']!=public['id']
    assert {t['source'] for t in store.poll()['tasks']}=={'cloud','song'}
    assert store.add(value,{**song(),'source':'cloud'})['id']==cloud['id']
    store.playback(value['id'],playback('',played=[cloud['id']]))
    again=store.add(value,{**song(),'source':'cloud'})
    assert again['id']!=cloud['id']
    assert client.post(base+'/controls',headers=headers,json={'original':1}).status_code==200
    assert store.status(value)['controls']['original']==1
    assert 3<=len(guest['nickname'])<=4 and not any(c.isdigit() for c in guest['nickname'])


def test_partial_cloud_sync_does_not_replace_last_catalog(station):
    store,_=station
    row={'song_id':'123','title':'保留目录','artists':[],'source':'cloud'}
    store.cloud_update({'revision':'a'*64,'offset':0,'total':1,'songs':[row]})
    store.cloud_update({'revision':'b'*64,'offset':0,'total':2,'songs':[{**row,'song_id':'124'}]})
    with pytest.raises(Refusal):store.cloud_update({'revision':'b'*64,'offset':2,'total':2,'songs':[]})
    assert store.search('保留','cloud')['songs'][0]['song_id']=='123'


def test_guide_audio_is_exact_current_song_and_authenticated_guest_only(station):
    import hashlib
    store,client=station;value,secret=room(store);store.poll()
    headers={'X-KTV-Room':secret};base='/api/rooms/'+value['id']
    guest=client.post(base+'/join',headers=headers,json={}).json()
    item=store.add(value,song())['id'];other=store.add(value,{**song(),'song_id':'124'})['id']
    raw=b'synthetic-guide'*200;checksum=hashlib.sha256(raw).hexdigest()
    folder=store.root.parent/'guides';folder.mkdir();(folder/(checksum+'.mp3')).write_bytes(raw)
    store.update({'updates':[{'id':item,'state':'ready','code':'none'}]})
    store.guide_update({'id':item,'sha256':checksum,'bytes':len(raw),'duration_ms':2000})
    store.playback(value['id'],playback(item,position_ms=500))
    assert client.get(base+'/guide?id='+item,headers=headers).status_code==403
    headers['X-KTV-Guest']=guest['key']
    assert client.get(base+'/guide?id='+other,headers=headers).status_code==409
    response=client.get(base+'/guide?id='+item,headers=headers)
    assert response.status_code==200 and response.content==raw
    assert response.headers['cache-control']=='private, no-store'
    status=store.status(value)
    assert status['playback']['position_ms']==500 and status['playback']['guide']['sha256']==checksum
    (folder/(checksum+'.mp3')).write_bytes(b'altered')
    assert client.get(base+'/guide?id='+item,headers=headers).status_code==409


def test_cloud_search_pages_cover_the_entire_artist_catalog(station):
    store,_=station
    rows=[{'song_id':str(i+1),'title':'共同歌手 第'+str(i+1)+'首','artists':['共同歌手'],'source':'cloud'} for i in range(65)]
    for offset in range(0,65,30):store.cloud_update({'revision':'a'*64,'offset':offset,'total':65,'songs':rows[offset:offset+30]})
    pages=[store.search('共同歌手','cloud',offset) for offset in [0,30,60]]
    assert [len(p['songs']) for p in pages]==[30,30,5]
    assert [p['has_more'] for p in pages]==[True,True,False]
    assert len({s['song_id'] for p in pages for s in p['songs']})==65


def test_monitor_accepts_only_bounded_aggregate_playback_fields(station):
    store,client=station;value,secret=room(store);base='/api/rooms/'+value['id'];headers={'X-KTV-Room':secret}
    payload={'phase':'playing','context':'running','primed':True,'session_playback':True,'node':True,'buffer_ms':60000,'position_ms':15000,'target_ms':15020}
    assert client.post(base+'/monitor',headers=headers,json=payload).status_code==403
    guest=store.join(value);headers['X-KTV-Guest']=guest['key']
    assert client.post(base+'/monitor',headers=headers,json=payload).status_code==200
    assert client.post(base+'/monitor',headers=headers,json={**payload,'cookie':'forbidden'}).status_code==400
    saved=json.loads((store.root/'monitor.json').read_text());assert saved[guest['id']]['node'] is True
    alignment={'phase':'failed','code':'ambiguous','frames':296,'score':220,'margin':10,'offset_ms':0}
    assert client.post(base+'/monitor',headers=headers,json={**payload,'alignment':alignment}).status_code==200
    release_failed={**alignment,'code':'capture_release_failed'}
    assert client.post(base+'/monitor',headers=headers,json={**payload,'phase':'failed','node':False,'alignment':release_failed}).status_code==200
    saved=json.loads((store.root/'monitor.json').read_text())
    assert saved[guest['id']]['alignment']['code']=='capture_release_failed'
    assert client.post(base+'/monitor',headers=headers,json={**payload,'alignment':{**alignment,'code':'arbitrary_device_data'}}).status_code==400
    assert client.post(base+'/monitor',headers=headers,json={**payload,'alignment':{**alignment,'audio':'forbidden'}}).status_code==400
    assert client.post(base+'/monitor',headers=headers,json={**payload,'alignment':{**alignment,'frames':1000000}}).status_code==400
    assert (store.root/'monitor.json').stat().st_mode&0o077==0


def test_artist_portraits_are_exact_name_official_images_and_privately_cached(station,monkeypatch):
    from integrations.ktv_public.nas_server import ARTISTS
    store,_=station
    monkeypatch.setattr(store,'public_metadata',lambda path,params:{'result':{'artists':[{'name':{'邓紫棋':'G.E.M.邓紫棋'}.get(params['s'],params['s']),'img1v1Url':'https://p1.music.126.net/portrait.jpg'}]}})
    store.refresh_artists({})
    saved=json.loads((store.root/'artist-portraits.json').read_text())
    assert len(saved['photos'])==len(ARTISTS)
    assert all(url.startswith('https://p1.music.126.net/') for url in saved['photos'].values())


@pytest.mark.parametrize('state', ['playing', 'paused', 'preparing', 'idle'])
def test_online_state_does_not_expire_or_reopen_a_persistent_session(station, state):
    import time
    store,_=station;value,_=room(store);store.poll();song_id=store.add(value,song())['id']
    with store.connection() as db:db.execute('UPDATE rooms SET expires=? WHERE id=?',(int(time.time())-30,value['id']))
    store.playback(value['id'],playback(song_id if state in {'playing','paused'} else '',state=state))
    store.poll()
    with store.connection() as db:assert db.execute('SELECT closed FROM rooms WHERE id=?',(value['id'],)).fetchone()[0]==0
    assert len(store.status(value)['songs']) == 1
    store.close(value['id']);store.playback(value['id'],playback(song_id,state='playing'))
    with store.connection() as db:assert db.execute('SELECT closed FROM rooms WHERE id=?',(value['id'],)).fetchone()[0]==1


def test_update_hold_blocks_voice_but_allows_mute_and_backing_controls(station):
    store,_=station;value,_=room(store);(store.root/'microphone-hold.json').write_text('{}')
    with pytest.raises(Refusal):store.controls(value,{'mic_muted':0})
    with pytest.raises(Refusal):store.controls(value,{'mic_gain':40})
    assert store.controls(value,{'mic_muted':1})['accepted']
    assert store.controls(value,{'music_gain':25})['accepted']
    with pytest.raises(Refusal) as invalid:store.controls(value,{'mic_gain':'wrong'})
    assert invalid.value.status==400


def test_tv_remote_controls_require_owner_and_live_room(station):
    store, client = station
    value, secret = room(store)
    path = '/agent/rooms/' + value['id'] + '/controls'
    assert client.post(path, headers={'X-KTV-Room': secret}, json={'original': 1}).status_code == 401
    owner = {'Authorization': 'Bearer ' + KEY}
    assert client.post(path, headers=owner, json={'original': 1}).status_code == 200
    state = store.status(value)
    assert state['controls']['original'] == 1
    assert state['controls']['mic_muted'] == 1
    assert client.post(path, headers=owner, json={'mic_gain': 100}).status_code == 400
    assert client.post(path, headers=owner, json={'mic_muted': False}).status_code == 400
    assert client.post(path, headers=owner, json={'vocal_preset': 'pop'}).status_code == 409
    assert client.post(path, headers=owner, json={'vocal_preset': 'vendor=1'}).status_code == 400
    caps = {'vocal_presets': ['dry', 'pop', 'ktv', 'power', 'opera', 'studio', 'echo'], 'sfx': True}
    store.playback(value['id'], playback(audio_capabilities=caps))
    for profile in ['pop', 'ktv', 'power', 'opera', 'studio', 'echo', 'dry']:
        assert client.post(path, headers=owner, json={'vocal_preset': profile}).status_code == 200
        updated = store.status(value)
        assert updated['controls']['vocal_preset'] == profile
        assert updated['controls']['mic_revision'] == state['controls']['mic_revision']
        assert updated['controls']['mic_gain'] == state['controls']['mic_gain']
        # A selected TV preset cannot become a fabricated DSP application receipt.
        assert updated['playback']['vocal_preset_state'] == 'unavailable'
    hold = store.root / 'microphone-hold.json'
    hold.write_text('{}')
    assert client.post(path, headers=owner, json={'mic_muted': 0}).status_code == 409
    assert client.post(path, headers=owner, json={'mic_muted': 1}).status_code == 200
    store.close(value['id'])
    assert client.post(path, headers=owner, json={'original': 0}).status_code == 410


def test_public_song_titles_remove_only_media_filename_wrapping(station):
    from integrations.ktv_public.nas_server import song_title
    from homeplane.ktv_metadata import song_title as private_title
    samples = {
        '01. 歌手 (Feat. 合作歌手) - 歌曲.flac': '歌曲',
        '01. 歌手 - 歌曲 (Live).flac': '歌曲 (Live)',
        '1973': '1973', 'Love - Part 2': 'Love - Part 2',
    }
    for raw, expected in samples.items():
        assert song_title(raw) == private_title(raw) == expected
    store, _ = station
    value, _ = room(store)
    store.poll()
    payload = {**song(), 'title': '01. 歌手 - 测试歌曲.flac'}
    store.add(value, payload)
    assert store.status(value)['songs'][0]['title'] == '测试歌曲'


def test_missing_lyrics_status_is_scoped_and_cleared_on_playback(station):
    store, _ = station
    value, _ = room(store)
    store.poll()
    item = store.add(value, song())['id']
    store.playback(value['id'], {**playback(), 'lyrics_waiting': item})
    assert store.status(value)['songs'][0]['lyrics_pending'] is True
    with pytest.raises(Refusal):
        store.playback(value['id'], {**playback(), 'lyrics_waiting': str(uuid.uuid4())})
    store.playback(value['id'], playback(item))
    assert store.status(value)['songs'][0]['lyrics_pending'] is False


def test_tv_bridge_rejects_wrong_room_and_synchronizes_controls(station, tmp_path):
    store, client = station
    config = tmp_path / 'remote.json'
    config.write_text(json.dumps({'endpoint': str(client.base_url).rstrip('/') + '/ktv', 'agent_key': KEY}))
    config.chmod(0o600)
    bridge = NasBridge(FakeQueue(), config, allow_loopback=True)
    bridge.endpoint = str(client.base_url).rstrip('/')
    store.origin, store.base_path = bridge.endpoint, ''
    party = bridge.create_party()
    assert bridge.control_from_tv({'room_id': party['room_id'], 'original': 1})['accepted']
    bridge.tick()
    assert bridge.current()['controls']['original'] == 1
    bridge.report_playback({'room_id': party['room_id'], **playback(audio_capabilities={
        'vocal_presets': ['dry', 'pop', 'ktv', 'power', 'opera'], 'sfx': True})})
    assert bridge.control_from_tv({'room_id': party['room_id'], 'vocal_preset': 'ktv'})['accepted']
    bridge.tick()
    assert bridge.current()['controls']['vocal_preset'] == 'ktv'
    assert bridge.control_from_tv({'room_id': party['room_id'], 'vocal_preset': 'dry'})['accepted']
    bridge.tick()
    assert bridge.current()['controls']['vocal_preset'] == 'dry'
    for body in [{'room_id': str(uuid.uuid4()), 'mic_muted': 0},
                 {'room_id': party['room_id'], 'mic_gain': 100},
                 {'room_id': party['room_id'], 'original': True},
                 {'room_id': party['room_id'], 'vocal_preset': True},
                 {'room_id': party['room_id'], 'vocal_preset': 'arbitrary-vendor-command'}]:
        with pytest.raises(ValueError):
            bridge.control_from_tv(body)


def test_audio_controls_are_capability_gated_and_old_tv_has_no_claimed_effect(station):
    store, client = station
    value, secret = room(store)
    store.poll()
    store.playback(value['id'], playback())
    status = store.status(value)
    assert status['controls']['vocal_preset'] == 'dry'
    assert status['controls']['sfx_gain'] == 60
    assert status['playback']['audio_capabilities'] == {}
    with pytest.raises(Refusal):
        store.controls(value, {'vocal_preset': 'pop'})
    for invalid in ({'vocal_preset': 'http://example.test'}, {'sfx_gain': 101}, {'sfx_gain': True}):
        with pytest.raises(Refusal):
            store.controls(value, invalid)
    caps = {'vocal_presets': ['dry', 'pop'], 'sfx': True}
    store.playback(value['id'], playback(audio_capabilities=caps, vocal_preset='pop', vocal_preset_state='requested'))
    revision = store.controls(value, {'vocal_preset': 'pop', 'sfx_gain': 75})['revision']
    status = store.status(value)
    assert status['controls']['control_revision'] == revision
    assert status['playback']['vocal_preset_state'] == 'requested'
    assert store.poll()['rooms'][0]['sfx_gain'] == 75
    # A normal command/control acknowledgement never promotes vendor writes to a readback.
    store.playback(value['id'], playback(applied_revision=revision))
    assert store.status(value)['playback']['vocal_preset_state'] == 'unavailable'
    base = '/api/rooms/' + value['id']
    assert client.post(base+'/command', headers={'X-KTV-Room': secret}, json={
        'action': 'sfx_stop', 'song': '', 'request_id': str(uuid.uuid4())}).status_code == 403


def test_sfx_receipt_idempotency_ttl_and_ack_scope(station):
    import time
    store, _ = station
    value, _ = room(store)
    store.poll()
    caps = {'vocal_presets': ['dry'], 'sfx': True}
    store.playback(value['id'], playback(audio_capabilities=caps))
    command = {'action': 'sfx_applause', 'song': '', 'request_id': str(uuid.uuid4())}
    first = store.command(value, command)
    assert store.command(value, command) == first
    assert store.status(value)['playback']['sfx_state'] == 'idle'
    with pytest.raises(Refusal):
        store.command(value, {**command, 'action': 'sfx_cheer'})
    with pytest.raises(Refusal):
        store.command(value, {**command, 'request_id': str(uuid.uuid4()), 'song': str(uuid.uuid4())})
    pending = store.poll()['rooms'][0]['commands'][0]
    assert pending['expires'] == pending['created'] + 5
    store.playback(value['id'], playback(last_command=first['seq'], audio_capabilities=caps,
        sfx_seq=first['seq'], sfx_action='sfx_applause', sfx_state='pending'))
    assert store.status(value)['playback']['sfx_state'] == 'pending'
    store.playback(value['id'], playback(last_command=first['seq'], audio_capabilities=caps,
        sfx_seq=first['seq'], sfx_action='sfx_applause', sfx_state='playing'))
    assert store.status(value)['playback']['sfx_state'] == 'playing'
    # Another room may never claim this room's effect ACK.
    other, _ = room(store)
    with pytest.raises(Refusal):
        store.playback(other['id'], playback(last_command=first['seq'],
            sfx_seq=first['seq'], sfx_action='sfx_applause', sfx_state='playing'))
    with pytest.raises(Refusal):
        store.playback(value['id'], playback(last_command=first['seq'],
            sfx_seq=first['seq'], sfx_action='sfx_cheer', sfx_state='playing'))
    # Neither expired nor fresh SFX backlog may occupy LIMIT 20 and hide stop.
    with store.connection() as db:
        now = int(time.time())
        for _ in range(24):
            db.execute('INSERT INTO commands(room,request_id,action,song,created) VALUES(?,?,?,?,?)',
                (value['id'], str(uuid.uuid4()), 'sfx_cheer', '', now-6 if _ < 12 else now))
    stop = store.command(value, {'action': 'sfx_stop', 'song': '', 'request_id': str(uuid.uuid4())})
    pending = next(r for r in store.poll()['rooms'] if r['room_id'] == value['id'])['commands']
    assert [c['seq'] for c in pending] == [stop['seq']]
    store.playback(value['id'], playback(last_command=stop['seq'],
        sfx_seq=stop['seq'], sfx_action='sfx_stop', sfx_state='stopped'))
    assert store.status(value)['playback']['sfx_state'] == 'stopped'
    # A late older ACK must not replace the latest stopped state.
    store.playback(value['id'], playback(last_command=first['seq'],
        sfx_seq=first['seq'], sfx_action='sfx_applause', sfx_state='playing'))
    assert store.status(value)['playback']['sfx_state'] == 'stopped'


def test_sfx_cached_commands_expire_and_stop_does_not_wait_for_previous_ack(station, tmp_path):
    import time
    store, client = station
    config = tmp_path/'bridge-sfx.json'
    config.write_text(json.dumps({'endpoint': str(client.base_url).rstrip('/')+'/ktv', 'agent_key': KEY}))
    config.chmod(0o600)
    bridge = NasBridge(FakeQueue(), config, allow_loopback=True)
    bridge.endpoint = str(client.base_url).rstrip('/')
    store.origin, store.base_path = bridge.endpoint, ''
    party = bridge.create_party()
    value = store.room(party['room_id'], parse_qs(urlsplit(party['join_url']).fragment)['k'][0])
    bridge.report_playback({'room_id': value['id'], **playback(audio_capabilities={'vocal_presets':['dry'],'sfx':True})})
    bridge.tick()
    for action in ('sfx_applause', 'sfx_stop'):
        store.command(value, {'action':action,'song':'','request_id':str(uuid.uuid4())})
    bridge.tick()
    assert [c['action'] for c in bridge.current()['commands']] == ['sfx_stop']
    for command in bridge.party['commands']:
        command.update(created=int(time.time())-6, expires=int(time.time())-1)
    assert bridge.current()['commands'] == []
    bridge.close()


def test_guide_reference_is_same_song_verified_variant_and_cleared_by_legacy_update(station):
    import hashlib
    store, client = station
    value, secret = room(store)
    store.poll()
    item = store.add(value, song())['id']
    store.update({'updates':[{'id':item,'state':'ready','code':'none'}]})
    folder = store.root.parent/'guides'
    folder.mkdir()
    blobs = [b'original'*400, b'accompaniment'*400]
    specs = []
    for raw in blobs:
        sha = hashlib.sha256(raw).hexdigest()
        (folder/(sha+'.mp3')).write_bytes(raw)
        specs.append({'sha256':sha,'bytes':len(raw),'duration_ms':5000})
    reference = {**specs[1], 'kind':'accompaniment'}
    payload = {'id':item, **specs[0], 'reference':reference}
    store.guide_update(payload)
    store.playback(value['id'], playback(item))
    assert store.status(value)['playback']['guide']['reference'] == reference
    base = '/api/rooms/'+value['id']
    headers = {'X-KTV-Room':secret}
    identity = client.post(base+'/join',headers=headers,json={}).json()
    assert client.get(base+'/guide?id='+item+'&variant=reference',headers=headers).status_code == 403
    headers['X-KTV-Guest'] = identity['key']
    assert client.get(base+'/guide?id='+item,headers=headers).content == blobs[0]
    assert client.get(base+'/guide?id='+item+'&variant=reference',headers=headers).content == blobs[1]
    assert client.get(base+'/guide?id='+item+'&variant=remote',headers=headers).status_code == 400
    for changed in ({'kind':'original'}, {'duration_ms':5200}, {'audio_url':'https://example.test'}):
        with pytest.raises(Refusal):
            store.guide_update({**payload,'reference':{**reference,**changed}})
    (folder/(reference['sha256']+'.mp3')).write_bytes(b'x'*reference['bytes'])
    assert client.get(base+'/guide?id='+item+'&variant=reference',headers=headers).status_code == 409
    store.guide_update({'id':item,**specs[0]})
    assert 'reference' not in store.status(value)['playback']['guide']
    assert client.get(base+'/guide?id='+item+'&variant=reference',headers=headers).status_code == 409


def test_bridge_blocked_guide_does_not_block_controls_or_overwrite_new_room(station, tmp_path):
    store, client = station
    entered, release = threading.Event(), threading.Event()
    class SlowGuide:
        def protect(self, _): pass
        def get(self, _):
            entered.set()
            assert release.wait(3)
            return {'sha256':'c'*64,'bytes':2048,'duration_ms':2000}
        def close(self): pass
    config = tmp_path/'bridge-workers.json'
    config.write_text(json.dumps({'endpoint':str(client.base_url).rstrip('/')+'/ktv','agent_key':KEY}))
    config.chmod(0o600)
    queue = FakeQueue()
    bridge = NasBridge(queue,config,allow_loopback=True,guide=SlowGuide())
    bridge.endpoint = str(client.base_url).rstrip('/')
    store.origin, store.base_path = bridge.endpoint, ''
    party = bridge.create_party()
    value = store.room(party['room_id'],parse_qs(urlsplit(party['join_url']).fragment)['k'][0])
    store.poll()
    item = store.add(value,song())['id']
    bridge.tick()
    queue.jobs[item].update(state='ready',code='none')
    bridge.background = True
    bridge.tick()
    worker = threading.Thread(target=bridge._work_once)
    worker.start()
    assert entered.wait(2)
    # Holding guide.get cannot hold snapshot/report or a second control poll.
    completed = threading.Event()
    def foreground():
        assert bridge.current()['room_id'] == value['id']
        bridge.report_playback({'room_id':value['id'],**playback(item)})
        store.controls(value, {'music_gain':42})
        bridge.tick()
        assert bridge.current()['controls']['music_gain'] == 42
        completed.set()
    foreground_thread = threading.Thread(target=foreground)
    foreground_thread.start()
    try:
        assert completed.wait(2)
        bridge.close_party()
        fresh = bridge.create_party()
        assert fresh['room_id'] != value['id']
    finally:
        release.set()
        worker.join(3)
        foreground_thread.join(3)
    assert bridge.current()['room_id'] == fresh['room_id']
    assert bridge.current()['order'] == []
    assert bridge.playback is None
    # Work captured from the previous room may not publish its pending media.
    with store.connection() as db:
        assert not db.execute('SELECT guide_sha FROM songs WHERE id=?',(item,)).fetchone()[0]
    bridge.close()


def test_new_party_defaults_to_muted_mic_and_warmup_preserves_it(station):
    store, _ = station
    value, _ = room(store)
    controls = store.status(value)['controls']
    assert controls['mic_muted'] == 1
    assert controls['mic_revision'] == 0
    # A warmup has no public queue song ID. Its actual mute report must not
    # silently rewrite the room's desired setting before the TV applies it.
    store.playback(value['id'], playback(state='playing', mic_muted=True))
    assert store.status(value)['controls']['mic_muted'] == 1
    store.controls(value, {'mic_muted': 1})
    explicit = store.status(value)['controls']
    store.playback(value['id'], playback(state='playing', mic_muted=False,
        applied_revision=explicit['control_revision']))
    store.controls(value, {'original': 1, 'music_gain': 35})
    room_snapshot = next(row for row in store.poll()['rooms'] if row['room_id'] == value['id'])
    assert room_snapshot['mic_muted'] == 1
    assert room_snapshot['mic_revision'] == explicit['mic_revision']
    # Restarting the NAS store preserves this room's explicit preference.
    reopened = Store(store.root, store.origin, store.base_path)
    assert reopened.status(value)['controls']['mic_muted'] == 1
    fresh, _ = room(reopened)
    assert reopened.status(fresh)['controls']['mic_muted'] == 1


def test_new_party_during_explicit_update_hold_remains_muted(station):
    store, _ = station
    (store.root / 'microphone-hold.json').write_text('{}')
    value, _ = room(store)
    status = store.status(value)
    assert status['controls']['mic_muted'] == 1
    assert status['microphone_available'] is False
    with pytest.raises(Refusal) as error:
        store.controls(value, {'mic_muted': 0})
    assert error.value.code == 'microphone_update_required'


def test_default_guest_names_are_room_unique_without_sharing_credentials(tmp_path, monkeypatch):
    # Even repeated random choices and simultaneous joins must not collide.
    monkeypatch.setattr('integrations.ktv_public.nas_server.secrets.randbelow', lambda count: 0)
    store = Store(tmp_path / 'nas', 'https://example.test')
    value, _ = room(store)
    with ThreadPoolExecutor(max_workers=6) as pool:
        guests = list(pool.map(lambda _: store.join(value), range(60)))
    assert len({guest['nickname'] for guest in guests}) == 60
    assert len({guest['id'] for guest in guests}) == 60
    assert len({guest['key'] for guest in guests}) == 60
    assert set(NAMES).issubset({guest['nickname'] for guest in guests})
    assert all(len(guest['nickname']) <= 24 for guest in guests)
    # Authentication reuses a credential; it never resolves identity by name.
    guest = guests[0]
    assert store.guest(value, guest['key']) == store.guest(value, guest['key'])
    assert store.guest(value, guest['key'])['id'] == guest['id']
    with pytest.raises(Refusal) as full:
        store.join(value)
    assert full.value.code == 'guest_capacity'
    fresh, _ = room(store)
    assert store.join(fresh)['nickname'] == NAMES[0]
    with pytest.raises(Refusal) as different_room:
        store.guest(fresh, guest['key'])
    assert different_room.value.code == 'guest_unavailable'


def test_default_names_avoid_saved_names_and_never_rename_saved_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr('integrations.ktv_public.nas_server.secrets.randbelow', lambda count: 0)
    store = Store(tmp_path / 'nas', 'https://example.test')
    value, _ = room(store)
    custom = store.join(value)
    store.profile(value, custom, {'nickname': NAMES[1], 'avatar': 1})
    generated = store.join(value)
    assert generated['nickname'] == NAMES[0]
    assert generated['nickname'] != store.guest(value, custom['key'])['nickname']
    # A deliberate duplicate saved through the profile editor remains a choice.
    store.profile(value, generated, {'nickname': NAMES[1], 'avatar': 1})
    before = [store.guest(value, guest['key']) for guest in (custom, generated)]
    reopened = Store(store.root, store.origin)
    assert [reopened.guest(value, guest['key']) for guest in (custom, generated)] == before
    assert reopened.join(value)['nickname'] not in {guest['nickname'] for guest in before}
    with reopened.connection() as db:
        assert {row[0] for row in db.execute('SELECT nickname_custom FROM guests WHERE id IN (?,?)',
            (custom['id'], generated['id']))} == {1}


def test_legacy_duplicate_defaults_migrate_once_without_changing_guest_or_song_identity(tmp_path, monkeypatch):
    monkeypatch.setattr('integrations.ktv_public.nas_server.secrets.randbelow', lambda count: 0)
    store = Store(tmp_path / 'nas', 'https://example.test')
    value, _ = room(store)
    store.poll()
    guests = [store.join(value) for _ in range(6)]
    names = [(NAMES[2], 2), (NAMES[2], 2), ('自己起的名字', 1),
             ('自己起的名字', 2), (NAMES[0] + ' 12', 0), (NAMES[5], 5)]
    songs = []
    for index, (guest, (nickname, avatar)) in enumerate(zip(guests, names)):
        profile = store.profile(value, guest, {'nickname': nickname, 'avatar': avatar})
        songs.append(store.add(value, {**song(), 'song_id': str(100 + index)}, profile))
    second_room, _ = room(store)
    other = store.join(second_room)
    store.profile(second_room, other, {'nickname': NAMES[2], 'avatar': 2})
    # Recreate the actual six-column legacy table: no historical custom marker.
    with store.connection() as db:
        credentials = {row['id']: row['token_hash'] for row in db.execute('SELECT id,token_hash FROM guests')}
        db.execute('CREATE TABLE guests_v16(id TEXT PRIMARY KEY,room TEXT NOT NULL,token_hash TEXT NOT NULL,'
                   'nickname TEXT NOT NULL,avatar INTEGER NOT NULL,created INTEGER NOT NULL)')
        db.execute('INSERT INTO guests_v16 SELECT id,room,token_hash,nickname,avatar,created FROM guests')
        db.execute('DROP TABLE guests')
        db.execute('ALTER TABLE guests_v16 RENAME TO guests')
    reopened = Store(store.root, store.origin)
    profiles = [reopened.guest(value, guest['key']) for guest in guests]
    assert profiles[0]['nickname'] == NAMES[2]
    assert profiles[1]['nickname'] != NAMES[2]
    assert profiles[1]['nickname'] == NAMES[profiles[1]['avatar']]
    assert [(guest['nickname'], guest['avatar']) for guest in profiles[2:]] == names[2:]
    assert reopened.guest(second_room, other['key'])['nickname'] == NAMES[2]
    rows = reopened.status(value)['songs']
    for row, item, guest, profile in zip(rows, songs, guests, profiles):
        assert row['id'] == item['id'] and row['guest_id'] == guest['id']
        assert (row['nickname'], row['avatar']) == (profile['nickname'], profile['avatar'])
    with reopened.connection() as db:
        assert {row['id']: row['token_hash'] for row in db.execute('SELECT id,token_hash FROM guests')} == credentials
        assert db.execute('SELECT count(*) FROM guests WHERE nickname_custom=-1').fetchone()[0] == 0
    # Restarting again cannot rename these rows or strip an intentional suffix.
    again = Store(store.root, store.origin)
    assert [again.guest(value, guest['key']) for guest in guests] == profiles
    assert again.status(value)['queue_revision'] == reopened.status(value)['queue_revision']


def test_legacy_default_collision_keeps_an_explicitly_saved_template_name(tmp_path, monkeypatch):
    monkeypatch.setattr('integrations.ktv_public.nas_server.secrets.randbelow', lambda count: 0)
    store = Store(tmp_path / 'nas', 'https://example.test')
    value, _ = room(store)
    legacy, custom = store.join(value), store.join(value)
    store.profile(value, custom, {'nickname': NAMES[0], 'avatar': 0})
    with store.connection() as db:
        db.execute('UPDATE guests SET nickname_custom=-1 WHERE id=?', (legacy['id'],))
    reopened = Store(store.root, store.origin)
    assert reopened.guest(value, custom['key']) == {'id': custom['id'], 'nickname': NAMES[0], 'avatar': 0}
    assert reopened.guest(value, legacy['key'])['nickname'] != NAMES[0]


def test_add_retries_and_existing_songs_do_not_consume_new_add_budget(station):
    store, client = station
    value, secret = room(store)
    store.poll()
    one, two = store.join(value), store.join(value)
    base = '/api/rooms/' + value['id']
    headers = {'X-KTV-Room': secret, 'X-KTV-Guest': one['key']}
    first_payload = song()
    first = client.post(base + '/songs', headers=headers, json=first_payload)
    assert first.status_code == 202
    for _ in range(30):
        repeated = client.post(base + '/songs', headers=headers, json=first_payload)
        assert repeated.status_code == 202 and repeated.json()['id'] == first.json()['id']
    for index in range(19):
        assert client.post(base + '/songs', headers=headers,
            json={**song(), 'song_id': str(1000 + index)}).status_code == 202
    assert client.post(base + '/songs', headers=headers, json={**song(), 'song_id': '9999'}).json()['code'] == 'rate_limited'
    # Retries still resolve after the room's new-add slots have all been used.
    assert client.post(base + '/songs', headers=headers, json=first_payload).json()['id'] == first.json()['id']
    other_headers = {**headers, 'X-KTV-Guest': two['key']}
    duplicate = client.post(base + '/songs', headers=other_headers, json=song())
    assert duplicate.status_code == 202 and duplicate.json()['duplicate'] is True
    assert duplicate.json()['id'] == first.json()['id']
    # A request ID still belongs to its original guest and exact payload.
    assert client.post(base + '/songs', headers=other_headers, json=first_payload).json()['code'] == 'idempotency_conflict'
    assert client.post(base + '/songs', headers=headers,
        json={**first_payload, 'title': '被更改的标题'}).json()['code'] == 'idempotency_conflict'
    assert len(store.status(value)['songs']) == 20


def test_status_authentication_stays_read_only_while_another_writer_holds_a_reserved_lock(tmp_path):
    store = Store(tmp_path / 'nas', 'https://example.test')
    value, secret = room(store)
    writer = sqlite3.connect(store.db)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute('UPDATE rooms SET music_gain=42 WHERE id=?', (value['id'],))
        future = pool.submit(lambda: store.status(store.room(value['id'], secret)))
        # A SQLite reserved writer permits reading the last committed snapshot.
        # The former _expire() inside room() attempted a write and timed out here.
        assert future.result(timeout=1)['controls']['music_gain'] == 70
        writer.commit()
        assert store.status(store.room(value['id'], secret))['controls']['music_gain'] == 42
    finally:
        writer.rollback()
        writer.close()
        pool.shutdown()


@pytest.mark.parametrize('mutation', ['closed', 'deleted'])
def test_status_after_concurrent_room_expiry_or_deletion_is_a_known_refusal(tmp_path, mutation):
    store = Store(tmp_path / 'nas', 'https://example.test')
    authenticated, _ = room(store)
    def change_room():
        with store.connection() as db:
            if mutation == 'closed':
                db.execute('UPDATE rooms SET closed=1 WHERE id=?', (authenticated['id'],))
            else:
                db.execute('DELETE FROM rooms WHERE id=?', (authenticated['id'],))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(change_room).result(timeout=1)
    with pytest.raises(Refusal) as refused:
        store.status(authenticated)
    assert refused.value.status == 410 and refused.value.code == 'room_closed'


def test_private_request_diagnostics_are_fixed_categories_rotated_and_contain_no_request_data(station, monkeypatch):
    store, client = station
    value, secret = room(store)
    private_detail = 'private key URL song path or exception content'
    def broken_status(room):
        error = sqlite3.OperationalError(private_detail)
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error
    monkeypatch.setattr(store, 'status', broken_status)
    response = client.get('/api/rooms/' + value['id'] + '/status', headers={'X-KTV-Room': secret})
    assert response.status_code == 503 and response.json()['code'] == 'service_unavailable'
    path = store.root / 'service-events.jsonl'
    event = json.loads(path.read_text())
    assert set(event) == {'event', 'at', 'operation', 'exception', 'sqlite'}
    assert event == {**event, 'event': 'request_failed', 'operation': 'status',
        'exception': 'OperationalError', 'sqlite': 'SQLITE_BUSY'}
    assert private_detail not in path.read_text() and secret not in path.read_text() and value['id'] not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr('integrations.ktv_public.nas_server.SERVICE_EVENT_LIMIT', 160)
    for _ in range(20):
        store.record_failure(private_detail, ValueError(private_detail))
    paths = sorted(store.root.glob('service-events*.jsonl'))
    assert len(paths) == 2 and all(item.stat().st_size < 400 for item in paths)
    for item in paths:
        assert private_detail not in item.read_text()
        assert item.stat().st_mode & 0o777 == 0o600
        assert all(json.loads(line)['operation'] in {'status', 'other'} for line in item.read_text().splitlines())


def test_private_request_diagnostics_refuse_a_symlink_target(tmp_path):
    store = Store(tmp_path / 'nas', 'https://example.test')
    outside = tmp_path / 'untouched'
    outside.write_text('keep')
    (store.root / 'service-events.jsonl').symlink_to(outside)
    store.record_failure('status', ValueError('private'))
    assert outside.read_text() == 'keep'


def test_duplicate_add_receipt_survives_completion_restart_and_payload_or_guest_conflicts(tmp_path):
    store = Store(tmp_path / 'nas', 'https://example.test')
    value, _ = room(store)
    store.poll()
    one, two = store.join(value), store.join(value)
    first = store.add(value, song(), one)
    duplicate_payload = {**song(), 'title': '同一首歌的搜索标题'}
    duplicate = store.add(value, duplicate_payload, two)
    assert duplicate['duplicate'] is True and duplicate['id'] == first['id']
    # The duplicate reply was lost, then the already queued song finished.
    store.playback(value['id'], playback('', played=[first['id']]))
    reopened = Store(store.root, store.origin)
    assert reopened.add(value, duplicate_payload, two) == duplicate
    assert len(reopened.status(value)['songs']) == 1
    for payload, guest in [
        (duplicate_payload, one),
        ({**duplicate_payload, 'title': '不同标题'}, two),
        ({**duplicate_payload, 'song_id': '124'}, two),
        ({**duplicate_payload, 'source': 'cloud'}, two),
    ]:
        with pytest.raises(Refusal) as conflict:
            reopened.add(value, payload, guest)
        assert conflict.value.code == 'idempotency_conflict'
    # An explicit new tap after completion remains a legitimate new order.
    next_song = reopened.add(value, {**duplicate_payload, 'request_id': str(uuid.uuid4())}, two)
    assert next_song['id'] != first['id'] and len(reopened.status(value)['songs']) == 2
    assert reopened.add(value, duplicate_payload, two)['id'] == first['id']
    another_room, _ = room(reopened)
    other = reopened.join(another_room)
    assert reopened.add(another_room, duplicate_payload, other)['id'] != first['id']
    with reopened.connection() as db:
        assert db.execute('SELECT count(*) FROM song_requests WHERE room=?', (value['id'],)).fetchone()[0] == 3
        db.execute('UPDATE rooms SET expires=0 WHERE id=?', (value['id'],))
    reopened.poll()
    with reopened.connection() as db:
        assert db.execute('SELECT count(*) FROM song_requests WHERE room=?', (value['id'],)).fetchone()[0] == 3
    # Retention is independent: only explicit end plus an elapsed retention
    # horizon may remove idempotency records from an old completed session.
    reopened.close(value['id'])
    with reopened.connection() as db:
        db.execute('UPDATE rooms SET expires=0 WHERE id=?', (value['id'],))
    reopened.poll()
    with reopened.connection() as db:
        assert db.execute('SELECT count(*) FROM song_requests WHERE room=?', (value['id'],)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM song_requests WHERE room=?', (another_room['id'],)).fetchone()[0] == 1


def legacy_guest_queue_store(tmp_path):
    store = Store(tmp_path / 'nas', 'https://example.test')
    value, _ = room(store)
    store.poll()
    one, two = store.join(value), store.join(value)
    for guest in (one, two):
        store.profile(value, guest, {'nickname': NAMES[2], 'avatar': 2})
    item = store.add(value, song(), two)
    with store.connection() as db:
        db.execute('CREATE TABLE guests_legacy(id TEXT PRIMARY KEY,room TEXT NOT NULL,token_hash TEXT NOT NULL,'
                   'nickname TEXT NOT NULL,avatar INTEGER NOT NULL,created INTEGER NOT NULL)')
        db.execute('INSERT INTO guests_legacy SELECT id,room,token_hash,nickname,avatar,created FROM guests')
        db.execute('DROP TABLE guests')
        db.execute('ALTER TABLE guests_legacy RENAME TO guests')
        db.execute('DROP TABLE song_requests')
    return store, value, (one, two), item


def test_native_schema_backup_precedes_guest_or_song_changes_and_has_private_atomic_receipt(tmp_path):
    store, value, guests, item = legacy_guest_queue_store(tmp_path)
    with store.connection() as db:
        before_guests = [tuple(row) for row in db.execute('SELECT * FROM guests ORDER BY rowid')]
        before_songs = [tuple(row) for row in db.execute('SELECT * FROM songs ORDER BY rowid')]
    upgraded = Store(store.root, store.origin)
    receipt_path = store.root / 'guest-queue-v2-backup.json'
    receipt = json.loads(receipt_path.read_text())
    backup = store.root / receipt['file']
    assert backup.name == 'state-before-guest-queue-v2.sqlite'
    assert receipt['schema'] == 'guest-queue-v2' and receipt['reused'] is False
    assert receipt['sha256'] == hashlib.sha256(backup.read_bytes()).hexdigest()
    assert receipt_path.stat().st_mode & 0o777 == backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM guests ORDER BY rowid')] == before_guests
        assert [tuple(row) for row in db.execute('SELECT * FROM songs ORDER BY rowid')] == before_songs
        assert 'nickname_custom' not in {row[1] for row in db.execute('PRAGMA table_info(guests)')}
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='song_requests'").fetchone()
        assert db.execute('PRAGMA quick_check').fetchall() == [('ok',)]
    assert upgraded.guest(value, guests[0]['key'])['nickname'] == NAMES[2]
    assert upgraded.guest(value, guests[1]['key'])['nickname'] != NAMES[2]
    assert upgraded.status(value)['songs'][0]['id'] == item['id']
    assert upgraded.status(value)['songs'][0]['guest_id'] == guests[1]['id']
    assert not list(store.root.glob('.guest-queue-v2-*'))


@pytest.mark.parametrize('initial', ['missing', 'zero', 'empty_sqlite'])
def test_new_empty_or_already_upgraded_store_does_not_create_migration_backup(tmp_path, initial):
    root = tmp_path / 'nas'
    root.mkdir()
    if initial == 'zero':
        (root / 'state.sqlite').touch()
    elif initial == 'empty_sqlite':
        with sqlite3.connect(root / 'state.sqlite') as db:
            db.execute('VACUUM')
    Store(root, 'https://example.test')
    Store(root, 'https://example.test')
    assert not list(root.glob('state-before-guest-queue-v2*'))
    assert not (root / 'guest-queue-v2-backup.json').exists()


@pytest.mark.parametrize('unsafe', ['corrupt', 'symlink'])
def test_bad_existing_migration_backup_fails_before_modifying_the_original_database(tmp_path, unsafe):
    store, _, _, _ = legacy_guest_queue_store(tmp_path)
    original = store.db.read_bytes()
    backup = store.root / 'state-before-guest-queue-v2.sqlite'
    untouched = tmp_path / 'untouched'
    untouched.write_bytes(b'keep original target')
    if unsafe == 'symlink':
        backup.symlink_to(untouched)
    else:
        backup.write_bytes(b'not a database')
    with pytest.raises((sqlite3.DatabaseError, ValueError)):
        Store(store.root, store.origin)
    assert store.db.read_bytes() == original
    assert untouched.read_bytes() == b'keep original target'
    assert not (store.root / 'guest-queue-v2-backup.json').exists()
    assert not list(store.root.glob('.guest-queue-v2-*'))


@pytest.mark.parametrize('alias', ['fixed', 'receipt', 'fixed_with_valid_receipt'])
def test_migration_backup_refuses_hard_links_to_live_database_before_any_changes(tmp_path, alias):
    store, _, _, _ = legacy_guest_queue_store(tmp_path)
    original = store.db.read_bytes()
    fixed = store.root / 'state-before-guest-queue-v2.sqlite'
    named = store.root / ('state-before-guest-queue-v2-20261101T010203-' + 'a' * 32 + '.sqlite')
    receipt = store.root / 'guest-queue-v2-backup.json'
    if alias == 'receipt':
        named.hardlink_to(store.db)
    else:
        fixed.hardlink_to(store.db)
    if alias == 'fixed_with_valid_receipt':
        named.write_bytes(original)
    if alias != 'fixed':
        receipt.write_text(json.dumps({'schema': 'guest-queue-v2', 'file': named.name,
            'sha256': hashlib.sha256(original).hexdigest()}))
    original_receipt = receipt.read_bytes() if receipt.exists() else None
    with pytest.raises(ValueError, match='unsafe_migration_backup'):
        Store(store.root, store.origin)
    assert store.db.read_bytes() == original
    assert (receipt.read_bytes() if receipt.exists() else None) == original_receipt
    assert not list(store.root.glob('.guest-queue-v2-*'))


@pytest.mark.parametrize('failure', ['copy', 'publish'])
def test_native_backup_failure_cannot_start_a_schema_migration(tmp_path, monkeypatch, failure):
    store, _, _, _ = legacy_guest_queue_store(tmp_path)
    original = store.db.read_bytes()
    if failure == 'publish':
        def refuse_link(*args, **kwargs):
            raise OSError('backup publication refused')
        monkeypatch.setattr('integrations.ktv_public.nas_server.os.link', refuse_link)
    else:
        original_connect = sqlite3.connect
        class RefuseBackup:
            def __init__(self, connection):
                self.connection = connection
            def __getattr__(self, name):
                return getattr(self.connection, name)
            def backup(self, *args, **kwargs):
                raise sqlite3.OperationalError('backup unavailable')
        def connect(path, *args, **kwargs):
            connection = original_connect(path, *args, **kwargs)
            return RefuseBackup(connection) if path == store.db.as_uri() + '?mode=rw' else connection
        monkeypatch.setattr('integrations.ktv_public.nas_server.sqlite3.connect', connect)
    with pytest.raises((OSError, sqlite3.OperationalError)):
        Store(store.root, store.origin)
    assert store.db.read_bytes() == original
    assert not (store.root / 'state-before-guest-queue-v2.sqlite').exists()
    assert not (store.root / 'guest-queue-v2-backup.json').exists()
    assert not list(store.root.glob('.guest-queue-v2-*'))


@pytest.mark.parametrize('changed', [False, True])
def test_failed_migration_rolls_back_and_keeps_backup_then_reuses_or_preserves_a_stale_snapshot(tmp_path, monkeypatch, changed):
    store, _, _, _ = legacy_guest_queue_store(tmp_path)
    original = store.db.read_bytes()
    original_upgrade = Store._upgrade_guest_names
    def fail_after_guest_update(self, db):
        original_upgrade(self, db)
        raise RuntimeError('migration interrupted')
    monkeypatch.setattr(Store, '_upgrade_guest_names', fail_after_guest_update)
    with pytest.raises(RuntimeError, match='migration interrupted'):
        Store(store.root, store.origin)
    assert store.db.read_bytes() == original
    fixed = store.root / 'state-before-guest-queue-v2.sqlite'
    fixed_bytes, fixed_inode = fixed.read_bytes(), fixed.stat().st_ino
    assert fixed.is_file() and (store.root / 'guest-queue-v2-backup.json').is_file()
    monkeypatch.setattr(Store, '_upgrade_guest_names', original_upgrade)
    if changed:
        # A legitimate write by the previous server must not make restart impossible.
        with store.connection() as db:
            db.execute("UPDATE songs SET title='备份之后的合法更新'")
    Store(store.root, store.origin)
    receipt = json.loads((store.root / 'guest-queue-v2-backup.json').read_text())
    assert fixed.read_bytes() == fixed_bytes and fixed.stat().st_ino == fixed_inode
    assert receipt['reused'] is (not changed)
    assert (receipt['file'] != fixed.name) is changed
    assert len(list(store.root.glob('state-before-guest-queue-v2*.sqlite'))) == (2 if changed else 1)
    with sqlite3.connect(store.root / receipt['file']) as db:
        assert (db.execute('SELECT title FROM songs').fetchone()[0] == '备份之后的合法更新') is changed


@pytest.mark.parametrize('legacy', [False, True])
def test_restart_recovers_interrupted_sqlite_write_before_reading_migration_schema(tmp_path, legacy):
    if legacy:
        store, value, guests, item = legacy_guest_queue_store(tmp_path)
    else:
        store = Store(tmp_path / 'nas', 'https://example.test')
        value, _ = room(store)
        guests = [store.join(value)]
        store.poll()
        item = store.add(value, song(), guests[0])
    with store.connection() as db:
        db.execute('CREATE TABLE crash_fixture(id INTEGER PRIMARY KEY, payload BLOB)')
        db.executemany('INSERT INTO crash_fixture(payload) VALUES (?)', [(b'original' * 1024,)] * 64)
        before_guests = [tuple(row) for row in db.execute('SELECT id,token_hash FROM guests ORDER BY id')]
    # Force dirty pages to disk, then exit without COMMIT or connection cleanup.
    # This reproduces a real hot rollback journal, not a mocked SQLite exception.
    child = subprocess.run([sys.executable, '-c', '''
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=DELETE')
db.execute('PRAGMA synchronous=FULL')
db.execute('PRAGMA cache_size=1')
db.execute('BEGIN IMMEDIATE')
db.execute("UPDATE guests SET nickname='uncommitted'")
db.execute("UPDATE songs SET title='uncommitted'")
db.execute('UPDATE crash_fixture SET payload=zeroblob(16384)')
os._exit(17)
''', str(store.db)], check=False)
    assert child.returncode == 17
    journal = store.db.with_name('state.sqlite-journal')
    assert journal.is_file() and journal.read_bytes()[:8] != b'\0' * 8
    reader = sqlite3.connect(store.db.as_uri() + '?mode=ro', uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            reader.execute('SELECT name FROM sqlite_master').fetchall()
    finally:
        reader.close()
    recovered = Store(store.root, store.origin)
    with recovered.connection() as db:
        assert db.execute('PRAGMA quick_check').fetchall()[0][0] == 'ok'
        assert [tuple(row) for row in db.execute('SELECT id,token_hash FROM guests ORDER BY id')] == before_guests
        assert db.execute("SELECT count(*) FROM crash_fixture WHERE payload=?", (b'original' * 1024,)).fetchone()[0] == 64
        assert not db.execute("SELECT id FROM guests WHERE nickname='uncommitted'").fetchone()
        assert not db.execute("SELECT id FROM songs WHERE title='uncommitted'").fetchone()
    assert recovered.status(value)['songs'][0]['id'] == item['id']
    for guest in guests:
        assert recovered.guest(value, guest['key'])['id'] == guest['id']
    if legacy:
        receipt = json.loads((store.root / 'guest-queue-v2-backup.json').read_text())
        with sqlite3.connect(store.root / receipt['file']) as db:
            assert db.execute('SELECT count(*) FROM crash_fixture WHERE payload=?', (b'original' * 1024,)).fetchone()[0] == 64
            assert 'nickname_custom' not in {row[1] for row in db.execute('PRAGMA table_info(guests)')}


def test_voice_protection_reports_round_trip_with_closed_scalar_schema(station):
    from homeplane.ktv_nas_bridge import audio_playback as private_audio
    from integrations.ktv_public.nas_server import audio_playback as public_audio
    store, client = station
    value, _ = room(store)
    store.poll()
    for state in ('off', 'preparing', 'monitoring', 'attenuated', 'muted', 'failed'):
        voice = {'state': state, 'effective_gain': 26 if state == 'attenuated' else 0, 'mute_confirmed': state != 'preparing'}
        payload = playback(audio_capabilities={'vocal_presets': ['dry', 'pop'], 'sfx': True, 'voice_protection': voice})
        assert private_audio(payload) == public_audio(payload)
        store.playback(value['id'], payload)
        assert store.status(value)['playback']['audio_capabilities']['voice_protection'] == voice
    for invalid in ({'state': 'vendor-string', 'effective_gain': 0},
                    {'state': 'muted', 'effective_gain': True},
                    {'state': 'muted', 'effective_gain': 101},
                    {'state': 'muted', 'effective_gain': 0, 'pcm': [1]},
                    {'state': 'muted', 'effective_gain': 0, 'mute_confirmed': 1}, None):
        payload = playback(audio_capabilities={'vocal_presets': ['dry'], 'sfx': True, 'voice_protection': invalid})
        for parse in (private_audio, public_audio):
            with pytest.raises(ValueError):
                parse(payload)
