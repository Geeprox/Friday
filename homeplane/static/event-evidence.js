const element=(tag,text,attrs={})=>{
  const node=document.createElement(tag);
  if(text!=null)node.textContent=text;
  for(const[key,value]of Object.entries(attrs))node.setAttribute(key,value);
  return node;
};
const append=(node,...children)=>{children.filter(Boolean).forEach(child=>node.append(child));return node;};
const button=(text,run,attrs={})=>{const node=element('button',text,{type:'button',...attrs});node.addEventListener('click',run);return node;};
const stateValue=value=>({on:'开启',off:'关闭',open:'打开',closed:'关闭',locked:'已上锁',unlocked:'未上锁',
  cool:'制冷',heat:'制热',auto:'自动',heat_cool:'自动冷暖',dry:'除湿',fan_only:'送风',idle:'待机',playing:'正在播放',paused:'已暂停'}[value]??String(value??'未知'));
const stamp=value=>{const date=new Date(value);return Number.isFinite(date.getTime())?date.toLocaleString(): '时间未知';};
const summary=row=>{
  const data=row.summary||{};
  if(Object.hasOwn(data,'from')&&Object.hasOwn(data,'to'))return `${stateValue(data.from)} → ${stateValue(data.to)}`;
  return row.status_label||({healthy:'数据正常',stale:'数据已过期',unknown:'数据待核对',suspect:'数据需核对',faulty:'数据异常'}[data.quality])||row.title;
};
const errors={
  evidence_context_changed:'连接、权限或设备配置已变化，请刷新事件。',
  evidence_projection_expired:'这份事件视图已过期，请刷新后重新选择。',
  evidence_reference_unavailable:'原记录已不可用，请刷新事件。',
  evidence_reference_changed:'原记录已变化，请刷新后重新选择。',
  evidence_choose_device:'这个房间的设备较多，请先选择一个设备。',
  evidence_selection_unavailable:'所选房间或设备已变化，请刷新事件。',
};

