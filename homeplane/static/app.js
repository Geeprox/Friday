'use strict';
const copyName = globalThis.FridayCopyLayout.name, withCopy = globalThis.FridayCopyLayout.withCopy;
const copyLayout = globalThis.FridayCopyLayout;
// Provider strings are always text nodes; household content is never interpreted as HTML.
const $ = id => document.getElementById(id);
const el = (tag, text, attrs = {}) => {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return copyLayout.decorate(node,text,attrs);
};
const add = (parent, ...children) => { children.forEach(child => parent.append(child)); return parent; };
const icon = name => el('span', null, {class: 'icon icon-' + name, 'aria-hidden': 'true'});
const badge = (text, quality = '') => el('span', text, {class: `badge ${quality}`});
const json = value => el('pre', JSON.stringify(value, null, 2));
const detail = (title, value) => add(el('details', null, {class:'details'}), el('summary',title), copyLayout.describeValue(json(value),value));
const note = (text, error = false) => el('div', text, {class:'notice' + (error ? ' error' : '')});
const time = value => value ? new Date(value).toLocaleString('zh-CN') : '时间未知';
const shortTime = value => value ? new Date(value).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'}) : '时间未知';
const display = value => value === null || value === undefined ? '未知' : typeof value === 'object' ? JSON.stringify(value) : String(value);
const qualityNames = {healthy:'有效',stale:'读数已过期',suspect:'需要核对',unknown:'暂不可确认',faulty:'无法读取',planned:'已生成预览',rejected:'无法生成预览',expired:'预览已过期',sent:'发送已记录',accepted:'HA 已接受',reported_match:copyName('Friday-0968'),unknown_outcome:'操作结果待核对',failed:'操作未完成'};
const qualityExplanation = reason => ({
  transport_gap:'连接中断，状态需要重新确认。', source_unavailable:'数据来源不可用。', upstream_unavailable:'上游数据来源不可用。',
  latched_state_observed_not_new_transition:'这是已有状态，未记录新的变化。', latched_state_transport_unknown:'尚未确认最近的连接状态。',
  measurement_time_unknown:'设备测量时间未知。', hardware_sample_time_unknown:'设备测量时间未知。',
  integration_write_is_not_hardware_sample:'来源更新时间不代表设备测量时间。', refresh_returned_cache:'数据来源返回了缓存。',ha_cached_state:'来源返回了缓存。',
  measurement_clock_uncertain:'设备时间需要核对。',measurement_in_future:'设备时间超前，需要核对。',measurement_expired:'读数已过期。',
  snapshot_validity_expired:'读数已过期，请刷新。',device_sample_within_validity:'测量时间仍在有效期内。',no_valid_independent_source:'暂无有效的替代来源。',
  recovery_observation_window_30_seconds:'连接刚恢复，正在确认状态。',provisional_identity_registry_unavailable:'设备身份需要核对。',
  recovery_requires_distinct_samples_and_window:'正在观察恢复，需要跨过观察期的新测量。',
  frozen_under_change:'温度不变而其他来源变化，暂停用于室温。温度长期未变，而独立来源持续变化；此来源暂不用于室温判断。',
  peer_disagreement_unresolved:'来源读数不一致，暂时无法判断室温。',
  no_valid_qualified_measurement_source:'没有可用且经过校准的室温来源。',
  fallback_calibration_required:'空调回风温度尚未校准，不能替代室温。',
  calibration_scope_expired_or_changed:'校准已过期或位置、模式发生变化。',
  outside_calibrated_measurement_range:'读数超出了已校准范围。',
  measurement_context_unknown_or_changed:'当前工作模式未知或与测量配置不一致。',
  temporarily_excluded:'此来源已临时排除。',measurement_binding_changed:'来源或房间已变，请重新保存配置',
  measurement_group_location_changed:'主来源房间已变，请重新设置室温来源',
  measurement_group_binding_changed:'主备来源或位置已变，请重新核对',
  measurement_configuration_unavailable:'测量配置暂不可读，温度判断已暂停。',
  relative_calibrated_fallback_not_absolute_truth:'根据相对校准推算，区间不代表绝对真值。',
  primary_profile_selected:'使用已配置的室温主来源。',
  battery_reading_stale_or_unknown:'电量记录已过期，不能证明当前健康。',battery_warning:'最近电量偏低。',
  physically_implausible:'读数超出合理范围。',missing_report:'缺少测量记录。',
  quality_clock_regressed:'时钟回退，重新观察来源。',query_page_value_expired:'读数已过期，请重新查询。',
  query_state_expired_or_clock_unknown:'读数过期或来源时间需要核对。'
}[reason] ?? '需要核对来源记录。');
const reading = state => state.value === null || state.value === undefined ? '暂不可确认' : `${display(state.value)}${state.unit ? ' ' + state.unit : ''}`;
const peopleAvailable = document.body.dataset.peopleEnabled === 'true';
const filesAvailable = document.body.dataset.fileStewardEnabled === 'true';
let filesView = null, filesViewVersion = 0, filesStylesheet = null;
const filesState = {tab:'Files',query:'',selected:new Set(),destinations:new Map()};
let peopleView = null, peopleViewVersion = 0, peopleStylesheet = null;
let experienceView=null, experiencePage=null, experienceVersion=0;
let snapshot = null, page = 'Home', lastTrace = null, loading = false, connected = false;
let lastRulePlan = null;
let homeQueryDraft = {room:null,kind:'state'}, homeQueryResult = null, homeQueryRequest = null, homeQuerySource = null, homeQuerySequence = 0;
let homeConversationView=null, homeConversationSlot=null, homeConversationSession=null;
let homeConversationVersion=0, homeConversationBoot=null, homeConversationCheck=null;
let homeConversationDraft = '';
let selectedScenario = 'baseline', loadedScenario = null, settingsData = null, settingsRequest = null, haStatus = null, geekCredentialAvailable = null;
let nasConnectionDraft = null, nasConnectionDraftGeneration = null, nasConnectionRefresh = null;
let haRefreshRequest = null;
const actionDrafts = new Map();
const deviceActionDrafts = new Map();
let ruleTextDraft = '', ruleTextSession = 0, lastRuleTextResult = null;
let ruleAnalysisDraft='',ruleAnalysisRecord='',ruleAnalysisVersion=0,ruleAnalysisSession=0,ruleAnalysisReview=null,ruleAnalysisResult=null,ruleAnalysisBusy=false,ruleAnalysisExpanded=false;
let coreRuleSession = 0, lastCoreRulePlan = null, coreRuleBusy = false;
let coreRuleWindowEndsAt = null;
let geekInventorySession = 0, geekInventoryRequest = null;
const coreRuleDraft = {name:'',triggerDevice:'',trigger:'',edge:'off:on',device:'',target:'',operation:'',values:{},brightness:''};
const deviceOperationLabels = {'light.set':'开关亮度','switch.set':'电源','fan.set':'风扇电源','fan.percentage':'风速比例','climate.mode':'空调模式','climate.temperature':'目标温度','cover.position':copyName('Friday-1017'),'lock.set':'门锁状态','number.value':'设定值','select.option':'功能选项'};
let selectedActionFunctionId = null;
let selectedConnection = 'settings-ha';
let showAllStates = false;
let inspectingStateId = null;
let archiveSession=0,archiveVersion=0,archiveContext=null,archiveResult=null;
const archiveDraft={camera:'',start:'',end:''};
let archivePlayerView=null,archivePlayerVersion=0;
let archivePrepareController=null,archiveExpiryTimer=null,archiveContextTimer=null;
let archiveSetupSession=0,archiveSetupVersion=0,archiveSetupRefresh=null;
const archiveSetupDraft={camera:'',folder:'',name:'',room:'',timezone:'Asia/Shanghai',offset:'',uncertainty:'',arrival:'',retention:'',timing:false,completion:false};
let selectedRoom = '*', selectedEntityId = null, deviceQuery = '', deviceLimit = 24, activityLimit = 30;
const pages = ['Home','Devices','Cameras','Safety','Interaction','Automations','Activity','Games',...(peopleAvailable ? ['People'] : []),...(filesAvailable ? ['Files'] : []),'System','Ask Home','Settings'];
const pageInfo = {
  Cameras:['摄像头','实时查看家中画面与录像。','video'],
  Safety:['智能预警','查看疑似异常，提醒家人及时照看。','eye'],
  Interaction:['互动控制','设置手势控制，查看最近的响应。','eye'],
  Games:['游戏库','选择游戏，与家人一起动起来。','player-play'],
  Files:['家庭资料（预览）','搜索已索引的 NAS 资料，查看整理建议。','folder'],
  People:['人物','为熟悉的家人登记名字，核对识别结果。','eye'],
  Home:['我的家','在房间里查看状态，控制家中设备。','home'], Devices:['设备','按房间找到需要的设备。','devices'],
  Automations:['自动化','查看家里的日常安排。','clock'], Activity:['回看','发生了什么，记录在这里。','player-play'],
  Settings:['连接','管理家庭服务与账号。','settings'], System:['系统状态','连接、存储与智能服务。','chart-bar'],
  Developer:['调试工具','状态与来源记录。','code'], 'Ask Home':['文字指令调试','用于检查文字理解与家庭记录','cpu']
};
async function api(path, options = {}) {
  if (options.method && options.method !== 'GET' && /^\/api\/(?:settings|source|archive\/(?:cameras|simulator)|simulator\/scenario)/.test(path)) clearHomeConversation();
  const response = await fetch(path, {headers:{'Content-Type':'application/json'}, ...options});
  if (!response.ok) throw new Error(`请求失败（${response.status}），请重试。`);
  return response.json();
}
function go(name) { const changed=page!==name;if(changed){archiveVersion++;archiveSetupVersion++;stopArchivePlayer();} page = name; lastTrace = null; history.replaceState(null,'','#' + name.toLowerCase().replace(' ','-')); render(); if(changed){const heading=$('page-title');heading?.setAttribute('tabindex','-1');heading?.focus?.({preventScroll:true});globalThis.scrollTo?.({top:0,left:0,behavior:'instant'});} if (page === 'Settings') void refreshHaStatus(); }
function button(text, action, attrs = {}) { const b = el('button',text,{type:'button',...attrs}); b.addEventListener('click',action); return b; }
function navigation() {
  const nav = $('navigation'); nav.replaceChildren();
  const utility = $('utility-navigation'); if (utility) utility.replaceChildren();
  for (const name of ['Home','Devices','Cameras','Safety','Interaction','Automations','Activity','Games',...(peopleAvailable ? ['People'] : []),'Settings',...($('developer-mode').checked ? ['System','Developer'] : [])]) {
    const [label,,mark] = pageInfo[name];
    const b = add(button(null,() => go(name), {'data-page':name,'aria-label':label,...(page === name ? {'aria-current':'page'} : {})}),icon(mark),el('span',label));
    (['Ask Home','Settings','System','Developer'].includes(name) && utility ? utility : nav).append(b);
  }
}
function expireSnapshotStates() {
  if (!snapshot) return false;
  let changed = false;
  for (const state of snapshot.states) {
    if (state.quality === 'healthy' && state.valid_until && Date.parse(state.valid_until) <= Date.now()) {
      state.quality = 'stale'; state.value = null; state.reasons = [...state.reasons,'snapshot_validity_expired']; changed = true;
    }
  }
  return changed;
}
function expireHomeQueryStates() {
  let changed=false;
  for(const item of homeQueryResult?.items ?? []) {
    if(item.kind==='state'&&item.quality==='healthy'&&item.valid_until&&Date.parse(item.valid_until)<=Date.now()) {
      item.quality='stale';item.value=null;item.value_interval=null;item.reasons=[...item.reasons,'query_page_value_expired'];changed=true;
    }
  }
  return changed;
}
document.addEventListener('Friday.homeStatus',event=>{
 const status=event.detail;if(!status||!status.generated_at||!Number.isFinite(Date.parse(status.generated_at)))return;
 $('mode').textContent=status.simulated?'示例家庭':status.connected?(status.control_count?'家庭已连接':'家庭已连接 · 只读'):'连接中断';
 $('snapshot-time').textContent='更新于'+shortTime(status.generated_at);
});
function renderSourceStatus() {
  $('mode').textContent = !snapshot ? '未连接' : !connected ? '连接中断' : snapshot.mode === 'simulator' ? '示例家庭' : snapshot.action_targets?.length ? '家庭已连接' : '家庭已连接 · 只读';
  $('snapshot-time').textContent = snapshot ? '更新于' + shortTime(snapshot.generated_at) : '';
  const source = $('settings-source-result');
  if (page === 'Settings' && source && source.getAttribute?.('aria-busy') !== 'true') settingsMessage(source,
    !snapshot ? '未连接家庭数据' : !connected ? '连接中断 · 显示上次记录' : snapshot.mode === 'simulator' ? '当前使用：示例家庭' : snapshot.action_targets?.length ? '通过Home Assistant操作设备。当前使用：Home Assistant · 设备受控' : '通过Home Assistant只读状态。当前使用：Home Assistant · 只读');
  const pause = $('settings-actions-pause');
  if (page === 'Settings' && pause) pause.disabled = !snapshot?.action_targets?.length;
  const ha = $('settings-ha-result');
  if (page === 'Settings' && ha && ha.getAttribute?.('aria-busy') !== 'true') showHaStatus(ha);
}
function loadPeopleStyles() {
  const link = el('link', null, {rel:'stylesheet', href:'/people-static/people.css', id:'friday-people-styles'});
  const ready = new Promise((resolve, reject) => {
    link.addEventListener('load', resolve, {once:true});
    link.addEventListener('error', () => reject(new Error('people_styles_unavailable')), {once:true});
  });
  peopleStylesheet = link;
  document.head.append(link);
  return ready;
}
function clearPeopleView() {
  peopleView?.destroy(); peopleView = null;
  peopleStylesheet?.remove(); peopleStylesheet = null;
}
function loadFilesStyles() {
  const link = el('link', null, {rel:'stylesheet',href:'/static/file-steward.css'});
  const ready = new Promise((resolve,reject) => {
    link.addEventListener('load',resolve,{once:true});
    link.addEventListener('error',reject,{once:true});
  });
  filesStylesheet=link;document.head.append(link);return ready;
}
function clearFilesView() {
  filesView?.destroy();filesView=null;filesStylesheet?.remove();filesStylesheet=null;
}
function retainHomeConversationDraft() {
  const text = homeConversationView?.getDraft();
  if (typeof text === 'string') homeConversationDraft = text;
}
function clearHomeConversation() {
  retainHomeConversationDraft();
  ++homeConversationVersion;
  homeConversationCheck?.controller.abort();homeConversationCheck=null;
  homeConversationBoot?.abort();homeConversationBoot=null;
  const session=homeConversationSession;
  homeConversationView?.dispose();homeConversationView=null;homeConversationSession=null;
  homeConversationSlot?.remove();homeConversationSlot=null;
  if(session) void api('/api/home-flow/dispose',{method:'POST',body:JSON.stringify({session_id:session}),keepalive:true}).catch(()=>{});
}
async function checkHomeConversation() {
  if(homeConversationCheck || !homeConversationSession || !homeConversationSlot?.isConnected)return;
  const session=homeConversationSession, version=homeConversationVersion;
  const controller=new AbortController();
  const ownedCheck={controller,promise:api('/api/home-flow/context',{method:'POST',body:JSON.stringify({session_id:session}),signal:controller.signal})};
  homeConversationCheck=ownedCheck;
  try {
    const response=await ownedCheck.promise;
    if(version!==homeConversationVersion || session!==homeConversationSession)return;
    if(response.code!=='flow_current') {
      retainHomeConversationDraft();
      homeConversationView?.update({sourceToken:null,recovery:'expired'});
      homeConversationSession=null;
      void api('/api/home-flow/dispose',{method:'POST',body:JSON.stringify({session_id:session})}).catch(()=>{});
    }
  } catch (_) {
    if(version===homeConversationVersion && session===homeConversationSession) {
      retainHomeConversationDraft();
      homeConversationView?.update({sourceToken:null,recovery:'unknown'});
      homeConversationSession=null;
      void api('/api/home-flow/dispose',{method:'POST',body:JSON.stringify({session_id:session})}).catch(()=>{});
    }
  } finally {if(homeConversationCheck===ownedCheck)homeConversationCheck=null;}
}
function mountHomeConversation(content) {
  const slot=el('section',null,{class:'surface',id:'home-conversation-slot'});
  homeConversationSlot=slot;content.append(slot);
  const status=el('p','正在开始对话，原文保留',{role:'status'});
  const client={
    send:(request,{signal}={})=>api('/api/home-flow/turn',{method:'POST',body:JSON.stringify(request),signal}),
    cancel:(request,{signal}={})=>api('/api/home-flow/cancel',{method:'POST',body:JSON.stringify(request),signal}),
  };
  const begin=async()=>{
    const old=homeConversationSession;
    retainHomeConversationDraft();
    homeConversationView?.update({sourceToken:null,recovery:'connecting'});
    homeConversationSession=null;
    if(old)void api('/api/home-flow/dispose',{method:'POST',body:JSON.stringify({session_id:old})}).catch(()=>{});
    homeConversationCheck?.controller.abort();homeConversationCheck=null;
    homeConversationBoot?.abort();
    const controller=new AbortController(),version=++homeConversationVersion;
    homeConversationBoot=controller;restart.disabled=true;
    status.textContent='正在开始对话，原文保留';
    try {
      const {HomeConversation}=await import('/static/home-conversation.js');
      if(version!==homeConversationVersion||controller.signal.aborted||page!=='Ask Home'||slot!==homeConversationSlot||!slot.isConnected)return;
      if(!homeConversationView)homeConversationView=HomeConversation.mount(slot,{sourceToken:null,client,initialDraft:homeConversationDraft,recovery:'connecting'});
      status.hidden=true;
      const response=await api('/api/home-flow/bootstrap',{method:'POST',signal:controller.signal});
      if(version!==homeConversationVersion || page!=='Ask Home' || slot!==homeConversationSlot || !slot.isConnected || controller.signal.aborted) {
        if(response?.session_id)void api('/api/home-flow/dispose',{method:'POST',body:JSON.stringify({session_id:response.session_id})}).catch(()=>{});
        return;
      }
      if(response.code!=='flow_ready'||response.phase!=='result')throw new Error('flow_bootstrap_unavailable');
      retainHomeConversationDraft();
      homeConversationSession=response.session_id;
      homeConversationView.update({sourceToken:response.session_id,response});
    } catch (_) {
      if(version===homeConversationVersion&&!controller.signal.aborted&&slot===homeConversationSlot&&slot.isConnected) {
        retainHomeConversationDraft();
        homeConversationView?.update({sourceToken:null,recovery:'unknown'});
        status.textContent='会话未确认，原文仍保留';
      }
    } finally {
      if(version===homeConversationVersion){homeConversationBoot=null;restart.disabled=false;}
    }
  };
  const restart=button('重新开始',()=>void begin(),{'data-home-flow':'bootstrap'});
  slot.append(status,restart);void begin();
}
let gamesView = null, gamesViewVersion=0;
function render() {
  if(experienceView&&experiencePage===page){renderSourceStatus();return;}
  experienceView?.dispose();experienceView=null;experiencePage=null;const experienceCurrent=++experienceVersion;
  if(page==='Games'&&gamesView)return;
  gamesView?.dispose();gamesView=null;++gamesViewVersion;
  if(page!=='Ask Home'&&(homeConversationSlot||homeConversationBoot))clearHomeConversation();
  if(page!=='Activity'&&archivePlayerView)stopArchivePlayer();
  expireSnapshotStates(); navigation();
  $('page-title').textContent = page === 'Home' ? '我的家' : pageInfo[page][0];
  document.title = 'Friday · ' + $('page-title').textContent;
  copyLayout.decorate($('page-title'),page === 'Home' ? '我的家' : pageInfo[page][0]);
  if ($('page-subtitle')) $('page-subtitle').textContent = pageInfo[page][1];
  $('content').setAttribute('aria-label',pageInfo[page][0]);
  const content = $('content'),activityEvents=$('activity-events');
  if(page==='Ask Home'&&homeConversationSlot?.isConnected) {
    renderSourceStatus();void checkHomeConversation();return;
  }
  if(page==='Files'&&filesView){renderSourceStatus();return;}
  filesViewVersion++;clearFilesView();
  if(page==='Activity'&&archivePlayerView&&snapshot&&activityEvents?.isConnected&&$('archive-lookup')?.isConnected){
    activityEvents.replaceChildren();if(!connected)activityEvents.append(note('连接中断，显示上次记录。请刷新后重试。',true));renderEventList(activityEvents,activityLimit);if(lastTrace)renderTrace(activityEvents,lastTrace);
    renderSourceStatus();
    const view=archivePlayerView;
    api('/api/archive/context').then(context=>{if(archivePlayerView!==view||page!=='Activity')return;if(context.fingerprint!==view.plan.context_fingerprint||context.simulated!==view.plan.simulated){archiveResult=null;archiveVersion++;stopArchivePlayer();render();}}).catch(()=>{if(archivePlayerView===view)stopArchivePlayer('归档状态暂不可核对，请重新查询。');});
    return;
  }
  peopleViewVersion++;
  clearPeopleView();
  content.replaceChildren();
  renderSourceStatus();
  if(page==='Safety'){
    experiencePage=page;
    import('/static/safety.js').then(({mountSafety})=>{if(experienceCurrent===experienceVersion&&experiencePage===page)experienceView=mountSafety(content,{go});}).catch(()=>{if(page==='Safety')content.append(note('预警页面暂不可用，请刷新。',true));});return;
  }
  if(page==='Games'){
    const version=gamesViewVersion;
    import('/static/games-library.js').then(({mountGames})=>{if(version===gamesViewVersion&&page==='Games')gamesView=mountGames(content,{go});}).catch(()=>{if(page==='Games')content.append(note('游戏库暂不可用，请刷新。',true));});return;
  }
  if(page==='Files'&&filesAvailable){
    const version=filesViewVersion;
    Promise.all([import('/static/file-steward.js'),loadFilesStyles()]).then(([{mountFiles}])=>{
      if(version!==filesViewVersion||page!=='Files')return;
      filesView=mountFiles(content,{state:filesState,readOnly:document.body.dataset.consoleReadOnly==='true',onOpenConnections:()=>{selectedConnection='settings-nas';go('Settings');}});
    }).catch(()=>{if(version===filesViewVersion&&page==='Files')content.append(note('文件页面暂时无法读取。',true));});
    return;
  }
  if (page === 'People' && peopleAvailable) {
    const version = peopleViewVersion;
    Promise.all([import('/people-static/people.js'), loadPeopleStyles()]).then(([{mountPeople}]) => {
      if (version !== peopleViewVersion || page !== 'People') return;
      peopleView = mountPeople(content, {endpoint:'/api/people', embedded:true,readOnly:document.body.dataset.consoleReadOnly==='true'});
    }).catch(() => {
      if (version === peopleViewVersion && page === 'People') content.append(note('人物页面暂时无法读取。', true));
    });
    return;
  }
  if(document.body.dataset.consoleSurface!=='legacy'&&['Home','Devices','Cameras','Interaction','Activity','Automations','Settings'].includes(page)&&(snapshot||page==='Settings')){
    experiencePage=page;
    import('/static/home-experience.js').then(({mountExperience})=>{if(experienceCurrent!==experienceVersion||experiencePage!==page)return;experienceView=mountExperience(content,{page,go,snapshot,legacyConnections:(slot,target)=>{if(target)selectedConnection=target;renderSettings(slot);},legacyRules:renderCoreRules,openEvidenceRecording:openEventRecording});}).catch(()=>{
      if(experienceCurrent!==experienceVersion||experiencePage!==page)return;
      experiencePage=null;
      // A missing optional view must not remove the basic Console's controls.
      const fallback={Home:renderHome,Devices:renderDevices,Activity:renderActivity,Automations:renderAutomations,Settings:renderSettings}[page];
      content.replaceChildren();
      if(fallback)fallback(content);else content.append(note('家庭界面暂不可用，请刷新。',true));
    });return;
  }
  if (!snapshot) {
    if (page === 'Settings') return renderSettings(content);
    return empty(content,'正在回到家中','正在同步房间和设备…');
  }
  if (!connected) content.append(note('连接中断，显示上次记录。请刷新后重试。',true));
  ({Home:renderHome, Devices:renderDevices, Activity:renderActivity, Automations:renderAutomations, System:renderSystem, Developer:renderDeveloper, Settings:renderSettings, 'Ask Home':renderAsk})[page](content);
}
function empty(content,title,description,action,handler) {
  const block = add(el('div',null,{class:'empty-state'}),el('h2',title),el('p',description,{class:'muted'}));
  if (action) block.append(button(action,handler,{class:'primary'}));
  content.append(block);
}
function statesFor(entity) { return snapshot.states.filter(s => sMatches(s,entity)); }
function primaryStateFor(entity) {
  if (!entity.function_ids) return statesFor(entity)[0];
  // The device's main function determines its summary. An auxiliary reading
  // cannot hide an unavailable actuator or make a healthy actuator look offline.
  const functions = entity.function_ids.map(id => snapshot.entities.find(e => e.id === id)).filter(Boolean);
  const primary = functions.find(e => e.kind === entity.kind) ?? functions[0];
  if (!primary) return undefined;
  return snapshot.states.find(s => s.id === primary.id + ':' + primary.kind + '.state') ?? statesFor(primary)[0];
}
function inventoryEntities() { return snapshot.devices?.length ? snapshot.devices.filter(d => d.category !== 'service') : snapshot.entities; }
function actionEntity(entity) {
  const functions = controllableFunctions(entity);
  return functions.find(e=>e.id===selectedActionFunctionId)??functions[0]??null;
}
function controllableFunctions(entity){return (entity.function_ids?snapshot.entities.filter(e=>entity.function_ids.includes(e.id)):[entity]).filter(e=>e.capabilities.some(c=>deviceOperationLabels[c.name]&&c.access==='write'&&c.support==='supported'));}
function entityName(state) { return entityLabel(snapshot.entities.find(e => state.id === e.id || state.id.startsWith(e.id + ':')) ?? {name:state.id}); }
function entityLabel(entity) { return ({'Hall light':'玄关灯','Living thermometer':'客厅温度计','Living air conditioner':'客厅空调','Front door':'入户门'})[entity.name] ?? entity.name; }
function roomKey(entity) { return entity.room ?? ''; }
function roomName(value) { value=(value??'').replace(/^/,'');return ({hall:'玄关','living-room':'客厅',Living:'客厅'})[value] ?? (value || '未分配房间'); }
function deviceIcon(entity) {
  const id = entity.id ?? '';
  if (entity.capabilities?.some(c => c.name === 'light.set') || id.startsWith('light.')) return 'bulb';
  if (entity.kind === 'climate' || id.startsWith('climate.')) return 'air-conditioning';
  if (entity.kind === 'camera') return 'video';
  if (id.includes('door') || id.includes('contact')) return 'door';
  if (id.includes('temperature') || entity.capabilities?.some(c => c.name === 'temperature')) return 'temperature';
  return 'devices';
}
function stateValue(state, hasEvidence = connected) {
  if (!hasEvidence || !state) return '暂不可确认';
  if (state.value_interval?.length === 2) {
    const interval = state.value_interval.map(v => Number(v.toFixed(2))).join('–') + (state.unit ? ' ' + state.unit : '');
    if (state.quality === 'unknown' && state.reasons.includes('peer_disagreement_unresolved')) return interval + ' · 读数不一致';
    if (state.quality === 'healthy') return interval + (state.calibration_version ? ' · 推算' : '');
  }
  if (state.quality !== 'healthy') return '暂不可确认';
  const value = state.value;
  if (value && typeof value === 'object') {
    if ('power' in value) return value.power === 'off' ? '关闭' : value.power === 'on' ? '开启' : display(value.power);
    return '查看状态';
  }
  if (!state.unit && ['on','off','open','closed'].includes(String(value))) return ({on:'开启',off:'关闭',open:'打开',closed:'关闭'})[value];
  return reading(state);
}
function stateRow(state) {
  const row = el('article',null,{class:'state-evidence','data-quality':state.quality,'data-state-id':state.id});
  add(row,add(el('div',null,{class:'state-heading'}),el('h3',state.unit ? '当前读数' : '当前状态'),badge(connected ? (qualityNames[state.quality] ?? '未知') : '连接中断',connected ? state.quality : 'unknown')),el('p',stateValue(state),{class:'reading'}));
  if (state.quality !== 'healthy') row.append(el('p',state.reasons.map(qualityExplanation).join(' '),{class:'muted'}));
  add(row,el('p','记录时间' + time(state.as_of),{class:'meta'}),(() => {const evidence = detail('来源记录',state); if (inspectingStateId === state.id) evidence.setAttribute('open',''); return evidence;})());
  return row;
}
function sourceQualityRow(source) {
  const entity = snapshot.entities.find(e => e.id === source.entity_id);
  const block = el('div',null,{class:'measurement-source'});
  add(block,el('p',withCopy(`${entity ? entityLabel(entity) : '测量来源'} · ${qualityNames[source.quality] ?? '未知'}`,qualityNames[source.quality])),
    el('p',`${({occupied_zone:'活动区',return_air:'回风口'})[source.placement] ?? source.placement}${source.installation_height_m === null || source.installation_height_m === undefined ? '' : ' · ' + source.installation_height_m + ' 米'} · ${({ambient:'环境测量','fan-only':'仅送风',cooling:'制冷'})[source.context] ?? '模式未知'}`,{class:'meta'}));
  const reasons = source.reasons.filter(r => !['device_sample_within_validity','primary_profile_selected'].includes(r));
  if (reasons.length) block.append(el('p',reasons.map(qualityExplanation).join(' '),{class:'muted'}));
  block.append(detail(copyName('Friday-1088'),source));
  return block;
}
function renderCanonicalMeasurement(content,entity) {
  const state = snapshot.states.find(s => s.id === entity.id+':temperature');
  if (!state) return;
  const block = el('section',null,{class:'canonical-measurement','data-canonical-id':entity.id,'data-quality':state.quality});
  add(block,add(el('div',null,{class:'section-heading'}),el('h3',entity.name),badge(qualityNames[state.quality] ?? '未知',state.quality)),el('p',stateValue(state),{class:'reading'}));
  if (state.quality !== 'healthy') block.append(el('p',state.reasons.map(qualityExplanation).join(' '),{class:'muted'}));
  else if (state.calibration_version) block.append(el('p','已用相对校准，位置与模式仍可能有偏差',{class:'muted'}));
  const provenance = add(el('details',null,{class:'details'}),el('summary',copyName('Friday-1090')));
  for (const source of snapshot.quality_sources ?? []) if (source.location === entity.room) provenance.append(sourceQualityRow(source));
  for (const [source,reason] of Object.entries(state.excluded_sources ?? {})) {
    const profile = (snapshot.quality_sources ?? []).find(s => s.physical_source_id === source);
    const label = snapshot.entities.find(e => e.id === profile?.entity_id);
    provenance.append(el('p',`${label ? entityLabel(label) : '来源'}未采用：${reason.split(',').map(qualityExplanation).join(' ')}`,{class:'muted'}));
  }
  add(provenance,el('p','有效至'+time(state.valid_until),{class:'meta'}),detail(copyName('Friday-1093'),state));
  block.append(provenance);
  if (state.simulated) block.append(el('p','示例数据 · 历史测量与校准为合成样本',{class:'meta'}));
  content.append(block);
}
function roomFilters(content,onChange) {
  const rooms = [...new Set((showAllStates ? snapshot.entities : inventoryEntities()).map(roomKey))];
  if (!rooms.includes(selectedRoom) && selectedRoom !== '*') selectedRoom = '*';
  const group = el('div',null,{class:'room-tabs',role:'group','aria-label':'筛选房间'});
  for (const key of ['*',...rooms]) group.append(button(key === '*' ? '全部' : roomName(key),() => {selectedRoom = key; selectedEntityId = null; lastTrace = null; deviceLimit = 24; onChange();},{'aria-pressed':String(selectedRoom === key),class:selectedRoom === key ? 'selected' : ''}));
  content.append(group);
}
function isPrimaryEntity(entity) {
  if (entity.function_ids && entity.category !== 'service') return true;
  return ['device','light','switch','climate','cover','lock','vacuum','fan','media_player','camera','alarm_control_panel','binary_sensor'].includes(entity.kind)
    || entity.capabilities.some(c => ['temperature','humidity','contact','motion','occupancy'].includes(c.name))
    || (entity.kind === 'sensor' && statesFor(entity).some(s => ['°C','°F','lx'].includes(s.unit)));
}
function primaryEntityRank(entity) {
  if (entity.capabilities.some(c => c.name === 'light.set')) return 0;
  return ({light:0,climate:1,cover:2,lock:3,vacuum:4,fan:5,camera:6,media_player:7,device:8,sensor:9,binary_sensor:10,switch:11,alarm_control_panel:12})[entity.kind] ?? 13;
}
function visibleEntities(primaryOnly = false) {
  const source = showAllStates && page === 'Devices' ? snapshot.entities : inventoryEntities();
  const entities = source.filter(e => (selectedRoom === '*' || roomKey(e) === selectedRoom) && (!primaryOnly || isPrimaryEntity(e)));
  return primaryOnly ? entities.sort((a,b) => primaryEntityRank(a) - primaryEntityRank(b)) : entities;
}
function inventoryCount(entities) { return snapshot.mode === 'simulator' || (snapshot.devices?.length && (page !== 'Devices' || !showAllStates)) ? `${entities.length} 个设备` : `${entities.length} 项设备状态`; }
function chooseEntity(entity) { inspectingStateId = null; selectedEntityId = entity.id; selectedActionFunctionId=null;lastTrace = null; render(); if (globalThis.matchMedia?.('(max-width:850px)').matches) $('device-details')?.scrollIntoView({block:'start'}); }
function entityItem(entity) {
  const state = primaryStateFor(entity);
  const item = button(null,() => chooseEntity(entity),{class:'device-row' + (selectedEntityId === entity.id ? ' selected' : ''),'data-entity-id':entity.id,'aria-pressed':String(selectedEntityId === entity.id)});
  const mark = add(el('span',null,{class:'device-mark'}),icon(deviceIcon(entity)));
  const text = add(el('span',null,{class:'device-text'}),el('span',entityLabel(entity),{class:'device-name'}),el('span',stateValue(state),{class:'device-value' + (state && state.quality !== 'healthy' ? ' uncertain' : '')}));
  if (state) text.append(el('span',!connected ? '连接中断' : state.quality === 'healthy' ? '更新于' + shortTime(state.as_of) : state.quality === 'unknown' ? '尚无有效记录' : qualityNames[state.quality] ?? '需要核对',{class:'device-time'}));
  return add(item,mark,text,icon('chevron-right'));
}
function selectedEntity(entities) {
  if (!entities.some(e => e.id === selectedEntityId)) selectedEntityId = (entities.find(e => e.capabilities.some(c => c.name === 'light.set' && c.access === 'write' && c.support === 'supported')) ?? entities[0])?.id ?? null;
  return entities.find(e => e.id === selectedEntityId);
}
function renderHome(content) {
  if (!inventoryEntities().some(isPrimaryEntity)) return empty(content,'还没有发现常用设备','检查家庭连接，或先体验示例家庭。','管理连接',() => go('Settings'));
  const roomColumn = el('div',null,{class:'room-column'}); roomFilters(roomColumn,render);
  const entities = visibleEntities(true); const selected = selectedEntity(entities);
  const layout = el('div',null,{class:'home-layout'}), rooms = el('div',null,{class:'room-grid'});
  const grouped = new Map();
  for (const entity of entities) { const key = roomKey(entity); if (!grouped.has(key)) grouped.set(key,[]); grouped.get(key).push(entity); }
  for (const [key, devices] of [...grouped].slice(0,4)) {
    const room = el('section',null,{class:'room-surface'});
    const openRoom = button('查看',() => {selectedRoom = key; go('Devices');},{class:'text-button','aria-label':'查看' + roomName(key) + '设备'});
    add(room,add(el('div',null,{class:'section-heading'}),add(el('div'),el('h2',roomName(key)),el('p',inventoryCount(devices),{class:'muted'})),openRoom));
    (snapshot.derived_entities ?? []).filter(e => e.room === key).forEach(e => renderCanonicalMeasurement(room,e));
    devices.slice(0,4).forEach(e => room.append(entityItem(e)));
    if (devices.length > 4) room.append(button('查看全部',() => {selectedRoom = key; go('Devices');},{class:'room-more'}));
    rooms.append(room);
  }
  if (grouped.size > 4) rooms.append(button(`查看全部 ${grouped.size} 个房间`,() => go('Devices'),{class:'room-more'}));
  roomColumn.append(rooms); layout.append(roomColumn);
  if (selected) { const inspector = el('aside',null,{class:'device-inspector',id:'device-details','aria-label':'设备详情'}); renderInspector(inspector,selected); layout.append(inspector); }
  content.append(layout);
  const issues = snapshot.states.filter(s => ['stale','faulty','suspect'].includes(s.quality)).filter(s => entities.some(e => sMatches(s,e)));
  if (issues.length) {
    const block = el('section',null,{class:'attention surface'});
    add(block,add(el('div',null,{class:'section-heading'}),el('h2','值得留意'),el('span',`${issues.length} 项`,{class:'muted'})));
    for (const state of issues.slice(0,3)) {
      const entity = entities.find(e => sMatches(state,e));
      const row = add(el('div',null,{'data-quality':state.quality,'data-state-id':state.id,class:'attention-row'}),add(el('span',null,{class:'attention-mark'}),icon('alert-circle')),add(el('div'),el('h3',entityName(state)),el('p',qualityNames[state.quality] ?? '需要核对',{class:'muted'})));
      row.append(button('检查来源',() => { if (entity) {selectedEntityId = entity.id;inspectingStateId = state.id;lastTrace = null;render();$('device-details')?.scrollIntoView({block:'nearest'});} },{class:'secondary'})); block.append(row);
    }
    if (issues.length > 3) block.append(button('查看全部',() => go('Devices'),{class:'text-button'}));
    content.append(block);
  }
  const recent = el('section',null,{class:'surface recent'});
  add(recent,add(el('div',null,{class:'section-heading'}),el('h2','最近动态'),button('查看全部',() => go('Activity'),{class:'text-button'})));
  renderEventList(recent,3); content.append(recent);
}
function sMatches(state,entity) { return (entity.function_ids ?? [entity.id]).some(id => state.id === id || state.id.startsWith(id + ':')); }
function renderInspector(content,entity) {
  add(content,add(el('div',null,{class:'inspector-heading'}),add(el('span',null,{class:'device-mark'}),icon(deviceIcon(entity))),add(el('div'),el('h2',entityLabel(entity)),el('p',roomName(roomKey(entity)),{class:'muted'}))));
  const states = statesFor(entity);
  const functionEntity = actionEntity(entity);
  const cap = functionEntity?.capabilities.find(c => deviceOperationLabels[c.name] && c.access === 'write' && c.support === 'supported');
  if (cap && connected) {
    const functions=controllableFunctions(entity);
    if(functions.length>1){
      const select=el('select',null,{id:'device-control-function','aria-label':'设备操作'});
      for(const e of functions)select.append(el('option',entityLabel(e),{value:e.id}));
      select.value=functionEntity.id;
      select.addEventListener('change',()=>{selectedActionFunctionId=select.value;lastTrace=null;render();});
      content.append(select);
    }
    if(cap.name==='light.set')renderActionForm(content,functionEntity);else renderDeviceActionForm(content,functionEntity);
  }
  else {
    if (!states.length) content.append(el('p','暂无状态记录',{class:'empty'}));
    states.forEach(s => content.append(stateRow(s)));
    if (!connected) content.append(el('p','连接恢复后可预览调整。',{class:'muted'}));
    else content.append(badge('只读'));
  }
  if (cap && connected && states.length) {const evidence = add(el('details',null,{class:'details current-state'}),el('summary','当前状态与来源')); states.forEach(s => evidence.append(stateRow(s))); if (states.some(s => s.id === inspectingStateId)) evidence.setAttribute('open',''); content.append(evidence);}
  content.append(detail('设备功能',{id:entity.id,identity_basis:entity.identity_basis,bindings:entity.bindings,capabilities:entity.capabilities,function_ids:entity.function_ids,limitations:entity.limitations}));
  if (entity.function_ids) content.append(detail('设备功能',snapshot.entities.filter(e => entity.function_ids.includes(e.id))));
  for (const source of snapshot.quality_sources ?? []) if ((entity.function_ids ?? [entity.id]).includes(source.entity_id)) content.append(sourceQualityRow(source));
}
function renderDevices(content) {
  roomFilters(content,render);
  const toolbar = el('div',null,{class:'device-toolbar'});
  const search = el('input',null,{type:'search',placeholder:'查找设备','aria-label':'搜索设备',id:'device-search'}); search.value = deviceQuery;
  const runSearch = () => {deviceQuery = search.value.trim();deviceLimit = 24;selectedEntityId = null;lastTrace = null;render();};
  search.addEventListener('change',runSearch); search.addEventListener('keydown',event => {if (event.key === 'Enter') runSearch();});
  add(toolbar,search,button('搜索',runSearch,{class:'secondary'}),button(showAllStates ? '常用设备' : '全部状态',() => {showAllStates = !showAllStates;deviceLimit = 24;selectedEntityId = null;lastTrace = null;render();},{class:'text-button','aria-pressed':String(showAllStates)})); content.append(toolbar);
  const entities = visibleEntities(!showAllStates).filter(e => (entityLabel(e) + ' ' + roomName(roomKey(e))).toLowerCase().includes(deviceQuery.toLowerCase()));
  if (!entities.length) return empty(content,'没有找到设备','试试其他名称或房间。');
  const selected = selectedEntity(entities), layout = el('div',null,{class:'devices-layout'}), list = el('div',null,{class:'surface device-list'});
  add(list,el('p',inventoryCount(entities),{class:'list-count muted'}));
  entities.slice(0,deviceLimit).forEach(e => list.append(entityItem(e)));
  if (entities.length > deviceLimit) list.append(button('加载更多',() => {deviceLimit += 24;render();},{class:'room-more'}));
  add(layout,list); if (selected) {const inspector = el('aside',null,{class:'device-inspector',id:'device-details','aria-label':'设备详情'});renderInspector(inspector,selected);layout.append(inspector);} content.append(layout);
}
function renderActionForm(content,entity = null) {
  if (!connected) {content.append(note('连接恢复后可预览调整。'));return;}
  const targets = entity ? [entity] : snapshot.entities.filter(e => e.capabilities.some(c => c.name === 'light.set' && c.access === 'write' && c.support === 'supported'));
  if (!targets.length) return;
  const form = el('form',null,{id:'action-form',class:'action-form'});
  const target = el('select',null,{id:'action-target',required:true});targets.forEach(e => target.append(el('option',entityLabel(e),{value:e.id})));
  const power = el('select',null,{id:'action-power'});add(power,el('option','打开',{value:'on'}),el('option','关闭',{value:'off'}));
  power.value = 'off'; const current = statesFor(targets[0]).find(s => s.quality === 'healthy' && s.value && typeof s.value === 'object' && 'power' in s.value);
  if (current && connected) power.value = current.value.power;
  const savedDraft = actionDrafts.get(targets[0].id);
  if (savedDraft?.version === snapshot.version) power.value = savedDraft.power;
  const brightness = el('input',null,{id:'action-brightness',type:'number',step:'1',placeholder:'保持档位'});
  const ttl = el('input',null,{id:'action-ttl',type:'number',min:'1',max:'60',value:'30',required:true});
  if (savedDraft?.version === snapshot.version) {brightness.value = savedDraft.brightness;ttl.value = savedDraft.ttl;}
  const rememberDraft = () => actionDrafts.set(target.value,{version:snapshot.version,power:power.value,brightness:brightness.value,ttl:ttl.value});
  brightness.addEventListener('input',rememberDraft);brightness.addEventListener('change',rememberDraft);
  if (!entity) form.append(add(el('label','设备'),target)); else {target.hidden = true;form.append(target);}
  power.hidden = true; form.append(power);
  const powerGroup = el('div',null,{class:'power-control',role:'group','aria-label':'电源'});
  const powerButtons = [];
  for (const [value,label] of [['on','打开'],['off','关闭']]) {
    const b = button(label,() => {power.value = value;rememberDraft();for (const [v,n] of powerButtons) n.setAttribute('aria-pressed',String(v === value));},{'aria-pressed':String(power.value === value)});
    powerButtons.push([value,b]);powerGroup.append(b);
  }
  form.append(add(el('div',null,{class:'control-field'}),el('span','电源',{class:'control-label'}),powerGroup));
  const brightnessGroup = el('div',null,{class:'brightness-control'});
  const shift = delta => {
    const capability = targets.find(e => e.id === target.value).capabilities.find(c => c.name === 'light.set');
    const base = brightness.value === '' ? capability.minimum : Number(brightness.value);
    if (!Number.isFinite(base)) return;
    brightness.value = String(Math.max(capability.minimum ?? -Infinity,Math.min(capability.maximum ?? Infinity,base + (brightness.value === '' ? 0 : delta))));rememberDraft();
  };
  add(brightnessGroup,add(button(null,() => shift(-1),{'aria-label':'降低亮度'}),icon('minus')),brightness,add(button(null,() => shift(1),{'aria-label':'提高亮度'}),icon('plus')));
  brightness.setAttribute('aria-label','亮度档位');
  form.append(add(el('div',null,{class:'control-field'}),el('span','亮度档位',{class:'control-label'}),brightnessGroup));
  const cap = targets[0].capabilities.find(c => c.name === 'light.set');
  const controlled = snapshot.mode === 'ha-read-only' && (snapshot.action_targets ?? []).includes(targets[0].id);
  if(controlled&&cap.minimum==null){brightness.disabled=true;brightnessGroup.hidden=true;}
  if (cap.minimum !== null && cap.minimum !== undefined) brightness.setAttribute('min',cap.minimum);
  if (cap.maximum !== null && cap.maximum !== undefined) brightness.setAttribute('max',cap.maximum);
  const hint = el('p',`范围 ${cap.minimum ?? '未知'}–${cap.maximum ?? '未知'}${controlled ? ' · HA 参数范围，真实最低档与无闪烁尚未核验' : ''}`,{class:'meta',id:'brightness-hint'});form.append(hint);
  ttl.hidden = true; form.append(ttl);
  const boundVersion = snapshot.version;
  const boundStateVersion = targets[0].state_version;
  const submit = add(el('button',null,{type:'submit',class:'primary',id:'dry-run-submit'}),icon('eye'),el('span','预览调整'));form.append(submit);
  form.addEventListener('submit',async event => {
    event.preventDefault();rememberDraft();submit.disabled = true;$('notification').textContent = '';
    const desired = {power:power.value}; if (brightness.value !== '' && power.value === 'on') desired.brightness = Number(brightness.value);
    const request = controlled ? {schema_version:1,target:target.value,desired,actor:'console',expires_at:new Date(Date.now()+Number(ttl.value)*1000).toISOString(),expected_state_version:boundStateVersion,idempotency_key:crypto.randomUUID()} : {schema_version:1,target:target.value,capability:'light.set',desired,actor:'console',origin:'dry-run',expires_at:new Date(Date.now()+Number(ttl.value)*1000).toISOString(),expected_version:boundVersion,idempotency_key:crypto.randomUUID()};
    try {
      lastTrace = await api(controlled ? '/api/actions/plan' : '/api/actions/dry-run',{method:'POST',body:JSON.stringify(request)});
      const known = new Set(snapshot.events.map(event => event.event_id));
      snapshot.events = [...snapshot.events,...(lastTrace.events ?? []).filter(event => !known.has(event.event_id))].slice(-1000);
      render();$('trace-result')?.scrollIntoView({block:'nearest'});
    } catch (error) {$('notification').textContent = error.message;submit.disabled = false;}
  });content.append(form);if (lastTrace) renderTrace(content,lastTrace);
}
function renderDeviceActionForm(content,entity) {
  const caps=entity.capabilities.filter(c=>deviceOperationLabels[c.name]&&c.access==='write'&&c.support==='supported');
  if(snapshot.mode!=='ha-read-only'||!(snapshot.action_targets??[]).includes(entity.id)||!caps.length)return;
  const form=el('form',null,{id:'device-action-form',class:'action-form'});
  const operation=el('select',null,{id:'device-action-operation','aria-label':'调整功能'});
  for(const c of caps)operation.append(el('option',deviceOperationLabels[c.name],{value:c.name}));
  const fields=el('div',null,{class:'control-field'}),submit=el('button','预览调整',{type:'submit',class:'primary',id:'device-action-preview'});
  const stamp=entity.state_version;
  const saved=deviceActionDrafts.get(entity.id);
  const draft=saved?.version===stamp?saved:{version:stamp,operation:caps[0].name,values:{}};
  if(caps.some(c=>c.name===draft.operation))operation.value=draft.operation;
  let parameter,key;
  const remember=()=>{
    draft.operation=operation.value;draft.values[operation.value]=parameter.value;
    deviceActionDrafts.delete(entity.id);deviceActionDrafts.set(entity.id,draft);
    if(deviceActionDrafts.size>64)deviceActionDrafts.delete(deviceActionDrafts.keys().next().value);
  };
  const draw=()=>{
    const cap=caps.find(c=>c.name===operation.value);fields.replaceChildren();
    key={'switch.set':'power','fan.set':'power','fan.percentage':'percentage','climate.mode':'hvac_mode','climate.temperature':'temperature','cover.position':'position','lock.set':'lock','number.value':'value','select.option':'option'}[cap.name];
    const choices=key==='power'?['on','off']:cap.choices??[];
    if(choices.length){
      parameter=el('select',null,{id:'device-action-value',required:true,'aria-label':deviceOperationLabels[cap.name]});
      parameter.append(el('option','目标状态',{value:''}));
      const names={on:'打开',off:'关闭',locked:'锁定',unlocked:'解锁',cool:'制冷',heat:'制热',heat_cool:'自动冷暖',auto:'自动',dry:'除湿',fan_only:'送风'};
      for(const value of choices)parameter.append(el('option',names[value]??value,{value}));
      parameter.value='';
    }else{
      parameter=el('input',null,{id:'device-action-value',type:'number',required:true,min:cap.minimum,max:cap.maximum,step:cap.step??'any','aria-label':deviceOperationLabels[cap.name],placeholder:'目标数值'});
    }
    const value=draft.values[operation.value]??'';
    parameter.value=choices.length&&!choices.includes(value)?'':value;
    parameter.addEventListener('input',remember);parameter.addEventListener('change',remember);
    fields.append(parameter);
    if(cap.minimum!=null)fields.append(el('p',`按设备允许范围调整，再读取核对。HA 范围 ${cap.minimum}–${cap.maximum} ${cap.unit??(key==='temperature'?'HA 温度单位':'')}；以设备返回状态核对结果。`,{class:'meta'}));
  };
  operation.addEventListener('change',()=>{draw();remember();});draw();
  add(form,add(el('label','调整功能'),operation),fields,submit);
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(!parameter.value)return;remember();submit.disabled=true;
    const numeric=['percentage','position','temperature','value'].includes(key);
    const request={schema_version:1,target:entity.id,operation:operation.value,desired:{[key]:numeric?Number(parameter.value):parameter.value},actor:'console',expected_state_version:stamp,expires_at:new Date(Date.now()+30000).toISOString(),idempotency_key:crypto.randomUUID()};
    try{lastTrace=await api('/api/actions/plan',{method:'POST',body:JSON.stringify(request)});render();$('trace-result')?.scrollIntoView({block:'nearest'});}
    catch(_){$('notification').textContent='预览未完成，请重新读取设备状态。';submit.disabled=false;}
  });
  content.append(form);if(lastTrace)renderTrace(content,lastTrace);
}
function renderTrace(content,trace) {
  const block = el('section',null,{class:'trace',id:'trace-result','aria-label':'调整预览'});
  add(block,el('h3',qualityNames[trace.status] ?? trace.status),el('p',trace.status === 'rejected' ? ({source_conflict:'家庭状态已更新，请刷新后重试。',ambiguous_target:'设备目标需要重新确认。',unsupported:'设备不支持这项调整。',unsupported_parameters:'调整超出设备支持范围。',stale_input:'设备状态需要更新，请刷新后重试。',unsupported_ttl:'预览时间设置无效，请重试。'}[trace.code] ?? '暂时无法预览，请检查设备状态。') : trace.real_write_attempted ? '设备操作已提交' : '预览已记录，设备未改变。',{class:'muted'}),detail('操作记录',trace));
  block.append(button('刷新记录',async () => {try {lastTrace = await api('/api/traces/'+encodeURIComponent(trace.action_id));render();}catch(error){$('notification').textContent = error.message;}},{class:'text-button'}));content.append(block);
  if (['ha-scoped-light','ha-scoped-device'].includes(trace.route)) {
    block.append(el('p',trace.status === 'planned' ? '点击执行才会操作这台设备；计划有时限。' : 'HA返回未确认实物效果，结果未知不重试。返回状态来自 HA，尚未独立确认物理效果。结果未知时不会自动重试。',{class:'muted'}));
    const submit = async (endpoint, control) => {
      control.disabled = true;
      try {
        lastTrace = await api(endpoint,{method:'POST',body:JSON.stringify({plan_id:trace.action_id})});
        try {snapshot = await api('/api/snapshot');connected = true;} catch (_) {connected = false;}
        render();
      } catch (_) {$('notification').textContent = '结果未知，请刷新记录，勿重复发送。未取得操作结果。请重新读取记录，不要重复发送。';}
    };
    if (trace.status === 'planned' && Date.parse(trace.request.expires_at) > Date.now()) {
      const execute = button(copyName('Friday-1168'),() => submit('/api/actions/execute',execute),{class:'primary',id:'execute-plan'});block.append(execute);
    }
    if (trace.status === 'unknown_outcome') {
      const reconcile = button(copyName('Friday-1169'),() => submit('/api/actions/reconcile',reconcile),{class:'secondary',id:'reconcile-plan'});block.append(reconcile);
    }
  }
}
function eventTitle(event) { return event.payload?.reason === 'camera_media_probe' ? '摄像头音视频已检查' : event.simulated && event.payload?.rule_run_id ? copyName('Friday-1171') : ({'action-result':'调整预览','quality-change':'测量来源已更新','state-transition':'设备状态变化','source-report':copyName('Friday-1174'),'state-updated':'状态更新',observation:'设备状态', 'state-snapshot':'设备状态已核对','observation-gap':'连接缺口','source-gap':'连接中断','source-recovered':'连接恢复'})[event.kind] ?? '家庭记录'; }
function renderEventList(content,limit) {
  const events = [...snapshot.events].sort((a,b) => Date.parse(b.wall_time)-Date.parse(a.wall_time));
  if (!events.length) {content.append(el('p','还没有新的动态。',{class:'empty'}));return;}
  const list = el('ol',null,{class:'timeline'});
  for (const event of events.slice(0,limit)) {
    const entry = el('li',null,{'data-event-id':event.event_id});
    add(entry,add(el('div',null,{class:'timeline-heading'}),icon(event.kind === 'action-result' ? 'eye' : 'clock'),el('time',shortTime(event.wall_time)),el('h3',eventTitle(event))));
    if (event.kind === 'quality-change') {
      entry.append(el('p',event.payload.reason === 'camera_media_probe'
        ? (event.payload.status === 'decoded_sync_unknown' ? '影音解码通过，音画同步尚未确认' : '影音验证未通过，请在连接设置查看。摄像头音视频验证未通过，请在连接设置查看结果。')
        : (event.payload.reasons ?? []).map(qualityExplanation).join(' '),{class:'muted'}));
      if (event.simulated) entry.append(el('p','示例数据 · 没有设备动作',{class:'meta'}));
    }
    if (event.kind === 'state-transition' && event.payload.entity_id) {
      entry.append(el('p',entityName({id:event.payload.entity_id}) + ' · ' + String(event.payload.from ?? '未知') + ' → ' + String(event.payload.to ?? '未知'),{class:'muted'}));
      if (event.source === 'ha') entry.append(el('p','HA报告状态变化，实物效果尚未确认',{class:'meta'}));
    }
    if (event.kind === 'state-snapshot' && event.payload.reconciled_after_gap) entry.append(el('p','已核对当前状态，不补发断连期间动作。重新读取了当前状态；不会补触发断连期间的动作。',{class:'muted'}));
    if (event.kind === 'observation-gap') entry.append(el('p','断连期间情况未知，恢复只核对当前状态。缺口期间发生了什么仍未知；恢复后只核对当前状态。',{class:'muted'}));
    if (event.kind === 'source-report' && event.payload.reason === 'late_report') entry.append(el('p','迟到或时间不可靠的报告不触发动作。迟到或时间不可靠的报告仅用于核对，不触发动作。',{class:'muted'}));
    if (page === 'Activity') {
      add(entry,el('p',event.causation_id ? copyName('Friday-1191') : '未记录触发原因',{class:'muted'}),detail('记录详情',event));
      if(event.simulated && typeof event.payload.rule_run_id === 'string') {
        entry.append(el('p','simulated · 仅改变示例状态',{class:'meta'}),button('执行记录',async()=>{
          try {const trace=await api('/api/rules/runs/'+encodeURIComponent(event.payload.rule_run_id));entry.append(detail('模拟执行记录',trace));}
          catch (_) {$('notification').textContent='暂时无法读取执行记录。';}
        },{class:'text-button'}));
      }
      if (event.kind === 'action-result' && typeof event.payload.action_id === 'string') entry.append(button('操作记录',async () => {try {lastTrace = await api('/api/traces/'+encodeURIComponent(event.payload.action_id));render();$('trace-result')?.scrollIntoView({block:'nearest'});}catch(error){$('notification').textContent = error.message;}},{'data-action-id':event.payload.action_id,class:'text-button'}));
    }
    list.append(entry);
  }
  content.append(list);
  if (page === 'Activity' && events.length > limit) content.append(button('加载更多',() => {activityLimit += 30;render();},{class:'room-more'}));
}
function renderActivity(content) {
  const block = el('section',null,{class:'surface',id:'activity-events'});renderEventList(block,activityLimit);if (lastTrace) renderTrace(block,lastTrace);content.append(block);
  renderArchiveLookup(content);
}
const archiveLabels={ready:'可准备隐私预览',pending:'等待落盘或完成核对',missing:'该区间没有已知片段',expired:'索引中的原片已不存在',unavailable:'暂不能定位原片'};
const archiveReasons={archive_camera_mapping_required:'请先对应NAS录像目录与摄像头',archive_recording_clock_unreviewed:'需要核对摄像头录像时钟。',archive_file_still_changing:'文件仍在变化，暂不能用于区间引用。',archive_completion_unreviewed:'录像写入完成的规则尚未核对。',archive_media_time_or_container_unvalidated:'媒体格式或时长与文件名区间尚未匹配。',archive_index_coverage_incomplete:'目录记录不完整，请刷新并检查对应关系。当前索引只覆盖部分目录或文件名；请刷新索引并检查映射。',archive_coverage_or_clock_boundary_uncertain:'片段之间存在空缺或时钟边界不确定。',archive_overlapping_segments_ambiguous:'该区间存在重叠片段，需要核对来源。',archive_within_declared_arrival_delay:'仍在你设置的归档等待时间内。',archive_source_segment_expired:'已知原片已从可读目录中消失。'};
function localArchiveDate(value){const d=new Date(value);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,23);}
function showArchiveResolution(output,report){
  output.replaceChildren();add(output,badge(report.simulated?'simulated · 合成录像引用':'NAS · 录像只读引用'),el('h3',archiveLabels[report.status]??'需要核对'),el('p',archiveReasons[report.reason]??(report.status==='ready'?(report.simulated?'已定位合成录像片段，未读取真实原片。':'录像引用和时间已核对；播放前会在本机生成保护预览。'):'请核对归档来源和当前索引。')));
  for(const segment of report.segments??[]){
    const row=el('article',null,{class:'automation-row'});add(row,el('p',`${time(segment.time_range.start)} — ${time(segment.time_range.end)}`),badge(archiveLabels[segment.status]??'需要核对'),el('p',`时钟误差 ${segment.clock_uncertainty_seconds===null?'未知':'±'+segment.clock_uncertainty_seconds+' 秒'} · ${segment.audio_track===true?'含音轨':segment.audio_track===false?'未发现音轨':'音轨未确认'}`,{class:'meta'}),detail('片段依据',segment));output.append(row);
    if(segment.status==='ready')row.append(button('打开片段',()=>openArchivePlayer(output,report,segment),{class:'secondary','data-archive-playback-ref':segment.media_ref}));
  }
  if(report.gaps?.length)output.append(detail('尚未覆盖的区间',report.gaps));
  output.append(el('p','附近录像只作画面参考，不能证明触发原因。保护预览默认静音。',{class:'meta'}));
  if(archivePlayerView&&archivePlayerView.plan.context_fingerprint===report.context_fingerprint)output.append(archivePlayerView.root);
}
function cancelArchivePlayback(plan){if(/^[a-f0-9]{64}$/.test(plan?.plan_id??''))void api('/api/archive/playback/cancel',{method:'POST',body:JSON.stringify({plan_id:plan.plan_id})}).catch(()=>{});}
function stopArchivePlayer(message=''){
  archivePrepareController?.abort();archivePrepareController=null;
  if(archiveExpiryTimer!==null&&typeof clearTimeout==='function')clearTimeout(archiveExpiryTimer);archiveExpiryTimer=null;
  if(archiveContextTimer!==null&&typeof clearInterval==='function')clearInterval(archiveContextTimer);archiveContextTimer=null;
  archivePlayerVersion++;const view=archivePlayerView;archivePlayerView=null;if(!view)return;
  view.video.pause?.();view.video.removeAttribute?.('src');view.video.load?.();view.video.setAttribute('hidden','');cancelArchivePlayback(view.plan);
  if(message)settingsMessage(view.status,message,true);
}
async function openEventRecording(output,details){
  stopArchivePlayer();const version=++archiveVersion;output.replaceChildren();
  if(!details)return;
  const request=details.request;
  const allowed=['schema_version','camera_id','expected_context','time_range'];
  if(!request||Object.keys(request).some(key=>!allowed.includes(key))
    ||(request.schema_version!==undefined&&request.schema_version!==1)
    ||!/^[a-f0-9]{64}$/.test(request.expected_context??'')||typeof request.camera_id!=='string'
    ||!request.time_range||Object.keys(request.time_range).some(key=>!['schema_version','start','end'].includes(key))
    ||(request.time_range.schema_version!==undefined&&request.time_range.schema_version!==1)
    ||!Number.isFinite(Date.parse(request.time_range.start))||!Number.isFinite(Date.parse(request.time_range.end))){
    output.append(el('p','这条录像引用暂不可核对，请刷新事件后重试。',{role:'status'}));return;
  }
  output.append(el('p','正在定位事件附近的录像…',{role:'status','aria-live':'polite'}));
  output.scrollIntoView?.({block:'nearest'});
  try{
    const report=await api('/api/archive/resolve',{method:'POST',body:JSON.stringify(request)}),context=await api('/api/archive/context');
    if(version!==archiveVersion||page!=='Activity'||!output.isConnected)return;
    if(report.context_fingerprint!==request.expected_context||context.fingerprint!==report.context_fingerprint||context.simulated!==report.simulated)throw Error('source_changed');
    showArchiveResolution(output,report);
    output.scrollIntoView?.({block:'nearest'});
    const ready=(report.segments??[]).filter(segment=>segment.status==='ready');
    if(ready.length===1)await openArchivePlayer(output,report,ready[0]);
  }catch(_){if(version===archiveVersion&&page==='Activity'&&output.isConnected)output.replaceChildren(el('p','事件录像暂不可定位，请核对 NAS 连接、时间和摄像头对应关系后重试。',{role:'status'}));}
}
async function openArchivePlayer(output,report,segment){
  stopArchivePlayer();const version=++archivePlayerVersion,queryVersion=archiveVersion;
  const start=Math.max(Date.parse(report.time_range.start),Date.parse(segment.time_range.start)+(segment.clock_uncertainty_seconds??0)*1000),end=Math.min(Date.parse(report.time_range.end),Date.parse(segment.time_range.end)-(segment.clock_uncertainty_seconds??0)*1000);
  if(!(end>start))return;
  const status=el('div','正在生成隐私预览…',{role:'status','aria-live':'polite'});output.append(status);
  if(typeof AbortController==='function')archivePrepareController=new AbortController();
  const signal=archivePrepareController?.signal;
  status.append(button('取消',()=>{stopArchivePlayer();status.textContent='已取消准备。';},{class:'secondary'}));
  let preparedPlan=null,adopted=false;
  try{
    preparedPlan=await api('/api/archive/playback',{method:'POST',signal,body:JSON.stringify({expected_context:report.context_fingerprint,camera_id:report.camera_id,media_ref:segment.media_ref,time_range:{start:new Date(start).toISOString(),end:new Date(end).toISOString()}})});
    const plan=preparedPlan;
    if(version!==archivePlayerVersion||queryVersion!==archiveVersion||page!=='Activity')return;
    const fresh=await api('/api/archive/context',{signal});
    if(version!==archivePlayerVersion||queryVersion!==archiveVersion||page!=='Activity'||fresh.fingerprint!==plan.context_fingerprint||fresh.simulated!==plan.simulated){cancelArchivePlayback(plan);return;}
    if(plan.privacy_protected!==true||plan.privacy_preview_generated!==true||plan.original_modified!==false||!['local_person_mosaic','local_full_frame_mosaic'].includes(plan.privacy_mode)){cancelArchivePlayback(plan);status.textContent='隐私预览未就绪';return;}
    if(!/^\/api\/archive\/media\/[a-f0-9]{64}$/.test(plan.stream_url)||!Number.isFinite(plan.start_offset_seconds)||!Number.isFinite(plan.end_offset_seconds)||plan.end_offset_seconds<=plan.start_offset_seconds){cancelArchivePlayback(plan);throw Error('unqualified media');}
    const root=el('section',null,{class:'archive-player',id:'archive-player'}),video=el('video',null,{controls:'',playsinline:'',preload:'metadata','aria-label':plan.simulated?'simulated 测试录像':copyName('Friday-1228')}),receipt=el('p','等待媒体定位…',{id:'archive-seek-evidence',class:'meta'});
    video.muted=true;
    const view={root,video,plan,status:receipt};archivePlayerView=view;
    const active=()=>archivePlayerView===view&&page==='Activity'&&version===archivePlayerVersion;
    const expires=Date.parse(plan.expires_at)-Date.now();
    if(!Number.isFinite(expires)||expires<=0||expires>301000){stopArchivePlayer('预览已过期，请重新选择。');return;}
    if(typeof setTimeout==='function')archiveExpiryTimer=setTimeout(()=>{if(active())stopArchivePlayer('预览已过期，请重新选择。');},expires);
    archiveContextTimer=setInterval(async()=>{
      if(!active())return;
      try{const context=await api('/api/archive/context');if(active()&&(context.fingerprint!==plan.context_fingerprint||context.simulated!==plan.simulated))stopArchivePlayer('录像来源已变化，请重新选择。');}
      catch(_){if(active())stopArchivePlayer('录像来源暂不可核对，请重新选择。');}
    },15000);
    const stateEvidence=()=>{
      for(const [key,value] of [['position',video.currentTime],['duration',video.duration],['width',video.videoWidth],['height',video.videoHeight],['ready-state',video.readyState]])if(Number.isFinite(value))video.setAttribute('data-'+key,value);
      if(typeof video.paused==='boolean')video.setAttribute('data-paused',video.paused);
      video.setAttribute('data-muted',video.muted);
    };
    for(const event of ['loadeddata','seeked','play','pause','timeupdate'])video.addEventListener(event,stateEvidence);
    let wanted=plan.start_offset_seconds,seekSequence=0,frameCallback=null,frameReceipt=null,ended=false;
    const failed=()=>{if(active())stopArchivePlayer('片段解码或读取失败，请重新查找原片。暂不能解码或继续读取该片段，请重新查询原片。');};
    const showPosition=()=>{
      if(frameReceipt){
        receipt.textContent=`录像帧位置与时钟误差。呈现帧 ${frameReceipt.mediaTime.toFixed(3)} 秒 · 媒体定位误差 ${frameReceipt.error.toFixed(3)} 秒 · 录像时钟 ±${plan.clock_uncertainty_seconds} 秒`;
        receipt.setAttribute('data-media-time',frameReceipt.mediaTime);receipt.setAttribute('data-seek-target',frameReceipt.target);receipt.setAttribute('data-seek-error',frameReceipt.error);
      }else{
        receipt.textContent=`浏览器位置已报告，实际帧待核对。浏览器报告位置 ${video.currentTime.toFixed(3)} 秒 · 实际帧定位误差待读取 · 录像时钟 ±${plan.clock_uncertainty_seconds} 秒`;
        for(const key of ['data-media-time','data-seek-target','data-seek-error'])receipt.removeAttribute(key);
      }
      if(ended)receipt.append(el('span',' · 所选区间结束'));
    };
    const observeFrame=()=>{
      if(!video.requestVideoFrameCallback||frameCallback!==null)return;
      const sequence=seekSequence,target=wanted;
      frameCallback=video.requestVideoFrameCallback((_,metadata)=>{
        frameCallback=null;
        if(!active()||sequence!==seekSequence||video.seeking||!Number.isFinite(metadata.mediaTime))return;
        frameReceipt={mediaTime:metadata.mediaTime,target,error:Math.abs(metadata.mediaTime-target)};showPosition();
      });
    };
    video.addEventListener('loadedmetadata',()=>{if(!active())return;if(!Number.isFinite(video.duration)||Math.abs(video.duration-plan.duration_seconds)>2){failed();return;}video.currentTime=wanted;observeFrame();});
    video.addEventListener('seeking',()=>{
      if(!active())return;
      const clamped=Math.max(plan.start_offset_seconds,Math.min(video.currentTime,plan.end_offset_seconds));wanted=clamped;seekSequence++;frameReceipt=null;if(clamped<plan.end_offset_seconds-.002)ended=false;
      if(frameCallback!==null&&video.cancelVideoFrameCallback)video.cancelVideoFrameCallback(frameCallback);
      frameCallback=null;
      if(video.currentTime!==clamped)video.currentTime=clamped;
      observeFrame();
    });
    video.addEventListener('seeked',()=>{
      if(!active())return;showPosition();if(!frameReceipt)observeFrame();
    });
    video.addEventListener('timeupdate',()=>{if(active()&&video.currentTime>=plan.end_offset_seconds){ended=true;video.pause();video.currentTime=Math.max(plan.start_offset_seconds,plan.end_offset_seconds-.001);receipt.append(el('span',' · 所选区间结束'));}});
    video.addEventListener('play',()=>{if(!active())return;if(Date.parse(plan.expires_at)<=Date.now()){stopArchivePlayer('播放资格已过期，请重新查询。');return;}if(ended){ended=false;video.currentTime=plan.start_offset_seconds;}});
    video.addEventListener('error',failed);
    const protection=plan.privacy_mode==='local_full_frame_mosaic'?'整帧遮挡保护':'人物遮挡保护';
    add(root,badge(plan.simulated?'simulated · 合成录像隐私预览':'本机隐私预览'),el('h3','所选片段'),video,receipt,el('p',`${protection} · 默认静音 · 播放资格至 ${time(plan.expires_at)} · 64 MiB 读取上限`,{class:'meta'}),button('停止回放',()=>stopArchivePlayer('已停止回放。'),{id:'archive-player-stop',class:'secondary'}));
    status.remove?.();output.append(root);video.setAttribute('src',plan.stream_url);adopted=true;root.scrollIntoView?.({block:'nearest'});
  }catch(_){if(version===archivePlayerVersion&&page==='Activity')settingsMessage(status,'保护预览暂不能播放，请重新选择片段并检查本机播放依赖、连接和时钟。',true);}
  finally{if(preparedPlan&&!adopted)cancelArchivePlayback(preparedPlan);if(version===archivePlayerVersion)archivePrepareController=null;}
}
function renderArchiveLookup(content){
  const session=++archiveSession,block=el('section',null,{class:'surface archive-lookup',id:'archive-lookup'});
  add(block,el('h2','附近录像'),el('p','按摄像头和时间定位 NAS 原片引用。',{class:'muted'}));
  const form=el('form',null,{class:'form-grid settings-form',id:'archive-lookup-form'}),camera=el('select',null,{id:'archive-camera','aria-label':copyName('Friday-1244')});
  const start=el('input',null,{type:'datetime-local',step:'0.001',id:'archive-start','aria-label':'开始时间'}),end=el('input',null,{type:'datetime-local',step:'0.001',id:'archive-end','aria-label':'结束时间'});
  const submit=el('button','查找录像',{type:'submit',id:'archive-resolve',class:'primary'});submit.disabled=true;
  const output=el('div',null,{id:'archive-result',role:'status','aria-live':'polite'}),examples=el('div',null,{class:'settings-actions'});
  add(form,add(el('label','摄像头'),camera),add(el('label','开始时间'),start),add(el('label','结束时间'),end),submit);add(block,form,examples,output);content.append(block);
  const current=()=>session===archiveSession&&page==='Activity'&&block.isConnected;
  const edited=()=>{archiveDraft.camera=camera.value;archiveDraft.start=start.value;archiveDraft.end=end.value;archiveResult=null;archiveVersion++;stopArchivePlayer();output.replaceChildren();};
  for(const field of [camera,start,end])field.addEventListener('input',edited);camera.addEventListener('change',edited);
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(!archiveContext||!camera.value)return;
    stopArchivePlayer();const version=++archiveVersion,context=archiveContext;submit.disabled=true;
    try{
      const request={expected_context:context.fingerprint,camera_id:camera.value,time_range:{start:new Date(start.value).toISOString(),end:new Date(end.value).toISOString()}};
      const report=await api('/api/archive/resolve',{method:'POST',body:JSON.stringify(request)}),fresh=await api('/api/archive/context');
      if(version!==archiveVersion||page!=='Activity')return;
      if(fresh.fingerprint!==report.context_fingerprint||fresh.simulated!==report.simulated){archiveResult=null;if(current())output.replaceChildren(note('归档来源或映射已变化，请重新查找。',true));return;}
      archiveContext=fresh;archiveResult=report;
      if(current())showArchiveResolution(output,report);else if($('archive-result'))showArchiveResolution($('archive-result'),report);
    }catch(_){if(current())output.replaceChildren(note('请核对连接状态与使用范围。暂不能读取归档区间。请检查 NAS 连接、摄像头映射和时间范围（最多 30 分钟）。',true));}
    finally{if(current())submit.disabled=!camera.value;}
  });
  api('/api/archive/context').then(context=>{
    if(!current())return;
    if(archiveContext&&archiveContext.fingerprint!==context.fingerprint){archiveResult=null;archiveVersion++;stopArchivePlayer();}
    archiveContext=context;
    for(const item of context.cameras??[])camera.append(el('option',`${item.name}${item.room?' · '+item.room:''}`,{value:item.id}));
    if(!context.cameras?.length){output.append(el('p','还没有对应的摄像头目录。'));output.append(button(copyName('Friday-1254'),()=>{selectedConnection='settings-nas';go('Settings');},{class:'secondary'}));return;}
    camera.value=(context.cameras??[]).some(c=>c.id===archiveDraft.camera)?archiveDraft.camera:context.cameras[0].id;
    if(!archiveDraft.start||!archiveDraft.end){const span=context.cameras[0].example_ranges?.ready??{start:new Date(Date.now()-120000).toISOString(),end:new Date().toISOString()};archiveDraft.start=localArchiveDate(span.start);archiveDraft.end=localArchiveDate(span.end);}
    archiveDraft.camera=camera.value;start.value=archiveDraft.start;end.value=archiveDraft.end;submit.disabled=false;
    if(archiveResult&&archiveResult.context_fingerprint===context.fingerprint)showArchiveResolution(output,archiveResult);
    if(context.simulated){
      const ranges=context.cameras[0].example_ranges;
      for(const [key,label] of [['ready','跨段示例'],['pending','示例等待落盘'],['expired','示例原片过期']])examples.append(button(label,()=>{start.value=localArchiveDate(ranges[key].start);end.value=localArchiveDate(ranges[key].end);edited();},{class:'secondary'}));
      for(const [scenario,label] of [['late-arrives',copyName('Friday-1258')],['offline','模拟归档不可用'],['reset',copyName('Friday-1260')]])examples.append(button(label,async()=>{try{archiveContext=await api('/api/archive/simulator',{method:'POST',body:JSON.stringify({expected_context:archiveContext.fingerprint,scenario})});archiveResult=null;archiveVersion++;stopArchivePlayer();render();}catch(_){if(current())output.replaceChildren(note('示例状态已变化，请重新读取。',true));}},{class:'secondary'}));
      examples.append(el('p','均为模拟引用，不读取真实NAS。所有示例引用均为 simulated，不读取真实 NAS。',{class:'meta'}));
    }
  }).catch(()=>{if(current())output.replaceChildren(el('p','归档引用暂不可读取。'));});
}
function renderAutomations(content) {
  content.append(note('结果尚未确认，请核对完整说明。外部米家规则可能不可见；中央 lease 只能约束本系统的动作，无法拦截未知外部规则。'));
  if (snapshot.mode === 'simulator') {renderRuleTextChanges(content);renderManagedRules(content);}
  else {renderCoreRules(content);}
  renderRuleAnalysis(content);
  renderGeekInventory(content);
  if (!snapshot.rules.length) return;
  const list = el('div',null,{class:'surface automation-list'});
  for (const rule of snapshot.rules) {
    const row = el('article',null,{class:'automation-row'});
    add(row,add(el('div',null,{class:'section-heading'}),add(el('div'),el('h2',rule.id === 'arrival' ? '回家' : rule.id),el('p',rule.source,{class:'meta'})),badge(rule.normalization === 'opaque' ? '需在原平台查看' : '已读取')));
    if (rule.raw !== null) row.append(detail('原始规则',rule.raw));
    row.append(detail('连接版本',{readable:rule.readable,writable:rule.writable,coverage:rule.coverage_note,fingerprint:rule.fingerprint}));list.append(row);
  }
  content.append(list);
}
function renderGeekInventory(content) {
  const version=++geekInventorySession,block=el('section',null,{class:'surface',id:'geek-rule-inventory'});
  add(block,el('h2',copyName('Friday-1270')),el('p','只含极客版可见规则，详情修改在原平台。仅包含极客版可见规则；详情和修改请在原平台查看。',{class:'muted'}));
  const output=el('div','正在读取…',{id:'geek-rule-inventory-result'});
  const current=()=>page==='Automations'&&version===geekInventorySession&&$('geek-rule-inventory')===block;
  const control=button('刷新规则',()=>refreshGeekInventory(),{class:'secondary',id:'geek-rule-inventory-refresh'});
  add(block,output,add(el('div',null,{class:'settings-actions'}),control,button('管理连接',()=>go('Settings'),{class:'text-button'})));content.append(block);
  let lastRendered = null;
  block.readInventory=async()=>{
    if(!current()||geekInventoryRequest)return;
    const request={version};geekInventoryRequest=request;control.disabled=true;
    try {
      const report=await api('/api/settings/geek/inventory',{signal:globalThis.AbortSignal?.timeout(5000)});
      if(!current())return;
      if(report.status!=='ready'||report.session?.status!=='online'||report.session?.inventory_stale!==false||report.session?.session_live!==true) {
        lastRendered=null;output.replaceChildren();
        const unavailable=report.session?.status==='blocked'?'网关连接未完成，请在连接设置核对。':report.session?.status==='starting'?'正在连接网关，等待完整规则清单。':'暂无可读规则，请在连接设置启动同步';
        output.append(el('p',({stale:'规则记录已过期，请检查连接。',integrity_failed:'规则记录未通过完整性核对，请重新同步。'})[report.status]??unavailable));return;
      }
      const updated=`已读取 ${report.graphs.length} 条规则 · ${shortTime(report.session.checked_at)}`;
      const key=/^[a-f0-9]{64}$/.test(report.fingerprint??'')?JSON.stringify([report.fingerprint,report.session.gap,report.session.last_change??null]):null;
      if(key&&key===lastRendered){$('geek-rule-inventory-updated').textContent=updated;return;}
      lastRendered=key;output.replaceChildren();
      add(output,el('p',updated,{class:'meta',id:'geek-rule-inventory-updated'}));
      if(report.session.gap)output.append(el('p','重连期间的变化可能未完整记录。',{class:'meta'}));
      const changes=report.session.last_change;
      if(changes)output.append(el('p',`新增、移除与修改的规则数量。最近发现：新增 ${changes.added}，移除 ${changes.removed}，修改 ${changes.modified}`,{class:'meta'}));
      for(const graph of report.graphs) {
        const row=el('article',null,{class:'automation-row','data-geek-graph':graph.id});
        add(row,el('h3',graph.name??'未命名规则'),badge(copyName('Friday-1284')));
        if(typeof graph.source_enabled==='boolean')row.append(el('p','来源标记'+(graph.source_enabled?'启用':'停用'),{class:'meta'}));
        row.append(detail('来源校验',{id:graph.id,normalization:'opaque',writable:false,content_fingerprint:graph.fingerprint,raw_sha256:graph.raw_sha256,raw_record:'私密保留',content_observed_at:report.observed_at}));output.append(row);
      }
      if(!report.graphs.length)output.append(el('p','极客版未返回可见规则。'));
    } catch (_) {if(current()){lastRendered=null;output.replaceChildren(note('规则暂不可读取，请刷新。',true));}}
    finally {if(geekInventoryRequest===request)geekInventoryRequest=null;if(current())control.disabled=false;}
  };
  void block.readInventory();
}
async function refreshGeekInventory() {
  if(page!=='Automations'||document.hidden===true)return;
  await $('geek-rule-inventory')?.readInventory?.();
}
function renderCoreRules(content) {
  const session=++coreRuleSession,block=el('section',null,{class:'surface',id:'core-rules'});
  add(block,el('h2','本机习惯'),el('p','确认后由此Mac响应新状态变化。由这台 Mac 执行。确认安装后，只响应动作窗口内的新状态变化；外部习惯仍在原平台管理。',{class:'muted'}));
  const list=el('div','正在读取…',{id:'core-rule-list'}),editor=el('div'),runtime=el('div',null,{id:'core-rule-runtime'});
  add(block,list,editor,runtime);content.append(block);
  const current=()=>session===coreRuleSession&&page==='Automations'&&snapshot?.mode==='ha-read-only'&&block.isConnected;
  const update=async(path,body,control)=>{
    if(coreRuleBusy||!current())return;coreRuleBusy=true;control.disabled=true;
    try{
      const result=await api(path,{method:'POST',body:JSON.stringify(body)});
      if(!current())return;
      if(result.new)lastCoreRulePlan=result;
      render();
    }catch(_){if(current()){$('notification').textContent='请求未完成，请刷新；不自动重试或补发。请求未完成，请刷新习惯记录后重试。不会自动重试或补发动作。';control.disabled=false;}}
    finally{coreRuleBusy=false;}
  };
  Promise.all([api('/api/rules/core/records'),api('/api/rules/core/runtime')]).then(([report,state])=>{
    if(!current())return;list.replaceChildren();
    for(const record of report.records??[]){
      const row=el('article',null,{class:'automation-row','data-core-rule':record.id});
      add(row,el('h3',record.rule.intent),badge(record.enabled?'已安装 · 本机执行':'已停用 · 版本保留'));
      for(const action of record.rule.actions)row.append(el('p',ruleActionLabel(action)));
      row.append(detail('触发保护',{trigger:record.rule.trigger,conditions:record.rule.conditions,safety:record.rule.safety,execution:record.rule.execution}));list.append(row);
    }
    if(!report.records?.length)list.append(el('p','还没有安装本机习惯。'));
    const history=el('details',null,{class:'details'});history.append(el('summary',copyName('Friday-1299')));
    for(const plan of report.plans??[])history.append(button(`${plan.new.rule.intent} · ${corePlanStatus(plan.status)}`,()=>{lastCoreRulePlan=plan;render();},{class:'text-button','data-core-plan':plan.id}));
    list.append(history);
    if(report.available&&connected)renderCoreRuleEditor(editor,report,current);
    else editor.append(note('预览需HA持续连接及设备控制授权。需要可用的 HA 连续连接与设备控制授权，才能预览新习惯。'));
    if(lastCoreRulePlan)renderCoreRulePlan(editor,lastCoreRulePlan,update,report.available&&connected&&lastCoreRulePlan.boot_id===report.boot_id&&lastCoreRulePlan.context_fingerprint===report.fingerprint,report.owner_available);
    renderCoreRuleRuntime(runtime,state,update);
  }).catch(()=>{if(current())list.replaceChildren(note('暂时无法读取本机习惯。',true));});
}
function corePlanStatus(status){return {validated:'等待模拟验证',awaiting_approval:'等待确认安装',verified:'本机配置已核对',rolled_back:'配置已恢复',failed:'未完成',expired:'预览已过期'}[status]??'需要核对';}
function renderCoreRulePlan(content,plan,update,available,ownerAvailable){
  const preview=el('section',null,{class:'notice',id:'core-rule-plan'}),rule=plan.new.rule;
  add(preview,el('h3',rule.intent),badge(corePlanStatus(plan.status)),el('p',`触发设备与状态变化。触发：${entityName({id:rule.trigger.entity})} · ${display(rule.trigger.from_value)} → ${display(rule.trigger.to_value)}`));
  for(const action of rule.actions)preview.append(el('p',ruleActionLabel(action)));
  if(plan.old){const ids=new Set(rule.actions.map(a=>a.id));preview.append(el('p',`移除：${plan.old.rule.actions.filter(a=>!ids.has(a.id)).map(ruleActionLabel).join('、')||'无'}`));}
  preview.append(el('p',`请核对权限与本次操作范围。保护：睡眠或手动接管时停止；状态未知、事件超过 ${rule.execution.max_event_age_ms/1000} 秒、连接重建或权限变化时不执行。配置恢复不会撤销已经发生的设备动作。`,{class:'muted'}));
  preview.append(detail('规则来源',{old:plan.old,new:plan.new,compilation:plan.compilation}));
  if(plan.trial){
    preview.append(el('p',plan.trial.outcome==='matched'?'仅供模拟，不能证明实际效果。simulated · 假设清醒窗口的模拟通过，未发送设备动作。':'仅供模拟，不能证明实际效果。simulated · 模拟被条件或质量检查抑制，不能安装。'));
    for(const reason of plan.trial.reasons??[]){
      if(reason.startsWith('unknown_target_state:'))preview.append(el('p',`请核对连接状态与使用范围。HA 未提供 ${entityName({id:reason.slice(21)})} 的可用状态。请先核对连接与原平台状态。`));
      if(reason.startsWith('unknown_numeric_quality:'))preview.append(el('p',`此设备没有有效读数，不能用于判断。${entityName({id:reason.slice(24)})} 没有有效的测量证据，不能用于这次条件判断。`));
    }
    preview.append(detail('模拟结果',plan.trial));
  }
  const controls=el('div',null,{class:'settings-actions'});
  const operation=(label,id,path,extra={})=>{const control=button(label,()=>update(path,{plan_id:plan.id,...extra},control),{id});control.disabled=!(id==='core-rule-rollback'?ownerAvailable:available)||coreRuleBusy||(['validated','awaiting_approval'].includes(plan.status)&&Date.parse(plan.expires_at)<=Date.now());controls.append(control);};
  if(plan.status==='validated')operation('模拟验证，不操作设备','core-rule-trial','/api/rules/core/plans/trial');
  if(plan.status==='awaiting_approval')operation(copyName('Friday-1319'),'core-rule-apply','/api/rules/core/plans/apply',{approval_digest:plan.approval_digest});
  if(!available&&['validated','awaiting_approval'].includes(plan.status))preview.append(el('p','来源程序已变，旧预览只读，请重新生成。来源或进程已变化，旧预览只能查看。请重新生成预览。'));
  if(plan.status==='verified'){preview.append(el('p','配置已安装，重开授权窗口才响应新触发'));operation(copyName('Friday-1322'),'core-rule-rollback','/api/rules/core/plans/rollback');}
  preview.append(controls);content.append(preview);
}
function renderCoreRuleEditor(content,report,current){
  const form=el('form',null,{id:'core-rule-form',class:'settings-form form-grid'});
  const field=(label,node)=>form.append(add(el('label',label),node));
  const name=el('input',null,{id:'core-rule-name',required:true,maxlength:128,placeholder:'例如：门打开时开启玄关灯'});name.value=coreRuleDraft.name;field('习惯名称',name);
  const triggers=snapshot.entities.filter(e=>e.capabilities.some(c=>c.access==='event'&&c.support==='supported'&&c.name.endsWith('.transition')));
  const triggerDevices=inventoryEntities().filter(d=>triggers.some(e=>d.function_ids?d.function_ids.includes(e.id):d.id===e.id));
  const triggerDevice=el('select',null,{id:'core-rule-trigger-device',required:true});triggerDevice.append(el('option','触发设备',{value:''}));
  for(const d of triggerDevices)triggerDevice.append(el('option',`${roomName(d.room)} · ${entityLabel(d)}`,{value:d.id}));triggerDevice.value=triggerDevices.some(d=>d.id===coreRuleDraft.triggerDevice)?coreRuleDraft.triggerDevice:'';field('触发设备',triggerDevice);
  const trigger=el('select',null,{id:'core-rule-trigger',required:true});trigger.append(el('option','触发功能',{value:''}));
  const drawTriggers=()=>{trigger.replaceChildren();trigger.append(el('option','触发功能',{value:''}));const d=triggerDevices.find(item=>item.id===triggerDevice.value);const functions=d?triggers.filter(e=>d.function_ids?d.function_ids.includes(e.id):d.id===e.id):[];for(const e of functions)trigger.append(el('option',entityLabel(e),{value:e.id}));trigger.value=functions.some(e=>e.id===coreRuleDraft.trigger)?coreRuleDraft.trigger:'';};drawTriggers();field('触发功能',trigger);
  const edge=el('select',null,{id:'core-rule-edge'});edge.append(el('option','off → on（来源状态）',{value:'off:on'}),el('option','on → off（来源状态）',{value:'on:off'}));edge.value=coreRuleDraft.edge;field('状态变化',edge);
  const devices=inventoryEntities().filter(d=>controllableFunctions(d).some(e=>(snapshot.action_targets??[]).includes(e.id)));
  const device=el('select',null,{id:'core-rule-device',required:true});device.append(el('option','选择设备',{value:''}));
  for(const d of devices)device.append(el('option',`${roomName(d.room)} · ${entityLabel(d)}`,{value:d.id}));device.value=devices.some(d=>d.id===coreRuleDraft.device)?coreRuleDraft.device:'';field('动作设备',device);
  const target=el('select',null,{id:'core-rule-target',required:true});field('设备功能',target);
  const operation=el('select',null,{id:'core-rule-operation',required:true});field('操作',operation);
  const fields=el('div',null,{class:'control-field full'});form.append(fields);
  let parameter=null,brightness=null,key=null;
  const invalidate=()=>{lastCoreRulePlan=null;$('core-rule-plan')?.remove();};
  const remember=()=>{
    coreRuleDraft.name=name.value;coreRuleDraft.triggerDevice=triggerDevice.value;coreRuleDraft.trigger=trigger.value;coreRuleDraft.edge=edge.value;
    coreRuleDraft.device=device.value;coreRuleDraft.target=target.value;coreRuleDraft.operation=operation.value;
    if(parameter)coreRuleDraft.values[operation.value]=parameter.value;
    if(brightness)coreRuleDraft.brightness=brightness.value;
    invalidate();
  };
  const drawValue=()=>{
    fields.replaceChildren();parameter=null;brightness=null;
    const entity=snapshot.entities.find(e=>e.id===target.value),cap=entity?.capabilities.find(c=>c.name===operation.value);
    if(!cap)return;
    key={'light.set':'power','switch.set':'power','fan.set':'power','fan.percentage':'percentage','climate.mode':'hvac_mode','climate.temperature':'temperature','cover.position':'position','lock.set':'lock','number.value':'value','select.option':'option'}[cap.name];
    const choices=key==='power'?['on','off']:cap.choices??[];
    if(choices.length){
      parameter=el('select',null,{id:'core-rule-value',required:true});parameter.append(el('option','目标状态',{value:''}));
      for(const v of choices)parameter.append(el('option',({on:'打开',off:'关闭',locked:'锁定',unlocked:'解锁',cool:'制冷',heat:'制热',auto:'自动',dry:'除湿',fan_only:'送风'})[v]??v,{value:v}));
    }else parameter=el('input',null,{id:'core-rule-value',type:'number',required:true,min:cap.minimum,max:cap.maximum,step:cap.step??'any',placeholder:'目标值'});
    parameter.value=coreRuleDraft.values[cap.name]??'';
    if(choices.length&&!choices.includes(parameter.value))parameter.value='';
    parameter.addEventListener('input',remember);parameter.addEventListener('change',remember);fields.append(add(el('label','目标状态'),parameter));
    if(cap.minimum!=null)fields.append(el('p',`HA 范围 ${cap.minimum}–${cap.maximum} ${cap.unit??''}`,{class:'meta'}));
    if(cap.name==='light.set'&&cap.minimum!=null){
      brightness=el('input',null,{id:'core-rule-brightness',type:'number',min:cap.minimum,max:cap.maximum,step:1,placeholder:copyName('Friday-1349')});brightness.value=coreRuleDraft.brightness;brightness.addEventListener('input',remember);
      fields.append(add(el('label',copyName('Friday-1350')),brightness),el('p','真实最低档和无高亮闪烁尚未核验。',{class:'meta'}));
    }
  };
  const drawOperations=()=>{
    operation.replaceChildren();const entity=snapshot.entities.find(e=>e.id===target.value);
    const caps=entity?.capabilities.filter(c=>deviceOperationLabels[c.name]&&c.access==='write'&&c.support==='supported')??[];
    for(const c of caps)operation.append(el('option',deviceOperationLabels[c.name],{value:c.name}));
    operation.value=caps.some(c=>c.name===coreRuleDraft.operation)?coreRuleDraft.operation:caps[0]?.name??'';drawValue();
  };
  const drawTargets=()=>{
    target.replaceChildren();const d=devices.find(item=>item.id===device.value);
    const functions=d?controllableFunctions(d).filter(e=>(snapshot.action_targets??[]).includes(e.id)):[];
    for(const e of functions)target.append(el('option',entityLabel(e),{value:e.id}));
    target.value=functions.some(e=>e.id===coreRuleDraft.target)?coreRuleDraft.target:functions[0]?.id??'';drawOperations();
  };
  device.addEventListener('change',()=>{coreRuleDraft.target='';coreRuleDraft.operation='';drawTargets();remember();});
  triggerDevice.addEventListener('change',()=>{coreRuleDraft.trigger='';drawTriggers();remember();});
  target.addEventListener('change',()=>{coreRuleDraft.operation='';drawOperations();remember();});
  operation.addEventListener('change',()=>{drawValue();remember();});
  for(const node of [name,trigger,edge]){node.addEventListener('input',remember);node.addEventListener('change',remember);}drawTargets();
  const submit=el('button','预览习惯',{id:'core-rule-preview',type:'submit',class:'primary'});submit.disabled=coreRuleBusy||!triggers.length||!devices.length;
  add(form,el('p','新建本机习惯，请先核对已有联动。这里建立一条新习惯，不接管原平台的规则。相同功能已有联动时，请先核对冲突。',{class:'meta full'}),submit);content.append(form);
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(!current()||coreRuleBusy||!parameter?.value||!trigger.value||!target.value||!name.value.trim())return;
    remember();const submitted=JSON.stringify(coreRuleDraft),entity=triggers.find(e=>e.id===trigger.value),cap=entity.capabilities.find(c=>c.access==='event'&&c.name.endsWith('.transition'));
    const [from,to]=edge.value.split(':'),numeric=['percentage','position','temperature','value'].includes(key);
    const desired={[key]:numeric?Number(parameter.value):parameter.value};
    if(brightness?.value&&desired.power==='on')desired.brightness=Number(brightness.value);
    const rule={id:'owned:'+crypto.randomUUID(),revision:1,intent:name.value.trim(),trigger:{type:'state_transition',entity:trigger.value,capability:cap.name.slice(0,-11),from_value:from,to_value:to},conditions:[],actions:[{id:'effect:1',type:'device.set',target:target.value,operation:operation.value,desired}],execution:{preferred_backend:'home_core',max_event_age_ms:3000},simulated:false};
    coreRuleBusy=true;submit.disabled=true;
    try{
      const plan=await api('/api/rules/core/plans',{method:'POST',body:JSON.stringify({rule,expected_context:report.fingerprint,idempotency_key:crypto.randomUUID(),expires_at:new Date(Date.now()+30000).toISOString()})});
      if(current()&&JSON.stringify(coreRuleDraft)===submitted){lastCoreRulePlan=plan;coreRuleBusy=false;render();}
    }catch(_){if(current())$('notification').textContent='预览未完成，设备未改，请刷新来源功能。预览未完成。请刷新来源与功能后重试；设备未改变。';}
    finally{coreRuleBusy=false;if(current())submit.disabled=false;}
  });
}
function renderCoreRuleRuntime(content,state,update){
  const active=state.mode_revision_valid&&Object.values(state.modes??{}).length===2&&Object.values(state.modes).every(v=>Date.parse(v.valid_until)>Date.now());
  coreRuleWindowEndsAt=active?Math.min(...Object.values(state.modes).map(v=>Date.parse(v.valid_until))):null;
  add(content,el('h3','动作窗口'),el('p',!active?'未开启 · 新事件不会发出设备动作':state.modes['home.manual']?.value?copyName('Friday-1357'):state.modes['home.sleep']?.value?'睡眠保护 · 普通规则停止':`清醒测试至 ${time(state.modes['home.sleep'].valid_until)}`,{id:'core-rule-window-status'}));
  const controls=el('div',null,{class:'settings-actions'});
  for(const [label,mode] of [[copyName('Friday-1360'),'awake'],['睡眠保护','sleep'],['手动接管','manual'],['停止窗口','unknown']]){
    const control=button(label,()=>update('/api/rules/core/runtime/mode',{mode,ttl_seconds:60},control),{id:'core-rule-mode-'+mode});control.disabled=(mode!=='unknown'&&!state.available)||coreRuleBusy;controls.append(control);
  }
  const refresh=button('刷新记录',()=>render(),{class:'secondary',id:'core-rule-refresh'});controls.append(refresh);content.append(controls);
  content.append(el('p',`${state.pending_count??0} 项等待中 · 恢复后不补发`,{class:'meta'}));
  for(const run of state.runs??[]){
    const row=el('article',null,{class:'automation-row','data-core-run':run.id});
    add(row,badge({completed:'HA 报告执行完成',cancelled:'已停止',suppressed:'已抑制',unknown_outcome:'结果待核对',waiting:'等待中',running:'执行中'}[run.status]??'需要核对'),el('p',run.real_write_attempted?'已尝试设备操作；物理效果未独立确认。':'没有发出设备动作。'),detail('执行记录',run));
    for(const effect of run.effects??[])if(effect.action_id)row.append(button('核对操作',async()=>{try{lastTrace=await api('/api/traces/'+encodeURIComponent(effect.action_id));go('Activity');}catch(_){$('notification').textContent='暂时无法读取操作记录。';}},{class:'text-button'}));content.append(row);
  }
}
function renderRuleAnalysis(content) {
  const session=++ruleAnalysisSession,block=el('details',null,{class:'surface',id:'rule-analysis'});
  block.open=ruleAnalysisExpanded;
  block.addEventListener('toggle',()=>{if(block.isConnected)ruleAnalysisExpanded=block.open;});
  add(block,el('summary',copyName('Friday-1377')),el('p','先核对分析范围，再审阅返回的变更差异。',{class:'muted'}));
  const form=el('form',null,{id:'rule-analysis-form',class:'action-form'});
  const select=el('select',null,{'aria-label':copyName('Friday-1379'),id:'rule-analysis-record'});
  select.append(el('option','可改习惯',{value:''}));select.value=ruleAnalysisRecord;
  const input=el('textarea',null,{id:'rule-analysis-input','aria-label':'修改要求',maxlength:'1024',rows:'2',placeholder:'例如：以后回家不开空调'});input.value=ruleAnalysisDraft;
  const preview=el('button','审阅范围',{type:'submit',class:'secondary',id:'rule-analysis-preview'});preview.disabled=true;
  const output=el('div',null,{id:'rule-analysis-output',role:'status','aria-live':'polite'});
  add(form,select,input,preview);add(block,form,output);content.append(block);
  let context=null;
  const current=()=>session===ruleAnalysisSession&&page==='Automations'&&block.isConnected;
  const approved=review=>({review_id:review.id,approval_digest:review.approval_digest});
  const cancelLocal=()=>{
    const review=ruleAnalysisReview,plan=ruleAnalysisResult?.plan;
    if(plan?.id===lastRulePlan?.id){lastRulePlan=null;$('rule-plan')?.remove();}
    if(plan?.id===lastCoreRulePlan?.id){lastCoreRulePlan=null;$('core-rule-plan')?.remove();}
    ruleAnalysisReview=null;ruleAnalysisResult=null;ruleAnalysisVersion++;
    if(review)void api('/api/rules/analysis/cancel',{method:'POST',body:JSON.stringify(approved(review))}).catch(()=>{});
    output.replaceChildren();
  };
  input.addEventListener('input',()=>{ruleAnalysisDraft=input.value;cancelLocal();});
  select.addEventListener('change',()=>{ruleAnalysisRecord=select.value;cancelLocal();});
  const show=()=>{
    output.replaceChildren();
    const review=ruleAnalysisReview,result=ruleAnalysisResult;
    if(result){
      output.append(el('p',result.status==='planned'?'候选已转成变更预览，请继续核对差异。':result.status==='clarification'?'还需要明确要移除的动作。':result.status==='needs_review'?'分析结果待核对，本次请求不会自动重试。':result.code==='analysis_controlled_actor_not_authorized'?'模型分析尚未启用。':'没有生成可部署的变更。'));
      if(result.summary)output.append(el('p',result.summary));
      if(result.baseline_comparison==='matched')output.append(el('p','候选与当前确定性解析一致。',{class:'meta'}));
      return;
    }
    if(!review)return;
    add(output,badge(review.context.simulated?'simulated · 示例规则':'本机习惯'),el('p','交给Codex分析，设备变更另审。批准后，将下列文字和习惯标签交给现有 Codex 账号分析。设备变更仍需另行审阅。'));
    const text=el('textarea',null,{'aria-label':'发送全文',readonly:'',rows:'3'});text.value=review.request.text;output.append(text);
    const list=el('ul');
    for(const choice of review.choices)list.append(el('li',`本次移除项与继续保留的动作。${choice.rule_label} · ${choice.action_label}；其余动作：${choice.retained_actions.join('，')}`));
    add(output,list,detail('分析详情',review.request),el('p',`模型 ${review.model} · 有效至 ${time(review.expires_at)}`,{class:'meta'}));
    const analyze=button(copyName('Friday-1397'),async()=>{
      if(ruleAnalysisBusy||Date.parse(review.expires_at)<=Date.now())return;
      const version=ruleAnalysisVersion;ruleAnalysisBusy=true;analyze.disabled=true;preview.disabled=true;
      try{
        const result=await api('/api/rules/analysis/analyze',{method:'POST',body:JSON.stringify(approved(review))});
        if(version!==ruleAnalysisVersion||ruleAnalysisReview?.id!==review.id)return;
        ruleAnalysisResult=result;
        if(result.plan){if(review.context.simulated)lastRulePlan=result.plan;else lastCoreRulePlan=result.plan;}
        if(current())render();
      }catch(_){if(version===ruleAnalysisVersion){ruleAnalysisResult={status:'needs_review'};if(current())show();}}
      finally{ruleAnalysisBusy=false;if(current())preview.disabled=!context?.available;}
    },{class:'primary',id:'rule-analysis-analyze'});
    analyze.disabled=ruleAnalysisBusy||!context?.analysis_enabled||Date.parse(review.expires_at)<=Date.now();
    const cancel=button('取消分析',async()=>{
      const version=++ruleAnalysisVersion;ruleAnalysisBusy=false;
      try{const result=await api('/api/rules/analysis/cancel',{method:'POST',body:JSON.stringify(approved(review))});
        if(version===ruleAnalysisVersion){ruleAnalysisResult=result;if(current())show();}}
      catch(_){if(current())output.replaceChildren(el('p','取消结果待核对，本次请求不会自动重试。'));}
    },{class:'secondary',id:'rule-analysis-cancel'});
    add(output,analyze,cancel);
    if(!context?.analysis_enabled)output.append(el('p','模型分析未启用，范围仅在本机未发送。模型分析尚未启用。当前范围仅保存在本机，尚未发送。',{class:'meta'}));
  };
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(!context?.available||ruleAnalysisBusy||!input.value.trim())return;
    const version=ruleAnalysisVersion,text=input.value,record=select.value;ruleAnalysisBusy=true;preview.disabled=true;
    try{
      const review=await api('/api/rules/analysis/reviews',{method:'POST',body:JSON.stringify({text,expected_context:context.fingerprint,idempotency_key:crypto.randomUUID(),expires_at:new Date(Date.now()+60000).toISOString(),record_ids:record?[record]:[]})});
      if(version!==ruleAnalysisVersion||input.value!==text||select.value!==record){void api('/api/rules/analysis/cancel',{method:'POST',body:JSON.stringify(approved(review))}).catch(()=>{});return;}
      ruleAnalysisReview=review;ruleAnalysisResult=null;if(current()){block.open=true;show();}
    }catch(_){if(current())output.replaceChildren(el('p','范围准备失败，请读取习惯并缩小范围。未能准备分析范围。请读取当前习惯并缩小范围后重试。'));}
    finally{ruleAnalysisBusy=false;if(current())preview.disabled=!context?.available;}
  });
  Promise.all([api('/api/rules/analysis/context'),api(snapshot.mode==='simulator'?'/api/rules/managed':'/api/rules/core/records')]).then(([report,listing])=>{
    if(!current())return;context=report;
    for(const record of listing.records??[])if(record.enabled)select.append(el('option',record.rule.intent,{value:record.id}));
    select.value=ruleAnalysisRecord;preview.disabled=!report.available||ruleAnalysisBusy;input.disabled=!report.available;
    if(ruleAnalysisReview&&ruleAnalysisReview.context.fingerprint!==report.fingerprint)cancelLocal();
    if(ruleAnalysisReview){block.open=true;show();}
    else if(!report.available)output.append(el('p','当前没有可编辑的本机规则来源。'));
  }).catch(()=>{if(current())output.append(el('p','暂时无法读取分析范围。'));});
}
function renderRuleTextChanges(content) {
  const session=++ruleTextSession,block=el('section',null,{class:'surface',id:'rule-text-changes'});
  add(block,el('h2','修改习惯'),el('p','先核对差异与模拟，再确认应用。先查看差异和模拟结果，再确认应用。当前支持移除已纳管习惯中的一个开灯或开空调动作。',{class:'muted'}));
  const form=el('form',null,{id:'rule-text-form',class:'action-form'});
  const input=el('textarea',null,{id:'rule-text-input','aria-label':'选择习惯',maxlength:'1024',rows:'2',placeholder:'例如：以后回家不开空调'});input.value=ruleTextDraft;
  input.addEventListener('input',()=>{
    ruleTextDraft=input.value;lastRuleTextResult=null;lastRulePlan=null;
    $('rule-plan')?.remove();$('rule-text-result')?.replaceChildren();
  });
  const submit=el('button','预览变更',{type:'submit',class:'primary',id:'rule-text-submit'});submit.disabled=true;
  const output=el('div',null,{role:'status','aria-live':'polite',id:'rule-text-result'});
  add(form,input,submit);add(block,form,output);content.append(block);
  let context=null,busy=false;
  const current=()=>session===ruleTextSession&&page==='Automations'&&block.isConnected;
  const messages={rule_context_changed:'习惯或家庭来源已变化，请重新生成预览。',text_request_conflict:'这次请求已变化，请重新生成预览。',text_request_expired:'请求已过期，请重新生成预览。',text_plan_expired:'变更预览已过期，请重新生成。',structured_planner_unavailable:'规划未接通，目前只可移除指定动作。这项修改需要的规划能力尚未接通。当前可以移除已纳管习惯中的开灯或开空调动作。',native_rule_source_unavailable:'真实规则尚不可编辑，请在原平台调整。',no_matching_managed_action:'本机习惯无匹配动作，外部规则可能不可见。已纳管习惯中没有找到匹配动作；外部规则仍可能不可见。',whole_rule_disable_requires_separate_review:'此为最后动作，停用整条习惯需另行审阅。这个动作是习惯的最后一项。停用整条习惯需要单独审阅。',text_scope_too_broad:'相关动作过多，请缩小范围。',source_conflict:'原规则已变化，请重新读取。'};
  const show=result=>{
    output.replaceChildren();
    if(result.status==='planned')return output.append(el('p','请核对移除项、保留项与触发条件。已生成变更预览。请核对下面的移除项、保留项和触发条件。'));
    output.append(el('p',messages[result.code]??(result.status==='clarification'?'有多个相关动作，请选择本次修改的一项。找到多个可能相关的动作，请选择本次要修改的一项。':'暂时无法生成这项变更。')));
    if(result.status==='clarification')for(const choice of result.choices){
      const pick=button(`${choice.rule_label} · ${choice.action_label}`,()=>send(choice),{class:'secondary','data-rule-text-choice':choice.action_id});output.append(pick);
    }
  };
  const send=async(choice=null)=>{
    if(!context?.available||busy||!input.value.trim())return;
    busy=true;submit.disabled=true;const submittedText=input.value;
    lastRuleTextResult=null;lastRulePlan=null;$('rule-plan')?.remove();output.replaceChildren();
    try{
      const result=await api('/api/rules/text/plan',{method:'POST',body:JSON.stringify({text:submittedText,expected_context:context.fingerprint,idempotency_key:crypto.randomUUID(),expires_at:new Date(Date.now()+30000).toISOString(),...(choice?{record_id:choice.record_id,action_id:choice.action_id,expected_fingerprint:choice.expected_fingerprint}:{})})});
      if(!current()||input.value!==submittedText)return;
      lastRuleTextResult=result;
      if(result.plan){lastRulePlan=result.plan;render();$('rule-plan')?.scrollIntoView({block:'nearest'});}else show(result);
    }catch(_){if(current())output.replaceChildren(el('p','预览未完成，请读取习惯；不会自动应用。预览未完成，请读取当前习惯后重试。不会自动应用。'));}
    finally{busy=false;if(current())submit.disabled=!context?.available;}
  };
  form.addEventListener('submit',event=>{event.preventDefault();ruleTextDraft=input.value;void send();});
  api('/api/rules/text/context').then(report=>{
    if(!current())return;context=report;submit.disabled=!report.available;input.disabled=!report.available;
    if(!report.available)output.append(el('p','真实规则尚不可编辑，请在原平台调整。'));
    else if(lastRuleTextResult&&lastRuleTextResult.context.fingerprint===report.fingerprint)show(lastRuleTextResult);
    else output.append(el('p','仅供模拟，不能证明实际效果。示例家庭 · 变更只影响模拟规则。进门联动是否对应“回家”需要你核对。',{class:'meta'}));
  }).catch(()=>{if(current())output.append(el('p','暂时无法读取可修改的习惯。'));});
}
const ruleStatus = {
  validated:'等待模拟验证',awaiting_approval:'模拟已通过，等待确认',staging:'正在创建禁用版本',
  staged_disabled:'新版本已暂存，尚未启用',pausing_old:'正在暂停旧版本',old_paused:'旧版本已暂停',
  activating:'正在启用新版本',activated:'新版本已启用，等待核对',verified:'示例配置已验证',
  needs_reconcile:copyName('Friday-1434'),rolling_back:'正在恢复旧配置',rolled_back:'旧配置已恢复',
  failed:'变更未应用',expired:'预览已过期'
};
function ruleHeading(rule) { return rule.id === 'managed.sim.arrival' ? '进门示例' : rule.intent; }
function ruleActionLabel(action) {
  if (action.type === 'light.set' || action.type === 'climate.set') return `${action.power === 'on' ? '打开' : '关闭'} ${entityName({id:action.target})}`;
  if(action.type==='device.set')return `${entityName({id:action.target})} · ${deviceOperationLabels[action.operation]??action.operation} → ${Object.entries(action.desired).map(([k,v])=>`${k}: ${display(v)}`).join('，')}`;
  return {delay:'等待',cancel_pending:'取消本习惯的待执行动作','feedback.speak':'语音反馈'}[action.type] ?? '动作';
}
async function ruleOperation(path, body, control) {
  control.disabled = true;
  try {
    lastRulePlan = await api(path,{method:'POST',body:JSON.stringify(body)});
    snapshot = await api('/api/snapshot');connected = true;render();
  } catch (_) {
    $('notification').textContent = '变更未完成，先刷新记录核对状态。变更请求未完成。请先刷新记录，确认当前状态后再操作。';
    control.disabled = false;
  }
}
function renderManagedRulePlan(block, plan) {
  const preview = el('section',null,{class:'notice',id:'rule-plan'});
  const kept = new Set(plan.new_rule.actions.map(action => action.id));
  const removed = plan.old.rule.actions.filter(action => !kept.has(action.id));
  add(preview,el('h3','变更预览'),el('p','仅改示例家庭，不改实物或已有米家规则。仅用于示例家庭，不会写入真实设备或已有米家规则。'),
      el('p',`移除：${removed.map(ruleActionLabel).join('、')}`),el('p',`保留：${plan.new_rule.actions.map(ruleActionLabel).join('、')}`),
      badge(ruleStatus[plan.status] ?? '待核对'));
  const trigger=plan.new_rule.trigger;
  preview.append(el('p',trigger.type==='state_transition'?`触发设备与前后状态。触发：${entityName({id:trigger.entity})} 从 ${display(trigger.from_value)} 变为 ${display(trigger.to_value)}`:`触发：${entityName({id:trigger.entity})} 的 ${trigger.capability} 事件`));
  preview.append(detail(copyName('Friday-1453'),{trigger,conditions:plan.new_rule.conditions,on_unknown:plan.new_rule.on_unknown,safety:plan.new_rule.safety,execution:plan.new_rule.execution}));
  if (plan.simulation) preview.append(el('p',plan.simulation.outcome === 'matched' ? '只生成模拟保留动作，未执行真实操作。模拟触发生成了保留动作；未调用真实执行器。' : '模拟被保护或数据质量条件抑制。'));
  if (plan.code === 'source_conflict' || plan.code === 'source_edited_no_automatic_overwrite') preview.append(note('原配置已变，不覆盖，请重新生成预览。原配置已经变化。本次变更不会覆盖它，请重新查看并生成预览。',true));
  const controls = el('div',null,{class:'settings-actions'});
  const operation = (label,id,path,extra={}) => {
    const control = button(label,() => ruleOperation(path,{plan_id:plan.id,...extra},control),{id});
    controls.append(control);
  };
  if (plan.status === 'validated') operation(copyName('Friday-1457'),'rule-simulate','/api/rules/plans/simulate');
  if (plan.status === 'awaiting_approval') operation(copyName('Friday-1458'),'rule-apply','/api/rules/plans/apply',{approval_digest:plan.approval_digest});
  if (['needs_reconcile','activated'].includes(plan.status)) operation(copyName('Friday-1459'),'rule-reconcile','/api/rules/plans/reconcile');
  if (['verified','needs_reconcile','activated','old_paused','staged_disabled'].includes(plan.status)) operation('恢复配置','rule-rollback','/api/rules/plans/rollback');
  controls.append(button('刷新记录',async () => {
    try {lastRulePlan = await api('/api/rules/plans/'+encodeURIComponent(plan.id));render();}
    catch (_) {$('notification').textContent = '暂时无法读取变更记录。';}
  },{class:'secondary'}));
  preview.append(controls);
  preview.append(detail('变更阶段',plan.history.map(item => ({stage:ruleStatus[item.status] ?? item.status,at:item.at}))));
  block.append(preview);
}
function renderManagedRules(content) {
  const block = el('section',null,{class:'surface',id:'managed-rules'});
  add(block,el('h2','示例习惯'),el('p','先审阅差异模拟，再确认；旧版保留可恢复。先查看差异和模拟结果，再确认应用；旧版本会保留以便恢复。',{class:'muted'}));
  const list = el('div','正在读取…',{id:'managed-rule-list'});block.append(list);content.append(block);
  renderSimulatedRuleRuntime(content);
  if (lastRulePlan) renderManagedRulePlan(block,lastRulePlan);
  api('/api/rules/managed').then(report => {
    if (page !== 'Automations' || !list.isConnected) return;
    list.replaceChildren();
    if (!report.available) return list.append(el('p','示例规则服务暂不可用。'));
    for (const item of report.records.filter(record => record.enabled)) {
      const row = el('article',null,{class:'automation-row'});
      add(row,el('h3',ruleHeading(item.rule)),badge('已启用 · 模拟'));
      for (const action of item.rule.actions) {
        const line = el('div',null,{class:'health-row'});line.append(el('span',ruleActionLabel(action)));
        if (item.rule.actions.length > 1 && ['light.set','climate.set'].includes(action.type)) {
          const remove = button(action.type === 'climate.set' ? copyName('Friday-1469') : copyName('Friday-1470'),() => ruleOperation(
            '/api/rules/plans/remove-action',{record_id:item.id,action_id:action.id,expected_fingerprint:item.fingerprint,
              idempotency_key:crypto.randomUUID()},remove),{class:'secondary','data-remove-action':action.id});
          line.append(remove);
        }
        row.append(line);
      }
      list.append(row);
    }
    const history = el('details',null,{class:'details'});history.append(el('summary','最近变更'));
    for (const plan of report.deployments) history.append(button(`${ruleHeading(plan.new_rule)} · ${ruleStatus[plan.status] ?? '待核对'}`,() => {
      lastRulePlan = plan;render();
    },{class:'text-button'}));
    list.append(history);
  }).catch(() => {if (page === 'Automations' && list.isConnected) list.replaceChildren(el('p','暂时无法读取示例习惯。'));});
}
function renderSimulatedRuleRuntime(content) {
  const block=el('section',null,{class:'surface',id:'simulated-rule-runtime'});
  add(block,el('h2','示例记录'),el('p','仅改变模拟家庭状态，不操作真实设备。这里会实际改变 Simulator 状态，所有数据与动作均为 simulated；不操作真实设备。',{class:'muted'}));
  const output=el('div',null,{role:'status','aria-live':'polite',id:'rule-runtime-status'});
  const controls=el('div',null,{class:'settings-actions'});
  const showRuntime=report=>{
    if(page!=='Automations'||!output.isConnected)return;
    output.replaceChildren();
    if(!report.available){output.append(el('p','示例运行点暂不可用。'));return;}
    const now=Date.now(),mode=report.modes ?? {};
    const valid=name=>mode[name]&&Date.parse(mode[name].valid_until)>now;
    const modeLabel=!valid('home.sleep')||!valid('home.manual') ? '保护模式未知，动作会被抑制' : mode['home.manual'].value ? '接管保护' : mode['home.sleep'].value ? '睡眠保护' : copyName('Friday-1480');
    add(output,el('p',modeLabel),el('p',`待执行：${report.pending_count ?? 0} 项`,{class:'meta'}));
    const states={running:'正在执行',waiting:'等待延迟',completed:copyName('Friday-1484'),cancelled:'待执行动作已取消',suppressed:copyName('Friday-1486'),unknown_outcome:'结果未知，不会自动重试'};
    for(const run of (report.runs ?? []).slice(0,5)){
      const row=el('article',null,{class:'automation-row','data-rule-run-id':run.id});
      add(row,el('p',states[run.status] ?? '状态待核对'),el('p',`${run.effects.length} 个模拟动作 · ${time(run.created_at)}`,{class:'meta'}));
      row.append(button('执行记录',async()=>{
        try {const trace=await api('/api/rules/runs/'+encodeURIComponent(run.id));row.append(detail('模拟执行记录',trace));}
        catch (_) {$('notification').textContent='暂时无法读取执行记录。';}
      },{class:'text-button'}));
      output.append(row);
    }
  };
  const operation=(label,id,path,body)=>{
    const control=button(label,async()=>{
      control.disabled=true;
      try {
        const report=await api(path,{method:'POST',body:JSON.stringify(body)});
        if(page!=='Automations'||!output.isConnected)return;
        if(report.snapshot){snapshot=report.snapshot;connected=true;renderSourceStatus();}
        showRuntime(report.runtime ?? report);
      } catch (_) {settingsMessage(output,'示例未完成，请刷新状态，不自动重试。示例请求未完成，请读取当前状态；不会自动重试。',true);}
      finally {control.disabled=false;}
    },{id});controls.append(control);
  };
  operation('清醒模拟','rule-runtime-awake','/api/rules/runtime/mode',{mode:'awake'});
  operation(copyName('Friday-1495'),'rule-runtime-sleep','/api/rules/runtime/mode',{mode:'sleep'});
  operation('接管模拟','rule-runtime-manual','/api/rules/runtime/mode',{mode:'manual'});
  operation('示例开门','rule-runtime-open','/api/rules/runtime/contact',{contact:'open'});
  operation('示例关门','rule-runtime-close','/api/rules/runtime/contact',{contact:'closed'});
  controls.append(button('读取状态',async()=>{
    try {showRuntime(await api('/api/rules/runtime'));}
    catch (_) {if(output.isConnected)settingsMessage(output,'暂时无法读取示例执行状态。',true);}
  },{id:'rule-runtime-refresh',class:'secondary'}));
  add(block,controls,output,el('p','请先核对恢复范围与原有内容。恢复清醒、恢复质量或重启不会补发旧事件。需要再次关门、开门产生新事件；这些门事件不代表真实人员到家。外部米家规则仍不受此执行点约束。',{class:'muted'}));
  content.append(block);
  api('/api/rules/runtime').then(report=>{if(page==='Automations'&&output.isConnected)showRuntime(report);}).catch(()=>{if(output.isConnected)settingsMessage(output,'暂时无法读取示例执行状态。',true);});
}
function renderSystem(content) {
  const health = el('section',null,{class:'surface system-health'});add(health,el('h2','运行状态'),el('p','正在读取…',{id:'doctor-report',class:'muted'}));content.append(health);
  api('/api/doctor').then(report => {
    const block = $('doctor-report'); if (page !== 'System' || !block?.isConnected) return;
    block.replaceChildren();
    const labels = {ha:'Home Assistant',geek:'米家极客版',camera:'Miloco · 房间感知',nas:copyName('Friday-1505'),arl:copyName('Friday-1506'),laya:'ARL · Laya',codex:copyName('Friday-1507'),rules:'规则运行',room_output:'房间语音'};
    const statuses = {available:'可用',settings_oauth_readonly_activation:'可连接',requires_local_pairing_passcode:'需要配对',account_unbound_license_unresolved:'尚未就绪',readonly_metadata_verified_media_timeline_pending:'可读取目录',audibility_and_offline_test_pending:'尚未就绪',arl_task_contract_pending:'任务决策接口待补齐',arl_provider_required:'等待共享 Provider',shared_sdk:'共享连接组件已接入',not_in_control_path:'尚未接入',simulated_compile_deploy_only:copyName('Friday-1519'),simulated_compile_deploy_execute_only:copyName('Friday-1520')};
    if (report.ha_ingress) {
      const ingress = report.ha_ingress;
      const label = ingress.observation_gap ? '连接缺口' : ingress.continuous_subscription ? copyName('Friday-1522') : copyName('Friday-1523');
      block.append(add(el('div',null,{class:'health-row'}),el('span','家庭状态'),el('span',label,{class:'muted'})));
      block.append(el('p','连接恢复只核对当前状态，不补触发旧动作。',{class:'muted'}));
      if (ingress.diagnostic_journal_available === false) block.append(note('记录暂时无法保存，请检查本机存储。',true));
    }
    for (const [key,label] of Object.entries(labels)) block.append(add(el('div',null,{class:'health-row'}),el('span',label),el('span',statuses[report.capabilities?.[key]] ?? '待检查',{class:'muted'})));
    block.append(detail('诊断详情',report));
  }).catch(() => {if (page === 'System') $('doctor-report')?.replaceChildren(el('p','暂时无法读取，请刷新。'));});
  const providers = el('section',null,{class:'surface provider-list'});add(providers,el('h2','智能服务'),el('p','正在读取…',{class:'muted'}));content.append(providers);
  api('/api/decision/providers').then(report => {if (page === 'System' && providers.isConnected) providers.replaceChildren(el('h2','智能服务'),detail('服务能力',report));}).catch(() => {if (page === 'System' && providers.isConnected) providers.replaceChildren(el('p','暂时无法读取智能服务。'));});
  renderDiagnostics(content);
}
function renderDiagnostics(content) {
  const section=el('section',null,{class:'surface system-health',id:'diagnostics-export-section'});
  const output=el('p','正在读取导出设置…',{id:'diagnostics-export-status',class:'muted',role:'status','aria-live':'polite'});
  const download=button(copyName('Friday-1535'),async()=>{
    download.disabled=true;section.setAttribute('aria-busy','true');output.textContent='正在生成诊断包…';
    let objectUrl=null;
    try {
      const response=await fetch('/api/diagnostics/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({redacted:true})});
      if(!response.ok){if(section.isConnected&&page==='System')output.textContent=response.status===429?'诊断正在生成，请稍后重试。':'暂时无法导出诊断。';return;}
      if(response.headers.get('content-type')!=='application/zip')throw Error('Unexpected diagnostics response');
      const file=await response.blob();
      if(file.size>65536)throw Error('Diagnostics budget exceeded');
      if(!section.isConnected||page!=='System')return;
      objectUrl=URL.createObjectURL(file);
      const link=el('a',null,{href:objectUrl,download:'friday-diagnostics.zip',hidden:''});section.append(link);
      try {link.click();} finally {link.remove();}
      const completedUrl=objectUrl;objectUrl=null;
      setTimeout(()=>URL.revokeObjectURL(completedUrl),60000);
      output.textContent='诊断包已交给浏览器下载。';
    } catch (_) {if(section.isConnected&&page==='System')output.textContent='暂时无法导出诊断。';}
    finally {if(objectUrl)URL.revokeObjectURL(objectUrl);download.disabled=false;section.setAttribute('aria-busy','false');}
  },{id:'diagnostics-export-download',class:'secondary'});
  download.disabled=true;
  add(section,el('h2','本机诊断'),el('p','请核对连接状态与使用范围。包含版本、连接状态和汇总计数；登录信息、设备名称与家庭内容不包含。',{class:'muted'}),
      el('p','服务端不留副本，下载文件由你管理。',{class:'muted'}),download,output);
  content.append(section);
  api('/api/diagnostics').then(policy=>{
    if(page!=='System'||!section.isConnected)return;
    if(policy.redacted!==true||policy.server_retention_seconds!==0||policy.max_bytes!==65536||policy.automatic_upload!==false)throw Error('Unqualified diagnostics policy');
    download.disabled=false;output.textContent='本地生成，最多 64 KB。';
  }).catch(()=>{if(page==='System'&&section.isConnected)output.textContent='导出服务暂不可用。';});
  const runtime=el('div',null,{id:'runtime-diagnostics'});
  const readRuntime=button('查看运行日志',async()=>{
    readRuntime.disabled=true;
    try {
      const report=await api('/api/diagnostics/runtime');
      if(page!=='System'||!runtime.isConnected)return;
      if(report.schema!==1||report.redacted!==true||report.automatic_upload!==false||!Array.isArray(report.events)||report.events.length>64)throw Error('Invalid runtime diagnostics');
      runtime.replaceChildren(el('p',report.disk_available===false?'日志文件暂不可写；下方仅显示当前进程记录。':'日志在本机轮转，最多 768 KB；这里只显示最近 64 条安全事件。',{class:'muted'}),detail('运行事件',report.events));
    } catch (_) {if(page==='System'&&runtime.isConnected)runtime.replaceChildren(el('p','运行诊断暂不可用。'));}
    finally {readRuntime.disabled=false;}
  },{id:'runtime-diagnostics-read',class:'secondary'});
  add(section,readRuntime,runtime);
}
function scenarioSelector(content) {
  if (snapshot.mode !== 'simulator') return;
  const select = el('select',null,{id:'scenario'});
  for (const [value,label] of [['baseline','正常状态'],['calibration-training','相对校准 · 明确模拟的成对测量'],['stale-temperature','温度过期 · 未校准替代源'],['calibrated-fallback','温度过期 · 相对校准替代源'],['frozen-temperature','温度冻结 · 独立来源持续变化'],['peer-disagreement',copyName('Friday-1550')],['all-sources-bad','来源全部异常'],['recovered','连接恢复 · 新测量不足'],['recovery-hold','已恢复 · 暂缓切回主源'],['cache-recovery',copyName('Friday-1554')],['watch-window','三条测量尚未跨过观察期'],['battery-stale',copyName('Friday-1556')],['wrong-ac-mode',copyName('Friday-1557')],['calibration-expired','相对校准已过期'],['transport-gap','连接中断']]) select.append(el('option',label,{value}));
  select.value = snapshot.simulator_scenario ?? selectedScenario;
  const load = button('加载场景',async () => {
    load.disabled = true;try {const requested = select.value;snapshot = await api('/api/simulator/scenario/'+requested,{method:'POST'});selectedScenario = requested;loadedScenario = requested;connected = true;lastTrace = null;render();}catch(error){$('notification').textContent = error.message;load.disabled = false;}
  },{id:'load-scenario'});
  content.append(add(el('div',null,{class:'toolbar'}),el('label','测试场景',{for:'scenario'}),select,load,el('span',snapshot.simulator_scenario ?? loadedScenario ?? 'baseline',{id:'loaded-scenario',class:'meta'})));
}
function renderDeveloper(content) {scenarioSelector(content);add(content,detail('物理设备',snapshot.devices ?? []),detail('设备来源',snapshot.entities),detail('房间读数',snapshot.derived_entities ?? []),detail('家庭状态',snapshot.states),detail('读数质量',snapshot.quality_sources ?? []),detail('观察记录',snapshot.observations),detail('事件记录',snapshot.events),detail('快照信息',{mode:snapshot.mode,version:snapshot.version,generated_at:snapshot.generated_at}));}
const settingsLabels = {
  check_expired: '检查已过期，请重新检查',
  not_configured: '未配置', mount_required: '需要挂载目录', readable: '目录可读取',
  path_unavailable: copyName('Friday-1574'), symlink_path_rejected: '请选择实际目录',
  probe_failed: '检查失败', probe_unavailable: '暂时无法检查',
  service_login_required: '请登录本地服务', account_login_required: '请登录账号',
  service_unavailable: '本地服务不可用', service_error: copyName('Friday-1581'),
  service_protocol_error: '无法识别服务响应', bound_stream_unverified: '账号已绑定',
  discarded_configuration_changed: '配置已更改，请重新检查',
  readonly_inventory_collected: '已读取设备与规则', pairing_required: '请先配对网关',
  probe_busy: copyName('Friday-1587'), unsupported_gateway_address: '请输入本地网关 IP 地址',
};
function settingsStatus(report) {
  const value = report?.status;
  return settingsLabels[value] ?? (typeof value === 'string' ? value.replaceAll('_', ' ') : '尚未检查');
}
function settingsField(form, label, id, value, attributes = {}, help = '') {
  const input = el('input', null, {id, name: id, type: 'text', 'aria-label':label, ...attributes});
  input.value = value ?? '';
  const wrapper = add(el('label', label, {for: id}), input);
  if (help) {
    const hint = el('span', help, {id: id + '-help', class: 'meta'});
    input.setAttribute('aria-describedby', id + '-help'); wrapper.append(hint);
  }
  form.append(wrapper); return input;
}
function settingsSection(content, title, description, id) {
  const section = el('section', null, {class: 'settings-section', id, 'aria-labelledby': id + '-title'});
  section.append(el('h2', title, {id: id + '-title'})); if (description) section.append(el('p', description, {class:'muted'}));
  const form = el('form', null, {class: 'form-grid settings-form', id: id + '-form'});
  const controls = el('div', null, {class: 'settings-actions full'});
  const output = el('div', '', {id: id + '-result', class: 'settings-result full', role: 'status', 'aria-live': 'polite'});
  add(section, form, controls, output); content.append(section);
  return {section, form, controls, output};
}
function settingsMessage(output, message, error = false) {
  output.replaceChildren(el('p', message, {class: error ? 'settings-error' : ''}));
}
function settingsButton(block, label, id, action) {
  const button = el('button', label, {type: 'button', id});
  button.addEventListener('click', async () => {
    button.disabled = true; block.output.setAttribute('aria-busy', 'true');
    settingsMessage(block.output, '处理中');
    try { await action(); }
    catch (_) {
      const message = id === 'settings-ha-activate'
        ? '连接失败，请检查授权和网络。'
        : '请求失败，请检查连接后重试。';
      settingsMessage(block.output, message, true);
    }
    finally { button.disabled = false; block.output.setAttribute('aria-busy', 'false'); }
  });
  block.controls.append(button); return button;
}
function settingsSave(block, id, patch) {
  const submit = el('button', '保存', {type: 'submit', form: id + '-form', id: id + '-save', class: 'primary'});
  block.controls.append(submit);
  block.form.addEventListener('submit', async event => {
    event.preventDefault(); submit.disabled = true; block.output.setAttribute('aria-busy', 'true');
    if (id === 'settings-geek') geekSessionRevision++;
    if (id === 'settings-camera') {cameraMediaRevision++;cameraMediaReport=null;}
    try {
      settingsData = await api('/api/settings', {method: 'PUT', body: JSON.stringify(patch())});
      if (id === 'settings-ha') haStatus = null;
      // Only the server's public config is retained. Blank replacement fields preserve stored identifiers.
      const cameraId = $('settings-camera-id'); if (cameraId) cameraId.value = '';
      const login = $('settings-camera-login');
      const loginHref = safeSettingsLink(settingsData.config.camera_service_url, ['http:', 'https:']);
      if (login) { login.hidden = settingsData.config.camera_media_source==='miot_sdk'||!loginHref; if (loginHref) login.setAttribute('href', loginHref); }
      settingsMessage(block.output, '已保存。');
      if (id === 'settings-nas') {
        archiveResult=null;archiveVersion++;archiveSetupVersion++;
        if(archiveSetupRefresh) void archiveSetupRefresh();
      }
      for (const name of ['ha', 'nas', 'camera', 'geek']) {
        const other = $('settings-' + name + '-result');
        if (other && other !== block.output) settingsMessage(other, '配置已更新，请重新检查。');
      }
      if (id === 'settings-ha') await refreshHaStatus(false);
      if (id === 'settings-camera') await refreshCameraMedia();
    } catch (_) { settingsMessage(block.output, '保存失败，请检查地址或路径。', true); }
    finally { submit.disabled = false; block.output.setAttribute('aria-busy', 'false'); }
  });
}
function savedSettings(block, patch) {
  const unchanged = Object.entries(patch()).every(([key, value]) => (settingsData.config[key] ?? null) === value);
  if (!unchanged) settingsMessage(block.output, '请先保存更改。', true);
  return unchanged;
}
const settingValue = (input) => input.value.trim() || null;
function safeSettingsLink(value, schemes) {
  try {
    const parsed = new URL(value);
    return schemes.includes(parsed.protocol) && !parsed.username && !parsed.password ? value : null;
  } catch (_) { return null; }
}
async function loadSettings(content) {
  const waiting = note('正在读取连接设置…'); content.append(waiting);
  if (!settingsRequest) settingsRequest = Promise.allSettled([api('/api/settings'), api('/api/settings/ha/status')]);
  const results = await settingsRequest; settingsRequest = null;
  if (results[0].status === 'fulfilled') settingsData = results[0].value;
  if (results[0].status === 'fulfilled') geekCredentialAvailable = settingsData.credentials?.geek?.credential_available ?? null;
  if (results[1].status === 'fulfilled') haStatus = results[1].value;
  if (page !== 'Settings') return;
  if (settingsData) { content.replaceChildren(); renderSettings(content); return; }
  waiting.replaceChildren(el('p', '暂时无法读取设置，请重试。'));
  const retry = el('button', '重试', {type: 'button'});
  retry.addEventListener('click', () => { content.replaceChildren(); renderSettings(content); }); waiting.append(retry);
}
function arlRefreshNotice(report) {
  if (!report.metadata_refresh_pending) return '';
  const localBusy = report.metadata_refresh_source === 'local_worker';
  if (report.metadata_freshness === 'last_known') return localBusy ? '本机正在处理另一项工作，保留上次服务状态；这不代表服务断开。' : '正在刷新，保留上次服务状态；本次检查尚未确认。';
  return localBusy ? '本机检查正在进行，尚无最近连接记录。' : '正在检查服务状态，当前尚无最近连接记录。';
}
function showArlConsumers(output, report) {
  const refreshNotice = arlRefreshNotice(report);
  if (refreshNotice) output.append(el('p',refreshNotice,{class:'meta'}));
  output.append(el('p', report.query?.console_readable ? '家庭状态可在本机只读查询。' : '家庭尚未连接，暂不能查询状态。', {class:'meta'}));
  output.append(el('p', report.query?.configured ? '读取消费者已配置；跨应用调用仍待接通。' : '跨应用家庭查询尚未接通。', {class:'meta'}));
  const states = {policy_not_configured:'规则分析的执行许可尚未配置。', codex_unavailable:'Codex 尚未可用于规则分析。', planning_contract_unavailable:'规则分析接口尚未接通。', owned_source_unavailable:'当前没有可分析的本机习惯来源。', review_required:'规则分析需先核对发送内容，再明确批准。'};
  output.append(el('p', states[report.planning?.status] ?? '规则分析的可用状态尚未确认。', {class:'meta'}));
}
function showHaStatus(output) {
  const available = haStatus?.credential_available;
  output.setAttribute('data-credential-state', available === true ? 'available' : available === false ? 'missing' : 'unknown');
  settingsMessage(output, haStatus?.home_readable === true ? '家庭只读连接正常。' : haStatus?.server_acceptance === 'unavailable' ? '现有登录暂不可用，请核对连接授权。' : available === true ? '本机已有登录记录；仍需实际连接确认。' : haStatus?.configured === false ? '请先保存内网地址。' : available === false ? '本机登录记录未就绪，请登录家庭网关。' : '授权状态暂不可确认，请重试。');
  output.append(detail('授权范围', {credential_available: available ?? null, discovery:'局域网 mDNS；发现不等于授权', ingress:copyName('Friday-1607'), hardware:copyName('Friday-1608'), offline:'未验证断网运行', device_writes:snapshot?.action_targets?.length ? '已授权设备能力；预览后显式执行' : 'disabled'}));
  if (location.search?.includes('ha=failed')) output.append(el('p', '登录未完成，先核对已有授权再重登。本次登录未完成。先检查已保存的授权；若登录信息不可用，再重新登录。', {class:'settings-error'}));
}
function renderLocalReadSources(section) {
  const output=el('div',null,{id:'settings-local-read-sources',role:'status','aria-live':'polite'});
  output.append(el('h3','家庭相册'),el('p','正在读取本机连接状态…',{class:'muted'}));
  section.append(output);
  let sequence=0;
  const refresh=()=>{
    const current=++sequence;
    return Promise.allSettled([api('/api/settings/nas/connection'),api('/api/family-media/context'),api('/api/query/contracts')]).then(results=>{
    if(!output.isConnected||current!==sequence)return;
    const nas=results[0].status==='fulfilled'?results[0].value:null;
    const catalog=results[1].status==='fulfilled'?results[1].value:null;
    const consumer=results[2].status==='fulfilled'?results[2].value.consumer_adoption:null;
    const states={not_configured:'未配置',disabled:'已停用',connecting:'正在连接',online:'读取连接在线',backoff:'连接中断，等待重试',paused:'连接已暂停',stopped:'已停止'};
    output.replaceChildren(el('h3','家庭相册'));
    output.append(el('p',withCopy(copyName('Friday-1620')+' · '+(states[nas?.state] ?? '状态暂不可确认'),copyName('Friday-1620'))));
    output.append(el('p',!catalog?'家庭照片与视频：状态暂不可确认。':!catalog.source_ref?'家庭照片与视频：尚未配置直连来源。':catalog.source_online?'照片视频来源可读，明确扫描后才建目录。家庭照片与视频：来源可读取；内容仅在显式扫描后进入目录。':'家庭照片与视频：来源暂不可读取。'));
    output.append(el('p',!consumer?'Mio 查询：状态暂不可确认。':consumer.configured?'每次读取前核对Mio身份与范围。Mio 查询：已组合本机读取入口；身份与范围需在每次读取时核验。':'Mio 查询：尚未接入读取许可。'));
    if(nas?.failure)output.append(el('p','连接来源待核对，未确认验证通过。直连设置或来源需要核对，当前状态不代表验证通过。',{class:'muted'}));
    output.append(el('p','结果尚未确认，请核对完整说明。读取这些状态不会扫描目录、读取原件或检查摄像头。照片拍摄时间、视频播放和裁剪效果尚未核验。',{class:'meta'}));
    if(catalog?.source_ref){
      const scan=catalog.checkpoint,age=scan?.checked_at?Date.now()-Date.parse(scan.checked_at):null;
      const message=!scan?'尚未扫描目录；连接成功不会自动读取原件。':!Number.isFinite(age)||age<0?'目录检查时间需核对，当前资料状态未知。上次目录检查的时间需核对，当前资料状态未知。':age>=60000?'目录检查已过1分钟，请重新核对状态。上次目录检查已超过一分钟，旧的资料状态需重新核对。':scan.enumeration_complete?'已到末页，不证明完整或文件上传完成。本次扫描已到末页；仍不能证明没有遗漏或文件已经上传完成。':'目录扫描尚未结束，资料显示可能不完整。';
      output.append(el('p',message,{class:'muted'}));
      if(catalog.completion_convention==='unreviewed')output.append(el('p','上传结束规则尚未核对，原件会保持待确认。',{class:'meta'}));
    }
    });
  };
  void refresh();return refresh;
}
function renderNASConnection(section, onStatusChanged=()=>{}) {
  const root=el('div',null,{id:'nas-direct-setup'}),form=el('form',null,{id:'nas-direct-form',class:'form-grid settings-form'});
  const result=el('div',null,{id:'nas-direct-result',class:'settings-result',role:'status','aria-live':'polite'});
  const message=el('p','正在读取已保存设置…',{id:'nas-direct-message',class:'muted',role:'status','aria-live':'polite'});
  add(root,el('h3','连接设置'),el('p','保存会停用读取；再连接，无需挂载目录。先保存，再由你连接。保存会停用当前读取；不需要 Finder 或挂载目录。',{class:'muted'}),form);
  const transport=el('select',null,{id:'nas-direct-transport'});
  add(transport,el('option',copyName('Friday-1640'),{value:'smb'}),el('option',copyName('Friday-1641'),{value:'sftp'}));
  form.append(add(el('label','连接方式',{for:'nas-direct-transport'}),transport));
  const host=settingsField(form,'主机地址','nas-direct-host','',{required:true,maxlength:253,autocomplete:'off',placeholder:'nas.local'},'只填主机名或IP，不填完整连接地址。填写主机名或 IP，不填写 smb:// 或 sftp:// 地址。');
  const port=settingsField(form,copyName('Friday-1645'),'nas-direct-port','',{type:'number',min:1,max:65535,step:1,placeholder:'445'},'留空使用所选方式的默认端口。');
  const credential=settingsField(form,'已保存的登录信息引用','nas-direct-credential','',{required:true,maxlength:128,autocomplete:'off',pattern:'[A-Za-z0-9_\\-]{1,128}',placeholder:'nas-family'},'这里只填登录引用，保存不等于验证。由原有本机登录信息服务保管账号与密码。这里只填引用；保存引用不代表已经认证。');
  const smb=el('div',null,{class:'form-grid full',id:'nas-direct-smb'});
  const share=settingsField(smb,'共享名称','nas-direct-share','',{required:true,maxlength:255,placeholder:'photos'},'填写NAS共享名称，不填完整地址。填写 NAS 上的共享名称，不填写完整地址。');
  const folder=settingsField(smb,copyName('Friday-1651'),'nas-direct-folder','',{maxlength:1024,placeholder:'Family'},'填共享内目录如Family，留空从根读取');
  smb.append(el('p','只读连接，默认要求SMB签名与加密。使用只读共享连接；默认要求 SMB 签名和加密。',{class:'full meta'}));form.append(smb);
  const sftp=el('div',null,{class:'form-grid full',id:'nas-direct-sftp',hidden:''});
  const pin=settingsField(sftp,copyName('Friday-1654'),'nas-direct-host-key','',{maxlength:50,autocomplete:'off',placeholder:'SHA256:…'},'结果尚未确认，请核对完整说明。通过 NAS 管理界面或可信渠道独立核对 SHA-256 指纹；不能自动接受未知主机。');
  settingsField(sftp,copyName('Friday-1656'),'nas-direct-sftp-root','/',{readonly:''},'只支持已被NAS限制目录的账号。此读取方式仅支持已由 NAS 限制在 / 的 SFTP 账号。');
  const confined=el('input',null,{type:'checkbox',id:'nas-direct-confined'});
  sftp.append(add(el('label','确认此账号只能访问指定根目录。我已确认此账号被 NAS 限制在这个根目录，不能访问其他位置。',{for:'nas-direct-confined',class:'nas-root-review full'}),confined));
  sftp.append(el('p','保存结果未确认，请先核对状态。Friday 不会修改 NAS 的账号或目录权限。未确认账号限制时不能保存 SFTP 连接。',{class:'full meta'}));form.append(sftp);
  const save=el('button',copyName('Friday-1660'),{type:'submit',id:'nas-direct-save',class:'primary'});form.append(save);
  const actions=el('div',null,{class:'settings-actions'});
  const connect=button('连接读取',()=>mutate('resume'),{id:'nas-direct-connect'});
  const disconnect=button(copyName('Friday-1662'),()=>mutate('disable'),{id:'nas-direct-disconnect',class:'secondary'});
  const refresh=button('刷新状态',()=>load(false),{id:'nas-direct-refresh',class:'secondary'});
  const reset=button('放弃草稿，载入已保存设置',()=>load(true),{id:'nas-direct-reset',class:'secondary'});
  add(actions,connect,disconnect,refresh,reset);add(root,el('h3','连接状态'),actions,message,result);section.append(root);
  let saved=null,generation=null,busy=false,reading=false,verified=false,dirty=Boolean(nasConnectionDraft),conflict=false,uncertain=null,sequence=0,polls=0,lastStatus=null;
  const inputs=[transport,host,port,credential,share,folder,pin,confined];
  const fields=()=>({transport:transport.value,host:host.value,port:port.value,credential_ref:credential.value,share:share.value,root:folder.value,host_key_sha256:pin.value,sftp_root_confined:confined.checked});
  const fill=value=>{
    transport.value=value.transport??'smb';host.value=value.host??'';port.value=String(value.port??'');credential.value=value.credential_ref??'';
    share.value=value.share??'';folder.value=value.root==='/'&&value.transport==='sftp'?'':value.root??'';
    pin.value=value.host_key_sha256??'';confined.checked=value.sftp_root_confined===true;
  };
  const mode=()=>{
    const native=transport.value==='sftp';smb.hidden=native;sftp.hidden=!native;port.setAttribute('placeholder',native?'22':'445');
    share.required=!native;pin.required=native;confined.required=native;
    for(const input of [share,folder])input.disabled=busy||native;
    for(const input of [pin,confined])input.disabled=busy||!native;
  };
  const controls=()=>{
    const known=generation!==null&&verified&&!reading;
    save.disabled=busy||!known||conflict||Boolean(uncertain);
    connect.disabled=busy||!known||dirty||conflict||Boolean(uncertain)||!saved?.config||(saved.status.enabled===true&&!['paused','backoff'].includes(saved.status.state));
    disconnect.disabled=busy||!known||Boolean(uncertain)||!saved?.config||saved.status.enabled!==true;
    refresh.disabled=busy||reading;reset.disabled=busy||reading||Boolean(uncertain)||!saved;
    connect.textContent=['paused','backoff','stopped'].includes(saved?.status.state)?'重新连接':'连接读取';
    for(const input of inputs)input.disabled=busy;mode();root.setAttribute('aria-busy',String(busy||reading));
  };
  const draft=()=>{if(busy)return;if(!nasConnectionDraft)nasConnectionDraftGeneration=generation;dirty=true;nasConnectionDraft=fields();message.textContent='先保存再连接，停止读取不使用未存草稿。有未保存修改。先保存，再连接；断开读取不使用这些草稿。';controls();};
  for(const input of inputs){input.addEventListener('input',draft);input.addEventListener('change',draft);}
  // NASConnectionConfig's closed fields/defaults; a fixture checks model drift.
  const configDefaults=Object.freeze({
    "schema_version":1,"port":null,"share":null,"root":"","enabled":false,
    "require_encryption":true,"host_key_sha256":null,"sftp_root_confined":false,
    "connect_timeout_seconds":8,"operation_timeout_seconds":5,"health_interval_seconds":30,
    "backoff_initial_seconds":1,"backoff_max_seconds":60
  });
  const configRequired=Object.freeze(["source_ref","transport","host","credential_ref"]);
  const configKeys=[...configRequired,...Object.keys(configDefaults)];
  const canonicalConfig=config=>{
    if(!config||typeof config!=='object'||Array.isArray(config)||Object.keys(config).some(key=>!configKeys.includes(key)))return null;
    if(configRequired.some(key=>!Object.prototype.hasOwnProperty.call(config,key)||typeof config[key]!=='string'))return null;
    const value={...configDefaults,...config};
    for(const [key,defaultValue] of Object.entries(configDefaults)){
      if(defaultValue===null){
        if(value[key]!==null&&(key==='port'?typeof value[key]!=='number'||!Number.isInteger(value[key])||value[key]<1||value[key]>65535:typeof value[key]!=='string'))return null;
      }else if(typeof value[key]!==typeof defaultValue||typeof value[key]==='number'&&!Number.isFinite(value[key]))return null;
    }
    if(value.schema_version!==1||!['smb','sftp'].includes(value.transport))return null;
    return configKeys.map(key=>value[key]);
  };
  const same=(a,b)=>{
    const left=canonicalConfig(a),right=canonicalConfig(b);
    return left!==null&&right!==null&&left.every((value,index)=>value===right[index]);
  };
  const request=async(path,options={})=>{
    const abort=typeof AbortController==='function'?new AbortController():null;
    const timer=abort?setTimeout(()=>abort.abort(),8000):null;
    try{
      const response=await fetch(path,{headers:{'Content-Type':'application/json'},...options,...(abort?{signal:abort.signal}:{})});
      const body=await response.json();
      if(!response.ok){const error=new Error('nas_request_failed');error.code=body?.code;error.status=response.status;throw error;}
      return body;
    }finally{if(timer!==null)clearTimeout(timer);}
  };
  const show=()=>{
    const status=saved?.status;result.replaceChildren();
    if(!status){result.append(el('p','已保存设置和连接状态尚未确认。'));return;}
    const labels={not_configured:'尚未配置',disabled:'已保存 · 读取停用',connecting:'正在连接',online:'读取连接在线',backoff:'连接中断或检查过期',paused:'连接已暂停',stopped:'读取尚未连接'};
    const online=status.state==='online'&&status.reachable===true&&status.authenticated===true&&status.readable===true;
    result.append(badge(online?'读取连接在线':labels[status.state]??'待核对',online?'healthy':''));
    result.append(el('p',saved.credential_reference_saved?'登录引用已保存，是否可用尚未检查':'尚未保存登录信息引用。'));
    const fact=value=>value===true?'已确认':value===false?'未验证通过':'尚未确认';
    result.append(el('p',`分别核对连接、登录与读取状态。主机可达：${fact(status.reachable)} · 认证：${fact(status.authenticated)} · 可读取：${fact(status.readable)}`));
    if(status.checked_at)result.append(el('p','最近检查'+time(status.checked_at),{class:'meta'}));
    if(status.state==='backoff')result.append(el('p',Number.isFinite(status.retry_in_seconds)?`请核对资料范围与当前状态。约 ${Math.ceil(status.retry_in_seconds)} 秒后自动重试。等待期间不要把旧资料视为当前可用。`:'连接检查过期，请刷新状态或重新连接。连接检查已过期，当前可读取状态未确认；可读取最新状态或重新连接。',{class:'muted'}));
    const reasons={nas_auth_failed:'请核对连接状态与使用范围。登录信息引用未就绪或认证未通过。请在原有登录信息服务中核对，再连接。',nas_access_denied:'读取未获授权，请NAS管理方核对目录。读取权限未通过；请由 NAS 管理方核对已授权目录。',nas_host_key_mismatch:'主机校验不符，读取暂停。主机指纹未匹配，读取已暂停。通过可信渠道核对后修改设置；不会自动接受新指纹。',nas_dependency_missing:'本机读取未就绪，请维护方配置后重试。本机读取组件尚未就绪，请由本机维护方完成配置后重试。',nas_invalid_request:'请核对本机设置，NAS连接尚未确认',nas_timeout:'本次连接或读取超时，结果尚未确认。',nas_transport_failed:'连接未完成，可核对主机地址后重试。'};
    if(reasons[status.failure])result.append(el('p',reasons[status.failure],{class:'muted'}));
    if(saved.config?.transport==='smb'&&saved.config.require_encryption===false)result.append(el('p','请核对连接状态与使用范围。此已保存共享连接未要求加密；保存草稿会保留现有要求。',{class:'meta'}));
    result.append(el('p','请核对连接状态与使用范围。连接状态不证明目录完整、原件上传完成或可以播放。这里只刷新本机状态，不扫描目录或读取原件。',{class:'meta'}));
  };
  const notify=()=>{
    const key=JSON.stringify([saved?.status.config_generation,saved?.status.state,saved?.status.connection_epoch,saved?.status.failure]);
    if(key!==lastStatus){lastStatus=key;onStatusChanged();}
  };
  const poll=()=>{
    if(polls>=15||!root.isConnected||section.hidden||!['connecting','backoff','stopped','online'].includes(saved?.status.state)||!saved?.status.enabled)return;
    polls++;setTimeout(()=>{if(root.isConnected&&!section.hidden&&!busy&&!reading)void load(false);},2000);
  };
  async function load(replaceDraft=false){
    if(busy||reading||!root.isConnected)return;reading=true;const current=++sequence;controls();
    try{
      const view=await request('/api/settings/nas/connection/config');
      if(current!==sequence||!root.isConnected)return;
      if(!view.status||!Number.isInteger(view.status.config_generation)||view.status.config_generation<0||typeof view.credential_reference_saved!=='boolean'||view.credential_availability!=='not_checked'||Boolean(view.config)!==view.status.configured)throw Error('invalid_saved_state');
      const previous=generation;let notice=dirty?'草稿保留。先保存修改，再连接。':'只读取本机设置，不检查登录或读取NAS。已读取本机保存设置；不会检查登录信息或读取 NAS。';
      if(uncertain){
        if(view.status.config_generation>uncertain.generation&&same(view.config,uncertain.config)){dirty=false;nasConnectionDraft=null;nasConnectionDraftGeneration=null;conflict=false;notice='已返回状态与上次请求一致的保存设置。';}
        else if(view.status.config_generation===uncertain.generation)notice='新版本还未确认，重试需防重复提交。尚未观察到上次请求的新版本。可显式重试；若请求已生效，版本检查会拒绝重复提交。';
        else{conflict=true;notice='设置已变，草稿保留；载入保存设置再核对。已保存设置发生变化。草稿保留；使用已保存设置并核对后再操作。';}
        uncertain=null;
      }else if(dirty&&((previous!==null&&view.status.config_generation!==previous)||(nasConnectionDraftGeneration!==null&&view.status.config_generation!==nasConnectionDraftGeneration))){conflict=true;notice='设置在别处已变，保留草稿，载入后核对。已保存设置在别处发生变化。草稿保留；使用已保存设置并核对后再操作。';}
      saved=view;generation=view.status.config_generation;verified=true;
      if(replaceDraft||!dirty&&!conflict){fill(view.config??{});dirty=false;conflict=false;nasConnectionDraft=null;nasConnectionDraftGeneration=null;}
      else if(nasConnectionDraftGeneration===null)nasConnectionDraftGeneration=generation;
      message.textContent=replaceDraft?'已载入保存设置，未保存草稿已放弃。':notice;show();notify();
    }catch(_){if(current===sequence&&root.isConnected){verified=false;message.textContent='请核对资料范围与当前状态。暂时无法读取最新设置。草稿保留；以下仅为上次记录，先读取状态核对后再提交。';}}
    finally{if(current===sequence){reading=false;controls();poll();}}
  }
  const candidate=()=>{
    const sftpMode=transport.value==='sftp',value=fields(),number=value.port.trim()?Number(value.port):null;
    if(!value.host.trim()||!value.credential_ref.trim().match(/^[A-Za-z0-9_-]{1,128}$/)||number!==null&&(!Number.isInteger(number)||number<1||number>65535)||sftpMode&&(!value.host_key_sha256.trim().match(/^SHA256:[A-Za-z0-9+/]{43}$/)||!value.sftp_root_confined)||!sftpMode&&!value.share.trim())throw Error('invalid_config');
    return {...(saved?.config??{source_ref:'family-nas',require_encryption:true}),transport:value.transport,host:value.host.trim(),port:number,credential_ref:value.credential_ref.trim(),enabled:false,share:sftpMode?null:value.share.trim(),root:sftpMode?'/':value.root.trim(),host_key_sha256:sftpMode?value.host_key_sha256.trim():null,sftp_root_confined:sftpMode};
  };
  async function mutate(operation){
    if(busy||reading||generation===null||!verified||uncertain||(conflict&&operation!=='disable')||operation==='resume'&&(dirty||!saved?.config||(saved.status.enabled&&!['paused','backoff'].includes(saved.status.state)))||operation==='disable'&&(!saved?.config||!saved.status.enabled))return;
    let config;try{config=operation==='save'?candidate():{...saved.config,enabled:operation==='resume'};}catch(_){message.textContent='请核对主机、登录引用与目录限制。请核对主机、登录信息引用，以及所选方式的共享或 SFTP 指纹与账号限制。';return;}
    const expected=generation;busy=true;++sequence;controls();message.textContent=operation==='save'?'正在保存设置，读取将保持停用…':operation==='resume'?'正在发起连接；保存引用不代表已经认证…':'正在断开并停用读取…';
    try{
      const status=await request(operation==='save'?'/api/settings/nas/connection':'/api/settings/nas/connection/'+operation,{method:operation==='save'?'PUT':'POST',body:JSON.stringify(operation==='save'?{expected_generation:expected,config}:{expected_generation:expected})});
      if(!root.isConnected)return;
      if(!Number.isInteger(status.config_generation)||status.config_generation<=expected)throw Error('unconfirmed_response');
      saved={config,status,credential_reference_saved:true,credential_availability:'not_checked'};generation=status.config_generation;verified=true;
      if(operation==='save'){dirty=false;nasConnectionDraft=null;nasConnectionDraftGeneration=null;fill(config);}
      message.textContent=operation==='save'?'设置已保存，读取停用；核对后再连接。设置已保存，读取停用。确认无误后点击连接读取。':operation==='resume'?'连接请求已保存，连接认证读取仍待确认':'读取已停用，旧目录与原件状态需重查。已断开并停用读取。旧的目录与原件状态需重新核对。';
      show();notify();polls=0;
    }catch(error){
      if(!root.isConnected)return;
      if(error.code==='nas_source_changed'){conflict=true;verified=false;message.textContent='连接版本已变，未重复保存，保留草稿重查。连接版本已变化，未重复保存。读取最新设置并核对；草稿保留。';}
      else if(error.status===422||error.status===403||error.code==='nas_busy')message.textContent=error.code==='nas_busy'?'其他设置正在处理，请先刷新状态':'请求未通过，请核对字段或本机访问状态。';
      else{uncertain={generation:expected,config};message.textContent='结果未知，勿重复提交，先刷新核对保存。请求结果尚未确认。不要重复提交；先读取最新状态核对保存结果。';}
    }finally{busy=false;if(root.isConnected){controls();poll();}}
  }
  form.addEventListener('submit',event=>{event.preventDefault();return mutate('save');});
  if(nasConnectionDraft)fill(nasConnectionDraft);mode();controls();
  nasConnectionRefresh=()=>load(false);void load(false);
}
async function refreshHaStatus(reloadConfiguration = true) {
  if (!settingsData) return;
  if (haRefreshRequest) {
    await haRefreshRequest;
    if (!reloadConfiguration) return refreshHaStatus(false);
    return;
  }
  const previous = settingsData;
  haRefreshRequest = (async () => {
    try {
      // Browser back/BFCache can restore old fields and old credential state after OAuth.
      // Update saved HA fields only when the user has not edited them; never clear drafts.
      if (reloadConfiguration) {
        const received = await api('/api/settings');
        if (settingsData !== previous) return;
        for (const [id, key] of [['settings-ha-url', 'ha_url'], ['settings-ha-instance', 'ha_instance']]) {
          const input = $(id);
          if (input && input.value === (previous.config[key] ?? '')) input.value = received.config[key] ?? '';
        }
        settingsData = received;
        geekCredentialAvailable = received.credentials?.geek?.credential_available ?? geekCredentialAvailable;
      }
      const selected = settingsData;
      const received = await api('/api/settings/ha/status');
      if (settingsData !== selected) return;
      haStatus = received;
    } catch (_) { haStatus = null; }
    if (page === 'Settings' && $('settings-ha-result')) showHaStatus($('settings-ha-result'));
  })();
  try { await haRefreshRequest; } finally { haRefreshRequest = null; }
}
function showNasCheck(output, report) {
  output.replaceChildren(el('p', settingsStatus(report)));
  output.append(el('p', report.readable === true ? '目录已连接' : '目录尚未连接', {class: 'meta'}));
  if (report.checked_at) output.append(el('p', '检查于' + time(report.checked_at), {class: 'meta'}));
  if (report.mount_required === true || report.status === 'mount_required') {
    const href = safeSettingsLink(report.open_url, ['smb:']);
    if (href) output.append(el('a', copyName('Friday-1715'), {href}));
    output.append(el('p', '连接后，重新检查目录。', {class: 'meta'}));
  }
}
function showCameraCheck(output, report) {
  const bound = report.account_bound ?? report.bound;
  output.replaceChildren(el('p', settingsStatus(report)), el('p', `账号：${bound === true ? '已绑定' : bound === false ? '未绑定' : '待检查'}`, {class: 'meta'}));
  if (report.checked_at) output.append(el('p', '检查于' + time(report.checked_at), {class: 'meta'}));
}
let cameraMediaReport = null;
let cameraMediaRefresh = null;
let cameraMediaRevision = 0;
function showCameraMedia(output, report) {
  const labels={not_requested:'尚未验证音视频',not_configured:'请先保存摄像头与读取方式',running:'正在验证音视频…',decoded_sync_unknown:copyName('Friday-1721'),failed:'音视频验证未通过',stopped:'验证已停止',discarded_configuration_changed:'连接配置已更改，请重新验证',expired:'上次验证已过期，请重新验证'};
  const failures={service_unavailable:'请核对连接状态与使用范围。本地 Miloco 服务未连接，可改用已登录米家账号读取。',service_login_required:'请核对权限与本次操作范围。本地服务授权不可用，请检查 Miloco 登录。',account_login_required:'相机未登录或本机登录文件不可读。摄像头账号尚未登录，或本机登录文件不可读取。',account_token_expired:'米家登录已过期，请更新本机登录。',sdk_not_qualified:'相机或本机连接组件尚未通过检查。当前摄像头或本机连接组件尚未通过适配检查。',camera_not_found:'米家账号中未找到所选摄像头。',camera_offline:'米家报告摄像头离线。',camera_pin_required:'相机有独立PIN保护，暂不支持验证。摄像头开启了独立 PIN 保护，暂不支持直接验证。',service_conflict:'媒体服务已运行，请改用现有服务。本地媒体服务已在运行，请使用现有 Miloco 服务读取。',unsupported_codec:'当前编码尚不支持。',codec_changed:'编码在验证期间改变，请重新验证。',missing_init:'未收到编码初始化信息。',malformed_packet:'收到无法识别的数据。',decode_failed:'媒体无法解码。',stream_closed:'媒体连接中断。',stream_idle:'音视频未持续到验证结束。',stream_error:'摄像头连接未完成。',no_decoded_frames:'未取得可解码的媒体。',input_budget:'媒体超出本次验证容量。',protocol_error:'服务响应无法识别。',worker_memory_limit:'验证进程超出内存限额，已停止。',worker_timeout:'验证超时，已停止。',worker_failed:'验证进程未完成。',worker_unavailable:'验证进程不可用。'};
  output.replaceChildren(el('p',labels[report?.status]??'验证状态暂不可确认'));
  if(report?.simulated===true)output.append(el('p','simulated · 示例音视频',{class:'meta'}));
  if(report?.checked_at)output.append(el('p','读取方式'+(report.provider==='miot_sdk'?'已登录米家账号':'现有 Miloco 服务'),{class:'meta'}));
  for(const [key,label] of [['video','视频'],['audio','音频']]){
    const stream=report?.[key];
    if(stream?.status==='decoded')output.append(el('p',label+'已解码，共'+stream.decoded_frames+'帧。仅确认音视频解码通过。音画同步仍无法核验。',{class:'meta'}));
    if(stream?.failure)output.append(el('p',label+'：'+(failures[stream.failure]??'验证未通过'),{class:'meta'}));
  }
  if(report?.failure)output.append(el('p',failures[report.failure]??'验证未完成',{class:'meta'}));
  if(report?.video?.status==='decoded'&&report?.audio?.status==='decoded')output.append(el('p','音画同步：无法核验，'+(report.provider==='miot_sdk'?'源时钟尚未标定。':'音频未提供源时间戳。'),{class:'meta'}));
  if(report?.checked_at)output.append(el('p','检查于'+time(report.checked_at),{class:'meta'}));
  const start=$('settings-camera-media-start'),stop=$('settings-camera-media-stop');
  if(start)start.disabled=report?.status==='running';
  if(stop)stop.disabled=report?.status!=='running';
}
async function refreshCameraMedia(){
  const output=$('settings-camera-media-result'),revision=cameraMediaRevision;
  if(page!=='Settings'||!output||document.hidden===true||cameraMediaRefresh||output.getAttribute?.('aria-busy')==='true')return;
  cameraMediaRefresh=(async()=>{
    try{
      const report=await api('/api/settings/camera/media',{signal:globalThis.AbortSignal?.timeout(5000)});
      if(page==='Settings'&&$('settings-camera-media-result')===output&&revision===cameraMediaRevision&&output.getAttribute?.('aria-busy')!=='true'){
        cameraMediaReport=report;showCameraMedia(output,report);
      }
    }catch(_){if($('settings-camera-media-result')===output&&revision===cameraMediaRevision)settingsMessage(output,'验证状态暂不可确认，请刷新查看。');}
  })();
  try{await cameraMediaRefresh;}finally{cameraMediaRefresh=null;}
}
function renderCameraMedia(camera,patch){
  const block=el('div',null,{class:'settings-credential'}),controls=el('div',null,{class:'settings-actions'}),output=el('div',null,{id:'settings-camera-media-result',role:'status','aria-live':'polite'});
  block.append(el('h3','影音连接'),el('p','读取6秒影音验证解码，不保存原片。连接后读取 6 秒音视频并验证解码。不会保存原片。',{class:'muted'}),controls,output);camera.section.append(block);
  const act=async(stopping)=>{
    if(!stopping&&!savedSettings(camera,patch))return;
    const revision=++cameraMediaRevision;
    output.setAttribute('aria-busy','true');start.disabled=stop.disabled=true;
    settingsMessage(output,stopping?'正在停止…':'正在启动验证…');
    try{
      const report=await api('/api/settings/camera/media'+(stopping?'/stop':''),{method:'POST',body:JSON.stringify(stopping?{}:{seconds:6})});
      if(page==='Settings'&&$('settings-camera-media-result')===output&&revision===cameraMediaRevision){cameraMediaReport=report;showCameraMedia(output,report);}
    }catch(_){if($('settings-camera-media-result')===output&&revision===cameraMediaRevision){cameraMediaReport=null;settingsMessage(output,'未取得验证状态，请刷新查看。');start.disabled=stop.disabled=true;}}
    finally{output.setAttribute('aria-busy','false');}
  };
  const start=button(copyName('Friday-1770'),()=>act(false),{id:'settings-camera-media-start'}),stop=button('停止验证',()=>act(true),{id:'settings-camera-media-stop'});
  controls.append(start,stop,button('刷新状态',refreshCameraMedia,{id:'settings-camera-media-refresh'}));
  showCameraMedia(output,cameraMediaReport);void refreshCameraMedia();
}
function showGeekCheck(output, report) {
  output.replaceChildren(el('p', settingsStatus(report)));
  for (const [key, label] of [['credential_available', '配对码已保存'], ['device_count', '设备'], ['graph_count', '规则'], ['scope_count', '范围'], ['writes_attempted', '已提交设备操作']]) {
    if (typeof report[key] === 'boolean' || typeof report[key] === 'number') output.append(el('p', label + ': ' + (typeof report[key] === 'boolean' ? (report[key] ? '是' : '否') : display(report[key])), {class: 'meta'}));
  }
  output.append(el('p', '数据来源' + (report.simulated === true ? '示例家庭' : report.simulated === false && report.device_count !== undefined ? '本地网关' : '待检查'), {class: 'meta'}));
  if (report.failure_phase) output.append(el('p', geekFailureMessage(report), {class: 'meta'}));
  if (report.failure_phase) output.append(detail('连接诊断', {phase:report.failure_phase, code:report.failure_code ?? 'check_failed'}));
  if (report.graph_semantics === 'opaque') output.append(el('p', '规则详情需在米家极客版中查看。', {class: 'meta'}));
  if (report.checked_at) output.append(el('p', '检查于' + time(report.checked_at), {class: 'meta'}));
}
function geekFailureMessage(report) {
  if (report?.failure_phase === 'websocket_connect') return '请核对权限与本次操作范围。网关无法连接，请检查网关网络及 macOS 的本地网络权限；已保存的配对码保持不变。';
  if (report?.failure_code === 'gateway_error_frame') return '网关拒绝连接，原因未知，请核对配对码。网关拒绝了握手，原因暂不可确认；请先核对网关当前的配对码。';
  if (report?.failure_phase === 'authenticate') return '配对握手未通过，请检查网关的配对状态。';
  if (report?.failure_phase === 'inventory') return '设备与规则未完整读取，请检查网关。连接后未能完整读取设备与规则，请检查网关状态。';
  return '后台同步未启用，请查服务后重新连接。后台同步未能启用，请检查服务状态后再连接。';
}
function showGeekSession(output, report) {
  let status = report?.status ?? 'stopped';
  if (status === 'online' && (report.session_live !== true || report.inventory_stale === true)) status = 'stale';
  const labels = {stopped:'持续同步已停止',starting:'正在连接网关',online:'已连接 · 持续只读同步',reconnecting:'连接中断，正在重连',blocked:'网关连接未完成',stale:'规则状态已过期',worker_stopped:'连接已结束',worker_fault:'同步进程异常',worker_unavailable:'同步进程不可用',pairing_required:'请先保存地址和配对码',unsupported_gateway_address:'需要有效的内网网关地址',background_unavailable:'后台同步服务暂不可用'};
  output.replaceChildren(el('p',labels[status]??'连接状态暂不可确认'));
  if (Number.isInteger(report?.graph_count)) output.append(el('p',`上次读取：${report.graph_count} 条极客版规则`,{class:'meta'}));
  const changes = report?.last_change ?? report?.changes;
  if (changes && ['added','removed','modified'].every(k=>Number.isInteger(changes[k])&&changes[k]>=0&&changes[k]<=64) && changes.added+changes.removed+changes.modified>0) output.append(el('p',`新增、移除与修改的规则数量。发现规则变化：新增 ${changes.added}，移除 ${changes.removed}，修改 ${changes.modified}`,{class:'meta'}));
  if (report?.gap === true) output.append(el('p','重连期间的变化可能未完整记录。',{class:'meta'}));
  if (report?.failure_phase || report?.failure_code) output.append(el('p',geekFailureMessage(report),{class:'meta'}));
  if (report?.checked_at) output.append(el('p','更新于'+time(report.checked_at),{class:'meta'}));
  if (report?.background_service_live === true) output.append(el('p',status === 'online' && report.keep_connected === true?'后台持续同步，关闭窗口仍继续':report.failure_phase === 'authenticate'?'后台运行中，网关需要重新配对':'后台运行中，等待网关连接',{class:'meta'}));
  const attempt=report?.background_last_attempt;
  if (attempt?.outcome === 'failed' && !(status === 'online' && report.connection_host === 'background_service')) {
    output.append(el('p','上次后台启用未完成'+(attempt.checked_at?' · '+time(attempt.checked_at):''),{class:'meta'}));
    output.append(el('p',geekFailureMessage(attempt),{class:'meta'}));
  }
  output.append(el('p','请核对连接状态与使用范围。配对码在本机保存，重新连接时复用。仅同步极客版可见规则。',{class:'meta'}));
}
let geekRefreshRequest = null;
let geekSessionRevision = 0;
async function refreshGeekSession() {
  const output = $('settings-geek-session-result');
  if (page !== 'Settings' || !output || document.hidden === true || geekRefreshRequest || output.getAttribute?.('aria-busy') === 'true') return;
  const selectedURL = settingsData?.config?.geek_url;
  const revision = geekSessionRevision;
  geekRefreshRequest = (async()=>{
    try {
      const report = await api('/api/settings/geek/session',{signal:globalThis.AbortSignal?.timeout(5000)});
      if (page === 'Settings' && $('settings-geek-session-result') === output && settingsData?.config?.geek_url === selectedURL && revision === geekSessionRevision && output.getAttribute?.('aria-busy') !== 'true') showGeekSession(output,report);
    } catch (_) { if ($('settings-geek-session-result') === output && revision === geekSessionRevision && output.getAttribute?.('aria-busy') !== 'true') settingsMessage(output,'同步状态暂不可确认，请刷新。'); }
  })();
  try { await geekRefreshRequest; } finally { geekRefreshRequest = null; }
}
async function checkSettings(block, name, patch, show) {
  if (!savedSettings(block, patch)) return;
  const checkedConfig = settingsData;
  const received = await api('/api/settings/' + name + '/probe', {method: 'POST', body: '{}'});
  if (checkedConfig !== settingsData) {
    settingsMessage(block.output, '配置已更改，请重新检查。'); return;
  }
  // Keep only public evidence fields, never an arbitrary credential-bearing response.
  const fields = ['status', 'checked_at', 'readable', 'mounted', 'mount_required', 'open_url',
    'account_bound', 'bound', 'credential_available', 'simulated', 'writes_attempted', 'graph_semantics', 'device_count', 'graph_count', 'scope_count', 'failure_phase', 'failure_code'];
  const report = Object.fromEntries(fields.filter(key => key in received).map(key => [key, received[key]]));
  settingsData.checks[name] = report; show(block.output, report);
}
function renderArchiveSettings(section) {
  const session=++archiveSetupSession,block=el('details',null,{class:'settings-credential',id:'settings-archive'});
  add(block,el('summary','录像设置'),el('p','读取目录后对应摄像头，录像只读。读取目录后，把每个目录对应到摄像头。录像只读复用。',{class:'muted'}));section.append(block);
  const output=el('div',null,{id:'settings-archive-status',role:'status','aria-live':'polite'}),listing=el('div'),controls=el('div',null,{class:'settings-actions'});
  const form=el('form',null,{id:'settings-archive-form',class:'form-grid settings-form'});
  const camera=el('select',null,{id:'settings-archive-camera','aria-label':copyName('Friday-1244')}),folder=el('select',null,{id:'settings-archive-folder','aria-label':'录像目录'});
  add(form,add(el('label','摄像头'),camera),add(el('label','录像目录'),folder));
  const fields={};
  for(const [key,label,attrs,help] of [
    ['name','名称',{maxlength:80},''],['room','房间',{maxlength:128},''],
    ['timezone','录像时区',{maxlength:64},'如 Asia/Shanghai。'],
    ['offset',copyName('Friday-1823'),{type:'number',step:'any',min:-86400,max:86400},'超前填正数，滞后填负数。'],
    ['uncertainty',copyName('Friday-1825'),{type:'number',step:'any',min:0,max:60},'未测量时留空；不要把未知填成 0。'],
    ['arrival','归档等待时间（秒）',{type:'number',min:0,max:86400},'由录像写入行为核对；未知可留空。'],
    ['retention',copyName('Friday-1829'),{type:'number',min:1,max:3650},'仅记录 NAS 策略，不会删除原片。']
  ])fields[key]=settingsField(form,label,'settings-archive-'+key,archiveSetupDraft[key],attrs,help);
  const timing=el('input',null,{type:'checkbox',id:'settings-archive-timing'}),completion=el('input',null,{type:'checkbox',id:'settings-archive-completion'});
  timing.checked=archiveSetupDraft.timing;completion.checked=archiveSetupDraft.completion;
  add(form,add(el('label','已核对文件名起止时间、时区和时钟误差',{class:'full archive-review'}),timing),add(el('label','仅将已完成保存的文件视为完整片段。已核对该目录仅以完成写入的文件表示闭合片段',{class:'full archive-review'}),completion));
  const save=el('button',copyName('Friday-1833'),{type:'submit',id:'settings-archive-save',class:'primary'});save.disabled=true;form.append(save);
  const scan=button('刷新目录',async()=>{
    if(!context||!current()||!savedNas())return;
    const version=++archiveSetupVersion,before=context;scan.disabled=true;save.disabled=true;settingsMessage(output,'正在读取目录索引…');
    try{
      const received=await api('/api/archive/scan',{method:'POST',body:JSON.stringify({expected_context:before.fingerprint})}),fresh=await api('/api/archive/context');
      if(!current()||version!==archiveSetupVersion)return;
      if(received.fingerprint!==fresh.fingerprint||fresh.simulated){settingsMessage(output,'归档来源已变化，请重新读取。',true);return;}
      context=fresh;archiveResult=null;archiveVersion++;showContext();
    }catch(_){if(current()&&version===archiveSetupVersion)settingsMessage(output,'请核对连接状态与使用范围。索引未完成。检查目录连接；SMB 共享需先在 Finder 登录并挂载。',true);}
    finally{if(current()){scan.disabled=!context||context.simulated;save.disabled=!folder.value||!context||context.simulated;}}
  },{id:'settings-archive-scan',class:'secondary'});scan.disabled=true;controls.append(scan);
  add(block,controls,output,listing,form,el('p','结果尚未确认，请核对完整说明。索引与诊断基本信息在本机保留 7 天，最多 1,000 条，按家庭敏感信息处理。原片仍由 NAS 保存。播放器与跳转误差尚未验证。',{class:'meta'}));
  let context=null;
  const current=()=>session===archiveSetupSession&&page==='Settings'&&block.isConnected;
  const remember=()=>{
    archiveSetupDraft.camera=camera.value;archiveSetupDraft.folder=folder.value;
    for(const [key,field] of Object.entries(fields))archiveSetupDraft[key]=field.value;
    archiveSetupDraft.timing=timing.checked;archiveSetupDraft.completion=completion.checked;
    archiveSetupVersion++;save.disabled=!folder.value||!context||context.simulated;
  };
  const savedNas=()=>{
    const config=settingsData?.config,kind=$('settings-nas-kind');
    if(!config)return false;
    const patch=kind?.value==='path'?{nas_path:settingValue($('settings-nas-path')),nas_url:null}:{nas_path:null,nas_url:settingValue($('settings-nas-url'))};
    if(Object.entries(patch).every(([key,value])=>(config[key]??null)===value))return true;
    settingsMessage(output,'请先保存目录地址，再读取索引。',true);return false;
  };
  const chooseCamera=()=>{
    const profile=context?.cameras?.find(item=>item.id===camera.value);
    if(profile){
      const keys={name:'name',room:'room',timezone:'timezone',offset:'clock_offset_seconds',uncertainty:'clock_uncertainty_seconds',arrival:'arrival_delay_seconds',retention:'source_retention_days'};
      for(const [key,source] of Object.entries(keys))fields[key].value=profile[source]??'';
      timing.checked=profile.timing_reviewed===true;completion.checked=profile.completion_convention==='closed_files_operator_reviewed';
      folder.value=context.folders.some(item=>item.folder_ref===profile.folder_ref)?profile.folder_ref:'';
      if(profile.room_binding_current===false)settingsMessage(output,'请核对摄像头对应的当前家庭房间，再确认保存。完成前不会把这里的录像连到家庭事件。');
    }else{
      for(const [key,field] of Object.entries(fields))field.value=key==='timezone'?'Asia/Shanghai':'';
      timing.checked=false;completion.checked=false;folder.value='';
    }
    remember();
  };
  camera.addEventListener('change',chooseCamera);folder.addEventListener('change',remember);
  for(const field of [...Object.values(fields),timing,completion])field.addEventListener('input',remember);
  function showContext(){
    listing.replaceChildren();camera.replaceChildren(el('option','新增相机',{value:''}));folder.replaceChildren(el('option','选择已读取目录',{value:''}));
    for(const item of context.cameras??[])camera.append(el('option',item.name+(item.source_binding_current===false?' · 需重新对应目录':item.room_binding_current===false?' · 需核对家庭房间':''),{value:item.id}));
    for(const item of context.folders??[]){folder.append(el('option',item.label,{value:item.folder_ref}));listing.append(el('p',withCopy(item.label+' · '+(item.scan_complete?copyName('Friday-1843'):'部分索引'),item.scan_complete?copyName('Friday-1843'):null),{class:'meta'}),detail('文件示例',item.sample_filenames));}
    camera.value=context.cameras.some(item=>item.id===archiveSetupDraft.camera)?archiveSetupDraft.camera:'';
    folder.value=context.folders.some(item=>item.folder_ref===archiveSetupDraft.folder)?archiveSetupDraft.folder:'';
    form.hidden=context.simulated;scan.disabled=context.simulated;save.disabled=!folder.value||context.simulated;
    if(context.simulated)settingsMessage(output,'仅供模拟，不能证明实际效果。simulated · 示例归档已对应摄像头，可到“回看”查找区间。');
    else if(context.scan)settingsMessage(output,`本次已查文件数；目录选择限60秒。本次索引 ${context.scan.indexed_this_scan} 个文件；目录选择在 60 秒后需重新读取。`);
    else settingsMessage(output,context.configured?'尚未读取索引。已保存的摄像头关系会保留。':'请先连接录像目录。');
  }
  const refresh=async()=>{
    const version=++archiveSetupVersion;scan.disabled=true;save.disabled=true;
    try{const fresh=await api('/api/archive/context');if(!current()||version!==archiveSetupVersion)return;context=fresh;showContext();}
    catch(_){if(current())settingsMessage(output,'录像索引设置暂不可读取。',true);}
  };
  archiveSetupRefresh=refresh;void refresh();
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(!context||context.simulated||!folder.value||!savedNas())return;
    const version=++archiveSetupVersion,before=context;save.disabled=true;
    const numeric=key=>fields[key].value.trim()===''?null:Number(fields[key].value);
    try{
      const body={expected_context:before.fingerprint,folder_ref:folder.value,camera_id:camera.value||null,name:fields.name.value.trim(),room:settingValue(fields.room),timezone:fields.timezone.value.trim(),timing_reviewed:timing.checked,clock_offset_seconds:numeric('offset'),clock_uncertainty_seconds:numeric('uncertainty'),completion_convention:completion.checked?'closed_files_operator_reviewed':'unreviewed',arrival_delay_seconds:numeric('arrival'),source_retention_days:numeric('retention')};
      const received=await api('/api/archive/cameras',{method:'POST',body:JSON.stringify(body)}),fresh=await api('/api/archive/context');
      if(!current()||version!==archiveSetupVersion)return;
      if(received.fingerprint!==fresh.fingerprint||fresh.simulated){settingsMessage(output,'归档来源已变化，请重新读取。',true);return;}
      context=fresh;archiveSetupDraft.camera=fresh.cameras.find(item=>item.folder_ref===body.folder_ref)?.id??'';archiveResult=null;archiveVersion++;showContext();settingsMessage(output,'摄像头对应关系已保存。');
    }catch(_){if(current()&&version===archiveSetupVersion)settingsMessage(output,'未保存，请刷新目录核对名称时区与误差。未保存。请重新读取目录，并核对名称、时区和时钟误差。',true);}
    finally{if(current())save.disabled=!folder.value||!context||context.simulated;}
  });
}
function renderSettings(content) {
  if (!settingsData) { void loadSettings(content); return; }
  const config = settingsData.config;
  const source = settingsSection(content, '家庭数据', '', 'settings-source');
  source.form.setAttribute('hidden', '');
  source.output.append(el('p', snapshot ? (snapshot.mode === 'simulator' ? '当前使用：示例家庭' : '当前使用：Home Assistant') : '未连接家庭数据'));
  const activate = async (path) => {
    snapshot = await api(path, {method: 'POST', body: '{}'}); connected = true; lastTrace = null; loadedScenario = null; render();
  };
  settingsButton(source, '示例家庭', 'settings-simulator', () => activate('/api/settings/source/simulator'));
  settingsButton(source, '使用 Home Assistant', 'settings-ha-activate', () => activate('/api/settings/ha/activate'));
  source.section.append(el('p','请核对权限与本次操作范围。真实操作按当前授权范围和设备能力执行。暂停会阻止后续操作，已经发送的操作无法撤回。',{class:'muted'}));
  const pause = settingsButton(source, '暂停后续真实操作', 'settings-actions-pause', async () => {
      await api('/api/settings/actions/pause',{method:'POST',body:'{}'});
      snapshot = await api('/api/snapshot');lastTrace = null;render();
  });
  pause.disabled = !snapshot?.action_targets?.length;
  const resume=settingsButton(source,'恢复已授权设备操作','settings-actions-resume',async()=>{
    const report=await api('/api/settings/actions/status');
    if(!report.configured){settingsMessage(source.output,'设备操作授权需要在本机重新设置。',true);return;}
    snapshot=await api('/api/settings/actions/resume',{method:'POST',body:JSON.stringify({expected_revision:report.revision})});connected=true;lastTrace=null;render();
  });
  resume.disabled=true;
  api('/api/settings/actions/status').then(report=>{
    if(!source.output.isConnected)return;
    resume.disabled=!report.configured||report.enabled||snapshot?.mode!=='ha-read-only';
    if(report.scope==='all_registered_devices')source.section.append(el('p','全家设备已授权，操作仍按HA能力开放。已授权本家庭全部设备；实际控制项按当前 HA 能力开放。',{class:'meta'}));
  }).catch(()=>{});

  const connections = [['settings-ha','Home Assistant'],['settings-geek','米家极客版'],['settings-nas','NAS'],['settings-camera','Miloco'],['settings-arl','智能服务'],['settings-measurements','测量来源']];
  const tabs = el('div',null,{class:'room-tabs connection-tabs',role:'group','aria-label':'家庭连接'});
  const connectionButtons = [];
  for (const [id,label] of connections) {
    const tabButton = button(label,() => {
      selectedConnection = id;
      for (const [sectionId] of connections) $(sectionId + '-title')?.setAttribute('tabindex','-1');
      for (const [sectionId,entry] of connectionButtons) {
        $(sectionId)?.setAttribute('hidden',''); entry.setAttribute('aria-pressed',String(sectionId === id)); entry.setAttribute('class',sectionId === id ? 'selected' : '');
      }
      const section = $(id); if (section) {section.hidden = false; section.removeAttribute?.('hidden');}
      if (id === 'settings-measurements') void loadMeasurementSettings();
      if (id === 'settings-nas') void nasConnectionRefresh?.();
    },{'aria-pressed':String(selectedConnection === id),class:selectedConnection === id ? 'selected' : ''});
    tabs.append(tabButton); connectionButtons.push([id,tabButton]);
  }
  content.append(tabs);
  const ha = settingsSection(content, 'Home Assistant', '登录Home Assistant读取家庭。登录你的 Home Assistant，读取家里的设备与房间。', 'settings-ha');
  const haUrl = settingsField(ha.form, '服务地址', 'settings-ha-url', config.ha_url, {type: 'url', placeholder: 'http://homeassistant.local:8123', maxlength: 512});
  const haInstance = settingsField(ha.form, '名称', 'settings-ha-instance', config.ha_instance, {maxlength: 96}, '');
  const haPatch = () => ({ha_url: settingValue(haUrl), ha_instance: settingValue(haInstance)});
  let discoveredChoice = null;
  const discoveryOutput = el('div', '', {id:'settings-ha-discovery', role:'status', 'aria-live':'polite'});
  ha.section.append(discoveryOutput);
  const chooseDiscovered = instance => {
    haUrl.value = instance.url;
    if (!haInstance.value.trim()) haInstance.value = instance.instance_id;
    discoveredChoice = instance.url;
  };
  const findHa = async () => {
    settingsMessage(discoveryOutput, '正在查找同一网络的家庭网关。正在查找局域网 Home Assistant…');
    const before = haUrl.value;
    try {
      const report = await api('/api/settings/ha/discover');
      if (!discoveryOutput.isConnected) return;
      const instances = report.instances ?? [];
      if (!instances.length) {
        settingsMessage(discoveryOutput, '未找到家庭网关，可手动填写地址。未发现 Home Assistant。检查同网段与 mDNS；也可手动填写地址。'); return;
      }
      settingsMessage(discoveryOutput, `请核对权限与本次操作范围。发现 ${instances.length} 个 Home Assistant。发现仅提供地址，仍需授权。`);
      if (instances.length === 1 && !before && !haUrl.value) {
        chooseDiscovered(instances[0]);
        discoveryOutput.append(el('p', '已自动填入内网地址，点击登录并授权即可。'));
      } else {
        const choices = el('select', null, {id:'settings-ha-discovered-instance', 'aria-label':'选择 Home Assistant 实例'});
        choices.append(el('option', '选择网关', {value:''}));
        instances.forEach((item,index) => choices.append(el('option', item.name + ' · ' + item.url, {value:String(index)})));
        choices.addEventListener('change', () => { if (choices.value !== '') chooseDiscovered(instances[Number(choices.value)]); });
        discoveryOutput.append(choices);
      }
    } catch (_) { settingsMessage(discoveryOutput, '局域网发现暂不可用；可手动配置地址。', true); }
  };
  settingsSave(ha, 'settings-ha', haPatch);
  const showHa = () => showHaStatus(ha.output);
  showHa();
  settingsButton(ha, '查找局域网 Home Assistant', 'settings-ha-discover', async () => { await findHa(); showHa(); });
  if (!config.ha_url) void findHa();
  settingsButton(ha, '登录授权', 'settings-ha-connect', async () => {
    // Explicit authorization also saves the selected advertised address. This removes
    // manual address entry/Save from the normal discovery path without auto-login.
    if (discoveredChoice && settingValue(haUrl) === discoveredChoice) {
      settingsData = await api('/api/settings', {method:'PUT', body:JSON.stringify(haPatch())});
      discoveredChoice = null;
    }
    if (!savedSettings(ha, haPatch)) return;
    const result = await api('/api/settings/ha/connect', {method: 'POST', body: '{}'});
    const href = safeSettingsLink(result.authorization_url, ['http:', 'https:']);
    if (!href) throw new Error('Invalid authorization address');
    location.assign(href);
  });
  settingsButton(ha, '检查授权', 'settings-ha-status', async () => {
    haStatus = await api('/api/settings/ha/status'); showHa();
  });

  const nas = settingsSection(content, copyName('Friday-1881'), '直读NAS照片视频，登录信息本机存。直连 NAS 读取已有照片与视频；登录信息保留在原有本机服务。', 'settings-nas');
  const locationType = el('select', null, {id: 'settings-nas-kind'});
  add(locationType, el('option', copyName('Friday-1640'), {value: 'smb'}), el('option', '已挂载目录', {value: 'path'}));
  locationType.value = config.nas_path ? 'path' : 'smb';
  nas.form.append(add(el('label', '连接方式', {for: 'settings-nas-kind'}), locationType));
  const nasUrl = settingsField(nas.form, '共享地址', 'settings-nas-url', config.nas_url, {placeholder: 'smb://nas.local/archive', maxlength: 1024}, '使用 Finder 登录。');
  const nasPath = settingsField(nas.form, '目录路径', 'settings-nas-path', config.nas_path, {placeholder: '/Volumes/archive', maxlength: 1024}, '如 /Volumes/archive。');
  const updateNasKind = () => { nasUrl.disabled = locationType.value !== 'smb'; nasPath.disabled = locationType.value !== 'path'; };
  locationType.addEventListener('change', updateNasKind); updateNasKind();
  const nasPatch = () => ({nas_url: locationType.value === 'smb' ? settingValue(nasUrl) : null, nas_path: locationType.value === 'path' ? settingValue(nasPath) : null});
  settingsSave(nas, 'settings-nas', nasPatch);
  if (settingsData.checks?.nas) showNasCheck(nas.output, settingsData.checks.nas); else settingsMessage(nas.output, '尚未检查目录');
  settingsButton(nas, '检查目录', 'settings-nas-probe', () => checkSettings(nas, 'nas', nasPatch, showNasCheck));
  let sourceReadStatus;
  renderNASConnection(nas.section,()=>sourceReadStatus?.());
  sourceReadStatus=renderLocalReadSources(nas.section);
  const legacy=el('details',null,{class:'details',id:'nas-legacy-archive'});
  legacy.append(el('summary','已有录像归档与旧版目录设置（可选）'),el('p','此为旧录像独立设置，直连无需挂载。这是现有录像归档的独立设置；上方直连流程不需要这些挂载设置。',{class:'muted'}),nas.form,nas.controls,nas.output);
  nas.section.append(legacy);renderArchiveSettings(legacy);

  const camera = settingsSection(content, 'Miloco · 房间感知', '请核对连接状态与使用范围。复用米家账号或连接本地 Miloco 服务读取摄像头。', 'settings-camera');
  const cameraUrl = settingsField(camera.form, '服务地址', 'settings-camera-url', config.camera_service_url, {type: 'url', placeholder: 'http://127.0.0.1:8001', maxlength: 512});
  const cameraId = settingsField(camera.form, '相机编号', 'settings-camera-id', '', {maxlength: 128, autocomplete: 'off'}, config.camera_id_configured ? '已保存，留空不更改。' : '尚未设置摄像头。');
  const cameraSource=el('select',null,{id:'settings-camera-source'});
  add(cameraSource,el('option','现有 Miloco 服务',{value:'miloco_service'}),el('option','已登录米家账号',{value:'miot_sdk'}));
  cameraSource.value=config.camera_media_source??'miloco_service';
  camera.form.append(add(el('label','读取方式',{for:'settings-camera-source'}),cameraSource));
  camera.form.append(el('p','验证时使用本机米家账号，结束后退出。直接读取在验证时复用本机米家账号，结束后退出。',{class:'full muted'}));
  const cameraPatch = () => ({camera_service_url: settingValue(cameraUrl), camera_media_source:cameraSource.value, ...(settingValue(cameraId) ? {camera_id: settingValue(cameraId)} : {})});
  settingsSave(camera, 'settings-camera', cameraPatch);
  const loginHref = safeSettingsLink(config.camera_service_url, ['http:', 'https:']);
  const login = el('a', '相机登录', {target: '_blank', rel: 'noopener noreferrer', id: 'settings-camera-login'});
  login.hidden = !loginHref; if (loginHref) login.setAttribute('href', loginHref); camera.controls.append(login);
  if (settingsData.checks?.camera) showCameraCheck(camera.output, settingsData.checks.camera); else settingsMessage(camera.output, '尚未检查账号');
  const cameraAccountCheck=settingsButton(camera, '检查账号', 'settings-camera-probe', () => checkSettings(camera, 'camera', cameraPatch, showCameraCheck));
  const cameraReadingMode=()=>{
    const native=cameraSource.value==='miot_sdk';
    cameraUrl.disabled=native;cameraAccountCheck.hidden=native;camera.output.hidden=native;login.hidden=native||!loginHref;
  };
  cameraSource.addEventListener('change',cameraReadingMode);cameraReadingMode();
  renderCameraMedia(camera,cameraPatch);

  const models = el('div',null,{class:'settings-credential',id:'settings-miloco-models'});
  camera.section.append(models);
  models.append(el('h3','感知模型'),el('p','结果尚未确认，请核对完整说明。建议先用 MiMo Flash。GLM、DeepSeek 保留为对照，音视频适配尚未验证。',{class:'muted'}));
  const modelForm = el('form',null,{class:'form-grid settings-form',id:'settings-model-form'});
  const modelChoice = el('select',null,{id:'settings-model-preset'});
  modelForm.append(add(el('label','模型',{for:'settings-model-preset'}),modelChoice));
  const modelKey = settingsField(modelForm,'API Key','settings-model-key','',{type:'password',autocomplete:'new-password',maxlength:4096},'仅保存在本机，留空保留同一配置。只保存在本机 Miloco；留空保留已保存的同一候选配置。');
  const modelSave = el('button',copyName('Friday-1911'),{type:'submit',id:'settings-model-save'});
  modelSave.disabled = true;modelForm.append(add(el('div',null,{class:'full settings-actions'}),modelSave));models.append(modelForm);
  const modelOutput = el('div',null,{role:'status','aria-live':'polite',id:'settings-model-status'});
  const modelPrice = el('p','',{class:'meta',id:'settings-model-price'});
  const modelProbe = button('文本验证',async()=>{
    modelProbe.disabled = true;
    try { showModelResult(await api('/api/settings/miloco/models/probe',{method:'POST',body:JSON.stringify({preset:modelChoice.value})})); }
    catch (_) {settingsMessage(modelOutput,'验证失败，可稍后重试。',true);}
    finally {modelProbe.disabled = false;}
  },{id:'settings-model-probe'});
  modelProbe.disabled = true;
  add(models,modelPrice,modelOutput,modelProbe,el('p','保存不启用感知，文本测试可能收费。保存不会启用感知。文本验证仅发送 Miloco 内置 ping，可能消耗极少量付费 token，不上传家庭影像或音频。视觉准确性与限额限制验证完成前保持未启用。',{class:'muted'}),el('p','家庭与规划模型继续由ARL提供。家庭 Agent、Laya 和规划模型继续由 ARL 提供。',{class:'muted'}));
  let modelCatalog = [];
  const showModelResult = report => {
    const messages = {saved_inactive:'候选已保存，未启用感知',already_saved:'已保留候选配置',key_required:'请在此处保存 API Key 后验证',text_probe_pass:'文本连接通过；视觉与家庭场景尚未验证',bad_key:'API Key 无效或无权限',rate_limited:'服务限流，请稍后再验证',media_adapter_required:'该模型的Miloco影音适配未完成。此模型的 Miloco 音视频适配尚未完成',inactive_candidate_required:'仅验证未启用配置，启用需原处管理。请在 Miloco 检查当前启用配置；此处只验证未启用的候选',profile_capacity_reached:'候选已满，请在Miloco管理配置。候选数量已达上限，请在 Miloco 管理已有配置',active_configuration_changed:'保存期间模型已变，请检查Miloco',save_unverified:'保存结果待核对，请重新读取；不要重复提交',service_unavailable:'Miloco 未连接，暂不能保存模型',unreachable:'模型服务暂不可达',not_found:'模型或地址不可用',probe_failed:'模型连接验证未通过'};
    settingsMessage(modelOutput,messages[report.status] ?? '模型连接尚未验证');
    if (report.catalog) {
      modelCatalog = report.catalog;
      const previous = modelChoice.value;
      modelChoice.replaceChildren(...modelCatalog.map(item=>el('option',item.label,{value:item.preset})));
      modelChoice.value = modelCatalog.some(item=>item.preset===previous) ? previous : 'mimo-flash';
      modelSave.disabled = report.status === 'service_unavailable';
      updateModelChoice();
    }
  };
  const updateModelChoice = () => {
    const item = modelCatalog.find(entry=>entry.preset===modelChoice.value);
    if (!item) return;
    modelPrice.textContent = `模型输入、输出价格与核价日期。输入 ¥${item.input_cny_per_million} / 输出 ¥${item.output_cny_per_million} 每百万 tokens · ${item.preset==='deepseek-flash' ? '峰时价；闲时半价' : '标准价'} · 核价 2026-09-30`;
    modelProbe.disabled = !item.saved || !item.key_configured || item.active || item.media_fit !== 'native_miloco_adapter';
    if (item.active) settingsMessage(modelOutput,'此配置已启用，请在Miloco管理。此候选已在 Miloco 启用，请在 Miloco 管理感知状态。');
    else if (item.saved) settingsMessage(modelOutput,item.key_configured ? '候选与 Key 已保存，未启用感知' : '候选已保存，待输入 API Key');
    else settingsMessage(modelOutput,'尚未保存此候选');
  };
  modelChoice.addEventListener('change',()=>{modelKey.value='';updateModelChoice();});
  modelForm.addEventListener('submit',async event=>{
    event.preventDefault();modelSave.disabled=true;modelProbe.disabled=true;
    const body=JSON.stringify({preset:modelChoice.value,...(modelKey.value ? {api_key:modelKey.value} : {})});modelKey.value='';
    try {showModelResult(await api('/api/settings/miloco/models/save',{method:'POST',body}));}
    catch (_) {settingsMessage(modelOutput,'保存结果未知，请重新读取，勿重复提交。保存结果待核对，请重新读取；不要重复提交。',true);}
    finally {modelSave.disabled=false;}
  });
  api('/api/settings/miloco/models').then(report=>{if(models.isConnected)showModelResult(report);}).catch(()=>{if(models.isConnected)settingsMessage(modelOutput,'暂时无法读取模型配置。',true);});
  models.append(button('刷新配置',async()=>{showModelResult(await api('/api/settings/miloco/models'));},{id:'settings-model-reload',class:'text-button'}));

  const geek = settingsSection(content, '米家极客版', '配对本地网关，读取设备与自动化。', 'settings-geek');
  const geekUrl = settingsField(geek.form, '网关地址', 'settings-geek-url', config.geek_url, {type: 'url', placeholder: 'http://192.168.1.2', maxlength: 512}, '');
  const geekPatch = () => ({geek_url: settingValue(geekUrl)});
  settingsSave(geek, 'settings-geek', geekPatch);
  const credentialDetails = el('details',null,{id:'settings-geek-credential',class:'settings-credential'});
  const credentialSummary = el('summary',geekCredentialAvailable === true ? '更换配对' : '保存配对');
  credentialDetails.open = geekCredentialAvailable !== true;
  credentialDetails.append(credentialSummary);
  const credentialForm = el('form', null, {id: 'settings-geek-credential-form', class: 'form-grid'});
  const passcode = settingsField(credentialForm, '配对码', 'settings-geek-passcode', '', {type: 'password', autocomplete: 'new-password', maxlength: 128, required: true}, '');
  const credentialSave = el('button', '保存配对', {type: 'submit', id: 'settings-geek-credential-save'}); credentialForm.append(credentialSave);
  credentialDetails.append(credentialForm);geek.section.append(credentialDetails);
  credentialForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (!savedSettings(geek, geekPatch)) { passcode.value = ''; return; }
    const body = JSON.stringify({passcode: passcode.value}); passcode.value = '';
    geekSessionRevision++;
    credentialSave.disabled = true;
    try {
      const result = await api('/api/settings/geek/credential', {method: 'POST', body});
      geekCredentialAvailable = result.credential_available === true;
      credentialDetails.open = !geekCredentialAvailable;credentialSummary.textContent = geekCredentialAvailable ? '更换配对' : '保存配对';
      settingsMessage(geek.output, '配对码已保存，可连接并后台同步。');
    }
    catch (_) { settingsMessage(geek.output, '保存失败，请重新输入配对码。', true); }
    finally { credentialSave.disabled = false; }
  });
  if (settingsData.checks?.geek) showGeekCheck(geek.output, settingsData.checks.geek);
  else settingsMessage(geek.output, geekCredentialAvailable === true ? '配对码已保存' : '尚未配对');
  const sessionOutput = el('div',null,{id:'settings-geek-session-result',class:'settings-result',role:'status','aria-live':'polite'});
  geek.section.append(sessionOutput);
  showGeekSession(sessionOutput,settingsData.geek_session??{status:'stopped'});
  const sessionBlock = {...geek,output:sessionOutput};
  settingsButton(sessionBlock,copyName('Friday-1951'),'settings-geek-connect',async()=>{
    if (!savedSettings(geek,geekPatch)) return;
    geekSessionRevision++;
    const selectedURL = settingsData.config.geek_url;
    const received = await api('/api/settings/geek/connect',{method:'POST',body:'{}'});
    if ($('settings-geek-session-result') !== sessionOutput) return;
    if (settingsData.config.geek_url !== selectedURL) {settingsMessage(sessionOutput,'配置已更改，请重新连接。');return;}
    showGeekSession(sessionOutput,received);
  });
  settingsButton(sessionBlock,'刷新状态','settings-geek-probe',async()=>{
    const revision = geekSessionRevision;
    const received=await api('/api/settings/geek/session');
    if ($('settings-geek-session-result') === sessionOutput && revision === geekSessionRevision) showGeekSession(sessionOutput,received);
  });
  settingsButton(sessionBlock,'停止同步','settings-geek-disconnect',async()=>{
    geekSessionRevision++;
    const received = await api('/api/settings/geek/disconnect',{method:'POST',body:'{}'});
    if ($('settings-geek-session-result') === sessionOutput) showGeekSession(sessionOutput,received);
  });
  if (settingsData.geek_managed_separately === true) {
    geek.form.hidden = true; credentialDetails.hidden = true;
    for (const id of ['settings-geek-connect','settings-geek-disconnect']) {const button=$(id);if(button)button.hidden=true;}
    geek.section.append(el('p','连接由现有后台管理，此处查看同步状态。',{class:'meta'}));
  }
  const arl = settingsSection(content, copyName('Friday-1506'), '连接ARL共享能力，不调用模型。通过 ARL 使用本机共享智能能力。连接不会启动模型推理。', 'settings-arl');
  const arlHome = settingsField(arl.form, copyName('Friday-1957'), 'settings-arl-home', config.arl_home, {maxlength:90}, '留空使用共享 ARL 默认目录');
  settingsSave(arl, 'settings-arl', () => ({arl_home: settingValue(arlHome)}));
  let arlConsumerSequence = 0;
  const showArl = async report => {
    const consumerSequence = ++arlConsumerSequence;
    let presentation;
    try{const {arlRuntimePresentation}=await import('/static/arl-status.js');presentation=arlRuntimePresentation(report);}
    catch(_){presentation={description:'ARL 连接状态尚未确认；身份与模型能力尚未验证。',recovery:'请重新读取连接状态。'};}
    if(!arl.output.isConnected||consumerSequence!==arlConsumerSequence||page!=='Settings')return;
    settingsMessage(arl.output,presentation.description);
    arl.output.append(el('p',presentation.recovery,{class:'meta'}));
    if (report.runtime_version) arl.output.append(el('p', 'ARL ' + report.runtime_version, {class:'meta'}));
    for (const provider of (report.providers ?? []).filter(provider => provider.name === 'codex_app_server')) {
      const state = {ready:'已发现',disabled:'尚未启用',unavailable:'不可用',not_ready:'尚未就绪'}[provider.availability] ?? '待检查';
      arl.output.append(el('p', provider.name + ' · ' + state, {class:'meta'}));
    }
    arl.output.append(el('p','Friday 仅接入 Codex。服务连接成功后，查询范围和规划执行许可仍需分别核对。',{class:'muted'}));
    if (document.body.dataset.consoleReadOnly === 'true') {
      api('/desktop/capabilities').then(capabilities => {if(arl.output.isConnected&&consumerSequence===arlConsumerSequence)showArlConsumers(arl.output,capabilities);})
        .catch(() => {if(arl.output.isConnected&&consumerSequence===arlConsumerSequence)arl.output.append(el('p','查询与规划的可用状态暂时无法确认。',{class:'meta'}));});
    }
  };
  const checkArl = async path => {
    if (arlHome.value.trim() !== (settingsData.config.arl_home ?? '')) { settingsMessage(arl.output,'请先保存后台服务目录。',true); return; }
    await showArl(await api(path,{method:'POST',body:'{}'}));
  };
  settingsButton(arl, '连接服务', 'settings-arl-connect', () => checkArl('/api/settings/arl/connect'));
  settingsButton(arl, copyName('Friday-1980'), 'settings-arl-probe', () => checkArl('/api/settings/arl/probe'));
  api('/api/settings/arl/status').then(report => {if (arl.output.isConnected) showArl(report);}).catch(() => {if (arl.output.isConnected) settingsMessage(arl.output,'暂时无法检查共享服务。',true);});
  for (const [id] of connections) { const section = $(id); if (section && selectedConnection !== id) section.setAttribute('hidden',''); }
  const measurements = settingsSection(content,'测量来源','设置测量位置、主备来源与校准。核对测量位置，设置房间主备来源与相对校准。','settings-measurements');
  measurements.form.hidden = true;
  measurements.section.hidden = selectedConnection !== 'settings-measurements';
  measurements.section.setAttribute('hidden','');
  if (selectedConnection === 'settings-measurements') {measurements.section.removeAttribute('hidden'); void loadMeasurementSettings();}
  content.append(button('服务能力',() => go('System'),{class:'text-button'}));
}

