"use strict";
const $=id=>document.getElementById(id),fragment=new URLSearchParams(location.hash.slice(1));
const names=["汽水猫","夜航兔","慢慢鲸","摇摆熊","月球旅人","小幽灵","唱片君","星星糖","柠檬汽水","仙人掌","北极企鹅","节拍小子"];
const extraNames=["山茶晚风","海盐汽水","弯月小岛","白桃列车","银杏信箱","雾蓝唱片","月亮贝壳","柚子雨","杏仁云","铃兰邮差","晚班星光","晴空纸船"];
const themes=[["content","内容与留白","安静的影像，给歌声留白"],["festival","音乐节","鲜明图形，今晚放开唱"],["live","Live","把客厅交给现场"]];
let room=fragment.get("r"),key=fragment.get("k"),identity=null,songs=[],selected=[],mode="home",online=false,theme="content",refreshTimer,chosenAvatar=0,queueRevision=0,playableOrder=null,playback={online:false,current:"",played:[],state:"idle"},controls={music_gain:70,sfx_gain:60,control_revision:0},drag=null,ordering=false,actionSong=null,commandBusy=false,pendingControls={},source="song",searchSequence=0,searchOffset=0,searchMore=false,searchBusy=false,searchQuery="",playlistId="",playlistTitle="",recommendations=null,playlistPage=0;
try{if(room&&key){sessionStorage.setItem("friday-ktv-room",JSON.stringify({room,key}));history.replaceState(null,"",location.pathname);}else({room,key}=JSON.parse(sessionStorage.getItem("friday-ktv-room")||"{}"));identity=JSON.parse(sessionStorage.getItem(`friday-ktv-guest:${room}`)||"null");}catch{}
const valid=typeof room==="string"&&/^[a-f0-9-]{36}$/.test(room)&&typeof key==="string"&&/^[A-Za-z0-9_-]{32,80}$/.test(key);
const pendingAdds=new Set();
let addQueue=null,addSnapshot={items:[],stopped:"",persistence:"memory"},addViewSignature="",statusFailures=0,lastStatusSuccess=0,lastStatusProbe=-Infinity,roomResumeAt=performance.now(),statusRequest=null,joining=false,joinTimer,statusFeed=null;
const roomStaleMs=12000;
let joinKey="",joinFailures=0;
let preferredProfile=null;
const guestSeedKey="friday-ktv-guest-seed-v1",guestProfileKey="friday-ktv-guest-profile";
const guestCredential=/^[A-Za-z0-9_-]{32,80}$/,randomSecret=/^[a-f0-9]{64}$/;
let playbackAvailable=true;
const pendingActions=new Map();
function busy(node,active,label="正在处理…"){
 if(!node)return;node.classList.toggle("is-busy",active);node.setAttribute("aria-busy",String(active));
 if(active)pendingActions.set(node,label);else pendingActions.delete(node);
 for(const current of pendingActions.keys())if(!current.isConnected)pendingActions.delete(current);
 const labels=[...pendingActions.values()];$("action-progress").hidden=!labels.length;$("action-progress-text").textContent=labels[labels.length-1]||"";
}
async function waitForTV(check,signal){
 const started=performance.now(),end=started+9000;
 while(performance.now()<end){
  if(signal?.aborted)throw Object.assign(new Error(),{code:"cancelled"});
  if(check())return;
  if(performance.now()-lastStatusSuccess>900)probeSoon();
  if(signal?.aborted)throw Object.assign(new Error(),{code:"cancelled"});
  if(check())return;
  await new Promise(resolve=>setTimeout(resolve,Math.min(250,Math.max(0,end-performance.now()))));
 }
 throw Object.assign(new Error(),{code:lastStatusSuccess>started?"tv_slow":"receipt_unavailable"});
}
const earClock=new FridayGuideClock();let earEnabled=false,earCanPlay=false,earSong="",earLoading=false;
const ear=new FridayEarMonitor(earClock,state=>{
 if(state==="suspended"){invalidateGuide({disable:true});updateGuide();$("guide-status").textContent="伴唱已暂停，点开启继续。";}
 else if(state==="start"||state==="loading")$("guide-status").textContent="正在加载原唱…";
});
const sourceOf=s=>["cloud","mv"].includes(s.source)?s.source:"song",songKey=s=>`${sourceOf(s)}:${s.song_id}`;
const alreadyQueued=s=>selected.some(v=>songKey(v)===songKey(s)&&v.state!=="cancelled"&&!playback.played.includes(v.id));
const frequentKey="friday-ktv-frequent-songs";
let frequentSongs=[],frequentSeen=[];
try{
 const saved=JSON.parse(localStorage.getItem(frequentKey)||"{}");
 if(Array.isArray(saved.songs))frequentSongs=saved.songs.filter(s=>s&&typeof s.song_id==="string"&&["song","mv"].includes(s.source)&&Number.isInteger(s.plays)&&s.plays>0).slice(0,40).map(s=>({...s,artists:Array.isArray(s.artists)?s.artists:[]}));
 if(Array.isArray(saved.seen))frequentSeen=saved.seen.slice(0,240);
}catch{}
function rememberFrequent(rows){
 if(!identity?.id||!playback.online||playback.state!=="playing"||!playback.current||frequentSeen.includes(playback.current))return false;
 const song=rows.find(s=>s.id===playback.current&&s.guest_id===identity.id&&s.state==="ready");
 if(!song||sourceOf(song)==="cloud")return false;
 const key=songKey(song),prior=frequentSongs.find(s=>songKey(s)===key),match=songs.find(s=>songKey(s)===key);
 const record={song_id:song.song_id,source:sourceOf(song),title:song.title,artwork:resolvedArtwork(song)||(match&&resolvedArtwork(match))||prior?.artwork,artists:match?.artists||prior?.artists||[],plays:(prior?.plays||0)+1};
 frequentSongs=[record,...frequentSongs.filter(s=>songKey(s)!==key)].sort((a,b)=>b.plays-a.plays).slice(0,40);
 frequentSeen=[song.id,...frequentSeen].slice(0,240);
 try{localStorage.setItem(frequentKey,JSON.stringify({songs:frequentSongs,seen:frequentSeen}));}catch{}
 return true;
}
const labels={queued:"等待准备",acquiring:"下载中",separating:"制作伴奏",transcoding:"即将就绪",ready:"已就绪",failed:"准备失败",cancelled:"已取消"};
function message(text){$("message").textContent=text;}
function toast(text){$("toast").textContent=text;$("toast").classList.add("show");setTimeout(()=>$("toast").classList.remove("show"),2400);}
function avatar(node,id){const i=Number.isInteger(id)&&id>=0&&id<12?id:0;node.style.backgroundPosition=`${i%4*100/3}% ${Math.floor(i/4)*50}%`;}
function readGuestStorage(storage,name){
 try{return globalThis[storage].getItem(name)||"";}catch{return "";}
}
function writeGuestStorage(storage,name,value){
 try{
  const data=globalThis[storage];
  data.setItem(name,value);
  return data.getItem(name)===value;
 }catch{return false;}
}
function usableGuestProfile(value){
 if(typeof value?.nickname!=="string"||/[\x00-\x1f\x7f]/.test(value.nickname))return null;
 const nickname=value.nickname.trim();
 if(!nickname||Array.from(nickname).length>24||!Number.isInteger(value.avatar)||value.avatar<0||value.avatar>=names.length)return null;
 return {nickname,avatar:value.avatar};
}
function savedGuestProfile(){
 for(const storage of ["localStorage","sessionStorage"]){
  try{
   const saved=usableGuestProfile(JSON.parse(readGuestStorage(storage,guestProfileKey)||"null"));
   if(saved)return saved;
  }catch{}
 }
 return preferredProfile;
}
function saveIdentity({prefer=false}={}){
 try{sessionStorage.setItem(`friday-ktv-guest:${room}`,JSON.stringify(identity));}catch{}
 const profile=usableGuestProfile(identity);
 if(profile&&(prefer||!savedGuestProfile())){
  preferredProfile=profile;
  const value=JSON.stringify(profile);
  if(!writeGuestStorage("localStorage",guestProfileKey,value))writeGuestStorage("sessionStorage",guestProfileKey,value);
 }
 $("my-name").textContent=identity.nickname;avatar($("my-avatar"),identity.avatar);
 $("profile-button").setAttribute("aria-label",`${identity.nickname}，修改昵称和头像`);
}
function connectionError(error){return ["network_error","request_timeout","service_unavailable"].includes(error?.code)||(!error?.code&&error?.status>=500);}
function connectionNotice(text){const node=$("connection-status");node.textContent=text;node.hidden=!text;}
const usableStatus=value=>Array.isArray(value?.songs)&&value.songs.every(song=>song&&typeof song.id==="string")&&(!value.playback||Array.isArray(value.playback.played));
const requestBudgets={status:3000,events:10000,search:7000,discover:5500,join:7500,profile:6000,controls:5500,command:5500};
async function api(operation,body,query="",{signal,timeoutMs=requestBudgets[operation]||15000,priority="auto",guestKey=identity?.key}={}){
 const controller=new AbortController(),abort=()=>controller.abort();let timedOut=false;
 const timeout=setTimeout(()=>{timedOut=true;controller.abort();},timeoutMs);signal?.addEventListener("abort",abort,{once:true});if(signal?.aborted)abort();
 try{
  const started=performance.now();let response;
  try{response=await fetch(`./api/rooms/${room}/${operation}${query}`,{method:body?"POST":"GET",headers:{"X-KTV-Room":key,...(guestKey?{"X-KTV-Guest":guestKey}:{}),...(body?{"Content-Type":"application/json"}:{})},body:body?JSON.stringify(body):undefined,credentials:"omit",signal:controller.signal,cache:"no-store",priority});}
  catch(error){throw Object.assign(new Error(),{code:signal?.aborted?"cancelled":timedOut?"request_timeout":"network_error",status:0});}
  let value;try{value=await response.json();}catch{throw Object.assign(new Error(),{code:signal?.aborted?"cancelled":timedOut?"request_timeout":"service_unavailable",status:response.status});}if(signal?.aborted)throw Object.assign(new Error(),{code:"cancelled"});
  if(!response.ok){const retry=response.headers?.get("Retry-After"),seconds=Number(retry),retryAfterMs=retry?(Number.isFinite(seconds)?Math.max(0,seconds*1000):Math.max(0,Date.parse(retry)-Date.now())):0;throw Object.assign(new Error(),{code:value.code||"service_unavailable",status:response.status,retryAfterMs});}
  if(operation==="status"){
   if(!usableStatus(value))throw Object.assign(new Error(),{code:"service_unavailable"});
   if(value.server_time_ms&&!earClock.observe(value,started,performance.now()))throw Object.assign(new Error(),{code:"stale_status"});
  }
  return value;
 }finally{clearTimeout(timeout);signal?.removeEventListener("abort",abort);}
}
function failure(error){if(["room_closed","room_unavailable","guest_unavailable"].includes(error.code))addQueue?.stop(error.code);if(connectionError(error)){message(identity?"这次操作未完成，请重试。":"正在连接房间…");if(identity)probeSoon();return;}const text={room_closed:"本场已经结束，扫描电视上的新二维码即可加入。",room_unavailable:"二维码不可用，重新扫描电视上的二维码。",guest_unavailable:"加入信息已失效，重新扫描电视二维码即可。",guest_capacity:"本场加入人数已满，请联系房主。",mac_offline:"房间暂时离线，稍后再点。",queue_full:"歌单有些满，稍后再来。",rate_limited:"操作有些频繁，稍等一下。",search_unavailable:"搜索暂时不可用，稍后再试。",cloud_song_unavailable:"这首已不在云盘中，刷新搜索后再试。",retry_limit:"暂时仍无法下载，换个版本试试。",retry_unavailable:"歌曲状态已变化，请刷新歌单。",tv_offline:"电视暂时未连接，打开电视上的 Friday KTV 后再试。",tv_slow:"电视还未确认这次操作，请查看当前状态。",receipt_unavailable:"房间连接较慢，操作结果待确认。",song_not_ready:"这首还在准备。",lyrics_not_ready:"正在为这首歌找歌词。",queue_changed:"歌单刚有更新，已刷新后可重新调整。",playback_changed:"电视已经换歌，已刷新当前状态。"}[error.code]||"操作未完成，请重试。";message(text);const alert=document.querySelector("dialog[open] [role=alert]");if(alert)alert.textContent=text;if(error.code==="room_closed"||error.code==="room_unavailable"){$("search").hidden=true;$("player").hidden=true;stopRoomStatus(true);try{sessionStorage.removeItem("friday-ktv-room");}catch{}}}
function applyTheme(value){if(!themes.some(t=>t[0]===value))return;theme=value;document.body.dataset.theme=value;document.querySelectorAll(".theme-option").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.theme===theme)));}
function artworkUrl(url){return typeof url==="string"&&/^https:\/\/[a-z0-9.-]+\.music\.126\.net\//.test(url)?url:"";}
function resolvedArtwork(song){return artworkUrl(song.artwork)||(sourceOf(song)==="song"?searchArtwork.get(song.song_id)||"":"");}
function placeholderCover(){const cover=document.createElement("div");cover.className="cover placeholder";cover.setAttribute("aria-hidden","true");cover.textContent="♪";return cover;}
function coverFor(song,visible=false){
 const url=resolvedArtwork(song);if(!url)return placeholderCover();
 const img=document.createElement("img");img.className="cover";img.alt="";img.loading=visible?"eager":"lazy";img.decoding="async";img.referrerPolicy="no-referrer";img.src=url;img.onerror=()=>img.replaceWith(placeholderCover());return img;
}
const resultRowCache=new Map(),resultGroupCache=new Map();
function reconcileChildren(parent,desired){
 const keep=new Set(desired);
 for(const child of Array.from(parent.children))if(!keep.has(child))parent.removeChild(child);
 for(let i=0;i<desired.length;i++)if(parent.children[i]!==desired[i])parent.insertBefore(desired[i],parent.children[i]||null);
}
function render(){tabs();for(const [node]of pendingActions)if(!node.isConnected)busy(node,false);const home=mode==="home";$("discover").hidden=!home;document.querySelector(".list-heading").hidden=mode!=="queue";updateSearchPager();const rows=home?[]:mode==="find"?songs:mode==="frequent"?frequentSongs:[...selected].sort((a,b)=>queueRank(a)-queueRank(b)),container=$("results"),focused=document.activeElement?.dataset.focusId||document.activeElement?.dataset.song;const historyOpen=container.querySelector("details.history")?.open||false;const rootRows=[],groupRows=new Map(),usedRows=new Set(),usedGroups=new Set();$("list-title").textContent="今晚的歌单";$("count").textContent=rows.length?`${rows.length} 首`:"";$("empty").hidden=home||rows.length>0;$("empty").textContent=mode==="find"?"搜一首你想唱的歌。":mode==="frequent"?"从这台手机点过并播放的歌曲会出现在这里。":"还没有已点歌曲，第一首交给你。";
 const nextQueuedId=nextPlayable()?.id;let queueGroup=null,lastQueueGroup="";
 rows.forEach((song,index)=>{if(mode==="queue"){
  const group=queueGroupName(song);
  if(group!==lastQueueGroup){
   const collapsible=group==="history"||group==="failed";
   queueGroup=resultGroupCache.get(group);
   if(!queueGroup){queueGroup=document.createElement(collapsible?"details":"section");queueGroup.className="queue-group "+group;if(group==="history")queueGroup.open=historyOpen;resultGroupCache.set(group,queueGroup);}
   usedGroups.add(group);const heading=queueGroup.children[0]||document.createElement(collapsible?"summary":"h3");
   heading.textContent=({current:"正在唱",pending:"接下来",preparing:"正在准备",failed:"准备失败",history:"唱过了"})[group];
   const count=document.createElement("span");count.textContent=String(rows.filter(s=>queueGroupName(s)===group).length);
   heading.append(count);groupRows.set(queueGroup,[heading]);rootRows.push(queueGroup);lastQueueGroup=group;
  }
 }
 const rowKey=mode==="queue"?`queue:${song.id}`:`${mode}:${songKey(song)}`,signature=JSON.stringify([song,mode,song.id===playback.current?playback.state:"",playback.played.includes(song.id),song.id===nextQueuedId]);usedRows.add(rowKey);
 const previousRow=resultRowCache.get(rowKey);if(previousRow?.signature===signature){if(mode==="queue")groupRows.get(queueGroup).push(previousRow.row);else rootRows.push(previousRow.row);return;}
 const row=document.createElement("article");row.className="song";row.dataset.renderKey=rowKey;if(mode==="queue"){row.classList.add("queue-song");row.dataset.queue=song.id;if(song.id===playback.current)row.classList.add("current");}const firstCovers=mode==="find"&&index<7;row.dataset.firstCovers=firstCovers?"1":"0";row.append(coverFor(song,firstCovers));const details=document.createElement("div");details.className="details";const title=document.createElement("div");title.className="song-title";title.textContent=song.title||`歌曲 ${song.song_id}`;details.append(title);if(sourceOf(song)!=="song"){const badge=document.createElement("span");badge.className="source-badge";badge.textContent=sourceOf(song)==="cloud"?"云盘":"MV";details.append(badge);}const artist=document.createElement("div");artist.className="artist";artist.textContent=(song.artists||[]).join(" / ");if(artist.textContent)details.append(artist);
 if(mode==="queue"){const by=document.createElement("div");by.className="requester";const picture=document.createElement("span");picture.className="avatar";picture.setAttribute("aria-hidden","true");avatar(picture,song.avatar);const label=document.createElement("span");label.textContent=`${song.nickname||"朋友"} 点的`;by.append(picture,label);details.append(by);if(!["ready","cancelled"].includes(song.state)){const process=document.createElement("div");process.className="preparation";const p=song.progress||{},steps=["原曲","伴奏","播放文件","MV"];const label=document.createElement("p");label.className="prepare-label";const stage={queued:"等待准备",acquiring:"获取原曲",waiting_storage:"等待 Mac 腾出缓存空间",waiting_separation:"等待制作伴奏",waiting_memory:"等待制作伴奏",separating:"正在生成伴奏",waiting_transcode:"伴奏已完成",transcoding:"准备播放文件",video:"匹配 MV"}[p.stage]||labels[song.state];label.textContent=song.state==="failed"?(song.code==="download_timeout"?"原曲下载未完成，请重试。":({source_login_required:"音源尚未连接，请联系房主。",source_rights_denied:"当前账户无法下载这个版本，请换一个版本。",source_api_unavailable:"音源服务暂时不可用，请稍后重试。",source_file_invalid:"这首暂时无法播放，请重试或换个版本。",storage_full:"Mac 缓存空间不足，释放空间后重试。"}[song.code]||"本次准备未完成，可重试或换一个版本。")):stage;process.append(label);const rail=document.createElement("div");rail.className="prepare-steps";rail.setAttribute("aria-label",`处理步骤：${stage}`);steps.forEach((name,i)=>{const step=document.createElement("span");step.textContent=name;step.className=i<(p.completed_steps||0)?"complete":i===(p.completed_steps||0)?"active":"";rail.append(step);});process.append(rail);if(p.source_total>0&&p.source_phase==="source_downloading"){const progress=document.createElement("progress");progress.max=p.source_total;progress.value=Math.min(p.source_bytes||0,p.source_total);progress.setAttribute("aria-label","原曲下载进度");process.append(progress);const bytes=document.createElement("p");bytes.className="prepare-wait";bytes.textContent=`已下载 ${Math.floor(progress.value/p.source_total*100)}%`;process.append(bytes);}details.append(process);}}row.append(details);
 if(mode==="find"||mode==="frequent"){const button=document.createElement("button");button.className="add";button.dataset.song=songKey(song);button.dataset.title=song.title;button.onclick=()=>add(song,button);row.append(button);}else{const state=document.createElement("span");state.className="state";state.textContent=song.id===playback.current?(playback.state==="playing"?"正在唱":playback.state==="preparing"?"加载中":"已暂停"):playback.played.includes(song.id)?"唱过了" :song.lyric_state===-1?"暂无同步歌词":song.lyrics_pending?"正在找歌词":(song.state==="failed"?({download_timeout:"下载超时",download_unavailable:"暂无法下载"}[song.code]||"准备失败"):labels[song.state]||"准备中");if(song.id===nextQueuedId&&!playback.played.includes(song.id))state.textContent="下一首 · "+state.textContent;row.append(state);const tools=document.createElement("div");tools.className="queue-tools";if(song.state==="failed"){const retry=document.createElement("button");retry.className="retry-song";retry.textContent="重试";retry.setAttribute("aria-label",`重新准备：${song.title}`);retry.onclick=async()=>{retry.disabled=true;busy(retry,true,"正在重新准备…");try{await api("retry",{id:song.id,request_id:crypto.randomUUID()});toast("正在重新准备");await refresh();}catch(error){failure(error);}finally{retry.disabled=false;busy(retry,false);}};tools.append(retry);}if(playback.played.includes(song.id)||song.state==="cancelled"){const repeat=document.createElement("button");repeat.className="repeat-song";repeat.dataset.song=songKey(song);repeat.dataset.title=song.title;repeat.textContent=alreadyQueued(song)?"已加入":"再唱一遍";repeat.disabled=alreadyQueued(song)||pendingAdds.has(songKey(song))||!online;repeat.setAttribute("aria-label",`再唱一遍：${song.title}`);repeat.onclick=()=>add(song,repeat);tools.append(repeat);}const more=document.createElement("button");more.className="more-song";more.textContent="···";more.setAttribute("aria-label",`调整歌曲：${song.title}`);more.onclick=()=>songActions(song);if(!playback.played.includes(song.id)&&song.state!=="cancelled")tools.append(more);if(song.id!==playback.current&&!playback.played.includes(song.id)&&!["failed","cancelled"].includes(song.state)){const handle=document.createElement("button");handle.className="reorder-handle";handle.textContent="≡";handle.setAttribute("aria-label",`拖动排序：${song.title}，也可按确定打开排序操作`);handle.onclick=()=>{if(!handle.dataset.moved)songActions(song);delete handle.dataset.moved;};handle.onpointerdown=event=>startDrag(event,song,handle);tools.append(handle);}row.append(tools);}for(const b of row.querySelectorAll("button"))b.dataset.focusId=song.id?`${song.id}:${b.className.split(" ")[0]}`:b.dataset.song;resultRowCache.set(rowKey,{row,signature});if(mode==="queue")groupRows.get(queueGroup).push(row);else rootRows.push(row);});
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
 try{initAddQueue();const item=addSnapshot.items.find(v=>v.key===songKey(song));if(item?.state==="failed")addQueue.retry(item.key);else addQueue.enqueue({...song,source:sourceOf(song),artwork:resolvedArtwork(song)});if(statusFailures)toast("已暂存，连接恢复后加入");}
 catch(error){if(error.code==="local_queue_full")message("待加入的歌曲已满，等前面的歌曲加入后再点。");else failure(error);}
}
function readinessHint(value){
 const ready=value.readiness,g=ready?.gates;
 if(!g||ready.state==="closed"||ready.state==="unavailable")return value.mac_online?"":"歌曲准备服务暂时离线，歌单会保留。";
 if(ready.ready===true)return "";
 if(g.link_tv===false)return "电视暂未连接，歌单会保留。";
 if(g.tv_playback===false)return "电视已连接，暂时不能播放。";
 if(g.tv_media===false)return "电视正在准备播放。";
 if(["link_worker","worker_source","worker_model","worker_download","worker_lyrics","worker_media"].some(k=>g[k]===false))return "歌曲准备服务正在连接，歌单会保留。";
 return ["nas_api","nas_data","nas_media","qr"].some(k=>g[k]===false)?"正在准备本场，已点歌单会保留。":"";
}
function applyStatus(value){
 if(!usableStatus(value))return false;
 statusFailures=0;lastStatusSuccess=performance.now();connectionNotice("");online=value.mac_online;
 playbackAvailable=value.readiness?.gates?.tv_playback!==false;
 const changed=JSON.stringify(selected)!==JSON.stringify(value.songs);if(!drag&&!ordering){selected=value.songs;queueRevision=value.queue_revision||0;playableOrder=Array.isArray(value.playable)?value.playable:null;}
 playback=value.playback||playback;controls={...controls,...(value.controls||{})};addQueue?.observe({songs:value.songs,played:playback.played});addQueue?.wake();
 const frequentChanged=rememberFrequent(value.songs);
 applyTheme(value.theme);updatePlayer();tabs();$("hint").textContent=readinessHint(value);
 if(mode==="queue"&&(changed||playbackSignature!==queuePlaybackSignature())&&!drag&&!ordering){playbackSignature=queuePlaybackSignature();render();}else if(mode==="frequent"&&frequentChanged)render();else updateAddButtons();
}
function statusFailure(error,channel="snapshot"){
 if(error.code==="stale_status"||error.code==="cancelled")return;
 if(channel==="events"&&error.code==="guest_unavailable"){
  stopRoomStatus(true);statusFeed=null;join();return;
 }
 if(connectionError(error)||error.code==="rate_limited"){
  if(channel==="events"){probeSoon();return;}
  if(performance.now()-Math.max(lastStatusSuccess,roomResumeAt)<roomStaleMs)return;
  if(!statusFailures){statusFailures=1;online=false;playback={...playback,online:false};updatePlayer();updateAddButtons();}
  connectionNotice("房间连接中断，正在重连。");
  return;
 }
 failure(error);
}
function refresh({signal}={}){
 if(!valid||document.hidden||signal?.aborted)return Promise.resolve(false);
 if(statusRequest)return statusRequest.task;
 lastStatusProbe=performance.now();
 const request={controller:new AbortController(),task:null};statusRequest=request;
 request.task=api("status",null,"",{signal:request.controller.signal}).then(value=>{
  if(statusRequest!==request||document.hidden||request.controller.signal.aborted)return false;
  applyStatus(value);return true;
 },error=>{
  if(statusRequest!==request||document.hidden||request.controller.signal.aborted)return false;
  statusFailure(error);
  return error.code==="stale_status";
 }).catch(()=>{message("房间状态无法显示，请刷新重试。");return false;}).finally(()=>{if(statusRequest===request)statusRequest=null;});
 return request.task;
}
function probeSoon(){if(performance.now()-lastStatusProbe>=1200)refresh();}
function stopRoomStatus(close=false){
 if(close)statusFeed?.close();else statusFeed?.stop();
 const request=statusRequest;statusRequest=null;request?.controller.abort();
 clearInterval(refreshTimer);
}
function startStatusFeed(){
 if(!identity||document.hidden)return;
 if(typeof FridayStatusFeed!=="function"){clearInterval(refreshTimer);refreshTimer=setInterval(probeSoon,2000);return;}
 if(!statusFeed)statusFeed=new FridayStatusFeed({
  probeEvery:4500,pollEvery:300,
  events:({cursor,signal})=>api("events",null,"?after="+cursor+"&wait=3",{signal}),probe:options=>refresh(options),visible:()=>!document.hidden,
  onStatus:value=>{if(!usableStatus(value)){probeSoon();return;}const now=performance.now();if(earClock.observe(value,now,now,{calibrate:false}))applyStatus(value);},onFailure:error=>statusFailure(error,"events")
 });
 statusFeed.start();
}
let searchController=null,searchTimer=null,searchComposing=false,nextPage=null,pagingNow=false,pagingFailed=false;
const searchArtwork=new Map(),artworkPending=new Set(),artworkAttempted=new Set();
let artworkController=null;
function stopArtwork(){
 artworkController?.abort();artworkController=null;artworkPending.clear();artworkAttempted.clear();
}
function queueArtwork(rows){
 if(!identity||mode!=="find"||document.hidden)return;
 for(const song of rows)if(sourceOf(song)==="song"&&/^[1-9][0-9]{0,17}$/.test(song.song_id)&&!resolvedArtwork(song)&&!artworkAttempted.has(song.song_id))artworkPending.add(song.song_id);
 enrichArtwork();
}
async function enrichArtwork(){
 if(artworkController||!artworkPending.size)return;
 const controller=new AbortController(),ids=[...artworkPending].slice(0,30);
 artworkController=controller;
 for(const id of ids){artworkPending.delete(id);artworkAttempted.add(id);}
 try{
  const {covers}=await api("artwork",null,"?"+new URLSearchParams({ids:ids.join(",")}),{signal:controller.signal,timeoutMs:4000,priority:"low"});
  if(controller.signal.aborted||mode!=="find")return;
  for(const id of ids){
   const url=artworkUrl(covers?.[id]);if(!url)continue;
   searchArtwork.set(id,url);if(searchArtwork.size>240)searchArtwork.delete(searchArtwork.keys().next().value);
   const row=resultRowCache.get("find:song:"+id)?.row;
   if(row?.firstElementChild?.classList.contains("placeholder"))row.firstElementChild.replaceWith(coverFor({song_id:id,source:"song"},row.dataset.firstCovers==="1"));
  }
 }catch{
  // 封面暂未取到时保留占位图，歌曲仍可以点。
 }finally{if(artworkController===controller){artworkController=null;enrichArtwork();}}
}
const searchPages=new Map();
const pagingObserver=typeof IntersectionObserver==="function"?new IntersectionObserver(entries=>{
 if(entries.some(entry=>entry.isIntersecting))appendNextPage();
},{rootMargin:"0px 0px 420px"}):null;
function updateSearchPager(){
 const more=mode==="find"&&searchMore&&!searchBusy;
 $("search-page-end").hidden=!more;
 $("page-progress").hidden=!more||!pagingNow;
 $("page-progress").textContent=pagingNow?"正在加载下一页…":"";
 $("load-more").hidden=!more||pagingNow||(!pagingFailed&&!!pagingObserver);
 $("load-more").textContent=pagingFailed?"重试下一页":"加载更多";
}
function observeSearchEnd(){
 if(!pagingObserver)return;
 pagingObserver.disconnect();
 if(mode==="find"&&searchMore&&!searchBusy&&!pagingFailed)pagingObserver.observe($("search-page-end"));
}
function resetSearchBusy(){searchBusy=false;const b=$("search").querySelector("button");b.disabled=false;busy(b,false);updateSearchPager();}
function cancelSearchInput(){
 const wasBusy=searchBusy;
 clearTimeout(searchTimer);searchTimer=null;searchController?.abort();searchController=null;
 nextPage?.controller.abort();nextPage=null;pagingNow=false;pagingFailed=false;stopArtwork();
 pagingObserver?.disconnect();searchSequence++;resetSearchBusy();
 if(wasBusy&&$("message").textContent==="正在找歌…")message("");
}
function scheduleSearch(){cancelSearchInput();if(searchComposing||mode!=="find"||document.hidden)return;searchTimer=setTimeout(()=>{searchTimer=null;findSongs();},180);}
async function searchPage(q,kind,list,offset,signal,prefetch=false){
 const operation=list?"playlist":"search";
 const params=list?new URLSearchParams({id:list,offset}):new URLSearchParams({q,source:kind,offset});
 const cacheKey=operation+"?"+params;
 const hit=searchPages.get(cacheKey);
 if(hit&&performance.now()-hit.at<45000){searchPages.delete(cacheKey);searchPages.set(cacheKey,hit);return hit.value;}
 const value=await api(operation,null,"?"+params,{signal,priority:prefetch?"low":"auto"});
 if(!signal.aborted&&(kind!=="cloud"||value.updated)){
  searchPages.delete(cacheKey);searchPages.set(cacheKey,{at:performance.now(),value});
  if(searchPages.size>24)searchPages.delete(searchPages.keys().next().value);
 }
 return value;
}
function applySearchPage(value,offset,append){
 const seen=new Set(append?songs.map(songKey):[]);
 const rows=value.songs.filter(song=>{const key=songKey(song);if(seen.has(key))return false;seen.add(key);return true;});
 songs=append?[...songs,...rows]:rows;
 searchOffset=Number.isInteger(value.next_offset)?value.next_offset:offset+30;
 searchMore=value.has_more===true&&searchOffset>offset&&searchOffset<=100000;
 render();queueArtwork(rows);
}
function prefetchNextPage(){
 if(mode!=="find"||document.hidden||!searchMore||searchBusy||searchTimer||searchComposing||pagingFailed)return null;
 if(nextPage)return nextPage;
 const page={offset:searchOffset,sequence:searchSequence,controller:new AbortController()};
 page.promise=searchPage(searchQuery,source,playlistId,page.offset,page.controller.signal,true).then(value=>{
  if(!page.controller.signal.aborted&&page.sequence===searchSequence&&Array.isArray(value.songs))queueArtwork(value.songs);
  return {value};
 },error=>({error}));
 nextPage=page;
 return page;
}
async function appendNextPage(retry=false){
 if(pagingNow||mode!=="find"||document.hidden||!searchMore||searchTimer||searchComposing)return;
 if(retry&&pagingFailed){nextPage=null;pagingFailed=false;}
 const page=prefetchNextPage();if(!page)return;
 pagingNow=true;updateSearchPager();
 const result=await page.promise;
 if(nextPage!==page||page.sequence!==searchSequence||page.controller.signal.aborted)return;
 pagingNow=false;nextPage=null;
 if(result.error){pagingFailed=true;updateSearchPager();return;}
 applySearchPage(result.value,page.offset,true);
 prefetchNextPage();observeSearchEnd();
}
async function findSongs(more=false,collection=null){
 if(more)return appendNextPage(true);
 cancelSearchInput();playlistId=collection?.id||"";playlistTitle=collection?.title||"";
 const q=$("query").value.trim();
 if(!q&&source!=="cloud"&&!playlistId){searchQuery="";searchMore=false;songs=[];message("");render();return;}
 const sequence=searchSequence,requestedSource=source,requestedPlaylist=playlistId,controller=new AbortController();
 searchController=controller;searchBusy=true;$("search").querySelector("button").disabled=true;updateSearchPager();message("正在找歌…");
 try{
  const value=await searchPage(q,requestedSource,requestedPlaylist,0,controller.signal);
  if(controller.signal.aborted||sequence!==searchSequence||source!==requestedSource||playlistId!==requestedPlaylist)return;
  searchQuery=q;applySearchPage(value,0,false);
  message(songs.length?"":requestedSource==="cloud"&&!value.updated?"云盘正在连接，稍后再试。":"没有找到，换个歌名或歌手试试。");
 }catch(error){
  if(!controller.signal.aborted&&sequence===searchSequence&&mode==="find"){
   searchMore=false;updateSearchPager();
   if(connectionError(error)||["search_unavailable","search_busy","rate_limited"].includes(error.code))message("搜索暂时未完成，可以点搜索重试。");
   else failure(error);
  }
 }finally{if(searchController===controller)searchController=null;if(sequence===searchSequence){resetSearchBusy();prefetchNextPage();observeSearchEnd();}}
}
function resumeSearchView(){
 if(mode!=="find")return;
 if(!playlistId&&$("query").value.trim()!==searchQuery)scheduleSearch();
 else{queueArtwork(songs);prefetchNextPage();observeSearchEnd();}
}
function searchArtist(name){setView("find");source="song";$("query").value=name;updateSources();findSongs();}
function updateSources(){$("query").placeholder=source==="cloud"?"搜索云盘歌曲或歌手":source==="mv"?"搜索 MV 或歌手":"搜索歌曲或歌手";for(const [button,kind]of[["source-song","song"],["source-mv","mv"],["source-cloud","cloud"]])$(button).setAttribute("aria-pressed",String(source===kind));}
async function loadDiscover(showProgress=false){if(showProgress)busy($("refresh-discover"),true,"正在加载推荐…");try{const value=await api("discover");recommendations=value;$("artist-picker").replaceChildren();for(const name of value.artists){const b=document.createElement("button");b.type="button";const portrait=document.createElement("span");portrait.className="artist-portrait";portrait.setAttribute("aria-hidden","true");portrait.textContent=name.slice(0,1);const image=value.artist_photos?.[name];if(image&&/^https:\/\/[a-z0-9.-]+\.music\.126\.net\//.test(image)){const img=document.createElement("img");img.src=image;img.alt="";img.loading="lazy";img.referrerPolicy="no-referrer";img.onerror=()=>img.remove();portrait.append(img);}const label=document.createElement("span");label.textContent=name;b.append(portrait,label);b.onclick=()=>searchArtist(name);$("artist-picker").append(b);}renderPlaylists();$("discover-status").textContent=value.playlists.length?"":"歌单暂时未加载，点歌手或直接搜索也能点歌。";}catch{$("discover-status").textContent="推荐暂时未加载，稍后重试。";}finally{busy($("refresh-discover"),false);}}
function renderPlaylists(){const picker=$("playlist-picker");picker.replaceChildren();for(const list of recommendations.playlists.slice(playlistPage*4,playlistPage*4+4)){const b=document.createElement("button");b.type="button";b.className="playlist-card";b.append(coverFor(list));const title=document.createElement("strong");title.textContent=list.title;b.append(title);b.onclick=()=>{setView("find");source="song";updateSources();$("query").value="";findSongs(false,list);};picker.append(b);}}
$("refresh-discover").onclick=()=>{if(!recommendations)return loadDiscover(true);playlistPage=(playlistPage+1)%Math.max(1,Math.ceil(recommendations.playlists.length/4));renderPlaylists();};
$("load-more").onclick=()=>findSongs(true);
$("search").addEventListener("submit",event=>{event.preventDefault();if(!searchComposing&&!event.isComposing)findSongs();});
$("query").oncompositionstart=()=>{searchComposing=true;cancelSearchInput();};
$("query").oncompositionend=()=>{searchComposing=false;scheduleSearch();};
$("query").oninput=event=>{if(event?.isComposing){searchComposing=true;cancelSearchInput();}else scheduleSearch();};
for(const [id,value]of[["source-song","song"],["source-mv","mv"],["source-cloud","cloud"]])$(id).onclick=()=>{source=value;songs=[];playlistId="";playlistTitle="";searchQuery="";searchMore=false;searchSequence++;for(const [button,kind]of[["source-song","song"],["source-mv","mv"],["source-cloud","cloud"]])$(button).setAttribute("aria-pressed",String(source===kind));$("query").placeholder=source==="cloud"?"搜索云盘歌曲或歌手":source==="mv"?"搜索 MV 或歌手":"搜索歌曲或歌手";render();findSongs();};
const viewScroll={home:0,find:0,queue:0,frequent:0};
function tabs(){
 const count=selected.filter(isUpcoming).length;
 document.body.dataset.view=mode;
 $("search-entry").hidden=$("frequent-entry").hidden=!identity||mode!=="home";
 $("search").hidden=!identity||mode!=="find";$("source-picker").hidden=!identity||mode!=="find";
 $("view-queue").hidden=!identity||mode!=="find";$("search-queue-count").textContent=`已点 ${count} 首`;
 $("queue-count").textContent=count;$("queue-tab").disabled=!identity;
 $("page-heading").hidden=mode==="home";$("page-title").textContent=mode==="queue"?"已点歌曲":mode==="frequent"?"常唱歌曲":playlistTitle||"搜索";
}
function setView(next,{push=true,focus}={}){
 if(!["home","find","queue","frequent"].includes(next)||mode===next)return;
 const previous=mode;viewScroll[mode]=window.scrollY;mode=next;if(previous==="find"&&next!=="find"){searchComposing=false;cancelSearchInput();}
 if(push)history.pushState({ktvView:next},"");
 message("");tabs();render();
 const focusId=focus||(next==="home"?(previous==="queue"?"queue-tab":previous==="frequent"?"frequent-entry":"search-entry"):"page-title");
 // Focus in the gesture task so opening search keeps the mobile keyboard active.
 $(focusId).focus({preventScroll:true});
 requestAnimationFrame(()=>{if(mode===next)window.scrollTo({top:viewScroll[next],behavior:"instant"});});
 if(next==="queue")refresh();else if(next==="find")resumeSearchView();
}
history.replaceState({ktvView:"home"},"");
window.addEventListener("popstate",event=>setView(event.state?.ktvView||"home",{push:false}));
$("page-back").onclick=()=>{if(history.state?.ktvView&&mode!=="home")history.back();else setView("home",{push:false});};
$("search-entry").onclick=()=>setView("find",{focus:"query"});
$("frequent-entry").onclick=()=>setView("frequent");
document.querySelector(".brand").onclick=event=>{event.preventDefault();setView("home");};
$("queue-tab").onclick=()=>setView("queue");$("view-queue").onclick=()=>setView("queue");
function openDialog(id){const dialog=$(id);if(dialog.showModal)dialog.showModal();else dialog.setAttribute("open","");}
function chooseAvatar(id){chosenAvatar=id;document.querySelectorAll(".avatar-choice").forEach((b,i)=>b.setAttribute("aria-pressed",String(i===id)));}
for(let i=0;i<12;i++){const b=document.createElement("button");b.type="button";b.className="avatar-choice";b.setAttribute("aria-label",`选择${names[i]}头像`);b.setAttribute("aria-pressed","false");const picture=document.createElement("span");picture.className="avatar";picture.setAttribute("aria-hidden","true");avatar(picture,i);b.append(picture);b.onclick=()=>chooseAvatar(i);$("avatar-picker").append(b);}
$("profile-button").onclick=()=>{if(!identity)return;$("nickname").value=identity.nickname;chooseAvatar(identity.avatar);$("profile-error").textContent="";openDialog("profile-dialog");};
$("random-profile").onclick=()=>{const [picture,word]=crypto.getRandomValues(new Uint32Array(2)),choices=[...names,...extraNames];chooseAvatar(picture%names.length);$("nickname").value=choices[word%choices.length];};
$("save-profile").onclick=async()=>{const name=$("nickname").value.trim();if(!name){$("profile-error").textContent="起一个昵称再保存。";$("nickname").focus();return;}const b=$("save-profile");b.disabled=true;busy(b,true,"正在保存…");try{const result=await api("profile",{nickname:name,avatar:chosenAvatar});identity={...identity,...result};saveIdentity({prefer:true});$("profile-dialog").close();toast("就用这个名字，开唱吧");await refresh();}catch{$("profile-error").textContent="还没保存，检查连接后再试。";}finally{b.disabled=false;busy(b,false);}};
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
const volumeControls=[["music","music_gain"],["sfx","sfx_gain"]];
function updateMuteIcon(button,muted,confirmed,note,status){
 button.dataset.muted=String(muted);button.setAttribute("aria-pressed",confirmed?String(muted):"mixed");
 if(note.textContent!==status)note.textContent=status;
}
function updateMuteButtons(){
 const level=pendingControls.music_gain??controls.music_gain;
 roomMusicVolume.remember(level);const musicMuted=level===0;
 const musicConfirmed=playback.online&&pendingControls.music_gain===undefined&&!controlPendingVersions.has($("music-mute-button"))&&playback.applied_revision>=controls.control_revision;
 $("music-mute-button").disabled=!playback.online;
 updateMuteIcon($("music-mute-button"),musicMuted,musicConfirmed,$("music-mute-state"),!playback.online?"电视未连接，音乐音量尚未确认":!musicConfirmed?"已选择音乐音量，等待电视确认":musicMuted?"音乐已静音，按下恢复静音前音量":"音乐未静音，按下静音");

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
function isUpcoming(song){return song.id!==playback.current&&!playback.played.includes(song.id)&&!["failed","cancelled"].includes(song.state);}
function isPlayable(song){return song.state==="ready"&&(playableOrder?playableOrder.includes(song.id):!song.lyrics_pending);}
function nextPlayable(){return selected.find(s=>isUpcoming(s)&&isPlayable(s));}
function queueGroupName(song){
 if(song.id===playback.current)return "current";
 if(playback.played.includes(song.id)||song.state==="cancelled")return "history";
 if(song.state==="failed")return "failed";
 return isPlayable(song)?"pending":"preparing";
}
function queueRank(song){return ["current","pending","preparing","failed","history"].indexOf(queueGroupName(song));}
function updatePlayer(){updateGuide();updatePartyAudio();
 $("player").hidden=!identity;document.body.classList.toggle("has-player",!!identity);const current=selected.find(s=>s.id===playback.current);const queued=selected.some(isUpcoming),next=nextPlayable();$("now-title").textContent=current?.title||(next?"接下来："+next.title:queued?"正在准备下一首":playback.state==="playing"&&playback.online?"暖场":"准备好，唱下一首");
 $("player-status").textContent=!playback.online?(statusFailures?"正在连接房间":"等待电视连接"):playback.state==="playing"?(current?(current.nickname||"朋友")+" 正在唱":"暖场播放中"):playback.state==="preparing"?"正在加载音源":"已暂停";
 const transportReady=playback.online&&playbackAvailable;
 const playing=playback.state==="playing"||playback.state==="preparing";$("play-toggle").textContent=playing?"Ⅱ":"▶";$("play-toggle").setAttribute("aria-label",playing?"暂停播放":"开始播放");$("play-toggle").disabled=!transportReady||commandBusy;$("skip-button").disabled=!transportReady||(!playback.current&&(!queued||playback.state==="preparing"))||commandBusy;$("restart-button").disabled=!transportReady||!playback.current||commandBusy;
 for(const [id,value]of[["mode-instrumental",0],["mode-original",1]]){$(id).setAttribute("aria-pressed",String((controls.original||0)===value));$(id).disabled=!playback.online;}

 for(const [id,key]of volumeControls){const input=$(`${id}-volume`);input.disabled=!playback.online||(id==="sfx"&&playback.audio_capabilities?.sfx!==true);const level=pendingControls[key]??controls[key];updateVolumeButtons(id,level);if(document.activeElement!==input){input.value=level;$(`${id}-value`).textContent=`${level}%`;}}
 updateMuteButtons();
}
async function sendCommand(action,song="",origin=""){if(commandBusy||!playbackAvailable)return;const before={...playback};commandBusy=true;const button=$(origin||(action==="next"?"skip-button":"play-toggle"));busy(button,true,origin==="restart-button"?"正在从头开始…":action==="next"?"正在切歌…":action==="pause"?"正在暂停…":"正在开始播放…");updatePlayer();try{const result=await api("command",{action,song,request_id:crypto.randomUUID()});await waitForTV(()=>FridayPartyAudio.commandObserved(action,song,before,playback,result.seq));}catch(error){failure(error);}finally{commandBusy=false;busy(button,false);await refresh();updatePlayer();}}
$("mode-instrumental").onclick=()=>setControl({original:0});$("mode-original").onclick=()=>setControl({original:1});
let controlWrites=Promise.resolve(),controlSerial=0;const controlPendingVersions=new Map();
const controlFields={original:["mode-original","mode-instrumental"],music_gain:["music-value","music-mute-button"],sfx_gain:["sfx-value"]};
function setControl(value,{signal,propagate=false}={}){
 const version=++controlSerial,fields=Object.keys(value),nodes=fields.flatMap(field=>controlFields[field].map($));
 const errorNode=$(Object.hasOwn(value,"sfx_gain")?"sfx-error":"sound-error");
 for(const node of nodes){controlPendingVersions.set(node,version);busy(node,true,"正在调整声音…");}
 // HTTP 写入串行；等待电视确认不阻塞后续设置。
 const request=controlWrites.catch(()=>{}).then(async()=>{if(signal?.aborted)throw Object.assign(new Error(),{code:"cancelled"});const result=await api("controls",value,"",{signal});if(signal?.aborted)throw Object.assign(new Error(),{code:"cancelled"});controls={...controls,...value,control_revision:Math.max(controls.control_revision||0,result.revision||0)};return result;});controlWrites=request;
 return request.then(result=>waitForTV(()=>playback.applied_revision>=result.revision,signal)).then(()=>{errorNode.textContent="";}).catch(error=>{if(error.code==="cancelled"){if(propagate)throw error;return;}errorNode.textContent=["tv_slow","receipt_unavailable"].includes(error.code)?"电视操作结果待确认。":"暂时无法调整，请重试。";failure(error);if(propagate)throw error;}).finally(()=>{for(const field of fields)for(const id of controlFields[field]){const node=$(id);if(controlPendingVersions.get(node)!==version)continue;controlPendingVersions.delete(node);if(id==="music-value"||id==="sfx-value")delete pendingControls[field];busy(node,false);}updatePlayer();});
}
$("sound-button").onclick=()=>{updatePlayer();openDialog("sound-dialog");};$("play-toggle").onclick=()=>sendCommand(["playing","preparing"].includes(playback.state)?"pause":"resume",playback.current);$("skip-button").onclick=()=>sendCommand("next",playback.current);$("restart-button").onclick=()=>sendCommand("play",playback.current,"restart-button");
for(const [id,key]of volumeControls){const input=$(`${id}-volume`);input.oninput=()=>changeGain(id,key,Number(input.value));$(`${id}-minus`).onclick=()=>changeGain(id,key,Number(pendingControls[key]??controls[key])-10,0);$(`${id}-plus`).onclick=()=>changeGain(id,key,Number(pendingControls[key]??controls[key])+10,0);}
function updateVolumeButtons(id,level){$(`${id}-minus`).disabled=level<=0||!playback.online||(id==="sfx"&&playback.audio_capabilities?.sfx!==true);$(`${id}-plus`).disabled=level>=100||!playback.online||(id==="sfx"&&playback.audio_capabilities?.sfx!==true);}
const sfxCommands=new FridayPartyAudio.SfxCommands({send:payload=>api("command",payload),onState:state=>{$("sfx-status").textContent=FridayPartyAudio.sfxMessage(state);}});
function updatePartyAudio(){
 const supported=playback.audio_capabilities?.sfx===true;
 for(const id of["sfx-applause","sfx-cheer"])$(id).disabled=!playback.online||!supported;$("sfx-stop").disabled=!playback.online||(!supported&&!playback.sfx_seq&&!sfxCommands.state.serial);
 sfxCommands.observe(playback);
 $("sfx-status").textContent=!playback.online?"等待电视连接":!supported?"电视暂未提供氛围音效":FridayPartyAudio.sfxMessage(sfxCommands.state);
}
async function trackSfx(serial){const end=performance.now()+7500;while(serial===sfxCommands.state.serial&&performance.now()<end&&["sending","sent"].includes(sfxCommands.state.phase)){probeSoon();await new Promise(resolve=>setTimeout(resolve,125));}sfxCommands.observe(playback);}
for(const [id,action]of[["sfx-applause","sfx_applause"],["sfx-cheer","sfx_cheer"],["sfx-stop","sfx_stop"]])$(id).onclick=()=>{
 $("sfx-error").textContent="";const request=sfxCommands.trigger(action),serial=sfxCommands.state.serial;
 request.then(()=>trackSfx(serial)).catch(error=>{$("sfx-error").textContent=error.code==="sfx_unavailable"?"电视暂未提供氛围音效":"暂未发送成功，请重试";});
};
$("atmosphere-button").onclick=()=>{updatePlayer();openDialog("atmosphere-dialog");};
function songActions(song){actionSong=song;$("song-action-title").textContent=song.title;$("sing-now").textContent=song.id===playback.current?"重新演唱":"现在唱";$("song-error").textContent="";$("sing-now").disabled=(!isPlayable(song)&&song.id!==playback.current)||!playback.online||!playbackAvailable;const pending=song.id!==playback.current&&!playback.played.includes(song.id)&&!["failed","cancelled"].includes(song.state);for(const id of["sing-next","move-up","move-down"])$(id).disabled=!pending;openDialog("song-dialog");}
async function saveOrder(ids,revision=queueRevision){if(ordering)return;ordering=true;busy($("queue-tab"),true,"正在调整顺序…");try{await api("order",{ids,revision});toast("顺序已更新");}catch(error){failure(error);}finally{ordering=false;busy($("queue-tab"),false);await refresh();render();}}
function moveSong(id,direction){const ids=selected.map(s=>s.id),pending=selected.filter(s=>s.id!==playback.current&&!playback.played.includes(s.id)&&!["failed","cancelled"].includes(s.state)).map(s=>s.id);const at=pending.indexOf(id);if(at<0)return;let next=direction==="next"?0:Math.max(0,Math.min(pending.length-1,at+direction));if(at===next)return;const target=pending[next];ids.splice(ids.indexOf(id),1);ids.splice(ids.indexOf(target)+(next>at?1:0),0,id);saveOrder(ids);}
$("sing-now").onclick=()=>{if(!actionSong)return;$("song-dialog").close();sendCommand("play",actionSong.id);};$("sing-next").onclick=()=>{if(!actionSong)return;$("song-dialog").close();moveSong(actionSong.id,"next");};$("move-up").onclick=()=>{if(actionSong)moveSong(actionSong.id,-1);};$("move-down").onclick=()=>{if(actionSong)moveSong(actionSong.id,1);};
function startDrag(event,song,handle){if(event.button!==0||ordering)return;const row=handle.closest("article");drag={id:song.id,revision:queueRevision,startY:event.clientY,moved:false};handle.setPointerCapture(event.pointerId);
 handle.onpointermove=e=>{if(!drag)return;if(Math.abs(e.clientY-drag.startY)>6){drag.moved=true;handle.dataset.moved="1";row.classList.add("dragging");}if(!drag.moved)return;const target=document.elementFromPoint(e.clientX,e.clientY)?.closest("article[data-queue]");if(!target||target===row)return;const id=target.dataset.queue;if(id===playback.current||playback.played.includes(id))return;const from=selected.findIndex(s=>s.id===song.id),to=selected.findIndex(s=>s.id===id);if(from<0||to<0)return;const [item]=selected.splice(from,1);selected.splice(to,0,item);if(from<to)target.after(row);else target.before(row);if(e.clientY>innerHeight-170)scrollBy(0,18);else if(e.clientY<100)scrollBy(0,-18);};
 const finish=cancelled=>{if(!drag)return;const result=drag;drag=null;row.classList.remove("dragging");handle.onpointermove=null;handle.onpointerup=null;handle.onpointercancel=null;if(result.moved&&!cancelled)saveOrder(selected.map(s=>s.id),result.revision);else refresh();};handle.onpointerup=()=>finish(false);handle.onpointercancel=()=>finish(true);
}
const hexBytes=bytes=>Array.from(bytes,byte=>byte.toString(16).padStart(2,"0")).join("");
function localSecret(name){
 const saved=readGuestStorage("localStorage",name);
 if(randomSecret.test(saved))return saved;
 const session=readGuestStorage("sessionStorage",name);
 const secret=randomSecret.test(session)?session:hexBytes(crypto.getRandomValues(new Uint8Array(32)));
 writeGuestStorage("localStorage",name,secret);
 const persisted=readGuestStorage("localStorage",name);
 if(randomSecret.test(persisted))return persisted;
 writeGuestStorage("sessionStorage",name,secret);
 return secret;
}
async function sharedLocalSecret(name){
 const saved=readGuestStorage("localStorage",name);
 if(randomSecret.test(saved))return saved;
 try{
  // 按来源串行初始化，两个新标签同时扫码会共用种子。
  if(globalThis.navigator?.locks?.request)return await navigator.locks.request(name,()=>localSecret(name));
 }catch{}
 return localSecret(name);
}
async function automaticGuestKey(){
 if(joinKey)return joinKey;
 if(!globalThis.crypto?.getRandomValues)return "";
 try{
  const seed=await sharedLocalSecret(guestSeedKey);
  const bytes=Uint8Array.from(seed.match(/../g),part=>parseInt(part,16));
  const context=Uint8Array.from("Friday KTV guest room v1/"+room,character=>character.charCodeAt(0));
  const secret=await crypto.subtle.importKey("raw",bytes,{name:"HMAC",hash:"SHA-256"},false,["sign"]);
  joinKey=hexBytes(new Uint8Array(await crypto.subtle.sign("HMAC",secret,context)));
 }catch{
  joinKey=await sharedLocalSecret(`friday-ktv-join:${room}`);
 }
 return joinKey;
}
async function join(){
 if(joining||document.hidden)return;
 if(!valid){$("profile-button").disabled=true;$("settings-button").disabled=true;return;}
 joining=true;clearTimeout(joinTimer);
 if(!identity){$("my-name").textContent="正在加入…";$("empty").textContent="正在连接本场…";}
 try{
  const retainedKey=`friday-ktv-room-credential:${room}`;
  const known=[identity?.key,readGuestStorage("localStorage",retainedKey),readGuestStorage("sessionStorage",`friday-ktv-join:${room}`)]
   .find(candidate=>typeof candidate==="string"&&guestCredential.test(candidate));
  const proposed=await automaticGuestKey(),profile=savedGuestProfile();
  const payload={...(proposed?{guest_key:proposed}:{}),...(profile?{profile}:{})};
  identity=await api("join",payload,"",{guestKey:known||""});
  if(guestCredential.test(identity.key||"")&&(identity.key!==proposed||known)){
   writeGuestStorage("localStorage",retainedKey,identity.key!==proposed?identity.key:"");
  }
  joinFailures=0;saveIdentity();message("");connectionNotice("");render();
  initAddQueue();clearInterval(refreshTimer);startStatusFeed();Promise.resolve(refresh()).then(()=>{if(identity&&!document.hidden)loadDiscover();});
 }catch(error){
  failure(error);
  if(connectionError(error)||error.code==="rate_limited"){
   joinFailures++;
   if(joinFailures<=4)joinTimer=setTimeout(join,Math.min(8000,1000*2**(joinFailures-1)));
   else{$("my-name").textContent="加入失败";$("empty").textContent="暂时连不上，请刷新重试。";}
  }else{$("my-name").textContent="等待扫码";$("empty").textContent="扫描电视上的二维码加入。";}
 }finally{joining=false;}
}
function resumeRoom(){
 if(!identity){join();return;}
 roomResumeAt=performance.now();initAddQueue();startStatusFeed();refresh();resumeSearchView();
}
join();document.addEventListener("visibilitychange",()=>{if(document.hidden){stopRoomStatus();searchComposing=false;cancelSearchInput();}else resumeRoom();});
window.addEventListener("online",()=>{if(!document.hidden)resumeRoom();});
window.addEventListener("pagehide",()=>{addQueue?.close();stopRoomStatus();searchComposing=false;cancelSearchInput();clearTimeout(joinTimer);});
window.addEventListener("pageshow",event=>{if(event.persisted&&!document.hidden)resumeRoom();});

// 伴唱只播放已校验的原唱音轨，跟随电视报告的播放时钟。
let guideGeneration=0,guideOperation=null;
const appGuideError=code=>Object.assign(new Error(code),{code});
function guideVersionMatches(op){return !!op&&op.spec?.sha256===playback.guide?.sha256;}
function guideCurrent(op){return !!op&&guideOperation===op&&op.generation===guideGeneration&&!op.controller.signal.aborted&&earEnabled&&playback.current===op.song&&guideVersionMatches(op);}
function checkGuide(op){if(!guideCurrent(op))throw appGuideError("cancelled");}
function invalidateGuide({disable=false}={}){
 guideGeneration++;guideOperation?.controller.abort();guideOperation=null;
 earCanPlay=false;earLoading=false;earSong="";
 if(disable)earEnabled=false;ear.stop();busy($("guide-toggle"),false);
}
function beginGuide(){invalidateGuide();const op={generation:guideGeneration,controller:new AbortController(),song:playback.current,spec:playback.guide};guideOperation=op;earSong=op.song;return op;}
async function verifiedGuide(op,spec){
 if(!spec||typeof spec.sha256!=="string"||!/^[a-f0-9]{64}$/.test(spec.sha256)||!Number.isInteger(spec.bytes)||spec.bytes<=1024||spec.bytes>32*1024*1024)throw appGuideError("guide_unavailable");
 const response=await fetch("./api/rooms/"+room+"/guide?id="+encodeURIComponent(op.song),{headers:{"X-KTV-Room":key,"X-KTV-Guest":identity.key},credentials:"omit",cache:"no-store",signal:op.controller.signal});
 checkGuide(op);if(!response.ok)throw appGuideError("guide_unavailable");
 const bytes=await response.arrayBuffer();checkGuide(op);if(bytes.byteLength!==spec.bytes)throw appGuideError("changed");
 const hash=await crypto.subtle.digest("SHA-256",bytes);checkGuide(op);
 if(Array.from(new Uint8Array(hash)).map(b=>b.toString(16).padStart(2,"0")).join("")!==spec.sha256)throw appGuideError("changed");
 return bytes;
}
async function loadGuide(){
 if(!earEnabled||!playback.current||!playback.guide||document.hidden)return;
 const op=beginGuide();earLoading=true;ear.calibrating=true;busy($("guide-toggle"),true,"正在加载伴唱…");
 try{
  // 在点击任务中打开浏览器的播放上下文，校验时长后再出声。
  await ear.start(async signal=>{
   signal.addEventListener("abort",()=>op.controller.abort(),{once:true});checkGuide(op);
   const [original]=await Promise.all([verifiedGuide(op,op.spec),controls.original===0?null:setControl({original:0},{signal:op.controller.signal,propagate:true})]);
   checkGuide(op);if(controls.original!==0)throw appGuideError("changed");return original;
  });checkGuide(op);
  if(!Number.isFinite(ear.buffer?.duration)||!Number.isFinite(op.spec.duration_ms)||Math.abs(ear.buffer.duration*1000-op.spec.duration_ms)>=150)throw appGuideError("changed");
  earCanPlay=true;
 }catch(error){
  if(!guideCurrent(op))return;
  invalidateGuide({disable:true});updateGuide();
  $("guide-status").textContent=error.code==="audio_start_blocked"?"浏览器尚未开始播放，点开启重试。":"伴唱还没加载好，点开启重试。";
 }finally{
  if(guideOperation===op){earLoading=false;ear.calibrating=false;busy($("guide-toggle"),false);updateGuide();}
 }
}
function updateGuide(){
 if(earEnabled&&(!playback.online||!playback.current)){invalidateGuide({disable:true});$("guide-status").textContent="等待电视播放。";}
 if(earEnabled&&earSong&&(earSong!==playback.current||!guideVersionMatches(guideOperation)))invalidateGuide();
 if(earEnabled&&controls.original===1&&!earLoading&&earSong){invalidateGuide({disable:true});$("guide-status").textContent="电视已切换原唱。";}
 $("guide-toggle").setAttribute("aria-pressed",String(earEnabled));
 $("guide-toggle").textContent=earEnabled?(earLoading?"取消":"关闭"):"开启";
 $("guide-toggle").setAttribute("aria-label",earEnabled?(earLoading?"取消加载耳边伴唱":"关闭耳边伴唱"):"开启耳边伴唱");
 $("guide-toggle").disabled=!playback.online||!playback.current;$("guide-settings").hidden=!earEnabled;
 if(!earEnabled)return;
 if(!earLoading&&!earSong){
  if(playback.guide&&!document.hidden)loadGuide();else $("guide-status").textContent="伴唱正在准备…";
  return;
 }
 if(earCanPlay){
  const synced=ear.sync();
  if(earEnabled)$("guide-status").textContent=synced?"已跟随电视":"等待电视播放。";
 }
}
$("guide-toggle").onclick=()=>{
 if(earEnabled){invalidateGuide({disable:true});$("guide-status").textContent="";updateGuide();return;}
 if(!playback.guide){$("guide-status").textContent="伴唱正在准备，稍后再试。";return;}
 earEnabled=true;loadGuide();updateGuide();
};
$("guide-volume").oninput=()=>ear.volume(Number($("guide-volume").value)/100);
function adjustGuide(change){
 if(!earCanPlay||!ear.buffer||earLoading)return;
 try{ear.setManualAdjustment(earClock.adjustment+change);}catch{return;}
 $("guide-offset").textContent=(earClock.adjustment>0?"+":"")+earClock.adjustment+" ms";ear.sync(true);
}
$("guide-delay").onclick=()=>adjustGuide(-50);$("guide-advance").onclick=()=>adjustGuide(50);
$("sound-dialog").addEventListener("close",()=>{if(earLoading){invalidateGuide({disable:true});updateGuide();}});
function suspendGuidePage(){if(earEnabled){invalidateGuide({disable:true});$("guide-status").textContent="伴唱已暂停，点开启继续。";updateGuide();}}
document.addEventListener("visibilitychange",()=>{if(document.hidden)suspendGuidePage();});
window.addEventListener("pagehide",suspendGuidePage);

// Match content clearance to the real toolbar height, including text zoom.
if(typeof ResizeObserver!=="undefined")new ResizeObserver(()=>{const height=$("player").hidden?0:$("player").getBoundingClientRect().height;document.documentElement.style.setProperty("--player-space",`${Math.ceil(height+28)}px`);}).observe($("player"));
