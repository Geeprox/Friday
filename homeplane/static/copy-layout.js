/* Reviewed base copy; new candidate entries still require independent copy review. */
(function (global) {
  if (global.FridayCopyLayout) return;
  const descriptions = {
  "Friday-0892": {
    "name": "选择样本",
    "description": "可拆分人物或纠正归属"
  },
  "Friday-0968": {
    "name": "状态匹配",
    "description": "HA返回符合目标，实物效果未核验"
  },
  "Friday-1017": {
    "name": "打开幅度",
    "description": "按百分比设置打开程度"
  },
  "Friday-1033": {
    "name": "连接设置",
    "description": "管理家庭服务、账号与隐私"
  },
  "Friday-1057": {
    "name": "新建对话",
    "description": "重新开始一次60秒的家庭对话"
  },
  "Friday-1088": {
    "name": "测量设置",
    "description": "查看位置配置及测量依据"
  },
  "Friday-1090": {
    "name": "室温来源",
    "description": "查看来源与读数有效时间"
  },
  "Friday-1093": {
    "name": "校准状态",
    "description": "查看统一读数及校准版本"
  },
  "Friday-1168": {
    "name": "批准调整",
    "description": "按所显示的目标调整设备"
  },
  "Friday-1169": {
    "name": "核对状态",
    "description": "只读取设备，不发送操作"
  },
  "Friday-1171": {
    "name": "示例执行",
    "description": "只用于示例家庭"
  },
  "Friday-1174": {
    "name": "状态报告",
    "description": "设备返回的状态报告"
  },
  "Friday-1191": {
    "name": "关联记录",
    "description": "有相关触发记录，未确认完整原因"
  },
  "Friday-1200": {
    "name": "原片位置",
    "description": "已取得原片的位置引用"
  },
  "Friday-1228": {
    "name": "录像来源",
    "description": "NAS上的原录像，只读"
  },
  "Friday-1236": {
    "name": "只读录像",
    "description": "NAS原录像仅供读取"
  },
  "Friday-1244": {
    "name": "选择相机",
    "description": "选择已有录像的摄像头"
  },
  "Friday-1254": {
    "name": "录像设置",
    "description": "设置NAS录像目录与时钟"
  },
  "Friday-1258": {
    "name": "模拟保存",
    "description": "只模拟原片保存"
  },
  "Friday-1260": {
    "name": "重置示例",
    "description": "只重置模拟录像资料"
  },
  "Friday-1270": {
    "name": "米家习惯",
    "description": "仅显示极客版可见的规则"
  },
  "Friday-1284": {
    "name": "原站管理",
    "description": "在原平台管理规则"
  },
  "Friday-1299": {
    "name": "变更历史",
    "description": "查看变更，并可恢复之前的配置"
  },
  "Friday-1319": {
    "name": "安装习惯",
    "description": "确认后在本机执行习惯"
  },
  "Friday-1322": {
    "name": "恢复配置",
    "description": "恢复本次安装前的配置"
  },
  "Friday-1349": {
    "name": "亮度设定",
    "description": "留空沿用当前灯光设定"
  },
  "Friday-1350": {
    "name": "开灯亮度",
    "description": "可以不填"
  },
  "Friday-1357": {
    "name": "手动接管",
    "description": "接管期间规则停止执行"
  },
  "Friday-1360": {
    "name": "确认测试",
    "description": "清醒测试限60秒"
  },
  "Friday-1377": {
    "name": "模型分析",
    "description": "分析并准备移除一个习惯动作"
  },
  "Friday-1379": {
    "name": "选择习惯",
    "description": "选择要交给模型分析的习惯"
  },
  "Friday-1397": {
    "name": "批准分析",
    "description": "只批准这一次模型分析"
  },
  "Friday-1434": {
    "name": "结果待查",
    "description": "切换结果尚未核对"
  },
  "Friday-1453": {
    "name": "触发条件",
    "description": "同时查看保护限制"
  },
  "Friday-1457": {
    "name": "模拟触发",
    "description": "只触发这一次模拟"
  },
  "Friday-1458": {
    "name": "应用示例",
    "description": "只修改示例家庭"
  },
  "Friday-1459": {
    "name": "核对配置",
    "description": "只读取，不修改"
  },
  "Friday-1469": {
    "name": "移除动作",
    "description": "从习惯中移除开空调动作"
  },
  "Friday-1470": {
    "name": "移除动作",
    "description": "从习惯中移除开灯动作"
  },
  "Friday-1480": {
    "name": "清醒模拟",
    "description": "仅模拟30秒"
  },
  "Friday-1484": {
    "name": "模拟完成",
    "description": "仅模拟执行完成"
  },
  "Friday-1486": {
    "name": "已阻止",
    "description": "保护条件或读数质量阻止执行"
  },
  "Friday-1495": {
    "name": "睡眠模拟",
    "description": "只模拟睡眠保护"
  },
  "Friday-1505": {
    "name": "家庭录像",
    "description": "NAS上的家庭录像"
  },
  "Friday-1506": {
    "name": "智能服务",
    "description": "由ARL提供共享能力"
  },
  "Friday-1507": {
    "name": "任务规划",
    "description": "由ARL提供规划能力"
  },
  "Friday-1519": {
    "name": "示例可验",
    "description": "仅示例家庭可验证"
  },
  "Friday-1520": {
    "name": "示例可用",
    "description": "仅示例家庭可执行"
  },
  "Friday-1522": {
    "name": "持续更新",
    "description": "持续接收状态变化"
  },
  "Friday-1523": {
    "name": "只读核对",
    "description": "只读取当时状态，不操作设备"
  },
  "Friday-1535": {
    "name": "导出诊断",
    "description": "只导出已去除私密内容的诊断"
  },
  "Friday-1550": {
    "name": "读数不一",
    "description": "不同来源的读数不一致"
  },
  "Friday-1554": {
    "name": "仍待核对",
    "description": "刷新旧记录不能证明已经恢复"
  },
  "Friday-1556": {
    "name": "电量过期",
    "description": "旧电量不能证明当前设备正常"
  },
  "Friday-1557": {
    "name": "模式不符",
    "description": "空调工作模式与原校准不符"
  },
  "Friday-1574": {
    "name": "不可读取",
    "description": "当前目录无法读取"
  },
  "Friday-1581": {
    "name": "服务出错",
    "description": "本机服务返回错误"
  },
  "Friday-1587": {
    "name": "检查中",
    "description": "正在检查网关"
  },
  "Friday-1607": {
    "name": "只读状态",
    "description": "只发现设备并读取状态"
  },
  "Friday-1608": {
    "name": "设备验证",
    "description": "每台设备分别验证"
  },
  "Friday-1620": {
    "name": "硬盘直连",
    "description": "直接读取网络硬盘"
  },
  "Friday-1640": {
    "name": "共享目录",
    "description": "使用SMB连接"
  },
  "Friday-1641": {
    "name": "受限账号",
    "description": "使用SFTP连接"
  },
  "Friday-1645": {
    "name": "连接端口",
    "description": "可以留空"
  },
  "Friday-1651": {
    "name": "起始目录",
    "description": "共享内相对目录，可留空"
  },
  "Friday-1654": {
    "name": "主机校验",
    "description": "填写独立核对过的主机校验值"
  },
  "Friday-1656": {
    "name": "受限目录",
    "description": "账号须被NAS限制在此目录"
  },
  "Friday-1660": {
    "name": "保存设置",
    "description": "保存后仍停用读取"
  },
  "Friday-1662": {
    "name": "停止读取",
    "description": "断开连接并停用读取"
  },
  "Friday-1715": {
    "name": "打开目录",
    "description": "在Finder中连接目录"
  },
  "Friday-1721": {
    "name": "解码通过",
    "description": "仅确认影音解码通过"
  },
  "Friday-1770": {
    "name": "验证影音",
    "description": "读取6秒，不保存原片"
  },
  "Friday-1816": {
    "name": "选择相机",
    "description": "选择已有录像的摄像头"
  },
  "Friday-1823": {
    "name": "时钟偏差",
    "description": "录像时钟超前的秒数"
  },
  "Friday-1825": {
    "name": "误差上限",
    "description": "单位：秒"
  },
  "Friday-1829": {
    "name": "保留天数",
    "description": "NAS原片保留的天数"
  },
  "Friday-1833": {
    "name": "保存对应",
    "description": "保存目录与摄像头的对应关系"
  },
  "Friday-1843": {
    "name": "扫描完成",
    "description": "扫描完成不证明文件完整或已上传"
  },
  "Friday-1881": {
    "name": "家庭资料",
    "description": "NAS上的家庭照片与视频"
  },
  "Friday-1883": {
    "name": "共享目录",
    "description": "使用SMB连接"
  },
  "Friday-1911": {
    "name": "保存配置",
    "description": "只保存尚未启用的候选"
  },
  "Friday-1951": {
    "name": "连接同步",
    "description": "在后台持续同步"
  },
  "Friday-1955": {
    "name": "智能服务",
    "description": "由ARL提供共享能力"
  },
  "Friday-1957": {
    "name": "服务目录",
    "description": "后台服务所在目录，高级设置"
  },
  "Friday-1960": {
    "name": "启动中",
    "description": "ARL后台服务正在启动"
  },
  "Friday-1965": {
    "name": "权限不符",
    "description": "后台服务目录权限不符合要求"
  },
  "Friday-1980": {
    "name": "检查连接",
    "description": "只检查ARL连接"
  },
  "Friday-2002": {
    "name": "安装高度",
    "description": "单位：米"
  },
  "Friday-2017": {
    "name": "局部温度",
    "description": "含空调回风位置"
  },
  "Friday-2018": {
    "name": "读数时效",
    "description": "单位：秒。测量有效期（秒）"
  },
  "Friday-2026": {
    "name": "临时排除",
    "description": "仅排除1小时"
  },
  "Friday-2031": {
    "name": "室温来源",
    "description": "设置房间的主、备用读数来源"
  },
  "Friday-2052": {
    "name": "保存来源",
    "description": "保存主要与备用来源"
  },
  "Friday-2071": {
    "name": "预览校准",
    "description": "相对主要来源校准备用读数"
  },
  "Friday-2076": {
    "name": "采用校准",
    "description": "仅采用本次相对校准"
  },
  "Friday-2082": {
    "name": "模拟读数",
    "description": "加载模拟的成对读数"
  },
  "Friday-2083": {
    "name": "模拟失效",
    "description": "只模拟主要来源失效"
  },
  "Friday-2129": {
    "name": "最近记录",
    "description": "最近1小时的记录"
  },
  "Friday-2161": {
    "name": "处理冲突",
    "description": "核对并解决目录规则冲突"
  },
  "Friday-2180": {
    "name": "只记目录",
    "description": "只保存目录记录，不提取内容"
  },
  "Friday-2181": {
    "name": "优先文字",
    "description": "优先提取PDF中的文字"
  },
  "Friday-2190": {
    "name": "读取上限",
    "description": "单位：MB"
  },
  "Friday-2191": {
    "name": "页段上限",
    "description": "每次允许的页数或片段数"
  },
  "Friday-2192": {
    "name": "处理时限",
    "description": "单位：秒"
  },
  "Friday-2198": {
    "name": "单独规则",
    "description": "此文件采用单独的规则"
  },
  "Friday-2200": {
    "name": "保持原样",
    "description": "保留原文件名称与位置"
  },
  "Friday-2217": {
    "name": "继续计划",
    "description": "继续之前暂停的提取计划"
  },
  "Friday-2222": {
    "name": "目标位置",
    "description": "相对于起始目录填写"
  },
  "Friday-2223": {
    "name": "目标位置",
    "description": "相对于起始目录填写"
  },
  "Friday-2232": {
    "name": "确认预览",
    "description": "只确认当前这批预览"
  },
  "Friday-2233": {
    "name": "取消此批",
    "description": "放弃当前这批整理计划"
  },
  "Friday-2245": {
    "name": "查找内容",
    "description": "输入标题、正文或片段"
  },
  "Friday-2286": {
    "name": "示例输入",
    "description": "只用于模拟测试"
  },
  "Friday-2293": {
    "name": "原规则",
    "description": "查看停止时保存的规则记录"
  },
  "Friday-2296": {
    "name": "触发限制",
    "description": "查看触发条件与执行限制"
  },
  "Friday-2302": {
    "name": "批准变更",
    "description": "只批准这次预览的变更"
  },
  "Friday-2306": {
    "name": "核对部署",
    "description": "只读取原部署记录，不重发"
  },
  "Friday-2320": {
    "name": "原动作",
    "description": "查看停止时保存的动作记录"
  },
  "Friday-2325": {
    "name": "批准动作",
    "description": "只批准这次预览的动作"
  },
  "Friday-2326": {
    "name": "核对动作",
    "description": "只读取原动作记录，不重发"
  },
  "Friday-2344": {
    "name": "原记录",
    "description": "查看停止时保存的原服务记录"
  },
  "Friday-2345": {
    "name": "服务结果",
    "description": "原服务返回的结果"
  },
  "Friday-2351": {
    "name": "批准预览",
    "description": "只批准当前显示的预览"
  },
  "Friday-2352": {
    "name": "核对部署",
    "description": "只读取原部署记录，不重发"
  },
  "Friday-2361": {
    "name": "可查内容",
    "description": "当前连接下已提取、原件记录有效的内容"
  },
  "Friday-2362": {
    "name": "范围有限",
    "description": "不含未提取、过期或无法访问的内容"
  },
  "Friday-2363": {
    "name": "来源离线",
    "description": "无法核对出处，已存内容不代表原件可用"
  },
  "Friday-2364": {
    "name": "暂无匹配",
    "description": "当前可查内容无匹配，不代表来源没有文件"
  },
  "Friday-2365": {
    "name": "查找内容",
    "description": "输入文字，仅查本机已提取的内容"
  }
};
  const brand = Symbol('Friday display copy');
  const anchors = new Set();
  let serial = 0, scheduled = false;
  const name = id => {
    const row = descriptions[id];
    if (!row) throw Error('Unknown display copy');
    return {[brand]: [id], toString: () => row.name, toJSON: () => row.name};
  };
  const withCopy = (text, ...values) => ({
    [brand]: [...new Set(values.flatMap(value => value?.[brand] ?? []))],
    toString: () => String(text), toJSON: () => String(text),
  });
  const entries = value => value?.[brand] ?? [];
  const schedule = () => {
    if (!scheduled) { scheduled = true; queueMicrotask(() => { scheduled = false; flush(); }); }
  };
  const decorate = (node, text, attrs = {}) => {
    const ids = [...new Set([...entries(text), ...Object.values(attrs).flatMap(entries)])];
    if (ids.length || node.__fridayCopy) {
      node.__fridayCopy = ids;
      if (ids.length) node.setAttribute('data-friday-copy',ids.join(' '));
      else node.removeAttribute('data-friday-copy');
      schedule();
    }
    return node;
  };
  const describeValue = (node, value) => {
    const ids = [];
    const visit = item => {
      if (entries(item).length) ids.push(...entries(item));
      else if (item && typeof item === 'object') Object.values(item).forEach(visit);
    };
    visit(value);
    if (ids.length) { node.__fridayCopy = [...new Set(ids)]; node.setAttribute('data-friday-copy',node.__fridayCopy.join(' ')); schedule(); }
    return node;
  };
  function flush() {
    const groups = new Map();
    for (const node of document.querySelectorAll('[data-friday-copy]')) {
      const anchor = node.tagName === 'OPTION' ? node.parentElement :
        (node.closest('button, a, summary, label') || node);
      if (!anchor?.parentNode) continue;
      if (!groups.has(anchor)) groups.set(anchor, {ids: new Set(), nodes: []});
      const group = groups.get(anchor);
      node.__fridayCopy.forEach(id=>group.ids.add(id));
      group.nodes.push(node);
      anchors.add(anchor);
    }
    const describe = (node, ids) => {
      const existing = (node.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
      const other = existing.filter(id => !id.startsWith('friday-copy-description-'));
      const value = [...new Set([...other, ...ids])].join(' ');
      if (value) node.setAttribute('aria-describedby', value);
      else if (existing.length) node.removeAttribute('aria-describedby');
    };
    for (const anchor of anchors) {
      if (!anchor.isConnected) { anchors.delete(anchor); continue; }
      const group = groups.get(anchor) || {ids: new Set(), nodes: []};
      const notes = anchor.__fridayCopyNotes ??= new Map();
      for (const [id,note] of notes) if (!group.ids.has(id)) { note.remove(); notes.delete(id); }
      for (const id of group.ids) {
        if (notes.has(id)) continue;
        const row = descriptions[id];
        const note = document.createElement('small');
        note.className = 'copy-description meta';
        note.style.display = 'block';
        note.id = 'friday-copy-description-' + (++serial);
        note.setAttribute('data-copy-description', id);
        note.textContent = anchor.tagName === 'SELECT' ? row.name + '：' + row.description : row.description;
        // Preserve the actual control, listener, disabled state and short name.
        anchor.parentNode.insertBefore(note, anchor.nextSibling);
        notes.set(id, note);
      }
      describe(anchor,[...notes.values()].map(note=>note.id));
      for (const node of group.nodes) if (node !== anchor && node.tagName !== 'OPTION') {
        describe(node,node.__fridayCopy.map(id=>notes.get(id)?.id).filter(Boolean));
      }
    }
  }
  new MutationObserver(schedule).observe(document.documentElement, {subtree: true, childList: true});
  global.FridayCopyLayout = {name, withCopy, decorate, describeValue, flush};
})(globalThis);
