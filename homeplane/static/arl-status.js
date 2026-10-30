// Public connection metadata cannot qualify identity, authorization or a model task.
export function arlPresentation(report) {
  const value = report && typeof report === 'object' ? report : {};
  const status = value.status;
  const freshness = value.metadataFreshness;
  const pending = value.refreshPending === true;
  let state = 'unknown';
  if (pending) state = 'refreshing';
  else if (freshness === 'last_known') state = 'stale';
  else if (freshness === 'current' || freshness === 'unavailable') {
    if (status === 'ready' && value.ready === true && freshness === 'current') state = 'connected';
    else if (status === 'disconnected') state = 'disconnected';
    else if (status === 'not_running') state = 'not_running';
    else if (status === 'sdk_missing') state = 'sdk_missing';
    else if (status === 'user_disabled') state = 'disabled';
    else if (status === 'unsafe_home') state = 'unsafe_configuration';
    else if (status === 'starting') state = 'starting';
  }
  const task = '身份与模型能力尚未验证';
  const rows = {
    connected: ['通信正常', `ARL 健康通信正常；${task}。`, '在本次任务预览中核对模型能力与授权。'],
    disconnected: ['连接失败', 'ARL 连接检查失败，健康通信暂不可用。', '检查本机 ARL 服务后重新读取。'],
    not_running: ['尚未运行', 'ARL 尚未运行。', '在本机启动已配置的 ARL 后重新读取。'],
    sdk_missing: ['需要安装', '本机尚未安装 ARL SDK。', '按部署指南安装 ARL SDK 后重新读取。'],
    disabled: ['已停用', 'ARL 已停用。', '需要使用时，在 ARL 本机设置中启用后重新读取。'],
    unsafe_configuration: ['需要检查', 'ARL 本机配置未通过安全检查。', '在本机检查 ARL 配置与文件权限后重新读取。'],
    starting: ['正在启动', 'ARL 正在启动，连接就绪状态待确认。', '等待启动完成后重新读取。'],
    unknown: ['待核对', 'ARL 连接状态尚未确认。', '重新读取连接状态；若仍不可读，在 Mac 版检查 ARL 服务。'],
  };
  let row = rows[state];
  if (state === 'stale') {
    const description = status === 'ready' && value.ready === true
      ? `ARL 上次健康通信正常；当前连接待核对，${task}。`
      : status === 'disconnected'
        ? 'ARL 上次连接检查失败；当前连接待核对。'
        : 'ARL 仅有上次检查记录；当前连接待核对。';
    row = ['上次记录', description, '重新读取连接状态；历史记录不能确认当前连接。'];
  }
  if (state === 'refreshing') {
    const description = freshness === 'last_known'
      ? status === 'ready' && value.ready === true
        ? `ARL 上次健康通信正常；正在刷新，${task}。`
        : status === 'disconnected'
          ? 'ARL 上次连接检查失败；正在刷新。'
          : 'ARL 正在刷新，保留上次检查结果；当前连接状态待确认。'
      : 'ARL 连接状态正在检查，尚无最近确认记录。';
    row = ['正在核对', description, '等待本次检查完成后重新读取。'];
  }
  return {
    state, connected: state === 'connected', label: row[0], description: row[1], recovery: row[2],
    taskReadiness: 'unverified', identityVerified: false, modelCapabilityVerified: false,
  };
}

// Settings uses the SDK's snake_case runtime.status metadata. Keep exactly the
// same health/freshness projection as the desktop response, without promoting
// configured verifiers, provider declarations or previous tasks to permission.
export function arlRuntimePresentation(report) {
  const value = report && typeof report === 'object' ? report : {};
  return arlPresentation({
    status: value.status,
    ready: value.ready,
    metadataFreshness: value.metadata_freshness,
    refreshPending: value.metadata_refresh_pending,
  });
}
