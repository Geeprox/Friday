const el=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
const messages={identity_candidate_unavailable:'候选人脸已更新，请刷新后重试。',identity_samples_required:'请面向镜头停留片刻，等待清晰人脸。',identity_session_changed:'镜头已切换，请重新选择。',identity_samples_inconsistent:'人脸样本不一致，请单独站到镜头前重试。',people_revision_conflict:'人物记录已变化，请刷新后重试。',people_name_conflict:'名字已存在，请选择已有的人物。'};

export function mountLivePeople(root,{readOnly=false,onSaved=()=>{}}={}){
  let data=null,closed=false,busy=false,timer=null,choice=null,imageCamera=null,showLive=false;
  const candidateNodes=new Map();
  root.classList.add('people-live');
  const title=el('h2','实时人物'),status=el('span','正在连接…',{class:'live-caption'});
  const control=el('button','现场补充',{type:'button'}),cameraSelect=el('select',undefined,{'aria-label':'人物识别镜头'});
  const header=el('div',undefined,{class:'live-heading'});header.append(title,status,cameraSelect,control);
  const note=el('p','人脸自动积累，填写昵称即可登记。',{class:'inspector-subtitle'});
  const body=el('div',undefined,{class:'live-workspace'}),view=el('div',undefined,{class:'live-preview'});
  const image=el('img',undefined,{alt:'当前登记镜头'}),boxes=el('div',undefined,{class:'live-face-boxes'});
  view.append(image,boxes);view.hidden=true;
  const side=el('div'),tracks=el('div',undefined,{class:'live-track-list'}),form=el('form');
  const identity=el('select',undefined,{'aria-label':'登记到人物'}),name=el('input',undefined,{placeholder:'姓名',maxlength:'40','aria-label':'人物姓名'});
  const save=el('button','登记当前人物',{type:'submit'}),notice=el('p','',{role:'status','aria-live':'polite',class:'live-caption'});
  identity.addEventListener('change',()=>{const selected=data?.profiles.find(p=>p.person_id===identity.value);name.value=selected?.name||'';name.disabled=Boolean(selected);});
  form.append(identity,name,save);form.hidden=readOnly;side.append(tracks,form);body.append(view,side);
  const profiles=el('div',undefined,{class:'live-profiles'}),candidates=el('div',undefined,{class:'live-candidates'});body.hidden=true;root.append(header,note,notice,profiles,candidates,body);
  function render(){
    if(!data||closed)return;
    const whole=data.recognition||{},rec=whole.sessions?.find(s=>s.camera===(cameraSelect.value||whole.camera))||whole,cameras=data.cameras||[],room=id=>cameras.find(c=>c.id===id)?.room||'镜头';
    const rows=rec.tracks||[];
    const running=!['stopped',undefined].includes(whole.state);
    if(cameraSelect.dataset.loaded!=='true'){
      cameraSelect.append(el('option','默认镜头',{value:''}));for(const c of cameras)cameraSelect.append(el('option',c.name||c.room,{value:c.id}));cameraSelect.dataset.loaded='true';
    }
    cameraSelect.disabled=busy;cameraSelect.hidden=!showLive;control.disabled=busy||readOnly;control.hidden=readOnly;
    control.textContent=showLive?'收起现场':'现场补充';body.hidden=!showLive;
    const statusRows=showLive?[rec]:(whole.sessions||[whole]);
    status.textContent=statusRows.some(s=>s.state==='live')?'后台识别中':statusRows.every(s=>s.state==='stopped')?'识别已停止':'画面恢复中';
    tracks.replaceChildren();boxes.replaceChildren();
    if((choice?.camera!==rec.camera||choice?.session!==rec.session||!rows.some(r=>r.track===choice?.track))&&!busy)choice=null;
    form.hidden=readOnly||!rows.length;
    if(!rows.length)tracks.append(el('p',rec.state==='live'?'暂未检测到清晰人脸。请面向镜头停留片刻。':running?'正在等待摄像头画面…':'开始识别后，面向镜头停留片刻。',{class:'live-caption'}));
    for(const r of rows){
      const person=data.profiles.find(p=>p.person_id===r.person_id);
      const label=person?.name||`当前人物 ${r.track}`;
      const b=el('button',`${label} · ${r.enroll_ready?'可登记':'采样中'}`,{type:'button','aria-pressed':choice?.track===r.track});
      b.disabled=busy||readOnly;b.onclick=()=>{choice={track:r.track,camera:rec.camera,session:rec.session};render();};tracks.append(b);
      if(r.box){const outline=el('span',label,{class:'live-face-outline'});Object.assign(outline.style,{left:`${r.box.x*100}%`,top:`${r.box.y*100}%`,width:`${r.box.w*100}%`,height:`${r.box.h*100}%`});boxes.append(outline);}
    }
    const old=identity.value,known=data.profiles.map(p=>p.person_id).join(',');
    if(identity.dataset.known!==known){identity.replaceChildren(el('option','登记新人物',{value:''}));for(const p of data.profiles)identity.append(el('option',p.name,{value:p.person_id}));identity.dataset.known=known;identity.value=old;}
    const ready=choice&&rows.find(r=>r.track===choice.track)?.enroll_ready;
    save.disabled=busy||!ready;
    if(showLive&&rec.state==='live'&&rec.camera){
      view.hidden=false;if(imageCamera!==rec.camera){imageCamera=rec.camera;image.src=`/api/embodied/cameras/${encodeURIComponent(rec.camera)}/frame?t=${Date.now()}`;}
    }else view.hidden=true;
    const available=new Set((data.candidates||[]).map(c=>c.id));
    for(const [id,card] of candidateNodes)if(!available.has(id)){card.remove();candidateNodes.delete(id);}
    for(const candidate of data.candidates||[]){
      if(candidateNodes.has(candidate.id)){
        const card=candidateNodes.get(candidate.id),photo=card.querySelector('img');
        if(card.dataset.portrait!==String(candidate.portrait_revision)){
          photo.src=`/api/people/live/candidates/${encodeURIComponent(candidate.id)}/portrait?v=${candidate.portrait_revision}`;
          card.dataset.portrait=String(candidate.portrait_revision);
        }
        continue;
      }
      const card=el('form',undefined,{class:'live-candidate'}),photo=el('img',undefined,{class:'live-candidate-portrait',alt:'待命名的人脸',src:`/api/people/live/candidates/${encodeURIComponent(candidate.id)}/portrait`});
      const input=el('input',undefined,{placeholder:'填写昵称',maxlength:'40','aria-label':'待命名人物昵称'}),submit=el('button','登记昵称',{type:'submit'});
      const existing=el('select',undefined,{'aria-label':'登记到已有的人物'});
      existing.append(el('option','新人物',{value:''}));for(const p of data.profiles)existing.append(el('option',p.name,{value:p.person_id}));
      existing.disabled=readOnly;existing.onchange=()=>{const p=data.profiles.find(p=>p.person_id===existing.value);input.value=p?.name||'';input.disabled=Boolean(p)||readOnly;};
      submit.disabled=readOnly;input.disabled=readOnly;
      card.dataset.portrait=String(candidate.portrait_revision);
      card.append(photo,el('p','待命名'),existing,input,submit);
      card.onsubmit=event=>{event.preventDefault();if(!busy&&input.value.trim())void mutate('name-candidate',{expected_revision:data.revision,candidate:candidate.id,name:input.value.trim(),...(existing.value?{person_id:existing.value}:{})});};
      candidateNodes.set(candidate.id,card);candidates.append(card);
    }
    profiles.replaceChildren();
    if(!data.profiles.length)profiles.append(el('p','尚未登记人物。',{class:'live-caption'}));
    for(const p of data.profiles){
      const sightings=data.observations.filter(o=>o.person_id===p.person_id),active=sightings.filter(o=>o.present);
      const rooms=[...new Set(active.map(o=>room(o.camera)))];
      const latest=sightings[0],place=rooms.length>1?`同时出现在 ${rooms.join('、')} · 待核对`:rooms.length?rooms[0]:latest?`最近在 ${room(latest.camera)} · ${new Date(latest.last_seen*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}`:'尚无房间记录';
      const row=el('div',undefined,{class:'live-profile-row'}),text=el('div');text.append(el('strong',p.name),el('p',p.enrolled?place:'名字已保存 · 尚未登记人脸',{class:'live-caption'}));
      if(p.portrait)row.append(el('img',undefined,{class:'live-portrait',alt:p.name+'的登记人脸',src:`/api/people/live/${encodeURIComponent(p.person_id)}/portrait?v=${p.updated}`,loading:'lazy'}));
      row.append(text);
      if(p.enrolled&&!readOnly){const forget=el('button','删除人脸登记',{type:'button'});forget.disabled=busy;forget.onclick=()=>mutate('forget',{person_id:p.person_id});row.append(forget);}
      profiles.append(row);
    }
  }
  async function refresh(){
    if(closed||busy)return;
    try{const r=await fetch('/api/people/live',{cache:'no-store'});if(!r.ok)throw Error();data=await r.json();imageCamera=null;render();}
    catch{status.textContent='实时人物暂不可用';}
  }
  async function mutate(path,body){
    if(busy||readOnly)return;busy=true;save.disabled=true;for(const card of candidateNodes.values())card.querySelector('button').disabled=true;
    try{const response=await fetch(`/api/people/live/${path}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),result=await response.json();
      if(!response.ok){notice.textContent=messages[result.detail?.code]||'登记未完成，请刷新后重试。';return;}
      data=result;notice.textContent=path==='forget'?'人脸登记已删除。':'人脸已登记。';onSaved();
    }catch{notice.textContent='连接中断，请核对登记状态。';}finally{busy=false;for(const card of candidateNodes.values())card.querySelector('button').disabled=readOnly;await refresh();}
  }
  control.onclick=()=>{showLive=!showLive;imageCamera=null;render();};
  cameraSelect.onchange=()=>{choice=null;imageCamera=null;render();};
  form.addEventListener('submit',event=>{event.preventDefault();if(!choice||!name.value.trim()||!data)return;void mutate('register',{expected_revision:data.revision,...choice,name:name.value.trim(),...(identity.value?{person_id:identity.value}:{})});});
  void refresh();timer=setInterval(refresh,1800);
  return {destroy(){closed=true;clearInterval(timer);root.replaceChildren();}};
}
