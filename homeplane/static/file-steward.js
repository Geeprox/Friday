import './copy-layout.js';
const copyName = globalThis.FridayCopyLayout.name, withCopy = globalThis.FridayCopyLayout.withCopy;
const copyLayout = globalThis.FridayCopyLayout;
const node = (tag, text, attrs = {}) => {
  const element = document.createElement(tag);
  if (text !== null && text !== undefined) element.textContent = String(text);
  for (const [key, value] of Object.entries(attrs)) if (value !== undefined && value !== null && value !== false) element.setAttribute(key, String(value));
  return copyLayout.decorate(element,text,attrs);
};
const append = (parent, ...children) => { parent.append(...children); return parent; };
const action = (label, callback, attrs = {}) => {
  const element = node('button', label, {type:'button', ...attrs});
  element.addEventListener('click', callback); return element;
};
const statusNames = {ready:'原件就绪',pending:'待核对原件',unavailable:'来源离线',missing:'原件待找回',stale:'重新核对',
  needs_extraction:'待提取',disabled:'提取待启用',queued:'待提取',paused:'继续提取',running:'提取中',done:'已索引',needs_ocr:'部分页面待识字',failed:'提取待重试',offline:'来源离线'};
const blockerNames = {destination_collision:'目标同名',destination_recheck_required:'核对目录',destination_inventory_expired:'刷新目录',destination_namespace_unverified:'核对来源',
  source_namespace_diff_required:'核对位置',declared_operation_mismatch:'核对范围',policy_preserve_name:'保留原名',policy_preserve_location:'保留位置',
  policy_preserve_tree:'保留结构',directory_policy_conflict:copyName('Friday-2161'),dependent_bundle_review_required:'整包审核'};
const bytes = value => value < 1048576 ? `${Math.ceil(value / 1024)} KB` : `${(value / 1048576).toFixed(1)} MB`;
const anchorName = anchor => anchor.kind === 'page' ? `第 ${anchor.page} 页` : anchor.kind.endsWith('_time') ?
  `${Math.floor(anchor.start_ms / 60000)}:${String(Math.floor(anchor.start_ms / 1000) % 60).padStart(2,'0')}–${Math.floor(anchor.end_ms / 60000)}:${String(Math.floor(anchor.end_ms / 1000) % 60).padStart(2,'0')}` : anchor.kind === 'image_region' ? '截图区域' : '文件';

