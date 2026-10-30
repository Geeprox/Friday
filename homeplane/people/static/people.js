import '../../static/copy-layout.js';
import {mountLivePeople} from './live.js';
const copyName = globalThis.FridayCopyLayout.name, withCopy = globalThis.FridayCopyLayout.withCopy;
const copyLayout = globalThis.FridayCopyLayout;
// People content always uses text nodes. This module runs no recognition.
const node=(tag,text,attrs={})=>{const el=document.createElement(tag);if(text!==null&&text!==undefined)el.textContent=String(text);for(const [key,value]of Object.entries(attrs))el.setAttribute(key,String(value));return copyLayout.decorate(el,text,attrs);};
const add=(parent,...children)=>{parent.append(...children);return parent;};
const button=(text,action,attrs={})=>{const b=node('button',text,{type:'button',...attrs});b.addEventListener('click',action);return b;};
const stateText={candidate:'待命名',named:'已命名',unknown:'样本不足',ambiguous:'需要核对'};
const voiceText={candidate:'等待确认',confirmed:'已关联',rejected:'已拒绝',unlinked:'已解除',stale:'需补充声音匹配依据'};
const errors={people_revision_conflict:'人物记录已更新，请重新核对。',people_name_conflict:'这个名字已存在，可以合并这两组。',people_merge_target_required:'请选择合并后保留的人物。',people_split_subset_required:'请选择部分样本，至少保留一个。',people_sample_review_required:'先核对或拆分这些样本，再为人物命名。',people_voice_review_not_ready:'声音样本已变化，请重新核对',people_undo_unavailable:'当前没有可撤销的修改。',people_voice_conflict:'这个声音已有其他人物关联，请先解除。'};

