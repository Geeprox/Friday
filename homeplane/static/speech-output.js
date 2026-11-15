/* 选择 Friday 的输出目标，沿用系统音量与现有的安静模式。 */
export function mountSpeechOutput(container, {api, isAlive = () => true, toast = () => {}}) {
 const base = '/api/embodied/speech-output';
 let current = null;
 let generation = 0;
 const names = {mac:'这台 Mac', panel:'通知设备', media_player:'媒体播放器', homepod:'HomePod'};

 function el(tag, text, attrs = {}) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
 }

 const section = el('section', null, {class:'speech-output-settings', 'aria-label':'Friday 播报音箱'});
 container.append(section);
 if (!document.getElementById('friday-speech-output-style')) {
  document.head.append(el('link', null, {
   id:'friday-speech-output-style', rel:'stylesheet', href:'/static/speech-output.css'
  }));
 }
 const alive = () => isAlive() && section.isConnected;
 function button(text, action, attrs = {}) {
  const node = el('button', text, {type:'button', ...attrs});
  node.onclick = action;
  return node;
 }

 function reason(result) {
  const messages = {
   homepod_not_found:'还未找到这台 HomePod，请检查它的局域网地址。',
   homepod_setup_required:'先查找并选择一台 HomePod。',
   homepod_pairing_required:'这台 HomePod 需要完成 AirPlay 配对。',
   homepod_playback_state_unavailable:'暂时无法读取音箱播放状态，请稍后重试。',
   homepod_connection_unavailable:'暂时连不上 HomePod，请检查它的局域网连接。',
   homepod_stream_unavailable:'这台音箱当前无法接收语音播报。',
   mac_speech_unavailable:'Mac 的系统语音暂不可用。'
  };
  return messages[result.reason] || '试听未完成，请检查声音输出。';
 }

 async function refresh() {
  const revision = ++generation;
  try {
   const value = await api(base);
   if (!alive() || revision !== generation) return;
   current = value;
   render();
  } catch {
   if (alive() && revision === generation) {
    section.replaceChildren(el('h2', '播报音箱'), el('p', '音箱设置暂时无法读取。'), button('重试', refresh));
   }
  }
 }

 function render() {
  let saving = false;
  const view = current;
  const selected = view.output === 'jbl' ? 'media_player' : view.output;
  const mac = selected === 'mac';
  const canTest = (mac || selected === 'homepod') && view.configured === true && !view.quiet;
  const heading = el('div', null, {class:'speech-output-heading'});
  heading.append(el('h2', '播报音箱'));
  const output = el('select', null, {'aria-label':'播报到'});
  for (const [value, name] of Object.entries(names)) {
   if (value !== 'mac' || mac || view.mac_available) output.append(el('option', name, {value}));
  }
  output.value = selected;
  heading.append(output);
  const status = el('p', null, {class:'speech-output-status', role:'status'});
  if (!view.configured) status.textContent = mac ? 'Mac 的系统语音暂不可用。' : '尚未设置播报设备';
  else if (mac) status.textContent = '使用 Mac 当前的声音输出。';
  else if (selected === 'homepod') status.textContent = (view.homepod.name || 'HomePod') + ' · 已选择';

  const actions = el('div', null, {class:'speech-output-actions'});
  const lookup = button('查找 HomePod', () => find());
  const probe = button('检查连接', async () => {
   probe.disabled = true;
   status.textContent = '正在检查连接…';
   try {
    const result = await api(base + '/probe', {});
    if (alive()) {
     if (result.status === 'ready') status.textContent = 'HomePod 已连接，可以播报。';
     else if (result.status === 'busy') status.textContent = 'HomePod 正在播放，暂不播报。';
     else status.textContent = reason(result);
    }
   } catch {
    if (alive()) status.textContent = '连接检查未完成，请重试。';
   } finally {
    probe.disabled = false;
   }
  });
  const test = button('试听播报', async () => {
   if (test.disabled) return;
   test.disabled = true;
   status.textContent = '正在试听…';
   try {
    const result = await api(base + '/test', {output:selected});
    if (!alive()) return;
    if (result.status === 'accepted') status.textContent = mac ? '试听已完成。' : '测试语音已发送。';
    else if (result.status === 'paused') status.textContent = '安静模式已开启。';
    else if (result.status === 'busy') status.textContent = 'Friday 正在播报，稍后再试。';
    else status.textContent = reason(result);
   } catch {
    if (alive()) status.textContent = '请核对当前播报设备后再试听。';
   } finally {
    test.disabled = !canTest;
   }
  });
  probe.disabled = !view.homepod.configured;
  test.disabled = !canTest;
  if (!mac) actions.append(lookup);
  if (selected === 'homepod') actions.append(probe);
  actions.append(test);
  section.replaceChildren(heading, status, actions);
  if (view.quiet) section.append(el('p', '安静模式已开启。', {class:'caption'}));

  const results = el('div', null, {class:'speech-output-devices'});
  const advanced = el('details', null, {class:'speech-output-address'});
  const form = el('form');
  const host = el('input', null, {
   type:'text', inputmode:'decimal', placeholder:'例如 192.0.2.123', 'aria-label':'HomePod IP 地址'
  });
  const connect = el('button', '查找', {type:'submit'});
  form.append(host, connect);
  form.onsubmit = event => {
   event.preventDefault();
   void find(host.value.trim());
  };
  advanced.append(el('summary', '按 IP 地址查找'), form);

  function showDiscovery() {
   if (!lookup.isConnected) actions.append(lookup);
   if (!results.isConnected) section.append(results, advanced);
  }

  async function save(payload, device) {
   if (saving) return;
   saving = true;
   output.disabled = true;
   for (const option of results.children) option.disabled = true;
   try {
    await api(base, payload);
    if (alive()) {
     if (device) toast('播报音箱已设为 ' + device.name);
     await refresh();
    }
   } catch {
    if (alive()) {
     status.textContent = '设置未保存，请重试。';
     output.value = selected;
    }
   } finally {
    saving = false;
    output.disabled = false;
    for (const option of results.children) option.disabled = false;
   }
  }

  async function find(address) {
   if (saving || lookup.disabled) return;
   showDiscovery();
   lookup.disabled = connect.disabled = output.disabled = true;
   results.replaceChildren();
   status.textContent = '正在查找 HomePod…';
   try {
    const query = address ? '?' + new URLSearchParams({host:address}) : '';
    const result = await api(base + '/devices' + query);
    if (!alive()) return;
    output.value = selected;
    if (!result.devices?.length) {
     status.textContent = '还未发现 HomePod。';
     return;
    }
    status.textContent = '选择要使用的 HomePod。';
    for (const device of result.devices) {
     const payload = {output:'homepod', identifier:device.identifier, ...(address ? {host:address} : {})};
     results.append(button(device.name, () => save(payload, device), {class:'speech-output-device'}));
    }
   } catch {
    if (alive()) status.textContent = '查找未完成，请检查家庭网络。';
   } finally {
    lookup.disabled = connect.disabled = output.disabled = false;
   }
  }

  if (!mac) showDiscovery();
  output.onchange = async () => {
   if (output.value === 'homepod') {
    await find();
    return;
   }
   await save({output:output.value});
  };
 }

 section.append(el('p', '正在读取播报音箱…'));
 void refresh();
 return {dispose() { generation++; section.remove(); }};
}
