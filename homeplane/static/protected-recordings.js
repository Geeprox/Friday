import {mountEventEvidence} from './event-evidence.js';

const element=(tag,text,attributes={})=>{
  const node=document.createElement(tag);
  if(text!=null)node.textContent=text;
  for(const[key,value]of Object.entries(attributes))node.setAttribute(key,value);
  return node;
};
const append=(node,...children)=>{node.append(...children);return node;};
const button=(text,run,attributes={})=>{
  const node=element('button',text,{type:'button',...attributes});
  node.addEventListener('click',run);return node;
};
const reasons={detector_unconfigured:'检测器未配置，已整帧遮挡',detector_unavailable:'检测暂不可用，已整帧遮挡',analysis_uncertain:'部分画面无法确定，已加强遮挡'};
const errors={recording_invalid_offset:'这个时间点已不在录像内，请重新选择。',archive_privacy_busy:'另一个隐私预览正在准备，请稍后重试。',archive_privacy_budget:'片段超过预览限额，请选择更短的录像。',recording_source_changed:'录像来源已变化，请刷新列表。',recording_refresh_required:'这条录像引用已变化，请刷新列表。',archive_privacy_unavailable:'隐私预览暂不可生成，请检查本机隐私播放依赖后重试。'};

export function protectedRecordingPlan(plan){
  return plan?.privacy_protected===true&&plan.privacy_preview_generated===true&&plan.original_modified===false
    &&/^[a-f0-9]{48}$/.test(plan.token??'')&&plan.url===('/api/home/recordings/media/'+plan.token)
    &&['local_person_mosaic','local_full_frame_mosaic'].includes(plan.privacy_mode)
    &&Number.isFinite(plan.expires_in)&&plan.expires_in>0&&plan.expires_in<=300
    &&Number.isFinite(plan.source_duration_seconds)&&plan.source_duration_seconds>0&&plan.source_duration_seconds<=600
    &&Number.isFinite(plan.media_start_seconds)&&plan.media_start_seconds>=0&&plan.media_start_seconds<plan.source_duration_seconds
    &&Number.isFinite(plan.duration_seconds)&&plan.duration_seconds>0&&plan.duration_seconds<=30.15;
}

