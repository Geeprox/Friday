"use strict";
const $=id=>document.getElementById(id),fragment=new URLSearchParams(location.hash.slice(1));
const names=["汽水猫","夜航兔","慢慢鲸","摇摆熊","月球旅人","小幽灵","唱片君","星星糖","柠檬汽水","仙人掌","北极企鹅","节拍小子"];
const themes=[["content","内容与留白","安静的影像，给歌声留白"],["festival","音乐节","鲜明图形，今晚放开唱"],["live","Live","把客厅交给现场"]];
let room=fragment.get("r"),key=fragment.get("k"),identity=null,songs=[],selected=[],mode="home",online=false,micAvailable=true,theme="content",refreshTimer,refreshing=false,chosenAvatar=0,queueRevision=0,playback={online:false,current:"",played:[],state:"idle",mic_muted:true},controls={music_gain:70,mic_gain:50,mic_muted:1,vocal_preset:"dry",sfx_gain:60,control_revision:0},drag=null,ordering=false,actionSong=null,commandBusy=false,pendingControls={},source="song",searchSequence=0,searchOffset=0,searchMore=false,searchBusy=false,searchQuery="",playlistId="",playlistTitle="",recommendations=null,playlistPage=0;
try{if(room&&key){sessionStorage.setItem("friday-ktv-room",JSON.stringify({room,key}));history.replaceState(null,"",location.pathname);}else({room,key}=JSON.parse(sessionStorage.getItem("friday-ktv-room")||"{}"));identity=JSON.parse(sessionStorage.getItem(`friday-ktv-guest:${room}`)||"null");}catch{}
const valid=typeof room==="string"&&/^[a-f0-9-]{36}$/.test(room)&&typeof key==="string"&&/^[A-Za-z0-9_-]{32,80}$/.test(key);
const pendingAdds=new Set();
let addQueue=null,addSnapshot={items:[],stopped:"",persistence:"memory"},addViewSignature="",statusFailures=0,lastStatusSuccess=0,joining=false,joinTimer,statusFeed=null;
const acousticModule=import("./sync.mjs");acousticModule.catch(()=>{});
const pendingActions=new Map();
function busy(node,active,label="正在处理…"){
 if(!node)return;node.classList.toggle("is-busy",active);node.setAttribute("aria-busy",String(active));
 if(active)pendingActions.set(node,label);else pendingActions.delete(node);
 const progress=$("action-progress");if(active){const dialog=node.closest("dialog")||document.querySelector("dialog[open]"),host=dialog?.id==="sound-dialog"?dialog.querySelector(".sheet-heading"):dialog||document.body;if(progress.parentElement!==host){if(dialog?.id==="sound-dialog")host.insertBefore(progress,host.querySelector("button"));else host.append(progress);}}
 for(const current of pendingActions.keys())if(!current.isConnected)pendingActions.delete(current);
 const labels=[...pendingActions.values()];$("action-progress").hidden=!labels.length;$("action-progress-text").textContent=labels[labels.length-1]||"";
}
async function waitForTV(check,signal){const end=performance.now()+9000;while(performance.now()<end){if(signal?.aborted)throw Object.assign(new Error(),{code:"cancelled"});await refresh();if(signal?.aborted)throw Object.assign(new Error(),{code:"cancelled"});if(check())return;await new Promise(resolve=>setTimeout(resolve,125));}throw Object.assign(new Error(),{code:"tv_slow"});}
const earClock=new FridayGuideClock();let earEnabled=false,earCanPlay=false,earSong="",earLoading=false,earReported=0,earPhase="off";const ear=new FridayEarMonitor(earClock,state=>{earPhase=state;reportEar(state);if(state==="suspended"){invalidateGuide({disable:true});updateGuide();$("guide-status").textContent="伴唱已暂停，点开启继续。";return;}$("guide-status").textContent=(state==="start"||state==="loading")?"正在加载原唱…":state==="playing"?"已跟随电视": "";});
const sourceOf=s=>["cloud","mv"].includes(s.source)?s.source:"song",songKey=s=>`${sourceOf(s)}:${s.song_id}`;
const alreadyQueued=s=>selected.some(v=>songKey(v)===songKey(s)&&v.state!=="cancelled"&&!playback.played.includes(v.id));
const labels={queued:"等待准备",acquiring:"下载中",separating:"制作伴奏",transcoding:"即将就绪",ready:"已就绪",failed:"准备失败",cancelled:"已取消"};
function message(text){$("message").textContent=text;}
function toast(text){$("toast").textContent=text;$("toast").classList.add("show");setTimeout(()=>$("toast").classList.remove("show"),2400);}
function avatar(node,id){const i=Number.isInteger(id)&&id>=0&&id<12?id:0;node.style.backgroundPosition=`${i%4*100/3}% ${Math.floor(i/4)*50}%`;}
function saveIdentity(){sessionStorage.setItem(`friday-ktv-guest:${room}`,JSON.stringify(identity));$("my-name").textContent=identity.nickname;avatar($("my-avatar"),identity.avatar);$("profile-button").setAttribute("aria-label",`${identity.nickname}，修改昵称和头像`);}
function connectionError(error){return ["network_error","request_timeout","service_unavailable"].includes(error?.code)||(!error?.code&&error?.status>=500);}
function connectionNotice(text){const node=$("connection-status");node.textContent=text;node.hidden=!text;}
async function api(operation,body,query="",{signal}={}){
 const controller=new AbortController(),abort=()=>controller.abort();let timedOut=false;
 const timeout=setTimeout(()=>{timedOut=true;controller.abort();},15000);signal?.addEventListener("abort",abort,{once:true});if(signal?.aborted)abort();
 try{
  const started=performance.now();let response;
  try{response=await fetch(`./api/rooms/${room}/${operation}${query}`,{method:body?"POST":"GET",headers:{"X-KTV-Room":key,...(identity?.key?{"X-KTV-Guest":identity.key}:{}),...(body?{"Content-Type":"application/json"}:{})},body:body?JSON.stringify(body):undefined,credentials:"omit",signal:controller.signal,cache:"no-store"});}
  catch(error){throw Object.assign(new Error(),{code:signal?.aborted?"cancelled":timedOut?"request_timeout":"network_error",status:0});}
  let value;try{value=await response.json();}catch{throw Object.assign(new Error(),{code:signal?.aborted?"cancelled":timedOut?"request_timeout":"service_unavailable",status:response.status});}if(signal?.aborted)throw Object.assign(new Error(),{code:"cancelled"});
  if(!response.ok){const retry=response.headers?.get("Retry-After"),seconds=Number(retry),retryAfterMs=retry?(Number.isFinite(seconds)?Math.max(0,seconds*1000):Math.max(0,Date.parse(retry)-Date.now())):0;throw Object.assign(new Error(),{code:value.code||"service_unavailable",status:response.status,retryAfterMs});}
  if(operation==="status"&&value.server_time_ms&&!earClock.observe(value,started,performance.now()))throw Object.assign(new Error(),{code:"stale_status"});
  return value;
 }finally{clearTimeout(timeout);signal?.removeEventListener("abort",abort);}
}
function failure(error){if(["room_closed","room_unavailable","guest_unavailable"].includes(error.code))addQueue?.stop(error.code);if(connectionError(error)){connectionNotice("连接暂时不稳定，正在重新连接。");return;}const text={room_closed:"本场已经结束，扫描电视上的新二维码即可加入。",room_unavailable:"二维码不可用，重新扫描电视上的二维码。",guest_unavailable:"加入信息已失效，重新扫描电视二维码即可。",guest_capacity:"本场加入人数已满，请联系房主。",mac_offline:"房间暂时离线，稍后再点。",queue_full:"歌单有些满，稍后再来。",rate_limited:"操作有些频繁，稍等一下。",search_unavailable:"搜索暂时不可用，稍后再试。",cloud_song_unavailable:"这首已不在云盘中，刷新搜索后再试。",retry_limit:"暂时仍无法下载，换个版本试试。",retry_unavailable:"歌曲状态已变化，请刷新歌单。",microphone_update_required:"电视正在更新，请稍后开启麦克风。",tv_offline:"电视暂时未连接，打开电视上的 Friday KTV 后再试。",tv_slow:"电视暂未响应，操作结果可能稍后生效，请先查看播放状态。",song_not_ready:"这首还在准备，准备好后再开始唱。",queue_changed:"歌单刚有更新，已刷新后可重新调整。",playback_changed:"电视已经换歌，已刷新当前状态。"}[error.code]||"操作未完成，请重试。";message(text);const alert=document.querySelector("dialog[open] [role=alert]");if(alert)alert.textContent=text;if(error.code==="room_closed"||error.code==="room_unavailable"){$("search").hidden=true;$("player").hidden=true;clearInterval(refreshTimer);statusFeed?.close();sessionStorage.removeItem("friday-ktv-room");}}
function applyTheme(value){if(!themes.some(t=>t[0]===value))return;theme=value;document.body.dataset.theme=value;document.querySelectorAll(".theme-option").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.theme===theme)));}
function coverFor(song){if(typeof song.artwork==="string"&&/^https:\/\/[a-z0-9.-]+\.music\.126\.net\//.test(song.artwork)){const img=document.createElement("img");img.className="cover";img.alt="";img.src=song.artwork;img.loading="lazy";img.referrerPolicy="no-referrer";img.onerror=()=>img.replaceWith(coverFor({...song,artwork:""}));return img;}const cover=document.createElement("div");cover.className="cover placeholder";cover.setAttribute("aria-hidden","true");cover.textContent="♪";return cover;}
const resultRowCache=new Map(),resultGroupCache=new Map();
function reconcileChildren(parent,desired){
 const keep=new Set(desired);
 for(const child of Array.from(parent.children))if(!keep.has(child))parent.removeChild(child);
 for(let i=0;i<desired.length;i++)if(parent.children[i]!==desired[i])parent.insertBefore(desired[i],parent.children[i]||null);
}
function render(){tabs();for(const [node]of pendingActions)if(!node.isConnected)busy(node,false);const home=mode==="home";$("discover").hidden=!home;document.querySelector(".list-heading").hidden=home;$("load-more").hidden=mode!=="find"||!searchMore;const rows=home?[]:mode==="find"?songs:[...selected].sort((a,b)=>queueRank(a)-queueRank(b)),container=$("results"),focused=document.activeElement?.dataset.focusId||document.activeElement?.dataset.song;const historyOpen=container.querySelector("details.history")?.open||false;const rootRows=[],groupRows=new Map(),usedRows=new Set(),usedGroups=new Set();$("list-title").textContent=mode==="find"?(playlistTitle||(songs.length?"找到喜欢的版本":"选一首喜欢的")):"今晚的歌单";$("count").textContent=rows.length?`${rows.length} 首`:"";$("empty").hidden=home||rows.length>0;$("empty").textContent=mode==="find"?"搜一首你想唱的歌。":"还没有已点歌曲，第一首交给你。";
 const nextQueuedId=selected.find(s=>s.id!==playback.current&&!playback.played.includes(s.id)&&s.state!=="cancelled")?.id;let queueGroup=null,lastQueueGroup="";rows.forEach(song=>{if(mode==="queue"){const group=song.id===playback.current?"current":playback.played.includes(song.id)||song.state==="cancelled"?"history":"pending";if(group!==lastQueueGroup){queueGroup=resultGroupCache.get(group);if(!queueGroup){queueGroup=document.createElement(group==="history"?"details":"section");queueGroup.className=`queue-group ${group}`;if(group==="history")queueGroup.open=historyOpen;resultGroupCache.set(group,queueGroup);}usedGroups.add(group);const heading=queueGroup.children[0]||document.createElement(group==="history"?"summary":"h3");heading.textContent=({current:"正在唱",pending:"接下来",history:"唱过了"})[group];const count=document.createElement("span");count.textContent=String(rows.filter(s=>(s.id===playback.current?"current":playback.played.includes(s.id)||s.state==="cancelled"?"history":"pending")===group).length);heading.append(count);groupRows.set(queueGroup,[heading]);rootRows.push(queueGroup);lastQueueGroup=group;}}
 const rowKey=mode==="queue"?`queue:${song.id}`:`find:${songKey(song)}`,signature=JSON.stringify([song,mode,song.id===playback.current?playback.state:"",playback.played.includes(song.id),song.id===nextQueuedId]);usedRows.add(rowKey);
 const previousRow=resultRowCache.get(rowKey);if(previousRow?.signature===signature){if(mode==="queue")groupRows.get(queueGroup).push(previousRow.row);else rootRows.push(previousRow.row);return;}
 const row=document.createElement("article");row.className="song";row.dataset.renderKey=rowKey;if(mode==="queue"){row.classList.add("queue-song");row.dataset.queue=song.id;if(song.id===playback.current)row.classList.add("current");}row.append(coverFor(song));const details=document.createElement("div");details.className="details";const title=document.createElement("div");title.className="song-title";title.textContent=song.title||`歌曲 ${song.song_id}`;details.append(title);if(sourceOf(song)!=="song"){const badge=document.createElement("span");badge.className="source-badge";badge.textContent=sourceOf(song)==="cloud"?"云盘":"MV";details.append(badge);}const artist=document.createElement("div");artist.className="artist";artist.textContent=(song.artists||[]).join(" / ");if(artist.textContent)details.append(artist);
 if(mode==="queue"){const by=document.createElement("div");by.className="requester";const picture=document.createElement("span");picture.className="avatar";picture.setAttribute("aria-hidden","true");avatar(picture,song.avatar);const label=document.createElement("span");label.textContent=`${song.nickname||"朋友"} 点的`;by.append(picture,label);details.append(by);if(!["ready","cancelled"].includes(song.state)){const process=document.createElement("div");process.className="preparation";const p=song.progress||{},steps=["原曲","伴奏","播放文件","MV"];const label=document.createElement("p");label.className="prepare-label";const stage={queued:"等待准备",acquiring:"获取原曲",waiting_separation:"等待制作伴奏",waiting_memory:"等待制作伴奏",separating:"正在生成伴奏",waiting_transcode:"伴奏已完成",transcoding:"准备播放文件",video:"匹配 MV"}[p.stage]||labels[song.state];label.textContent=song.state==="failed"?(song.code==="download_timeout"?"原曲下载未完成，请重试。":({source_login_required:"音源尚未连接，请联系房主。",source_rights_denied:"当前账户无法下载这个版本，请换一个版本。",source_api_unavailable:"音源服务暂时不可用，请稍后重试。",source_file_invalid:"这首暂时无法播放，请重试或换个版本。"}[song.code]||"本次准备未完成，可重试或换一个版本。")):stage;process.append(label);const rail=document.createElement("div");rail.className="prepare-steps";rail.setAttribute("aria-label",`处理步骤：${stage}`);steps.forEach((name,i)=>{const step=document.createElement("span");step.textContent=name;step.className=i<(p.completed_steps||0)?"complete":i===(p.completed_steps||0)?"active":"";rail.append(step);});process.append(rail);if(p.source_total>0&&p.source_phase==="source_downloading"){const progress=document.createElement("progress");progress.max=p.source_total;progress.value=Math.min(p.source_bytes||0,p.source_total);progress.setAttribute("aria-label","原曲下载进度");process.append(progress);const bytes=document.createElement("p");bytes.className="prepare-wait";bytes.textContent=`已下载 ${Math.floor(progress.value/p.source_total*100)}%`;process.append(bytes);}details.append(process);}}row.append(details);
 if(mode==="find"){const button=document.createElement("button");button.className="add";button.dataset.song=songKey(song);button.dataset.title=song.title;button.onclick=()=>add(song,button);row.append(button);}else{const state=document.createElement("span");state.className="state";state.textContent=song.id===playback.current?(playback.state==="playing"?"正在唱":playback.state==="preparing"?"加载中":"已暂停"):playback.played.includes(song.id)?"唱过了" :song.lyrics_pending?"等待歌词":(song.state==="failed"?({download_timeout:"下载超时",download_unavailable:"暂无法下载"}[song.code]||"准备失败"):labels[song.state]||"准备中");if(song.id===nextQueuedId&&!playback.played.includes(song.id))state.textContent="下一首 · "+state.textContent;row.append(state);const tools=document.createElement("div");tools.className="queue-tools";if(song.state==="failed"){const retry=document.createElement("button");retry.className="retry-song";retry.textContent="重试";retry.setAttribute("aria-label",`重新准备：${song.title}`);retry.onclick=async()=>{retry.disabled=true;busy(retry,true,"正在重新准备…");try{await api("retry",{id:song.id,request_id:crypto.randomUUID()});toast("正在重新准备");await refresh();}catch(error){failure(error);}finally{retry.disabled=false;busy(retry,false);}};tools.append(retry);}if(playback.played.includes(song.id)||song.state==="cancelled"){const repeat=document.createElement("button");repeat.className="repeat-song";repeat.dataset.song=songKey(song);repeat.dataset.title=song.title;repeat.textContent=alreadyQueued(song)?"已加入":"再唱一遍";repeat.disabled=alreadyQueued(song)||pendingAdds.has(songKey(song))||!online;repeat.setAttribute("aria-label",`再唱一遍：${song.title}`);repeat.onclick=()=>add(song,repeat);tools.append(repeat);}const more=document.createElement("button");more.className="more-song";more.textContent="···";more.setAttribute("aria-label",`调整歌曲：${song.title}`);more.onclick=()=>songActions(song);if(!playback.played.includes(song.id)&&song.state!=="cancelled")tools.append(more);if(song.id!==playback.current&&!playback.played.includes(song.id)&&!["failed","cancelled"].includes(song.state)){const handle=document.createElement("button");handle.className="reorder-handle";handle.textContent="≡";handle.setAttribute("aria-label",`拖动排序：${song.title}，也可按确定打开排序操作`);handle.onclick=()=>{if(!handle.dataset.moved)songActions(song);delete handle.dataset.moved;};handle.onpointerdown=event=>startDrag(event,song,handle);tools.append(handle);}row.append(tools);}for(const b of row.querySelectorAll("button"))b.dataset.focusId=song.id?`${song.id}:${b.className.split(" ")[0]}`:b.dataset.song;resultRowCache.set(rowKey,{row,signature});if(mode==="queue")groupRows.get(queueGroup).push(row);else rootRows.push(row);});
 for(const [group,nodes]of groupRows)reconcileChildren(group,nodes);reconcileChildren(container,rootRows);
 for(const key of resultRowCache.keys())if(!usedRows.has(key))resultRowCache.delete(key);for(const key of resultGroupCache.keys())if(!usedGroups.has(key))resultGroupCache.delete(key);
 for(const [node]of pendingActions)if(!node.isConnected)busy(node,false);updateAddButtons();if(focused){const button=Array.from(container.querySelectorAll("button")).find(b=>(b.dataset.focusId||b.dataset.song)===focused);button?.focus({preventScroll:true});}
}
function updateAddButtons(){for(const b of document.querySelectorAll(".add,.repeat-song")){
 const item=addSnapshot.items.find(v=>v.key===b.dataset.song),added=selected.some(s=>songKey(s)===b.dataset.song&&s.state!=="cancelled"&&!playback.played.includes(s.id))||(!item&&addQueue?.has(b.dataset.song)),pending=!!item&&item.state!=="failed",repeat=b.classList.contains("repeat-song");
 b.textContent=repeat?(added?"已加入":item?.state==="failed"?"重试":pending?"等待加入":"再唱一遍"):(added?"✓":item?.state==="failed"?"↻":pending?"…":"+");
 b.classList.toggle("is-added",!!added);b.setAttribute("aria-label",added?`已点：${b.dataset.title}`:item?`${FridayAddQueue.message(item)}：${b.dataset.title}`:`点歌：${b.dataset.title}`);
 b.disabled=!!added||pending||!identity||!!addSnapshot.stopped;busy(b,item?.state==="sending","正在加入歌单…");
}}
function renderPendingAdds(){
 const node=$("pending-adds"),signature=JSON.stringify(addSnapshot);node.hidden=!addSnapshot.items.length;
 if(signature===addViewSignature)return;addViewSignature=signature;
 $("pending-adds-title").textContent=`待加入 ${addSnapshot.items.length} 首`;
 $("pending-adds-note").textContent=addSnapshot.stopped?"本次连接已失效，请重新扫描电视二维码。":addSnapshot.persistence==="memory"?"已暂存在当前页面，加入完成前请保持页面打开。":"暂存在本机，连接恢复后按点歌顺序加入。";
 const list=$("pending-adds-list");list.replaceChildren();
 for(const item of addSnapshot.items){const row=document.createElement("li"),copy=document.createElement("div"),title=document.createElement("strong"),state=document.createElement("span");title.textContent=item.song.title;state.textContent=FridayAddQueue.message(item);copy.append(title,state);row.append(copy);
  if(item.state==="failed"&&!addSnapshot.stopped){const retry=document.createElement("button");retry.type="button";retry.textContent="重试";retry.setAttribute("aria-label",`重新加入：${item.song.title}`);retry.onclick=()=>addQueue?.retry(item.key);row.append(retry);}list.append(row);
 }
}
function initAddQueue(){
 if(!identity?.id)return;
 if(addQueue&&!addQueue.closed&&addQueue.roomId===room&&addQueue.guestId===identity.id)return;
 addQueue?.close();addQueue=new FridayAddQueue.AddQueue({roomId:room,guestId:identity.id,send:(payload,{signal})=>api("songs",payload,"",{signal}),
  onChange:value=>{addSnapshot=value;pendingAdds.clear();for(const item of value.items)pendingAdds.add(item.key);renderPendingAdds();updateAddButtons();},
  onAccepted:({song,receipt})=>{if(!selected.some(s=>s.id===receipt.id)){const found=songs.find(s=>songKey(s)===songKey(song));selected.push({...found,...song,id:receipt.id,state:receipt.state});}updateAddButtons();tabs();toast(receipt.duplicate?"这首已在歌单里":"已加入，等你开唱");refresh();}
 });addQueue.observe({songs:selected,played:playback.played});addQueue.start();
}
function add(song,button){
 if(!identity||alreadyQueued(song))return;
 try{initAddQueue();const item=addSnapshot.items.find(v=>v.key===songKey(song));if(item?.state==="failed")addQueue.retry(item.key);else addQueue.enqueue({...song,source:sourceOf(song)});if(!online||statusFailures)toast("已暂存，连接恢复后加入");}
 catch(error){if(error.code==="local_queue_full")message("待加入的歌曲已满，等前面的歌曲加入后再点。");else failure(error);}
}
function readinessHint(value){
 const ready=value.readiness,g=ready?.gates;
 if(!g||ready.state==="closed"||ready.state==="unavailable")return value.mac_online?"":"歌曲准备服务暂时离线，歌单会保留。";
 if(ready.ready===true)return "";
 if(g.link_tv===false)return "电视暂未连接，歌单会保留。";
 if(["tv_playback","tv_audio","tv_media"].some(k=>g[k]===false))return "电视正在准备，准备好后可开始唱。";
 if(["link_worker","worker_source","worker_model","worker_download","worker_lyrics","worker_media"].some(k=>g[k]===false))return "歌曲准备服务正在连接，歌单会保留。";
 return "正在准备本场，已点歌单会保留。";
}
function applyStatus(value){
 statusFailures=0;lastStatusSuccess=performance.now();connectionNotice("");online=value.mac_online;micAvailable=value.microphone_available!==false;
 const changed=JSON.stringify(selected)!==JSON.stringify(value.songs);if(!drag&&!ordering){selected=value.songs;queueRevision=value.queue_revision||0;}
 playback=value.playback||playback;controls={...controls,...(value.controls||{})};addQueue?.observe({songs:value.songs,played:playback.played});addQueue?.wake();
 applyTheme(value.theme);updatePlayer();tabs();$("hint").textContent=readinessHint(value);
 if(mode==="queue"&&(changed||playbackSignature!==queuePlaybackSignature())&&!drag&&!ordering){playbackSignature=queuePlaybackSignature();render();}else updateAddButtons();
}
function statusFailure(error){
 if(error.code==="stale_status"||error.code==="cancelled")return;
 statusFailures++;
 if(statusFailures>=3||performance.now()-lastStatusSuccess>6000){online=false;playback={...playback,online:false};updatePlayer();}
 if(connectionError(error)||error.code==="rate_limited")connectionNotice("连接暂时不稳定，正在重新连接。已点歌曲会保留。");else failure(error);
 updateAddButtons();
}
async function refresh({signal}={}){
 if(!valid||document.hidden||refreshing)return;refreshing=true;let received=false;
 try{const value=await api("status",null,"",{signal});received=true;applyStatus(value);}
 catch(error){if(received)message("页面状态未更新，请刷新重试。");else statusFailure(error);}
 finally{refreshing=false;}
}
function startStatusFeed(){
 if(!identity||document.hidden)return;
 if(typeof FridayStatusFeed!=="function"){clearInterval(refreshTimer);refreshTimer=setInterval(refresh,1000);return;}
 if(!statusFeed)statusFeed=new FridayStatusFeed({
  events:({cursor,signal})=>api("events",null,`?after=${cursor}&wait=5`,{signal}),probe:options=>refresh(options),visible:()=>!document.hidden,
  onStatus:value=>{const now=performance.now();if(earClock.observe(value,now,now,{calibrate:false}))applyStatus(value);},onFailure:statusFailure
 });
 statusFeed.start();
}
let searchController=null,searchTimer=null,searchComposing=false;
function resetSearchBusy(){searchBusy=false;const b=$("search").querySelector("button");b.disabled=false;$("load-more").disabled=false;busy(b,false);busy($("load-more"),false);}
function cancelSearchInput(){const wasBusy=searchBusy;clearTimeout(searchTimer);searchTimer=null;searchController?.abort();searchController=null;searchSequence++;resetSearchBusy();if(wasBusy&&$("message").textContent==="正在找歌…")message("");}
function scheduleSearch(){cancelSearchInput();if(searchComposing||mode!=="find"||document.hidden)return;searchTimer=setTimeout(()=>{searchTimer=null;findSongs();},250);}
async function findSongs(more=false,collection=null){
 if(more&&searchBusy)return;
 if(!more){cancelSearchInput();playlistId=collection?.id||"";playlistTitle=collection?.title||"";}
 const q=more?searchQuery:$("query").value.trim();
 if(!q&&source!=="cloud"&&!playlistId){searchQuery="";searchMore=false;songs=[];message("");render();return;}
 const sequence=searchSequence,offset=more?searchOffset:0,button=$("search").querySelector("button"),requestedSource=source,requestedPlaylist=playlistId,controller=new AbortController();searchController=controller;
 searchBusy=true;button.disabled=true;busy(more?$("load-more"):button,true,"正在找歌…");$("load-more").disabled=true;$("load-more").textContent="加载中…";message("正在找歌…");
 try{
  const query=requestedPlaylist?`?id=${requestedPlaylist}&offset=${offset}`:`?q=${encodeURIComponent(q)}&source=${requestedSource}&offset=${offset}`;
  const value=await api(requestedPlaylist?"playlist":"search",null,query,{signal:controller.signal});
  if(controller.signal.aborted||sequence!==searchSequence||source!==requestedSource||playlistId!==requestedPlaylist)return;
  const previous=more?songs:[];songs=[...previous,...value.songs.filter(s=>!previous.some(p=>songKey(p)===songKey(s)))];searchQuery=q;searchOffset=value.next_offset||offset+30;searchMore=value.has_more===true;tabs();
  if(mode==="find"){render();message(songs.length?"":requestedSource==="cloud"&&!value.updated?"云盘正在连接，稍后再试。":"没有找到，换个歌名或歌手试试。");}
 }catch(error){if(!controller.signal.aborted&&sequence===searchSequence&&mode==="find")failure(error);}
 finally{if(searchController===controller)searchController=null;if(sequence===searchSequence){resetSearchBusy();$("load-more").textContent="加载更多";}}
}
function searchArtist(name){setView("find");source="song";$("query").value=name;updateSources();findSongs();}
function updateSources(){$("query").placeholder=source==="cloud"?"搜索云盘歌曲或歌手":source==="mv"?"搜索 MV 或歌手":"搜索歌曲或歌手";for(const [button,kind]of[["source-song","song"],["source-mv","mv"],["source-cloud","cloud"]])$(button).setAttribute("aria-pressed",String(source===kind));}
async function loadDiscover(){busy($("refresh-discover"),true,"正在加载推荐…");try{const value=await api("discover");recommendations=value;$("artist-picker").replaceChildren();for(const name of value.artists){const b=document.createElement("button");b.type="button";const portrait=document.createElement("span");portrait.className="artist-portrait";portrait.setAttribute("aria-hidden","true");portrait.textContent=name.slice(0,1);const image=value.artist_photos?.[name];if(image&&/^https:\/\/[a-z0-9.-]+\.music\.126\.net\//.test(image)){const img=document.createElement("img");img.src=image;img.alt="";img.loading="lazy";img.referrerPolicy="no-referrer";img.onerror=()=>img.remove();portrait.append(img);}const label=document.createElement("span");label.textContent=name;b.append(portrait,label);b.onclick=()=>searchArtist(name);$("artist-picker").append(b);}renderPlaylists();$("discover-status").textContent=value.playlists.length?"":"歌单暂时未加载，点歌手或直接搜索也能点歌。";}catch{$("discover-status").textContent="推荐暂时未加载，稍后重试。";}finally{busy($("refresh-discover"),false);}}
function renderPlaylists(){const picker=$("playlist-picker");picker.replaceChildren();for(const list of recommendations.playlists.slice(playlistPage*4,playlistPage*4+4)){const b=document.createElement("button");b.type="button";b.className="playlist-card";b.append(coverFor(list));const title=document.createElement("strong");title.textContent=list.title;b.append(title);b.onclick=()=>{setView("find");source="song";updateSources();$("query").value="";findSongs(false,list);};picker.append(b);}}
$("refresh-discover").onclick=()=>{if(!recommendations)return loadDiscover();playlistPage=(playlistPage+1)%Math.max(1,Math.ceil(recommendations.playlists.length/4));renderPlaylists();};
$("load-more").onclick=()=>findSongs(true);
$("search").addEventListener("submit",event=>{event.preventDefault();if(!searchComposing&&!event.isComposing)findSongs();});
$("query").oncompositionstart=()=>{searchComposing=true;cancelSearchInput();};
$("query").oncompositionend=()=>{searchComposing=false;scheduleSearch();};
$("query").oninput=event=>{if(event?.isComposing){searchComposing=true;cancelSearchInput();}else scheduleSearch();};
for(const [id,value]of[["source-song","song"],["source-mv","mv"],["source-cloud","cloud"]])$(id).onclick=()=>{source=value;songs=[];playlistId="";playlistTitle="";searchQuery="";searchMore=false;searchSequence++;for(const [button,kind]of[["source-song","song"],["source-mv","mv"],["source-cloud","cloud"]])$(button).setAttribute("aria-pressed",String(source===kind));$("query").placeholder=source==="cloud"?"搜索云盘歌曲或歌手":source==="mv"?"搜索 MV 或歌手":"搜索歌曲或歌手";render();findSongs();};
const viewScroll={home:0,find:0,queue:0};
function tabs(){
 const count=selected.filter(s=>s.state!=="cancelled"&&!playback.played.includes(s.id)).length;
 document.body.dataset.view=mode;
 $("search-entry").hidden=!identity||mode!=="home";
 $("search").hidden=!identity||mode!=="find";$("source-picker").hidden=!identity||mode!=="find";
 $("search-queue-link").hidden=!identity||mode!=="find"||!count;$("search-queue-count").textContent=`已点 ${count} 首`;
 $("queue-count").textContent=count;$("queue-tab").disabled=!identity;
 $("page-heading").hidden=mode==="home";$("page-title").textContent=mode==="queue"?"已点歌曲":"搜索";
}
function setView(next,{push=true,focus}={}){
 if(!["home","find","queue"].includes(next)||mode===next)return;
 const previous=mode;viewScroll[mode]=window.scrollY;mode=next;if(previous==="find"&&next!=="find"){searchComposing=false;cancelSearchInput();}
 if(push)history.pushState({ktvView:next},"");
 message("");tabs();render();
 const focusId=focus||(next==="home"?(previous==="queue"?"queue-tab":"search-entry"):"page-title");
 // Focus in the gesture task so opening search keeps the mobile keyboard active.
 $(focusId).focus({preventScroll:true});
 requestAnimationFrame(()=>{if(mode===next)window.scrollTo({top:viewScroll[next],behavior:"instant"});});
 if(next==="queue")refresh();else if(next==="find"&&!playlistId&&$("query").value.trim()!==searchQuery)scheduleSearch();
}
history.replaceState({ktvView:"home"},"");
window.addEventListener("popstate",event=>setView(event.state?.ktvView||"home",{push:false}));
$("page-back").onclick=()=>{if(history.state?.ktvView&&mode!=="home")history.back();else setView("home",{push:false});};
$("search-entry").onclick=()=>setView("find",{focus:"query"});
document.querySelector(".brand").onclick=event=>{event.preventDefault();setView("home");};
$("queue-tab").onclick=()=>setView("queue");$("view-queue").onclick=()=>setView("queue");
function openDialog(id){const dialog=$(id);if(dialog.showModal)dialog.showModal();else dialog.setAttribute("open","");}
function chooseAvatar(id){chosenAvatar=id;document.querySelectorAll(".avatar-choice").forEach((b,i)=>b.setAttribute("aria-pressed",String(i===id)));}
for(let i=0;i<12;i++){const b=document.createElement("button");b.type="button";b.className="avatar-choice";b.setAttribute("aria-label",`选择${names[i]}头像`);b.setAttribute("aria-pressed","false");const picture=document.createElement("span");picture.className="avatar";picture.setAttribute("aria-hidden","true");avatar(picture,i);b.append(picture);b.onclick=()=>chooseAvatar(i);$("avatar-picker").append(b);}
$("profile-button").onclick=()=>{if(!identity)return;$("nickname").value=identity.nickname;chooseAvatar(identity.avatar);$("profile-error").textContent="";openDialog("profile-dialog");};
$("random-profile").onclick=()=>{const id=crypto.getRandomValues(new Uint32Array(1))[0]%12;chooseAvatar(id);$("nickname").value=names[id];};
$("save-profile").onclick=async()=>{const name=$("nickname").value.trim();if(!name){$("profile-error").textContent="起一个昵称再保存。";$("nickname").focus();return;}const b=$("save-profile");b.disabled=true;busy(b,true,"正在保存…");try{const result=await api("profile",{nickname:name,avatar:chosenAvatar});identity={...identity,...result};saveIdentity();$("profile-dialog").close();toast("就用这个名字，开唱吧");await refresh();}catch{$("profile-error").textContent="还没保存，检查连接后再试。";}finally{b.disabled=false;busy(b,false);}};
for(const [value,title,description]of themes){const button=document.createElement("button");button.type="button";button.className="theme-option";button.dataset.theme=value;button.setAttribute("aria-pressed",String(theme===value));const preview=document.createElement("img");preview.className="theme-preview";preview.src=`./${value}.png?v=3`;preview.alt="";const text=document.createElement("div");text.className="theme-copy";const heading=document.createElement("strong");heading.textContent=title;const detail=document.createElement("span");detail.textContent=description;text.append(heading,detail);button.append(preview,text);button.onclick=async()=>{if([...document.querySelectorAll(".theme-option")].some(b=>b.getAttribute("aria-busy")==="true"))return;button.disabled=true;busy(button,true,"正在切换风格…");try{await api("theme",{theme:value});applyTheme(value);$("theme-error").textContent="";toast("本场风格已更新");}catch{$("theme-error").textContent="还没切换，检查连接后再试。";}finally{button.disabled=false;busy(button,false);}};$("theme-picker").append(button);}
$("settings-button").onclick=()=>{if(!identity)return;$("theme-error").textContent="";openDialog("settings-dialog");};
// The remembered nonzero level belongs to this room and survives a page reload.
// Storage may be unavailable in an embedded browser; the in-memory value still works.
class RoomMusicVolume {
 constructor(roomId,storage){this.storage=storage;this.key=`friday-ktv-music-level:${roomId}`;this.level=70;try{const saved=Number(storage.getItem(this.key));if(Number.isInteger(saved)&&saved>0&&saved<=100)this.level=saved;}catch{}}
 remember(level){if(!Number.isInteger(level)||level<=0||level>100)return;this.level=level;try{this.storage.setItem(this.key,String(level));}catch{}}
 toggle(level){if(Number.isInteger(level)&&level>0&&level<=100){this.remember(level);return 0;}return this.level;}
}
let roomGainStorage;try{roomGainStorage=sessionStorage;}catch{}
const roomMusicVolume=new RoomMusicVolume(room,roomGainStorage),gainTimers=new Map();
let quickMutePending=false,quickMuteError="";
function updateMuteIcon(button,muted,confirmed,note,status){
 button.dataset.muted=String(muted);button.setAttribute("aria-pressed",confirmed?String(muted):"mixed");
 if(note.textContent!==status)note.textContent=status;
}
function updateMuteButtons(){
 const micMuted=playback.mic_muted!==false,micConfirmed=playback.online&&typeof playback.mic_muted==="boolean"&&playback.audio_capabilities?.voice_protection?.mute_confirmed!==false;
 const micButton=$("mute-button");micButton.disabled=micMuted&&(!playback.online||!micAvailable);
 const micWaiting=controlPendingVersions.has(micButton);
 updateMuteIcon(micButton,micMuted,micConfirmed,$("mic-mute-state"),micWaiting?"已发送麦克风设置，等待电视确认":!micConfirmed?"麦克风状态尚未确认":micMuted?"麦克风已静音，按下取消静音":"麦克风未静音，按下静音");
 const quick=$("quick-mute-button"),quickStatus=$("quick-mute-status");
 quick.disabled=!playback.online||quickMutePending||(micConfirmed&&micMuted&&!micWaiting);
 quick.textContent=quickMutePending?"静音待确认":micConfirmed&&micMuted&&!micWaiting?"麦克风已静音":"麦克风静音";
 quick.setAttribute("aria-busy",String(quickMutePending));
 const quickText=!playback.online?"电视未连接，暂时无法发送静音。":quickMutePending?"正在静音，等待电视确认。":quickMuteError||(micConfirmed&&micMuted&&!micWaiting?"电视已确认麦克风静音。":!micConfirmed?"麦克风状态未确认，可发送静音请求。":"");
 if(quickStatus.textContent!==quickText)quickStatus.textContent=quickText;quickStatus.hidden=!quickText;
 const level=pendingControls.music_gain??controls.music_gain;
 roomMusicVolume.remember(level);const musicMuted=level===0;
 const musicConfirmed=playback.online&&pendingControls.music_gain===undefined&&!controlPendingVersions.has($("music-mute-button"))&&playback.applied_revision>=controls.control_revision;
 $("music-mute-button").disabled=!playback.online;
 updateMuteIcon($("music-mute-button"),musicMuted,musicConfirmed,$("music-mute-state"),!playback.online?"电视未连接，音乐音量尚未确认":!musicConfirmed?"已选择音乐音量，等待电视确认":musicMuted?"音乐已静音，按下恢复静音前音量":"音乐未静音，按下静音");
 $("sound-status").textContent=!playback.online?"等待电视连接":!micAvailable?"麦克风暂不可用，更新完成后可开启":playback.safety_latched?(micConfirmed&&micMuted?"人声保护已静音，需手动恢复":"人声保护正在静音，等待电视确认"):micWaiting?"正在调整麦克风…":"";
}
function changeGain(id,key,level,delay=120){
 level=Math.max(0,Math.min(100,Math.round(level)));clearTimeout(gainTimers.get(id));gainTimers.delete(id);
 pendingControls[key]=level;$(`${id}-volume`).value=String(level);$(`${id}-value`).textContent=`${level}%`;
 if(id==="music")roomMusicVolume.remember(level);updateVolumeButtons(id,level);updateMuteButtons();
 if(delay===0)return setControl({[key]:level});
 gainTimers.set(id,setTimeout(()=>{gainTimers.delete(id);setControl({[key]:level});},delay));
}
function toggleMusic(){
 if($("music-mute-button").disabled)return;
 const level=pendingControls.music_gain??controls.music_gain;
 return changeGain("music","music_gain",roomMusicVolume.toggle(level),0);
}
$("music-mute-button").onclick=toggleMusic;
let playbackSignature="";
function queuePlaybackSignature(){return JSON.stringify([playback.current,playback.played,playback.state]);}
function queueRank(song){return song.id===playback.current?0:playback.played.includes(song.id)||song.state==="cancelled"?2:1;}
function updatePlayer(){updateGuide();updatePartyAudio();
 $("player").hidden=!identity;document.body.classList.toggle("has-player",!!identity);const current=selected.find(s=>s.id===playback.current);$("now-title").textContent=current?.title||"准备好，唱下一首";
 const protection=playback.audio_capabilities?.voice_protection;
 $("player-status").textContent=!playback.online?"等待电视连接":playback.safety_latched?(playback.mic_muted&&protection?.mute_confirmed!==false?"人声保护已静音，需手动恢复":"人声保护正在静音，等待电视确认"):protection?.state==="preparing"?"正在准备语音保护":protection?.state==="attenuated"?"正在抑制啸叫风险 · 已下调人声设定":protection?.state==="failed"?"语音保护不可用，麦克风保持静音":playback.state==="playing"?`${current?.nickname||"朋友"} 正在唱`:playback.state==="preparing"?"正在加载音源":"已暂停";
 const playing=playback.state==="playing"||playback.state==="preparing";$("play-toggle").textContent=playing?"Ⅱ":"▶";$("play-toggle").setAttribute("aria-label",playing?"暂停播放":"开始播放");$("play-toggle").disabled=!playback.online||commandBusy;$("skip-button").disabled=!playback.online||!playback.current||commandBusy;$("restart-button").disabled=!playback.online||!playback.current||commandBusy;
 for(const [id,value]of[["mode-instrumental",0],["mode-original",1]]){$(id).setAttribute("aria-pressed",String((controls.original||0)===value));$(id).disabled=!playback.online;}

 for(const [id,key]of[["music","music_gain"],["mic","mic_gain"],["sfx","sfx_gain"]]){const input=$(`${id}-volume`);input.disabled=!playback.online||(id==="mic"&&!micAvailable)||(id==="sfx"&&!FridayPartyAudio.capabilities(playback).sfx);const level=pendingControls[key]??controls[key];updateVolumeButtons(id,level);if(document.activeElement!==input){input.value=level;$(`${id}-value`).textContent=`${level}%`;}}
 updateMuteButtons();
}
async function sendCommand(action,song="",origin=""){if(commandBusy)return;const before={...playback};commandBusy=true;const button=$(origin||(action==="next"?"skip-button":"play-toggle"));busy(button,true,origin==="restart-button"?"正在从头开始…":action==="next"?"正在切歌…":action==="pause"?"正在暂停…":"正在开始播放…");updatePlayer();try{const result=await api("command",{action,song,request_id:crypto.randomUUID()});await waitForTV(()=>FridayPartyAudio.commandObserved(action,song,before,playback,result.seq));}catch(error){failure(error);}finally{commandBusy=false;busy(button,false);await refresh();updatePlayer();}}
$("mode-instrumental").onclick=()=>setControl({original:0});$("mode-original").onclick=()=>setControl({original:1});
let controlWrites=Promise.resolve(),controlSerial=0;const controlPendingVersions=new Map();
function setControl(value,{signal,propagate=false}={}){
 const version=++controlSerial,nodes=Object.keys(value).flatMap(k=>k==="original"?[$("mode-original"),$("mode-instrumental")]:k==="mic_muted"?[$("mute-button")]:k==="vocal_preset"?[$("vocal-status")]:k==="music_gain"?[$("music-value"),$("music-mute-button")]:[$(({music_gain:"music-value",mic_gain:"mic-value",sfx_gain:"sfx-value"})[k])]);
 for(const node of nodes){controlPendingVersions.set(node,version);busy(node,true,"正在调整声音…");}
 // Serialize HTTP writes only. Waiting for a TV must never hold up a later mute.
 const request=controlWrites.catch(()=>{}).then(async()=>{if(signal?.aborted)throw Object.assign(new Error(),{code:"cancelled"});const result=await api("controls",value,"",{signal});if(signal?.aborted)throw Object.assign(new Error(),{code:"cancelled"});controls={...controls,...value,control_revision:Math.max(controls.control_revision||0,result.revision||0)};return result;});controlWrites=request;
 return request.then(result=>waitForTV(()=>playback.applied_revision>=result.revision&&(!Object.hasOwn(value,"mic_muted")||(playback.online&&playback.mic_muted===Boolean(value.mic_muted)&&playback.audio_capabilities?.voice_protection?.mute_confirmed!==false)),signal)).then(()=>{$(Object.hasOwn(value,"sfx_gain")?"sfx-error":"sound-error").textContent="";}).catch(error=>{if(error.code==="cancelled"){if(propagate)throw error;return;}$(Object.hasOwn(value,"sfx_gain")?"sfx-error":"sound-error").textContent=error.code==="tv_slow"?"电视尚未响应，请稍后查看。":"暂时无法调整，请重试。";failure(error);if(propagate)throw error;}).finally(()=>{for(const node of nodes)if(controlPendingVersions.get(node)===version){controlPendingVersions.delete(node);if(node.id==="music-value")delete pendingControls.music_gain;if(node.id==="mic-value")delete pendingControls.mic_gain;if(node.id==="sfx-value")delete pendingControls.sfx_gain;busy(node,false);}updatePlayer();});
}
$("sound-button").onclick=()=>{updatePlayer();openDialog("sound-dialog");};$("play-toggle").onclick=()=>sendCommand(["playing","preparing"].includes(playback.state)?"pause":"resume",playback.current);$("skip-button").onclick=()=>sendCommand("next",playback.current);$("restart-button").onclick=()=>sendCommand("play",playback.current,"restart-button");
async function quickMute(){
 if(!identity||!playback.online||quickMutePending)return;
 // This is deliberately one-way, including while an earlier unmute waits for ACK.
 // Volume writes remain separate keys and cannot cancel this mute choice.
 quickMuteError="";quickMutePending=true;updateMuteButtons();
 try{await setControl({mic_muted:1},{propagate:true});}
 catch{quickMuteError="静音尚未确认，请使用电视遥控器或话筒静音。";}
 finally{quickMutePending=false;updateMuteButtons();}
}
$("quick-mute-button").onclick=quickMute;
function toggleMic(){if($("mute-button").disabled||$("mute-button").getAttribute("aria-busy")==="true")return;setControl({mic_muted:playback.mic_muted!==false?0:1});}$("mute-button").onclick=toggleMic;
for(const [id,key]of[["music","music_gain"],["mic","mic_gain"],["sfx","sfx_gain"]]){const input=$(`${id}-volume`);input.oninput=()=>changeGain(id,key,Number(input.value));$(`${id}-minus`).onclick=()=>changeGain(id,key,Number(pendingControls[key]??controls[key])-10,0);$(`${id}-plus`).onclick=()=>changeGain(id,key,Number(pendingControls[key]??controls[key])+10,0);}
function updateVolumeButtons(id,level){$(`${id}-minus`).disabled=level<=0||!playback.online||(id==="mic"&&!micAvailable)||(id==="sfx"&&!FridayPartyAudio.capabilities(playback).sfx);$(`${id}-plus`).disabled=level>=100||!playback.online||(id==="mic"&&!micAvailable)||(id==="sfx"&&!FridayPartyAudio.capabilities(playback).sfx);}
const sfxCommands=new FridayPartyAudio.SfxCommands({send:payload=>api("command",payload),onState:state=>{$("sfx-status").textContent=FridayPartyAudio.sfxMessage(state);}});
for(const [value,label]of FridayPartyAudio.presets){const button=document.createElement("button");button.type="button";button.dataset.preset=value;button.textContent=label;button.onclick=()=>setControl({vocal_preset:value});$("vocal-presets").append(button);}
function updatePartyAudio(){
 const view=FridayPartyAudio.vocalView(controls,playback);$("vocal-status").textContent=view.text;
 for(const button of $("vocal-presets").children){button.disabled=!view.supported.includes(button.dataset.preset);button.setAttribute("aria-pressed",String(view.selected===button.dataset.preset));}
 const supported=FridayPartyAudio.capabilities(playback).sfx;
 for(const id of["sfx-applause","sfx-cheer"])$(id).disabled=!playback.online||!supported;$("sfx-stop").disabled=!playback.online||(!supported&&!playback.sfx_seq&&!sfxCommands.state.serial);
 sfxCommands.observe(playback);
 $("sfx-status").textContent=!playback.online?"等待电视连接":!supported?"电视暂未提供氛围音效":FridayPartyAudio.sfxMessage(sfxCommands.state);
}
async function trackSfx(serial){const end=performance.now()+7500;while(serial===sfxCommands.state.serial&&performance.now()<end&&["sending","sent"].includes(sfxCommands.state.phase)){await refresh();await new Promise(resolve=>setTimeout(resolve,125));}sfxCommands.observe(playback);}
for(const [id,action]of[["sfx-applause","sfx_applause"],["sfx-cheer","sfx_cheer"],["sfx-stop","sfx_stop"]])$(id).onclick=()=>{
 $("sfx-error").textContent="";const request=sfxCommands.trigger(action),serial=sfxCommands.state.serial;
 request.then(()=>trackSfx(serial)).catch(error=>{$("sfx-error").textContent=error.code==="sfx_unavailable"?"电视暂未提供氛围音效":"暂未发送成功，请重试";});
};
$("atmosphere-button").onclick=()=>{updatePlayer();openDialog("atmosphere-dialog");};
function songActions(song){actionSong=song;$("song-action-title").textContent=song.title;$("sing-now").textContent=song.id===playback.current?"重新演唱":"现在唱";$("song-error").textContent="";$("sing-now").disabled=song.state!=="ready"||!playback.online;const pending=song.id!==playback.current&&!playback.played.includes(song.id)&&!["failed","cancelled"].includes(song.state);for(const id of["sing-next","move-up","move-down"])$(id).disabled=!pending;openDialog("song-dialog");}
async function saveOrder(ids,revision=queueRevision){if(ordering)return;ordering=true;busy($("queue-tab"),true,"正在调整顺序…");try{await api("order",{ids,revision});toast("顺序已更新");}catch(error){failure(error);}finally{ordering=false;busy($("queue-tab"),false);await refresh();render();}}
function moveSong(id,direction){const ids=selected.map(s=>s.id),pending=selected.filter(s=>s.id!==playback.current&&!playback.played.includes(s.id)&&!["failed","cancelled"].includes(s.state)).map(s=>s.id);const at=pending.indexOf(id);if(at<0)return;let next=direction==="next"?0:Math.max(0,Math.min(pending.length-1,at+direction));if(at===next)return;const target=pending[next];ids.splice(ids.indexOf(id),1);ids.splice(ids.indexOf(target)+(next>at?1:0),0,id);saveOrder(ids);}
$("sing-now").onclick=()=>{if(!actionSong)return;$("song-dialog").close();sendCommand("play",actionSong.id);};$("sing-next").onclick=()=>{if(!actionSong)return;$("song-dialog").close();moveSong(actionSong.id,"next");};$("move-up").onclick=()=>{if(actionSong)moveSong(actionSong.id,-1);};$("move-down").onclick=()=>{if(actionSong)moveSong(actionSong.id,1);};
function startDrag(event,song,handle){if(event.button!==0||ordering)return;const row=handle.closest("article");drag={id:song.id,revision:queueRevision,startY:event.clientY,moved:false};handle.setPointerCapture(event.pointerId);
 handle.onpointermove=e=>{if(!drag)return;if(Math.abs(e.clientY-drag.startY)>6){drag.moved=true;handle.dataset.moved="1";row.classList.add("dragging");}if(!drag.moved)return;const target=document.elementFromPoint(e.clientX,e.clientY)?.closest("article[data-queue]");if(!target||target===row)return;const id=target.dataset.queue;if(id===playback.current||playback.played.includes(id))return;const from=selected.findIndex(s=>s.id===song.id),to=selected.findIndex(s=>s.id===id);if(from<0||to<0)return;const [item]=selected.splice(from,1);selected.splice(to,0,item);if(from<to)target.after(row);else target.before(row);if(e.clientY>innerHeight-170)scrollBy(0,18);else if(e.clientY<100)scrollBy(0,-18);};
 const finish=cancelled=>{if(!drag)return;const result=drag;drag=null;row.classList.remove("dragging");handle.onpointermove=null;handle.onpointerup=null;handle.onpointercancel=null;if(result.moved&&!cancelled)saveOrder(selected.map(s=>s.id),result.revision);else refresh();};handle.onpointerup=()=>finish(false);handle.onpointercancel=()=>finish(true);
}
async function join(){
 if(joining)return;if(!valid){$("profile-button").disabled=true;$("settings-button").disabled=true;return;}joining=true;clearTimeout(joinTimer);
 try{if(identity?.key){try{identity={...identity,...await api("profile")};}catch(error){if(error.code!=="guest_unavailable")throw error;addQueue?.close();identity=null;}}
  if(!identity)identity=await api("join",{});saveIdentity();$("search").hidden=false;$("source-picker").hidden=false;render();await refresh();initAddQueue();loadDiscover();clearInterval(refreshTimer);startStatusFeed();
 }catch(error){failure(error);if(connectionError(error)||error.code==="rate_limited")joinTimer=setTimeout(join,3000);}finally{joining=false;}
}
join();document.addEventListener("visibilitychange",()=>{if(document.hidden){statusFeed?.stop();clearInterval(refreshTimer);searchComposing=false;cancelSearchInput();}else{if(identity){initAddQueue();startStatusFeed();}else join();refresh();}});
window.addEventListener("online",()=>{addQueue?.wake();if(identity)refresh();else join();});
window.addEventListener("pagehide",()=>{addQueue?.close();statusFeed?.stop();clearInterval(refreshTimer);searchComposing=false;cancelSearchInput();clearTimeout(joinTimer);});
window.addEventListener("pageshow",()=>{if(!document.hidden&&identity){initAddQueue();startStatusFeed();refresh();}});

// One generation owns the loader, reference, alignment and UI. Old finally/catch
// callbacks cannot stop or publish the result of a newer guide session.
let guideGeneration=0,guideOperation=null,referenceBuffer=null,referenceSong="";
let acoustic=null,syncSong="",alignLoading=false;
let alignmentState={phase:"idle",code:"none",frames:0,score:0,margin:0,offset_ms:0};
const appGuideError=code=>Object.assign(new Error(code),{code});
const captureReleaseMessage="手机麦克风尚未释放，请关闭页面后重新进入。";
function guideVersionMatches(op){return !!op&&op.spec?.sha256===playback.guide?.sha256&&op.spec?.reference?.sha256===playback.guide?.reference?.sha256;}
function guideCurrent(op){return !!op&&guideOperation===op&&op.generation===guideGeneration&&!op.controller.signal.aborted&&earEnabled&&playback.current===op.song&&guideVersionMatches(op);}
function checkGuide(op){if(!guideCurrent(op))throw appGuideError("cancelled");}
function invalidateGuide({disable=false}={}){
 guideGeneration++;guideOperation?.controller.abort();guideOperation=null;acoustic?.cancel();acoustic=null;
 earCanPlay=false;earLoading=false;alignLoading=false;syncSong="";referenceBuffer=null;referenceSong="";earSong="";
 if(disable)earEnabled=false;ear.stop();busy($("guide-toggle"),false);$("guide-align-cancel").hidden=true;
 $("guide-delay").disabled=$("guide-advance").disabled=false;
}
function beginGuide(){invalidateGuide();const op={generation:guideGeneration,controller:new AbortController(),song:playback.current,spec:playback.guide,alignmentGeneration:null};guideOperation=op;earSong=op.song;return op;}
async function verifiedGuide(op,spec,variant){
 if(!spec||typeof spec.sha256!=="string"||!/^[a-f0-9]{64}$/.test(spec.sha256)||!Number.isInteger(spec.bytes)||spec.bytes<=1024||spec.bytes>32*1024*1024)throw appGuideError("reference_unavailable");
 const response=await fetch(`./api/rooms/${room}/guide?id=${encodeURIComponent(op.song)}${variant?"&variant=reference":""}`,{headers:{"X-KTV-Room":key,"X-KTV-Guest":identity.key},credentials:"omit",cache:"no-store",signal:op.controller.signal});
 checkGuide(op);if(!response.ok)throw appGuideError(variant?"reference_unavailable":"guide_unavailable");
 const bytes=await response.arrayBuffer();checkGuide(op);if(bytes.byteLength!==spec.bytes)throw appGuideError("changed");
 const hash=await crypto.subtle.digest("SHA-256",bytes);checkGuide(op);
 if(Array.from(new Uint8Array(hash)).map(b=>b.toString(16).padStart(2,"0")).join("")!==spec.sha256)throw appGuideError("changed");
 return bytes;
}
async function loadGuide(){
 if(!earEnabled||!playback.current||!playback.guide||document.hidden)return;
 const op=beginGuide();earLoading=true;ear.calibrating=true;busy($("guide-toggle"),true,"正在加载伴唱…");
 try{
  // start() activates AudioContext and the silent primer before this first await.
  await ear.start(async signal=>{
   signal.addEventListener("abort",()=>op.controller.abort(),{once:true});checkGuide(op);
   if(op.spec.reference?.kind!=="accompaniment")throw appGuideError("reference_unavailable");
   await setControl({original:0},{signal:op.controller.signal,propagate:true});checkGuide(op);
   if(controls.original!==0)throw appGuideError("changed");
   const [original,reference]=await Promise.all([verifiedGuide(op,op.spec,false),verifiedGuide(op,op.spec.reference,true)]);checkGuide(op);
   const decoded=await ear.context.decodeAudioData(reference);checkGuide(op);
   if(!Number.isFinite(decoded.duration)||!Number.isFinite(op.spec.reference.duration_ms)||Math.abs(decoded.duration*1000-op.spec.reference.duration_ms)>=150)throw appGuideError("changed");
   referenceBuffer=decoded;referenceSong=op.song;return original;
  },{deferPlayback:true});checkGuide(op);
  if(!Number.isFinite(ear.buffer?.duration)||!Number.isFinite(op.spec.duration_ms)||Math.abs(ear.buffer.duration*1000-op.spec.duration_ms)>=150)throw appGuideError("changed");
  earLoading=false;await prepareAlignment(op);checkGuide(op);
 }catch(error){
  if(!guideCurrent(op))return;
  ear.stop();earEnabled=false;earCanPlay=false;earPhase=error.code==="audio_start_blocked"?"blocked":"failed";reportEar(earPhase);
  $("guide-status").textContent=error.code==="capture_release_failed"?captureReleaseMessage:error.code==="reference_unavailable"?"这首的伴奏正在准备，暂时无法自动对拍。":earPhase==="blocked"?"浏览器尚未开始播放，点开启重试。":"伴唱还没加载好，点开启重试。";
 }finally{
  if(guideOperation===op&&op.generation===guideGeneration){earLoading=false;if(!alignLoading){ear.calibrating=false;busy($("guide-toggle"),false);}updateGuide();}
 }
}
function alignmentUpdate(value){alignmentState={...alignmentState,...value};reportEar(earPhase);}
function cancelAlignment(){if(!earLoading&&!alignLoading)return;invalidateGuide({disable:true});alignmentUpdate({phase:"cancelled",code:"cancelled"});$("guide-status").textContent="已取消对拍。";updateGuide();}
function updateGuide(){
 if(earEnabled&&(!playback.online||!playback.current)){invalidateGuide({disable:true});$("guide-status").textContent="等待电视播放。";}
 if(earEnabled&&earSong&&(earSong!==playback.current||!guideVersionMatches(guideOperation))){invalidateGuide();if(playback.guide)loadGuide();else $("guide-status").textContent="伴唱正在准备…";}
 if(earEnabled&&alignLoading&&playback.state!=="playing"){invalidateGuide({disable:true});$("guide-status").textContent="电视播放已变化，点开启重新对拍。";}
 if(earEnabled&&controls.original===1&&!earLoading){invalidateGuide({disable:true});$("guide-status").textContent="电视已切换原唱。";}
 $("guide-toggle").setAttribute("aria-pressed",String(earEnabled));
 $("guide-toggle").textContent=earEnabled?(earLoading||alignLoading?"取消":earCanPlay?"关闭":"重试"):"开启";
 $("guide-toggle").setAttribute("aria-label",earEnabled?(earLoading||alignLoading?"取消耳边伴唱对拍":earCanPlay?"关闭耳边伴唱":"重试耳边伴唱"):"开启耳边伴唱");
 $("guide-toggle").disabled=!playback.online||!playback.current;$("guide-settings").hidden=!earEnabled;
 if(!earEnabled)return;
 if(performance.now()-earReported>5000)reportEar(earPhase);
 if(!earLoading&&!alignLoading&&!earSong&&playback.guide){loadGuide();return;}
 const op=guideOperation;
 // Try once per playback timeline. A failed attempt waits for the user, while
 // a same-song restart may replace a cancelled attempt from the old timeline.
 if(!document.hidden&&!earLoading&&!alignLoading&&guideCurrent(op)&&playback.state==="playing"&&controls.original===0&&ear.buffer&&referenceBuffer&&referenceSong===op.song&&ear.needsAlignment()&&op.alignmentGeneration!==earClock.generation){
  prepareAlignment(op).catch(error=>{if(!guideCurrent(op))return;earCanPlay=false;ear.halt();earPhase="failed";reportEar(earPhase);$("guide-status").textContent=error.code==="capture_release_failed"?captureReleaseMessage:"歌曲或设置已变化，请重新对拍。";updateGuide();});return;
 }
 if(earCanPlay)ear.sync();else ear.halt();
}
$("guide-toggle").onclick=()=>{
 if(earLoading||alignLoading){cancelAlignment();return;}
 if(earEnabled&&!earCanPlay&&ear.buffer&&referenceBuffer&&referenceSong===playback.current){
  const op=guideOperation;if(!guideCurrent(op))return;
  // Claim this attempt before resume can yield: a second click cancels it and
  // cannot start a concurrent matcher or clear the first matcher's ownership.
  earLoading=true;busy($("guide-toggle"),true,"正在准备对拍…");$("guide-align-cancel").hidden=false;
  // User activation happens synchronously; no network/module await before resume.
  const resumed=ear.resumePlayback();updateGuide();
  resumed.then(()=>{checkGuide(op);earLoading=false;return prepareAlignment(op);}).catch(error=>{
   if(!guideCurrent(op))return;earPhase="blocked";reportEar(earPhase);$("guide-status").textContent=error.code==="capture_release_failed"?captureReleaseMessage:error.code==="changed"?"歌曲或设置已变化，请重新对拍。":"浏览器尚未开始播放，点重试继续。";
  }).finally(()=>{if(guideCurrent(op)){earLoading=false;if(!alignLoading){busy($("guide-toggle"),false);$("guide-align-cancel").hidden=true;}updateGuide();}});return;
 }
 if(earEnabled){invalidateGuide({disable:true});updateGuide();return;}
 if(!playback.guide){$("guide-status").textContent="伴唱正在准备，稍后再试。";return;}
 earEnabled=true;loadGuide();updateGuide();
};
$("guide-volume").oninput=()=>ear.volume(Number($("guide-volume").value)/100);
function adjustGuide(change){
 if(!earEnabled||!ear.buffer||earLoading||alignLoading)return;
 try{ear.setManualAdjustment(earClock.adjustment+change);}catch(error){earCanPlay=false;ear.halt();earPhase="blocked";$("guide-status").textContent=ear.capturePending()?captureReleaseMessage:"歌曲或设置已变化，请重新对拍。";reportEar(earPhase);updateGuide();return;}
 earCanPlay=ear.sync(true);
 earPhase=earCanPlay?"playing":"blocked";$("guide-status").textContent=earCanPlay?"已应用微调":"播放尚未开始，点重试继续。";
 $("guide-offset").textContent=`${earClock.adjustment>0?"+":""}${earClock.adjustment} ms`;reportEar(earPhase);updateGuide();
}
$("guide-delay").onclick=()=>adjustGuide(-50);$("guide-advance").onclick=()=>adjustGuide(50);
function reportEar(phase){if(!identity?.key)return;earReported=performance.now();api("monitor",{phase,...ear.stats(),alignment:alignmentState}).catch(()=>{});}
async function alignGuide(op){
 checkGuide(op);if(!ear.buffer||playback.state!=="playing")throw appGuideError("changed");
 alignLoading=true;syncSong=op.song;alignmentUpdate({phase:"requesting",code:"none",frames:0,score:0,margin:0,offset_ms:earClock.adjustment});
 const button=$("guide-toggle");busy(button,true,"正在准备对拍…");$("guide-align-cancel").hidden=false;$("guide-delay").disabled=$("guide-advance").disabled=true;
 updateGuide();
 try{
  const {FridayAcousticSync}=await acousticModule;checkGuide(op);
  if(!referenceBuffer||referenceSong!==op.song)throw appGuideError("reference_unavailable");
  const session=new FridayAcousticSync();acoustic=session;let previous=-1;
  const result=await session.run(ear,earClock,seconds=>{if(!guideCurrent(op)||seconds===previous)return;previous=seconds;$("guide-status").textContent=`正在聆听电视伴奏… ${seconds} 秒`;busy(button,true,"正在聆听电视伴奏…");},value=>{if(guideCurrent(op))alignmentUpdate(value);},{referenceBuffer,referenceSong:op.song});
  checkGuide(op);ear.acceptAlignment(result);alignmentUpdate({phase:"matched",code:"matched",frames:result.frames,score:Math.round(result.score*1000),margin:Math.round(result.margin*1000),offset_ms:result.offset});
  $("guide-offset").textContent=`${result.offset>0?"+":""}${result.offset} ms`;$("guide-status").textContent="已自动对拍";return true;
 }catch(error){
  if(!guideCurrent(op))return false;
  const code=error.name==="NotAllowedError"?"denied":error.code;
  const known=["denied","unsupported","quiet","ambiguous","changed","cancelled","permission_timeout","capture_timeout","capture_release_failed","reference_unavailable","busy","audio_start_blocked"];
  alignmentUpdate({phase:code==="cancelled"?"cancelled":"failed",code:known.includes(code)?code:"unavailable",frames:error.frames||alignmentState.frames,score:Math.round((error.score||0)*1000),margin:Math.round((error.margin||0)*1000)});
  $("guide-status").textContent=({denied:"请允许使用麦克风后重试。",unsupported:"此浏览器暂不支持自动对拍，请使用手动微调。",quiet:"没有听清伴奏，靠近电视后重试。",ambiguous:"这段没能对准，换一段伴奏后重试。",changed:"歌曲或设置已变化，请重新对拍。",cancelled:"已取消对拍。",permission_timeout:"尚未取得麦克风权限，允许后再试。",capture_timeout:"没有收到手机麦克风声音，重新开启伴唱后重试。",capture_release_failed:captureReleaseMessage,reference_unavailable:"这首的伴奏正在准备，暂时无法自动对拍。",busy:"正在完成上一次对拍，稍后再试。",audio_start_blocked:"浏览器尚未开始播放，点重试继续。"})[code]||"对拍未完成，请重试。";return false;
 }finally{
  if(guideCurrent(op)){acoustic=null;alignLoading=false;syncSong="";busy(button,false);$("guide-align-cancel").hidden=true;$("guide-delay").disabled=$("guide-advance").disabled=false;}
 }
}
async function prepareAlignment(op){
 checkGuide(op);op.alignmentGeneration=earClock.generation;if(controls.original!==0)throw appGuideError("changed");earCanPlay=false;ear.halt();const ok=await alignGuide(op);checkGuide(op);
 ear.calibrating=false;earCanPlay=ok===true&&ear.context?.state==="running"&&ear.sync(true);
 earPhase=earCanPlay?"playing":"failed";if(!earCanPlay)ear.halt();reportEar(earPhase);updateGuide();return earCanPlay;
}
$("guide-align-cancel").onclick=cancelAlignment;$("sound-dialog").addEventListener("close",cancelAlignment);
function suspendGuidePage(){if(earEnabled||earLoading||alignLoading){invalidateGuide({disable:true});$("guide-status").textContent="伴唱已暂停，点开启继续。";updateGuide();}}
document.addEventListener("visibilitychange",()=>{if(document.hidden)suspendGuidePage();});
window.addEventListener("pagehide",suspendGuidePage);

// Match content clearance to the real toolbar height, including text zoom.
if(typeof ResizeObserver!=="undefined")new ResizeObserver(()=>{const height=$("player").hidden?0:$("player").getBoundingClientRect().height;document.documentElement.style.setProperty("--player-space",`${Math.ceil(height+28)}px`);}).observe($("player"));
