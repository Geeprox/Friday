const ENDPOINT = '/api/settings/intelligence';
const reasons = {
  api_quota_exhausted:'API 额度不足。', api_rate_limited:'API 暂时限流。',
  api_key_missing:'请保存已有 API Key。', api_key_rejected:'API Key 未被接受，请核对配置。',
  api_model_unavailable:'所选模型不可用，请核对项目权限或更换模型。', api_timeout:'API 响应超时。',
  api_unavailable:'API 暂不可用。', cloud_capability_disabled:'云端文字处理未开启。',
  daily_requests_reached:'已达到每日请求上限。', daily_text_tokens_reached:'已达到每日文本 Token 上限。',
  daily_images_reached:'已达到每日背景图片上限。', daily_voice_seconds_reached:'已达到每日语音上限。'
};
function node(tag,text,attrs={}) {
  const result=document.createElement(tag);
  if(text!==null)result.textContent=text;
  for(const [key,value] of Object.entries(attrs))result.setAttribute(key,String(value));
  return result;
}
export async function mountIntelligenceSettings(container,{api,onOpenConversation}) {
  const panel=node('section',null,{class:'ai-config','aria-label':'智能通道与用量'});
  const output=node('p','正在读取智能配置…',{role:'status','aria-live':'polite'});
  panel.append(node('h3','智能通道与每日用量'),output);container.append(panel);
  if(!document.getElementById('intelligence-settings-style'))document.head.append(node('link',null,{id:'intelligence-settings-style',rel:'stylesheet',href:'/static/intelligence-settings.css'}));
  const fields=new Map();
  let busy=false;
  function field(parent,key,label,value,{type='text',help='',readonly=false}={}) {
    const id='intelligence-'+key;
    const wrap=node('div',null,{class:'ai-field'}),input=node('input',null,{id,type});
    if(type==='checkbox')input.checked=value===true;else input.value=String(value??'');
    if(type==='number'){input.min='0';input.step='1';}else input.maxLength=128;
    if(readonly)input.disabled=true;
    wrap.append(node('label',label,{for:id}),input);
    if(help){const hint=node('small',help,{id:id+'-help'});wrap.append(hint);input.setAttribute('aria-describedby',hint.id);}
    parent.append(wrap);fields.set(key,input);return input;
  }
  function action(parent,label,handler) {
    const button=node('button',label,{type:'button'});
    button.addEventListener('click',async()=>{
      if(busy)return;busy=true;
      for(const control of panel.querySelectorAll('button'))control.disabled=true;
      try{await handler();}catch(_){if(panel.isConnected)output.textContent='暂时无法保存或读取配置，请重试。';}
      finally{busy=false;for(const control of panel.querySelectorAll('button'))control.disabled=false;}
    });parent.append(button);return button;
  }
  function report(value) {
    if(!panel.isConnected)return;
    const usage=value.usage,used=usage.used,limit=usage.limits;
    const key=value.api_key_configured?'本机已配置 Key，余额尚未核验。':'尚未配置 API Key。';
    output.textContent=key+' 今天已用 '+used.requests+' / '+limit.requests+' 次请求，'
      +used.text_tokens+' / '+limit.text_tokens+' 文本 Token，'+used.images+' / '+limit.images+' 张图片，'
      +used.voice_seconds+' / '+limit.voice_seconds+' 秒语音会话。';
    const detail=panel.querySelector('[data-ai-usage]');
    if(detail)detail.textContent=(reasons[value.last_route?.reason]??'')
      +(value.last_route?.reason?(value.last_route.provider==='arl'?' 已改用 ARL。':' 当前保留本地功能。'):'')
      +(usage.uncertain_requests?' 有 '+usage.uncertain_requests+' 次调用的预留用量待核对。':'')
      +' 下一次重置：'+new Date(usage.resets_at).toLocaleString()+'。';
  }
  let initial;
  try{initial=await api(ENDPOINT);}catch(_){output.textContent='暂时无法读取智能配置。';action(panel,'重试读取',async()=>report(await api(ENDPOINT)));return;}
  if(!panel.isConnected)return;
  const config=initial.config,form=node('div',null,{class:'ai-fields'});
  const provider=node('select',null,{id:'intelligence-provider'});
  for(const [value,label] of [['arl','ARL · Codex'],['openai','OpenAI API'],['auto','自动 · API 优先'],['local','仅本地功能']])provider.append(node('option',label,{value}));
  provider.value=config.provider;fields.set('provider',provider);
  const providerField=node('div',null,{class:'ai-field'});providerField.append(node('label','文字处理通道',{for:provider.id}),provider);form.append(providerField);
  field(form,'cloud_text','允许云端文字处理',config.cloud_text,{type:'checkbox',help:'发送本次问题及必要设备目录；旧版即时规划还包含有限状态。设备动作仍需本地校验。'});
  field(form,'allow_arl_fallback','API 不可用时改用 ARL',config.allow_arl_fallback,{type:'checkbox'});
  field(form,'cloud_images','自动为歌曲生成背景',config.cloud_images,{type:'checkbox',help:'仅发送歌名和演唱者。先显示本地背景，准备好后替换，同一背景可反复使用。'});
  field(form,'cloud_voice','开启云端语音输入',config.cloud_voice,{type:'checkbox',help:'主动点击后录音，最长 8 秒。转写先放入输入框，由你发送。'});
  const limits=node('fieldset',null);limits.append(node('legend','每日上限'));
  for(const [key,label] of [['daily_requests','API 请求次数'],['daily_text_tokens','文本 Token'],['daily_images','歌曲背景张数'],['daily_voice_seconds','语音会话秒数']])field(limits,key,label,config[key],{type:'number'});
  field(limits,'timezone','每日重置时区',config.timezone);
  limits.append(node('p','上限为 0 时暂停对应调用。用量先预留，未知结果保留预留；这里统计本应用用量，不代表账户余额。',{class:'meta'}));
  const advanced=node('details',null);advanced.append(node('summary','模型配置'));
  field(advanced,'background_with_mv','已有 MV 时也生成背景',config.background_with_mv,{type:'checkbox'});
  field(advanced,'cached_background_limit','保留的歌曲背景张数上限',config.cached_background_limit,{type:'number'});
  for(const [key,label] of [['responses_model','文字模型'],['decisions_model','快速判断模型'],['image_model','图片模型'],['realtime_model','语音模型']])field(advanced,key,label,config[key],{readonly:key==='decisions_model'});
  field(advanced,'max_output_tokens','单次文本输出上限',config.max_output_tokens,{type:'number'});
  form.append(limits,advanced);panel.append(form);
  const controls=node('div',null,{class:'ai-actions'});panel.append(controls);
  action(controls,'保存智能配置',async()=>{
    const patch={};for(const [key,input] of fields)patch[key]=input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.value.trim();
    report(await api(ENDPOINT,{method:'PUT',body:JSON.stringify(patch)}));
  });
  action(controls,'刷新用量',async()=>report(await api(ENDPOINT)));
  if(onOpenConversation)action(controls,'打开家庭对话',async()=>onOpenConversation());
  action(controls,'重新允许 API 尝试',async()=>report(await api(ENDPOINT+'/retry',{method:'POST',body:'{}'})));
  const credential=node('details',null);credential.append(node('summary','配置已有 API Key'));
  const secret=node('input',null,{id:'intelligence-api-key',type:'password',autocomplete:'off',maxlength:'512'});
  credential.append(node('label','API Key',{for:secret.id}),secret,node('p','仅保存到本机受限文件，不在页面回显。',{class:'meta'}));
  action(credential,'保存已有 Key',async()=>{const value=secret.value;secret.value='';report(await api(ENDPOINT+'/credential',{method:'POST',body:JSON.stringify({api_key:value})}));});
  action(credential,'移除本机保存的 Key',async()=>report(await api(ENDPOINT+'/credential',{method:'DELETE'})));
  panel.append(credential,node('p','',{class:'meta','data-ai-usage':''}));report(initial);
}