let measurementRequestSequence = 0, measurementSelectedKey = null, measurementCalibrationPending = null;
async function loadMeasurementSettings() {
  const section = $('settings-measurements'), output = $('settings-measurements-result');
  if (!section || !output) return;
  const sequence = ++measurementRequestSequence;
  measurementCalibrationPending = null;
  output.replaceChildren(el('p','正在读取测量来源…',{class:'meta'}));
  try {
    const report = await api('/api/settings/measurements');
    if ($('settings-measurements') !== section || sequence !== measurementRequestSequence) return;
    renderMeasurementSettings(output,report,sequence);
  } catch (_) {
    if ($('settings-measurements') !== section || sequence !== measurementRequestSequence) return;
    output.replaceChildren(note('测量来源暂不可读，请检查家庭连接。',true),button('重试',()=>loadMeasurementSettings(),{class:'text-button'}));
  }
}
function renderMeasurementSettings(output,report,sequence) {
  measurementCalibrationPending = null;
  const session = {busy:false};
  output.replaceChildren(el('p',report.simulated ? '示例测量来源 · simulated' : 'Home Assistant温度来源。Home Assistant · 温度来源',{class:'meta'}));
  if (!report.sources.length) {output.append(el('p','当前没有可配置的摄氏温度来源。')); return;}
  const sourceSelect = el('select',null,{id:'settings-measurement-source','aria-label':'测量来源'});
  for (const source of report.sources) sourceSelect.append(el('option',roomName(source.room)+' · '+entityLabel({name:source.name}),{value:source.source_key}));
  sourceSelect.value = report.sources.some(s=>s.source_key===measurementSelectedKey) ? measurementSelectedKey : report.sources[0].source_key;
  output.append(sourceSelect);
  if (report.omitted_count) output.append(el('p',`当前最多显示256个来源。另有 ${report.omitted_count} 个来源未列出；当前最多显示 256 个。`,{class:'meta'}));
  const editor = el('div',null,{class:'measurement-editor'}); output.append(editor);
  const draw = () => {
    const source = report.sources.find(s=>s.source_key===sourceSelect.value);
    if (!source) return;
    measurementSelectedKey = source.source_key;
    editor.replaceChildren();
    const p = source.profile;
    add(editor,el('p',source.configured ? '已保存位置配置' : p ? '示例配置' : '尚未配置位置',{class:'meta'}));
    if (!source.binding_valid) editor.append(note('来源房间已变，请核对保存，旧校准失效',true));
    const evidence = el('div',null,{class:'measurement-evidence'});
    add(evidence,badge(qualityNames[source.state?.quality] ?? '暂不可确认',source.state?.quality ?? 'unknown'),
        el('p',(source.state?.reasons ?? []).map(qualityExplanation).join(' '),{class:'muted'}));
    if (source.excluded) evidence.append(el('p','排除期限'+time(source.excluded_until)));
    editor.append(evidence);
    const form = el('form',null,{class:'form-grid settings-form',id:'settings-measurement-form'});
    const placementNames={occupied_zone:'活动区域',return_air:'回风位置'};
    const placement = settingsField(form,'测量位置','settings-measurement-placement',placementNames[p?.placement] ?? p?.placement ?? '',{maxlength:128,required:true,placeholder:'例如：床边，避开空调出风口'});
    const height = settingsField(form,copyName('Friday-2002'),'settings-measurement-height',p?.installation_height_m ?? '',{type:'number',min:0,max:10,step:.01});
    const selectField = (label,id,entries,current) => {
      const select = el('select',null,{id});
      for(const [value,text] of entries) select.append(el('option',text,{value}));
      select.value = current; form.append(add(el('label',label,{for:id}),select));return select;
    };
    const reporting = selectField('上报方式','settings-measurement-reporting',[
      ['periodic_sample','定时测量'],['change_triggered','变化触发'],['latched_state','保持状态'],['derived','推算值']],p?.reporting ?? 'periodic_sample');
    const context = selectField('工作模式','settings-measurement-context',[
      ['unknown','尚不清楚'],['ambient','环境温度'],['fan-only','空调送风'],['cooling','空调制冷'],['heating','空调制热']],p?.context ?? 'unknown');
    const role = selectField('代表位置','settings-measurement-role',[
      ['unreviewed','尚未核对'],['room_reference','已核对：房间代表位置'],['contextual_air',copyName('Friday-2017')]],source.representativeness ?? 'unreviewed');
    if(source.representativeness==='contextual_air'&&source.entity_id.includes('climate'))role.disabled=true;
    const ttl = settingsField(form,copyName('Friday-2018'),'settings-measurement-ttl',p?.ttl_seconds ?? 300,{type:'number',min:1,max:3600,step:1,required:true});
    const status = el('div',null,{id:'settings-measurement-status',role:'status','aria-live':'polite',class:'settings-result'});
    const controls = el('div',null,{class:'settings-actions'}), actions = [];
    let busy = false;
    const mutate = async (operation,extra={}) => {
      if (busy || session.busy || sequence !== measurementRequestSequence) return;
      busy = true;session.busy=true; for(const action of actions)action.disabled=true;sourceSelect.disabled=true;
      status.replaceChildren(el('p','正在保存…',{class:'meta'}));
      try {
        const response = await fetch('/api/settings/measurements/'+operation,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
          realm:report.realm,expected_revision:report.revision,source_key:source.source_key,binding_fingerprint:source.binding_fingerprint,...extra})});
        if (!response.ok) {const error=new Error('measurement_save_failed');error.conflict=response.status===409;throw error;}
        const next=await response.json();
        if ($('settings-measurements-result') !== output || sequence !== measurementRequestSequence) return;
        renderMeasurementSettings(output,next,sequence);
        $('settings-measurement-status')?.append(el('p',operation==='profile' ? '配置已保存；等待新的有效测量。' : operation==='exclude' ? '已临时排除，页面查询规则采用新质量' : '已解除排除；正在重新观察测量。'));
        // Update state consumers without replacing other connection forms or key drafts.
        const previousSnapshot=snapshot;
        try {const nextSnapshot=await api('/api/snapshot');if(snapshot===previousSnapshot){snapshot=nextSnapshot;connected=true;}} catch (_) {if(snapshot===previousSnapshot)connected=false;}
      } catch(error) {
        if ($('settings-measurements-result') !== output || sequence !== measurementRequestSequence) return;
        status.replaceChildren(note(error.conflict ? '家庭来源或配置已变化，请重新读取后保存。' : '保存失败，请检查连接后重试。',true));
      } finally {busy=false;session.busy=false;for(const action of actions)action.disabled=false;resume.disabled=!source.excluded;sourceSelect.disabled=false;}
    };
    const save = el('button','保存配置',{type:'submit',form:'settings-measurement-form',id:'settings-measurement-save',class:'primary'});
    form.addEventListener('submit',event=>{event.preventDefault();void mutate('profile',{fields:{placement:placement.value,installation_height_m:height.value===''?null:Number(height.value),reporting:reporting.value,expected_context:context.value,ttl_seconds:Number(ttl.value),representativeness:role.value}});});
    const exclude = button(copyName('Friday-2026'),()=>mutate('exclude',{duration_seconds:3600}),{id:'settings-measurement-exclude'});
    const resume = button('解除排除',()=>mutate('resume'),{id:'settings-measurement-resume'});resume.disabled=!source.excluded;
    actions.push(save,exclude,resume);add(controls,...actions);
    add(editor,form,controls,status,el('p','改设置不等于测量，更新时间不替代。位置与模式配置不代表新测量；HA 的更新时间仍不能替代设备测量时间。',{class:'meta'}),
        el('p','位置或模式改变后，需重新校准。改变位置或模式会使旧校准失效。空调回风温度需经过相对校准才能作为室温备用来源。',{class:'meta'}),
        button('刷新来源',()=>loadMeasurementSettings(),{id:'settings-measurement-reload',class:'text-button'}));
  };
  sourceSelect.addEventListener('change',draw);draw();
  renderMeasurementGroups(output,report,sequence,session);
}

