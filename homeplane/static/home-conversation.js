import './copy-layout.js';
const copyName = globalThis.FridayCopyLayout.name, withCopy = globalThis.FridayCopyLayout.withCopy;
const copyLayout = globalThis.FridayCopyLayout;
/* F0 transport + original domain projections. No permissions or sessions minted here. */
const MAX_TURNS = 16;
const MAX_HISTORY_BYTES = 16384;
const MAX_RESPONSE_BYTES = 65536;
const bytes = value => new TextEncoder().encode(value).length;
const opaque = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value);
const uid = prefix => `${prefix}:${crypto.randomUUID()}`;

function element(tag, text, attrs = {}) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, String(value));
  return copyLayout.decorate(node,text,attrs);
}

function button(label, action, attrs = {}) {
  const node = element('button', label, {type: 'button', ...attrs});
  node.addEventListener('click', action);
  return node;
}

function domainOf(response) {
  return response?.domain?.report ?? response?.domain;
}

function planOf(response) {
  const domain = domainOf(response);
  return domain?.plan ?? (domain?.old?.rule && domain?.new_rule ? domain : null);
}

function validResponse(response) {
  if (!response || !opaque(response.session_id) || !opaque(response.turn_id) ||
      !opaque(response.operation_id) || !opaque(response.code) ||
      !['console_text', 'simulated_fixture', 'qualified_ambient'].includes(response.input_kind) ||
      typeof response.caption !== 'string' || response.caption.length > 2048 ||
      (response.choices && (!Array.isArray(response.choices) || response.choices.length > 16 ||
        response.choices.some(choice => !opaque(choice.id) || typeof choice.label !== 'string' || choice.label.length > 256))) ||
      !['clarify', 'select', 'preview', 'result', 'cancelled', 'unavailable', 'refused'].includes(response.phase)) return false;
  for (const [name, values] of Object.entries({
    asr_admission: ['unavailable', 'unqualified', 'qualified'], visual_admission: ['unavailable', 'unqualified', 'qualified'],
    native_graph: ['unavailable', 'opaque', 'qualified'], nas_lease: ['closed', 'unavailable', 'metadata_only', 'content_qualified'],
    output_audibility: ['unavailable', 'unverified', 'qualified'],
  })) if (response[name] !== undefined && !values.includes(response[name])) return false;
  if (response.domain !== undefined && response.domain !== null && (typeof response.domain !== 'object' || Array.isArray(response.domain))) return false;
  if (response.limitations !== undefined && (!Array.isArray(response.limitations) || response.limitations.length > 16 || response.limitations.some(line => typeof line !== 'string' || bytes(line) > 1024))) return false;
  if (response.plan_id !== undefined && response.plan_id !== null && !opaque(response.plan_id)) return false;
  for (const name of ['context_token', 'approval_digest']) if (response[name] !== undefined && response[name] !== null && !/^[a-f0-9]{64}$/.test(response[name])) return false;
  try { return bytes(JSON.stringify(response)) <= MAX_RESPONSE_BYTES; } catch { return false; }
}

