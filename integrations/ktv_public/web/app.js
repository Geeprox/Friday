"use strict";
const $=id=>document.getElementById(id),fragment=new URLSearchParams(location.hash.slice(1));
const names=["汽水猫","夜航兔","慢慢鲸","摇摆熊","月球旅人","小幽灵","唱片君","星星糖","柠檬汽水","仙人掌","北极企鹅","节拍小子",
 "鸣人","柯南","路飞","孙悟空","卡卡西","樱木花道","琪琪","龙猫","齐天大圣","哪吒","猪八戒","唐僧","白素贞","嫦娥","李白","鲁智深","史努比","哆啦A梦","月野兔","小丸子"];
const extraNames=["山茶晚风","海盐汽水","弯月小岛","白桃列车","银杏信箱","雾蓝唱片","月亮贝壳","柚子雨","杏仁云","铃兰邮差","晚班星光","晴空纸船"];
const themes=[["content","内容与留白","安静的影像，给歌声留白"],["festival","音乐节","鲜明图形，今晚放开唱"],["live","Live","把客厅交给现场"]];
let room=fragment.get("r"),key=fragment.get("k"),identity=null,songs=[],selected=[],mode="home",online=false,theme="content",refreshTimer,chosenAvatar=0,queueRevision=0,playableOrder=null,playback={online:false,current:"",played:[],state:"idle"},controls={music_gain:70,sfx_gain:60,control_revision:0},drag=null,ordering=false,actionSong=null,commandBusy=false,pendingControls={},source="song",searchSequence=0,searchOffset=0,searchMore=false,searchBusy=false,searchQuery="",playlistId="",playlistTitle="",recommendations=null,playlistPage=0;
try{if(room&&key){sessionStorage.setItem("friday-ktv-room",JSON.stringify({room,key}));history.replaceState(null,"",location.pathname);}else({room,key}=JSON.parse(sessionStorage.getItem("friday-ktv-room")||"{}"));identity=JSON.parse(sessionStorage.getItem(`friday-ktv-guest:${room}`)||"null");}catch{}
const validRoom=(id,token)=>typeof id==="string"&&/^[a-f0-9-]{36}$/.test(id)&&typeof token==="string"&&/^[A-Za-z0-9_-]{32,80}$/.test(token);
let valid=validRoom(fragment.get("r"),fragment.get("k")),roomLookup=null;
let addQueue=null,addSnapshot={items:[],stopped:"",persistence:"memory"},addViewSignature="",statusFailures=0,lastStatusSuccess=0,lastStatusProbe=-Infinity,roomResumeAt=performance.now(),statusRequest=null,joining=false,joinTimer,statusFeed=null;
let lastAddGuestRecovery=-Infinity;
const roomStaleMs=12000;
let joinKey="",joinFailures=0;
let preferredProfile=null;
let themeRevision=0,themeIntent=null;
const guestSeedKey="friday-ktv-guest-seed-v1",guestProfileKey="friday-ktv-guest-profile";
const deviceStorageKey="friday-ktv-browser-v1",deviceCookie="__Secure-FridayKTVBrowser";
const guestCredential=/^[A-Za-z0-9_-]{32,80}$/,randomSecret=/^[a-f0-9]{64}$/;
let playbackAvailable=true;
let queueWithdrawReady=false;
const pendingActions=new Map(),receiptListeners=new Set();
let commandRequest=null,draggedHandle=null;
function busy(node,active,label="正在处理…"){
 if(!node||active===pendingActions.has(node)&&(!active||pendingActions.get(node)===label))return;
 node.classList.toggle("is-busy",active);node.setAttribute("aria-busy",String(active));
 if(active)pendingActions.set(node,label);else pendingActions.delete(node);
 for(const current of pendingActions.keys())if(!current.isConnected)pendingActions.delete(current);
 const labels=[...pendingActions.values()];$("action-progress").hidden=!labels.length;setText($("action-progress-text"),labels.at(-1)||"");
}
async function waitForTV(check,signal){
 const started=performance.now(),end=started+9000;
 while(performance.now()<end){
  if(signal?.aborted)throw Object.assign(new Error(),{code:"cancelled"});
  if(check())return;
  if(performance.now()-lastStatusSuccess>700)probeSoon();
  await new Promise(resolve=>{
   function wake(){
    clearTimeout(timer);receiptListeners.delete(wake);signal?.removeEventListener("abort",wake);resolve();
   }
   const timer=setTimeout(wake,Math.min(350,Math.max(0,end-performance.now())));
   receiptListeners.add(wake);
   signal?.addEventListener("abort",wake,{once:true});
  });
 }
 throw Object.assign(new Error(),{code:lastStatusSuccess>started?"tv_slow":"receipt_unavailable"});
}
const earClock=new FridayGuideClock();let earEnabled=false,earCanPlay=false,earSong="",earLoading=false;
const ear=new FridayEarMonitor(earClock,state=>{
 if(state==="suspended"){invalidateGuide({disable:true});updateGuide();$("guide-status").textContent="伴唱已暂停，点开启继续。";}
 else if(state==="start"||state==="loading")$("guide-status").textContent="正在加载原唱…";
});
const sourceOf=s=>["cloud","mv"].includes(s.source)?s.source:"song",songKey=s=>`${sourceOf(s)}:${s.song_id}`;
const isQueued=s=>s.state!=="cancelled"&&!playback.played.includes(s.id);
const queuedSong=s=>selected.find(v=>songKey(v)===songKey(s)&&isQueued(v));
const canWithdraw=s=>!!s&&!!identity?.id&&s.guest_id===identity.id&&s.id!==playback.current&&isQueued(s)&&(s.state!=="ready"||queueWithdrawReady);
const canWithdrawIntent=s=>!s||s.guest_id!==identity?.id||canWithdraw(s);
const withdrawUnavailable="电视暂不支持撤回已就绪的歌曲。";
const frequentKey="friday-ktv-frequent-songs";
let frequentSongs=[],frequentSeen=[],remoteFrequent=[],frequentRequest=null;
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
function frequentRows(){
 const merged=new Map(remoteFrequent.map(song=>[songKey(song),song]));
 for(const song of frequentSongs){
  const previous=merged.get(songKey(song));
  merged.set(songKey(song),previous?{...song,...previous,plays:Math.max(previous.plays,song.plays)}:song);
 }
 return [...merged.values()].sort((a,b)=>b.plays-a.plays).slice(0,40);
}
function loadFrequent(){
 if(!identity||document.hidden||frequentRequest)return frequentRequest?.task;
 const controller=new AbortController(),request={controller,task:null};frequentRequest=request;
 if(!frequentRows().length)message("正在读取常唱歌曲…");
 request.task=api("profile",null,"?frequent=1",{signal:controller.signal}).then(value=>{
  if(frequentRequest!==request||controller.signal.aborted)return;
  if(Array.isArray(value.songs))remoteFrequent=value.songs;
  if(mode==="frequent")message("");
 }).catch(error=>{
  if(frequentRequest!==request||controller.signal.aborted||mode!=="frequent")return;
  if(["room_closed","room_unavailable","guest_unavailable"].includes(error.code))failure(error);
  else message("常唱歌曲暂时未更新。");
 }).finally(()=>{if(frequentRequest===request){frequentRequest=null;if(mode==="frequent")render();}});
 return request.task;
}
const labels={queued:"等待准备",acquiring:"下载中",separating:"制作伴奏",transcoding:"即将就绪",ready:"已就绪",failed:"准备失败",cancelled:"已取消"};
function setText(node,value){if(node.textContent!==value)node.textContent=value;}
function setAttribute(node,name,value){if(node.getAttribute(name)!==value)node.setAttribute(name,value);}
function message(text){setText($("message"),text);}
let toastTimer=null;
function toast(text){
 const node=$("toast");
 clearTimeout(toastTimer);setText(node,text);node.classList.add("show");
 toastTimer=setTimeout(()=>{toastTimer=null;node.classList.remove("show");},2400);
}
function avatar(node,id){
 const valid=Number.isInteger(id)&&id>=0&&id<names.length?id:0;
 node.style.backgroundPosition=`${valid%4*100/3}% ${Math.floor(valid/4)*100/(Math.ceil(names.length/4)-1)}%`;
}
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
function connectionNotice(text){const node=$("connection-status");setText(node,text);node.hidden=!text;}
const usableStatus=value=>Array.isArray(value?.songs)&&value.songs.every(song=>song&&typeof song.id==="string")&&(!value.playback||Array.isArray(value.playback.played));
const requestBudgets={status:3000,events:10000,search:7000,discover:5500,join:7500,profile:6000,controls:5500,command:5500};
const songReadinessText={song_not_ready:"这首还在准备。",lyrics_not_ready:"正在为这首歌找歌词。",media_not_ready:"所选音轨暂时不可用。"};
async function api(operation,body,query="",{signal,timeoutMs=requestBudgets[operation]||15000,priority="auto",guestKey=identity?.key}={}){
 const current=operation==="current",requestRoom=room;
 if(!current&&!valid)throw Object.assign(new Error(),{code:"cancelled"});
 const headers={...(!current?{"X-KTV-Room":key}:{}),...(guestKey?{"X-KTV-Guest":guestKey}:{}),...(body?{"Content-Type":"application/json"}:{})};
 const controller=new AbortController(),abort=()=>controller.abort();let timedOut=false;
 const timeout=setTimeout(()=>{timedOut=true;controller.abort();},timeoutMs);signal?.addEventListener("abort",abort,{once:true});if(signal?.aborted)abort();
 try{
  const started=performance.now();let response;
  const url=current?"./?room=current":`./api/rooms/${requestRoom}/${operation}${query}`;
  try{response=await fetch(url,{method:body?"POST":"GET",headers,body:body?JSON.stringify(body):undefined,credentials:"omit",signal:controller.signal,cache:"no-store",priority});}
  catch(error){throw Object.assign(new Error(),{code:signal?.aborted?"cancelled":timedOut?"request_timeout":"network_error",status:0});}
  let value;try{value=await response.json();}catch{throw Object.assign(new Error(),{code:signal?.aborted?"cancelled":timedOut?"request_timeout":"service_unavailable",status:response.status});}if(signal?.aborted||(!current&&(!valid||requestRoom!==room)))throw Object.assign(new Error(),{code:"cancelled"});
  if(!response.ok){const retry=response.headers?.get("Retry-After"),seconds=Number(retry),retryAfterMs=retry?(Number.isFinite(seconds)?Math.max(0,seconds*1000):Math.max(0,Date.parse(retry)-Date.now())):0;throw Object.assign(new Error(),{code:value.code||"service_unavailable",status:response.status,retryAfterMs});}
  if(operation==="status"){
   if(!usableStatus(value))throw Object.assign(new Error(),{code:"service_unavailable"});
   if(value.server_time_ms&&!earClock.observe(value,started,performance.now()))throw Object.assign(new Error(),{code:"stale_status"});
  }
  return value;
 }finally{clearTimeout(timeout);signal?.removeEventListener("abort",abort);}
}
function failure(error){
 if(["room_closed","room_unavailable"].includes(error.code)){
  void enterCurrentRoom(true);
  return;
 }
 if(error.code==="cancelled")return;
 if(error.code==="guest_unavailable"){
  addQueue?.stop(error.code);
  message("正在恢复本场连接…");
  void join();
  return;
 }
 if(connectionError(error)){
  message(identity?"这次操作未完成，请重试。":"正在连接房间…");
  if(identity)probeSoon();
  return;
 }
 const text=songReadinessText[error.code]||{guest_capacity:"本场加入人数已满，请联系房主。",mac_offline:"房间暂时离线，稍后再点。",queue_full:"歌单有些满，稍后再来。",rate_limited:"操作有些频繁，稍等一下。",search_unavailable:"搜索暂时不可用，稍后再试。",cloud_song_unavailable:"这首已不在云盘中，刷新搜索后再试。",retry_limit:"暂时仍无法下载，换个版本试试。",retry_unavailable:"歌曲状态已变化，请刷新歌单。",tv_offline:"电视暂时未连接，打开电视上的 Friday KTV 后再试。",tv_slow:"电视还未确认这次操作，请查看当前状态。",receipt_unavailable:"房间连接较慢，操作结果待确认。",queue_changed:"歌单刚有更新，已刷新后可重新调整。",playback_changed:"电视已经换歌，已刷新当前状态。"}[error.code]||"操作未完成，请重试。";
 message(text);
 const alert=document.querySelector("dialog[open] [role=alert]");if(alert)alert.textContent=text;
}
function applyTheme(value){if(!themes.some(t=>t[0]===value)||value===theme&&document.body.dataset.theme===value)return;theme=value;document.body.dataset.theme=value;document.querySelectorAll(".theme-option").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.theme===theme)));}
function artworkUrl(url){
 if(typeof url!=="string")return "";
 if(/^https:\/\/[a-z0-9.-]+\.music\.126\.net\//.test(url))return url;
 return url.length<1500000&&/^data:image\/(?:jpeg|png);base64,[A-Za-z0-9+/=]+$/.test(url)?url:"";
}
function resolvedArtwork(song){
 const url=artworkUrl(song.artwork);
 return needsCatalogCover(song)?searchArtwork.get(artworkKey(song))||(!artworkFailures.has(artworkKey(song))&&url.startsWith("data:")?url:""):url;
}
function artworkKey(song){return sourceOf(song)==="cloud"?"cloud:"+song.song_id:song.song_id;}
function placeholderCover(){const cover=document.createElement("div");cover.className="cover placeholder";cover.setAttribute("aria-hidden","true");cover.textContent="♪";return cover;}
function coverFor(song,visible=false){
 const url=resolvedArtwork(song);if(!url)return placeholderCover();
 const img=document.createElement("img");img.className="cover";img.alt="";img.loading=visible?"eager":"lazy";img.decoding="async";img.referrerPolicy="no-referrer";img.src=url;
 img.onload=()=>artworkFailures.delete(artworkKey(song));
 img.onerror=()=>{
  if(!img.isConnected)return;
  img.replaceWith(placeholderCover());
  const cached=searchArtwork.get(artworkKey(song));
  if(!needsCatalogCover(song)||cached&&cached!==url)return;
  const id=artworkKey(song),attempt=(artworkFailures.get(id)||0)+1;
  searchArtwork.delete(id);artworkFailures.set(id,attempt);artworkRefresh.add(id);
  if(artworkFailures.size>240)artworkFailures.delete(artworkFailures.keys().next().value);
  artworkMissing.set(id,performance.now()+Math.min(8000,650*2**Math.min(attempt-1,4)));
  queueArtwork(id);
 };
 return img;
}
const resultRowCache=new Map(),resultGroupCache=new Map();
function reconcileChildren(parent,desired){
 const keep=new Set(desired);
 for(const child of Array.from(parent.children))if(!keep.has(child))parent.removeChild(child);
 for(let i=0;i<desired.length;i++)if(parent.children[i]!==desired[i])parent.insertBefore(desired[i],parent.children[i]||null);
}
function createPreparation(){
 const element=document.createElement("div");element.className="preparation";
 const label=document.createElement("p");label.className="prepare-label";
 const rail=document.createElement("div");rail.className="prepare-steps";
 rail.setAttribute("role","group");
 rail.setAttribute("aria-label","音频准备步骤");
 for(const name of ["原曲","伴奏","播放文件"]){
  const step=document.createElement("span");step.textContent=name;rail.append(step);
 }
 const bar=document.createElement("progress");bar.value=0;bar.hidden=true;
 bar.setAttribute("aria-label","原曲下载进度");
 const amount=document.createElement("p");amount.className="prepare-wait";amount.hidden=true;
 element.append(label,rail,bar,amount);
 return {element,label,rail,bar,amount};
}
const preparationStages={
 queued:"等待准备",acquiring:"获取原曲",waiting_storage:"等待 Mac 腾出缓存空间",
 waiting_separation:"等待制作伴奏",waiting_memory:"等待制作伴奏",separating:"正在生成伴奏",
 waiting_transcode:"伴奏已完成",transcoding:"准备播放文件",video:"等待播放就绪"
};
const preparationErrors={
 download_timeout:"原曲下载未完成，请重试。",
 source_login_required:"音源尚未连接，请联系房主。",
 source_rights_denied:"当前账户无法下载这个版本，请换一个版本。",
 source_api_unavailable:"音源服务暂时不可用，请稍后重试。",
 source_file_invalid:"这首暂时无法播放，请重试或换个版本。",
 storage_full:"Mac 缓存空间不足，释放空间后重试。"
};
function updatePreparation({label,rail,bar,amount},song,progress){
 const p=progress||{};
 const message=song.state==="queued"&&!online&&!statusFailures?"等待制备服务":song.state==="failed"?
  preparationErrors[song.code]||"本次准备未完成，可重试或换一个版本。":
  preparationStages[p.stage]||labels[song.state];
 if(label.textContent!==message)label.textContent=message;
 const completed=p.completed_steps||0;
 for(let i=0;i<rail.children.length;i++){
  const step=rail.children[i],state=i<completed?"complete":i===completed?"active":"";
  if(step.className!==state){
   step.className=state;
   step.setAttribute("aria-current",state==="active"?"step":"false");
  }
 }
 const hidden=!(p.source_total>0&&p.source_phase==="source_downloading");
 if(bar.hidden!==hidden){bar.hidden=hidden;amount.hidden=hidden;}
 if(hidden)return;
 const bytes=Math.min(p.source_bytes||0,p.source_total);
 if(bar.max!==p.source_total)bar.max=p.source_total;
 if(bar.value!==bytes)bar.value=bytes;
 const percent=`已下载 ${Math.floor(bytes/p.source_total*100)}%`;
 if(amount.textContent!==percent)amount.textContent=percent;
}
function render(){tabs();for(const [node]of pendingActions)if(!node.isConnected)busy(node,false);const home=mode==="home";$("discover").hidden=!home;updateSearchPager();const rows=home?[]:mode==="find"?songs:mode==="frequent"?frequentRows():[...selected].sort((a,b)=>queueRank(a)-queueRank(b)),container=$("results"),focused=document.activeElement?.dataset.focusId||document.activeElement?.dataset.song;const historyOpen=container.querySelector("details.history")?.open||false;const rootRows=[],groupRows=new Map(),usedRows=new Set(),usedGroups=new Set();
 const searchPrompt=mode==="find"&&!$("query").value.trim()&&source!=="cloud"&&!playlistId;
 $("empty").hidden=home||rows.length>0||(mode==="find"&&!searchPrompt)||(mode==="frequent"&&!!frequentRequest);
 $("empty").textContent=mode==="find"?"搜一首你想唱的歌。":mode==="frequent"?"你点过并唱过的歌曲会出现在这里。":"还没有已点歌曲，第一首交给你。";
 const nextQueuedId=nextPlayable()?.id,firstOrderableId=selected.find(canReorder)?.id;
 const groupCounts=new Map();
 if(mode==="queue")for(const song of rows){
  const group=queueGroupName(song);
  groupCounts.set(group,(groupCounts.get(group)||0)+1);
 }
 let queueGroup=null,lastQueueGroup="";
 rows.forEach((song,index)=>{if(mode==="queue"){
  const group=queueGroupName(song);
  if(group!==lastQueueGroup){
   const collapsible=group==="history"||group==="failed";
   queueGroup=resultGroupCache.get(group);
   if(!queueGroup){queueGroup=document.createElement(collapsible?"details":"section");queueGroup.className="queue-group "+group;if(group==="history")queueGroup.open=historyOpen;resultGroupCache.set(group,queueGroup);}
   usedGroups.add(group);const heading=queueGroup.children[0]||document.createElement(collapsible?"summary":"h2");
   const label=({current:!playback.online?"上次播放":playback.state==="paused"?"已暂停":playback.state==="preparing"?"正在加载":"正在唱",pending:"接下来",preparing:"正在准备",failed:"暂不可唱",history:"唱过了"})[group];
   if(heading.dataset.label!==label){heading.dataset.label=label;heading.textContent=label;}
   const count=heading.firstElementChild||document.createElement("span"),total=String(groupCounts.get(group));
   if(count.textContent!==total)count.textContent=total;
   if(!count.parentElement)heading.append(count);
   groupRows.set(queueGroup,[heading]);rootRows.push(queueGroup);lastQueueGroup=group;
  }
 }
 const {progress,...songDetails}=song;
 const rowKey=mode==="queue"?`queue:${song.id}`:`${mode}:${songKey(song)}`,signature=JSON.stringify([songDetails,mode,song.id===playback.current?[playback.state,playback.online]:"",playback.played.includes(song.id),song.id===nextQueuedId,song.id===firstOrderableId,mode==="queue"&&lastQueueGroup,mode==="queue"&&groupCounts.get(lastQueueGroup)>1]);usedRows.add(rowKey);
 const previousRow=resultRowCache.get(rowKey);
 if(previousRow?.signature===signature){
  if(previousRow.preparation)updatePreparation(previousRow.preparation,song,progress);
  if(mode==="queue")groupRows.get(queueGroup).push(previousRow.row);else rootRows.push(previousRow.row);
  return;
 }
 const row=document.createElement("article");row.className="song";row.dataset.renderKey=rowKey;if(mode==="queue"){row.classList.add("queue-song");row.dataset.queue=song.id;if(song.id===playback.current)row.classList.add("current");}
 if(needsCatalogCover(song))row.dataset.artworkId=artworkKey(song);
 row.append(coverFor(song,index<7));
 const details=document.createElement("div");details.className="details";const title=document.createElement("div");title.className="song-title";title.textContent=song.title||`歌曲 ${song.song_id}`;details.append(title);
 const metadata=document.createElement("div");metadata.className="song-metadata";
 const artist=document.createElement("span");artist.className="artist";
 artist.textContent=[(song.artists||[]).join(" / "),song.album].filter(Boolean).join(" · ");
 if(artist.textContent){artist.title=artist.textContent;metadata.append(artist);}
 if(sourceOf(song)!=="song"){const badge=document.createElement("span");badge.className="source-badge";badge.textContent=sourceOf(song)==="cloud"?"云盘":"MV";metadata.append(badge);}
 if(mode!=="queue"&&song.cached_accompaniment===true){const badge=document.createElement("span");badge.className="cache-badge";badge.textContent="伴奏已缓存";metadata.append(badge);}
 if(metadata.childElementCount)details.append(metadata);
 let preparation;
 if(mode==="queue"){
  const by=document.createElement("div");by.className="requester";
  const picture=document.createElement("span");picture.className="avatar";picture.setAttribute("aria-hidden","true");avatar(picture,song.avatar);
  const label=document.createElement("span");label.textContent=`${song.nickname||"朋友"} 点的`;
  by.append(picture,label);details.append(by);
  if(!["ready","cancelled"].includes(song.state)){
   preparation=createPreparation();
   updatePreparation(preparation,song,progress);
   details.append(preparation.element);
  }
 }
 row.append(details);
 if(mode==="find"||mode==="frequent"){const button=document.createElement("button");button.className="add";button.dataset.song=songKey(song);button.dataset.title=song.title;button.onclick=()=>activateSong(song);row.append(button);}else addQueueActions(row,details,song,nextQueuedId,firstOrderableId,groupCounts.get(lastQueueGroup)>1);for(const b of row.querySelectorAll("button"))b.dataset.focusId=song.id?`${song.id}:${b.className.split(" ")[0]}`:b.dataset.song;resultRowCache.set(rowKey,{row,signature,preparation});if(mode==="queue")groupRows.get(queueGroup).push(row);else rootRows.push(row);});
 for(const [group,nodes]of groupRows)reconcileChildren(group,nodes);reconcileChildren(container,rootRows);observeArtwork();
 for(const key of resultRowCache.keys())if(!usedRows.has(key))resultRowCache.delete(key);for(const key of resultGroupCache.keys())if(!usedGroups.has(key))resultGroupCache.delete(key);
 for(const [node]of pendingActions)if(!node.isConnected)busy(node,false);updateAddButtons();if(focused){const button=Array.from(container.querySelectorAll("button")).find(b=>(b.dataset.focusId||b.dataset.song)===focused);button?.focus({preventScroll:true});}
}
function addQueueActions(row,details,song,nextQueuedId,firstOrderableId,hasPeers){
 const state=document.createElement("span");
 state.className="state";
 if(song.id===playback.current){
  state.textContent=!playback.online?"上次播放":playback.state==="playing"?"正在唱":playback.state==="preparing"?"加载中":"已暂停";
 }else if(playback.played.includes(song.id)){
  state.textContent="唱过了";
 }else if(song.lyric_state===-1){
  state.textContent="暂无同步歌词";
 }else if(song.lyrics_pending){
  state.textContent=song.synced_lyrics?"电视正在加载歌词":"正在找歌词";
 }else if(song.state==="ready"&&!isPlayable(song)){
  state.textContent="准备播放";
 }else if(song.state==="failed"){
  state.textContent=({download_timeout:"下载超时",download_unavailable:"暂无法下载"})[song.code]||"准备失败";
 }else{
  state.textContent=labels[song.state]||"准备中";
 }
 if(song.id===nextQueuedId)state.textContent="下一首 · "+state.textContent;
 details.append(state);
 const tools=document.createElement("div");
 tools.className="queue-tools";
 if(queueGroupName(song)==="history"){
  const repeat=document.createElement("button");
  repeat.className="repeat-song";repeat.dataset.song=songKey(song);repeat.dataset.title=song.title;
  repeat.onclick=()=>activateSong(song);
  tools.append(repeat);
 }else{
  const canPromote=canReorder(song)&&song.id!==firstOrderableId;
  if(song.id!==playback.current&&(isPlayable(song)||canPromote||hasPeers&&canReorder(song)||canWithdraw(song)||song.state==="failed"||song.lyric_state===-1)){
   const more=document.createElement("button");
   more.type="button";more.className="more-song";more.textContent="⋯";
   more.setAttribute("aria-label","调整歌曲："+song.title);
   more.setAttribute("aria-haspopup","dialog");
   more.onclick=()=>songActions(song);
   tools.append(more);
  }
  if(hasPeers&&canReorder(song)){
   const handle=document.createElement("button");
   handle.type="button";handle.className="reorder-handle";handle.textContent="≡";
   handle.setAttribute("aria-label","拖动排序："+song.title);
   handle.setAttribute("aria-keyshortcuts","ArrowUp ArrowDown Home");
   handle.onclick=event=>{
    if(event.detail&&draggedHandle===handle){draggedHandle=null;return;}
    tools.querySelector(".more-song")?.click();
   };
   handle.onpointerdown=event=>startDrag(event,song,handle);
   handle.onkeydown=event=>{
    const direction={ArrowUp:-1,ArrowDown:1,Home:"top"}[event.key];
    if(direction){event.preventDefault();moveSong(song.id,direction);}
   };
   tools.append(handle);
  }
 }
 row.append(tools);
}
function updateAddButtons(){
 const buttons=document.querySelectorAll(".add,.repeat-song");
 if(!buttons.length)return;
 const played=new Set(playback.played);
 const queued=new Map(selected.filter(s=>s.state!=="cancelled"&&!played.has(s.id)).map(s=>[songKey(s),s]));
 const pending=new Map(addSnapshot.items.map(item=>[item.key,item]));
 for(const button of buttons){
  const key=button.dataset.song,item=pending.get(key),song=queued.get(key),title=button.dataset.title;
  const retry=item?.state==="failed"&&(!item.withdraw||canWithdrawIntent(song));
  const withdrawing=!!item?.withdraw&&item.state!=="failed";
  const undo=!item?.withdraw&&!retry&&(item?canWithdrawIntent(song):canWithdraw(song));
  const added=!!song||!item&&addQueue?.has(key),repeat=button.classList.contains("repeat-song");
  const label=retry?"重试":withdrawing?"撤回中":undo?"撤回":added?(repeat?"已点":"✓"):repeat?"再唱一遍":"+";
  setText(button,label);
  button.classList.toggle("is-added",!!added&&!undo&&!retry&&!withdrawing);
  button.classList.toggle("is-intent",undo||retry||withdrawing);
  setAttribute(button,"aria-label",`${retry?(item.withdraw?"重试撤回":"重试点歌"):withdrawing?"正在撤回":undo?"撤回点歌":added?"已点":"点歌"}：${title}`);
  button.disabled=withdrawing||!!(added||item)&&!undo&&!retry||!identity||!!addSnapshot.stopped;
 }
}
const addRows=new Map();
function renderPendingAdds(){
 const node=$("pending-adds");node.hidden=!addSnapshot.items.length;
 const locked=new Set(addSnapshot.items.length?selected.filter(s=>isQueued(s)&&!canWithdrawIntent(s)).map(songKey):[]);
 const signature=JSON.stringify([addSnapshot,[...locked]]);
 if(signature===addViewSignature)return;addViewSignature=signature;
 const heading=$("pending-adds-title"),withdrawing=addSnapshot.items.filter(item=>item.withdraw).length;
 const additions=addSnapshot.items.length-withdrawing;
 setText(heading,[additions?`待加入 ${additions} 首`:"",withdrawing?`待撤回 ${withdrawing} 首`:""].filter(Boolean).join(" · "));
 setText($("pending-adds-note"),addSnapshot.stopped==="guest_unavailable"?"正在恢复本场连接。":addSnapshot.stopped?"本场连接已结束。":addSnapshot.persistence==="memory"?"当前页面将继续处理这些操作。":"连接恢复后自动处理。");
 const list=$("pending-adds-list"),rows=[],keys=new Set(),focused=document.activeElement;
 for(const item of addSnapshot.items){
  let entry=addRows.get(item.key);
  if(!entry){
   const row=document.createElement("li"),copy=document.createElement("div"),title=document.createElement("strong"),state=document.createElement("span");
   const actions=document.createElement("div"),retry=document.createElement("button"),undo=document.createElement("button");
   actions.className="pending-adds-actions";retry.type=undo.type="button";retry.textContent="重试";undo.textContent="撤回";
   retry.onclick=()=>{
    const pending=addSnapshot.items.find(value=>value.key===item.key);
    if(pending&&(!pending.withdraw||canWithdrawIntent(queuedSong(pending.song))))addQueue?.retry(item.key);
   };
   undo.onclick=()=>withdrawSong(addSnapshot.items.find(value=>value.key===item.key)?.song);
   actions.append(retry,undo);copy.append(title,state);row.append(copy,actions);
   entry={row,title,state,retry,undo};addRows.set(item.key,entry);
  }
  const {row,title,state,retry,undo}=entry;
  const blocked=locked.has(item.key);
  setText(title,item.song.title);setText(state,blocked&&item.withdraw&&item.state==="failed"?withdrawUnavailable:FridayAddQueue.message(item));
  setAttribute(retry,"aria-label",`${item.withdraw?"重试撤回":"重试点歌"}：${item.song.title}`);
  setAttribute(undo,"aria-label",`撤回点歌：${item.song.title}`);
  retry.hidden=item.state!=="failed"||!!addSnapshot.stopped||!!item.withdraw&&blocked;
  undo.hidden=!!item.withdraw||!!addSnapshot.stopped||blocked;
  if((focused===retry&&retry.hidden)||(focused===undo&&undo.hidden))(undo.hidden?heading:undo).focus({preventScroll:true});
  rows.push(row);keys.add(item.key);
 }
 for(const [key,entry]of addRows)if(!keys.has(key)){
  if(focused===entry.retry||focused===entry.undo)(node.hidden?$(mode==="home"?"queue-tab":"page-title"):heading).focus({preventScroll:true});
  addRows.delete(key);
 }
 reconcileChildren(list,rows);
}
function recoverAddGuest(){
 if(addQueue?.stopped!=="guest_unavailable"||joining||roomLookup||!valid||document.hidden||performance.now()-lastAddGuestRecovery<4000)return;
 lastAddGuestRecovery=performance.now();
 void join();
}
function initAddQueue({authenticated=false}={}){
 if(!identity?.id)return;
 if(addQueue&&!addQueue.closed&&addQueue.roomId===room&&addQueue.guestId===identity.id){if(authenticated)addQueue.recoverGuest();return;}
 addQueue?.close();addQueue=new FridayAddQueue.AddQueue({roomId:room,guestId:identity.id,send:(payload,{signal})=>api("songs",payload,"",{signal}),
  onChange:value=>{addSnapshot=value;renderPendingAdds();updateAddButtons();refreshSongActions();recoverAddGuest();},
  onAccepted:({song,receipt})=>{
   if(!selected.some(s=>s.id===receipt.id)){
    const found=songs.find(s=>songKey(s)===songKey(song));
    selected.push({...found,...song,id:receipt.id,state:receipt.state,...(receipt.duplicate?{}:{guest_id:identity.id,nickname:identity.nickname,avatar:identity.avatar})});
   }
   if(!renderQueueStatus(true)){updateAddButtons();tabs();}
   toast(receipt.duplicate?"这首已在歌单里":online||receipt.state==="ready"?"已点歌":"已点 · 等待制备服务");
   refresh();
  },
  onWithdrawn:({song,receipt,code})=>{
   if(receipt){
    if(receipt.queue_removed){
     if(Number.isInteger(receipt.queue_revision))queueRevision=Math.max(queueRevision,receipt.queue_revision);
     selected=selected.filter(song=>song.id!==receipt.id);
    }
    if($("song-dialog").open&&actionSong?.id===receipt.id)$("song-dialog").close();
    if(!renderQueueStatus(true)){updateAddButtons();tabs();}
    toast("已撤回点歌");
   }else{
    const text={song_already_started:"这首已经开始，无法撤回。",song_not_owned:"这首由其他人点唱。",
     song_unavailable:"这首已不在歌单里。",song_start_unavailable:withdrawUnavailable}[code];
    if($("song-dialog").open&&actionSong&&songKey(actionSong)===songKey(song))setText($("song-error"),text);
    else toast(text);
   }
   refresh();
  }
 });addQueue.observe({songs:selected,played:playback.played});if(authenticated)addQueue.recoverGuest();addQueue.start();
}
function add(song){
 if(!identity||queuedSong(song))return;
 try{initAddQueue();const item=addSnapshot.items.find(v=>v.key===songKey(song));if(item?.state==="failed")addQueue.retry(item.key);else addQueue.enqueue({...song,source:sourceOf(song),artwork:resolvedArtwork(song)});if(statusFailures)toast("已暂存，连接恢复后加入");}
 catch(error){if(error.code==="local_queue_full")message("待加入的歌曲已满，等前面的歌曲加入后再点。");else failure(error);}
}
function withdrawSong(song,id=""){
 if(!song||!identity)return;
 const current=id?selected.find(s=>s.id===id):queuedSong(song);
 if(id?!canWithdraw(current):!canWithdrawIntent(current))return;
 try{
  initAddQueue();
  addQueue.withdraw({...song,source:sourceOf(song)},id);
 }catch(error){failure(error);}
}
function activateSong(song){
 if(!identity||addSnapshot.stopped)return;
 const key=songKey(song),item=addSnapshot.items.find(v=>v.key===key);
 const existing=queuedSong(song);
 if(item){
  if(item.state==="failed"){if(!item.withdraw||canWithdrawIntent(existing))addQueue?.retry(key);}
  else if(!item.withdraw)withdrawSong(song);
  return;
 }
 if(canWithdraw(existing))withdrawSong(existing,existing.id);
 else if(!existing)add(song);
}
function readinessHint(value){
 const ready=value.readiness,g=ready?.gates;
 if(!g||ready.state==="closed"||ready.state==="unavailable")return value.mac_online?"":"歌曲准备服务暂时离线，歌单会保留。";
 if(ready.ready===true)return "";
 if(g.link_tv===false)return "电视暂未连接，歌单会保留。";
 if(g.tv_playback===false)return "电视已连接，暂时不能播放。";
 if(value.playback?.online&&value.playback.current&&["playing","paused"].includes(value.playback.state))return "";
 if(g.tv_media===false)return "电视正在准备播放。";
 if(["link_worker","worker_source","worker_model","worker_download","worker_lyrics","worker_media"].some(k=>g[k]===false))return "歌曲准备服务正在连接，歌单会保留。";
 return ["nas_api","nas_data","nas_media"].some(k=>g[k]===false)?"正在准备本场，已点歌单会保留。":"";
}
function applyStatus(value){
 if(!usableStatus(value))return false;
 statusFailures=0;lastStatusSuccess=performance.now();connectionNotice("");online=value.mac_online;
 playbackAvailable=value.readiness?.gates?.tv_playback!==false;
 const revision=value.queue_revision||0;
 const currentRevision=revision>=queueRevision;
 const acceptQueue=!drag&&!ordering&&currentRevision;
 if(currentRevision)queueWithdrawReady=value.queue_withdraw_ready===true;
 const nextPlayableOrder=Array.isArray(value.playable)?value.playable:null;
 const changed=acceptQueue&&(JSON.stringify(selected)!==JSON.stringify(value.songs)||
   JSON.stringify(playableOrder)!==JSON.stringify(nextPlayableOrder));
 if(acceptQueue){selected=value.songs;queueRevision=revision;playableOrder=nextPlayableOrder;}
 playback=value.playback||playback;
 const revisionReceived=value.controls?.control_revision;
 if(!Number.isInteger(revisionReceived)||revisionReceived>=controls.control_revision)controls={...controls,...(value.controls||{})};
 if(acceptQueue)addQueue?.observe({songs:selected,played:playback.played});
 addQueue?.wake();recoverAddGuest();
 const frequentChanged=rememberFrequent(selected);
 if(!themeIntent&&(!Number.isInteger(value.theme_revision)||value.theme_revision>=themeRevision)){
  themeRevision=value.theme_revision||0;applyTheme(value.theme);
 }
 updatePlayer();renderPendingAdds();tabs();$("hint").textContent=readinessHint(value);
 if(mode==="frequent"&&frequentChanged)render();else if(!renderQueueStatus(changed))updateAddButtons();
 for(const wake of receiptListeners)wake();
}
function statusFailure(error,channel="snapshot"){
 if(error.code==="stale_status"||error.code==="cancelled")return;
 if(channel==="events"&&error.code==="guest_unavailable"){
  stopRoomStatus(true);statusFeed=null;join();return;
 }
 if(connectionError(error)||error.code==="rate_limited"){
  if(channel==="events"){probeSoon();return;}
  if(performance.now()-Math.max(lastStatusSuccess,roomResumeAt)<roomStaleMs)return;
  if(!statusFailures){
   statusFailures=1;online=false;playback={...playback,online:false};updatePlayer();
   if(!renderQueueStatus())updateAddButtons();
  }
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
  events:({cursor,signal})=>api("events",null,"?after="+cursor+"&wait=3",{signal}),
  probe:options=>lastStatusSuccess>0&&performance.now()-lastStatusSuccess<900?Promise.resolve(true):refresh(options),visible:()=>!document.hidden,
  onStatus:value=>{if(!usableStatus(value)){probeSoon();return;}const now=performance.now();if(earClock.observe(value,now,now,{calibrate:false}))applyStatus(value);},onFailure:error=>statusFailure(error,"events")
 });
 statusFeed.start();
}
let searchController=null,searchTimer=null,searchComposing=false,nextPage=null,warmPage=null,pagingNow=false,pagingFailed=false,searchRetryDelay=600;
const searchArtwork=new Map(),artworkPending=new Set(),artworkMissing=new Map(),artworkFailures=new Map(),artworkRefresh=new Set();
let artworkController=null,artworkRetry=null,artworkDelay=650;
const artworkObserver=typeof IntersectionObserver==="function"?new IntersectionObserver(entries=>{
 for(const entry of entries)if(entry.isIntersecting)queueArtwork(entry.target.dataset.artworkId);
 enrichArtwork();
},{rootMargin:"240px 0px"}):null;
function needsCatalogCover(song){return sourceOf(song)!=="mv"&&/^[1-9][0-9]{0,17}$/.test(song.song_id);}
function stopArtwork(){
 artworkController?.abort();artworkController=null;clearTimeout(artworkRetry);artworkRetry=null;artworkDelay=650;
 artworkObserver?.disconnect();artworkPending.clear();
}
function queueArtwork(id){
 if(!id||searchArtwork.has(id))return;
 artworkPending.add(id);
 if((artworkMissing.get(id)||0)>performance.now())enrichArtwork();
}
function observeArtwork(){
 if(!identity||mode==="home"||document.hidden)return;
 artworkObserver?.disconnect();
 for(const {row} of resultRowCache.values()){
  const id=row.dataset.artworkId;
  if(!id||searchArtwork.has(id))continue;
  if(artworkObserver)artworkObserver.observe(row);else queueArtwork(id);
 }
 enrichArtwork();
}
async function enrichArtwork(){
 if(artworkController||document.hidden||mode==="home")return;
 clearTimeout(artworkRetry);artworkRetry=null;
 if(!artworkPending.size)return;
 const now=performance.now(),ready=[...artworkPending].filter(id=>(artworkMissing.get(id)||0)<=now);
 if(!ready.length){
  const wait=Math.min(...[...artworkPending].map(id=>artworkMissing.get(id)-now));
  artworkRetry=setTimeout(()=>{artworkRetry=null;enrichArtwork();},Math.ceil(wait));
  return;
 }
 // 坏图按各自的截止时间重试，先补当前视图中尚未失败的封面。
 ready.sort((a,b)=>Number(artworkMissing.has(a))-Number(artworkMissing.has(b)));
 const controller=new AbortController(),source=ready[0].startsWith("cloud:")?"cloud":"song";
 const retrying=artworkMissing.has(ready[0]);
 const keys=ready.filter(id=>id.startsWith("cloud:")===(source==="cloud")&&artworkMissing.has(id)===retrying).slice(0,source==="cloud"?3:6);
 const ids=keys.map(id=>source==="cloud"?id.slice(6):id);
 const reload=keys.some(id=>artworkRefresh.has(id));
 artworkController=controller;
 for(const id of keys)artworkPending.delete(id);
 try{
  const {covers,pending=[],albums={},tracks={}}=await api("artwork",null,"?"+new URLSearchParams({
   ids:ids.join(","),inline:1,...(source==="cloud"?{source}:{}),...(reload?{refresh:1}:{})
  }),{signal:controller.signal,timeoutMs:15000,priority:"low"});
  if(controller.signal.aborted||mode==="home")return;
  if(!covers||Array.isArray(covers)||typeof covers!=="object")throw Object.assign(new Error(),{code:"search_unavailable"});
  let resolved=0;const retryKeys=[];
  for(const id of keys){
   const ident=source==="cloud"?id.slice(6):id;
   if(pending.includes(ident)){retryKeys.push(id);artworkRefresh.add(id);continue;}
   artworkPending.delete(id);
   const url=artworkUrl(covers?.[ident]);
   if(!url){
    artworkMissing.set(id,performance.now()+90000);
    if(artworkMissing.size>240)artworkMissing.delete(artworkMissing.keys().next().value);
    continue;
   }
   resolved++;
   artworkMissing.delete(id);artworkRefresh.delete(id);
   searchArtwork.set(id,url);if(searchArtwork.size>240)searchArtwork.delete(searchArtwork.keys().next().value);
   for(const {row} of resultRowCache.values())if(row.dataset.artworkId===id&&row.firstElementChild?.classList.contains("placeholder")){
    row.firstElementChild.replaceWith(coverFor({song_id:ident,source},true));
    artworkObserver?.unobserve(row);
   }
  }
  let metadataChanged=false;
  for(const song of [...songs,...selected,...frequentSongs,...remoteFrequent]){
   if(sourceOf(song)!==source)continue;
   const track=tracks[song.song_id],album=track?.album||albums[song.song_id];
   if(typeof album==="string"&&album!==song.album){song.album=album;metadataChanged=true;}
   if(Array.isArray(track?.artists)&&track.artists.every(name=>typeof name==="string")
      &&JSON.stringify(track.artists)!==JSON.stringify(song.artists||[])){
    song.artists=track.artists;metadataChanged=true;
   }
  }
  if(metadataChanged){
   if(mode==="queue")renderQueueStatus(true);
   else render();
  }
  if(resolved)artworkDelay=650;
  if(retryKeys.length){
   const retryAt=performance.now()+artworkDelay;
   for(const id of retryKeys){artworkPending.add(id);artworkMissing.set(id,retryAt);}
   artworkDelay=Math.min(6000,artworkDelay*2);
  }
 }catch(error){
  if(controller.signal.aborted||mode==="home")return;
  if(connectionError(error)||["search_busy","search_unavailable","rate_limited","guest_unavailable"].includes(error.code)){
   const retryAt=performance.now()+Math.max(artworkDelay,error.retryAfterMs||0);
   for(const id of keys){artworkPending.add(id);artworkMissing.set(id,retryAt);}
   artworkDelay=Math.min(6000,artworkDelay*2);
  }
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
 warmPage?.controller.abort();warmPage=null;
 pagingObserver?.disconnect();searchSequence++;resetSearchBusy();
 if(wasBusy&&$("message").textContent==="正在找歌…")message("");
}
function clearSearchResults(){
 searchQuery=null;searchOffset=0;searchMore=false;songs=[];
}
function scheduleSearch(){
 if(mode!=="find"||document.hidden)return;
 searchRetryDelay=600;
 cancelSearchInput();
 if($("query").value.trim()!==searchQuery||playlistId){
  playlistId="";playlistTitle="";clearSearchResults();message("");render();
 }
 if(!searchComposing)searchTimer=setTimeout(()=>{searchTimer=null;findSongs();},180);
}
async function searchPage(q,kind,list,offset,signal,priority="high"){
 const operation=list?"playlist":"search";
 const params=list?new URLSearchParams({id:list,offset}):new URLSearchParams({q,source:kind,offset});
 const cacheKey=operation+"?"+params;
 const hit=searchPages.get(cacheKey);
 if(hit&&performance.now()-hit.at<45000){searchPages.delete(cacheKey);searchPages.set(cacheKey,hit);return hit.value;}
 const value=await api(operation,null,"?"+params,{signal,priority});
 if(!Array.isArray(value?.songs))throw Object.assign(new Error(),{code:"search_unavailable"});
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
 searchMore=value.has_more===true&&value.songs.length>0&&searchOffset>offset&&searchOffset<=100000;
 render();
}
function warmNextPage(){
 if(warmPage||mode!=="find"||document.hidden||!searchMore||pagingNow||pagingFailed)return;
 const page={offset:searchOffset,controller:new AbortController(),task:null};
 warmPage=page;
 page.task=searchPage(searchQuery,source,playlistId,page.offset,page.controller.signal,"low").catch(()=>null);
}
async function appendNextPage(retry=false){
 if(pagingNow||mode!=="find"||document.hidden||!searchMore||searchBusy||searchTimer||searchComposing||pagingFailed&&!retry)return;
 const page={offset:searchOffset,sequence:searchSequence,controller:new AbortController()};
 const warmed=warmPage?.offset===page.offset?warmPage:null;
 nextPage=page;pagingFailed=false;
 pagingNow=true;updateSearchPager();
 try{
  const value=(warmed&&await warmed.task)||await searchPage(searchQuery,source,playlistId,page.offset,page.controller.signal);
  if(nextPage===page&&page.sequence===searchSequence&&!page.controller.signal.aborted)applySearchPage(value,page.offset,true);
 }catch{
  if(nextPage===page)pagingFailed=true;
 }finally{
  if(nextPage===page){if(warmPage===warmed)warmPage=null;pagingNow=false;nextPage=null;updateSearchPager();observeSearchEnd();warmNextPage();}
 }
}
async function findSongs(more=false,collection=null){
 if(more)return appendNextPage(true);
 cancelSearchInput();
 const q=$("query").value.trim(),list=collection?.id||"";
 if(q!==searchQuery||list!==playlistId)clearSearchResults();
 playlistId=list;playlistTitle=collection?.title||"";
 if(!q&&source!=="cloud"&&!playlistId){searchQuery="";message("");render();return;}
 const sequence=searchSequence,requestedSource=source,requestedPlaylist=playlistId,controller=new AbortController();
 searchController=controller;searchBusy=true;$("search").querySelector("button").disabled=true;message("正在找歌…");render();
 try{
  const value=await searchPage(q,requestedSource,requestedPlaylist,0,controller.signal);
  if(controller.signal.aborted||sequence!==searchSequence||source!==requestedSource||playlistId!==requestedPlaylist)return;
  searchQuery=q;applySearchPage(value,0,false);
  searchRetryDelay=600;
  message(songs.length?"":requestedSource==="cloud"&&!value.updated?"云盘正在连接，稍后再试。":"没有找到，换个歌名或歌手试试。");
 }catch(error){
  if(!controller.signal.aborted&&sequence===searchSequence&&mode==="find"){
   searchMore=false;updateSearchPager();
   if(connectionError(error)||["search_unavailable","search_busy","rate_limited"].includes(error.code)){
    message("正在重新连接曲库…");
    searchTimer=setTimeout(()=>{
     searchTimer=null;
     if(mode==="find"&&!document.hidden&&!searchComposing&&$("query").value.trim()===q&&source===requestedSource){
      findSongs(false,requestedPlaylist?{id:requestedPlaylist,title:playlistTitle}:null);
     }
    },Math.max(searchRetryDelay,error.retryAfterMs||0));
    searchRetryDelay=Math.min(3000,searchRetryDelay*2);
   }
   else failure(error);
  }
 }finally{if(searchController===controller)searchController=null;if(sequence===searchSequence){resetSearchBusy();observeSearchEnd();warmNextPage();}}
}
function resumeSearchView(){
 if(mode!=="find"||searchBusy||searchTimer||searchComposing)return;
 const q=$("query").value.trim();
 if(playlistId&&!songs.length)findSongs(false,{id:playlistId,title:playlistTitle});
 else if(!playlistId&&(q!==searchQuery||(!songs.length&&(q||source==="cloud"))))scheduleSearch();
 else{observeArtwork();observeSearchEnd();warmNextPage();}
}
function searchTitles(query){setView("find");selectSearchSource("song");$("query").value=query;findSongs();}
function selectSearchSource(kind){
 if(source!==kind){source=kind;clearSearchResults();}
 updateSources();
}
function updateSources(){$("query").placeholder=source==="cloud"?"搜索云盘歌曲或歌手":source==="mv"?"搜索 MV 或歌手":"搜索歌曲或歌手";for(const [button,kind]of[["source-song","song"],["source-mv","mv"],["source-cloud","cloud"]])$(button).setAttribute("aria-pressed",String(source===kind));}
const starterArtists=["周杰伦","陈奕迅","林俊杰","五月天","孙燕姿","张惠妹","张学友","王菲"];
let discoverRequest=null,discoverRetry=null,discoverDelay=1500,artistSignature="";
function renderArtists(artists=starterArtists,photos={}){
 const signature=JSON.stringify([artists,photos]);
 if(artistSignature===signature)return;
 artistSignature=signature;
 const buttons=artists.map(name=>{
  const button=document.createElement("button");
  button.type="button";
  const portrait=document.createElement("span");
  portrait.className="artist-portrait";
  portrait.setAttribute("aria-hidden","true");
  portrait.textContent=name.slice(0,1);
  const image=artworkUrl(photos[name]);
  if(image){
   const img=document.createElement("img");
   img.src=image;img.alt="";img.loading="lazy";img.referrerPolicy="no-referrer";
   img.onerror=()=>img.remove();
   portrait.append(img);
  }
  const label=document.createElement("span");
  label.textContent=name;
  button.append(portrait,label);
  button.onclick=()=>searchTitles(name);
  return button;
 });
 $("artist-picker").replaceChildren(...buttons);
}
function renderPlaylists(){
 const playlists=recommendations?.playlists||[];
 const cards=playlists.slice(playlistPage*4,playlistPage*4+4).map(list=>{
  const button=document.createElement("button");
  button.type="button";button.className="playlist-card";
  button.append(coverFor(list));
  const title=document.createElement("strong");
  title.textContent=list.title;button.append(title);
  button.onclick=()=>{
   setView("find");selectSearchSource("song");$("query").value="";
   findSongs(false,list);
  };
  return button;
 });
 $("playlist-picker").replaceChildren(...cards);
 $("refresh-discover").textContent=playlists.length?"换一批":"重试";
 $("refresh-discover").hidden=playlists.length>0&&playlists.length<=4;
}
function stopDiscover(){
 clearTimeout(discoverRetry);discoverRetry=null;
 const active=discoverRequest;discoverRequest=null;active?.controller.abort();
}
function retryDiscover(serverDelay=0){
 clearTimeout(discoverRetry);
 if(document.hidden||mode!=="home")return;
 discoverRetry=setTimeout(loadDiscover,Math.max(serverDelay,discoverDelay));
 discoverDelay=Math.min(30000,discoverDelay*2);
}
function loadDiscover(){
 if(!valid||!identity||document.hidden||mode!=="home")return;
 if(discoverRequest)return discoverRequest.task;
 clearTimeout(discoverRetry);discoverRetry=null;
 if(!recommendations){
  if(!artistSignature)renderArtists();
  if(!$("discover-status").textContent)$("discover-status").textContent="正在加载歌单…";
 }
 const controller=new AbortController(),request={controller,task:null};
 discoverRequest=request;
 request.task=api("discover",null,"",{signal:controller.signal,priority:"low"}).then(value=>{
  if(discoverRequest!==request||controller.signal.aborted)return;
  const artists=Array.isArray(value.artists)?value.artists.filter(name=>typeof name==="string"&&name):[];
  if(artists.length)renderArtists(artists,value.artist_photos||{});
  const playlists=Array.isArray(value.playlists)?value.playlists:[];
  if(playlists.length){
   const changed=JSON.stringify(playlists)!==JSON.stringify(recommendations?.playlists);
   recommendations={playlists};
   if(changed){
    playlistPage=Math.min(playlistPage,Math.ceil(playlists.length/4)-1);
    renderPlaylists();
   }
  }
  $("discover-status").textContent=recommendations?"":value.refreshing?"正在加载歌单…":"歌单暂未加载，稍后自动重试。";
  if(!recommendations)renderPlaylists();
  if(value.refreshing||!recommendations)retryDiscover();
  else discoverDelay=1500;
 }).catch(error=>{
  if(discoverRequest!==request||controller.signal.aborted)return;
  if(!recommendations){
   $("discover-status").textContent="歌单暂未加载，稍后自动重试。";
   renderPlaylists();
   if(!["room_closed","room_unavailable","guest_unavailable"].includes(error.code))retryDiscover(error.retryAfterMs);
  }
 }).finally(()=>{if(discoverRequest===request)discoverRequest=null;});
 return request.task;
}
$("refresh-discover").onclick=()=>{
 if(!recommendations)return loadDiscover();
 playlistPage=(playlistPage+1)%Math.ceil(recommendations.playlists.length/4);
 renderPlaylists();
};
$("load-more").onclick=()=>findSongs(true);
$("search").addEventListener("submit",event=>{event.preventDefault();if(!searchComposing&&!event.isComposing)findSongs();});
$("query").oncompositionstart=()=>{searchComposing=true;cancelSearchInput();};
$("query").oncompositionend=()=>{searchComposing=false;scheduleSearch();};
$("query").oninput=event=>{if(!event.isComposing&&!searchComposing)scheduleSearch();};
for(const [id,value]of[["source-song","song"],["source-mv","mv"],["source-cloud","cloud"]])$(id).onclick=()=>{selectSearchSource(value);findSongs();};
const viewScroll={home:0,find:0,queue:0,frequent:0};
let viewHeadingMotion=null;
function tabs(){
 const count=selected.filter(isUpcoming).length;
 document.body.dataset.view=mode;
 $("search-entry").hidden=$("frequent-entry").hidden=!identity||mode!=="home";
 $("search").hidden=!identity||mode!=="find";$("source-picker").hidden=!identity||mode!=="find";
 $("view-queue").hidden=!identity||mode!=="find";setText($("search-queue-count"),`已点 ${count} 首`);
 const badge=$("queue-count");if(String(badge.textContent)!==String(count))badge.textContent=count;
 $("queue-tab").disabled=!identity;
 let total=$("queue-total");
 if(!total){
  // 旧页面也会加载新版脚本，把原来的首数移到当前页标题。
  const previous=$("count"),heading=previous?.parentElement;
  if(heading?.classList.contains("list-heading")){
   total=previous;
   total.id="queue-total";
   $("page-heading").insertBefore(total,$("view-queue"));
   heading.remove();
  }
 }
 if(total){
  const number=selected.length?`${selected.length} 首`:"";
  total.hidden=mode!=="queue"||!number;
  if(total.textContent!==number)total.textContent=number;
 }
 const title=mode==="queue"?"已点歌曲":mode==="frequent"?"常唱歌曲":playlistTitle||"搜索";
 $("page-heading").hidden=mode==="home";
 if($("page-title").textContent!==title)$("page-title").textContent=title;
}
function setView(next,{push=true,focus}={}){
 if(!["home","find","queue","frequent"].includes(next)||mode===next)return;
 const previous=mode;viewScroll[mode]=window.scrollY;cancelDrag();stopArtwork();mode=next;
 if(previous==="find"&&next!=="find"){searchComposing=false;cancelSearchInput();}
 if(previous==="home")stopDiscover();
 if(push)history.pushState({ktvView:next},"");
 message("");
 if(next==="frequent")loadFrequent();
 tabs();render();
 if(next==="home")loadDiscover();
 const heading=$("page-heading");
 if(!heading.hidden&&heading.animate){
  const opacity=viewHeadingMotion?.playState==="running"?Number(getComputedStyle(heading).opacity):.76;
  const motion=getComputedStyle(document.documentElement);
  viewHeadingMotion?.cancel();
  viewHeadingMotion=heading.animate([{opacity},{opacity:1}],{
   duration:parseFloat(motion.getPropertyValue("--motion-state"))||150,
   easing:motion.getPropertyValue("--motion-ease").trim()||"ease-out"
  });
 }else viewHeadingMotion?.cancel();
 const focusId=focus||(next==="home"?(previous==="queue"?"queue-tab":previous==="frequent"?"frequent-entry":"search-entry"):
  next==="find"&&!push&&previous==="queue"?"view-queue":"page-title");
 // Focus in the gesture task so opening search keeps the mobile keyboard active.
 $(focusId).focus({preventScroll:true});
 requestAnimationFrame(()=>{if(mode===next&&!drag)window.scrollTo({top:viewScroll[next],behavior:"instant"});});
 if(next==="queue")refresh();else if(next==="find")resumeSearchView();
}
history.replaceState({ktvView:"home"},"");
window.addEventListener("popstate",event=>setView(event.state?.ktvView||"home",{push:false}));
$("page-back").onclick=()=>{if(history.state?.ktvView&&mode!=="home")history.back();else setView("home",{push:false});};
$("search-entry").onclick=()=>setView("find",{focus:"query"});
$("frequent-entry").onclick=()=>setView("frequent");
document.querySelector(".brand").onclick=event=>{event.preventDefault();setView("home");};
$("queue-tab").onclick=()=>setView("queue");$("view-queue").onclick=()=>setView("queue");
function openDialog(id){
 const dialog=$(id);
 if(dialog.showModal)dialog.showModal();else dialog.setAttribute("open","");
 dialog.querySelector(".sheet-heading h2").focus({preventScroll:true});
}
function chooseAvatar(id){
 chosenAvatar=id;
 document.querySelectorAll(".avatar-choice").forEach(button=>button.setAttribute("aria-pressed",String(Number(button.dataset.avatar)===id)));
}
function renderAvatarPicker(){
 const picker=$("avatar-picker");
 if(picker.children.length)return;
 const displayOrder=[...names.map((name,i)=>[i,name]).slice(12),...names.map((name,i)=>[i,name]).slice(0,12)];
 for(const [id,name] of displayOrder){
  const button=document.createElement("button");button.type="button";button.className="avatar-choice";button.dataset.avatar=String(id);
  button.setAttribute("aria-label",`选择${name}头像`);button.setAttribute("aria-pressed","false");
  const picture=document.createElement("span");picture.className="avatar";picture.setAttribute("aria-hidden","true");avatar(picture,id);
  button.append(picture);button.onclick=()=>chooseAvatar(id);picker.append(button);
 }
}
$("profile-button").onclick=()=>{if(!identity)return;renderAvatarPicker();$("nickname").value=identity.nickname;chooseAvatar(identity.avatar);$("profile-error").textContent="";openDialog("profile-dialog");};
$("random-profile").onclick=()=>{const [picture,word]=crypto.getRandomValues(new Uint32Array(2)),choices=[...names,...extraNames];chooseAvatar(picture%names.length);$("nickname").value=choices[word%choices.length];};
$("save-profile").onclick=async()=>{const name=$("nickname").value.trim();if(!name){$("profile-error").textContent="起一个昵称再保存。";$("nickname").focus();return;}const b=$("save-profile");b.disabled=true;busy(b,true,"正在保存…");try{const result=await api("profile",{nickname:name,avatar:chosenAvatar});identity={...identity,...result};saveIdentity({prefer:true});$("profile-dialog").close();toast("就用这个名字，开唱吧");await refresh();}catch{$("profile-error").textContent="还没保存，检查连接后再试。";}finally{b.disabled=false;busy(b,false);}};
async function chooseTheme(value,button){
 if(themeIntent||theme===value)return;
 const previous=theme,intent={};themeIntent=intent;
 $("theme-error").textContent="";applyTheme(value);busy(button,true,"正在切换风格…");
 try{
  const result=await api("theme",{theme:value},"",{priority:"high"});
  if(themeIntent===intent){themeRevision=result.theme_revision??themeRevision;toast("本场风格已更新");}
 }catch(error){
  if(themeIntent===intent&&error.code!=="cancelled"){
   applyTheme(previous);$("theme-error").textContent="这次未能切换，请重试。";
   if(["room_closed","room_unavailable","guest_unavailable"].includes(error.code))failure(error);
  }
 }finally{if(themeIntent===intent)themeIntent=null;busy(button,false);}
}
function renderThemePicker(){
 const picker=$("theme-picker");
 if(picker.children.length)return;
 for(const [value,title,description]of themes){
  const button=document.createElement("button");button.type="button";button.className="theme-option";button.dataset.theme=value;
  button.setAttribute("aria-pressed",String(theme===value));
  const preview=document.createElement("img");preview.className="theme-preview";preview.src=`./${value}.png?v=4`;preview.alt="";preview.decoding="async";
  const copy=document.createElement("div");copy.className="theme-copy";
  const heading=document.createElement("strong");heading.textContent=title;
  const detail=document.createElement("span");detail.textContent=description;copy.append(heading,detail);
  button.append(preview,copy);button.onclick=()=>chooseTheme(value,button);picker.append(button);
 }
}
$("settings-button").onclick=()=>{if(!identity)return;renderThemePicker();$("theme-error").textContent="";openDialog("settings-dialog");};
// The remembered nonzero level belongs to this room and survives a page reload.
// Storage may be unavailable in an embedded browser; the in-memory value still works.
class RoomMusicVolume {
 constructor(roomId,storage){this.storage=storage;this.key=`friday-ktv-music-level:${roomId}`;this.level=70;try{const saved=Number(storage.getItem(this.key));if(Number.isInteger(saved)&&saved>0&&saved<=100)this.level=saved;}catch{}}
 remember(level){if(!Number.isInteger(level)||level<=0||level>100)return;this.level=level;try{this.storage.setItem(this.key,String(level));}catch{}}
 toggle(level){if(Number.isInteger(level)&&level>0&&level<=100){this.remember(level);return 0;}return this.level;}
}
let roomGainStorage;try{roomGainStorage=sessionStorage;}catch{}
let roomMusicVolume=new RoomMusicVolume(room,roomGainStorage);
const gainTimers=new Map();
const volumeControls=[["music","music_gain"],["sfx","sfx_gain"]];
function updateMuteIcon(button,muted,confirmed,note,status){
 button.dataset.muted=String(muted);button.setAttribute("aria-pressed",confirmed?String(muted):"mixed");
 if(note.textContent!==status)note.textContent=status;
}
function updateMuteButtons(){
 const level=pendingControls.music_gain??controls.music_gain;
 roomMusicVolume.remember(level);const musicMuted=level===0;
 const musicConfirmed=playback.online&&pendingControls.music_gain===undefined&&!controlPendingVersions.has($("music-mute-button"))&&playback.applied_revision>=controls.control_revision;
 const mute=$("music-mute-button");
 mute.disabled=!playback.online;
 mute.setAttribute("aria-disabled",String(mute.disabled||Boolean(musicMuteSending)));
 updateMuteIcon(mute,musicMuted,musicConfirmed,$("music-mute-state"),!playback.online?"电视未连接，音乐音量尚未确认":!musicConfirmed?"已选择音乐音量，等待电视确认":musicMuted?"音乐已静音，按下恢复静音前音量":"音乐未静音，按下静音");

}
function changeGain(id,key,level,delay=120){
 level=Math.max(0,Math.min(100,Math.round(level)));clearTimeout(gainTimers.get(id));gainTimers.delete(id);
 pendingControls[key]=level;$(`${id}-volume`).value=String(level);$(`${id}-value`).textContent=`${level}%`;
 if(id==="music")roomMusicVolume.remember(level);updateVolumeButtons(id,level);updateMuteButtons();
 if(delay===0)return setControl({[key]:level});
 gainTimers.set(id,setTimeout(()=>{gainTimers.delete(id);setControl({[key]:level});},delay));
}
function toggleMusic(){
 if($("music-mute-button").disabled||musicMuteSending)return;
 const level=pendingControls.music_gain??controls.music_gain;
 return changeGain("music","music_gain",roomMusicVolume.toggle(level),0);
}
$("music-mute-button").onclick=toggleMusic;
let playbackSignature="",queuePresentationDirty=false;
function renderQueueStatus(changed=false){
 if(mode!=="queue")return false;
 queuePresentationDirty||=changed;
 if(drag||ordering)return false;
 const signature=JSON.stringify([playback.current,playback.played,playback.state,playback.online,online,queueWithdrawReady]);
 if(!queuePresentationDirty&&signature===playbackSignature)return false;
 playbackSignature=signature;
 queuePresentationDirty=false;
 render();
 return true;
}
function isUpcoming(song){return song.id!==playback.current&&!playback.played.includes(song.id)&&!["failed","cancelled"].includes(song.state);}
function isPlayable(song){return song.state==="ready"&&song.lyric_state!==-1&&(playableOrder?playableOrder.includes(song.id):!song.lyrics_pending);}
function nextPlayable(){
 if(!playableOrder)return selected.find(s=>isUpcoming(s)&&isPlayable(s));
 for(const id of playableOrder){
  const song=selected.find(s=>s.id===id);
  if(song&&isUpcoming(song)&&isPlayable(song))return song;
 }
}
function queueGroupName(song){
 if(song.id===playback.current)return "current";
 if(playback.played.includes(song.id)||song.state==="cancelled")return "history";
 if(song.state==="failed"||song.lyric_state===-1)return "failed";
 return isPlayable(song)?"pending":"preparing";
}
function queueRank(song){return ["current","pending","preparing","failed","history"].indexOf(queueGroupName(song));}
function updatePlayer(){updateGuide();updatePartyAudio();
 $("player").hidden=!identity;document.body.classList.toggle("has-player",!!identity);
 const current=selected.find(s=>s.id===playback.current),queued=selected.filter(isUpcoming),next=nextPlayable();
 const onlyUnavailable=queued.length>0&&queued.every(song=>song.lyric_state===-1);
 const title=current?.title||(next?"接下来："+next.title:onlyUnavailable?"等待可唱歌曲":queued.length?"正在准备下一首":playback.state==="playing"&&playback.online?"暖场":"准备好，唱下一首");
 const phases={playing:current?"正在唱":"暖场播放中",preparing:"正在加载音源",paused:"已暂停",idle:next?"待播放":""};
 const caption=playback.online?phases[playback.state]||"":statusFailures?"正在连接房间":"等待电视连接";
 if($("now-title").textContent!==title)$("now-title").textContent=title;
 if($("player-status").textContent!==caption)$("player-status").textContent=caption;
 const transportReady=playback.online&&playbackAvailable;
 const playing=playback.state==="playing"||playback.state==="preparing";
 const toggle=$("play-toggle");
 toggle.dataset.playing=String(playing);
 if(!toggle.firstElementChild)setText(toggle,playing?"Ⅱ":"▶");
 setAttribute(toggle,"aria-label",playing?"暂停播放":"开始播放");
 toggle.disabled=!transportReady||commandBusy||(!playback.current&&!playing&&onlyUnavailable);
 $("skip-button").disabled=!transportReady||commandBusy||(!playback.current&&(!queued.length||onlyUnavailable||playback.state==="preparing"));
 $("restart-button").disabled=!transportReady||!playback.current||commandBusy;
 updateSoundMode();

 for(const [id,key]of volumeControls){const input=$(`${id}-volume`),level=pendingControls[key]??controls[key];updateVolumeButtons(id,level);if(document.activeElement!==input){if(String(input.value)!==String(level))input.value=level;setText($(`${id}-value`),`${level}%`);}}
 updateMuteButtons();refreshSongActions();
}
async function sendCommand(action,song="",origin=""){
 if(commandBusy||!playbackAvailable)return;
 const before={...playback},button=$(origin||(action==="next"?"skip-button":"play-toggle"));
 const sheet=origin==="sing-now"?$("song-dialog"):null;
 const label=origin==="restart-button"?"正在从头开始…":action==="next"?"正在切歌…":action==="pause"?"正在暂停…":"正在开始播放…";
 const request={signal:controlRoom.signal,button};
 commandRequest=request;commandBusy=true;
 busy(button,true,label);
 updatePlayer();
 try{
  const result=await api("command",{action,song,request_id:crypto.randomUUID()},"",{signal:request.signal});
  if(commandRequest!==request||request.signal.aborted)return;
  if(sheet){
   if(sheet.open&&actionSong?.id===song)sheet.close();
   busy(request.button,false);
   request.button=$("play-toggle");
   busy(request.button,true,label);
  }
  await waitForTV(()=>FridayPartyAudio.commandObserved(action,song,before,playback,result.seq),request.signal);
 }catch(error){
  if(!request.signal.aborted){
   const notice=sheet?.open&&actionSong?.id===song&&songReadinessText[error.code];
   if(notice)$("song-error").textContent=notice;
   else failure(error);
  }
 }
 finally{
  if(commandRequest===request){
   commandRequest=null;commandBusy=false;busy(request.button,false);
   await refresh();updatePlayer();
  }
 }
}
$("mode-instrumental").onclick=()=>chooseSoundMode(0);
$("mode-original").onclick=()=>chooseSoundMode(1);
let controlWrites=Promise.resolve(),controlSerial=0,modeIntent=null,controlRoom=new AbortController();
let musicMuteVersion=0,musicMuteSending=0;
const controlPendingVersions=new Map();
const controlFields={original:[],music_gain:["music-value","music-mute-button"],sfx_gain:["sfx-value"]};
function updateSoundMode(){
 const chosen=modeIntent?.value??controls.original??0;
 for(const [id,value] of [["mode-instrumental",0],["mode-original",1]]){
  const button=$(id),pressed=String(chosen===value);
  if(button.getAttribute("aria-pressed")!==pressed)button.setAttribute("aria-pressed",pressed);
  button.disabled=!playback.online;
 }
 const status=$("sound-mode-status"),text=modeIntent?.notice?"等待电视切换…":"";
 if(status.textContent!==text)status.textContent=text;
}
function chooseSoundMode(value){
 if(!playback.online||modeIntent?.value===value)return;
 if(!modeIntent&&controls.original===value&&playback.applied_revision>=controls.control_revision)return;
 if(value===1&&earLoading){invalidateGuide({disable:true});updateGuide();}
 return setControl({original:value});
}
function setControl(value,{signal,propagate=false}={}){
 const controller=new AbortController(),roomSignal=controlRoom.signal,abort=()=>controller.abort();
 signal?.addEventListener("abort",abort,{once:true});roomSignal.addEventListener("abort",abort,{once:true});
 if(signal?.aborted||roomSignal.aborted)abort();
 const requestSignal=controller.signal;
 const version=++controlSerial,fields=Object.keys(value);
 const musicGain=Object.hasOwn(value,"music_gain");
 const priorityMute=musicGain&&value.music_gain===0&&fields.length===1;
 const expectedMute=controls.music_mute_revision;
 if(priorityMute)musicMuteVersion=musicMuteSending=version;
 const nodes=fields.flatMap(field=>controlFields[field].map($));
 const errorNode=$(Object.hasOwn(value,"sfx_gain")?"sfx-error":"sound-error");
 let intent=null;
 if(Object.hasOwn(value,"original")){
  if(modeIntent)clearTimeout(modeIntent.timer);
  intent={version,value:value.original,notice:false,timer:null};
  modeIntent=intent;
  intent.timer=setTimeout(()=>{
   if(modeIntent===intent){intent.notice=true;updateSoundMode();}
  },500);
  errorNode.textContent="";
  updateSoundMode();
 }
 for(const node of nodes){controlPendingVersions.set(node,version);busy(node,true,"正在调整声音…");}
 if(priorityMute)updatePlayer();
 const current=()=>Boolean(intent&&modeIntent===intent)||nodes.some(node=>controlPendingVersions.get(node)===version);
 // 已发的写入保持顺序，跳过还没发出且已被替换的手动音轨选择。
 const send=async()=>{
  if(requestSignal.aborted)throw Object.assign(new Error(),{code:"cancelled"});
  if(musicGain&&!priorityMute&&version<musicMuteVersion)return null;
  if(intent&&!propagate&&!nodes.length&&modeIntent!==intent)return null;
  const payload=musicGain&&value.music_gain>0&&Number.isInteger(expectedMute)?
   {...value,music_mute_revision:expectedMute}:value;
  const result=await api("controls",payload,"",{signal:requestSignal,priority:"high"});
  if(requestSignal.aborted)throw Object.assign(new Error(),{code:"cancelled"});
  if(result.revision>=controls.control_revision)controls={...controls,...value,control_revision:result.revision};
  if(Number.isInteger(result.music_mute_revision)){
   controls.music_mute_revision=Math.max(controls.music_mute_revision||0,result.music_mute_revision);
  }
  return result;
 };
 const request=priorityMute?send():controlWrites.catch(()=>{}).then(send);
 if(!priorityMute)controlWrites=request;
 return request.then(async result=>{
  if(musicMuteSending===version){musicMuteSending=0;updatePlayer();}
  if(!result||(!current()&&!propagate))return;
  await waitForTV(()=>playback.applied_revision>=result.revision&&
   (!intent||!["preparing","playing"].includes(playback.state)||
    playback.backing_original==null||playback.backing_original===value.original),requestSignal);
  if(current())errorNode.textContent="";
 }).catch(error=>{
  if(error.code!=="cancelled"&&current()){
   errorNode.textContent=error.code==="music_mute_changed"?"音乐状态刚有变化，请按当前音量再操作。":
    ["tv_slow","receipt_unavailable"].includes(error.code)?(intent?"电视暂未确认音轨切换。":"电视暂未确认音量。"):"这次未能调整声音，请重试。";
   if(error.code==="music_mute_changed")probeSoon();
   if(["room_closed","room_unavailable","guest_unavailable"].includes(error.code))failure(error);
  }
  if(propagate)throw error;
 }).finally(()=>{
  if(musicMuteSending===version)musicMuteSending=0;
  signal?.removeEventListener("abort",abort);roomSignal.removeEventListener("abort",abort);
  if(intent&&modeIntent===intent){
   clearTimeout(intent.timer);
   modeIntent=null;
  }
  for(const field of fields){
   for(const id of controlFields[field]){
    const node=$(id);
    if(controlPendingVersions.get(node)!==version)continue;
    controlPendingVersions.delete(node);
    if((id==="music-value"&&!gainTimers.has("music"))||
       (id==="sfx-value"&&!gainTimers.has("sfx")))delete pendingControls[field];
    busy(node,false);
   }
  }
  updatePlayer();
 });
}
$("sound-button").onclick=()=>{updatePlayer();openDialog("sound-dialog");};$("play-toggle").onclick=()=>sendCommand(["playing","preparing"].includes(playback.state)?"pause":"resume",playback.current);$("skip-button").onclick=()=>sendCommand("next",playback.current);$("restart-button").onclick=()=>sendCommand("play",playback.current,"restart-button");
for(const [id,key]of volumeControls){const input=$(`${id}-volume`);input.oninput=()=>changeGain(id,key,Number(input.value));$(`${id}-minus`).onclick=()=>changeGain(id,key,Number(pendingControls[key]??controls[key])-10,0);$(`${id}-plus`).onclick=()=>changeGain(id,key,Number(pendingControls[key]??controls[key])+10,0);}
function updateVolumeButtons(id,level){
 const enabled=playback.online&&(id!=="music"||!musicMuteSending)&&(id!=="sfx"||playback.audio_capabilities?.sfx===true),range=$(`${id}-volume`);
 range.disabled=!enabled;
 if(range.dataset.level!==String(level)){
  range.dataset.level=String(level);
  range.style.setProperty("--volume-level",`${level}%`);
 }
 $(`${id}-minus`).disabled=!enabled||level<=0;
 $(`${id}-plus`).disabled=!enabled||level>=100;
}
const sfxCommands=new FridayPartyAudio.SfxCommands({send:payload=>api("command",payload),onState:state=>{$("sfx-status").textContent=FridayPartyAudio.sfxMessage(state);}});
function updatePartyAudio(){
 const supported=playback.audio_capabilities?.sfx===true;
 for(const id of["sfx-applause","sfx-cheer"])$(id).disabled=!playback.online||!supported;$("sfx-stop").disabled=!playback.online||(!supported&&!playback.sfx_seq&&!sfxCommands.state.serial);
 sfxCommands.observe(playback);
 setText($("sfx-status"),!playback.online?"等待电视连接":!supported?"电视暂未提供氛围音效":
  sfxCommands.state.phase==="idle"?"":FridayPartyAudio.sfxMessage(sfxCommands.state));
}
async function trackSfx(serial){const end=performance.now()+7500;while(serial===sfxCommands.state.serial&&performance.now()<end&&["sending","sent"].includes(sfxCommands.state.phase)){probeSoon();await new Promise(resolve=>setTimeout(resolve,125));}sfxCommands.observe(playback);}
for(const [id,action]of[["sfx-applause","sfx_applause"],["sfx-cheer","sfx_cheer"],["sfx-stop","sfx_stop"]])$(id).onclick=()=>{
 $("sfx-error").textContent="";const request=sfxCommands.trigger(action),serial=sfxCommands.state.serial;
 request.then(()=>trackSfx(serial)).catch(error=>{$("sfx-error").textContent=error.code==="sfx_unavailable"?"电视暂未提供氛围音效":"暂未发送成功，请重试";});
};
$("atmosphere-button").onclick=()=>{updatePlayer();openDialog("atmosphere-dialog");};
function canReorder(song){return isUpcoming(song)&&song.lyric_state!==-1;}
function songActions(song){
 actionSong=song;
 $("song-action-title").textContent=song.title;
 $("song-error").textContent="";
 refreshSongActions(true);
 openDialog("song-dialog");
}
function refreshSongActions(opening=false){
 if(!actionSong||!opening&&!$("song-dialog").open)return;
 const song=selected.find(item=>item.id===actionSong.id);
 if(song)actionSong=song;
 const current=song?.id===playback.current;
 const ready=!!song&&(current||isPlayable(song)),connected=playback.online&&playbackAvailable;
 const play=$("sing-now"),label=current?"重新演唱":"现在唱";
 if(play.textContent!==label)play.textContent=label;
 if(opening||ready)play.hidden=!ready;
 play.disabled=!ready||!connected||commandBusy;
 $("move-top").hidden=!song||!canReorder(song)||selected.find(canReorder)?.id===song.id;
 const peers=song&&canReorder(song)?selected.filter(item=>canReorder(item)&&queueGroupName(item)===queueGroupName(song)):[];
 const index=peers.indexOf(song),up=$("move-up"),down=$("move-down"),withdraw=$("withdraw-song");
 if(up)up.hidden=index<=0;
 if(down)down.hidden=index<0||index>=peers.length-1;
 if(withdraw){
  const item=song&&addSnapshot.items.find(value=>value.key===songKey(song));
  withdraw.hidden=!canWithdraw(song);
  withdraw.disabled=!!item?.withdraw&&item.state!=="failed";
  setText(withdraw,item?.withdraw?(withdraw.disabled?"正在撤回…":"重试撤回"):"撤回点歌");
 }
 const focused=document.activeElement;
 if(focused?.hidden&&[up,down,withdraw].includes(focused)){
  const next=focused===up?down:focused===down?up:null;
  (next&&!next.hidden?next:$("song-action-title")).focus({preventScroll:true});
 }
 $("retry-song").hidden=song?.state!=="failed";
 $("find-song-version").hidden=song?.state!=="failed"&&song?.lyric_state!==-1;
 const notice=$("song-availability");
 if(!notice)return;
 const error=$("song-error");
 let text="";
 if(!song)text="这首已从歌单移除。";
 else if(!play.hidden){
  if(!connected)text="等待电视连接。";
  else if(!ready){
   if(song.lyric_state===-1)text="暂无同步歌词。";
   else if(song.lyrics_pending)text=songReadinessText.lyrics_not_ready;
   else text=songReadinessText[song.state==="ready"?"media_not_ready":"song_not_ready"];
  }
 }
 if(ready&&notice.textContent&&error.textContent===notice.textContent)error.textContent="";
 if(notice.textContent!==text)notice.textContent=text;
 notice.hidden=!text||text===error.textContent;
}
async function saveOrder(ids,revision=queueRevision){
 if(ordering)return;
 const previous=selected.map(song=>song.id),rows=new Map(selected.map(song=>[song.id,song]));
 ordering=true;
 selected=ids.map(id=>rows.get(id));
 if(mode==="queue")render();
 try{
  await api("order",{ids,revision});
  queueRevision=Math.max(queueRevision,revision+1);
  toast("顺序已更新");
 }catch(error){
  const latest=new Map(selected.map(song=>[song.id,song]));
  selected=mergeQueueOrder(previous).map(id=>latest.get(id));
  failure(error);
 }finally{
  ordering=false;
  renderQueueStatus(true);
  await refresh();
 }
}
function mergeQueueOrder(ids){
 const current=new Set(selected.map(song=>song.id));
 const order=ids.filter(id=>current.has(id));
 const scope=new Set(order);
 let index=0;
 return selected.map(song=>scope.has(song.id)?order[index++]:song.id);
}
function moveSong(id,direction){
 const song=selected.find(item=>item.id===id);
 if(!song||!canReorder(song)||ordering)return;
 const group=queueGroupName(song);
 const pending=selected.filter(item=>canReorder(item)&&(direction==="top"||queueGroupName(item)===group)).map(item=>item.id);
 const from=pending.indexOf(id);
 const to=direction==="top"?0:Math.max(0,Math.min(pending.length-1,from+direction));
 if(from===to)return;
 pending.splice(from,1);pending.splice(to,0,id);
 return saveOrder(mergeQueueOrder(pending));
}
$("sing-now").onclick=()=>{
 if(!actionSong||$("sing-now").disabled)return;
 const song=selected.find(row=>row.id===actionSong.id);
 if(!song||(song.id!==playback.current&&!isPlayable(song))){
  refreshSongActions();
  return;
 }
 $("song-error").textContent="";
 return sendCommand("play",song.id,"sing-now");
};
$("move-top").onclick=()=>{
 if(!actionSong)return;
 $("song-dialog").close();
 moveSong(actionSong.id,"top");
};
for(const [id,direction]of [["move-up",-1],["move-down",1]]){
 const button=$(id);
 if(button)button.onclick=()=>{if(actionSong&&!button.hidden)moveSong(actionSong.id,direction);};
}
if($("withdraw-song"))$("withdraw-song").onclick=()=>{
 const song=selected.find(row=>row.id===actionSong?.id);
 if(!canWithdraw(song))return;
 const item=addSnapshot.items.find(value=>value.key===songKey(song));
 if(item?.withdraw){if(item.state==="failed")addQueue?.retry(item.key);return;}
 setText($("song-error"),"");
 withdrawSong(song,song.id);
};
$("retry-song").onclick=async()=>{
 const song=actionSong,button=$("retry-song");
 if(song?.state!=="failed"||button.disabled)return;
 button.disabled=true;busy(button,true,"正在重新准备…");
 try{
  await api("retry",{id:song.id,request_id:crypto.randomUUID()});
  $("song-dialog").close();
  toast("正在重新准备");
  await refresh();
 }catch(error){failure(error);}
 finally{button.disabled=false;busy(button,false);}
};
$("find-song-version").onclick=()=>{
 if(!actionSong)return;
 const title=actionSong.title;
 $("song-dialog").close();
 searchTitles(title||"");
};
function dragRows(group){return Array.from(group.children).filter(node=>node.dataset.queue);}
function startDrag(event,song,handle){
 if(event.button===0)draggedHandle=null;
 if(event.button!==0||drag||ordering||!canReorder(song))return;
 const row=handle.closest("article"),group=row?.parentElement;
 if(!group||dragRows(group).length<2)return;
 const surface=$("results");
 drag={row,group,surface,handle,pointer:event.pointerId,startY:event.clientY,y:event.clientY,
       grab:event.clientY-row.getBoundingClientRect().top,revision:queueRevision,moved:false,offset:0,frame:null};
 window.addEventListener("pointermove",trackDrag);
 window.addEventListener("pointerup",dropDrag);
 window.addEventListener("pointercancel",cancelDrag);
 surface.addEventListener("lostpointercapture",lostDragCapture);
}
function trackDrag(event){
 const active=drag;
 if(!active||event.pointerId!==active.pointer)return;
 active.y=event.clientY;
 if(!active.moved){
  if(Math.abs(active.y-active.startY)<6)return;
  // 容器保持在原位，即使歌曲行移动也能继续收到本次手势。
  active.surface.setPointerCapture(event.pointerId);
  active.moved=true;
  active.row.classList.add("dragging");
 }
 if(active.frame===null)active.frame=requestAnimationFrame(updateDrag);
}
function updateDrag(){
 const active=drag;
 if(!active?.moved)return;
 active.frame=null;
 placeDraggedSong(active);
 const player=$("player"),bottom=player.hidden?innerHeight:player.getBoundingClientRect().top;
 const edge=72,distance=active.y>bottom-edge?active.y-(bottom-edge):active.y<edge?active.y-edge:0;
 if(distance){
  const before=window.scrollY;
  window.scrollBy(0,Math.sign(distance)*Math.min(24,Math.max(3,Math.round(Math.abs(distance)/4))));
  if(window.scrollY!==before)active.frame=requestAnimationFrame(updateDrag);
 }
}
function placeDraggedSong(active){
 const {row,group,y}=active;
 const position=y+window.scrollY;
 if(!active.anchors)measureDrag(active);
 const target=active.anchors.find(anchor=>position<anchor.center)?.node||null;
 if(row.nextElementSibling!==target){group.insertBefore(row,target);measureDrag(active);}
 const offset=position-active.grab-active.top;
 if(offset!==active.offset){active.offset=offset;row.style.setProperty("--drag-offset",offset+"px");}
}
function measureDrag(active){
 const scroll=window.scrollY;
 active.anchors=dragRows(active.group).filter(node=>node!==active.row).map(node=>{
  const rect=node.getBoundingClientRect();
  return {node,center:scroll+rect.top+rect.height/2};
 });
 active.top=scroll+active.row.getBoundingClientRect().top-active.offset;
}
function endDrag(drop){
 const active=drag;
 if(!active)return;
 if(active.frame!==null)cancelAnimationFrame(active.frame);
 if(drop&&active.moved)placeDraggedSong(active);
 drag=null;
 const {row,surface,pointer}=active;
 window.removeEventListener("pointermove",trackDrag);
 window.removeEventListener("pointerup",dropDrag);
 window.removeEventListener("pointercancel",cancelDrag);
 surface.removeEventListener("lostpointercapture",lostDragCapture);
 if(surface.hasPointerCapture(pointer))surface.releasePointerCapture(pointer);
 row.classList.remove("dragging");
 row.style.removeProperty("--drag-offset");
 if(active.moved)draggedHandle=active.handle;
 if(drop&&active.moved){
  const ids=mergeQueueOrder(dragRows(active.group).map(node=>node.dataset.queue));
  if(ids.some((id,index)=>id!==selected[index].id)){
   saveOrder(ids,active.revision);
   return;
  }
 }
 renderQueueStatus(active.moved);
 refresh();
}
function dropDrag(event){if(event.pointerId===drag?.pointer){drag.y=event.clientY;endDrag(true);}}
function cancelDrag(event){if(drag&&(!event||event.pointerId===drag.pointer))endDrag(false);}
function lostDragCapture(event){if(event.target===drag?.surface)cancelDrag(event);}
window.addEventListener("resize",()=>{if(drag)drag.anchors=null;});
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
function savedDeviceKey(){
 const stored=readGuestStorage("localStorage",deviceStorageKey);
 if(randomSecret.test(stored))return stored;
 try{
  const backup=document.cookie.split(";").map(value=>value.trim()).find(value=>value.startsWith(deviceCookie+"="))?.split("=")[1]||"";
  if(randomSecret.test(backup))return backup;
 }catch{}
 return readGuestStorage("sessionStorage",deviceStorageKey);
}
async function joinPayload(includeRoomKey){
 if(!globalThis.crypto?.getRandomValues)return {snapshot:true};
 const restored=savedDeviceKey();
 const device=randomSecret.test(restored)?restored:await sharedLocalSecret(guestSeedKey);
 writeGuestStorage("localStorage",deviceStorageKey,device);
 try{
  if(location.protocol==="https:"&&!document.cookie.includes(deviceCookie+"=")){
   document.cookie=`${deviceCookie}=${device}; Path=${location.pathname}; Max-Age=31536000; Secure; SameSite=Strict`;
  }
 }catch{}
 const proposed=includeRoomKey?await automaticGuestKey():"",profile=savedGuestProfile();
 return {snapshot:true,device_key:device,...(proposed?{guest_key:proposed}:{}),...(profile?{profile}:{})};
}
function attachRoom(next){
 room=next.room;key=next.key;valid=true;joinFailures=0;statusFailures=0;lastStatusSuccess=0;lastStatusProbe=-Infinity;
 lastAddGuestRecovery=-Infinity;
 roomMusicVolume=new RoomMusicVolume(room,roomGainStorage);roomResumeAt=performance.now();
 writeGuestStorage("sessionStorage","friday-ktv-room",JSON.stringify({room,key}));
}
function activateGuest(guest,snapshot,started){
 identity=guest;joinFailures=0;saveIdentity({prefer:true});message("");connectionNotice("");
 $("profile-button").disabled=$("settings-button").disabled=false;
 if(usableStatus(snapshot)){
  const now=performance.now();
  if(snapshot.server_time_ms)earClock.observe(snapshot,started,now);
  lastStatusProbe=now;applyStatus(snapshot);
 }
 render();initAddQueue({authenticated:true});clearInterval(refreshTimer);startStatusFeed();loadDiscover();
 if(!usableStatus(snapshot))refresh();
 if(mode==="frequent")loadFrequent();
}
async function enterCurrentRoom(expired=false){
 if(roomLookup||document.hidden)return;
 const controller=new AbortController();
 roomLookup=controller;
 controlRoom.abort();controlRoom=new AbortController();controlWrites=Promise.resolve();
 musicMuteVersion=musicMuteSending=0;
 if(commandRequest)busy(commandRequest.button,false);
 commandRequest=null;commandBusy=false;
 if(modeIntent)clearTimeout(modeIntent.timer);
 modeIntent=null;
 for(const node of controlPendingVersions.keys())busy(node,false);
 controlPendingVersions.clear();
 const retainedKey=identity?.key||"";
 valid=false;identity=null;joinKey="";themeIntent=null;themeRevision=0;
 const historyRequest=frequentRequest;frequentRequest=null;historyRequest?.controller.abort();
 clearTimeout(joinTimer);stopRoomStatus(true);statusFeed=null;stopDiscover();
 if(expired){
  addQueue?.stop("room_closed");addQueue?.close();addQueue=null;
  addSnapshot={items:[],stopped:"",persistence:"memory"};renderPendingAdds();
  cancelDrag();searchComposing=false;cancelSearchInput();clearSearchResults();
  playlistId="";playlistTitle="";mode="home";
  selected=[];queueRevision=0;queueWithdrawReady=false;playableOrder=null;playbackSignature="";
  playback={online:false,current:"",played:[],state:"idle"};
  controls={music_gain:70,sfx_gain:60,control_revision:0};pendingControls={};controlPendingVersions.clear();
  for(const timer of gainTimers.values())clearTimeout(timer);
  gainTimers.clear();
  invalidateGuide({disable:true});updatePlayer();
 }
 try{sessionStorage.removeItem("friday-ktv-room");}catch{}
 $("profile-button").disabled=$("settings-button").disabled=true;
 $("my-name").textContent="正在加入…";
 for(const sheet of document.querySelectorAll("dialog[open]"))sheet.close();
 render();
 const opening=expired?"本场已结束，正在进入新场…":"正在连接当前房间…";
 message(opening);
 try{
  const started=performance.now(),options={signal:controller.signal,timeoutMs:7500,priority:"high",guestKey:retainedKey};
  const joined=await api("current",await joinPayload(false),"",options).catch(error=>{
   if([404,405].includes(error.status)&&error.code!=="no_active_room")return null;
   throw error;
  });
  const next=joined?.room||await api("current",null,"",options);
  if(!validRoom(next?.room,next?.key))throw Object.assign(new Error(),{code:"service_unavailable"});
  attachRoom(next);
  if(joined?.guest)activateGuest(joined.guest,joined.status,started);
 }catch(error){
  if(!controller.signal.aborted){
   const closed=error.code==="no_active_room";
   $("my-name").textContent=closed?"等待开场":"正在连接…";
   message(closed?"当前没有正在进行的场次。开场后会自动加入。":opening);
   if(!document.hidden)joinTimer=setTimeout(()=>enterCurrentRoom(),closed?5000:1800);
  }
 }finally{if(roomLookup===controller)roomLookup=null;}
 if(valid&&!identity)void join();
}
async function join(){
 if(joining||document.hidden)return;
 if(!valid){void enterCurrentRoom();return;}
 const joinedRoom=room;joining=true;clearTimeout(joinTimer);
 if(!identity){$("my-name").textContent="正在加入…";$("empty").textContent="正在连接本场…";}
 try{
  const retainedKey=`friday-ktv-room-credential:${room}`;
  const known=[identity?.key,readGuestStorage("localStorage",retainedKey),readGuestStorage("sessionStorage",`friday-ktv-join:${room}`)]
   .find(candidate=>typeof candidate==="string"&&guestCredential.test(candidate));
  const payload=await joinPayload(true),proposed=payload.guest_key,started=performance.now();
  if(!valid||room!==joinedRoom)return;
  let result;
  try{result=await api("join",payload,"",{guestKey:known||"",priority:"high"});}
  catch(error){
   if(error.status!==400||error.code!=="invalid_request")throw error;
   const profile=payload.profile?.avatar<12?payload.profile:undefined;
   result=await api("join",{guest_key:proposed,...(profile?{profile}:{})},"",{guestKey:known||"",priority:"high"});
  }
  const {status,...guest}=result;
  if(guestCredential.test(guest.key||"")&&(guest.key!==proposed||known)){
   writeGuestStorage("localStorage",retainedKey,guest.key!==proposed?guest.key:"");
  }
  activateGuest(guest,status,started);
 }catch(error){
  if(error.code==="cancelled")return;
  failure(error);
  if(!valid)return;
  if(connectionError(error)||error.code==="rate_limited"){
   joinFailures=Math.min(4,joinFailures+1);
   if(!document.hidden)joinTimer=setTimeout(join,Math.max(error.retryAfterMs||0,1000*2**(joinFailures-1)));
  }else{$("my-name").textContent="暂未加入";$("empty").textContent="刷新页面后可重新加入本场。";}
 }finally{joining=false;if(valid&&room!==joinedRoom)void join();}
}
function resumeRoom(){
 if(!identity){join();return;}
 roomResumeAt=performance.now();initAddQueue();startStatusFeed();refresh();loadDiscover();resumeSearchView();
 if(mode==="queue")observeArtwork();
 if(mode==="frequent"){loadFrequent();observeArtwork();}
}
join();document.addEventListener("visibilitychange",()=>{if(document.hidden){stopRoomStatus();stopDiscover();cancelDrag();searchComposing=false;cancelSearchInput();}else resumeRoom();});
window.addEventListener("online",()=>{if(!document.hidden)resumeRoom();});
window.addEventListener("pagehide",()=>{roomLookup?.abort();roomLookup=null;addQueue?.close();stopRoomStatus();stopDiscover();cancelDrag();searchComposing=false;cancelSearchInput();clearTimeout(joinTimer);});
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
   const inBackingMode=controls.original===0&&(playback.applied_revision??0)>=controls.control_revision&&
    (playback.backing_original==null||playback.backing_original===0)&&modeIntent?.value!==1;
   const [original]=await Promise.all([verifiedGuide(op,op.spec),inBackingMode?null:setControl({original:0},{signal:op.controller.signal,propagate:true})]);
   checkGuide(op);if(controls.original!==0||modeIntent?.value===1)throw appGuideError("changed");return original;
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
if(typeof ResizeObserver!=="undefined")new ResizeObserver(entries=>{
 const player=$("player"),height=player.hidden?0:entries[0].borderBoxSize?.[0]?.blockSize??player.getBoundingClientRect().height;
 document.documentElement.style.setProperty("--player-height",`${Math.ceil(height)}px`);
}).observe($("player"));