function renderMeasurementGroups(output,report,sequence,session) {
  const groups = el('section',null,{class:'measurement-groups'});
  output.append(groups);
  add(groups,el('h3',copyName('Friday-2031')),el('p','主备须来自不同设备，并单独校准。主来源应位于房间代表位置。备用来源需有不同的设备身份，并单独进行相对校准。',{class:'muted'}));
  const status=el('div',null,{id:'settings-measurement-group-status',role:'status','aria-live':'polite',class:'settings-result'});
  const errors={source_conflict:'家庭来源或配置已变化，请重新读取。',independent_source_identity_required:'备用来源的设备身份尚未确认。',aliased_measurement_sources:'这两个功能属于同一来源，不能作为主备。',measurement_position_review_required:'请先核对并保存每个来源的位置。',room_reference_position_required:'请先核对主来源是否处于房间代表位置。',measurement_group_room_mismatch:'主备来源需要属于同一房间。',managed_measurement_group_required:'请先保存房间主备来源。',measurement_group_binding_changed:'来源绑定或位置已变化，请重新核对。',distinct_group_source_required:'请选择不同于主来源的备用测量源。',calibration_requires_healthy_comparable_sources:'新读数不足，模式与设备状态待确认。主备来源当前没有合格的新测量，或工作模式、健康状态尚未确认。',insufficient_distinct_calibration_pairs:'至少8对读数跨60秒，刷新不增加。合格的成对测量不足；至少需要 8 对并跨过 60 秒。刷新页面不会增加测量。',calibration_temperature_coverage_insufficient:'温度变化不足，偏差与适用范围暂不明。样本温度变化不足，暂不能确定偏差与适用范围。',calibration_candidate_expired:'预览已过期，请重新生成。',calibration_evidence_changed:'测量或依据已变化，请重新生成预览。',calibration_candidate_budget:'预览数量已达上限，稍后重试。'};
  const current=()=>sequence===measurementRequestSequence&&$('settings-measurements-result')===output;
  const send=async(path,body)=>{
    const response=await fetch('/api/settings/measurements/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({realm:report.realm,expected_revision:report.revision,...body})});
    const value=await response.json();
    if(!response.ok){const error=new Error('measurement_enrollment_failed');error.code=value.code;throw error;}
    return value;
  };
  const refreshConsumers=async()=>{const before=snapshot;try{const next=await api('/api/snapshot');if(snapshot===before)snapshot=next;}catch(_){}};
  const form=el('form',null,{class:'form-grid settings-form',id:'settings-measurement-group-form'});
  const primary=el('select',null,{id:'settings-measurement-primary','aria-label':'主要来源'});
  primary.append(el('option','选择已核对的房间代表来源',{value:''}));
  for(const source of report.sources.filter(s=>s.binding_valid&&s.representativeness==='room_reference'&&(s.configured||report.simulated)))primary.append(el('option',roomName(source.room)+' · '+entityLabel(source),{value:source.source_key}));
  const backup=el('select',null,{id:'settings-measurement-backup','aria-label':'备用来源'});
  const name=settingsField(form,'读数名称','settings-measurement-group-name','',{maxlength:96,required:true});
  form.append(add(el('label','主要来源',{for:primary.id??'settings-measurement-primary'}),primary),add(el('label','备用来源',{for:'settings-measurement-backup'}),backup));
  const save=el('button',copyName('Friday-2052'),{type:'submit',form:'settings-measurement-group-form',id:'settings-measurement-group-save',class:'primary'});
  const selectPrimary=()=>{
    const source=report.sources.find(s=>s.source_key===primary.value);
    backup.replaceChildren(el('option','暂不设置备用来源',{value:''}));
    if(source){
      const saved=(report.groups??[]).find(g=>g.location===source.room);
      name.value=saved?.name??roomName(source.room)+'室温';
      for(const other of report.sources.filter(s=>s.source_key!==source.source_key&&s.room===source.room)){
        const option=el('option',entityLabel(other)+(other.identity_fingerprint?'':' · 身份未确认'),{value:other.source_key});
        option.disabled=!source.identity_fingerprint||!other.identity_fingerprint||source.identity_fingerprint===other.identity_fingerprint||!other.binding_valid||(!other.configured&&!report.simulated);
        backup.append(option);
      }
      const existing=saved?.source_keys.find(key=>key!==source.source_key);if(existing)backup.value=existing;
    }
    save.disabled=!source;
  };
  primary.value=(report.groups??[]).find(g=>g.primary_key&&report.sources.some(s=>s.source_key===g.primary_key&&s.representativeness==='room_reference'))?.primary_key??report.sources.find(s=>s.representativeness==='room_reference'&&(s.configured||report.simulated))?.source_key??'';
  primary.addEventListener('change',selectPrimary);selectPrimary();
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(session.busy||!primary.value||!current())return;
    session.busy=true;save.disabled=true;measurementCalibrationPending=null;
    try{
      const members=[primary.value,backup.value].filter(Boolean).map(key=>{const s=report.sources.find(s=>s.source_key===key);return {source_key:s.source_key,binding_fingerprint:s.binding_fingerprint,identity_fingerprint:s.identity_fingerprint??null};});
      const next=await send('group',{name:name.value,primary_key:primary.value,members});
      if(!current())return;renderMeasurementSettings(output,next,sequence);
      $('settings-measurement-group-status')?.append(el('p','主备来源已存，旧校准失效，等待新测量'));
      await refreshConsumers();
    }catch(error){if(current())status.replaceChildren(note(errors[error.code]??'保存结果待核对，请重新读取。',true));}
    finally{session.busy=false;save.disabled=!primary.value;}
  });
  add(groups,form,save,status);
  if(!report.sources.some(s=>s.representativeness==='room_reference'&&(s.configured||report.simulated)))groups.append(el('p','请确认读数能代表房间温度。请先在上方核对测量位置，并将合适的来源标为「房间代表位置」。',{class:'meta'}));
  for(const group of report.groups??[]){
    const card=el('article',null,{class:'measurement-group', 'data-measurement-group':group.entity});
    add(card,el('h4',group.name),el('p',group.managed?'已保存主备关系':'示例主备关系 · 尚未保存',{class:'meta'}));
    const main=report.sources.find(s=>s.source_key===group.primary_key);
    const alternatives=report.sources.filter(s=>group.source_keys.includes(s.source_key)&&s.source_key!==group.primary_key);
    card.append(el('p','主来源'+(main?entityLabel(main):'来源暂不可见')+'；备用：'+(alternatives.map(s=>entityLabel(s)).join('、')||'未设置')));
    if(group.state)card.append(el('p',stateValue(group.state,true)),badge(qualityNames[group.state.quality]??'暂不可确认',group.state.quality),el('p',(group.state.reasons??[]).map(qualityExplanation).join(' '),{class:'muted'}));
    const calibration=group.calibration;
    card.append(el('p',({none:'未安装相对校准',active:'相对校准已安装',invalid:'校准依据已变化，需要重新核对',expired:'相对校准已过期'})[group.calibration_status]??'未安装相对校准'));
    if(calibration)card.append(el('p',`温度校准偏差、误差范围与有效期。偏差 ${calibration.bias.toFixed(2)} °C · 残差界限 ±${calibration.residual_bound.toFixed(2)} °C · 到期 ${time(calibration.valid_until)}`,{class:'meta'}));
    if(alternatives.length){
      const controls=el('div',null,{class:'settings-actions'}),candidateOutput=el('div',null,{class:'settings-result'});
      const choice=el('select',null,{'aria-label':group.name+'校准备用'});
      for(const source of alternatives)choice.append(el('option',entityLabel(source),{value:source.source_key}));
      const preview=button(copyName('Friday-2071'),async()=>{
        if(session.busy||!current())return;session.busy=true;preview.disabled=true;measurementCalibrationPending=null;
        candidateOutput.replaceChildren(el('p','正在核对合格的成对测量…'));
        try{
          const result=await send('calibration/preview',{group_entity:group.entity,source_key:choice.value});
          if(!current())return;
          const c=result.calibration;
          candidateOutput.replaceChildren(el('p',`合格读数数量与温度范围。${c.sample_count} 对合格测量 · 基准范围 ${c.reference_minimum.toFixed(1)}–${c.reference_maximum.toFixed(1)} °C`),
            el('p',`备用温度修正与允许误差。备用读数减去 ${c.bias.toFixed(2)} °C；残差界限 ±${c.residual_bound.toFixed(2)} °C。`),
            el('p','请核对资料范围与当前状态。这是相对于主来源的校准，不证明绝对温度正确。确认后仅在记录的模式、范围和有效期内使用。',{class:'meta'}));
          const approve=button(copyName('Friday-2076'),async()=>{
            if(session.busy||!current())return;
            if(Date.parse(result.expires_at)<=Date.now()){approve.disabled=true;candidateOutput.append(note('预览已过期，请重新生成。',true));return;}
            session.busy=true;approve.disabled=true;
            try{
              const next=await send('calibration/approve',{candidate_id:result.candidate_id,fingerprint:result.fingerprint,reviewed_relative_only:true});
              if(!current())return;renderMeasurementSettings(output,next,sequence);
              $('settings-measurement-group-status')?.append(el('p','相对校准已采用，过期或配置变更即停用。相对校准已安装；过期或来源配置变化后不再用于备用判断。'));
              await refreshConsumers();
            }catch(error){if(current()){measurementCalibrationPending=null;candidateOutput.replaceChildren(note(errors[error.code]??'安装结果待核对，请重新读取。',true));}}
            finally{session.busy=false;}
          },{class:'primary','data-calibration-approval':group.entity});
          add(candidateOutput,approve,el('p','预览期限'+time(result.expires_at),{class:'meta'}));
          measurementCalibrationPending={button:approve,output:candidateOutput,expiresAt:result.expires_at};
        }catch(error){if(current())candidateOutput.replaceChildren(note(errors[error.code]??'暂时无法生成校准，请重新读取。',true));}
        finally{session.busy=false;preview.disabled=!group.managed||!group.binding_valid;}
      },{'data-calibration-preview':group.entity});
      preview.disabled=!group.managed||!group.binding_valid;
      choice.addEventListener('change',()=>{measurementCalibrationPending=null;candidateOutput.replaceChildren();});
      add(controls,choice,preview);add(card,controls,candidateOutput);
    }
    groups.append(card);
  }
  if(report.simulated){
    const controls=el('div',null,{class:'settings-actions'});
    const load=async(scenario)=>{
      if(session.busy||!current())return;session.busy=true;measurementCalibrationPending=null;
      try{const next=await api('/api/simulator/scenario/'+scenario,{method:'POST'});if(!current())return;snapshot=next;await loadMeasurementSettings();}
      catch(_){if(current())status.replaceChildren(note('模拟场景加载失败，请重试。',true));}
      finally{session.busy=false;}
    };
    add(controls,button(copyName('Friday-2082'),()=>load('calibration-training'),{id:'settings-measurement-training'}),button(copyName('Friday-2083'),()=>load('calibrated-fallback'),{id:'settings-measurement-fallback'}));
    add(groups,el('p','校准练习仅模拟，需预览后确认。校准练习 · simulated：先保存主备关系，再加载成对测量、预览并确认。',{class:'meta'}),controls);
  }
}
function renderHomeQueryResult(output,report) {
  output.replaceChildren();
  if(report.status==='refused') {
    const reasons={outside_read_scope:'所选范围未获读取许可。',read_permission_required:'读取许可尚未就绪。',history_permission_required:'历史读取尚未获许可。',query_cursor_expired:'这次查询已过期，请重新读取。',query_context_changed:'来源或范围已变化，请重新读取。',query_busy:'正在读取另一项查询，请稍后重试。',query_source_unavailable:'家庭来源暂不可用。',query_projection_unavailable:'本机核对记录暂不可读取。本机证据暂不可读取。',query_capacity_exceeded:'当前查询较多，请稍后重试。',query_response_budget:'返回内容过大，请缩小范围。',query_projection_too_broad:'查询范围过大，请缩小范围。'};
    output.append(note(reasons[report.code] ?? '查询未完成，请重新读取。',true));return;
  }
  output.append(el('p',report.simulated ? '示例数据 · simulated' : '家庭来源报告 · 未独立确认物理效果',{class:'meta'}));
  output.append(el('p','记录起点'+time(report.snapshot_at),{class:'meta'}));
  if(!report.items.length)output.append(el('p',report.kind==='history' ? '该范围内没有可用记录；历史可能有缺口。' : '此范围内暂无可用于核对的记录'));
  const groups=new Map();
  for(const item of report.items) {
    if(item.kind==='state') {
      const key=item.device_id ?? item.entity_id;
      let group=groups.get(key);
      if(!group){group=el('article',null,{class:'automation-row'});group.append(el('h3',entityLabel({name:item.device_name ?? item.name})));groups.set(key,group);output.append(group);}
      const expired=item.quality==='healthy'&&item.valid_until&&Date.parse(item.valid_until)<=Date.now();
      const state=expired ? {...item,quality:'stale',value:null,value_interval:null} : item;
      const labels={temperature:'温度',humidity:'湿度',power:'电源','light.state':'灯状态','contact.transition':'门状态'};
      add(group,el('p',(labels[item.capability] ?? item.name)+' · '+stateValue(state,true)),badge(qualityNames[state.quality] ?? '未知',state.quality));
      if(state.quality!=='healthy')group.append(el('p',state.reasons.map(qualityExplanation).join(' '),{class:'muted'}));
      const measured=item.source_times.find(source=>source.measured_at)?.measured_at;
      group.append(el('p',measured ? '测量记录'+time(measured) : '硬件测量时间未知。',{class:'meta'}),detail('依据时间',item));
    } else if(item.kind==='device') {
      output.append(add(el('article',null,{class:'automation-row'}),el('h3',entityLabel(item)),el('p',`${item.function_ids.length} 项所选功能 · ${roomName(item.room)}`,{class:'meta'}),detail('设备依据',item)));
    } else {
      const kind={'state-transition':'状态变化','source-report':'来源报告','quality-change':'质量变化','action-result':'动作记录'}[item.event_kind] ?? '家庭记录';
      output.append(add(el('article',null,{class:'automation-row'}),el('h3',kind),el('p',time(item.event_time)),el('p',item.causation_ref ? '来源保存了触发关联，尚未独立核验。' : '记录中未保存触发原因。',{class:'muted'}),detail('依据时间',item)));
    }
  }
  if(report.kind==='history')output.append(note('请核对资料范围与当前状态。这里只包含本机保留且属于所选范围的记录。时间接近不能证明因果，外部米家规则仍可能不可见。'));
  output.append(detail('查询范围',{scope:report.permission_scope,coverage:report.coverage,limitations:report.limitations,execution_authority:report.execution_authority}));
}
function renderAsk(content) {
  mountHomeConversation(content);
  if(homeQuerySource!==snapshot){homeQueryResult=null;homeQueryRequest=null;homeQuerySequence++;}
  const rooms=[...new Set([...snapshot.entities,...(snapshot.derived_entities ?? [])].map(entity=>entity.room).filter(Boolean))];
  if(!rooms.length)return empty(content,'还没有可查询的房间','在设备来源中补全房间映射后再查询。','查看设备',()=>go('Devices'));
  if(!rooms.includes(homeQueryDraft.room))homeQueryDraft.room=rooms.includes(selectedRoom) ? selectedRoom : rooms[0];
  const block=el('section',null,{class:'surface'});
  add(block,el('h2','查看依据'),el('p','选择房间，查看设备状态和最近的记录。',{class:'muted'}));
  const form=el('form',null,{class:'settings-form',id:'home-query-form'});
  const room=el('select',null,{id:'home-query-room'}),kind=el('select',null,{id:'home-query-kind'});
  for(const value of rooms)room.append(el('option',roomName(value),{value}));room.value=homeQueryDraft.room;
  for(const [value,label] of [['state','常用状态'],['devices','设备功能'],['history',copyName('Friday-2129')]])kind.append(el('option',label,{value}));kind.value=homeQueryDraft.kind;
  const output=el('div',null,{id:'home-query-result',role:'status','aria-live':'polite'});
  const reset=()=>{homeQueryDraft={room:room.value,kind:kind.value};homeQueryResult=null;homeQueryRequest=null;homeQuerySequence++;output.replaceChildren();more.setAttribute('hidden','');};
  room.addEventListener('change',reset);kind.addEventListener('change',reset);
  add(form,add(el('label','房间'),room),add(el('label','查看什么'),kind));
  const submit=el('button','只读查询',{type:'submit',class:'primary',id:'home-query-submit'});form.append(submit);
  const more=button('更多结果',()=>read(true),{id:'home-query-more',class:'secondary'});more.setAttribute('hidden','');
  const read=async follow=>{
    if(follow&&(!homeQueryRequest||!homeQueryResult?.next_cursor))return;
    const sequence=++homeQuerySequence,source=snapshot;
    const request=follow ? {...homeQueryRequest,cursor:homeQueryResult.next_cursor} : {kind:kind.value,rooms:[room.value],limit:20};
    if(!follow&&kind.value==='state')request.capabilities=['temperature','humidity','light.state','climate.state','cover.state','lock.state','fan.state','contact.transition'];
    if(!follow&&kind.value==='history'){const end=new Date();request.end=end.toISOString();request.start=new Date(end.getTime()-3600000).toISOString();}
    submit.disabled=true;more.disabled=true;
    try {
      const report=await api('/api/query',{method:'POST',body:JSON.stringify(request)});
      if(page!=='Ask Home'||!output.isConnected||sequence!==homeQuerySequence)return;
      if(snapshot!==source||(report.simulated!==null&&report.simulated!== (source.mode==='simulator'))){output.replaceChildren(note('家庭来源已变更，请刷新后重新查询。',true));more.setAttribute('hidden','');return;}
      homeQuerySource=source;
      homeQueryRequest={...request};delete homeQueryRequest.cursor;
      homeQueryResult=follow&&report.status!=='refused' ? {...report,items:[...homeQueryResult.items,...report.items]} : report;
      renderHomeQueryResult(output,homeQueryResult);
      if(report.next_cursor)more.removeAttribute('hidden');else more.setAttribute('hidden','');
    } catch (_) {if(page==='Ask Home'&&output.isConnected)output.replaceChildren(note('查询暂不可用，请重试。',true));}
    finally{submit.disabled=false;more.disabled=false;}
  };
  form.addEventListener('submit',event=>{event.preventDefault();return read(false);});
  if(homeQueryResult){renderHomeQueryResult(output,homeQueryResult);if(homeQueryResult.next_cursor)more.removeAttribute('hidden');}
  add(block,form,el('p','这里仅查询家庭记录，不会操作设备。',{class:'meta'}),output,more);content.append(block);
}
async function refresh() {
  if(experienceView)return experienceView.refresh();
  if(page==='Files'&&filesView)return filesView.refresh();
  if(loading) return; loading=true; $('refresh').disabled=true; $('notification').textContent='';
  if (page === 'Settings') {await refreshHaStatus();await refreshGeekSession();await refreshCameraMedia();if(selectedConnection==='settings-nas')await nasConnectionRefresh?.();}
  try{snapshot=await api('/api/snapshot');loadedScenario=null;connected=true;if(page !== 'Settings')render();}catch(error){connected=false;$('notification').textContent=error.message;if(page !== 'Settings')render();}
  finally{renderSourceStatus();loading=false;$('refresh').disabled=false;}
}
let passiveLoading = false;
async function refreshPassive() {
  // Read only on viewing pages; drafts, approvals and source switches retain ownership.
  if (experienceView || loading || passiveLoading || snapshot?.mode !== 'ha-read-only' || !['Home','Activity'].includes(page) || document.hidden === true) return;
  const before = snapshot, viewing = page;
  passiveLoading = true;
  try {
    const signal = globalThis.AbortSignal?.timeout(5000);
    const next = await api('/api/snapshot',{signal});
    if (snapshot !== before || page !== viewing || next.mode !== snapshot.mode) return;
    snapshot = next; connected = true; render();
  } catch (_) {
    if (snapshot === before && page === viewing) {connected = false;render();}
  } finally {passiveLoading = false;}
}
$('brand-home')?.addEventListener('click',event => {event.preventDefault();go('Home');});
$('refresh').addEventListener('click',refresh);
$('developer-mode').addEventListener('change',()=>{if(page==='Developer'&&!$('developer-mode').checked)page='Home';navigation();render();});
const initial=pages.find(name=>name.toLowerCase().replace(' ','-')===location.hash.slice(1)); if(initial)page=initial;
render(); refresh();
// OAuth return and browser restoration must refresh authorization independently of HA polling.
globalThis.addEventListener?.('pageshow', () => { if (page === 'Settings') void refreshHaStatus(); });
globalThis.addEventListener?.('pagehide',clearHomeConversation);
globalThis.addEventListener?.('focus', () => { if (page === 'Settings') void refreshHaStatus(); });

