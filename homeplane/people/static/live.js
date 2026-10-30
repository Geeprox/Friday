const el=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
const messages={identity_candidate_unavailable:'候选人脸已更新，请刷新后重试。',identity_samples_required:'请面向镜头停留片刻，等待清晰人脸。',identity_session_changed:'镜头已切换，请重新选择。',identity_samples_inconsistent:'人脸样本不一致，请单独站到镜头前重试。',people_revision_conflict:'人物记录已变化，请刷新后重试。',people_name_conflict:'名字已存在，请选择已有的人物。'};

export function mountLivePeople(root,{readOnly=false,onSaved=()=>{}}={}){
  let data=null,closed=false,busy=false,timer=null,choice=null,imageCamera=null,showLive=false;
  const candidateNodes=new Map(),forgetButtons=new Map();
  const selectedGroups=new Set();
  let removalDialog=null;
  root.classList.add('people-live');
  const title=el('h2','实时人物'),status=el('span','正在连接…',{class:'live-caption'});
  const control=el('button','现场补充',{type:'button'}),cameraSelect=el('select',undefined,{'aria-label':'人物识别镜头'});
  const header=el('div',undefined,{class:'live-heading'});header.append(title,status,cameraSelect,control);
  const note=el('p','相似的人脸会归在一起。核对照片是同一人后，再登记姓名或归入已有家人。',{class:'inspector-subtitle'});
  const body=el('div',undefined,{class:'live-workspace'}),view=el('div',undefined,{class:'live-preview'});
  const image=el('img',undefined,{alt:'当前登记镜头'}),boxes=el('div',undefined,{class:'live-face-boxes'});
  view.append(image,boxes);view.hidden=true;
  const side=el('div'),tracks=el('div',undefined,{class:'live-track-list'}),form=el('form');
  const identity=el('select',undefined,{'aria-label':'登记到人物'}),name=el('input',undefined,{placeholder:'姓名',maxlength:'40','aria-label':'人物姓名'});
  const save=el('button','登记当前人物',{type:'submit'}),notice=el('p','',{role:'status','aria-live':'polite',class:'live-caption'});
  identity.addEventListener('change',()=>{const selected=data?.profiles.find(p=>p.person_id===identity.value);name.value=selected?.name||'';name.disabled=Boolean(selected);});
  form.append(identity,name,save);form.hidden=readOnly;side.append(tracks,form);body.append(view,side);
  const profiles=el('div',undefined,{class:'live-profiles'}),candidates=el('div',undefined,{class:'live-candidates'});candidates.hidden=readOnly;body.hidden=true;
  const groupTools=el('div',undefined,{class:'live-group-tools'}),groupTitle=el('h3','待确认的人物'),merge=el('button','选中的是同一人',{type:'button'}),undoGroup=el('button','撤销归组',{type:'button'});
  groupTools.append(groupTitle,merge,undoGroup);groupTools.hidden=readOnly;root.append(header,note,notice,profiles,groupTools,candidates,body);
  const visibleCandidates=()=>data?.candidate_groups??data?.candidates??[];
  function updateGrouping(){merge.disabled=busy||selectedGroups.size<2;undoGroup.disabled=busy||!data?.group_undo_available;groupTitle.textContent='待确认的人物'+(visibleCandidates().length?' · '+visibleCandidates().length:'');}
  merge.onclick=()=>{const members=[...new Set(visibleCandidates().filter(c=>selectedGroups.has(c.id)).flatMap(c=>c.members||[c.id]))];if(members.length>8){notice.textContent='一次最多合并 8 组照片，请分次选择。';return;}void mutate('candidate-group',{members});};
  undoGroup.onclick=()=>void mutate('candidate-group/undo',{});
  function confirmForget(profile){
    if(closed||busy||readOnly||removalDialog)return;
    const dialog=el('dialog',undefined,{class:'people-dialog','aria-labelledby':'people-forget-title'});
    removalDialog=dialog;
    const dismiss=()=>{dialog.close();dialog.remove();removalDialog=null;forgetButtons.get(profile.person_id)?.focus();};
    const cancel=el('button','取消',{type:'button',autofocus:''});cancel.onclick=dismiss;
    const confirm=el('button','删除人脸登记',{type:'button'});
    confirm.onclick=()=>{dismiss();void mutate('forget',{person_id:profile.person_id});};
    const actions=el('div',undefined,{class:'dialog-actions'});actions.append(cancel,confirm);
    dialog.append(el('h2','删除人脸登记？',{id:'people-forget-title'}),el('p',`删除 ${profile.name} 的人脸样本后，需要重新登记才能识别。人物名字仍保留。`),actions);
    dialog.addEventListener('cancel',event=>{event.preventDefault();dismiss();});root.append(dialog);dialog.showModal();
  }
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
    status.textContent=statusRows.some(s=>s.state==='live')?'后台识别中':statusRows.length&&statusRows.every(s=>['shutter_closed','privacy_closed','privacy_shutter_closed'].includes(s.state))?'镜头已关闭，暂未识别':statusRows.some(s=>s.state==='paused')?'人物识别已暂停':statusRows.every(s=>s.state==='stopped')?'识别已停止':'正在连接摄像头';
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
    const available=new Set(visibleCandidates().map(c=>c.id));
    for(const id of selectedGroups)if(!available.has(id))selectedGroups.delete(id);
    for(const [id,card] of candidateNodes)if(!available.has(id)){card.remove();candidateNodes.delete(id);}
    for(const candidate of visibleCandidates()){
      if(candidateNodes.has(candidate.id)){
        const card=candidateNodes.get(candidate.id),photo=card.querySelector('img');
        card.groupData=candidate;
        const existing=card.querySelector('select'),known=data.profiles.map(p=>p.person_id).join(',');
        if(existing.dataset.known!==known){const selected=existing.value;existing.replaceChildren(el('option','新人物',{value:''}));for(const p of data.profiles)existing.append(el('option',p.name,{value:p.person_id}));existing.dataset.known=known;existing.value=data.profiles.some(p=>p.person_id===selected)?selected:'';}
        if(card.groupSelect)card.groupSelect.checked=selectedGroups.has(candidate.id);
        if(card.dataset.portrait!==String(candidate.portrait_revision)){
          photo.src=`/api/people/live/candidates/${encodeURIComponent(candidate.portrait_id||candidate.id)}/portrait?v=${candidate.portrait_revision}`;
          card.dataset.portrait=String(candidate.portrait_revision);
        }
        continue;
      }
      const card=el('form',undefined,{class:'live-candidate'}),photo=el('img',undefined,{class:'live-candidate-portrait',alt:'待确认的人脸',src:`/api/people/live/candidates/${encodeURIComponent(candidate.portrait_id||candidate.id)}/portrait`});
      const input=el('input',undefined,{placeholder:'填写姓名',maxlength:'40','aria-label':'待确认人物姓名'}),submit=el('button','确认并登记',{type:'submit'});
      const existing=el('select',undefined,{'aria-label':'登记到已有的人物'});
      existing.append(el('option','新人物',{value:''}));for(const p of data.profiles)existing.append(el('option',p.name,{value:p.person_id}));
      existing.disabled=readOnly;existing.onchange=()=>{const p=data.profiles.find(p=>p.person_id===existing.value);input.value=p?.name||'';input.disabled=Boolean(p)||readOnly;};
      submit.disabled=readOnly;input.disabled=readOnly;
      card.dataset.portrait=String(candidate.portrait_revision);
      card.groupData=candidate;
      const count=candidate.members?.length||1;
      card.append(photo,el('p',count>1?count+' 组相似照片 · 请核对':'待确认'),existing,input,submit);
      if(count>1){const detail=el('details'),summary=el('summary','核对全部照片'),photos=el('div',undefined,{class:'candidate-photo-strip'});for(const member of candidate.members)photos.append(el('img',undefined,{alt:'这一组的人脸照片',src:`/api/people/live/candidates/${encodeURIComponent(member)}/portrait`,loading:'lazy'}));const split=el('button','不是同一人，分开',{type:'button'});split.onclick=()=>void mutate('candidate-group',{members:card.groupData.members,separate:true});detail.append(summary,photos,split);card.append(detail);}
      const choiceLabel=el('label',undefined,{class:'candidate-choice'}),choose=el('input',undefined,{type:'checkbox','aria-label':'选择这组人脸合并'});choose.onchange=()=>{if(choose.checked)selectedGroups.add(candidate.id);else selectedGroups.delete(candidate.id);updateGrouping();};choiceLabel.append(choose,el('span','选择合并'));card.append(choiceLabel);
      card.groupSelect=choose;
      card.onsubmit=event=>{event.preventDefault();const group=card.groupData;if(!busy&&input.value.trim())void mutate('name-candidate',{expected_revision:data.revision,candidate:candidate.id,name:input.value.trim(),...(group.members?{members:group.members,group_revision:group.group_revision}:{}),...(existing.value?{person_id:existing.value}:{})});};
      candidateNodes.set(candidate.id,card);candidates.append(card);
    }
    updateGrouping();
    const focusedPerson=[...forgetButtons].find(([,button])=>button===document.activeElement)?.[0];
    forgetButtons.clear();profiles.replaceChildren();
    if(!data.profiles.length)profiles.append(el('p','尚未登记人物。',{class:'live-caption'}));
    for(const p of data.profiles){
      const sightings=data.observations.filter(o=>o.person_id===p.person_id),active=sightings.filter(o=>o.present);
      const rooms=[...new Set(active.map(o=>room(o.camera)))];
      const latest=sightings[0],place=rooms.length>1?`同时出现在 ${rooms.join('、')} · 待核对`:rooms.length?rooms[0]:latest?`最近在 ${room(latest.camera)} · ${new Date(latest.last_seen*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}`:'尚无房间记录';
      const row=el('div',undefined,{class:'live-profile-row'}),text=el('div');text.append(el('strong',p.name),el('p',p.enrolled?place:'名字已保存 · 尚未登记人脸',{class:'live-caption'}));
      if(p.portrait)row.append(el('img',undefined,{class:'live-portrait',alt:p.name+'的登记人脸',src:`/api/people/live/${encodeURIComponent(p.person_id)}/portrait?v=${p.updated}`,loading:'lazy'}));
      row.append(text);
      if(p.enrolled&&!readOnly){const forget=el('button','删除人脸登记',{type:'button'});forget.disabled=busy;forget.onclick=()=>confirmForget(p);forgetButtons.set(p.person_id,forget);row.append(forget);}
      profiles.append(row);
    }
    if(focusedPerson)forgetButtons.get(focusedPerson)?.focus();
  }
  async function refresh(){
    if(closed||busy)return;
    try{const r=await fetch('/api/people/live',{cache:'no-store'});if(!r.ok)throw Error();data=await r.json();imageCamera=null;render();}
    catch{status.textContent='人物识别暂不可用，请检查摄像头连接';}
  }
  async function mutate(path,body){
    if(busy||readOnly)return;busy=true;save.disabled=true;for(const card of candidateNodes.values())card.querySelector('button').disabled=true;
    try{const response=await fetch(`/api/people/live/${path}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),result=await response.json();
      if(!response.ok){notice.textContent=messages[result.detail?.code]||'登记未完成，请刷新后重试。';return;}
      data=result;notice.textContent=path==='forget'?'人脸登记已删除。':path==='candidate-group'?'人脸已重新归组，可撤销。':path==='candidate-group/undo'?'已撤销归组。':'人脸已登记。';selectedGroups.clear();onSaved();
    }catch{notice.textContent='连接中断，请核对登记状态。';}finally{busy=false;for(const card of candidateNodes.values())card.querySelector('button').disabled=readOnly;await refresh();}
  }
  control.onclick=()=>{showLive=!showLive;imageCamera=null;render();};
  cameraSelect.onchange=()=>{choice=null;imageCamera=null;render();};
  form.addEventListener('submit',event=>{event.preventDefault();if(!choice||!name.value.trim()||!data)return;void mutate('register',{expected_revision:data.revision,...choice,name:name.value.trim(),...(identity.value?{person_id:identity.value}:{})});});
  void refresh();timer=setInterval(refresh,1800);
  return {destroy(){closed=true;clearInterval(timer);removalDialog?.close();removalDialog?.remove();removalDialog=null;forgetButtons.clear();root.replaceChildren();}};
}
