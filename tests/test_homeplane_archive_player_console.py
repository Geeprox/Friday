"""SIMULATED UI lifecycle, not a decoder or hardware acceptance test."""

import json
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homeplane.api import create_app
from homeplane.archive_contracts import ArchiveResolveRequest


def protected_api_plan(tmp_path):
    origin = "http://127.0.0.1:8740"
    app = create_app(db_path=tmp_path / "SIMULATED.sqlite", settings_path=tmp_path / "settings.json",
                     authority_path=tmp_path / "authority.json")
    with TestClient(app, base_url=origin) as client:
        context = client.get("/api/archive/context").json()
        camera = context["cameras"][0]
        start = datetime.fromisoformat(camera["example_ranges"]["ready"]["start"])
        request = {"expected_context": context["fingerprint"], "camera_id": camera["id"],
                   "time_range": {"start": (start + timedelta(seconds=30)).isoformat(),
                                  "end": (start + timedelta(seconds=40)).isoformat()}}
        report = client.post("/api/archive/resolve", json=request, headers={"Origin": origin})
        assert report.status_code == 200
        request["media_ref"] = report.json()["segments"][0]["media_ref"]
        response = client.post("/api/archive/playback", json=request, headers={"Origin": origin})
        assert response.status_code == 200, response.text
        lookup = ArchiveResolveRequest(**{key:value for key,value in request.items() if key != 'media_ref'})
        return response.json(), lookup.model_dump(mode="json")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable for DOM contract")