export function mountPeople(root,{endpoint='/api/people',embedded=false,readOnly=false}={}){
  let page=null,filter='all',selected=null,busy=false,notice='',failed=false,destroyed=false;
  let liveView=null;
  const mergeSelection=new Set(),sampleSelection=new Set();
  root.classList.add('people-root');
  function title(cluster){return page.people.find(p=>p.person_id===cluster.person_id)?.name||`人物组 ${String(page.clusters.indexOf(cluster)+1).padStart(2,'0')}`;}
  function art(sid,index=0){
    const palettes=[['#e6e4d9','#b8bfad'],['#e5dace','#c1a68d'],['#dee3e5','#a8b8bf'],['#e8e1dc','#c5b4a7']];
    const cluster=page.clusters.find(c=>c.sample_ids.includes(sid)), group=cluster?page.clusters.indexOf(cluster):3;
    const [bg,shape]=palettes[group%palettes.length];
    return node('div',null,{class:'sample-art','aria-hidden':'true',style:`--people-sample-bg:${bg};--people-sample-shape:${shape};--people-sample-rotate:${-40+index*9}deg`});
  }
  async function refresh(){
    busy=true;render();
    try{const response=await fetch(endpoint,{cache:'no-store'});if(!response.ok)throw new Error();page=await response.json();if(failed)notice='';failed=false;}
    catch{notice='人物记录暂时无法读取，请刷新重试。';failed=true;}finally{busy=false;render();}
  }
  async function edit(path,body,message){
    if(readOnly){notice='当前只读；人物核对修改与识别接入待启用。';render();return;}
    if(busy||!page)return;
    busy=true;notice='';render();
    try{
      const response=await fetch(`${endpoint}/${path}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:page.revision,...body})});
      const result=await response.json();
      if(!response.ok){notice=errors[result.detail?.code]||'这项修改未保存，请重新核对。';failed=true;
        if(result.detail?.code==='people_revision_conflict'){const latest=await fetch(endpoint,{cache:'no-store'});if(latest.ok)page=await latest.json();}return;}
      page=result;notice=message;failed=false;sampleSelection.clear();mergeSelection.clear();
    }catch{notice='连接中断，请刷新核对记录。';failed=true;}finally{busy=false;render();}
  }
  function checkbox(label,checked,onchange){const input=node('input',null,{type:'checkbox','aria-label':label});input.checked=checked;input.disabled=busy||readOnly;input.addEventListener('change',()=>onchange(input.checked));return input;}
  const syntheticSamples=()=>page?.last_run?.backend==='synthetic-contract-fake'||Boolean(page?.samples?.length&&(page.simulated===true||page.last_run?.simulated===true||page.samples.some(sample=>sample.simulated===true)));
  const sampleUnit=()=>syntheticSamples()?'个合成样本':'个登记记录';
  function render(){
    if(destroyed)return;
    liveView?.destroy();liveView=null;
    root.replaceChildren();
    const synthetic=syntheticSamples();
    const top=node('div',null,{class:'people-top'}),status=add(node('div',null,{class:'quiet-status'}),node('span',null,{class:'small-dot'}),node('span',synthetic?'合成观察目录':'人物目录'));
    const undo=button('撤销修改',()=>edit('undo',{},'已撤销上一次修改。'));undo.disabled=busy||readOnly||!page?.undo_available;
    const reload=button('刷新',refresh);reload.disabled=busy;
    add(top,status,add(node('div',null,{class:'top-actions'}),undo,reload));
    const heading=add(node('header',null,{class:'people-heading'}),add(node('div'),node('h1','人物'),node('p','为熟悉的家人登记名字，核对识别结果。')),
      node('span',page?`${page.samples.filter(s=>s.active).length} ${sampleUnit()} · ${page.clusters.length} 组人物`:'',{class:'people-count'}));
    add(root,top,...(embedded?[]:[heading]),node('div',notice,{class:'people-notice'+(failed?' error':''),role:'status','aria-live':'polite'}));
    if(readOnly)root.append(node('p','当前只读；人物识别接入与核对修改待启用。',{class:'people-note'}));
    if(!page){root.append(node('p',busy?'正在读取人物…':'还没有人物记录。',{class:'people-empty'}));return;}
    const liveRoot=node('section');root.append(liveRoot);
    liveView=mountLivePeople(liveRoot,{readOnly,onSaved:()=>{}});
    if(!page.clusters.length&&!page.samples.length)return;
    const filters=node('div',null,{class:'filters'});
    for(const [key,label,count] of [['all','全部',page.clusters.length],['candidate','待核对',page.clusters.filter(c=>c.state!=='named').length],['named','已命名',page.clusters.filter(c=>c.state==='named').length],['excluded','已排除',page.samples.filter(s=>s.state==='excluded').length]]){
      const b=button(null,()=>{filter=key;render();},{'aria-pressed':filter===key});add(b,node('span',label),node('span',count,{class:'count'}));filters.append(b);
    }
    const merge=button(mergeSelection.size>1?`合并 ${mergeSelection.size} 组`:'合并人物',mergeDialog,{class:'merge-action'});merge.disabled=busy||readOnly||mergeSelection.size<2;
    root.append(add(node('div',null,{class:'people-toolbar'}),filters,merge));
    if(filter==='excluded'){for(const sample of page.samples.filter(s=>s.state==='excluded')){
      const a=art(sample.sample_id);const restore=button('恢复样本',()=>edit('correct',{sample_id:sample.sample_id,operation:'restore'},'样本已恢复。'));restore.disabled=busy||readOnly;
      root.append(add(node('div',null,{class:'excluded-row'}),a,node('span',synthetic?'已排除的合成样本':'已排除的登记记录'),restore));}return;}
    const visible=page.clusters.filter(c=>filter==='all'||(filter==='named'?c.state==='named':c.state!=='named'));
    if(!visible.some(c=>c.cluster_id===selected)){selected=visible[0]?.cluster_id;sampleSelection.clear();}
    const layout=node('div',null,{class:'people-workspace'}),list=node('div',null,{class:'people-list','aria-label':'人物组'});
    visible.forEach((cluster,index)=>{
      const open=button(null,()=>{selected=cluster.cluster_id;sampleSelection.clear();render();},{class:'group-open','aria-label':`查看${title(cluster)}`,'aria-pressed':selected===cluster.cluster_id});
      add(open,art(cluster.representatives[0]),add(node('div'),node('span',title(cluster),{class:'group-name'}),add(node('div',null,{class:'group-meta'}),node('span',`${cluster.sample_ids.length} ${sampleUnit()}`),node('span',stateText[cluster.state]))),node('span','›',{class:'row-arrow','aria-hidden':'true'}));
      const check=checkbox(`选择${title(cluster)}参与合并`,mergeSelection.has(cluster.cluster_id),checked=>{checked?mergeSelection.add(cluster.cluster_id):mergeSelection.delete(cluster.cluster_id);render();});
      list.append(add(node('div',null,{class:'group-row'+(selected===cluster.cluster_id?' selected':''),style:`animation-delay:${index*25}ms`}),check,open));
    });
    if(!visible.length)list.append(node('p','这里还没有人物。',{class:'people-empty'}));
    add(layout,list,inspector(page.clusters.find(c=>c.cluster_id===selected)));root.append(layout);
    root.append(node('p','样本图为示意；登记名字只用于身份核对记录。',{class:'people-note'}));
  }
  function inspector(cluster){
    const panel=node('section',null,{class:'people-inspector','aria-label':'人物详情'});
    if(!cluster)return add(panel,node('p','选择一组人物，查看样本。',{class:'people-empty'}));
    add(panel,add(node('div',null,{class:'inspector-eyebrow'}),node('span','人物详情'),node('span',stateText[cluster.state],{class:'status-chip'})),node('h2',title(cluster),{class:'inspector-title'}),node('p',`${cluster.sample_ids.length} ${sampleUnit()} · ${readOnly?'人物元数据只读。':'可以命名、拆分或纠正归属。'}`,{class:'inspector-subtitle'}));
    if(readOnly){const sheet=node('div',null,{class:'contact-sheet'});cluster.sample_ids.forEach((sid,index)=>{const visual=art(sid,index);visual.append(node('small',`示意 ${String(index+1).padStart(2,'0')}`));sheet.append(visual);});return add(panel,sheet,node('p','仅基本信息与示意，识别关联修改未启用。此处仅显示基本信息与几何占位；识别、声音关联和修改待启用。',{class:'people-note'}));}
    const form=node('form',null,{class:'name-form'}),name=node('input',null,{type:'text',placeholder:'人物姓名',maxlength:40,'aria-label':'人物名字',autocomplete:'off'});
    name.value=page.people.find(p=>p.person_id===cluster.person_id)?.name||'';
    const save=node('button',cluster.state==='named'?'保存名字':'登记名字',{type:'submit',class:'primary'});save.disabled=busy||['unknown','ambiguous'].includes(cluster.state);name.disabled=busy;
    form.addEventListener('submit',event=>{event.preventDefault();if(name.value.trim())void edit('name',{cluster_id:cluster.cluster_id,name:name.value.trim()},'人物名字已保存。');});
    add(panel,add(form,name,save),add(node('div',null,{class:'sample-heading'}),node('h3','核对样本'),node('span',copyName('Friday-0892'))));
    const sheet=node('div',null,{class:'contact-sheet'});
    cluster.sample_ids.forEach((sid,index)=>{const label=node('label',null,{class:'sample-tile'}),visual=art(sid,index);visual.append(node('small',`${syntheticSamples()?'合成示意':'示意'} ${String(index+1).padStart(2,'0')}`));
      const input=checkbox(`选择样本 ${index+1}`,sampleSelection.has(sid),checked=>{checked?sampleSelection.add(sid):sampleSelection.delete(sid);render();});sheet.append(add(label,visual,input));});
    const split=button('拆分人物',()=>edit('split',{cluster_id:cluster.cluster_id,sample_ids:[...sampleSelection]},'选中的样本已拆成新人物。'));
    split.disabled=busy||!sampleSelection.size||sampleSelection.size>=cluster.sample_ids.length;
    add(panel,sheet,add(node('div',null,{class:'sample-selection'}),node('span',sampleSelection.size?`已选择 ${sampleSelection.size} 个观察`:'样本不是同一人？选择后拆分。'),split));
    const correction=node('div',null,{class:'correction-actions'}),select=node('select',null,{'aria-label':'纠正归属'});select.append(node('option','归入已有的人物',{value:''}));
    page.people.filter(p=>p.name&&!p.merged_into&&p.person_id!==cluster.person_id).forEach(p=>select.append(node('option',p.name,{value:p.person_id})));
    const move=button('更正归属',()=>{if(select.value)void edit('correct',{sample_id:[...sampleSelection][0],person_id:select.value,operation:'move'},'样本归属已更正。');});
    const exclude=button('排除样本',()=>edit('correct',{sample_id:[...sampleSelection][0],operation:'exclude'},'样本已排除，可以在「已排除」中恢复。'));
    move.disabled=exclude.disabled=busy||sampleSelection.size!==1;select.disabled=busy;
    add(panel,add(correction,select,move,exclude));
    const voices=add(node('section',null,{class:'voice-section'}),node('h3','关联声音'));
    const links=page.voice_links.filter(v=>v.person_id===cluster.person_id);
    if(!links.length)voices.append(node('p',cluster.state==='named'?'还没有可核对的声音。':'命名后可核对声音关联。',{class:'inspector-subtitle'}));
    for(const link of links){
      const mark=add(node('div',null,{class:'voice-mark','aria-hidden':'true'}),node('i'),node('i'),node('i'),node('i'));
      const text=add(node('div'),node('div','候选声音 A',{class:'voice-name'}),node('div',`${link.independent_windows} 段独立记录 · 关联待复核`,{class:'voice-detail'}));
      voices.append(add(node('div',null,{class:'voice-row'}),add(node('div',null,{class:'voice-identity'}),mark,text),node('span',voiceText[link.state],{class:'status-chip'})));
      const actions=node('div',null,{class:'voice-actions'});
      if(link.state==='candidate'){for(const [decision,label]of [['confirm','确认关联'],['reject','拒绝']]){const b=button(label,()=>edit('voice-review',{link_id:link.link_id,decision},decision==='confirm'?'声音关联已确认。':'已拒绝这条声音关联。'),{class:decision==='confirm'?'primary':''});b.disabled=busy;actions.append(b);}}
      if(link.state==='stale'){const b=button('拒绝关联',()=>edit('voice-review',{link_id:link.link_id,decision:'reject'},'已拒绝这条声音关联。'));b.disabled=busy;actions.append(b);}
      if(link.state==='confirmed'){const b=button('解除关联',()=>edit('voice-review',{link_id:link.link_id,decision:'unlink'},'声音关联已解除。'));b.disabled=busy;actions.append(b);}
      voices.append(actions);
    }
    add(panel,add(voices,node('p','主动说话人检测待接入',{class:'dependency-note'})));return panel;
  }
  function mergeDialog(){
    if(readOnly)return;
    const trigger=document.activeElement;
    const clusters=page.clusters.filter(c=>mergeSelection.has(c.cluster_id));
    const dialog=node('dialog',null,{class:'people-dialog','aria-labelledby':'people-merge-title'}),personIds=[...new Set(clusters.map(c=>c.person_id).filter(Boolean))];
    add(dialog,node('h2',`合并这 ${clusters.length} 组人物`,{id:'people-merge-title'}),node('p',clusters.map(title).join('、')),node('p','样本合并到同一人，声音关联需重新核对'));
    const select=node('select',null,{'aria-label':'保留人物'});
    if(personIds.length){dialog.append(node('label','保留姓名'));personIds.forEach(pid=>select.append(node('option',page.people.find(p=>p.person_id===pid)?.name||'未命名人物',{value:pid})));dialog.append(select);}
    const dismiss=()=>{dialog.close();dialog.remove();trigger?.focus();};
    const cancel=button('取消',dismiss);
    const confirm=button('确认合并',()=>{dismiss();void edit('merge',{cluster_ids:clusters.map(c=>c.cluster_id),...(personIds.length?{target_person_id:select.value}:{})},'人物组已合并。');},{class:'primary'});
    add(dialog,add(node('div',null,{class:'dialog-actions'}),cancel,confirm));dialog.addEventListener('cancel',event=>{event.preventDefault();dismiss();});root.append(dialog);dialog.showModal();
  }
  const ready=refresh();
  return {refresh,ready,destroy(){destroyed=true;liveView?.destroy();root.replaceChildren();root.classList.remove('people-root');}};
}