export function mount(container, {sourceToken, client, initialDraft = '', recovery = null} = {}) {
  if (!container?.append) throw new TypeError('home_conversation_container_required');
  if (typeof initialDraft !== 'string' || initialDraft.length > 1024 || bytes(initialDraft) > 4096) throw new TypeError('invalid_local_draft');
  const state = {
    sourceToken, client, sessionId: null, generation: 0, sequence: 0, disposed: false,
    records: [], active: null, pending: null, trimmed: 0, clocks: [],
    draftText: initialDraft, recovery, recoveryButtons: [], sessionDeadline: null,
  };
  const root = element('section', null, {class: 'hc-root', 'aria-label': '家庭对话'});
  const heading = element('h2', '对话');
  const status = element('p', '', {class: 'hc-status', role: 'status', 'aria-live': 'polite'});
  function showStatus(name, description) {
    status.replaceChildren(element('strong', name, {'data-hc': 'state'}), element('span', description, {'data-hc': 'state-description'}));
  }
  const history = element('details', null, {class: 'hc-history'});
  history.open = true;
  const summary = element('summary', '本次对话');
  const log = element('ol', null, {class: 'hc-log', role: 'log', 'aria-live': 'polite', 'aria-relevant': 'additions'});
  const retention = element('p', '本次来源最多保留 16 轮对话。', {class: 'hc-meta'});
  history.append(summary, log, retention);
  const form = element('form', null, {class: 'hc-composer'});
  const inputId = uid('hc-input');
  const label = element('label', '输入文字', {for: inputId});
  const input = element('textarea', null, {id: inputId, rows: '3', maxlength: '1024', placeholder: '例如：以后我回家不要自动开空调', 'data-hc': 'input'});
  input.value = initialDraft;
  const controls = element('div', null, {class: 'hc-controls'});
  const submit = element('button', '发送', {type: 'submit', class: 'primary', 'data-hc': 'send'});
  const fresh = button('新问题', () => {
    abandon();
    state.active = null;
    state.draftText = input.value;
    state.recovery = null;
    input.focus();
    render();
  }, {'data-hc': 'new', class: 'hc-secondary'});
  const stop = button('停止请求', () => void stopCurrent(), {'data-hc': 'stop', class: 'hc-secondary'});
  controls.append(submit, fresh, stop);
  const help = element('details', null, {class: 'hc-details'});
  help.append(element('summary', '详情'), element('p', '批准只作用于当前预览。Esc 停止，⌘ / Ctrl + Enter 发送。原文只在当前窗口保留。', {class: 'hc-meta'}));
  form.append(label, input, controls, help);
  const narrowViewport = globalThis.matchMedia?.('(max-width: 520px)');
  const visualViewport = globalThis.visualViewport;
  const returnToInput = button('回到输入', () => {
    if (state.disposed) return;
    input.focus({preventScroll: true});
    form.scrollIntoView?.({block: 'end', inline: 'nearest', behavior: 'auto'});
  }, {class: 'hc-return hc-secondary', 'data-hc': 'return-input', 'aria-controls': inputId, hidden: ''});
  root.append(heading, status, history, form, returnToInput);
  container.append(root); // preserve the existing structured Ask query and other content

  function viewportBounds() {
    const top = visualViewport?.offsetTop ?? 0;
    const height = visualViewport?.height ?? globalThis.innerHeight;
    return {top, height, bottom: top + height};
  }

  function updateInputNavigation() {
    if (state.disposed) return;
    const box = form.getBoundingClientRect?.(), view = viewportBounds();
    const visible = box && box.top >= view.top && box.bottom <= view.bottom;
    if (narrowViewport?.matches && state.records.length && (!visible || document.activeElement === returnToInput)) returnToInput.removeAttribute('hidden');
    else {
      if (document.activeElement === returnToInput) input.focus({preventScroll: true});
      returnToInput.setAttribute('hidden', '');
    }
  }

  function updateComposerLayout() {
    if (state.disposed) return;
    const box = form.getBoundingClientRect?.(), view = viewportBounds();
    // Leave at least half the viewport for reading; low-height/zoomed views use document flow.
    const sticky = !!(narrowViewport?.matches && box?.height > 0 &&
      Number.isFinite(view.height) && box.height + 48 <= view.height / 2 &&
      (visualViewport?.scale ?? 1) === 1);
    const keepFocusedInput = root.getAttribute('data-hc-sticky') === 'true' && !sticky && document.activeElement === input;
    root.setAttribute('data-hc-sticky', String(sticky));
    root.style?.setProperty('--hc-composer-height', sticky ? `${box.height}px` : '0px');
    if (keepFocusedInput) input.scrollIntoView?.({block: 'nearest', inline: 'nearest', behavior: 'auto'});
    updateInputNavigation();
  }

  function revealReply(record, pending = false) {
    if (!current(record)) return;
    const caption = record.article?.querySelector('.hc-caption');
    if (!caption) return;
    history.open = true;
    if (narrowViewport?.matches) {
      caption.scrollIntoView?.({block: 'start', inline: 'nearest', behavior: 'auto'});
    } else if (pending) log.scrollTop = log.scrollHeight;
    else {
      const target = caption.getBoundingClientRect?.(), viewport = log.getBoundingClientRect?.();
      if (target && viewport) log.scrollTop = (log.scrollTop || 0) + target.top - viewport.top - (log.clientTop || 0) - 12;
    }
    updateInputNavigation();
  }

  function transportReady() {
    return state.sessionId && (!Number.isFinite(state.sessionDeadline) || Date.now() < state.sessionDeadline)
      && typeof state.client?.send === 'function' && typeof state.client?.cancel === 'function';
  }

  function current(record) {
    return state.active?.record === record && !state.disposed;
  }

  function remember(record) {
    state.records.push(record);
    while (state.records.length > MAX_TURNS || state.records.reduce((total, item) => total + bytes(item.text + (item.response?.caption ?? '') + (item.error ?? '')), 0) > MAX_HISTORY_BYTES) {
      state.records.shift();
      state.trimmed++;
    }
  }

  function clearMedia() {
    for (const video of root.querySelectorAll('video')) {
      video.pause();
      video.removeAttribute('src');
      video.load();
    }
  }

  function cancelRequest(captured) {
    return {
      session_id: captured.session_id, turn_id: uid('turn'), operation_id: captured.operation_id,
      idempotency_key: uid('key'), kind: 'cancel', lane: captured.lane ?? 'auto',
      ...(captured.plan_id ? {plan_id: captured.plan_id} : {}),
      ...(captured.expected_context ? {expected_context: captured.expected_context} : {}),
    };
  }

  function bestEffortCancel(captured) {
    if (!captured || typeof state.client?.cancel !== 'function') return;
    try { Promise.resolve(state.client.cancel(cancelRequest(captured), {})).catch(() => {}); } catch { /* local fence already set */ }
  }

  function abandon() {
    const captured = state.pending?.request ?? state.active?.request;
    ++state.sequence;
    state.pending?.controller.abort();
    state.pending = null;
    clearMedia();
    bestEffortCancel(captured);
  }

  function resetSource(nextToken, recovery) {
    abandon();
    ++state.generation;
    state.sourceToken = nextToken;
    state.sessionId = null;
    state.sessionDeadline = null;
    state.active = null;
    state.records = [];
    state.trimmed = 0;
    state.recovery = recovery;
    input.value = state.draftText;
  }

  function expireSession() {
    if (!state.sessionId || !Number.isFinite(state.sessionDeadline) || Date.now() < state.sessionDeadline) return false;
    resetSource(null, 'expired');
    return true;
  }

  function canEdit(record) {
    return current(record) && !state.pending && typeof record.editableText === 'string'
      && (!input.value || input.value === record.editableText)
      && (!state.draftText || state.draftText === record.editableText);
  }

  function continueEditing(record) {
    if (!canEdit(record)) return;
    // Only local text is restored. No cancel, submit, context or approval is replayed.
    ++state.sequence;
    state.active = null;
    clearMedia();
    state.draftText = record.editableText;
    input.value = state.draftText;
    state.recovery = 'editing';
    render();
    input.focus();
  }

  function qualifier(response) {
    const labels = {
      unavailable: '未接入', unqualified: '未验证', qualified: '已验证', opaque: '不可见',
      closed: '关闭', metadata_only: '仅目录', content_qualified: '已核验内容权限', unverified: '未验证可听见',
    };
    const values = [
      ['输入', {console_text: '文字对话', simulated_fixture: copyName('Friday-2286'), qualified_ambient: '现场输入'}[response.input_kind] ?? response.input_kind],
      ['语音', labels[response.asr_admission ?? 'unavailable']],
      ['视觉', labels[response.visual_admission ?? 'unavailable']],
      ['本机规则', labels[response.native_graph ?? 'unavailable']],
      ['NAS', labels[response.nas_lease ?? 'closed']],
      ['房间输出', labels[response.output_audibility ?? 'unavailable']],
    ];
    return withCopy(values.map(([name, value]) => `${name} ${value}`).join(' · '),...values.map(([,value])=>value));
  }

  function addPlan(article, record, response) {
    const plan = planOf(response);
    if (!plan) return;
    const block = element('section', null, {class: 'hc-plan', 'aria-label': '规则差异'});
    block.append(element('h3', response.phase === 'cancelled' ? copyName('Friday-2293') : '本次差异'));
    const surviving = new Set((plan.new_rule?.actions ?? []).map(action => action.id));
    const choice = domainOf(response)?.choices?.find(item => item.record_id === plan.old?.id && item.expected_fingerprint === plan.old?.fingerprint);
    const diff = element('ul', null, {class: 'hc-diff'});
    for (const action of plan.old?.rule?.actions ?? []) {
      const retained = surviving.has(action.id);
      const name = !retained && choice ? choice.action_label : `${action.target ?? action.endpoint ?? action.id} · ${action.type}`;
      diff.append(element('li', `${retained ? '保留' : '移除'} · ${name}`, {class: retained ? 'hc-retain' : 'hc-remove'}));
    }
    const preserved = element('details', null, {class: 'hc-details'});
    preserved.append(element('summary', copyName('Friday-2296')));
    for (const [key, label] of [['trigger', '触发'], ['conditions', '条件'], ['execution', '执行'], ['safety', '安全']]) {
      const before = JSON.stringify(plan.old?.rule?.[key]), after = JSON.stringify(plan.new_rule?.[key]);
      preserved.append(element('p', `${label} · ${before === after ? '保留' : '变化'} · ${after}`, {class: 'hc-meta'}));
    }
    block.append(diff, preserved);
    if (response.phase === 'preview' && response.plan_id && response.approval_digest) {
      const action = button(copyName('Friday-2302'), () => void follow(record, 'approve', {
        plan_id: response.plan_id, approval_digest: response.approval_digest,
      }), {'data-hc': 'approve', class: 'primary'});
      action.disabled = !current(record) || !!state.pending;
      const expiry = element('p', '', {class: 'hc-meta', 'data-hc': 'expiry'});
      const deadline = Math.min(Date.parse(response.expires_at), Date.parse(plan.expires_at));
      state.clocks.push(() => {
        const remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
        action.disabled = !current(record) || !!state.pending || !Number.isFinite(deadline) || remaining === 0;
        expiry.textContent = remaining > 0 ? `本次审批窗口剩余 ${remaining} 秒。` : '本次审批窗口已过期，请重新预览。';
      });
      const row = element('div', null, {class: 'hc-controls'});
      row.append(action, button('取消预览', () => void stopCurrent(), {'data-hc': 'cancel-plan', class: 'hc-secondary'}));
      row.lastElementChild.disabled = !current(record);
      block.append(expiry, row);
    }
    if (['needs_reconcile', 'verified'].includes(plan.status) && response.plan_id && current(record)) {
      const row = element('div', null, {class: 'hc-controls'});
      if (plan.status === 'needs_reconcile') row.append(button(copyName('Friday-2306'), () => void follow(record, 'reconcile', {plan_id: response.plan_id}), {'data-hc': 'reconcile'}));
      row.append(button('恢复配置', () => void follow(record, 'rollback', {plan_id: response.plan_id}), {'data-hc': 'rollback'}));
      block.append(row, element('p', '配置回滚不撤销过去的物理动作。', {class: 'hc-meta'}));
    }
    article.append(block);
  }

  function addEvidence(article, record, response) {
    const domain = domainOf(response);
    if (!domain) return;
    if (domain.explanation?.statements?.length) {
      const block = element('section', null, {class: 'hc-evidence', 'aria-label': '依据范围'});
      block.append(element('h3', '依据范围'));
      for (const statement of domain.explanation.statements.slice(0, 16)) {
        block.append(element('p', statement.caption), element('p', `${statement.level} · ${(statement.evidence_refs ?? []).join('、')}`, {class: 'hc-meta'}));
      }
      if (domain.explanation.complete_household_history === false) block.append(element('p', '现有记录不覆盖全部家庭历史。', {class: 'hc-meta'}));
      article.append(block);
    }
    if (domain.archive_status) {
      const block = element('section', null, {class: 'hc-evidence', 'aria-label': '原片引用'});
      block.append(element('h3', '原片引用'), element('p', `${domain.archive_status.status} · ${domain.archive_status.reason}`));
      for (const camera of domain.archive_status.choices ?? []) {
        const choose = button(camera.label, () => void follow(record, 'turn', {lane: 'why', camera_id: camera.id}), {'data-hc': 'camera'});
        choose.disabled = !current(record) || !!state.pending;
        block.append(choose);
      }
      for (const segment of domain.archive?.segments ?? []) {
        const line = element('div', null, {class: 'hc-reference'});
        line.append(element('p', `${segment.camera_id} · ${segment.status}`), element('p', `${segment.time_range.start} — ${segment.time_range.end}`, {class: 'hc-meta'}));
        if (segment.status === 'ready' && opaque(segment.media_ref)) {
          const prepare = button('查看原片', () => void follow(record, 'playback', {lane: 'why', evidence_ref: segment.media_ref}), {'data-hc': 'playback'});
          prepare.disabled = !current(record) || !!state.pending;
          line.append(prepare);
        } else line.append(element('p', segment.reason, {class: 'hc-meta'}));
        block.append(line);
      }
      if (domain.playback) {
        const plan = domain.playback;
        const deadline = Math.min(Date.parse(plan.expires_at), Date.parse(response.expires_at));
        if (current(record) && response.nas_lease === 'content_qualified' &&
            /^\/api\/archive\/media\/[a-f0-9]{64}$/.test(plan.stream_url) && deadline > Date.now()) {
          const start = Number(plan.start_offset_seconds), end = Number(plan.end_offset_seconds);
          if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start) {
            block.append(element('p', '原领域播放区间不可用。', {class: 'hc-meta'}));
            article.append(block);
            return;
          }
          const video = element('video', null, {controls: '', preload: 'none', 'aria-label': '所选原片；显式点击播放'});
          video.muted = true;
          const clamp = () => {
            if (video.currentTime < start) video.currentTime = start;
            if (video.currentTime >= end) { video.pause(); video.currentTime = Math.max(start, end - 0.001); }
          };
          video.addEventListener('loadedmetadata', () => { video.currentTime = start; });
          video.addEventListener('seeking', clamp);
          video.addEventListener('timeupdate', clamp);
          video.addEventListener('play', () => {
            if (video.currentTime >= end - 0.001) video.currentTime = start;
            clamp();
          });
          video.src = plan.stream_url;
          block.append(video, element('p', `所选录像区间与原片时钟误差。所选区间 ${start} — ${end} 秒；原片时钟误差 ${plan.clock_uncertainty_seconds} 秒。`, {class: 'hc-meta'}),
            element('p', plan.simulated ? '仅供模拟，不能证明实际效果。模拟原片源；不证明真实 NAS 或房间可听见。' : '原片时钟与播放资格以原领域结果为准。', {class: 'hc-meta'}));
          state.clocks.push(() => {
            if (Date.now() >= deadline && video.getAttribute('src')) {
              video.pause(); video.removeAttribute('src'); video.load();
              bestEffortCancel(record.request);
            }
          });
        } else block.append(element('p', '内容权限、播放期限或媒体路由未就绪。', {class: 'hc-meta'}));
      }
      article.append(block);
    }
  }

  function addActionTrace(article, record, response) {
    const trace = domainOf(response);
    if (!trace?.action_id || !trace.request || !trace.route) return;
    const block = element('section', null, {class: 'hc-plan', 'aria-label': '动作记录'});
    block.append(element('h3', response.phase === 'cancelled' ? copyName('Friday-2320') : response.phase === 'preview' ? '本次动作预览' : '原动作'),
      element('p', `${trace.request.target} · ${trace.request.operation ?? 'light.set'} · ${trace.status} · ${trace.code}`),
      element('p', `分别显示实际写入与独立实物确认。真实写入尝试 ${trace.real_write_attempted} · 独立物理确认 ${trace.physically_confirmed}`, {class: 'hc-meta'}));
    block.append(element('p', `原绝对目标值 ${JSON.stringify(trace.request.desired)}。`, {class: 'hc-meta'}));
    if (response.phase === 'preview' && response.plan_id === trace.action_id && response.approval_digest) {
      const action = button(copyName('Friday-2325'), () => void follow(record, 'approve', {
        plan_id: response.plan_id, approval_digest: response.approval_digest,
      }), {'data-hc': 'approve', class: 'primary'});
      const expiry = element('p', '', {class: 'hc-meta'});
      const deadline = Math.min(Date.parse(response.expires_at), Date.parse(trace.request.expires_at));
      state.clocks.push(() => {
        const remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
        action.disabled = !current(record) || !!state.pending || !Number.isFinite(deadline) || remaining === 0;
        expiry.textContent = remaining > 0 ? `本次审批窗口剩余 ${remaining} 秒。` : '本次审批窗口已过期，请重新预览。';
      });
      block.append(expiry, action);
    }
    if (trace.status === 'unknown_outcome' && response.plan_id === trace.action_id && current(record)) {
      const reconcile = button(copyName('Friday-2326'), () => void follow(record, 'reconcile', {plan_id: response.plan_id}), {'data-hc': 'reconcile'});
      reconcile.disabled = !!state.pending;
      block.append(reconcile, element('p', '结果未知，读取原记录不会重新发送动作。结果尚未确认，读取原记录不会重新发送动作。', {class: 'hc-meta'}));
    }
    article.append(block);
  }

  function addModeAndOutput(article, response) {
    const domain = domainOf(response);
    if (domain?.effective) {
      const mode = element('p', '', {class: 'hc-mode', 'data-hc': 'mode'});
      const effective = domain.effective;
      state.clocks.push(() => {
        const until = Date.parse(effective.valid_until);
        const remaining = Math.max(0, Math.min(Number(effective.remaining_seconds) || 0, Math.ceil((until - Date.now()) / 1000)));
        const label = {sleep: '显式睡眠', manual: '手动', awake: '清醒', unknown: '模式未知'}[effective.mode] ?? effective.mode;
        const statusLabel = {expired: '已过期', unavailable: '未接入', unknown: '状态未知'}[effective.status] ?? effective.status;
        mode.textContent = effective.status === 'active' && Number.isFinite(remaining) && remaining > 0 ? `当前状态、剩余时间与处理服务。${label} · 剩余 ${remaining} 秒 · ${effective.backend}` :
          `当前模式需核对，请查看完整说明。${label ?? '模式未知'} · ${effective.status === 'active' ? '展示窗口已过期或时间未知' : statusLabel} · 剩余 0 秒 · ${effective.backend}`;
      });
      article.append(mode, element('p', '适用范围以执行服务和有效时间为准。覆盖范围以原 runtime 和有效窗口为准。', {class: 'hc-meta'}));
    }
    if (response.domain?.output) {
      const output = response.domain.output;
      if (output.simulated === true) article.append(element('p', '模拟房间反馈；不证明真实房间可听见。', {class: 'hc-meta'}));
    }
  }

  function addTechnicalRecord(details, response) {
    const plan = planOf(response);
    if (plan) {
      details.append(element('p', `原领域状态：${plan.status} · ${plan.code}`, {class: 'hc-meta'}),
        element('p', `原规则编号与内容校验码。原规则 ${plan.old?.id} · fingerprint ${plan.old?.fingerprint}`, {class: 'hc-meta'}));
    }
    if (response.domain?.output) {
      const output = response.domain.output;
      details.append(element('p', `房间反馈：${output.status} · ${output.code}`),
        element('p', `配置、调用与可听见状态分别显示。已配置 ${output.configured} · 尝试 ${output.attempted} · 调用完成 ${output.completed} · 模拟 ${output.simulated} · 可听见 ${output.audibility}`, {class: 'hc-meta'}));
    }
  }

  function tick() {
    if (expireSession()) { render(); return; }
    for (const updateClock of state.clocks) updateClock();
  }

  function render() {
    if (state.disposed) return;
    expireSession();
    clearMedia();
    log.replaceChildren();
    state.clocks = [];
    state.recoveryButtons = [];
    for (const record of state.records) {
      const item = element('li', null, {class: 'hc-turn'});
      item.append(element('p', record.text, {class: 'hc-user'}));
      const article = element('article', null, {class: 'hc-answer', tabindex: '-1'});
      if (record.response) {
        const response = record.response;
        article.append(element('p', response.caption, {class: 'hc-caption'}));
        if (response.choices?.length) {
          const choices = element('div', null, {class: 'hc-choices', 'aria-label': '澄清选择'});
          for (const choice of response.choices) {
            const select = button(choice.label, () => void follow(record, 'turn', {candidate_id: choice.id}), {'data-hc': 'choice'});
            select.disabled = !current(record) || !!state.pending;
            choices.append(select);
          }
          article.append(choices);
        }
        addPlan(article, record, response);
        addActionTrace(article, record, response);
        addEvidence(article, record, response);
        addModeAndOutput(article, response);
        if (response.limitations?.length) {
          const coverage = element('details', null, {class: 'hc-details'});
          coverage.append(element('summary', '来源范围'));
          for (const line of response.limitations) coverage.append(element('p', line));
          article.append(coverage);
        }
        const details = element('details', null, {class: 'hc-details'});
        details.append(element('summary', response.phase === 'cancelled' ? copyName('Friday-2344') : copyName('Friday-2345')));
        details.append(element('p', qualifier(response), {class: 'hc-meta'}));
        addTechnicalRecord(details, response);
        details.append(element('pre', JSON.stringify(response.domain, null, 2)));
        article.append(details);
      } else {
        article.append(element('h3', record.error ? '待核对' : '处理中'),
          element('p', record.error ? '请求结果尚未确认' : '正在等待本次结果', {class: 'hc-caption'}));
        if (record.error) {
          if (typeof record.editableText === 'string') {
            const edit = button('继续编辑', () => continueEditing(record), {'data-hc': 'edit', class: 'hc-secondary'});
            edit.disabled = !canEdit(record);
            state.recoveryButtons.push({edit, record});
            article.append(edit);
          }
          const details = element('details', null, {class: 'hc-details'});
          details.append(element('summary', '详情'), element('p', '原请求结果尚未确认。先核对原记录；继续编辑只恢复文字，不会重发操作。'));
          article.append(details);
        }
      }
      item.append(article);
      log.append(item);
      record.article = article;
    }
    retention.textContent = state.trimmed ? `仅显示最近 ${state.records.length} 轮对话` : '本次最多保留 16 轮对话';
    if (!transportReady()) {
      const states = {connecting: ['开始中', '原文保留，可继续编辑'], expired: ['已过期', '旧预览失效，原文仍保留'], unknown: ['待确认', '会话未确认，原文仍保留']};
      showStatus(...(states[state.recovery] ?? ['需连接', '先重新开始，再发送文字']));
    } else if (state.pending) showStatus('处理中', '正在处理，可停止本次请求');
    else if (state.recovery === 'editing') showStatus('继续编辑', '核对原记录后再发送');
    else if (state.active?.record.error) showStatus('待核对', '请求结果尚未确认');
    else showStatus('对话', '按当前范围读取');
    submit.disabled = !transportReady() || !!state.pending;
    fresh.disabled = !transportReady();
    stop.disabled = !transportReady() || !state.active;
    tick();
    updateComposerLayout();
  }

  async function send(request, text, useCancel = false, editableText = null) {
    if (!transportReady() || state.disposed) return;
    const generation = state.generation, token = state.sourceToken, sequence = ++state.sequence;
    const controller = new AbortController();
    const record = {request, text, response: null, error: null, editableText};
    remember(record);
    state.active = {request, record};
    state.pending = {request, controller};
    render();
    revealReply(record, true);
    try {
      const response = await state.client[useCancel ? 'cancel' : 'send'](request, {signal: controller.signal});
      if (state.disposed || generation !== state.generation || !Object.is(token, state.sourceToken) || sequence !== state.sequence || controller.signal.aborted) return;
      if (!validResponse(response) || response.session_id !== state.sessionId || response.turn_id !== request.turn_id || response.operation_id !== request.operation_id) throw new Error('invalid_flow_response');
      record.response = response;
      if (['flow_session_unavailable', 'flow_context_changed'].includes(response.code)) { resetSource(null, 'expired'); render(); }
    } catch (error) {
      if (state.disposed || generation !== state.generation || sequence !== state.sequence || controller.signal.aborted) return;
      record.error = 'flow_response_unconfirmed';
    } finally {
      if (!state.disposed && generation === state.generation && sequence === state.sequence) {
        state.pending = null;
        render();
        if (record.response?.phase === 'clarify') input.focus();
        else record.article?.focus({preventScroll: true});
        revealReply(record);
      }
    }
  }

  function follow(record, kind, fields = {}) {
    if (expireSession()) { render(); return; }
    if (!current(record) || state.pending || !record.response || !transportReady()) return;
    const response = record.response;
    if (kind === 'approve' && !(response.phase === 'preview' && Date.parse(response.expires_at) > Date.now())) return;
    const request = {
      session_id: state.sessionId, turn_id: uid('turn'), operation_id: record.request.operation_id,
      idempotency_key: uid('key'), kind, lane: record.request.lane ?? 'auto',
      ...(response.context_token ? {expected_context: response.context_token} : {}), ...fields,
    };
    const label = {approve: copyName('Friday-2351'), reconcile: copyName('Friday-2306'), rollback: '恢复配置', playback: '查看原片'}[kind] ?? '选择候选';
    return send(request, label);
  }

  async function stopCurrent() {
    const captured = state.pending?.request ?? state.active?.request;
    if (!captured || state.disposed) return;
    ++state.sequence;
    state.pending?.controller.abort();
    state.pending = null;
    clearMedia();
    await send(cancelRequest(captured), '停止请求', true);
  }

  function onSubmit(event) {
    event.preventDefault();
    if (expireSession()) { render(); return; }
    if (state.pending || !transportReady()) return;
    const originalText = input.value;
    const text = originalText.trim();
    if (!text || text.length > 1024 || bytes(text) > 4096 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(text)) {
      showStatus('需修改', '请缩短或检查文字后发送');
      input.focus();
      return;
    }
    const continuing = ['clarify', 'select'].includes(state.active?.record.response?.phase);
    if (!continuing) abandon();
    const operationId = continuing ? state.active.request.operation_id : uid('operation');
    const context = continuing ? state.active.record.response.context_token : null;
    state.draftText = originalText;
    state.recovery = null;
    input.value = '';
    void send({session_id: state.sessionId, turn_id: uid('turn'), operation_id: operationId,
      idempotency_key: uid('key'), kind: 'turn', lane: 'auto', text,
      ...(context ? {expected_context: context} : {})}, text, false, originalText);
  }

  function onKey(event) {
    if (event.key === 'Escape' && state.active) { event.preventDefault(); void stopCurrent(); }
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) onSubmit(event);
  }
  function onDraftInput() {
    state.draftText = input.value;
    for (const {edit, record} of state.recoveryButtons) edit.disabled = !canEdit(record);
  }
  input.addEventListener('input', onDraftInput);
  form.addEventListener('submit', onSubmit);
  root.addEventListener('keydown', onKey);
  globalThis.addEventListener?.('resize', updateComposerLayout, {passive: true});
  globalThis.addEventListener?.('scroll', updateInputNavigation, {passive: true});
  narrowViewport?.addEventListener?.('change', updateComposerLayout);
  visualViewport?.addEventListener?.('resize', updateComposerLayout, {passive: true});
  visualViewport?.addEventListener?.('scroll', updateInputNavigation, {passive: true});
  const composerObserver = typeof globalThis.ResizeObserver === 'function' ? new globalThis.ResizeObserver(updateComposerLayout) : null;
  composerObserver?.observe(form);
  const timer = setInterval(tick, 1000);
  render();

  return Object.freeze({
    getDraft() { return state.disposed ? '' : state.draftText; },
    update({sourceToken: nextToken = state.sourceToken, response, recovery = null} = {}) {
      if (state.disposed) return;
      if (!Object.is(nextToken, state.sourceToken)) resetSource(nextToken, recovery);
      else state.recovery = recovery;
      // F5 bootstraps a server-created session through the existing response
      // seam. Mount never invents a session from a source label or UUID.
      if (response && validResponse(response) && (!state.sessionId || response.session_id === state.sessionId)) {
        if (!state.sessionId) state.sessionDeadline = Date.parse(response.expires_at);
        state.sessionId = response.session_id;
        if (state.active && response.operation_id === state.active.request.operation_id &&
            response.turn_id === state.active.request.turn_id && !state.pending) state.active.record.response = response;
        if (response.caption) status.textContent = response.caption;
      }
      render();
    },
    dispose() {
      if (state.disposed) return;
      abandon();
      ++state.generation;
      state.disposed = true;
      clearInterval(timer);
      composerObserver?.disconnect();
      globalThis.removeEventListener?.('resize', updateComposerLayout);
      globalThis.removeEventListener?.('scroll', updateInputNavigation);
      narrowViewport?.removeEventListener?.('change', updateComposerLayout);
      visualViewport?.removeEventListener?.('resize', updateComposerLayout);
      visualViewport?.removeEventListener?.('scroll', updateInputNavigation);
      input.removeEventListener('input', onDraftInput);
      form.removeEventListener('submit', onSubmit);
      root.removeEventListener('keydown', onKey);
      root.remove();
      state.records = [];
      state.active = null;
      state.sessionId = null;
      state.draftText = '';
      state.sessionDeadline = null;
    },
  });
}

export const HomeConversation = Object.freeze({mount});