export function mountFiles(root, {state = {tab:'Files', query:'', selected:new Set(), destinations:new Map()}, endpoint = '/api/files', onOpenConnections, readOnly = false} = {}) {
  let destroyed = false, sequence = 0, context = null, rows = [], plans = [], next = null, error = '', busy = false;
  let readAbort = null, searchScope = null, coverage = null;
  let restorePosition = Number.isFinite(state.scrollY), focusTargets = new Map();
  const focusTarget = (element, key) => {element.setAttribute('data-files-focus',key);focusTargets.set(key,element);return element;};
  const abort = new AbortController();
  root.classList.add('files-root');
  const mode = () => state.tab === 'Search' ? 'indexed_cache' : 'catalog_page';
  function cancelRead() {sequence++;readAbort?.abort();readAbort=null;}
  function clearRead() {cancelRead();rows=[];next=null;searchScope=null;coverage=null;error='';busy=false;}
  async function api(path, body, signal = abort.signal) {
    if (readOnly && body && !['/query','/settings/query'].includes(path))
      throw new Error('当前只读，内容提取与整理尚未启用。当前只读；提取与整理修改待启用。');
    const response = await fetch(endpoint + path, {signal, headers:{'Content-Type':'application/json'},
      ...(body ? {method:'POST', body:JSON.stringify(body)} : {})});
    if (!response.ok) throw new Error(response.status === 409 ? '记录已变化，请刷新后核对。' : '暂时无法读取，请重试。');
    const result = await response.json();
    if (destroyed) throw new DOMException('View closed','AbortError');
    return result;
  }
  async function refresh(cursor = null) {
    readAbort?.abort();readAbort=new AbortController();
    const signal=readAbort.signal, query=state.query, searchMode=mode();
    const ticket = ++sequence;
    busy = true;error='';rows=[];next=null;searchScope=null;coverage=null;render();
    try {
      const current = await api('/context',null,signal);
      if (destroyed || ticket !== sequence) return;
      context = current;
      const result = await api('/query', {expected_context:current.catalog.fingerprint, cursor, limit:50, query:searchMode === 'indexed_cache' ? query : '',search_mode:searchMode},signal);
      if (destroyed || ticket !== sequence || query !== state.query || searchMode !== mode()) return;
      if (searchMode === 'indexed_cache' && (result.search_scope !== 'indexed_cache' || !result.coverage ||
          result.coverage.source_read_enabled !== false || result.coverage.complete_source_scan_claimed !== false))
        throw new Error('本机索引暂不可用，请刷新核对。');
      const currentPlans = await api('/relocations',null,signal);
      if (destroyed || ticket !== sequence || query !== state.query || searchMode !== mode()) return;
      context = current; rows = result.items; plans = currentPlans; next = result.next_cursor;searchScope=result.search_scope;coverage=result.coverage;error = '';
    } catch (failure) { if (!destroyed && ticket === sequence && failure.name !== 'AbortError') error = failure.message; }
    if (!destroyed && ticket === sequence) {
      busy = false; render();
      if (restorePosition) {globalThis.scrollTo?.({top:state.scrollY,behavior:'instant'});restorePosition=false;}
    }
  }
  async function perform(callback) {
    if (busy) return;
    busy = true; error = ''; render();
    try { await callback(); } catch (failure) { error = failure.message; }
    if (!destroyed) { busy = false; render(); }
  }
  function selectedRow() {
    const candidates = state.tab === 'Inbox' ? rows.filter(row => row.extraction_status !== 'done') : rows;
    return candidates.find(row => row.item.item_id === state.focused) || candidates[0];
  }
  function settingsDraft(view) {
    return {watched:view.settings.watched,extraction_mode:view.settings.extraction_mode,
      budget:{...view.settings.budget},rule:{...view.rule}};
  }
  async function openSettings(row, replace = false) {
    if (!row || (!replace && state.settings?.itemId === row.item.item_id)) return;
    await perform(async () => {
      const view = await api('/settings/query',{expected_context:context.catalog.fingerprint,sample_item_id:row.item.item_id});
      state.settings={itemId:row.item.item_id,view,draft:settingsDraft(view),dirty:false,advanced:false};
      state.focusNext='settings:watched';
    });
  }
  function renderSettings(container, row) {
    const settings=state.settings, view=settings.view, draft=settings.draft;
    container.append(node('p','目录设置',{class:'files-eyebrow'}),node('h2',view.rule.label),
      node('p',view.settings.directory_ref || '共享目录',{class:'files-scope'}));
    const savedStatus=node('p',settings.dirty ? '有改动未保存' : view.saved ? '已保存 · 提取待启用' : '尚未保存',{class:'files-state',role:'status'});
    container.append(savedStatus);
    if (context.session?.handoff_service) container.append(node('p',
      `${context.session.configuration_saved ? '来源设置已保存' : '来源待配置'} · ${context.session.readable === true ? '可读已核对' : context.session.readable === false ? '读取不可用' : '可读待核对'} · 写入待启用`,{class:'files-state'}));
    if (readOnly) {
      container.append(node('p','当前只读，目录修改与内容提取未启用。当前只读；目录设置修改与内容提取待启用。',{class:'files-state'}),
        action('刷新设置',() => openSettings(row,true),{disabled:busy ? '' : undefined}),
        action('返回详情',() => {state.settings=null;state.focusNext=`row:${row.item.item_id}`;render();}));
      return;
    }
    const form=node('form',null,{class:'files-settings'});
    const dirty=() => {settings.dirty=true;savedStatus.textContent='有改动未保存';};
    const checkbox=(key,label,values) => {
      const wrap=node('label',null,{class:'files-setting-check'}), input=focusTarget(node('input',null,{type:'checkbox'}),`settings:${key}`);
      input.checked=values[key];input.disabled=busy;
      input.addEventListener('change',() => {values[key]=input.checked;dirty();});wrap.append(input,node('span',label));return wrap;
    };
    form.append(checkbox('watched','关注目录',draft));
    const modeLabel=node('label','提取方式'), mode=focusTarget(node('select',null,{'aria-label':'提取方式'}),'settings:mode');
    for (const [value,label] of [['metadata_only',copyName('Friday-2180')],['pdf_text_first',copyName('Friday-2181')]]) mode.append(node('option',label,{value}));
    mode.value=draft.extraction_mode;mode.disabled=busy;
    mode.addEventListener('change',() => {draft.extraction_mode=mode.value;dirty();});modeLabel.append(mode);form.append(modeLabel);
    const nameLabel=node('label','目录标签'), name=focusTarget(node('input',null,{type:'text',maxlength:80,'aria-label':'目录标签'}),'settings:label');
    name.value=draft.rule.label;name.disabled=busy;name.addEventListener('input',() => {draft.rule.label=name.value;dirty();});nameLabel.append(name);form.append(nameLabel);
    const rules=node('fieldset');rules.append(node('legend','整理预览'));
    for (const [key,label] of [['rename_preview','改名建议'],['move_preview','移动建议'],['structure_preview','结构建议'],['dedup_review','核对重复']])
      rules.append(checkbox(key,label,draft.rule));
    form.append(rules);
    if (view.effective_policy.source === 'file_exception') form.append(node('p','此文件继续采用单独规则。',{class:'files-state'}));
    if (view.effective_policy.source === 'policy_conflict') form.append(node('p','目录规则冲突 · 保留原样',{class:'files-error'}));
    const advanced=node('details',null,{class:'files-settings-advanced'});advanced.open=settings.advanced;
    advanced.append(node('summary','提取限额'));
    advanced.addEventListener('toggle',() => {settings.advanced=advanced.open;});
    for (const [key,label,min,max,scale,step] of [['source_bytes',copyName('Friday-2190'),1,8,1048576,1],['units',copyName('Friday-2191'),1,8,1,1],
      ['seconds',copyName('Friday-2192'),0.1,5,1,0.1],['text_chars','文字上限',1,32000,1,1]]) {
      const wrap=node('label',label), input=focusTarget(node('input',null,{type:'number',min,max,step,'aria-label':label}),`settings:budget:${key}`);
      input.value=draft.budget[key]/scale;input.disabled=busy;
      input.addEventListener('input',() => {draft.budget[key]=Number(input.value)*scale;dirty();});wrap.append(input);advanced.append(wrap);
    }
    form.append(advanced,focusTarget(node('button','保存设置',{type:'submit',disabled:busy ? '' : undefined}),'settings:save'));
    form.addEventListener('submit',event => {
      event.preventDefault();void perform(async () => {
        const saved=await api('/settings/preview',{expected_context:context.catalog.fingerprint,sample_item_id:row.item.item_id,
          expected_fingerprint:view.fingerprint,...draft});
        settings.view=saved;settings.draft=settingsDraft(saved);settings.dirty=false;
        await refresh();
      });
    });
    container.append(form,action('刷新设置',() => openSettings(row,true),{disabled:busy ? '' : undefined}),
      action('返回详情',() => {state.settings=null;state.focusNext=`row:${row.item.item_id}`;render();}));
  }
  function renderInspector(container) {
    const row = selectedRow();
    if (!row) return;
    if (state.settings?.itemId === row.item.item_id) {renderSettings(container,row);return;}
    const item = row.item, policy = row.policy;
    container.append(node('p','原件内容',{class:'files-eyebrow'}),node('h2',row.title || '内容待提取'),
      node('p',policy.label,{class:'muted'}));
    const policyDetail = node('details',null,{class:'files-policy'});
    policyDetail.append(node('summary','目录规则'),node('p',policy.source === 'file_exception' ? copyName('Friday-2198') : policy.source === 'directory_policy' ? '采用已固定的目录规则' : copyName('Friday-2200')));
    for (const [key, label] of [['rename_preview','改名'],['move_preview','移动'],['structure_preview','目录结构'],['dedup_review','核对重复']])
      policyDetail.append(node('p',`${label}：${policy[key] ? '允许预览' : '保留'}`));
    if (policy.matched_rule_ids.length) policyDetail.append(node('p',`规则 ${policy.matched_rule_ids.join(' · ')}`,{class:'muted'}));
    container.append(policyDetail);
    const referenceList = node('div',null,{class:'files-references'});
    referenceList.append(node('h3','内容引用'));
    if (!row.references.length) referenceList.append(node('p','提取完成后显示页码与片段。',{class:'muted'}));
    for (const unit of row.references) {
      const ref = node('details');
      ref.append(node('summary',`${anchorName(unit.anchor)} · ${unit.title || '内容片段'}`),node('p',unit.text),
        node('p',`原件 ${unit.anchor.content_id.slice(0,12)} · ${unit.provider}`,{class:'muted'}));
      referenceList.append(ref);
    }
    for (const page of row.page_outcomes || []) if (page.status === 'needs_ocr')
      referenceList.append(node('p',`${anchorName(page.anchor)} · 待 OCR`,{class:'files-state'}));
    container.append(referenceList);
    container.append(action('目录设置',() => openSettings(row),{disabled:busy ? '' : undefined}));
    if (readOnly) {container.append(node('p','当前只读，原件不改不移，提取未启用。当前只读；原件不改名、不移动，提取待启用。',{class:'files-state'}));return;}
    const jobIds=new Set((row.jobs || []).map(job => job.job_id));
    for (const entry of context.queue || []) if (jobIds.has(entry.job_id)) {
      const names={held:'提取待启用',waiting:'待提取',claimed:'提取中',paused:'计划已暂停',done:'已完成',needs_ocr:'部分页面待识字',cancelled:'计划已取消',failed:'提取待核对',offline:'来源离线',stale:'核对来源'};
      container.append(node('p',names[entry.status],{class:'files-state'}));
      const control=decision => perform(async () => {
        let settingsFingerprint=entry.settings_fingerprint;
        if (decision === 'resume_preview') settingsFingerprint=(await api('/settings/query',{
          expected_context:context.catalog.fingerprint,sample_item_id:item.item_id})).fingerprint;
        await api('/queue/review',{expected_context:context.catalog.fingerprint,job_id:entry.job_id,
          expected_revision:entry.revision,expected_settings_fingerprint:settingsFingerprint,decision});
        context=await api('/context');
      });
      if (['paused','failed','offline','stale','cancelled'].includes(entry.status))
        container.append(action(copyName('Friday-2217'),() => control('resume_preview'),{disabled:busy ? '' : undefined}));
      if (!['done','needs_ocr','cancelled'].includes(entry.status))
        container.append(action('取消计划',() => control('cancel'),{disabled:busy ? '' : undefined}));
    }
    if (item.status === 'ready') container.append(action('规划提取',() => perform(async () => {
      const provider = item.media_kind === 'image' ? 'ocr' : ['audio','video'].includes(item.media_kind) ? 'asr_keyframes' : 'docling';
      await api('/jobs/preview',{expected_context:context.catalog.fingerprint,item_ids:[item.item_id],provider});
      await refresh();
    }),{disabled:busy ? '' : undefined}));
    if (item.status === 'ready' && (policy.rename_preview || policy.move_preview) && !policy.bundle_review_required) {
      container.append(action('改名建议',() => perform(async () => {
        const suggestion = await api('/suggest',{expected_context:context.catalog.fingerprint,item_id:item.item_id});
        state.suggestion = suggestion;
        if (!state.destinations.has(item.item_id) && suggestion.status === 'suggested')
          state.destinations.set(item.item_id,[suggestion.folder,suggestion.stem].filter(Boolean).join('/'));
      })));
      if (state.suggestion?.item_id === item.item_id) container.append(node('p',state.suggestion.title || '等待内容提取',{class:'muted'}));
      const input = node('input',null,{type:'text','aria-label':copyName('Friday-2222'),placeholder:copyName('Friday-2222')});
      focusTarget(input,`destination:${item.item_id}`);
      input.value = state.destinations.get(item.item_id) || '';
      input.addEventListener('input',() => state.destinations.set(item.item_id,input.value));
      container.append(input);
    }
    container.append(node('p',`内容 ID ${item.original_hash?.slice(0,16) || '待核对'}`,{class:'files-content-id'}));
  }
  function renderPlans(container) {
    if (!plans.length) { container.append(node('p','还没有整理预览。',{class:'muted'})); return; }
    for (const plan of plans) {
      const article = node('article',null,{class:'files-plan'});
      const labels = {awaiting_review:'待审核',blocked:'需要核对',reviewed_preview:'已确认预览',rejected:'已放下'};
      article.append(node('h2',labels[plan.status]),node('p',`所选文件、保留文件与同名文件数。${plan.counts.selected} 个文件 · ${plan.counts.protected} 个保留 · ${plan.counts.collision} 个同名`,{class:'muted'}));
      for (const entry of plan.entries) article.append(append(node('div',null,{class:'files-plan-entry'}),
        node('p',entry.before_relative_path || '原位置'),node('p',`→ ${entry.destination}`),node('small',entry.policy_label),
        node('p',withCopy(entry.blockers.map(b => blockerNames[b] || '重新核对').join(' · '),...entry.blockers.map(b=>blockerNames[b])),{class:'files-state'})));
      if (!readOnly && ['awaiting_review','blocked'].includes(plan.status)) {
        const review = decision => perform(async () => {
          await api('/relocations/review',{expected_context:context.catalog.fingerprint,plan_id:plan.plan_id,
            expected_plan_fingerprint:plan.plan_fingerprint,expected_inventory_fingerprint:plan.inventory_fingerprint,
            selected_entry_ids:plan.entries.map(e => e.entry_id),decision});
          plans = await api('/relocations');
        });
        article.append(action(copyName('Friday-2232'),() => review('approve_preview'),{disabled:plan.status === 'blocked' || busy ? '' : undefined}),
          action(copyName('Friday-2233'),() => review('reject'),{disabled:busy ? '' : undefined}));
      }
      container.append(article);
    }
  }
  function render() {
    if (destroyed) return;
    const active = document.activeElement, activeKey = active?.getAttribute?.('data-files-focus');
    if (busy && activeKey) state.busyFocus=activeKey;
    const selection = [active?.selectionStart,active?.selectionEnd], position = globalThis.scrollY;
    focusTargets = new Map();
    const restore = () => {
      const fallback=(!active || active === document.body) ? state.busyFocus : null;
      const target = focusTargets.get(state.focusNext || activeKey || fallback);state.focusNext=null;
      if (!busy) state.busyFocus=null;
      target?.focus?.({preventScroll:true});
      if (Number.isInteger(selection[0])) target?.setSelectionRange?.(...selection);
      if (Number.isFinite(position)) globalThis.scrollTo?.({top:position,behavior:'instant'});
    };
    root.replaceChildren();
    const toolbar = node('div',null,{class:'files-toolbar'}), tabs = node('nav',null,{'aria-label':'文件页面'});
    for (const [key, label] of [['Files','全部文件'],['Inbox','收件箱'],['Search','搜索'],['Review','审核']])
      tabs.append(focusTarget(action(label,() => {
        const previous=mode(), wasReview=state.tab === 'Review';state.tab=key;
        if (key === 'Review') {cancelRead();busy=false;render();}
        else if (previous !== mode() || wasReview) {clearRead();void refresh();}
        else render();
      },{'aria-current':state.tab === key ? 'page' : 'false'}),`tab:${key}`));
    toolbar.append(tabs,node('span',context ? context.session?.label || (context.catalog.source_online ? '目录在线' : '来源离线 · 保留索引') : '正在读取目录',{class:'files-state',role:'status'}));
    if (['authentication_required','connection_required'].includes(context?.session?.reason) && onOpenConnections)
      toolbar.append(action('核对连接',onOpenConnections));
    root.append(toolbar);
    if (readOnly) root.append(node('p','目前只读，提取和整理尚未启用。当前只读；提取、整理和目录设置修改待启用。',{class:'files-state'}));
    if (error) root.append(append(node('div',null,{class:'files-error'}),node('p',error,{role:'status'}),action('重试',() => refresh(),{disabled:busy ? '' : undefined})));
    if (context?.catalog.simulated) root.append(node('p','示例文件',{class:'files-eyebrow'}));
    if (state.tab === 'Review') { renderPlans(root); restore(); return; }
    if (state.tab === 'Search') {
      const form = node('form',null,{class:'files-search'}), input = node('input',null,{type:'search','aria-label':'搜索已索引内容',placeholder:copyName('Friday-2245')});
      focusTarget(input,'search');
      input.value=state.query;input.addEventListener('input',() => {state.query=input.value;clearRead();render();});
      form.append(input,node('button','搜索',{type:'submit'}));form.addEventListener('submit',event => {event.preventDefault();void refresh();});root.append(form);
      root.append(node('p',copyName(coverage?.source_online === false ? 'Friday-2363' : 'Friday-2361'),{class:'files-scope'}),
        node('p',copyName('Friday-2362'),{class:'muted'}));
    }
    const visible = state.tab === 'Inbox' ? rows.filter(row => row.extraction_status !== 'done') : rows;
    const bar = node('div',null,{class:'files-selection'});
    bar.append(node('span',state.tab === 'Search' ? `${visible.length} 项匹配 · 本机索引` : `${visible.length} 个文件 · 当前目录页`));
    const selectedRows = rows.filter(row => state.selected.has(row.item.item_id));
    if (!readOnly && selectedRows.length === 2 && state.selected.size === 2 && selectedRows.every(row => row.policy.dedup_review))
      bar.append(action('核对重复',() => perform(async () => {
        state.comparison = await api('/compare',{expected_context:context.catalog.fingerprint,item_ids:selectedRows.map(row => row.item.item_id)});
      })));
    if (!readOnly && state.selected.size) bar.append(action(`预览 ${state.selected.size} 个文件`,() => perform(async () => {
      const selections = rows.filter(row => state.selected.has(row.item.item_id) && state.destinations.get(row.item.item_id)).map(row => ({
        item_id:row.item.item_id,expected_content_id:row.item.original_hash,destination:state.destinations.get(row.item.item_id),operation:'rename_and_move'}));
      if (!selections.length || selections.length !== state.selected.size) throw new Error('先填写每个文件的目标路径。');
      await api('/relocations/preview',{expected_context:context.catalog.fingerprint,selections,
        inventory:{root_ref:'outputs-preview',namespace_revision:'unverified',checked_at:new Date().toISOString(),complete:false,paths:[],basis:'operator_preview'}});
      plans=await api('/relocations');state.tab='Review';
    })));
    root.append(bar);
    if (state.comparison) root.append(node('p',state.comparison.relation === 'exact_hash' ?
      `请核对资料范围与当前状态。内容完全相同 · ${state.comparison.evidence.original_count} 条已观察位置记录` : state.comparison.relation === 'possible_version' ?
      '提取文字相近 · 分别保留，核对版本' : '内容不同，或缺少确认依据。内容不同或证据不足',{class:'files-state'}));
    const layout = node('div',null,{class:'files-layout'}), list = node('div',null,{class:'files-list'}), inspector = node('aside',null,{class:'files-inspector','aria-label':'文件详情'});
    for (const row of visible) {
      const item = row.item, line = node('div',null,{class:'files-row' + (state.focused === item.item_id ? ' selected' : '')});
      const select = node('input',null,{type:'checkbox','aria-label':`选择 ${row.title || '原件 ' + item.item_id.slice(0,8)}`});
      focusTarget(select,`select:${item.item_id}`);
      select.checked=state.selected.has(item.item_id);select.disabled=readOnly || item.status !== 'ready' || busy;
      select.addEventListener('change',() => {state.comparison=null;select.checked ? state.selected.add(item.item_id) : state.selected.delete(item.item_id);render();});
      const main = focusTarget(action(null,() => {state.focused=item.item_id;render();},{class:'files-row-main'}),`row:${item.item_id}`);
      main.append(node('strong',row.title || ({image:'图片',video:'视频',audio:'音频',other:'文档'}[item.media_kind]) + ' · ' + item.item_id.slice(0,8)),
        node('span',`${bytes(item.size_bytes)} · ${row.policy.label}`,{class:'muted'}));
      line.append(select,main,node('span',statusNames[row.extraction_status] || statusNames[item.status],{class:'files-state'}));list.append(line);
    }
    if (!visible.length) list.append(node('p',busy ? '正在读取…' : state.tab === 'Search' ? copyName(searchScope === 'indexed_cache' ? 'Friday-2364' : 'Friday-2365') : '当前目录页没有文件。',{class:'muted'}));
    if (next) list.append(action(state.tab === 'Search' ? '更多结果' : '下一页',() => refresh(next)));
    renderInspector(inspector);layout.append(list,inspector);root.append(layout);
    restore();
  }
  const ready = refresh();
  return {ready, refresh, destroy() {state.scrollY=globalThis.scrollY;destroyed=true;cancelRead();abort.abort();root.classList.remove('files-root');root.replaceChildren();}};
}
