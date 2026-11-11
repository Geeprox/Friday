const fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const source=fs.readFileSync('homeplane/static/werewolf-host.js','utf8');
const extract=(start,end)=>source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));

test('same-phase effects refresh without rebuilding seats or clearing a confirmation',async()=>{
 let mutations=0,text='Friday 语音主持已开启';
 const voice={get textContent(){return text;},set textContent(value){text=value;mutations++;}},recovery={},restore={},timer={};
 const nodes={'[data-voice-state]':voice,'[data-light-recovery]':recovery,'[data-restore-lights]':restore,'[data-timer]':timer};
 const original={active:true,session:'synthetic',game:{revision:4,phase:'wolves',paused:false,seconds_left:20,players:[]},camera_hints:[],camera_candidates:[],effects:{narration:true,voice:'requested',lights:{recovery_pending:false}}};
 const next=structuredClone(original);next.effects.voice='unconfirmed';next.effects.lights.recovery_pending=true;
 const scope=vm.createContext({current:original,alive:true,busy:false,context:{},generation:0,readRequest:null,lobbyUI:null,roundCamera:null,syncFailed:false,syncNotice:'对局服务暂时无法连接，正在重试。',AbortController,confirmation:'close',notice:{},content:{querySelector:selector=>nodes[selector],querySelectorAll:()=>[]},api:async()=>next,render:()=>assert.fail('a status change must not rebuild the board or dismiss confirmation')});
 vm.runInContext(extract('function updateEffects()', '\n function render()'),scope);
 vm.runInContext(extract('function roundCameraKey()', '\n function closeRoundPanel('),scope);
 vm.runInContext(extract('function updateRoundCamera()', '\n function updateLobby()'),scope);
 vm.runInContext(extract('function updateSpeakerControls()', '\n function live()'),scope);
 scope.revision=scope.renderKey();
 vm.runInContext(extract('function updateIdentityProgress()', '\n content.append('),scope);
 await scope.refresh();
 assert.equal(voice.textContent,'播报暂不可用，屏幕主持继续');assert.equal(recovery.hidden,false);assert.equal(restore.hidden,true);
 assert.equal(scope.confirmation,'close');assert.equal(timer.textContent,'20 秒');
 await scope.refresh();assert.equal(mutations,1,'stable status is not repeatedly replaced');
 next.game.phase='finished';scope.updateEffects();assert.equal(restore.hidden,false);
 next.effects.lights.recovery_pending=false;scope.updateEffects();assert.equal(recovery.hidden,true);
});
