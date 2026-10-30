// Observation metadata never grants authority to execute an external rule.
const node=(tag,text,attrs={})=>{const e=document.createElement(tag);if(text!=null)e.textContent=text;for(const[k,v]of Object.entries(attrs))e.setAttribute(k,v);return e;};
const append=(parent,...children)=>{children.forEach(c=>parent.append(c));return parent;};
const sources={friday:'Friday',geek:'极客版',mijia:'米家'};
const evidence={native_backup:'已读取配置',ui_details:'已核对内容',ui_name_index:'仅保存名称'};
export function mountAutomationCatalog(parent,{api,isAlive=()=>true,onUseEntry=null,externalOnly=false}){
 if(!document.querySelector('link[data-automation-catalog]'))document.head.append(node('link',null,{rel:'stylesheet',href:'/static/automations-catalog.css','data-automation-catalog':''}));
 const root=node('section',null,{class:'automation-catalog','aria-label':'已有家庭行为'});parent.append(root);
 let report=null,source='',query='',loading=false;
 const button=(label,run)=>{const b=node('button',label,{type:'button'});b.addEventListener('click',run);return b;};
 const title=node('h2','已有行为'),status=node('p','正在读取盘点',{role:'status',class:'muted'}),tools=node('div',null,{class:'catalog-tools'}),rows=node('div',null,{class:'catalog-rows'}),links=node('div',null,{class:'catalog-modes'});
 const filter=node('select',null,{'aria-label':'来源'});append(filter,node('option','全部来源',{value:''}),...Object.entries(sources).map(([v,t])=>node('option',t,{value:v})));
 const search=node('input',null,{type:'search',placeholder:'查找行为','aria-label':'查找行为'});
 const file=node('input',null,{type:'file',accept:'.json,application/json',hidden:''});
 const refresh=button('刷新',()=>load());const importing=button('导入',()=>file.click());
 const exporting=button('导出',()=>{if(!report)return;const bundle={schema_version:1,revision:report.revision,entries:report.entries,modes:report.modes};const url=URL.createObjectURL(new Blob([JSON.stringify(bundle,null,2)],{type:'application/json'}));const a=node('a',null,{href:url,download:'Friday-自动化盘点.json'});root.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);});
 append(tools,filter,search,refresh,importing,exporting,file);append(root,title,node('p','统一查看各平台的家庭行为',{class:'muted'}),tools,status,links,rows);
 filter.addEventListener('change',()=>{source=filter.value;render();});search.addEventListener('input',()=>{query=search.value.trim().toLocaleLowerCase();render();});
 function render(){if(!report)return;rows.replaceChildren();links.replaceChildren();
  const owned=externalOnly?[]:report.owned.map(e=>({...e,source:'friday',kind:'automation',evidence:'owned',summary:'在 Friday 中运行'}));
  const all=[...owned,...report.entries],visible=all.filter(e=>(!source||e.source===source)&&(!query||(e.name+' '+e.summary).toLocaleLowerCase().includes(query)));
  status.textContent=`${all.length} 项行为 · ${report.geek_connection.status==='ready'?'极客版后台已连接':'极客版后台未连接'}`;
  if(!visible.length)rows.append(node('p',all.length?'没有匹配行为':'尚无盘点，可导入已有配置索引',{class:'product-empty'}));
  for(const e of visible){const card=node('details',null,{class:'catalog-row'}),head=node('summary');append(head,node('strong',e.name),node('span',sources[e.source],{class:'catalog-source'}),node('span',evidence[e.evidence]||'当前规则',{class:'catalog-evidence'}));card.append(head);
   const body=node('div',null,{class:'catalog-detail'});if(e.summary)body.append(node('p',e.summary));
   if(e.observed_at)body.append(node('p','盘点时间：'+new Date(e.observed_at).toLocaleString(),{class:'caption'}));
   body.append(node('p',e.evidence==='owned'?(e.enabled?'当前已启用':'当前已停用'):e.enabled==null?'启用状态未读取':e.enabled?'盘点时已启用':'盘点时已停用',{class:'caption'}));
   for(const[field,label]of [['triggers','触发'],['conditions','条件'],['actions','动作'],['issues','待核对']])if(e[field]?.length){body.append(node('h3',label));const list=node('ul');e[field].forEach(v=>list.append(node('li',v)));body.append(list);}
   if(e.evidence==='ui_name_index')body.append(node('p','目前只保存了名称。填写触发条件和动作后，才能交给 Friday 运行。'));
   else if(e.source!=='friday')body.append(node('p','原规则仍由'+sources[e.source]+'运行。重新设置为 Friday 规则前，请先在原应用暂停这条规则，避免同时执行。',{class:'caption'}));
   if(onUseEntry&&e.source!=='friday')body.append(button('在 Friday 重新设置',()=>onUseEntry(e)));
   card.append(body);rows.append(card);
  }
  if(report.modes.length){links.append(node('h3','模式关联'));for(const m of report.modes){const detail=node('details'),summary=node('summary',m.name);append(detail,summary,node('p',m.chain.join(' → ')),node('p',m.gap||'当前模式值尚未接入'));links.append(detail);}}
 }
 async function load(){if(loading)return;loading=true;refresh.disabled=true;status.textContent=report?'正在检查连接，保留上次盘点':'正在读取盘点';try{const next=await api('/api/home/automations/catalog');if(!isAlive()||!root.isConnected)return;report=next;render();}catch(_){status.textContent=report?'读取中断，保留上次盘点':'盘点暂不可读，请重试';}finally{loading=false;refresh.disabled=false;}}
 file.addEventListener('change',async()=>{const selected=file.files?.[0];if(!selected)return;importing.disabled=true;try{if(selected.size>262144)throw Error('budget');const bundle=JSON.parse(await selected.text());if(!report)throw Error('unavailable');await api('/api/home/automations/catalog',{expected_revision:report.revision,bundle});await load();}catch(_){status.textContent='导入未完成，请检查文件或刷新后重试';}finally{file.value='';importing.disabled=false;}});
 void load();return root;
}