export function mountRecordings(root,{api,isAlive=()=>true,go=()=>{},openEvidenceRecording=()=>{}}={}){
  if(!document.getElementById('protected-recordings-style')){
    const style=element('link',null,{id:'protected-recordings-style',rel:'stylesheet',href:'/static/protected-recordings.css'});
    (document.head||root).append(style);
  }
  let alive=true,generation=0,listingGeneration=0,plan=null,video=null,request=null,expiry=null;
  let folderValue='',dateValue='',listingContext=null,contextTimer=null;
  let nextPlan=null,nextRequest=null,nextTask=null,cleanup=Promise.resolve();
  const current=()=>alive&&isAlive();
  const shell=element('div',null,{class:'protected-recordings'}),evidenceRoot=element('div');
  const evidencePlayer=element('section',null,{'aria-label':'事件录像',class:'evidence-playback'});
  const recordingsRoot=element('section',null,{'aria-label':'录像列表',class:'recording-section'});
  const eventDetails=append(element('details',null,{class:'recording-events'}),element('summary','查看家庭事件'),evidenceRoot);
  append(shell,recordingsRoot,eventDetails,evidencePlayer);root.append(shell);

  function release(value){if(/^[a-f0-9]{48}$/.test(value?.token??''))return api('/api/home/recordings/stop',{token:value.token}).catch(()=>{});return Promise.resolve();}
  function stop(){
    generation++;request?.abort();request=null;nextRequest?.abort();nextRequest=null;nextTask=null;
    const releasing=[release(nextPlan)];nextPlan=null;
    if(expiry!==null)clearTimeout(expiry);expiry=null;
    video?.pause();video?.removeAttribute('src');video?.load();video=null;
    releasing.push(release(plan));plan=null;cleanup=Promise.allSettled([cleanup,...releasing]);
  }
  function clearEvidence(){void Promise.resolve(openEvidenceRecording(evidencePlayer,null)).catch(()=>{});}
  const evidence=mountEventEvidence(evidenceRoot,{api,isAlive:current,go,onOpenRecording:async details=>{
    stop();await openEvidenceRecording(evidencePlayer,details);
  }});
  const stamp=value=>typeof value==='string'&&value.length===14
    ?`${value.slice(0,4)}-${value.slice(4,6)}-${value.slice(6,8)} ${value.slice(8,10)}:${value.slice(10,12)}:${value.slice(12,14)}`:'时间未知';

  async function load(){
    stop();const version=++listingGeneration;
    recordingsRoot.replaceChildren(element('h2','录像'),element('p','正在读取录像列表…',{role:'status'}));
    const query=new URLSearchParams();if(folderValue)query.set('folder',folderValue);if(dateValue)query.set('date',dateValue.replaceAll('-',''));
    try{
      const listing=await api('/api/home/recordings?'+query);
      if(!current()||version!==listingGeneration)return;
      listingContext=listing.context_fingerprint??null;
      const header=element('div',null,{class:'recording-toolbar'}),folder=element('select',null,{'aria-label':'录像来源'});
      folder.append(element('option','全部摄像头',{value:''}));
      for(const name of listing.folders??[])folder.append(element('option',listing.names?.[name]||`摄像头 ${Number(name.split('_')[1])+1}`,{value:name}));
      folder.value=folderValue;folder.addEventListener('change',()=>{folderValue=folder.value;void load();});
      const date=element('input',null,{type:'date','aria-label':'录像日期',value:dateValue});
      date.addEventListener('change',()=>{dateValue=date.value;void load();});
      append(header,folder,date,button('最新录像',()=>{dateValue='';void load();}),button('刷新录像',()=>void load()));
      const list=element('div',null,{class:'recording-list'}),player=element('section',null,{class:'recording-player','aria-label':'隐私录像播放器'});
      const empty=()=>player.replaceChildren(element('h3','选择一段录像'),element('p','播放前在本机生成保护预览，原始录像保留。',{class:'muted'}));
      empty();recordingsRoot.replaceChildren(element('h2','录像'),header,append(element('div',null,{class:'recording-layout'}),list,player));
      if(!listing.connected){
        player.replaceChildren(element('h3','录像暂时无法读取'),element('p',listing.code==='recordings_mount_required'?'自动恢复 NAS 连接未完成，请检查网络或已保存的登录。':'NAS 暂时无法读取，请稍后重试。'),button('重试连接',()=>void load()),button('登录与存储设置',()=>go('Settings')));return;
      }
      if(!listing.files?.length)list.append(element('p','这一天没有片段，可换个日期或查看最新录像。',{class:'product-empty'}));
      const timeLabel=seconds=>`${Math.floor(seconds/60)}:${String(Math.floor(seconds%60)).padStart(2,'0')}`;
      for(const item of listing.files??[]){
        async function begin(offset=0,event){
          const muted=video?.muted??true;
          clearEvidence();stop();const playbackVersion=generation;
          const active=()=>current()&&version===listingGeneration&&playbackVersion===generation;
          row.disabled=true;request=new AbortController();const signal=request.signal;
          player.replaceChildren(element('h3','正在准备录像'),element('p','完成这一段画面保护后即开始播放。',{role:'status','aria-live':'polite'}),button('取消',()=>{stop();empty();}));
          async function fetchWindow(start,abortSignal){
            const value=await api('/api/home/recordings/play',{id:item.id,offset_seconds:start},{signal:abortSignal});
            if(!protectedRecordingPlan(value)||Math.abs(value.media_start_seconds-start)>.001){release(value);throw Error('archive_privacy_unavailable');}
            value.localDeadline=Date.now()+value.expires_in*1000;
            if(!active()){release(value);return null;}
            return value;
          }
          try{
            await cleanup;
            if(!active())return;
            const value=await fetchWindow(offset,signal);
            if(!value)return;
            request=null;
            video=element('video',null,{controls:'',playsinline:'',preload:'auto','aria-label':'隐私保护录像'});video.muted=muted;video.autoplay=true;
            const playing=video,status=element('p','正在载入录像…',{class:'caption',role:'status'});
            const clock=element('output','',{class:'recording-clock','aria-live':'off'});
            const timeline=element('input',null,{type:'range',min:'0',max:String(value.source_duration_seconds),step:'1','aria-label':'选择录像时间'});
            const advance=button('继续播放',()=>void continuePlayback(),{hidden:''});
            timeline.addEventListener('change',()=>void begin(Math.min(Number(timeline.value),value.source_duration_seconds-0.1)));
            const updateClock=()=>{
              if(!active()||!plan)return;
              const position=Math.min(plan.source_duration_seconds,plan.media_start_seconds+(playing.currentTime||0));
              if(document.activeElement!==timeline)timeline.value=String(position);
              clock.textContent=`${timeLabel(position)} / ${timeLabel(plan.source_duration_seconds)}`;
              timeline.setAttribute('aria-valuetext',`${timeLabel(position)}，共 ${timeLabel(plan.source_duration_seconds)}`);
            };
            timeline.addEventListener('input',()=>{clock.textContent=`${timeLabel(Number(timeline.value))} / ${timeLabel(value.source_duration_seconds)}`;});
            function prefetch(){
              if(!active()||!plan)return;
              const start=plan.media_start_seconds+30;
              if(start>=plan.source_duration_seconds-.05)return;
              nextRequest=new AbortController();const prefetchSignal=nextRequest.signal;
              nextTask=fetchWindow(start,prefetchSignal).then(next=>{
                if(!next)return null;
                nextPlan=next;return next;
              }).catch(()=>null);
            }
            function showWindow(value){
              const previous=plan;plan=value;
              if(expiry!==null)clearTimeout(expiry);
              expiry=setTimeout(()=>{if(active()&&plan===value){stop();status.textContent='录像播放已暂停，请重新选择时间继续。';}},Math.max(0,value.localDeadline-Date.now()));
              advance.hidden=true;playing.src=value.url;updateClock();
              const description=value.privacy_mode==='local_full_frame_mosaic'?(reasons[value.privacy_degraded_reason]||'整帧遮挡保护'):'人物遮挡保护';
              status.textContent=`${description} · ${playing.muted?'静音播放':'声音已开启'}`;
              const released=release(previous);
              void playing.play().catch(()=>{if(active())status.textContent='录像已就绪，点击画面中的播放按钮开始。';});
              void released.then(()=>{if(active()&&plan===value)prefetch();});
            }
            async function continuePlayback(){
              if(!active()||!plan)return;
              if(plan.media_start_seconds+30>=plan.source_duration_seconds-.05){status.textContent='已播放到这段录像的结尾。';return;}
              advance.hidden=true;status.textContent='正在准备后续画面…';
              if(!nextTask)prefetch();
              const next=await nextTask;
              if(!active())return;
              nextTask=null;nextRequest=null;nextPlan=null;
              if(!next){status.textContent='后续画面暂未就绪，可点击继续播放重试。';advance.hidden=false;return;}
              showWindow(next);
            }
            playing.addEventListener('timeupdate',updateClock);
            playing.addEventListener('ended',()=>void continuePlayback());
            playing.addEventListener('error',()=>{if(active()){stop();status.textContent='录像未能播放，请重新选择片段。';}});
            player.replaceChildren(playing,append(element('div',null,{class:'recording-timeline'}),timeline,clock),element('h3',listing.names?.[item.folder]||`摄像头 ${Number(item.folder.split('_')[1])+1}`),element('p',stamp(item.recorded_start)+' — '+stamp(item.recorded_end)),status,advance,button('停止回放',()=>{stop();empty();}));
            showWindow(value);
            if(event?.isTrusted)player.scrollIntoView?.({block:'nearest'});
            for(const sibling of list.children)sibling.classList.remove('selected');row.classList.add('selected');
          }catch(error){
            if(active()){request=null;player.replaceChildren(element('h3','暂不可播放'),element('p',errors[error?.message]||'录像未就绪，请重试或换一个片段。'),button('重试',()=>void begin(offset)));}
          }finally{row.disabled=false;}
        }
        const row=button('',event=>void begin(0,event),{class:'recording-row','data-recording-id':item.id});
        append(row,element('span','',{class:'icon icon-video','aria-hidden':'true'}),append(element('div'),element('strong',stamp(item.recorded_start)),element('span',(listing.names?.[item.folder]||`摄像头 ${Number(item.folder.split('_')[1])+1}`)+' · '+Math.round(item.size/1024/1024)+' MB')));list.append(row);
      }
      if(listing.files?.length)queueMicrotask(()=>{if(current()&&version===listingGeneration)list.firstElementChild?.click?.();});
      const details=append(element('details'),element('summary','隐私保护详情'),element('p','本机逐帧生成保护预览；检测器未配置或无法判断时加强至整帧遮挡。自动识别仍可能漏检。原片单段上限 10 分钟、256 MiB；按播放位置逐段准备，每段最多 30 秒、64 MiB，五分钟后清除。'));
      recordingsRoot.append(element('p',listing.partial?'目录条目较多，当前列表不完整。可选择具体摄像头和日期缩小范围。':'时间来自录像文件名，尚不代表摄像头时钟已校准。',{class:'caption'}),details);
    }catch(_){if(current()&&version===listingGeneration)recordingsRoot.replaceChildren(element('h2','录像'),element('p','录像列表暂不可读取，请重试。',{role:'status'}),button('重试',()=>void load()));}
  }
  contextTimer=setInterval(async()=>{
    if(!current()||!plan||!listingContext)return;
    const activePlan=plan;
    try{
      const context=await api('/api/archive/context');
      if(plan!==activePlan||!current())return;
      if(context.fingerprint!==listingContext){stop();recordingsRoot.replaceChildren(element('h2','录像来源已变化'),element('p','已停止旧预览，请重新读取列表。'),button('刷新录像',()=>void load()));}
    }catch(_){if(plan===activePlan)stop();}
  },15000);
  void load();
  return {refresh(){evidence.refresh();return load();},dispose(){alive=false;listingGeneration++;stop();clearInterval(contextTimer);evidence.dispose();clearEvidence();shell.remove();}};
}