export function mountEventEvidence(root,{api,isAlive=()=>true,go=()=>{},onOpenRecording=()=>{}}={}){
  if(typeof api!=='function')throw new TypeError('event_evidence_api_required');
  if(!document.getElementById('event-evidence-style')){
    const link=element('link',null,{id:'event-evidence-style',rel:'stylesheet',href:'/static/event-evidence.css'});
    (document.head||root).append(link);
  }
  let alive=true,version=0,detailVersion=0,targetVersion=0,context=null,projection=null,selectedId=null,targetTimer=null;
  const current=()=>alive&&isAlive();
  const panel=element('section',null,{class:'event-evidence','aria-label':'家庭事件与证据'});
  const heading=append(element('div',null,{class:'evidence-heading'}),append(element('div'),element('h2','家庭事件'),element('p','从状态变化查看相关操作、规则与录像。',{class:'muted'})));
  const refresh=button('刷新事件',()=>void bootstrap(),{class:'secondary'});heading.append(refresh);
  const simulated=element('p','',{class:'evidence-simulation',hidden:''});
  const filters=element('form',null,{class:'evidence-filters'});
  const room=element('select',null,{'aria-label':'事件房间'}),target=element('select',null,{'aria-label':'事件设备'});
  const search=element('input',null,{type:'search',placeholder:'筛选设备名称','aria-label':'筛选事件设备',maxlength:'128'});
  const hours=element('select',null,{'aria-label':'事件时间范围'});
  for(const[value,label]of [['24','最近 24 小时'],['6','最近 6 小时'],['1','最近 1 小时']])hours.append(element('option',label,{value}));
  const field=(label,node)=>append(element('label'),element('span',label),node);
  append(filters,field('房间',room),field('设备',target),field('筛选设备',search),field('时间',hours));
  filters.addEventListener('submit',event=>event.preventDefault());
  const status=element('p','正在读取事件…',{class:'evidence-status',role:'status','aria-live':'polite'});
  const coverage=element('details',null,{class:'evidence-coverage',hidden:''});
  const list=element('div',null,{class:'evidence-list','aria-label':'家庭事件列表'}),detail=element('section',null,{class:'evidence-detail','aria-label':'事件证据详情'});
  const layout=append(element('div',null,{class:'evidence-layout'}),list,detail);
  const boundary=element('p','这里只展示本机保留的记录。未记录的外部规则和手动操作可能不可见。',{class:'caption'});
  append(panel,heading,simulated,filters,status,coverage,layout,boundary);root.append(panel);

  function stopPlayback(){try{const pending=onOpenRecording(null);pending?.catch?.(()=>{});}catch(_){} }
  function clearSelection(){detailVersion++;selectedId=null;detail.replaceChildren();stopPlayback();}
  function failure(error){
    projection=null;clearSelection();list.replaceChildren();coverage.hidden=true;coverage.replaceChildren();
    status.textContent=errors[error?.message]||'事件暂时无法读取，请刷新后重试。';
    status.dataset.error='true';
  }
  function options(node,entries,placeholder){
    node.replaceChildren(element('option',placeholder,{value:''}));
    for(const item of entries)node.append(element('option',item.name,{value:item.id}));
  }
  function roomInfo(){return context?.rooms.find(item=>item.id===room.value);}
  async function loadTargets({queryAfter=false}={}){
    const token=++targetVersion,roomId=room.value,previous=target.value;
    if(!roomId||!context)return;
    const params=new URLSearchParams({expected_context:context.fingerprint,room:roomId,search:search.value});
    try{
      const result=await api('/api/home/evidence/targets?'+params);
      if(!current()||token!==targetVersion||room.value!==roomId)return;
      options(target,result.entities,search.value||roomInfo()?.requires_entity?'选择一个设备':'房间全部设备');
      if(result.entities.some(item=>item.id===previous))target.value=previous;
      if(queryAfter){
        if(target.value||!search.value&&!roomInfo()?.requires_entity)await load();
        else status.textContent=result.entities.length?'请选择设备查看事件。':'没有匹配的设备，可修改筛选名称。';
      }else if(result.partial)status.textContent='设备较多，可输入名称缩小选择。';
    }catch(error){if(current()&&token===targetVersion)failure(error);}
  }
  async function load(){
    const token=++version;targetVersion++;clearSelection();projection=null;list.replaceChildren();
    coverage.hidden=true;coverage.replaceChildren();
    if(!context||!room.value)return;
    if(roomInfo()?.requires_entity&&!target.value){status.textContent='这个房间的设备较多，请先选择一个设备。';return;}
    status.textContent='正在读取所选范围的事件…';status.dataset.error='false';refresh.disabled=true;
    try{
      const result=await api('/api/home/evidence/query',{expected_context:context.fingerprint,room:room.value,
        ...(target.value?{entity_id:target.value}:{}),window_seconds:Number(hours.value)*3600,limit:30});
      if(!current()||token!==version)return;
      projection=result;
      const gaps=Array.isArray(result.coverage_gaps)?result.coverage_gaps:[];
      if(gaps.length){
        coverage.hidden=false;coverage.append(element('summary',`家庭连接记录有 ${gaps.length} 处观测缺口`));
        for(const gap of gaps)coverage.append(element('p',`${stamp(gap.event_time)} · ${gap.caption}`,{class:'caption'}));
      }
      for(const row of result.items){
        const item=button(null,()=>void select(row),{class:'evidence-event','data-event-id':row.id});
        append(item,element('time',stamp(row.event_time),{datetime:row.event_time}),
          element('strong',row.entity_names.join('、')||row.title),element('span',summary(row)),
          element('span',row.source_binding==='unverified'?'历史来源待核对':row.title,{class:'caption'}));list.append(item);
      }
      if(!result.items.length){
        status.textContent='这个范围没有可展示的事件。可更换房间、设备或时间范围。';
        detail.append(element('p','没有记录不代表没有发生事件。',{class:'muted'}));
      }else{
        status.textContent=`${result.simulated?'simulated · 示例事件 · ':''}显示最近 ${result.items.length} 条${result.older_items_omitted?' · 更早记录已省略':''}`;
        detail.append(element('p','选择一个事件，查看来源记录和相关证据。',{class:'muted'}));
      }
    }catch(error){if(current()&&token===version)failure(error);}
    finally{if(current()&&token===version)refresh.disabled=false;}
  }
  async function select(row){
    if(!projection)return;
    const token=++detailVersion,saved=projection;selectedId=row.id;stopPlayback();
    for(const item of list.children)item.setAttribute('aria-pressed',String(item.getAttribute('data-event-id')===selectedId));
    detail.replaceChildren(element('p','正在核对事件证据…',{role:'status'}));
    try{
      const report=await api('/api/home/evidence/detail',{expected_context:saved.context_fingerprint,projection_id:saved.projection_id,event_id:row.id});
      if(!current()||token!==detailVersion||saved!==projection)return;
      drawDetail(report,token);
    }catch(error){if(current()&&token===detailVersion){stopPlayback();detail.replaceChildren(element('p',errors[error?.message]||'证据暂时无法核对，请刷新事件。',{role:'status'}));}}
  }
  function drawDetail(report,token){
    const row=report.event;
    detail.replaceChildren(element('h3',row.entity_names.join('、')||row.title),element('p',`${stamp(row.event_time)} · ${row.title}`),element('p',summary(row),{class:'evidence-change'}));
    const facts=element('dl',null,{class:'evidence-facts'});
    for(const[label,value]of [['记录来源',row.source],['记录归属',report.source_binding_caption],['时间依据',({upstream_event:'上游事件时间',local_event:'本机记录时间',unknown:'时间依据未知'}[row.event_time_basis])],['物理效果','未独立确认']])append(facts,element('dt',label),element('dd',value));
    detail.append(facts,element('h4','为什么发生'),element('p',report.explanation.caption));
    if(report.recorded_cause)detail.append(element('p',`来源引用：${stamp(report.recorded_cause.event_time)} · ${report.recorded_cause.entity_names.join('、')} · ${summary(report.recorded_cause)}`,{class:'caption'}));
    else if(row.causation_ref)detail.append(element('p','来源引用暂不可核验，或不在本次范围内。',{class:'caption'}));
    if(report.recorded_trigger)detail.append(element('p',`运行记录引用的触发事件：${stamp(report.recorded_trigger.event_time)} · ${report.recorded_trigger.entity_names.join('、')} · ${summary(report.recorded_trigger)}`,{class:'caption'}));
    if(report.action){
      const requested=report.action.requested||{},desired=Object.values(requested).map(stateValue).join('、');
      detail.append(element('h4','原操作记录'),element('p',`${report.action.actor_label} · ${report.action.target_name}`),
        element('p',`请求：${desired||'参数待核对'} · ${report.action.status_label||'结果待核对'}`),element('p',report.action.readback_caption,{class:'caption'}));
    }
    if(report.rules.length){
      detail.append(element('h4','关联规则'));
      for(const rule of report.rules)detail.append(append(element('div',null,{class:'evidence-rule'}),element('strong',rule.name),element('span',`${rule.enabled?'当前已启用':'当前已停用'} · ${rule.status_label||'运行状态待核对'}`),button('查看自动化',()=>go('Automations'),{class:'secondary'})));
      detail.append(element('p','修改或暂停规则需要进入自动化页面核对；这里不会执行控制。',{class:'caption'}));
    }
    if(report.correlated_events.length){
      detail.append(element('h4','同组记录'));
      for(const item of report.correlated_events)detail.append(element('p',`${stamp(item.event_time)} · ${summary(item)}`));
      detail.append(element('p',report.correlation_caption,{class:'caption'}));
    }
    detail.append(element('h4','附近录像'));
    if(report.recordings.length){
      for(const lookup of report.recordings){
        const open=button(`查看 ${lookup.camera_name} 隐私录像`,async()=>{
          if(!current()||token!==detailVersion)return;
          open.disabled=true;
          try{await onOpenRecording(lookup);}
          catch(_){if(current()&&token===detailVersion)playbackStatus.textContent='录像预览暂不可用，请检查隐私处理和录像连接。';}
          finally{if(current()&&token===detailVersion)open.disabled=false;}
        },{class:'secondary'});detail.append(open);
      }
      detail.append(element('p',report.recording_caption,{class:'caption'}));
    }else{
      detail.append(element('p',report.recording_code==='event_source_unverified'?'历史事件的来源尚未核对，不能关联当前家庭录像。':report.recording_code==='camera_room_binding_unreviewed'?'摄像头与当前家庭房间尚未重新核对，请在连接设置中确认。':report.recording_code==='event_clock_unknown'?'事件时间依据未知，无法可靠定位录像。':'当前没有经过核对的房间与录像时钟映射，可在连接设置中核对。',{class:'caption'}));
      detail.append(button('检查录像连接',()=>go('Settings'),{class:'text-button'}));
    }
    const playbackStatus=element('p','',{role:'status','aria-live':'polite'});detail.append(playbackStatus);
  }
  async function bootstrap(){
    const token=++version;targetVersion++;projection=null;clearSelection();list.replaceChildren();
    coverage.hidden=true;coverage.replaceChildren();
    const previousRoom=room.value,previousTarget=target.value;
    status.textContent='正在读取家庭事件范围…';refresh.disabled=true;
    try{
      const report=await api('/api/home/evidence/context');
      if(!current()||token!==version)return;
      context=report;
      simulated.hidden=!report.simulated;simulated.textContent='simulated · 合成示例，不控制真实设备';
      options(room,report.rooms,'选择房间');
      room.value=report.rooms.some(item=>item.id===previousRoom)?previousRoom:report.rooms[0]?.id||'';
      options(target,roomInfo()?.entities||[],roomInfo()?.requires_entity?'选择一个设备':'房间全部设备');
      if(roomInfo()?.entities.some(item=>item.id===previousTarget))target.value=previousTarget;
      if(!room.value){status.textContent='当前没有可查询的房间。请先检查家庭连接与房间登记。';return;}
      await load();
    }catch(error){if(current()&&token===version)failure(error);}
    finally{if(current())refresh.disabled=false;}
  }
  room.addEventListener('change',()=>{search.value='';options(target,roomInfo()?.entities||[],roomInfo()?.requires_entity?'选择一个设备':'房间全部设备');void load();void loadTargets();});
  target.addEventListener('change',()=>void load());hours.addEventListener('change',()=>void load());
  search.addEventListener('input',()=>{
    clearTimeout(targetTimer);version++;targetVersion++;projection=null;clearSelection();list.replaceChildren();
    coverage.hidden=true;coverage.replaceChildren();
    refresh.disabled=false;status.dataset.error='false';status.textContent='正在筛选设备…';
    targetTimer=setTimeout(()=>void loadTargets({queryAfter:true}),250);
  });
  void bootstrap();
  return {refresh:bootstrap,dispose(){alive=false;version++;detailVersion++;targetVersion++;clearTimeout(targetTimer);stopPlayback();panel.remove();}};
}
