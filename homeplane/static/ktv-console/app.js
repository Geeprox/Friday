export function mountKtv(target,{endpoint='/v1',onBack=null,embedded=false,partyAudio=globalThis.FridayPartyAudio,readOnly=false}={}){
let alive=true;const lifetime=new AbortController();
const $ = id => shell.querySelector('#ktv-'+id);
const el = (tag, text, cls) => { const n=document.createElement(tag); if(text!==undefined&&text!==null)n.textContent=text;if(cls)n.className=cls;return n; };
const tabs=['room','search','queue','cache','settings','jobs'];
const primaryTabs=[['room','正在唱'],['search','点歌'],['queue','已点'],['cache','歌曲文件'],['settings','设置']];
const commandObserved=partyAudio?.commandObserved||(()=>false);
const volumeControls=[['music_gain','音乐音量'],['sfx_gain','氛围音量']];
const phase={queued:'排队中',acquiring:'获取原曲',waiting_storage:'等待 Mac 腾出缓存空间',waiting_separation:'等待制作伴奏',waiting_memory:'等待内存',separating:'生成伴奏',waiting_transcode:'等待封装',transcoding:'准备播放文件',video:'匹配 MV',ready:'已准备好',failed:'准备失败',cancelled:'已取消',source_authorizing:'验证下载权限',source_downloading:'下载原曲',source_cache_hit:'命中原曲缓存',decoding:'校验原曲'};
const errors={
 music_mute_changed:'音乐状态刚有变化，请按当前音量再调整。',
 song_not_ready:'这首歌正在准备，稍后就能唱。',
 media_not_ready:'所选音轨暂时不可用，可切换演唱模式或稍后重试。',
 lyrics_not_ready:'正在找同步歌词，准备好后可唱。',
 lyrics_unavailable:'这个版本暂无同步歌词，请换一个版本。',
 failed_history_already_prepared:'同一版本已准备完成，可删除这条旧失败记录。',
 failed_history_source_changed:'歌单中的歌曲已换用其他版本。',
 failed_history_hidden:'这条失败记录已删除，请刷新。',
 cache_miss:'暂时找不到这个版本的制备文件。',
 remove_unavailable:'暂时无法删除这条失败记录。',
 retry_in_progress:'这个版本正在制备。',
 mac_offline:'Mac 制备服务暂未连接。',
 background_unavailable:'暂时无法制作歌曲背景。',
 daily_images_reached:'已达每日生成上限，可在这里调整。',
 invalid_background_limit:'请输入大于或等于 0 的整数。',
 api_quota_exhausted:'OpenAI API 额度不足。',
 api_rate_limited:'OpenAI API 暂时限流。',
 cloud_images_disabled:'歌曲背景已关闭。',
 cloud_capability_disabled:'Friday 的当前服务模式未启用生图。',
 api_key_missing:'请先在 Friday 配置 OpenAI。',
 api_key_unavailable:'Friday 暂时无法读取 OpenAI 配置。',
 api_key_rejected:'OpenAI 未接受当前凭据。',
 api_model_unavailable:'OpenAI 暂不支持当前生图模型。',
 api_unavailable:'暂时连不上 OpenAI。',
 api_timeout:'OpenAI 生图请求超时。',
 private_song_metadata:'暂未匹配到这首云盘歌曲的公开发行资料。',
 background_recording_unverified:'暂未核对到公开曲库的歌曲资料。',
 background_catalog_unavailable:'暂时无法读取公开曲库。',
 background_storage_limit_reached:'已达 Friday 背景缓存设置上限。',
 background_queue_full:'背景正在排队，请稍后再试。',
 ktv_response_timeout:'KTV 服务响应较慢，请重试。',
 ktv_response_unavailable:'暂时无法读取 KTV 服务响应，请重试。',
 cache_changed:'文件或歌单已变化，请刷新后重新选择。',archive_changed:'NAS 归档已变化，请刷新后重新选择。',cache_in_use:'所选歌曲正在使用或制作，请稍后再试。',plan_expired:'删除预览已过期，请重新选择。',nas_management_unavailable:'NAS 暂未连接，恢复连接后可继续操作。',tv_offline:'电视离线，请开机后再操作。',room_changed:'房间已变化，请刷新后重试。',queue_changed:'歌单已更新，请刷新后重新选择。',song_playing:'当前歌曲正在播放，请先切歌。',sfx_unavailable:'电视氛围音效暂不可用。',source_login_required:'请在 KTV 设置的音源授权中登录。',source_rights_denied:'这个版本未能取得可用音频。',source_api_unavailable:'音源暂未连接，请稍后重试。',source_file_invalid:'原曲文件不完整，请重新准备。',download_timeout:'原曲下载未完成，请重试或换一个版本。',separation_failed:'伴奏生成失败，请重试。',transcode_failed:'播放文件封装失败，请重试。',storage_full:'存储空间不足，请调整上限或删除空闲歌曲。',search_unavailable:'暂时无法搜索，请稍后重试。',queue_full:'歌单已满，请先移除不需要的歌曲。',rate_limited:'操作较频繁，请稍等片刻再试。',cloud_song_unavailable:'暂时无法取得这个音乐云盘文件。',failed_history_source_unknown:'这条记录的原音源待确认，请重新搜索该曲。',retry_unavailable:'本次制备暂不能重试。',retry_limit:'本条记录的重试次数已用完。',invalid_policy:'请输入有效的容量上限（2–102400 GB）。',unauthorized:'管理会话已过期，请刷新页面。',ktv_offline:'KTV 暂未连接。请确认本机 KTV 服务已运行，再重试连接。',ktv_not_configured:'家庭 KTV 尚未配置。完成本机连接后即可使用。',ktv_config_unavailable:'KTV 连接配置暂不可读，请检查本机配置后重试。',ktv_read_only:'当前窗口只读，请在本机 Friday 窗口操作。',ktv_unavailable:'KTV 暂时无法完成操作，请稍后重试。',unavailable:'连接未完成，请检查服务后重试。'};
const themes=[['content','内容与留白'],['festival','音乐节'],['live','Live']], sourceNames={song:'曲库',mv:'MV',cloud:'音乐云盘'};
const reasons={preparing:'正在制作',in_queue:'当前歌单正在使用',transferring:'正在归档或校验',unsafe:'文件需要核对',nas_offline:'NAS 离线，暂不能核对使用情况'};
function size(bytes){
 if(bytes==null)return '暂不可读';
 if(bytes>=1024**3)return `${(bytes/1024**3).toFixed(2)} GB`;
 if(bytes>=1024**2)return `${(bytes/1024**2).toFixed(1)} MB`;
 if(bytes>=1024)return `${(bytes/1024).toFixed(1)} KB`;
 return `${bytes} B`;
}
const date=n=>n?new Date(n*1000).toLocaleString('zh-CN',{hour12:false}):'暂无记录';
let view='room',viewRequest=null,changing=false,room=null;
let roomSyncRequest=null,roomSyncError='',queueDrag=null,queueHistoryOpen=false,queueSelecting=false;
let roomUi=null,queueUi=null,pendingCommand=null,pendingMix=null,pendingMute=null;
let transportFeedback='',soundFeedback='',lastMusicGain=70;
const mixDraft=new Map();
let cacheData=null,cacheQuery='',cachePresence='all',cacheSort='recent',scope='mac';
let page=0,deletePlan=null,lastCheckbox=-1;
let selectingFiles=false;
let libraryFilterTimer=null,libraryIndexTimer=null;
const libraryPageSize=50;
const cacheHosts=['mac','nas'];
const cacheScopes=[['mac','仅 Mac'],['nas','仅 NAS'],['both','Mac 和 NAS']];
const libraryReadError='歌曲文件暂时无法读取。';
const libraryLoading='正在读取歌曲文件…';
let searchData=null,searchQuery='',searchSource='song',searchOffset=0;
let searchRequest=null,searchRowsSignature='';
const songMetadata=new Map(),songPresentations=new Set(),artworkQueue=new Map();
const artworkTargets=new WeakMap();
let artworkTimer=null,artworkBusy=false,artworkObserver=null,artworkRequest=null;
let sourceSession=null;
let backgroundView=null;
let retryStorageSettings=null;
const storagePolicies=new Map();
const renderSignatures=new Map();
const selected=new Map(),queueSelected=new Set(),searchSelected=new Set(),addRequests=new Map(),added=new Set();
function message(text,error=false){if(!alive)return;$('status').textContent=text;$('status').classList.toggle('error',error);}
function text(node,value){if(node.textContent!==value)node.textContent=value;}
function attribute(node,name,value){if(node.getAttribute(name)!==value)node.setAttribute(name,value);}
function explain(error){return errors[error.message]||errors.unavailable;}
function button(text,fn,options={}){const b=el('button',text,options.className);b.type='button';b.disabled=!!options.disabled;b.onclick=fn;return b;}
function field(label,node){if(!node.id)node.id='ktv-field-'+crypto.randomUUID();const wrap=el('label',null,'field');wrap.htmlFor=node.id;wrap.append(el('span',label),node);return wrap;}
function select(options,value){const n=el('select');for(const[key,label]of options){const o=el('option',label);o.value=key;n.append(o);}n.value=value;return n;}
async function api(path,body,signal=lifetime.signal){
 if(!alive)throw new Error('disposed');if(readOnly&&body!==undefined)throw new Error('ktv_read_only');
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),path==='manage/delete'?65000:18000);
 try{const response=await fetch(endpoint+'/'+path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store',credentials:'same-origin',signal:AbortSignal.any([controller.signal,lifetime.signal,signal])});const data=await response.json();if(!response.ok)throw new Error(data.detail||data.code||'unavailable');return data;}finally{clearTimeout(timer);}
}
function libraryPath({offset=page*libraryPageSize,limit=libraryPageSize,selectable=selectingFiles?scope:undefined}={}){
 const query=new URLSearchParams({offset,limit,q:cacheQuery,presence:cachePresence,sort:cacheSort});
 if(selectable)query.set('selectable',selectable);
 return 'manage/library?'+query;
}
function loadCachePage(){
 if(view!=='cache'||changing)return;
 viewRequest?.abort();viewRequest=null;
 void refresh();
}
function resetContent(){
 roomUi=queueUi=queueDrag=null;searchRowsSignature='';
 $('summary').replaceChildren();$('content').replaceChildren();songPresentations.clear();artworkObserver?.disconnect();
 if(view==='jobs')$('content').append(button('返回已点',()=>setView('queue'),{className:'ktv-inline-back'}));
}
function roomSignature(data){return JSON.stringify(data,(key,value)=>['server_time_ms','position_ms','position_at','cursor'].includes(key)?undefined:value);}
async function refresh({quiet=false}={}){
 if(!alive||document.hidden||changing||queueDrag)return;
 if(view==='settings'){
  viewRequest?.abort();
  viewRequest=new AbortController();
  resetContent();
  if(!quiet)message('');
  renderSettings(viewRequest.signal);
  return;
 }
 if(viewRequest)return;
 const current=view,request=new AbortController(),signal=request.signal;
 const loadingText=current==='cache'?libraryLoading:'正在读取…';
 viewRequest=request;
 $('refresh').disabled=true;
 if(!quiet){
  if(current==='cache'&&!$('library-rows'))$('content').replaceChildren();
  message(loadingText);
 }
 try{
  const path=current==='jobs'?'jobs':current==='cache'?libraryPath():'manage/room';
  const data=await api(path,undefined,signal);
  if(signal.aborted||changing||queueDrag)return;
  if(current==='cache'&&data.indexing){
   if(!$('library-rows')){$('content').replaceChildren();message(libraryLoading);}
   clearTimeout(libraryIndexTimer);
   libraryIndexTimer=setTimeout(()=>{if(view==='cache')void refresh({quiet:true});},750);
   return;
  }
  if(current==='cache')clearTimeout(libraryIndexTimer);
  if(current==='cache'&&page>0&&page*libraryPageSize>=data.total){
   page=Math.max(0,Math.ceil(data.total/libraryPageSize)-1);
   queueMicrotask(loadCachePage);
   return;
  }
  if(['room','queue','search'].includes(current)&&!adoptRoom(data))return;
  const signature=roomSignature(data);
  const clearStatus=!quiet||current==='cache'&&[libraryReadError,libraryLoading].includes($('status').textContent);
  if(quiet&&renderSignatures.get(current)===signature){
   if(clearStatus)message('');
   return;
  }
  renderSignatures.set(current,signature);
  if(current==='jobs'||current==='cache'&&!$('library-rows'))resetContent();
  if(data?.simulated)shell.querySelector('.ktv-context').textContent='合成验证 · 本机管理';
  if(current==='cache'){
   cacheData=data;if(!data.nas.configured)scope='mac';
   if($('library-rows')){
    renderCacheTotals(data);renderCacheRows();
   }else renderCache(data);
  }
  else if(current==='jobs')renderJobs(data.jobs);
  else{
   renderRoomView();
  }
  if(clearStatus)message('');
 }catch(error){
  if(signal.aborted||changing||queueDrag)return;
  if(current==='cache'){
   message(libraryReadError,true);
   if(!$('content').children.length)$('content').append(button('重试读取歌曲文件',()=>refresh(),{className:'primary'}));
  }else{
   message(explain(error),true);
   if(!$('content').children.length)renderDisconnected(error);
  }
 }finally{
  if(viewRequest===request){
   viewRequest=null;$('refresh').disabled=false;
   if(!quiet&&(signal.aborted||changing||queueDrag)&&$('status').textContent===loadingText)message('');
  }
 }
}
async function action(b,operation,success){
 if(changing||!alive)return;
 if(readOnly){message(errors.ktv_read_only,true);return;}
 roomSyncRequest?.abort();changing=true;if(b)b.disabled=true;
 try{
  let data;
  try{data=await operation();}finally{changing=false;}
  if(!alive)return;
  await refresh({quiet:true});
  message(typeof success==='function'?success(data):success||'已保存。');return data;
 }catch(error){
  if(!alive)return;
  if(['queue_changed','room_changed'].includes(error.message))await refresh({quiet:true});
  message(explain(error),true);
 }finally{if(b?.isConnected)b.disabled=false;}
}
function adoptRoom(data){
 const incomingTime=data.status?.server_time_ms??data.server_time_ms,currentTime=room?.status?.server_time_ms??room?.server_time_ms;
 if(incomingTime&&currentTime&&incomingTime<currentTime)return false;
 if(room?.room_id===data.room_id&&(data.status?.queue_revision??0)<(room.status?.queue_revision??0))return false;
 const previous=room?.room_id,wasAdded=new Set(added);room=data;
 if(previous&&previous!==data.room_id){
  added.clear();addRequests.clear();queueSelected.clear();queueSelecting=false;queueHistoryOpen=false;
  clearRoomIntents();mixDraft.clear();transportFeedback=soundFeedback='';lastMusicGain=70;
  if(view==='room'||view==='queue')resetContent();
 }
 const pending=new Set((data.status?.songs||[]).filter(s=>s.id===data.status.playback.current||!data.status.playback.played.includes(s.id)&&s.state!=='cancelled').map(s=>s.source+':'+s.song_id));
 for(const key of wasAdded)if(!pending.has(key))addRequests.delete(data.room_id+':'+key);
 added.clear();for(const key of pending)added.add(key);
 if(data.status?.controls?.music_gain>0)lastMusicGain=data.status.controls.music_gain;
 observeRoomIntents(data.status);
 return true;
}
function renderRoomView(){
 if(view==='queue')renderQueueView(room);
 else if(view==='search'){
  if(!$('search-results')){resetContent();renderSearch();}
  else{
   updateSearchSummary();
   if(searchAvailability()!==searchRowsSignature)renderSearchRows();
  }
 }else if(view==='room'){
  if(!room.active&&!roomUi&&$('content').querySelector('.ktv-empty-state'))return;
  if(!room.active){resetContent();renderNoRoom();}
  else renderRoom(room.status);
 }
}
async function syncRoom(){
 if(roomSyncRequest||changing||queueDrag||viewRequest||!alive)return;
 const request=new AbortController(),current=view;roomSyncRequest=request;
 try{
  const data=await api('manage/room',undefined,request.signal);
  if(request.signal.aborted||!alive||current!==view||changing||queueDrag||!adoptRoom(data))return;
  const signature=roomSignature(data);
  if(renderSignatures.get(current)!==signature){renderSignatures.set(current,signature);renderRoomView();}
  if(roomSyncError&&$('status').textContent===roomSyncError)message('');
  roomSyncError='';
 }catch(error){if(!request.signal.aborted&&alive&&current===view&&!changing&&!queueDrag){roomSyncError=explain(error);message(roomSyncError,true);}}
 finally{if(roomSyncRequest===request)roomSyncRequest=null;}
}
function roomAction(name,value,roomId=room?.room_id,signal=lifetime.signal){return api('manage/room/'+name,name==='preferences'?value:{room_id:roomId,value},signal);}
function recording(song){
 const id=String(song.song_id||song.id?.split(':').at(-1)||'');
 const source=song.source||(song.id?.startsWith('cloud:')?'cloud':'song');
 return /^[1-9][0-9]{0,17}$/.test(id)&&['song','cloud'].includes(source)?{id,source,key:source+':'+id}:null;
}
function artworkUrl(value){
 if(typeof value!=='string')return '';
 if(/^data:image\/(?:jpeg|png|webp);base64,[A-Za-z0-9+/=]+$/.test(value))return value;
 try{const url=new URL(value);return url.protocol==='https:'&&/(^|\.)music\.126\.net$/.test(url.hostname)?url.href:'';}catch{return '';}
}
function artistsOf(value){return Array.isArray(value)?value.filter(item=>typeof item==='string').join(' / '):typeof value==='string'?value:'';}
function paintSong(presentation){
 const {song,cover,title,metadata}=presentation,identity=recording(song),stored=identity&&songMetadata.get(identity.key)||{};
 text(title,stored.title||song.title);
 const artists=artistsOf(stored.artists)||artistsOf(song.artists),album=stored.album||song.album||'';
 text(metadata,[artists,album].filter(Boolean).join(' · ')||(stored.complete?'暂无歌曲资料':'正在读取歌曲资料…'));
 metadata.setAttribute('aria-label',['歌手',artists||'待补全','专辑',album||'待补全'].join('：'));
 const image=artworkUrl(stored.artwork||song.artwork||song.cover);
 if(image&&cover.dataset.url!==image){
  cover.dataset.url=image;const img=el('img');img.src=image;img.alt='';img.loading='lazy';img.decoding='async';img.referrerPolicy='no-referrer';
  img.onerror=()=>{cover.replaceChildren(el('span','♪'));cover.dataset.url='';};cover.replaceChildren(img);
 }
}
function songPresentation(song,{compact=false}={}){
 const card=el('div',null,'song-presentation'+(compact?' compact':'')),cover=el('div',null,'song-cover'),info=el('div',null,'entry-info'),title=el(compact?'strong':'h3'),metadata=el('p',null,'song-metadata');
 cover.setAttribute('aria-hidden','true');cover.append(el('span','♪'));
 metadata.tabIndex=0;info.append(title,metadata);card.append(cover,info);
 const presentation={song,card,cover,title,metadata};songPresentations.add(presentation);paintSong(presentation);
 const identity=recording(song);
 if(identity){
  const saved=songMetadata.get(identity.key);
  if(!saved?.complete&&(saved?.retryAt||0)<=Date.now()){
   if(typeof IntersectionObserver==='undefined')queueArtwork(identity);
   else{
    if(!artworkObserver)artworkObserver=new IntersectionObserver(entries=>{for(const entry of entries)if(entry.isIntersecting){artworkObserver.unobserve(entry.target);const item=artworkTargets.get(entry.target);if(item)queueArtwork(item);}},{rootMargin:'240px'});
    artworkTargets.set(card,identity);artworkObserver.observe(card);
   }
  }
 }
 return {card,info,presentation};
}
function queueArtwork(identity){
 if(!alive)return;artworkQueue.set(identity.key,identity);
 if(!artworkBusy){clearTimeout(artworkTimer);artworkTimer=setTimeout(readArtwork,40);}
}
function scheduleArtwork(){
 if(!alive||!artworkQueue.size)return;
 const readyAt=Math.min(...[...artworkQueue.keys()].map(key=>songMetadata.get(key)?.retryAt||0));
 artworkTimer=setTimeout(readArtwork,Math.max(40,readyAt-Date.now()));
}
function pruneSongPresentations(){for(const item of songPresentations)if(!item.card.isConnected)songPresentations.delete(item);}
async function readArtwork(){
 artworkTimer=null;if(!alive||artworkBusy||!artworkQueue.size)return;
 pruneSongPresentations();
 const visible=new Set([...songPresentations].map(item=>recording(item.song)?.key));
 for(const key of artworkQueue.keys())if(!visible.has(key))artworkQueue.delete(key);
 if(!artworkQueue.size)return;
 const ready=[...artworkQueue.values()].filter(item=>(songMetadata.get(item.key)?.retryAt||0)<=Date.now());
 if(!ready.length){scheduleArtwork();return;}
 const source=ready[0].source,items=ready.filter(item=>item.source===source).slice(0,source==='cloud'?3:12);
 for(const item of items)artworkQueue.delete(item.key);
 const request=new AbortController();artworkRequest=request;artworkBusy=true;
 try{
  const result=await api('manage/artwork?'+new URLSearchParams({ids:items.map(item=>item.id).join(','),source}),undefined,request.signal);
  if(!alive||request.signal.aborted)return;
  for(const item of items){
   const previous=songMetadata.get(item.key)||{},track=result.tracks?.[item.id]||{},artwork=artworkUrl(result.covers?.[item.id]),pending=result.pending?.includes(item.id);
   songMetadata.set(item.key,{...previous,...track,album:track.album||result.albums?.[item.id]||previous.album,artwork:artwork||previous.artwork,complete:!pending,retryAt:pending?Date.now()+2000:0});
   if(pending)artworkQueue.set(item.key,item);
  }
  for(const presentation of songPresentations)if(presentation.card.isConnected)paintSong(presentation);
  while(songMetadata.size>512)songMetadata.delete(songMetadata.keys().next().value);
 }catch(error){if(alive&&!request.signal.aborted)for(const item of items){songMetadata.set(item.key,{...songMetadata.get(item.key),retryAt:Date.now()+5000});artworkQueue.set(item.key,item);}}
 finally{if(artworkRequest===request)artworkRequest=null;artworkBusy=false;scheduleArtwork();}
}
function playbackView(state){
 const p=state.playback,songs=state.songs,current=songs.find(song=>song.id===p.current);
 const upcoming=Array.isArray(state.upcoming)?state.upcoming.map(id=>songs.find(song=>song.id===id)).filter(Boolean)
  :songs.filter(song=>song.id!==p.current&&!p.played.includes(song.id)&&!['failed','cancelled'].includes(song.state));
 const next=Array.isArray(state.playable)?songs.find(song=>song.id===state.playable[0]):upcoming.find(song=>queueGroup(song,state)==='pending');
 const unavailable=upcoming.length>0&&upcoming.every(song=>song.lyric_state===-1);
 const ready=p.online&&state.readiness?.gates?.tv_playback!==false;
 const playing=['playing','preparing'].includes(p.state);
 return {ready,playing,nextId:next?.id,canToggle:ready&&(Boolean(p.current)||playing||!unavailable),
  canSkip:ready&&(Boolean(p.current)||upcoming.length>0&&!unavailable&&p.state!=='preparing'),
  title:current?.title||(p.online&&p.state==='playing'?'暖场':next?`接下来：${next.title}`:unavailable?'等待可唱歌曲':upcoming.length?'正在准备下一首':'等你点歌'),
  caption:!p.online?'等待电视连接':!ready?'电视播放暂不可用':({playing:current?'正在唱':'暖场播放中',preparing:'正在准备',paused:'已暂停',idle:next?'待播放':''})[p.state]||''};
}
function refreshRoomIntents(){
 if(!alive||!room?.active)return;
 if(view==='room'&&roomUi)renderRoom(room.status);
 if(view==='queue'&&queueUi){text(queueUi.feedback,transportFeedback);for(const row of queueUi.rows.values())attribute(row.play,'aria-disabled',String(row.play.disabled||!!pendingCommand));}
}
function finishRoomIntent(intent,feedback){
 clearTimeout(intent.timer);
 if(intent===pendingCommand){pendingCommand=null;transportFeedback=feedback;}
 else if(intent===pendingMix){pendingMix=null;soundFeedback=feedback;}
 else if(intent===pendingMute){pendingMute=null;soundFeedback=feedback;}
 else return;
 refreshRoomIntents();
}
function clearRoomIntents(){
 for(const intent of [pendingCommand,pendingMix,pendingMute])if(intent){clearTimeout(intent.timer);intent.controller.abort();}
 pendingCommand=pendingMix=pendingMute=null;
}
function observeRoomIntents(state){
 if(!state)return;
 const p=state.playback,c=state.controls,command=pendingCommand;
 if(command&&commandObserved(command.action,command.song,command.before,p,command.seq))finishRoomIntent(command,'');
 for(const intent of [pendingMix,pendingMute]){
  if(!p.online||!intent||!Number.isInteger(intent.revision)||p.applied_revision<intent.revision)continue;
  const keys=Object.keys(intent.values),matched=keys.every(key=>c[key]===intent.values[key]);
  if(matched&&keys.includes('original')&&['preparing','playing'].includes(p.state)&&p.backing_original!=null&&p.backing_original!==intent.values.original)continue;
  if(intent===pendingMix)for(const key of keys)if(mixDraft.get(key)?.value===intent.values[key])mixDraft.delete(key);
  const feedback=matched?(intent===pendingMute?(intent.values.music_gain===0?'音乐已静音。':'音乐已恢复。'):'声音已更新。'):'声音已在另一端调整。';
  finishRoomIntent(intent,feedback);
 }
}
async function playCommand(action,song=''){
 if(!alive||readOnly||pendingCommand||!room?.active||!playbackView(room.status).ready)return;
 const intent={controller:new AbortController(),action,song,before:{...room.status.playback}},id=room.room_id;
 pendingCommand=intent;
 transportFeedback=({play:'正在准备这首歌…',next:'正在切歌…',pause:'正在暂停…',resume:'正在开始播放…'})[action];
 refreshRoomIntents();
 try{
  const result=await roomAction('command',{action,song,request_id:crypto.randomUUID()},id,intent.controller.signal);
  if(pendingCommand!==intent)return;
  intent.seq=result.seq;
  transportFeedback='等待电视回报…';
  intent.timer=setTimeout(()=>finishRoomIntent(intent,'电视暂未确认，请查看当前播放状态后再试。'),7000);
  observeRoomIntents(room.status);refreshRoomIntents();void syncRoom();
 }catch(error){if(pendingCommand===intent)finishRoomIntent(intent,explain(error));}
}
function draftSound(key,value){
 const c=room.status.controls;
 if(c[key]===value)mixDraft.delete(key);
 else mixDraft.set(key,{value,muteRevision:c.music_mute_revision});
 if(!pendingMix&&!pendingMute)soundFeedback='';
}
async function writeSound(values,muteRevision,{mute=false}={}){
 if(readOnly||!room?.status.playback.online||(mute?pendingMute:pendingMix)||!Object.keys(values).length)return;
 const intent={controller:new AbortController(),values},id=room.room_id;
 if(mute)pendingMute=intent;else pendingMix=intent;
 soundFeedback='正在调整声音…';refreshRoomIntents();
 const payload=values.music_gain>0&&Number.isInteger(muteRevision)?{...values,music_mute_revision:muteRevision}:values;
 try{
  const result=await roomAction('controls',payload,id,intent.controller.signal);
  if((mute?pendingMute:pendingMix)!==intent)return;
  intent.revision=result.revision;
  soundFeedback='等待电视确认…';
  intent.timer=setTimeout(()=>finishRoomIntent(intent,'电视暂未确认声音设置，请重试。'),7000);
  observeRoomIntents(room.status);refreshRoomIntents();void syncRoom();
 }catch(error){
  if((mute?pendingMute:pendingMix)!==intent)return;
  if(error.message==='music_mute_changed')mixDraft.delete('music_gain');
  finishRoomIntent(intent,explain(error));void syncRoom();
 }
}
function buildRoom(){
 const summary=el('div',null,'room-summary'),title=el('h2'),caption=el('p','','muted');
 const commands=el('div',null,'button-row'),toggle=button('',()=>{
  const p=room.status.playback,playback=playbackView(room.status);
  if(playback.canToggle)void playCommand(playback.playing?'pause':'resume',p.current);
 }),skip=button('切歌',()=>{if(playbackView(room.status).canSkip)void playCommand('next',room.status.playback.current);});
 const queue=button('查看已点',()=>setView('queue'));
 const feedback=el('p','','ktv-action-status muted');feedback.setAttribute('role','status');
 commands.append(toggle,skip,button('去点歌',()=>setView('search'),{className:'primary'}),queue);
 summary.append(title,caption,commands,feedback);
 $('summary').replaceChildren(summary);
 const mix=el('section',null,'section'),form=el('form',null,'mix-form'),ranges=new Map();
 mix.append(el('h2','声音与房间'));
 for(const[key,label]of volumeControls){
  const input=el('input');input.type='range';input.min='0';input.max='100';input.name=key;
  const output=el('output');
  input.oninput=()=>{draftSound(key,Number(input.value));refreshRoomIntents();};
  const wrap=field(label,input);wrap.append(output);form.append(wrap);
  ranges.set(key,{input,output,wrap});
 }
 const original=select([['0','伴奏'],['1','原唱']],'0');
 original.onchange=()=>{draftSound('original',Number(original.value));refreshRoomIntents();};
 const save=el('button','应用声音设置','primary');save.type='submit';
 const mute=button('音乐静音',()=>{
  const c=room.status.controls;
  if(pendingMute)return;
  mixDraft.delete('music_gain');
  void writeSound({music_gain:c.music_gain===0?lastMusicGain:0},c.music_mute_revision,{mute:true});
 });
 const sound=el('p','','ktv-action-status muted');sound.setAttribute('role','status');
 form.append(field('演唱模式',original),save,mute,sound);
 form.onsubmit=event=>{
  event.preventDefault();
  const values=Object.fromEntries([...mixDraft].filter(([key])=>key!=='music_gain'||!pendingMute).map(([key,value])=>[key,value.value]));
  void writeSound(values,mixDraft.get('music_gain')?.muteRevision);
 };
 const skin=select(themes,'content');
 skin.onchange=async()=>{
  await action(skin,()=>roomAction('theme',{theme:skin.value}),'本场主题已保存。');
  if(skin.isConnected&&room?.status)skin.value=room.status.theme;
 };
 mix.append(form,field('本场主题',skin));
 const effects=el('div',null,'ktv-atmosphere button-row'),effectFeedback=el('p','','ktv-action-status muted');
 effectFeedback.setAttribute('role','status');
 const roomId=room.room_id,effectButtons=new Map();
 const sfx=partyAudio?.SfxCommands?new partyAudio.SfxCommands({
  send:payload=>room?.room_id===roomId?roomAction('command',payload,roomId):Promise.reject(new Error('room_changed')),
  onState:value=>{
   text(effectFeedback,partyAudio.sfxMessage(value));
   effectButtons.get('sfx_stop').hidden=!effectCanStop(room?.status?.playback,value);
  },
 }):null;
 for(const[key,label]of [['sfx_applause','鼓掌'],['sfx_cheer','喝彩'],['sfx_stop','停止氛围音']]){
  const control=button(label,()=>{void sfx?.trigger(key).then(()=>syncRoom()).catch(error=>text(effectFeedback,explain(error)));});
  effects.append(control);effectButtons.set(key,control);
 }
 effects.append(effectFeedback);mix.append(effects);$('content').replaceChildren(mix);
 return {title,caption,toggle,skip,queue,feedback,form,ranges,original,save,mute,sound,skin,effects,effectButtons,sfx};
}
function effectCanStop(playback,action){
 return ['pending','playing'].includes(playback?.sfx_state)||['sending','sent'].includes(action?.phase);
}
function renderRoom(state){
 if(!roomUi)roomUi=buildRoom();
 const ui=roomUi,p=state.playback,c=state.controls,playback=playbackView(state),count=liveQueueCount(state);
 text(ui.title,playback.title);
 text(ui.caption,[playback.caption,count?`已点 ${count} 首`:''].filter(Boolean).join(' · '));
 text(ui.toggle,playback.playing?'暂停':p.current?'继续播放':'开始播放');
 const skipFocused=document.activeElement===ui.skip;
 ui.toggle.disabled=readOnly||!playback.canToggle;ui.skip.disabled=readOnly||!playback.canSkip;
 for(const control of [ui.toggle,ui.skip])attribute(control,'aria-disabled',String(control.disabled||!!pendingCommand));
 ui.skip.hidden=!playback.canSkip;text(ui.queue,`查看已点（${count}）`);
 if(ui.skip.hidden&&skipFocused)(ui.toggle.disabled?ui.queue:ui.toggle).focus({preventScroll:true});
 text(ui.feedback,transportFeedback);
 const sfxAvailable=p.online&&p.audio_capabilities?.sfx===true;
 for(const[key,{input,output,wrap}]of ui.ranges){
  const draft=mixDraft.get(key);
  if(draft&&(draft.value===c[key]||key==='music_gain'&&draft.muteRevision!==c.music_mute_revision)){
   if(draft.value!==c[key])soundFeedback='音乐已在另一端静音。';
   mixDraft.delete(key);
  }
  const value=String((key==='music_gain'?pendingMute?.values.music_gain:undefined)??mixDraft.get(key)?.value??c[key]);
  if(input.value!==value)input.value=value;
  text(output,`${value}%`);
  wrap.hidden=key==='sfx_gain'&&!sfxAvailable;
  input.disabled=readOnly||!p.online||key==='music_gain'&&!!pendingMute;
 }
 if(mixDraft.get('original')?.value===c.original)mixDraft.delete('original');
 const original=String(mixDraft.get('original')?.value??c.original);
 if(ui.original.value!==original)ui.original.value=original;
 ui.original.disabled=readOnly||!p.online;
 ui.save.disabled=readOnly||!p.online||![...mixDraft.keys()].some(key=>key!=='music_gain'||!pendingMute);
 attribute(ui.save,'aria-disabled',String(ui.save.disabled||!!pendingMix));
 const muted=(pendingMute?.values.music_gain??c.music_gain)===0;
 text(ui.mute,muted?'恢复音乐':'音乐静音');
 attribute(ui.mute,'aria-pressed',String(muted));attribute(ui.mute,'aria-disabled',String(!!pendingMute));
 ui.mute.disabled=readOnly||!p.online;
 text(ui.sound,soundFeedback);
 if(document.activeElement!==ui.skin&&ui.skin.value!==state.theme)ui.skin.value=state.theme;
 ui.skin.disabled=readOnly;
 ui.effects.hidden=!sfxAvailable||!ui.sfx;
 for(const control of ui.effectButtons.values())control.disabled=readOnly;
 ui.sfx?.observe(p);
 ui.effectButtons.get('sfx_stop').hidden=!effectCanStop(p,ui.sfx?.state);
}
function queueGroup(song,state){
 if(song.id===state.playback.current)return 'current';
 if(state.playback.played.includes(song.id)||song.state==='cancelled')return 'history';
 if(song.state==='failed'||song.lyric_state===-1)return 'failed';
 return song.state==='ready'&&(Array.isArray(state.playable)?state.playable.includes(song.id):!song.lyrics_pending&&song.lyric_state!==-1)?'pending':'preparing';
}
function liveQueueCount(state){return state.songs.filter(song=>queueGroup(song,state)!=='history').length;}
function orderableSongs(state){return state.songs.filter(song=>['pending','preparing','failed'].includes(queueGroup(song,state)));}
function reconcile(parent,nodes){
 const keep=new Set(nodes);
 for(const child of [...parent.children])if(!keep.has(child))child.remove();
 for(const [index,node]of nodes.entries())if(parent.children[index]!==node)parent.insertBefore(node,parent.children[index]||null);
}
async function moveQueue(songId,targetId,b){
 const state=room?.status;if(!state)return;
 const movable=orderableSongs(state).map(song=>song.id),from=movable.indexOf(songId),to=movable.indexOf(targetId);
 if(from<0||to<0||from===to)return;
 movable.splice(from,1);movable.splice(to,0,songId);
 const moving=new Set(movable);let index=0;
 const ids=state.songs.map(song=>moving.has(song.id)?movable[index++]:song.id);
 return action(b,()=>roomAction('order',{ids,revision:state.queue_revision}),'歌单顺序已同步。');
}
function dragHandle(song){
 const handle=button('⠿',()=>{}, {className:'queue-drag'});
 handle.setAttribute('aria-label','拖拽排序 '+song.title);handle.title='拖拽排序，也可用上下方向键移动';handle.draggable=true;
 handle.ondragstart=event=>{
  if(changing){event.preventDefault();return;}
  queueDrag={id:song.id,revision:room.status.queue_revision,roomId:room.room_id};
  roomSyncRequest?.abort();viewRequest?.abort();
  event.dataTransfer.effectAllowed='move';event.dataTransfer.setData('text/plain',song.id);
 };
 handle.ondragend=()=>{
  const cancelled=!!queueDrag;queueDrag=null;
  for(const row of queueUi?.rows.values()||[])row.element.classList.remove('drag-over');
  if(cancelled)void syncRoom();
 };
 handle.onkeydown=event=>{
  const ids=orderableSongs(room.status).map(item=>item.id),index=ids.indexOf(song.id),delta=event.key==='ArrowUp'?-1:event.key==='ArrowDown'?1:0;
  if(!delta||index+delta<0||index+delta>=ids.length)return;
  event.preventDefault();void moveQueue(song.id,ids[index+delta],handle);
 };
 return handle;
}
function buildQueue(){
 const section=el('section',null,'section queue-section'),heading=el('div',null,'queue-heading');
 const total=el('span','','muted');heading.append(el('h2','本场歌单'),total);
 section.append(heading);
 const tools=el('div',null,'toolbar');
 const remove=button(`移除所选（${queueSelected.size}）`,async()=>{
  const ids=[...queueSelected],revision=room.status.queue_revision;
  if(!ids.length||changing)return;
  remove.disabled=true;
  try{
   await action(null,async()=>{
    await roomAction('queue-remove',{ids,revision});queueSelected.clear();
    if(queueUi?.remove===remove)updateQueueSelection(queueUi);
   },`已从歌单移除 ${ids.length} 首。`);
  }finally{if(queueUi?.remove===remove)updateQueueSelection(queueUi);}
 });
 const all=button('全选待唱',()=>{for(const song of orderableSongs(room.status))queueSelected.add(song.id);renderQueue(room.status);});
 const clear=button('取消选择',()=>{queueSelected.clear();renderQueue(room.status);});
 const selectSongs=button('选择歌曲',()=>{
  queueSelecting=!queueSelecting;
  if(!queueSelecting)queueSelected.clear();
  renderQueue(room.status);
 });
 tools.append(button('继续点歌',()=>setView('search'),{className:'primary'}),selectSongs,all,clear,remove,button('制作进度',()=>setView('jobs')));
 const feedback=el('p','','ktv-action-status muted');feedback.setAttribute('role','status');
 const empty=el('p','还没有已点歌曲','empty'),groups=el('div',null,'ktv-queue-groups');
 section.append(tools,feedback,empty,groups);$('content').replaceChildren(section);
 return {total,remove,all,clear,selectSongs,empty,feedback,groups,sections:new Map(),rows:new Map()};
}
function buildQueueRow(song){
 const element=el('article',null,'entry queue-entry');element.dataset.queueId=song.id;
 const check=el('input');check.type='checkbox';
 check.onchange=()=>{check.checked?queueSelected.add(song.id):queueSelected.delete(song.id);updateQueueSelection(queueUi);};
 const {card,info,presentation}=songPresentation(song),note=el('p','','queue-note');
 info.append(note);
 const handle=dragHandle(song),spacer=el('span',null,'queue-drag-spacer');
 const actions=el('div',null,'button-row queue-row-actions');
 const pin=button('置顶',event=>moveQueue(song.id,orderableSongs(room.status)[0]?.id,event.currentTarget));
 element.ondragover=event=>{
  if(handle.hidden||!queueDrag||queueDrag.id===song.id)return;
  event.preventDefault();event.dataTransfer.dropEffect='move';element.classList.add('drag-over');
 };
 element.ondragleave=()=>element.classList.remove('drag-over');
 element.ondrop=event=>{
  if(handle.hidden||!queueDrag)return;
  event.preventDefault();element.classList.remove('drag-over');const drag=queueDrag;queueDrag=null;
  if(drag.roomId!==room.room_id||drag.revision!==room.status.queue_revision){message(errors.queue_changed,true);void syncRoom();return;}
  void moveQueue(drag.id,song.id,handle);
 };
 const retry=button('重试',event=>action(event.currentTarget,()=>roomAction('retry',{id:song.id,request_id:crypto.randomUUID()}),'正在重新准备。'));
 const alternate=button('换个版本',()=>findCloudVersion(presentation.song.title,presentation.song.source));
 const play=button('现在唱',()=>void playCommand('play',song.id));
 actions.append(pin,retry,alternate,play);
 element.append(handle,spacer,check,card,actions);
 return {element,check,handle,spacer,pin,retry,alternate,play,note,presentation,actions};
}
function updateQueueSelection(ui){
 text(ui.remove,`移除所选（${queueSelected.size}）`);
 ui.remove.disabled=readOnly||!queueSelected.size;ui.clear.disabled=!queueSelected.size;
}
function renderQueue(state){
 if(!queueUi)queueUi=buildQueue();
 const ui=queueUi,songs=state.songs,movable=orderableSongs(state),available=new Set(movable.map(song=>song.id)),first=movable[0]?.id;
 const previousFocus=document.activeElement,hadFocus=$('content').contains(previousFocus);
 for(const id of queueSelected)if(!available.has(id))queueSelected.delete(id);
 text(ui.total,`${liveQueueCount(state)} 首`);
 text(ui.selectSongs,queueSelecting?'退出选择':'选择歌曲');
 ui.selectSongs.hidden=!movable.length&&!queueSelecting||readOnly;
 ui.all.hidden=ui.clear.hidden=ui.remove.hidden=!queueSelecting;
 ui.all.disabled=!movable.length||movable.every(song=>queueSelected.has(song.id));
 ui.empty.hidden=songs.length>0;text(ui.feedback,transportFeedback);updateQueueSelection(ui);
 const parts={current:[],pending:[],preparing:[],failed:[],history:[]},next=playbackView(state),current=state.playback;
 const labels={current:!current.online?'上次播放':current.state==='paused'?'已暂停':current.state==='preparing'?'正在准备':'正在唱',pending:'接下来',preparing:'正在准备',failed:'暂不可唱',history:'唱过了'};
 const retained=new Set();
 for(const song of songs){
  const group=queueGroup(song,state),movable=available.has(song.id);
  let row=ui.rows.get(song.id);
  if(!row){row=buildQueueRow(song);ui.rows.set(song.id,row);}
  else{
   const old=row.presentation.song;
   row.presentation.song=song;
   if(song.title!==old.title||song.album!==old.album||song.artwork!==old.artwork||artistsOf(song.artists)!==artistsOf(old.artists))paintSong(row.presentation);
  }
  retained.add(song.id);
  const progress=song.progress;
  let reason=song.lyric_state===-1?'暂无同步歌词':song.lyrics_pending?'正在找歌词':phase[progress?.source_phase]||phase[song.state]||'准备中';
  if(group==='preparing'&&song.state==='ready'&&!song.lyrics_pending)reason='正在准备音轨';
  if(progress?.source_phase==='source_downloading'&&progress.source_total>0&&Number.isFinite(progress.source_bytes))
   reason+=` ${Math.round(100*Math.min(progress.source_bytes,progress.source_total)/progress.source_total)}%`;
  if(group==='pending'&&song.id===next.nextId)reason='下一首 · 已就绪';
  text(row.note,`${song.nickname||'朋友'} 点歌`+(['pending','preparing','failed'].includes(group)?' · '+reason:''));
  attribute(row.check,'aria-label','选择 '+song.title);attribute(row.handle,'aria-label','拖拽排序 '+song.title);
  row.check.checked=queueSelected.has(song.id);row.check.hidden=!movable||!queueSelecting;row.check.disabled=!movable||readOnly;
  row.handle.hidden=!movable||readOnly;row.spacer.hidden=!row.handle.hidden;
  row.pin.hidden=!movable||readOnly||song.id===first;
  row.retry.hidden=song.state!=='failed'||readOnly;
  row.alternate.hidden=song.lyric_state!==-1||song.state==='failed'||readOnly;
  row.play.hidden=!['pending','history'].includes(group)||song.state!=='ready'||song.lyric_state===-1||song.lyrics_pending===true||readOnly;
  text(row.play,group==='history'?'再唱':'现在唱');
  row.play.disabled=!next.ready;
  attribute(row.play,'aria-disabled',String(row.play.disabled||!!pendingCommand));
  row.actions.hidden=[row.pin,row.retry,row.alternate,row.play].every(control=>control.hidden);
  parts[group].push(row.element);
 }
 for(const [id,row]of ui.rows)if(!retained.has(id)){songPresentations.delete(row.presentation);artworkObserver?.unobserve(row.presentation.card);ui.rows.delete(id);}
 const sections=[];
 for(const [group,rows]of Object.entries(parts)){
  if(!rows.length){ui.sections.get(group)?.list.remove();ui.sections.delete(group);continue;}
  let entry=ui.sections.get(group);
  if(!entry){
   const history=group==='history',list=el(history?'details':'section',null,'queue-group '+group),title=el(history?'summary':'h3');
   if(history){list.open=queueHistoryOpen;list.ontoggle=()=>{queueHistoryOpen=list.open;};}
   entry={list,title};ui.sections.set(group,entry);
  }
  text(entry.title,`${labels[group]} · ${rows.length}`);
  reconcile(entry.list,[entry.title,...rows]);sections.push(entry.list);
 }
 reconcile(ui.groups,sections);
 if(hadFocus&&(document.activeElement!==previousFocus||!previousFocus.isConnected||previousFocus.hidden||previousFocus.disabled)){
  if(previousFocus.isConnected&&!previousFocus.hidden&&!previousFocus.disabled)previousFocus.focus({preventScroll:true});
  else $('content').focus({preventScroll:true});
 }
}
function storageSummary(data,host){
 const box=el('div',null,'storage-summary'),v=data[host],mac=host==='mac';
 const policyLabel=mac?'播放成品回收目标':'容量上限';
 const policyStatus=v.policy?.enabled?`${policyLabel} ${size(v.policy.limit_bytes)} · 自动清理已开启`:'尚未开启容量自动清理';
 box.append(el('h2',mac?'Mac 歌曲文件':'NAS 歌曲文件'),el('p',size(v.bytes),'storage-size'),el('p',v.available?policyStatus:v.configured?'暂未连接，无法读取占用':'尚未配置 NAS','muted'));
 if(v.legacy_bytes)box.append(el('p',`包含旧归档 ${size(v.legacy_bytes)}`,'muted'));
 if(mac&&v.hot_cache?.bytes!=null)box.append(el('p',`本机播放成品 ${size(v.hot_cache.bytes)}`,'muted'));
 if(v.available&&v.policy?.enabled){
  const state=v.last_cleanup?.state;
  if(mac&&state==='retained_over_target')box.append(el('p','本场使用中或尚未核对 NAS 归档的成品暂时保留。','muted'));
  if(mac&&state==='pending')box.append(el('p','正在核对本机播放成品的清理状态。','muted'));
  if(!mac&&state==='protected_over_limit')box.append(el('p','空闲歌曲已清理，正在使用的歌曲仍超出上限。请调整上限或结束本场。','error'));
 }
 return box;
}
function selectedHosts(){return cacheHosts.filter(host=>host===scope||scope==='both');}
function eligible(row){
 const hosts=selectedHosts();
 return !(hosts.includes('nas')&&!cacheData.nas.available)
  &&hosts.some(host=>row[host])&&hosts.every(host=>!row[host]||row[host].can_delete);
}
function selectCacheRows(rows,checked){
 for(const row of rows.filter(eligible)){
  if(!checked)selected.delete(row.id);
  else if(selected.size<128)selected.set(row.id,row);
 }
}
function renderCacheTotals(data){
 const totals=el('div',null,'storage-totals');
 totals.append(storageSummary(data,'mac'),storageSummary(data,'nas'));$('summary').replaceChildren(totals);
}
function librarySelect(node,label){
 node.setAttribute('aria-label',label);
 const wrap=el('div',null,'library-select');
 wrap.append(node);
 return wrap;
}
function renderCache(data){
 renderCacheTotals(data);
 const query=el('input');query.type='search';query.maxLength=120;query.placeholder='歌曲、歌手或专辑';query.value=cacheQuery;
 const presence=select([['all','全部位置'],['both','两端都有'],['nas','仅 NAS 有副本'],
  ['mac','仅 Mac 有副本'],['orphan','未关联文件']],cachePresence);
 const sort=select([['recent','最近使用'],['size','占用从大到小'],['title','歌曲名称']],cacheSort);
 query.setAttribute('aria-label','搜索歌曲');
 const changeListing=()=>{
  clearTimeout(libraryFilterTimer);
  cacheQuery=query.value;cachePresence=presence.value;cacheSort=sort.value;page=0;lastCheckbox=-1;loadCachePage();
 };
 query.oninput=()=>{clearTimeout(libraryFilterTimer);libraryFilterTimer=setTimeout(changeListing,200);};
 presence.onchange=sort.onchange=changeListing;

 const host=el('div',null,'cleanup-targets');host.setAttribute('role','group');host.setAttribute('aria-label','清理位置');
 for(const [value,label]of [['mac','Mac'],['nas','NAS'],['both','两端']]){
  const option=button(label,()=>{
   scope=value;selected.clear();lastCheckbox=-1;page=0;renderCacheRows();loadCachePage();
  },{disabled:value!=='mac'&&!data.nas.available});
  option.dataset.scope=value;option.setAttribute('aria-pressed',String(value===scope));host.append(option);
 }
 const choose=button('选择筛选结果',async event=>{
  const trigger=event.currentTarget;
  const params={offset:0,limit:128,selectable:scope},path=libraryPath(params);
  trigger.disabled=true;
  try{
   const response=await api(path);
   if(view==='cache'&&selectingFiles&&path===libraryPath({...params,selectable:scope}))selectCacheRows(response.entries,true);
  }catch(error){if(view==='cache')message(explain(error),true);}
  finally{if(view==='cache'&&trigger.isConnected)renderCacheRows();}
 });
 choose.id='ktv-select-filtered';
 const clear=button('清空选择',()=>{selected.clear();lastCheckbox=-1;renderCacheRows();});
 clear.id='ktv-clear-selected';
 const remove=button('清理所选',openDelete,{className:'danger-outline'});
 remove.id='ktv-delete-selected';
 const count=el('p','','selection-count');count.id='ktv-selection-count';count.setAttribute('role','status');
 const actions=el('div',null,'library-actions');actions.id='ktv-file-actions';
 const cleanupScope=el('div',null,'library-cleanup-scope');
 cleanupScope.append(el('span','清理位置'),host);
 actions.append(cleanupScope,choose,count,clear,remove);

 const toggle=button('选择文件',()=>{
  selectingFiles=!selectingFiles;
  selected.clear();lastCheckbox=-1;
  page=0;renderCacheRows();loadCachePage();
 });
 toggle.id='ktv-select-files';toggle.setAttribute('aria-controls',actions.id);
 const total=el('span','','library-total');total.id='ktv-library-total';total.setAttribute('role','status');
 const filters=el('div',null,'library-filters');
 filters.append(query,librarySelect(presence,'存放位置'),librarySelect(sort,'排序'),total,toggle);

 const table=el('table',null,'library-table');
 const caption=el('caption','歌曲文件与占用','sr-only');table.append(caption);
 const head=el('thead'),tr=el('tr'),selectHead=el('th'),all=el('input');
 selectHead.className='library-check';selectHead.id='ktv-select-column';selectHead.scope='col';
 all.type='checkbox';all.id='ktv-select-page';all.setAttribute('aria-label','选择本页可删除的歌曲');
 all.onchange=()=>{selectCacheRows(cacheData.entries,all.checked);lastCheckbox=-1;renderCacheRows();};
 selectHead.append(all);tr.append(selectHead);
 for(const heading of ['歌曲','Mac','NAS','最近使用']){
  const th=el('th',heading);th.scope='col';tr.append(th);
  if(heading==='歌曲')th.className='library-name';
 }
 head.append(tr);
 const body=el('tbody');body.id='ktv-library-rows';table.append(head,body);
 const wrap=el('div',null,'table-scroll');
 wrap.tabIndex=0;wrap.setAttribute('role','region');wrap.setAttribute('aria-label','歌曲文件列表');wrap.append(table);
 const pager=el('div',null,'pager');pager.id='ktv-cache-pager';
 $('content').append(filters,actions,wrap,pager);renderCacheRows();
}
function matchesCache(row){
 const query=cacheQuery.normalize('NFKC').trim().toLocaleLowerCase();
 if(query&&![row.title,artistsOf(row.artists),row.album||''].join(' ').normalize('NFKC').toLocaleLowerCase().includes(query))return false;
 if(cachePresence==='orphan')return row.kind==='orphan';
 if(cachePresence==='both')return !!(row.mac&&row.nas);
 if(cachePresence==='mac')return !!(cacheData.nas.available&&row.mac&&!row.nas);
 if(cachePresence==='nas')return !!(row.nas&&cacheData.mac.available&&!row.mac);
 return true;
}
function renderCacheRows(focusId){
 const body=$('library-rows');if(!body)return;
 artworkObserver?.disconnect();
 $('file-actions').hidden=!selectingFiles;
 $('select-column').hidden=!selectingFiles;
 $('select-files').textContent=selectingFiles?'退出选择':'选择文件';
 $('select-files').setAttribute('aria-expanded',String(selectingFiles));
 $('library-total').textContent=selectingFiles?'可清理 '+cacheData.total+' 项':'共 '+cacheData.total+' 项';
 const rows=selectingFiles?cacheData.entries.filter(eligible):cacheData.entries,available=new Set(rows.filter(eligible).map(row=>row.id));
 for(const option of $('file-actions').querySelectorAll('[data-scope]')){option.setAttribute('aria-pressed',String(option.dataset.scope===scope));option.disabled=option.dataset.scope!=='mac'&&!cacheData.nas.available;}
 for(const row of rows)if(selected.has(row.id)){
  if(available.has(row.id))selected.set(row.id,row);
  else selected.delete(row.id);
 }
 const pages=Math.max(1,Math.ceil(cacheData.total/libraryPageSize));
 body.replaceChildren();
 pruneSongPresentations();
 rows.forEach((row,index)=>{
  const tr=el('tr'),td=el('td',null,'library-check'),check=el('input');
  td.hidden=!selectingFiles;
  check.type='checkbox';check.checked=selected.has(row.id);check.disabled=!available.has(row.id);
  check.setAttribute('aria-label','选择 '+row.title);
  check.onclick=event=>{
   const group=event.shiftKey&&lastCheckbox>=0
    ?rows.slice(Math.min(index,lastCheckbox),Math.max(index,lastCheckbox)+1):[row];
   selectCacheRows(group,check.checked);lastCheckbox=index;renderCacheRows(row.id);
  };
  td.append(check);
  const name=el('td',null,'library-name'),{card}=songPresentation(row,{compact:true});name.append(card);
  const blocked=selectedHosts().map(h=>row[h]).find(item=>item&&!item.can_delete);
  if(selectingFiles&&blocked)name.append(el('span',reasons[blocked.reason]||'暂时保留','row-note'));
  tr.append(td,name,el('td',row.mac?size(row.mac.bytes):'—','numeric'),
   el('td',row.nas?size(row.nas.bytes):'—','numeric'),el('td',date(row.last_used),'date'));
  body.append(tr);
  if(focusId===row.id)check.focus({preventScroll:true});
 });
 if(!rows.length){
  const tr=el('tr'),td=el('td',selectingFiles
   ?(scope==='mac'?'Mac':scope==='nas'?'NAS':'所选位置')+' 没有可清理的歌曲文件。可切换清理位置或调整筛选。'
   :cacheQuery||cachePresence!=='all'?'没有匹配的歌曲文件。':'准备好的歌曲和原曲会显示在这里。','empty');
  td.colSpan=selectingFiles?5:4;tr.append(td);body.append(tr);
 }
 const enabled=rows.filter(row=>available.has(row.id)),checked=enabled.filter(row=>selected.has(row.id)).length;
 $('select-page').checked=!!checked&&checked===enabled.length;
 $('select-page').indeterminate=checked>0&&checked<enabled.length;
 $('select-page').disabled=!enabled.length;
 const hosts=selectedHosts();
 const bytes=hosts.map(host=>{
  const label=host==='mac'?'Mac':'NAS';
  return label+' '+size([...selected.values()].reduce((sum,row)=>sum+(row[host]?.bytes||0),0));
 });
 const hidden=[...selected.values()].filter(row=>!matchesCache(row)).length;
 $('selection-count').textContent='已选 '+selected.size+' 项'+(selected.size
  ?' · '+bytes.join(' / ')+(hidden?' · '+hidden+' 项在筛选外':''):'');
 $('select-filtered').disabled=selected.size>=128||!cacheData.total||selectedHosts().includes('nas')&&!cacheData.nas.available;
 $('clear-selected').hidden=!selected.size;
 $('delete-selected').disabled=!selected.size||changing;
 $('delete-selected').textContent=scope==='mac'?'清理 Mac 缓存':'删除所选文件';
 $('cache-pager').replaceChildren();
 if(pages>1)$('cache-pager').append(button('上一页',()=>{if($('refresh').disabled)return;page--;lastCheckbox=-1;loadCachePage();},{disabled:page===0}),
  el('span',(page+1)+' / '+pages),button('下一页',()=>{if($('refresh').disabled)return;page++;lastCheckbox=-1;loadCachePage();},{disabled:page+1>=pages}));
}
async function openDelete(){
 if(!selected.size||changing)return;
 const trigger=$('delete-selected');
 trigger.disabled=true;changing=true;
 try{
  deletePlan=await api('manage/preview',{ids:[...selected.keys()],scope});
  const location=cacheScopes.find(([value])=>value===scope)[1];
  $('delete-title').textContent=(scope==='mac'?'清理 ':'删除 ')+deletePlan.songs.length+' 项 · '+location;
  $('delete-note').textContent=scope==='mac'?'有 NAS 归档的歌曲可以回档，其余曲目再次点歌时会重新准备。':'清除所选歌曲的归档和相应的已唱记录。';
  $('delete-songs').replaceChildren(...deletePlan.songs.map(song=>el('li',song.title)));
  $('delete-bytes').textContent=selectedHosts().map(host=>(host==='mac'?'Mac':'NAS')+' '+size(deletePlan.bytes[host])).join(' / ');
  $('delete-error').textContent='';$('confirm-delete').disabled=false;$('confirm-delete').textContent=scope==='mac'?'清理缓存':'确认删除';
  $('delete-dialog').showModal();
 }catch(error){message(explain(error),true);}
 finally{changing=false;trigger.disabled=false;}
}
function wireDelete(){
$('cancel-delete').onclick=()=>$('delete-dialog').close();
$('confirm-delete').onclick=async()=>{
 if(!deletePlan||deletePlan.invalid||changing)return;
 changing=true;const b=$('confirm-delete');b.disabled=true;$('cancel-delete').disabled=true;$('delete-error').textContent='';
 try{
  const result=await api('manage/delete',{plan:deletePlan.plan});
  if(!alive)return;
  if(!result.complete){
   const done=Object.keys(result.results).map(h=>h==='mac'?'Mac':'NAS').join('、');
   const partial=Object.keys(result.nas_stores||{}).length?'NAS 已完成部分文件清理；':'';
   deletePlan.invalid=!result.retryable;
   $('delete-error').textContent=`${done?done+' 已删除；':''}${partial}${explain(new Error(result.code))}${result.retryable?'点击“重试剩余删除”继续。':'请关闭窗口，刷新后重新选择剩余歌曲。'}`;
   b.textContent=result.retryable?'重试剩余删除':'请重新选择';return;
  }
  $('delete-dialog').close();selected.clear();deletePlan=null;changing=false;await refresh({quiet:true});message('所选位置中的歌曲文件已删除。');
 }catch(error){
  if(!alive)return;
  deletePlan.invalid=['cache_changed','archive_changed','cache_in_use','plan_expired'].includes(error.message);
  $('delete-error').textContent=deletePlan.invalid?explain(error)+'请关闭窗口后刷新。':'尚未取得完整删除结果。点击重试会先核对已完成的部分。';
  b.textContent=deletePlan.invalid?'请重新选择':'重试剩余删除';
 }finally{changing=false;if(alive){b.disabled=!!deletePlan?.invalid;$('cancel-delete').disabled=false;}}
};
$('delete-dialog').addEventListener('cancel',e=>{if(changing)e.preventDefault();});
}

function updateSearchSummary(){
 $('summary').replaceChildren(el('h2','找一首想唱的歌'),el('p',room?.active?`本场已点 ${liveQueueCount(room.status)} 首。`:'房间开启后可以点歌；现在可以先搜索。','muted'));
}
function renderSearch(){
 updateSearchSummary();
 const form=el('form',null,'search-form'),input=el('input');input.type='search';input.value=searchQuery;input.maxLength=120;input.placeholder='歌曲、歌手';input.setAttribute('aria-label','歌曲或歌手');
 const origin=select(Object.entries(sourceNames),searchSource);origin.setAttribute('aria-label','搜索范围');
 const submit=el('button','搜索','primary');submit.type='submit';submit.id='ktv-search-submit';
 function invalidateSearch(){
  searchRequest?.abort();searchRequest=null;searchData=null;searchSelected.clear();submit.disabled=false;submit.textContent='搜索';renderSearchRows();message('');
 }
 input.oninput=origin.onchange=invalidateSearch;
 form.append(input,origin,submit);form.onsubmit=async event=>{
  event.preventDefault();searchQuery=input.value.trim();searchSource=origin.value;searchOffset=0;searchSelected.clear();await searchSongs(submit);
 };
 $('content').append(form);const results=el('section',null,'section search-results');results.id='ktv-search-results';$('content').append(results);renderSearchRows();
}
async function searchSongs(b){
 if(!searchQuery&&searchSource!=='cloud'){message('请输入歌曲或歌手。',true);return;}
 searchRequest?.abort();const request=new AbortController();searchRequest=request;
 searchData=null;renderSearchRows();b.disabled=true;b.textContent='正在搜索…';message('正在搜索…');
 try{
  const data=await api('manage/search?'+new URLSearchParams({q:searchQuery,source:searchSource,offset:searchOffset}),undefined,request.signal);
  if(request.signal.aborted||!alive||view!=='search')return;
  searchData=data;renderSearchRows();message('');
 }catch(error){if(!request.signal.aborted&&alive&&view==='search')message(explain(error),true);}
 finally{if(searchRequest===request){searchRequest=null;if(b.isConnected){b.disabled=false;b.textContent=b.id==='ktv-search-submit'?'搜索':b.textContent;}}}
}
function canAddSong(song){return room?.active&&(room.status?.mac_online||song.cached_accompaniment===true);}
function searchAvailability(){
 return JSON.stringify([room?.active,room?.status?.mac_online,...(searchData?.songs||[]).map(song=>added.has(song.source+':'+song.song_id))]);
}
function renderSearchRows(){
 const box=$('search-results');if(!box)return;
 searchRowsSignature=searchAvailability();
 const focusKey=document.activeElement?.dataset?.searchFocus;box.replaceChildren();artworkObserver?.disconnect();pruneSongPresentations();
 if(!searchData){box.append(el('p',searchRequest?'正在搜索…':'搜索曲库、MV，或浏览已授权的音乐云盘。','empty'));return;}
 const songs=searchData.songs||[],tools=el('div',null,'toolbar');
 for(const key of searchSelected)if(added.has(key)||!songs.some(song=>song.source+':'+song.song_id===key&&canAddSong(song)))searchSelected.delete(key);
 const selectedSongs=()=>songs.filter(song=>searchSelected.has(song.source+':'+song.song_id)&&!added.has(song.source+':'+song.song_id));
 const add=button(`点所选歌曲（${searchSelected.size}）`,event=>addSongs(selectedSongs(),event.currentTarget),{disabled:!searchSelected.size||!selectedSongs().every(canAddSong)});
 tools.append(button('全选本页',()=>{for(const song of songs)if(!added.has(song.source+':'+song.song_id)&&canAddSong(song))searchSelected.add(song.source+':'+song.song_id);renderSearchRows();}),button('取消选择',()=>{searchSelected.clear();renderSearchRows();}),add);box.append(tools);
 if(!songs.length)box.append(el('p','没有找到匹配歌曲，请换个关键词或搜索范围。','empty'));
 for(const song of songs){
  const key=song.source+':'+song.song_id,row=el('article',null,'entry search-entry'),check=el('input');
  check.type='checkbox';check.checked=searchSelected.has(key);check.disabled=added.has(key)||!canAddSong(song);check.setAttribute('aria-label','选择 '+song.title);check.dataset.searchFocus=key+':选择';
  check.onchange=()=>{check.checked?searchSelected.add(key):searchSelected.delete(key);add.textContent=`点所选歌曲（${searchSelected.size}）`;add.disabled=!searchSelected.size||!selectedSongs().every(canAddSong);};
  const {card,info}=songPresentation(song),note=el('div',null,'song-tags');
  note.append(el('span',sourceNames[song.source]||'曲库','source-tag'));
  if(song.cached_accompaniment)note.append(el('span','伴奏已缓存','cached-tag'));
  info.append(note);
  const addButton=button(added.has(key)?'已点':'点歌',event=>addSongs([song],event.currentTarget),{disabled:added.has(key)||!canAddSong(song)});addButton.dataset.searchFocus=key+':点歌';
  row.append(check,card,addButton);box.append(row);
  if(focusKey===check.dataset.searchFocus)check.focus({preventScroll:true});
  if(focusKey===addButton.dataset.searchFocus)addButton.focus({preventScroll:true});
 }
 if(searchData.has_more||searchOffset){
  const pager=el('div',null,'pager');pager.append(button('上一页',event=>{searchOffset=Math.max(0,searchOffset-30);searchSelected.clear();void searchSongs(event.currentTarget);},{disabled:searchOffset===0}),el('span',`${Math.floor(searchOffset/30)+1}`),button('下一页',event=>{searchOffset=searchData.next_offset??searchOffset+30;searchSelected.clear();void searchSongs(event.currentTarget);},{disabled:!searchData.has_more}));box.append(pager);
 }
}
async function addSongs(songs,b){
 if(changing||readOnly||!room?.active||!songs.length)return;
 roomSyncRequest?.abort();changing=true;b.disabled=true;const roomId=room.room_id;let count=0;
 try{
  for(const song of songs){
   const key=song.source+':'+song.song_id;if(added.has(key))continue;
   const requestKey=roomId+':'+key;if(!addRequests.has(requestKey))addRequests.set(requestKey,crypto.randomUUID());
   await roomAction('songs',{song_id:song.song_id,title:song.title,source:song.source,request_id:addRequests.get(requestKey)},roomId);
   added.add(key);searchSelected.delete(key);count++;
  }
  message(`已点 ${count} 首，后台将自动准备。`);
 }catch(error){message(`已点 ${count} 首。${explain(error)}剩余歌曲仍保留选择。`,true);}
 finally{changing=false;renderSearchRows();if(b.isConnected)b.disabled=false;void syncRoom();}
}
const finishedJobStates=new Set(['ready','failed','cancelled']);
const preparationSources={song:'曲库版本',mv_direct:'MV 版本',cloud:'音乐云盘文件'};
const jobRetryRequests=new Map();
function renderJobs(jobs){
 const unfinished=jobs.filter(job=>!finishedJobStates.has(job.state)).length;
 $('summary').append(el('h2',`正在准备 ${unfinished} 首`),el('p','自动按歌单顺序处理。','muted'));
 if(!jobs.length)$('content').append(el('p','点歌后，下载和伴奏制作进度会显示在这里。','empty'));
 for(const job of [...jobs].reverse()){
  const row=el('article',null,'entry'),info=el('div',null,'entry-info'),finished=finishedJobStates.has(job.state);
  const detail=job.state==='ready'&&job.cache_hit?'命中完整播放缓存':phase[finished?job.state:job.progress?.stage]||phase[job.state];
  info.append(el('h3',job.title),el('p',[detail,preparationSources[job.source_preference]].filter(Boolean).join(' · '),'muted'));
  if(job.state==='failed'){
   if(job.retry_reason==='failed_history_already_prepared'){
    info.append(el('p',errors[job.retry_reason],'muted'));
   }else{
    const failure=job.code==='storage_full'?'这次制作时 Mac 可用空间不足。':errors[job.code]||'本次未准备完成。';
    info.append(el('p',failure,'error'));
    if(job.retry_reason!==job.code&&errors[job.retry_reason])info.append(el('p',errors[job.retry_reason],'muted'));
   }
  }
  if(!finished){
   const bar=el('progress');bar.max=4;bar.value=job.progress?.completed_steps||0;
   bar.setAttribute('aria-label','处理步骤');
   info.append(bar);
  }
  const details=el('details'),summary=el('summary','处理记录'),list=el('ol',null,'events');
  details.append(summary,list);
  details.ontoggle=async()=>{
   if(!details.open||list.children.length)return;
   try{
    const data=await api(`jobs/${encodeURIComponent(job.id)}/journal`);
    for(const e of data.events)list.append(el('li',`${date(e.at)} · ${phase[e.stage]||'阶段更新'}${e.code!=='none'?' · '+(errors[e.code]||'本阶段未完成'):''}`));
    if(!data.events.length)list.append(el('li','暂无处理记录。'));
   }catch{list.append(el('li','记录暂时无法读取，关闭后再试。'));}
  };
  info.append(details);row.append(info);
  if(job.state==='failed')renderFailedJobActions(row,job);
  $('content').append(row);
 }
}
function renderFailedJobActions(row,job){
 const tools=el('div',null,'button-row');
 const endpoint=`manage/jobs/${encodeURIComponent(job.id)}`;
 if(!readOnly&&job.can_retry===true)tools.append(button('重试制备',e=>action(e.currentTarget,async()=>{
  if(!jobRetryRequests.has(job.id))jobRetryRequests.set(job.id,crypto.randomUUID());
  const result=await api(endpoint+'/retry',{request_id:jobRetryRequests.get(job.id)});
  if(result.accepted!==true)throw new Error('retry_unavailable');
  jobRetryRequests.delete(job.id);
  return result;
 },'已提交重试。'),{className:'primary'}));
 if(job.code==='source_rights_denied'&&job.retry_reason!=='failed_history_already_prepared'&&['song','mv_direct'].includes(job.source_preference))tools.append(button('去音乐云盘搜这首',()=>findCloudVersion(job.title)));
 if(!readOnly&&job.can_remove===true)tools.append(button('删除失败记录',e=>action(e.currentTarget,async()=>{
  const result=await api(endpoint+'/remove',{});
  if(result.removed!==true)throw new Error('unavailable');
  jobRetryRequests.delete(job.id);
  return result;
 },'已删除失败记录。')));
 if(tools.children.length)row.append(tools);
}
function findCloudVersion(title,source='cloud'){
 searchQuery=title.trim().slice(0,120);
 searchSource=Object.hasOwn(sourceNames,source)?source:'song';searchOffset=0;searchData=null;searchSelected.clear();
 setView('search',()=>{
  const submit=$('search-submit');
  if(view==='search'&&submit)void searchSongs(submit);
 });
}
function renderSettings(signal){
 $('summary').append(el('h2','KTV 设置'),el('p','设置新房间的声音与画面，管理音源和制作方式。','muted'));
 const shortcuts=el('div',null,'button-row');
 const sourceButton=button('音源授权与下载品质',e=>openSourceDialog(e.currentTarget));
 sourceButton.setAttribute('aria-haspopup','dialog');
 shortcuts.append(sourceButton,button('查看制作进度',()=>setView('jobs')));
 $('summary').append(shortcuts);
 settingsSection('歌曲制作性能',()=>api('scheduler',undefined,signal),renderScheduler,signal);
 settingsSection('新房间默认设置',()=>api('manage/room',undefined,signal),renderRoomDefaults,signal);
 renderStorageSettings(signal);
 renderBackgroundSettings(signal);
}
function settingsSection(title,read,render,signal){
 const section=el('section',null,'section'),body=el('div');
 section.append(el('h2',title),body);
 $('content').append(section);
 async function load(){
  section.setAttribute('aria-busy','true');
  body.replaceChildren(el('p','正在读取…','muted'));
  try{
   const data=await read();
   if(signal.aborted)return;
   body.replaceChildren();
   render(body,data);
  }catch(error){
   if(!signal.aborted)body.replaceChildren(el('p',explain(error),'error'),button('重试',load));
  }finally{
   if(!signal.aborted)section.setAttribute('aria-busy','false');
  }
 }
 void load();
}
function renderStorageSettings(signal){
 const section=el('section',null,'section');
 const grid=el('div',null,'policy-grid');
 const retries=[];
 for(const host of ['mac','nas']){
  const form=el('form',null,'policy-form'),mac=host==='mac',name=mac?'Mac':'NAS';
  const usage=el('p',null,'muted');
  const notice=el('p',null,'muted');
  notice.setAttribute('role','status');
  const description=mac?'达到回收目标时，优先清理最久未使用且已核对 NAS 归档的本机播放成品。':'达到上限时，优先清理最久未使用的歌曲文件。制作中、当前播放和本场待唱歌曲会保留。';
  const note=el('p',description,'setting-note');
  const enabled=el('input');
  enabled.type='checkbox';
  const check=el('label',null,'check-label');
  check.append(enabled,el('span',mac?'达到回收目标自动清理':'达到上限自动清理'));
  const limit=el('input');
  limit.type='number';limit.min='2';limit.max='102400';limit.step='1';limit.required=true;
  const limitField=field(mac?'回收目标（GB）':'容量上限（GB）',limit);
  const save=el('button','保存容量策略','primary');
  const retry=button(`重试读取 ${name} 容量设置`,()=>load());
  save.type='submit';
  form.append(el('h3',name),usage,note,check,limitField,save,notice,retry);
  let loading=false,writing=false,failed=false,revision=0;
  function display(data,{force=false}={}){
   const prior=storagePolicies.get(host)?.policy;
   const changed=prior&&(enabled.checked!==prior.enabled||limit.value!==String(Math.round(prior.limit_bytes/1024**3)));
   if(data)storagePolicies.set(host,data);
   const hasPolicy=!!data?.policy;
   if(hasPolicy&&(force||!changed)){
    enabled.checked=data.policy.enabled;
    limit.value=String(Math.round(data.policy.limit_bytes/1024**3));
   }
   const bytes=mac?data?.hot_cache?.bytes:data?.bytes;
   usage.textContent=bytes==null?'':`${mac?'本机播放成品':'最近统计'} ${size(bytes)}`;
   usage.hidden=bytes==null;
   note.hidden=check.hidden=limitField.hidden=save.hidden=!hasPolicy;
   enabled.disabled=limit.disabled=save.disabled=readOnly||!hasPolicy||writing;
  }
  function status(text,error=false){
   notice.textContent=text;
   notice.hidden=!text;
   notice.classList.toggle('error',error);
  }
  async function load(){
   if(signal.aborted||loading||writing)return;
   loading=true;retry.disabled=true;
   const began=revision;
   if(!storagePolicies.has(host)&&!failed)status('正在读取…');
   try{
    const data=await api('manage/policy/'+host,undefined,signal);
    if(signal.aborted||revision!==began)return;
    failed=false;
    display(data);
    status(data.configured===false?'尚未配置 NAS。':'');
   }catch(error){
    if(signal.aborted||revision!==began)return;
    failed=true;
    const unavailable=!mac&&error.message==='nas_management_unavailable';
    status(unavailable?'暂时无法连接 NAS。':`暂时无法读取 ${name} 容量设置。`,true);
   }finally{
    loading=false;retry.disabled=false;retry.hidden=!failed;
   }
  }
  form.onsubmit=async e=>{
   e.preventDefault();
   if(signal.aborted||save.disabled||!form.reportValidity())return;
   const value={enabled:enabled.checked,limit_bytes:Number(limit.value)*1024**3};
   writing=true;revision++;
   enabled.disabled=limit.disabled=save.disabled=true;
   try{
    const policy=await api('manage/policy',{host,value},signal);
    if(signal.aborted)return;
    failed=false;retry.hidden=true;
    display({...storagePolicies.get(host),policy},{force:true});
    status('容量策略已保存。');
   }catch(error){
    if(!signal.aborted)status(explain(error),true);
   }finally{
    writing=false;
    if(!signal.aborted)enabled.disabled=limit.disabled=save.disabled=readOnly;
   }
  };
  retry.hidden=true;
  display(storagePolicies.get(host),{force:true});
  retries.push(()=>{if(failed)void load();});
  grid.append(form);
  void load();
 }
 section.append(grid);
 section.prepend(el('h2','容量与自动清理'));
 $('content').append(section);
 retryStorageSettings=()=>retries.forEach(retry=>retry());
}
function renderRoomDefaults(section,roomResult){
 section.append(el('p','用于下一次新建的房间。','setting-note'));
 const form=el('form',null,'room-defaults-form'),fields=el('div',null,'settings-fields');
 const defaults=roomResult.defaults;
 const theme=select(themes,defaults.theme),original=select([['0','伴奏'],['1','原唱']],String(defaults.original));
 fields.append(field('房间皮肤',theme),field('演唱模式',original));
 const values={};
 for(const[key,label]of volumeControls){
  const input=el('input');
  input.type='number';input.min='0';input.max='100';input.required=true;input.value=String(defaults[key]);
  values[key]=input;
  fields.append(field(label,input));
 }
 const save=el('button','保存默认设置','primary');
 save.type='submit';
 form.append(fields,save);
 form.onsubmit=e=>{
  e.preventDefault();
  const value={theme:theme.value,original:Number(original.value)};
  for(const[key,input]of Object.entries(values))value[key]=Number(input.value);
  action(save,()=>roomAction('preferences',value),'新房间默认设置已保存。');
 };
 section.append(form);
}
function renderScheduler(section,scheduler){
 section.append(el('p',`${scheduler.hardware.cpu} · ${Math.round(scheduler.hardware.memory_bytes/1024**3)} GB 内存`,'muted'));
 const form=el('form',null,'scheduler-form'),fields=el('div',null,'settings-fields');
 const mode=select([['auto','自动（推荐）'],['manual','手动调整']],scheduler.settings.mode),controls={};
 fields.append(field('运行方式',mode));
 section.append(el('p','伴奏默认并行 3 首，手动可选最多 10 首。','setting-note'));
 for(const[key,title]of [['download','原曲下载'],['model','伴奏制作'],['encode','播放文件封装']]){
  const n=select(Array.from({length:scheduler.limits[key]},(_,i)=>[String(i+1),`${i+1} 路`]),String(scheduler.settings[key]));
  n.disabled=mode.value==='auto';
  controls[key]=n;
  fields.append(field(title,n));
 }
 mode.onchange=()=>Object.values(controls).forEach(n=>n.disabled=mode.value==='auto');
 const save=el('button','保存制作设置','primary');
 save.type='submit';
 form.append(fields,save);
 form.onsubmit=e=>{
  e.preventDefault();
  action(save,()=>api('scheduler',{mode:mode.value,...Object.fromEntries(Object.entries(controls).map(([key,n])=>[key,Number(n.value)]))}),'制作设置已保存，将用于后续任务。');
 };
 section.append(form);
}
function renderBackgroundSettings(signal){
 const section=el('section',null,'section background-settings');
 const status=el('p',null,'muted');status.setAttribute('role','status');
 const policy=el('form',null,'background-limit');policy.hidden=true;
 const enabled=el('input');enabled.type='checkbox';enabled.setAttribute('role','switch');
 enabled.disabled=readOnly;
 const switchField=el('label',null,'background-switch');
 switchField.append(enabled,el('span','启用 AI 歌曲背景'));
 const limit=el('input');limit.type='number';limit.min='0';limit.step='1';limit.required=true;limit.disabled=readOnly;
 const save=el('button','保存上限');save.type='submit';save.disabled=readOnly;
 const result=el('p',null,'muted background-policy-result');result.setAttribute('role','status');
 policy.append(switchField,field('每日生成上限（张）',limit),save);
 const list=el('div',null,'background-items');
 const retry=button('重试读取歌曲背景',()=>readBackgrounds(state));retry.hidden=true;
 const state={signal,section,status,policy,enabled,limit,list,retry,items:new Map(),loading:false,saving:false,
  active:true,remaining:null,issue:'',revision:0,reload:false};
 backgroundView=state;
 section.append(el('h2','歌曲背景'),policy,result,status,retry,list);
 async function savePolicy(patch){
  if(state.saving||readOnly||signal.aborted)return;
  state.saving=true;
  state.revision++;
  enabled.disabled=save.disabled=true;
  result.textContent='';
  try{
   const data=await api('manage/backgrounds/policy',patch,signal);
   if(signal.aborted)return;
   if('daily_images' in patch)limit.defaultValue=limit.value=String(data.daily_limit);
   applyBackgroundPolicy(state,data);
   result.textContent='已保存。';
   if(state.loading)state.reload=true;
   else void readBackgrounds(state);
  }catch(error){
   if(!signal.aborted){enabled.checked=state.active;result.textContent=explain(error);}
  }finally{
   state.saving=false;
   enabled.disabled=save.disabled=readOnly;
  }
 }
 enabled.onchange=()=>void savePolicy({enabled:enabled.checked});
 policy.onsubmit=event=>{
  event.preventDefault();
  void savePolicy({daily_images:limit.valueAsNumber});
 };
 $('content').append(section);
 void readBackgrounds(state);
}
function applyBackgroundPolicy(state,data){
 if(Number.isInteger(data.daily_limit)){
  if(state.policy.hidden||state.limit.value===state.limit.defaultValue)state.limit.value=String(data.daily_limit);
  state.limit.defaultValue=String(data.daily_limit);
  state.policy.hidden=false;
 }
 state.active=data.enabled!==false;
 state.enabled.checked=state.active;
 state.remaining=Number.isInteger(data.daily_remaining)?data.daily_remaining:null;
 state.issue=errors[data.generation_issue]||'';
 updateBackgroundStatus(state);
 for(const row of state.items.values())updateBackgroundRow(state,row);
}
function updateBackgroundStatus(state){
 const quota=state.remaining===0?'已达每日生成上限。':state.remaining===null
  ?(state.policy.hidden?'':'调试模式不限每日张数。'):`今日还可生成 ${state.remaining} 张。`;
 const message=state.active?state.issue:'已关闭，电视使用常规背景。';
 state.status.textContent=[message,quota].filter(Boolean).join(' ');
}
async function readBackgrounds(state){
 if(state.loading||state.signal.aborted)return;
 const revision=state.revision;
 state.loading=true;
 if(!state.items.size)state.status.textContent='正在读取歌曲背景…';
 state.retry.hidden=true;
 try{
  const data=await api('manage/backgrounds',undefined,state.signal);
  if(state.signal.aborted||revision!==state.revision)return;
  applyBackgroundPolicy(state,data);
  const ids=new Set(data.items.map(item=>item.id));
  for(const [id,row] of state.items){
   if(!ids.has(id)&&!row.writing){row.element.remove();state.items.delete(id);}
  }
  for(const item of data.items){
   let row=state.items.get(item.id);
   if(!row){
    const element=el('article',null,'entry'),info=el('div',null,'entry-info');
    const title=el('h3',item.title),status=el('p',null,'muted'),notice=el('p',null,'error');
    const generate=button('生成',()=>generateBackground(state,row));
    const preview=el('canvas',null,'background-preview');preview.hidden=true;
    const previewNotice=el('p',null,'muted');previewNotice.setAttribute('role','status');
    preview.id='ktv-background-preview-'+item.id;preview.setAttribute('role','img');
    preview.setAttribute('aria-label',`《${item.title}》的背景预览`);
    const previewToggle=button('预览',()=>toggleBackgroundPreview(state,row));
    previewToggle.setAttribute('aria-controls',preview.id);
    const controls=el('div',null,'button-row');controls.append(previewToggle,generate);
    info.append(title,status,notice,previewNotice,preview);element.append(info,controls);state.list.append(element);
    row={element,status,notice,generate,preview,previewNotice,previewToggle,item,writing:false,previewOpen:false,previewLoaded:false,previewRevision:0};
    state.items.set(item.id,row);
   }
   if(!row.writing){
    const refreshed=row.item.status==='preparing'&&item.status==='ready';
    row.item=item;
    if(refreshed&&row.previewOpen)void loadBackgroundPreview(state,row);
   }
   updateBackgroundRow(state,row);
  }
 }catch{
  if(!state.signal.aborted&&revision===state.revision){state.status.textContent='暂时无法读取歌曲背景。';state.retry.hidden=false;}
 }finally{
  state.loading=false;
  if(state.reload){state.reload=false;void readBackgrounds(state);}
 }
}
function updateBackgroundRow(state,row){
 const {item}=row;
 const preparing=row.writing||item.status==='preparing';
 const label=preparing?(item.ready?'正在更新背景':'正在生成背景'):({ready:'已生成',failed:item.ready?'更新失败，当前背景可用':'生成失败'})[item.status]||'尚未生成';
 row.status.textContent=[preparationSources[item.source],label].filter(Boolean).join(' · ');
 row.notice.textContent=row.error||(!preparing&&item.status==='failed'?(errors[item.reason]||''):'');
 row.generate.textContent=item.ready?'重新生成':'生成';
 row.generate.setAttribute('aria-label',`${row.generate.textContent}《${item.title}》的背景`);
 row.generate.disabled=readOnly||preparing||!state.active||state.remaining===0;
 row.previewToggle.hidden=!item.ready;
 row.previewToggle.textContent=row.previewOpen?'收起预览':'预览';
 row.previewToggle.setAttribute('aria-expanded',String(row.previewOpen));
 row.previewToggle.setAttribute('aria-label',`${row.previewToggle.textContent}《${item.title}》的背景`);
 row.preview.hidden=!item.ready||!row.previewOpen||!row.previewLoaded;
}
function toggleBackgroundPreview(state,row){
 row.previewOpen=!row.previewOpen;
 updateBackgroundRow(state,row);
 if(row.previewOpen)void loadBackgroundPreview(state,row);
 else row.previewNotice.textContent='';
}
async function loadBackgroundPreview(state,row){
 const revision=++row.previewRevision;
 row.previewNotice.classList.toggle('error',false);
 row.previewNotice.textContent=row.previewLoaded?'':'正在读取背景预览…';
 let bitmap;
 try{
  const data=await api(`manage/backgrounds/${encodeURIComponent(row.item.id)}/preview`,undefined,state.signal);
  if(state.signal.aborted||revision!==row.previewRevision||!row.previewOpen)return;
  const encoded=typeof data.image==='string'&&data.image.length<=192*1024&&data.image.match(/^data:image\/jpeg;base64,([A-Za-z0-9+/=]+)$/)?.[1];
  if(!encoded)throw new Error('background_unavailable');
  const bytes=Uint8Array.from(atob(encoded),character=>character.charCodeAt(0));
  bitmap=await createImageBitmap(new Blob([bytes],{type:'image/jpeg'}));
  if(state.signal.aborted||revision!==row.previewRevision||!row.previewOpen)return;
  row.preview.width=bitmap.width;row.preview.height=bitmap.height;
  row.preview.getContext('2d').drawImage(bitmap,0,0);
  row.previewLoaded=true;row.previewNotice.textContent='';
  updateBackgroundRow(state,row);
 }catch{
  if(!state.signal.aborted&&revision===row.previewRevision&&row.previewOpen){
   row.previewNotice.classList.toggle('error',true);
   row.previewNotice.textContent='暂时无法读取背景预览。';
  }
 }finally{bitmap?.close();}
}
async function generateBackground(state,row){
 if(state.signal.aborted||row.generate.disabled)return;
 row.writing=true;
 row.error='';
 updateBackgroundRow(state,row);
 try{
  const data=await api(`manage/backgrounds/${encodeURIComponent(row.item.id)}/regenerate`,{},state.signal);
  if(!state.signal.aborted){
   row.item={...row.item,status:data.status,ready:data.ready};
   if(row.previewOpen&&data.status==='ready')void loadBackgroundPreview(state,row);
   state.revision++;
   if(state.loading)state.reload=true;
   else void readBackgrounds(state);
  }
 }catch(error){
  if(!state.signal.aborted)row.error=explain(error);
 }finally{
  row.writing=false;
  if(!state.signal.aborted)updateBackgroundRow(state,row);
 }
}
function openSourceDialog(opener){
 if(sourceDialog.open)return;
 sourceSession?.controller.abort();
 sourceSession={opener,controller:new AbortController(),status:null,pending:false,waiting:false};
 sourceState.textContent='正在读取音源状态…';
 sourceNotice.textContent='';
 sourceNotice.classList.toggle('error',false);
 sourceAuthorize.textContent='打开网易云官方登录';
 sourceQuality.value='exhigh';
 sourceRetry.hidden=true;
 sourceDialog.showModal();
 sourceClose.focus({preventScroll:true});
 void sourceRequest();
}
async function sourceRequest(path='source/status',body){
 const session=sourceSession;
 if(!sourceDialog.open||!session||session.pending)return;
 const signal=session.controller.signal;
 session.pending=true;
 sourceAuthorize.disabled=sourceQuality.disabled=sourceRetry.disabled=true;
 if(body!==undefined){
  sourceNotice.textContent=path==='source/login'?'正在打开网易云官方窗口…':'正在保存下载品质…';
  sourceNotice.classList.toggle('error',false);
 }
 try{
  const data=await api(path,body,signal);
  if(signal.aborted)return;
  if(path==='source/settings'){
   session.status.settings=data;
   sourceQuality.value=data.level;
   sourceNotice.textContent='下载品质已保存，将用于新下载。';
  }else{
   if(session.waiting&&data.logged_in)sourceNotice.textContent='';
   session.status=data;
   session.waiting=!data.logged_in&&['waiting_official','waiting_scan','waiting_confirm'].includes(data.state);
   sourceState.textContent=data.logged_in?'音源已连接':({waiting_official:'请在网易云官方窗口完成登录。',waiting_scan:'请在手机扫码并确认。',waiting_confirm:'请在手机确认授权。'})[data.state]||'尚未登录。';
   sourceAuthorize.textContent=data.logged_in?'重新授权':'打开网易云官方登录';
   sourceQuality.value=data.settings?.level||'exhigh';
   sourceRetry.hidden=true;
   if(path==='source/login')sourceNotice.textContent=data.logged_in?'请在网易云官方窗口完成登录。':'';
  }
 }catch(error){
  if(signal.aborted)return;
  if(path==='source/status'){
   session.waiting=false;
   sourceState.textContent='暂时无法读取音源状态。';
   sourceRetry.hidden=false;
  }else{
   sourceNotice.textContent=explain(error);
   sourceNotice.classList.toggle('error',true);
   if(path==='source/settings')sourceQuality.value=session.status?.settings?.level||'exhigh';
  }
 }finally{
  if(!signal.aborted){
   session.pending=false;
   sourceAuthorize.disabled=sourceQuality.disabled=readOnly||!session.status;
   sourceRetry.disabled=false;
  }
 }
}
function setView(name,afterRefresh){
 if(!alive||changing||!tabs.includes(name)||name===view&&!afterRefresh)return;
 clearTimeout(libraryFilterTimer);clearTimeout(libraryIndexTimer);
 viewRequest?.abort();viewRequest=null;
 roomSyncRequest?.abort();searchRequest?.abort();searchRequest=null;
 artworkRequest?.abort();
 view=name;renderSignatures.delete(name);resetContent();
 $('refresh').disabled=false;
 for(const [key]of primaryTabs)$(key+'-tab').setAttribute('aria-pressed',String(key===name||(key==='queue'&&name==='jobs')));
 const loading=refresh();
 const request=viewRequest;
 $('content').focus({preventScroll:true});
 if(afterRefresh)void loading.then(()=>{if(view===name&&!request?.signal.aborted)afterRefresh();});
}
function renderNoRoom(){
 const section=el('section',null,'ktv-empty-state');
 section.append(el('h2','今晚，想唱哪一首？'),el('p','在电视上打开 Friday KTV，歌房和已点歌曲会出现在这里。'),button('先找一首歌',()=>setView('search'),{className:'primary'}));
 $('content').append(section);
}
function renderDisconnected(error){
 const section=el('section',null,'ktv-empty-state');section.append(el('h2',error.message==='ktv_not_configured'?'准备家庭 KTV':'KTV 暂未连接'),el('p',explain(error)),button('重试连接',()=>refresh(),{className:'primary'}));
 if(onBack)section.append(button('返回游戏库',onBack));$('content').append(section);
}
function renderQueueView(data){
 if(!data.active){
  if(!$('content').querySelector('.ktv-empty-state')){
   resetContent();
   const empty=el('section',null,'ktv-empty-state');
   empty.append(el('h2','已点歌曲'),el('p','在电视上打开 Friday KTV 就能点歌。'),button('去点歌',()=>setView('search'),{className:'primary'}));
   $('content').append(empty);
  }
  return;
 }
 renderQueue(data.status);
}
const shell=el('section',null,'ktv-console');shell.dataset.embedded=String(embedded);shell.setAttribute('aria-label','家庭 KTV');
const heading=el('div',null,'ktv-heading'),titles=el('div'),title=el('h2','家庭 KTV');title.tabIndex=-1;
if(onBack){const back=button('返回游戏库',onBack,{className:'ktv-back'});const arrow=el('span',null,'icon icon-arrow-right');arrow.setAttribute('aria-hidden','true');back.prepend(arrow);heading.append(back);}
const context=el('p',readOnly?'只读浏览':'电视播放 · 手机也能点歌','ktv-context');titles.append(title,context);const refreshButton=button('刷新',()=>refresh());refreshButton.id='ktv-refresh';heading.append(titles,refreshButton);
const nav=el('nav',null,'ktv-navigation');nav.setAttribute('aria-label','KTV');for(const[name,label]of primaryTabs){const b=button(label,()=>setView(name));b.id='ktv-'+name+'-tab';b.setAttribute('aria-pressed',String(name==='room'));nav.append(b);}
const status=el('p','','ktv-status');status.id='ktv-status';status.setAttribute('role','status');status.setAttribute('aria-live','polite');
const summary=el('section');summary.id='ktv-summary';summary.setAttribute('aria-label','当前概况');const content=el('div');content.id='ktv-content';content.tabIndex=-1;
const dialog=el('dialog');dialog.id='ktv-delete-dialog';dialog.setAttribute('aria-labelledby','ktv-delete-heading');dialog.setAttribute('aria-describedby','ktv-delete-note');
for(const[tag,id,text]of [['h2','delete-heading','删除歌曲文件'],['p','delete-title',''],['p','delete-note',''],['ul','delete-songs',''],['p','delete-bytes',''],['p','delete-error','']]){const n=el(tag,text);n.id='ktv-'+id;if(id==='delete-error')n.setAttribute('role','alert');dialog.append(n);}
const actions=el('div',null,'actions'),cancel=button('取消',()=>dialog.close()),confirm=button('确认删除',()=>{}, {className:'danger'});cancel.id='ktv-cancel-delete';cancel.autofocus=true;confirm.id='ktv-confirm-delete';actions.append(cancel,confirm);dialog.append(actions);
const sourceDialog=el('dialog',null,'source-dialog');
sourceDialog.id='ktv-source-dialog';sourceDialog.setAttribute('aria-labelledby','ktv-source-heading');
const sourceHeading=el('h2','音源授权'),sourceHeader=el('div',null,'source-dialog-header');
sourceHeading.id='ktv-source-heading';
const sourceClose=button('×',()=>sourceDialog.close(),{className:'source-dialog-close'});
sourceClose.setAttribute('aria-label','关闭音源授权');sourceClose.autofocus=true;
sourceHeader.append(sourceHeading,sourceClose);
const sourceState=el('p',null,'source-status');sourceState.id='ktv-source-state';sourceState.setAttribute('role','status');
const sourceNotice=el('p',null,'source-notice');sourceNotice.setAttribute('role','status');
const sourceButtons=el('div',null,'button-row');
const sourceAuthorize=button('打开网易云官方登录',()=>sourceRequest('source/login',{}),{className:'primary'});
const sourceRetry=button('重试读取状态',()=>sourceRequest());
sourceButtons.append(sourceAuthorize,sourceRetry);
const sourceQuality=select([['exhigh','优先 320 kbps MP3'],['standard','128 kbps MP3']],'exhigh');
sourceQuality.onchange=()=>sourceRequest('source/settings',{level:sourceQuality.value});
sourceDialog.append(sourceHeader,sourceState,sourceButtons,field('新歌下载品质',sourceQuality),sourceNotice);
shell.append(heading,nav,status,summary,content,dialog,sourceDialog);target.append(shell);
// Wire handlers only after the scoped surface exists.
wireDelete();dialog.addEventListener('close',()=>{if(alive)$('delete-selected')?.focus();});
sourceDialog.addEventListener('close',()=>{
 if(sourceDialog.open)return;
 const session=sourceSession;sourceSession=null;
 session?.controller.abort();
 if(alive&&session?.opener.isConnected)session.opener.focus({preventScroll:true});
});
const timer=setInterval(()=>{
 if(!alive||document.hidden)return;
 if(sourceDialog.open){if(sourceSession?.waiting)void sourceRequest();return;}
 if(view==='settings'){
  retryStorageSettings?.();
  if(backgroundView&&[...backgroundView.items.values()].some(row=>row.item.status==='preparing'))void readBackgrounds(backgroundView);
  return;
 }
 if(viewRequest||changing||dialog.open)return;
 if(['room','queue','search'].includes(view)){void syncRoom();return;}
 if(view==='cache'&&selected.size||shell.querySelector('details[open]'))return;
 const focused=document.activeElement;
 if(focused&&focused!==document.body&&content.contains(focused))return;
 void refresh({quiet:true});
},3000);
document.addEventListener?.('visibilitychange',()=>{if(!document.hidden&&['room','queue','search'].includes(view))void syncRoom();},{signal:lifetime.signal});
void refresh();
return{dispose(){if(!alive)return;alive=false;clearRoomIntents();viewRequest?.abort();viewRequest=null;roomSyncRequest?.abort();searchRequest?.abort();sourceSession?.controller.abort();lifetime.abort();clearInterval(timer);clearTimeout(libraryFilterTimer);clearTimeout(libraryIndexTimer);clearTimeout(artworkTimer);artworkObserver?.disconnect();songPresentations.clear();if(dialog.open)dialog.close();if(sourceDialog.open)sourceDialog.close();shell.remove();}};
}

const standalone=document.querySelector('[data-ktv-standalone]');
if(standalone){const start=async()=>{if(!globalThis.FridayPartyAudio)await import('./party-audio.js').catch(()=>{});mountKtv(standalone);};void start();}