// Age cached evidence locally; never extend timestamps, poll actions or replay plans.
setInterval(() => {
  if(archivePlayerView&&Date.parse(archivePlayerView.plan.expires_at)<=Date.now())stopArchivePlayer('播放资格已过期，请重新查询。');
  if(page==='Automations'){
    if(ruleAnalysisReview&&Date.parse(ruleAnalysisReview.expires_at)<=Date.now()){
      if($('rule-analysis-analyze'))$('rule-analysis-analyze').disabled=true;
    }
    if(coreRuleWindowEndsAt&&coreRuleWindowEndsAt<=Date.now()){const status=$('core-rule-window-status');if(status)status.textContent='窗口已过期 · 新事件不会发出设备动作';coreRuleWindowEndsAt=null;}
    if(lastCoreRulePlan&&['validated','awaiting_approval'].includes(lastCoreRulePlan.status)&&Date.parse(lastCoreRulePlan.expires_at)<=Date.now()){
      for(const id of ['core-rule-trial','core-rule-apply'])if($(id))$(id).disabled=true;
    }
  }
  if(measurementCalibrationPending&&Date.parse(measurementCalibrationPending.expiresAt)<=Date.now()){
    measurementCalibrationPending.button.disabled=true;
    measurementCalibrationPending.output.append(note('预览已过期，请重新生成。',true));measurementCalibrationPending=null;
  }
  const snapshotExpired=expireSnapshotStates(), queryExpired=expireHomeQueryStates();
  // Keep the conversation/caret intact while expiring its independent read panel.
  if(queryExpired&&page==='Ask Home'&&$('home-query-result'))renderHomeQueryResult($('home-query-result'),homeQueryResult);
  // Aging unrelated sensor cards must not tear down an open rule editor or
  // its source details. Rule deadlines and inventory have their own guards.
  if(!['Settings','Automations','Ask Home'].includes(page)&&snapshotExpired)render();
  void refreshPassive();
  void refreshGeekSession();
  void refreshCameraMedia();
  void refreshGeekInventory();
}, 10000);