def test_player_mute_seek_receipt_preservation_stop_and_late_source_guards(tmp_path):
    actual_plan, actual_lookup = protected_api_plan(tmp_path.resolve())
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict'),nodes=new Map();
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.attrs={};this._text='';this.value='';this.isConnected=true;this.duration=60;this.currentTime=0;this.pauseCount=0;}
 set textContent(v){this._text=String(v);this.children=[];}
 get textContent(){return this._text+this.children.map(c=>c.textContent??'').join('');}
 set innerHTML(_){throw Error('Unsafe source HTML');}
 setAttribute(k,v){this.attrs[k]=String(v);if(k==='id')nodes.set(String(v),this);if(k==='value')this.value=String(v);}
 removeAttribute(k){delete this.attrs[k];}
 append(...c){this.children.push(...c);if(this.tag==='select'&&!this.value&&this.children.length)this.value=this.children[0].value;}
 replaceChildren(...c){this.replaceCount=(this.replaceCount??0)+1;this._text='';this.children=[];this.append(...c);}
 addEventListener(k,v){this.handlers[k]=v;}
 pause(){this.pauseCount++;}
 load(){}
 requestVideoFrameCallback(fn){this.frame=fn;return 1;}
 cancelVideoFrameCallback(){this.frame=null;}
}
for(const id of ['navigation','developer-mode','mode','snapshot-time','page-title','content','notification','refresh']){const n=new Element('div');n.setAttribute('id',id);}
const fixture={mode:'simulator',generated_at:new Date().toISOString(),entities:[],devices:[],states:[],events:[],observations:[],rules:[],limitations:[]};
const range={start:'2026-10-01T00:00:30.300Z',end:'2026-10-01T00:00:40Z'},source={start:'2026-10-01T00:00:00Z',end:'2026-10-01T00:01:00Z'};
let context={fingerprint:'a'.repeat(64),simulated:true,cameras:[{id:'SIMULATED-camera',name:'SIMULATED camera',example_ranges:{ready:range,pending:range,expired:range}}]};
const segment={media_ref:'b'.repeat(64),time_range:source,status:'ready',audio_track:false,clock_uncertainty_seconds:0};
const report=()=>({context_fingerprint:context.fingerprint,simulated:true,camera_id:'SIMULATED-camera',time_range:range,status:'ready',segments:[segment],gaps:[]});
const actualPlan=JSON.parse(process.argv[1]);
const plan=()=>({...actualPlan,plan_id:'c'.repeat(64),context_fingerprint:context.fingerprint,simulated:true,stream_url:'/api/archive/media/'+'c'.repeat(64),start_offset_seconds:30.3,end_offset_seconds:40,duration_seconds:60,clock_uncertainty_seconds:0,previous_keyframe_seconds:30,expires_at:new Date(Date.now()+300000).toISOString()});
let calls=[],pending=null,late=false,foreign=false,tick=null,contextFailure=false,contextWaiting=false,waitingContext=null,unprotected=false;
const response=v=>({ok:true,json:async()=>v});
const fetch=async(path,options={})=>{
 if(path==='/api/archive/context'){
  if(contextFailure)throw Error('SIMULATED context unavailable');
  if(contextWaiting)return await new Promise((resolve,reject)=>{waitingContext=resolve;options.signal?.addEventListener('abort',()=>reject(Error('SIMULATED cancelled')),{once:true});});
  return response(context);
 }
 if(options.method==='POST'){
  const body=JSON.parse(options.body);calls.push([path,body]);
  if(path==='/api/archive/resolve')return response(report());
  if(path==='/api/archive/playback'){
   assert.equal(body.media_ref,'b'.repeat(64));assert.equal(body.camera_id,'SIMULATED-camera');assert(!('path' in body));
   const p=plan();if(foreign)p.stream_url='https://foreign.example/media';if(unprotected)delete p.privacy_preview_generated;
   if(late)return await new Promise(resolve=>pending=()=>resolve(response(p)));
   return response(p);
  }
  if(path==='/api/archive/playback/cancel')return response({status:'revoked'});
  throw Error('Unexpected actuator/model route '+path);
 }
 return response(fixture);
};
const env=vm.createContext({document:{getElementById:id=>nodes.get(id),createElement:tag=>new Element(tag)},fetch,location:{hash:''},history:{replaceState(){}},Date,JSON,URL,AbortController,console,setInterval:fn=>{tick=fn;}});
vm.runInContext(fs.readFileSync('homeplane/static/app.js','utf8'),env);
const settle=()=>new Promise(resolve=>setImmediate(resolve)),all=n=>[n,...n.children.flatMap(all)],content=nodes.get('content');
const query=()=>nodes.get('archive-lookup-form').handlers.submit({preventDefault(){}});
const open=()=>all(content).find(n=>n.attrs['data-archive-playback-ref']).handlers.click();
(async()=>{
 await settle();vm.runInContext("go('Activity')",env);await settle();await query();await open();
 const video=all(content).find(n=>n.tag==='video');assert(video);assert.equal(video.muted,true);assert(!video.attrs.autoplay);assert.equal(video.attrs.src,'/api/archive/media/'+'c'.repeat(64));
 video.handlers.loadedmetadata();assert.equal(video.currentTime,30.3);video.handlers.seeking();video.handlers.seeked();video.frame(0,{mediaTime:30.333});
 assert.match(nodes.get('archive-seek-evidence').textContent,/0.033/);assert.equal(nodes.get('archive-seek-evidence').attrs['data-seek-target'],'30.3');
 video.currentTime=40.2;video.handlers.timeupdate();assert(video.pauseCount>0);assert.equal(video.currentTime,39.999);
 video.handlers.seeking();video.handlers.seeked();assert.match(nodes.get('archive-seek-evidence').textContent,/所选区间结束/);assert(!('data-seek-error' in nodes.get('archive-seek-evidence').attrs),'old frame receipt is cleared during a new seek');
 video.handlers.play();assert.equal(video.currentTime,30.3,'explicit replay returns to the selected start');
 const replaces=content.replaceCount,lookup=nodes.get('archive-lookup');
 vm.runInContext('render()',env);await settle();assert.equal(all(content).find(n=>n.tag==='video'),video,'snapshot rerender retains playback element');assert.equal(content.replaceCount,replaces,'a refresh cannot detach and pause native media');assert.equal(nodes.get('archive-lookup'),lookup);
 nodes.get('archive-end').value='2026-10-01T08:00:39';nodes.get('archive-end').handlers.input();await settle();assert(video.pauseCount>0);assert(!video.attrs.src);assert(calls.some(([p])=>p.endsWith('/cancel')));
 await query();late=true;const job=open();await settle();assert(pending);
 nodes.get('archive-start').value='2026-10-01T08:00:31';nodes.get('archive-start').handlers.input();pending();await job;await settle();
 assert(!all(content).some(n=>n.tag==='video'),'edited input cannot load a late stream');
 late=false;await query();foreign=true;await open();assert(!all(content).some(n=>n.tag==='video'),'foreign stream URLs never load');
 foreign=false;await query();unprotected=true;await open();assert(!all(content).some(n=>n.tag==='video'),'an ungenerated preview cannot create a player');unprotected=false;
 await query();let cancels=calls.filter(([p])=>p.endsWith('/cancel')).length;contextFailure=true;await open();contextFailure=false;
 assert(calls.filter(([p])=>p.endsWith('/cancel')).length>cancels,'a completed preview is revoked if context lookup fails');
 await query();cancels=calls.filter(([p])=>p.endsWith('/cancel')).length;contextWaiting=true;const pendingOpen=open();await settle();assert(waitingContext);
 all(content).find(n=>n.tag==='button'&&n.textContent==='取消').handlers.click();await pendingOpen;contextWaiting=false;
 assert(calls.filter(([p])=>p.endsWith('/cancel')).length>cancels,'cancelling a pending context lookup revokes its completed preview');
 const actualLookup=JSON.parse(process.argv[2]);actualLookup.expected_context=context.fingerprint;actualLookup.camera_id='SIMULATED-camera';
 await env.openEventRecording(content,{request:actualLookup});
 assert(all(content).some(n=>n.tag==='video'),'an actual versioned ArchiveResolveRequest opens protected event playback');
 vm.runInContext('stopArchivePlayer()',env);
 assert(calls.every(([p])=>p.startsWith('/api/archive/')),'viewing and stopping only touches readonly archive');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(
        ["node", "-e", script, json.dumps(actual_plan), json.dumps(actual_lookup)],
        cwd=Path(__file__).parents[1], text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
