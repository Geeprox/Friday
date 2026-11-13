const element=(tag,cls)=>{const n=document.createElement(tag);if(cls)n.className=cls;return n;};

// A view of an existing source, never a request to start recognition or capture.
export function mountSeatCamera(section,{heading,session,phase,round,api,isCurrent,onChange,go,hostOpens=false,cameraBusy=()=>false}){
 const actions=element('div','ww-camera-view-actions'),toggle=element('button','ww-text-button'),settings=element('button','ww-text-button');
 toggle.type=settings.type='button';toggle.textContent='显示画面';toggle.setAttribute('aria-pressed','false');settings.textContent='摄像头设置';settings.onclick=()=>go('Cameras');
 if(!hostOpens)actions.append(toggle);
 actions.hidden=hostOpens;
 actions.append(settings);heading.append(actions);
 const view=element('div','ww-seat-camera'),frame=element('div','ww-seat-camera-frame'),canvas=element('canvas'),labels=element('div','ww-seat-camera-labels'),status=element('p','ww-field-help');
 frame.setAttribute('role','img');canvas.setAttribute('aria-hidden','true');labels.setAttribute('aria-hidden','true');status.setAttribute('role','status');frame.append(canvas,labels);view.append(frame,status);view.hidden=frame.hidden=true;section.dataset.viewing='false';section.append(view);
 let alive=true,open=false,epoch=0,request=null,next=null,expiry=null,picture=null,until=0;
 const header=document.querySelector?.('.sidebar');
 const offset=()=>section.style.setProperty('--ww-camera-top',Math.ceil(header?.getBoundingClientRect().height||0)+12+'px');
 const headerSize=header?new ResizeObserver(offset):null;headerSize?.observe(header);if(header)offset();
 const current=token=>alive&&open&&token===epoch&&isCurrent()&&!document.hidden;
 function message(text,showSettings=false){
  if(status.textContent!==text)status.textContent=text;
  if(hostOpens)actions.hidden=!showSettings;
 }
 function clear(text,showSettings=false){clearTimeout(expiry);expiry=null;picture=null;until=0;frame.hidden=true;canvas.width=canvas.height=0;labels.replaceChildren();message(text,showSettings);}
 function retire(){epoch++;request?.abort();request=null;clearTimeout(next);next=null;clear('');}
 async function refresh(){
  const token=epoch;if(!current(token)||request)return;
  const controller=new AbortController(),began=performance.now();request=controller;
  try{
   const data=await api(phase?{session,phase,round}:{session},controller.signal);if(!current(token))return;
   if(!data.available&&data.reason==='motion_game_camera_busy'){
    clear(cameraBusy()?'':'体感游戏正在使用镜头，结束后可对应座位。');onChange();return;
   }
   if(!data.available){clear(hostOpens?'镜头画面暂不可用。':'画面暂不可用，可检查摄像头设置。',true);onChange();return;}
   if(!Number.isFinite(data.ttl_ms)||data.ttl_ms<=0||data.ttl_ms>1000||typeof data.jpeg!=='string'||data.jpeg.length>2700000||!Array.isArray(data.people))throw Error('invalid_frame');
   const bytes=Uint8Array.from(atob(data.jpeg),c=>c.charCodeAt(0));
   const bitmap=await createImageBitmap(new Blob([bytes],{type:'image/jpeg'}));
   try{
    if(!current(token))return;
    const remaining=data.ttl_ms-(performance.now()-began);
    if(remaining<=0||bitmap.width!==data.width||bitmap.height!==data.height)throw Error('expired_frame');
    canvas.width=bitmap.width;canvas.height=bitmap.height;canvas.getContext('2d').drawImage(bitmap,0,0);
    const boxes=data.people.map(person=>{const box=element('div','ww-seat-camera-person'),label=element('span');box.dataset.track=String(person.track);box.dataset.edge=person.box.x+person.box.w/2>.5?'end':'start';Object.assign(box.style,{left:person.box.x*100+'%',top:person.box.y*100+'%',width:person.box.w*100+'%',height:person.box.h*100+'%'});label.textContent='#'+person.track;box.append(label);return box;});
    labels.replaceChildren(...boxes);frame.setAttribute('aria-label',data.people.length?'镜头中的人物编号：'+data.people.map(p=>p.track).join('、'):'当前镜头画面，尚未看到可对应的人物');frame.hidden=false;
    clearTimeout(expiry);picture=data;until=performance.now()+remaining;
    if(data.people.length)message(hostOpens?'':'选择画面中的编号，对应大家的座位。');
    else message('走到镜头前，就会出现可对应的编号。');
    expiry=setTimeout(()=>{if(current(token)){clear('画面正在恢复，编号出现后可继续对应。');onChange();}},remaining);
    onChange();
   }finally{bitmap.close();}
  }catch{if(current(token)){clear('画面暂不可用，正在重试。');onChange();}}
  finally{if(request===controller){request=null;if(current(token))next=setTimeout(()=>{next=null;void refresh();},400);}}
 }
 function setOpen(value){if(!alive||!isCurrent()||open===value)return;open=value;retire();view.hidden=!open;section.dataset.viewing=String(open);toggle.textContent=open?'隐藏画面':'显示画面';toggle.setAttribute('aria-pressed',String(open));onChange();if(open){if(!cameraBusy())message('正在读取画面…');void refresh();}}
 toggle.onclick=()=>setOpen(!open);
 const visibility=()=>{if(!open)return;retire();onChange();if(!document.hidden){if(!cameraBusy())message('正在读取画面…');void refresh();}};
 document.addEventListener('visibilitychange',visibility);
 return{
  show(){setOpen(true);},
  recheck(){
   if(!open)return;
   if(!isCurrent()){
    retire();message('正在同步对局…');onChange();
   }else if(!picture&&!request&&!next&&!document.hidden)void refresh();
  },
  available(fallback){return open?Boolean(picture&&performance.now()<until):fallback===true;},
  candidates(fallback,source){
   if(!open)return fallback;
   const observed=source||fallback[0];
   if(picture&&observed&&(observed.camera!==picture.camera||observed.session!==picture.session))clear('镜头已切换，正在读取画面…');
   return picture&&performance.now()<until?picture.people.map(p=>({camera:picture.camera,session:picture.session,
    track:p.track,track_revision:p.track_revision??0})):[];
  },
  dispose(){alive=false;open=false;retire();headerSize?.disconnect();section.dataset.viewing='false';document.removeEventListener('visibilitychange',visibility);view.remove();actions.remove();}
 };
}
