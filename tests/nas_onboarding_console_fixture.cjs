// Exercises actual Console helpers with synthetic endpoints; no DOM package/network.
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[2],'utf8'),scenario=process.argv[3];
const clone=value=>JSON.parse(JSON.stringify(value));
const config={source_ref:'SYNTHETIC-family',transport:'smb',host:'synthetic.invalid',port:null,
  credential_ref:'SYNTHETIC-ref',share:'SYNTHETIC-share',root:'Family',enabled:false,
  host_key_sha256:null,sftp_root_confined:false,require_encryption:true,operation_timeout_seconds:3};
// Match the actual closed model's persisted defaults, not the previous partial UI fixture.
const modelDefaults={schema_version:1,port:null,share:null,root:'',enabled:false,require_encryption:true,
  host_key_sha256:null,sftp_root_confined:false,connect_timeout_seconds:8,operation_timeout_seconds:5,
  health_interval_seconds:30,backoff_initial_seconds:1,backoff_max_seconds:60};
const status=(generation=0,state='not_configured',enabled=false)=>({config_generation:generation,
  configured:generation>0,enabled,state,reachable:null,authenticated:null,readable:null,
  connection_epoch:null,failure:null,retry_in_seconds:null,checked_at:null});
const view=(cfg=null,st=status())=>({config:cfg,status:st,credential_reference_saved:Boolean(cfg),credential_availability:'not_checked'});
const response=(body,code=200)=>({ok:code<400,status:code,json:async()=>clone(body)});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function harness(initial=view()){
  const nodes=new Map(),calls=[],timers=new Map();let saved=clone(initial),fault=null,hold=null,pending=null,nextTimer=0;
  class Element{
    constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.attrs={};this._text='';this.value='';this.alive=true;this.hidden=false;this.checked=false;this.disabled=false;}
    set textContent(text){this._text=String(text);this.children=[];}
    get textContent(){return this._text+this.children.map(child=>child.textContent).join(' ');}
    set innerHTML(_){throw Error('Household text cannot become HTML');}
    get isConnected(){return this.alive&&(this.parent?this.parent.isConnected:true);}
    setAttribute(key,value){this.attrs[key]=String(value);if(key==='id')nodes.set(String(value),this);if(key==='value')this.value=String(value);if(key==='hidden')this.hidden=true;if(key==='required')this.required=true;}
    append(...children){for(const child of children){if(child.parent)child.parent.children=child.parent.children.filter(item=>item!==child);child.parent=this;this.children.push(child);}if(this.tag==='select'&&!this.value)this.value=this.children[0]?.value??'';}
    replaceChildren(...children){this._text='';this.children=[];this.append(...children);}
    addEventListener(name,handler){this.handlers[name]=handler;}
  }
  const fetch=async(path,options={})=>{
    const method=options.method??'GET',body=options.body?JSON.parse(options.body):null;calls.push({path,method,body});
    assert(path.startsWith('/api/settings/nas/connection'),'no media, Finder, credential, device or model routes');
    if(method==='GET'){
      assert.equal(path,'/api/settings/nas/connection/config');
      if(fault==='read'){fault=null;throw Error('SYNTHETIC private error <script>BAD</script>');}
      if(hold==='read'){hold=null;return await new Promise(resolve=>{pending=()=>resolve(response(saved));});}
      return response(saved);
    }
    if(fault==='invalid'){fault=null;return response({code:'invalid_request',message:'<script>SYNTHETIC-secret</script>'},422);}
    if(body.expected_generation!==saved.status.config_generation)return response({code:'nas_source_changed'},409);
    const generation=body.expected_generation+1;
    if(method==='PUT')saved=view(body.config,status(generation,'disabled',false));
    else{
      const enabled=path.endsWith('/resume');saved=view({...saved.config,enabled},status(generation,enabled?'stopped':'disabled',enabled));
    }
    if(fault==='lost'){fault=null;throw Error('SYNTHETIC lost response');}
    if(hold==='write'){hold=null;return await new Promise(resolve=>{pending=()=>resolve(response(saved.status));});}
    return response(saved.status);
  };
  class AbortController {constructor(){this.signal={};}abort(){this.signal.aborted=true;}}
  const document={createElement:tag=>new Element(tag),getElementById:id=>nodes.get(id)};
  const body=new Element('body');
  const copyDOM=require('./copy_layout_dom.cjs').install(Element,document,body);
  const context=vm.createContext({...copyDOM,document,fetch,console,Date,JSON,Promise,AbortController,
    setTimeout:(fn,delay)=>{const id=++nextTimer;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id)});
  const helpers=source.slice(source.indexOf('const $ ='),source.indexOf('const qualityNames'));
  const field=source.slice(source.indexOf('function settingsField('),source.indexOf('function settingsSection('));
  const button=source.match(/^function button\(.*$/m)[0];
  const begin=source.indexOf('function renderNASConnection('),end=source.indexOf('\nasync function refreshHaStatus',begin);
  vm.runInContext(fs.readFileSync(require('node:path').join(require('node:path').dirname(process.argv[2]),'copy-layout.js'),'utf8'),context);
  vm.runInContext("const copyName=FridayCopyLayout.name,withCopy=FridayCopyLayout.withCopy,copyLayout=FridayCopyLayout;\n"+helpers+'\n'+field+'\n'+button+'\nlet nasConnectionDraft=null,nasConnectionDraftGeneration=null,nasConnectionRefresh=null;\n'+source.slice(begin,end),context);
  let root;
  const render=()=>{if(root)root.alive=false;root=new Element('section');root.ownerDocument=document;body.replaceChildren(root);context.renderNASConnection(root);return root;};
  render();await tick();
  const get=id=>nodes.get('nas-direct-'+id);
  const edit=(id,value)=>{const node=get(id);assert(node&&!node.disabled,id+' must be editable');if(id==='confined')node.checked=value;else node.value=String(value);node.handlers.input();};
  const submit=()=>get('form').handlers.submit({preventDefault(){}});
  const click=id=>get(id).handlers.click();
  const writes=()=>calls.filter(call=>call.method!=='GET');
  return {nodes,context,get,edit,submit,click,writes,calls,timers,render,text:()=>root.textContent,
    saved:()=>clone(saved),setSaved:value=>saved=clone(value),fault:value=>fault=value,
    hold:value=>hold=value,resolve:()=>{assert(pending);const run=pending;pending=null;run();},root:()=>root};
}
async function run(){
  if(scenario==='catalog_completeness'){
    class Element{constructor(tag,text){this.text=text??'';this.children=[];this.isConnected=true;}append(...children){this.children.push(...children);}replaceChildren(...children){this.children=children;}get textContent(){return this.text+' '+this.children.map(n=>n.textContent).join(' ');}}
    let catalog={source_ref:'SYNTHETIC-family',source_online:true,completion_convention:'unreviewed',checkpoint:null};const calls=[];
    const context=vm.createContext({Date,Promise,el:(tag,text)=>new Element(tag,text),api:async path=>{calls.push(path);return path.includes('family-media')?clone(catalog):path.includes('/query/')?{consumer_adoption:{configured:false}}:{state:'online'};}});
    const begin=source.indexOf('function renderLocalReadSources('),end=source.indexOf('function renderNASConnection(',begin);
    vm.runInContext(source.slice(begin,end),context);
    const root=new Element('section'),refresh=context.renderLocalReadSources(root);await tick();assert.match(root.textContent,/尚未扫描目录/);
    catalog.checkpoint={checked_at:new Date().toISOString(),enumeration_complete:false};await refresh();assert.match(root.textContent,/扫描尚未结束/);assert.match(root.textContent,/保持待确认/);
    catalog.checkpoint={checked_at:new Date(Date.now()-61000).toISOString(),enumeration_complete:true};await refresh();assert.match(root.textContent,/超过一分钟/);
    catalog.checkpoint={checked_at:new Date(Date.now()+60000).toISOString(),enumeration_complete:true};await refresh();assert.match(root.textContent,/时间需核对/);
    catalog.checkpoint={checked_at:new Date().toISOString(),enumeration_complete:true};await refresh();assert.match(root.textContent,/仍不能证明没有遗漏/);
    assert(calls.every(path=>['/api/settings/nas/connection','/api/family-media/context','/api/query/contracts'].includes(path)));
  }else if(scenario.startsWith('reconcile_changed:')||scenario.startsWith('reconcile_null:')||scenario.startsWith('reconcile_invalid:')||scenario==='reconcile_unknown_existing'){
    const key=scenario.split(':')[1],sftpMode=['host_key_sha256','sftp_root_confined'].includes(key);
    const initial={...modelDefaults,...config,...(sftpMode?{transport:'sftp',share:null,root:'/',host_key_sha256:'SHA256:'+'a'.repeat(43),sftp_root_confined:true}:{}),...(scenario==='reconcile_unknown_existing'?{unknown_option:true}:{})};
    const h=await harness(view(initial,status(1,'disabled')));h.edit('host','changed.synthetic.invalid');h.fault('lost');
    await h.submit();assert.equal(h.writes().length,1);assert.match(h.text(),/请求结果尚未确认/);
    const newer=h.saved();newer.status.config_generation++;
    if(scenario.startsWith('reconcile_changed:')){
      const changes={schema_version:2,source_ref:'SYNTHETIC-other',transport:'sftp',host:'other.synthetic.invalid',port:1445,
        share:'SYNTHETIC-other-share',root:'Other',credential_ref:'SYNTHETIC-other-ref',enabled:true,require_encryption:false,
        host_key_sha256:'SHA256:'+'b'.repeat(43),sftp_root_confined:false,connect_timeout_seconds:9,
        operation_timeout_seconds:4,health_interval_seconds:31,backoff_initial_seconds:2,backoff_max_seconds:61,unknown_option:true};
      assert(Object.prototype.hasOwnProperty.call(changes,key));newer.config[key]=changes[key];
    }else if(scenario.startsWith('reconcile_null:'))newer.config[key]=null;
    else if(scenario.startsWith('reconcile_invalid:')){
      if(key==='missing_required')delete newer.config.source_ref;
      else if(key==='boolean_string')newer.config.require_encryption='true';
      else if(key==='number_string')newer.config.connect_timeout_seconds='8';
      else if(key==='nullable_type')newer.config.port='445';
      else if(key==='unknown_proto')newer.config=JSON.parse(JSON.stringify(newer.config).slice(0,-1)+',"__proto__":{"test":true}}');
      else throw Error('Unknown invalid case');
    }
    newer.status.enabled=newer.config.enabled===true;newer.status.state=newer.status.enabled?'stopped':'disabled';h.setSaved(newer);
    await h.click('refresh');assert(!h.text().includes('已返回状态与上次请求一致的保存设置'));
    assert(h.get('save').disabled,key+' must preserve conflict');assert(h.get('connect').disabled);
    assert.equal(h.get('host').value,'changed.synthetic.invalid','lost-response draft retained');
    await h.submit();await h.click('connect');assert.equal(h.writes().length,1,'mismatch never repeats save or connect');
    await h.click('refresh');assert(h.get('save').disabled);assert(h.get('connect').disabled,'ordinary refresh cannot clear review gate');
    if(scenario==='reconcile_changed:enabled'){
      assert(!h.get('disconnect').disabled);await h.click('disconnect');
      assert.equal(h.writes()[1].path,'/api/settings/nas/connection/disable');assert.equal(h.writes()[1].body.expected_generation,3);
      assert(h.get('save').disabled,'disconnect must not discard unresolved draft/conflict');
    }else if(scenario.startsWith('reconcile_changed:')&&key!=='schema_version'&&key!=='unknown_option'){
      await h.click('reset');assert(!h.get('save').disabled);assert(!h.get('connect').disabled);
      assert.equal(h.writes().length,1,'explicit review itself is read-only');
      await h.click('connect');assert.equal(h.writes().length,2);assert.equal(h.writes()[1].body.expected_generation,3);
    }
  }else if(scenario.startsWith('reconcile_defaults:')){
    const mode=scenario.split(':')[1],isSFTP=mode==='nullable_sftp';
    const initial=isSFTP?{...config,transport:'sftp',share:null,root:'/',host_key_sha256:'SHA256:'+'a'.repeat(43),sftp_root_confined:true}:config;
    const h=await harness(mode==='first_save'?view():view(initial,status(1,'disabled')));
    h.edit('host','changed.synthetic.invalid');
    if(mode==='first_save'){h.edit('credential','SYNTHETIC-ref');h.edit('share','SYNTHETIC-share');}
    h.fault('lost');await h.submit();const newer=h.saved();
    const normalized={...modelDefaults,...newer.config};
    if(mode==='first_save'||mode==='nullable_sftp')newer.config=normalized;
    else if(mode==='omitted'){
      newer.config=normalized;for(const [key,value] of Object.entries(modelDefaults))if(newer.config[key]===value)delete newer.config[key];
    }else if(mode==='reordered')newer.config=Object.fromEntries(Object.entries(normalized).reverse());
    else if(mode==='numeric')newer.config={...normalized,connect_timeout_seconds:8.0,health_interval_seconds:30.0};
    else throw Error('Unknown defaults case');
    h.setSaved(newer);await h.click('refresh');assert.match(h.text(),/已返回状态与上次请求一致的保存设置/);
    assert(!h.get('save').disabled);assert(!h.get('connect').disabled);assert.equal(h.writes().length,1,'normalization never auto-connects');
    assert.equal(h.get('host').value,'changed.synthetic.invalid');
    await h.click('connect');assert.equal(h.writes().length,2);assert.equal(h.writes()[1].body.expected_generation,newer.status.config_generation);
  }else if(scenario==='reconcile_lost_unaccepted'){
    const h=await harness(view(config,status(1,'disabled'))),original=h.saved();h.edit('host','changed.synthetic.invalid');h.fault('lost');
    await h.submit();h.setSaved(original);await h.click('refresh');assert.match(h.text(),/尚未观察到上次请求的新版本/);
    assert(!h.get('save').disabled);assert(h.get('connect').disabled,'unaccepted save retains dirty draft');assert.equal(h.writes().length,1);
    // A late first acceptance arrives after readback; the deliberate CAS retry is rejected.
    h.setSaved(view({...h.writes()[0].body.config},status(2,'disabled')));await h.submit();
    assert.equal(h.writes()[1].body.expected_generation,1);assert.match(h.text(),/连接版本已变化/);assert(h.get('connect').disabled);
  }else if(scenario.startsWith('reconcile_lost_operation:')){
    const operation=scenario.split(':')[1],online={...status(1,'online',true),reachable:true,authenticated:true,readable:true};
    const h=await harness(operation==='resume'?view(config,status(1,'disabled')):view({...config,enabled:true},online));
    h.fault('lost');await h.click(operation==='resume'?'connect':'disconnect');assert.equal(h.writes().length,1);
    const newer=h.saved();newer.status.config_generation++;
    if(operation==='resume'){newer.config.require_encryption=false;newer.status.state='paused';newer.status.failure='nas_auth_failed';}
    else newer.config.health_interval_seconds=31;
    h.setSaved(newer);await h.click('refresh');assert(!h.text().includes('已返回状态与上次请求一致的保存设置'));
    assert(h.get('save').disabled);assert(h.get('connect').disabled,'no-draft lost operation still needs review');
    await h.submit();await h.click('connect');assert.equal(h.writes().length,1);
    if(operation==='resume'){assert(!h.get('disconnect').disabled);await h.click('disconnect');assert.equal(h.writes()[1].body.expected_generation,3);}
    await h.click('reset');assert(!h.get('save').disabled);assert(!h.get('connect').disabled);
  }else if(scenario==='first_run'){
    const h=await harness();assert.match(h.text(),/尚未配置/);assert.match(h.text(),/认证：尚未确认/);
    assert.equal(h.writes().length,0);assert(h.get('connect').disabled);assert(h.get('disconnect').disabled);
    h.edit('host','synthetic.invalid');h.edit('credential','SYNTHETIC-ref');h.edit('share','SYNTHETIC-share');h.edit('folder','Family');
    await h.submit();assert.equal(h.writes().length,1);assert.equal(h.writes()[0].method,'PUT');
    assert.equal(h.writes()[0].body.config.enabled,false);assert.equal(h.writes()[0].body.config.require_encryption,true);
    assert.match(h.text(),/登录引用已保存/);assert.match(h.text(),/认证：尚未确认/);assert(!h.get('connect').disabled);
    await h.click('connect');assert.equal(h.writes().length,2);assert(h.get('connect').disabled);
    await h.click('connect');assert.equal(h.writes().length,2,'no programmatic duplicate connect');
    assert.match(h.text(),/连接认证读取仍待确认/);assert.match(h.text(),/认证：尚未确认/);
    await h.click('disconnect');assert.equal(h.writes().length,3);assert.equal(h.saved().config.enabled,false);
    assert.match(h.text(),/已断开并停用读取/);assert(h.get('disconnect').disabled);
    assert(!h.calls.some(call=>/probe|mount|scan|read|keychain|password|models|actions/.test(call.path)));
  }else if(scenario==='sftp'){
    const h=await harness(view(config,status(1,'disabled')));h.edit('transport','sftp');
    assert(h.get('smb').hidden);assert(!h.get('sftp').hidden);assert(h.get('share').disabled);
    assert.equal(h.get('sftp-root').value,'/');assert.equal(h.get('port').attrs.placeholder,'22');
    await h.submit();assert.equal(h.writes().length,0);
    h.edit('host-key','SHA256:'+'a'.repeat(43));await h.submit();assert.equal(h.writes().length,0);
    h.edit('confined',true);h.edit('port',2222);await h.submit();
    const sent=h.writes()[0].body.config;assert.equal(sent.root,'/');assert.equal(sent.share,null);
    assert.equal(sent.sftp_root_confined,true);assert.equal(sent.enabled,false);assert.equal(sent.port,2222);
    assert.equal(sent.operation_timeout_seconds,3,'preserve source-owned advanced config');
    assert.match(h.text(),/不会修改 NAS 的账号或目录权限/);assert.match(h.text(),/认证：尚未确认/);
  }else if(scenario==='lost_response'){
    const h=await harness(view(config,status(1,'disabled')));h.edit('host','changed.synthetic.invalid');h.fault('lost');
    await h.submit();assert.match(h.text(),/请求结果尚未确认/);assert(h.get('save').disabled);
    await h.submit();await h.click('connect');assert.equal(h.writes().length,1,'uncertain acceptance blocks duplicate write');
    await h.click('refresh');assert.match(h.text(),/已返回状态与上次请求一致的保存设置/);assert(!h.get('save').disabled);
    assert.equal(h.get('host').value,'changed.synthetic.invalid');assert.equal(h.writes().length,1);
    const other=await harness(view(config,status(1,'disabled')));other.fault('lost');await other.click('connect');
    other.setSaved(view({...config,host:'other.synthetic.invalid',enabled:true},{...status(3,'paused',true),failure:'nas_auth_failed'}));
    await other.click('refresh');assert(other.get('save').disabled);assert(other.get('connect').disabled);
    assert.equal(other.get('host').value,'synthetic.invalid','unrelated changes require explicit review even without a pending draft');
    await other.click('reset');assert.equal(other.get('host').value,'other.synthetic.invalid');assert(!other.get('connect').disabled);
  }else if(scenario==='conflict_and_disconnect'){
    const online={...status(1,'online',true),reachable:true,authenticated:true,readable:true,connection_epoch:'SYNTHETIC-epoch'};
    const h=await harness(view({...config,enabled:true},online));h.edit('host','draft.synthetic.invalid');
    h.setSaved(view({...config,host:'other.synthetic.invalid',enabled:true},{...online,config_generation:2}));
    await h.submit();assert.match(h.text(),/连接版本已变化/);assert.equal(h.get('host').value,'draft.synthetic.invalid');assert(h.get('save').disabled);
    await h.click('refresh');assert(h.get('save').disabled);assert.equal(h.get('host').value,'draft.synthetic.invalid');
    assert(!h.get('disconnect').disabled);await h.click('disconnect');
    assert.equal(h.writes()[1].body.expected_generation,2);assert.equal(h.saved().config.host,'other.synthetic.invalid');
    assert.equal(h.get('host').value,'draft.synthetic.invalid');await h.click('reset');
    assert.equal(h.get('host').value,'other.synthetic.invalid');assert(!h.get('save').disabled);
  }else if(scenario==='navigation_draft_and_late_read'){
    const h=await harness(view(config,status(1,'disabled')));h.edit('host','draft.synthetic.invalid');
    h.setSaved(view({...config,host:'other.synthetic.invalid'},status(2,'disabled')));h.render();await tick();
    assert.equal(h.get('host').value,'draft.synthetic.invalid');assert(h.get('save').disabled);assert.match(h.text(),/别处发生变化/);
    await h.click('reset');h.hold('read');const job=h.click('refresh');
    h.edit('host','typed.synthetic.invalid');h.resolve();await job;
    assert.equal(h.get('host').value,'typed.synthetic.invalid','slow readback must preserve typing');
    h.hold('read');const old=h.click('refresh');h.render();await tick();
    h.edit('host','newview.synthetic.invalid');h.resolve();await old;
    assert.equal(h.get('host').value,'newview.synthetic.invalid','detached response must not overwrite current view');
  }else if(scenario==='unknown_and_validation'){
    const h=await harness(view(config,status(1,'disabled')));h.edit('host','synthetic.invalid');h.fault('invalid');
    await h.submit();assert.match(h.text(),/请求未通过/);assert(!h.text().includes('SYNTHETIC-secret'));assert(!h.text().includes('<script>'));
    h.fault('read');await h.click('refresh');assert(h.get('save').disabled);assert(h.get('connect').disabled);
    assert.equal(h.get('host').value,'synthetic.invalid');await h.submit();assert.equal(h.writes().length,1);
    await h.click('refresh');assert(!h.get('save').disabled);
    h.hold('write');const job=h.submit();await h.submit();assert.equal(h.writes().length,2);
    assert(h.get('host').disabled);h.resolve();await job;assert(!h.get('host').disabled);
  }else if(scenario==='truth_and_polling'){
    const st={...status(1,'paused',true),failure:'nas_auth_failed',authenticated:false,readable:false};
    const h=await harness(view({...config,enabled:true},st));assert.match(h.text(),/主机可达：尚未确认/);
    assert.match(h.text(),/登录信息引用未就绪或认证未通过/);assert(!h.text().includes('密码错误'));
    h.setSaved(view({...config,enabled:true},{...st,state:'backoff',failure:'nas_timeout',retry_in_seconds:1.2}));
    await h.click('refresh');assert.match(h.text(),/2 秒后自动重试/);assert.match(h.text(),/不要把旧资料视为当前可用/);
    for(let i=0;i<30;i++){const timer=[...h.timers].find(([,entry])=>entry.delay===2000);if(!timer)break;h.timers.delete(timer[0]);timer[1].fn();await tick();}
    assert.equal(h.writes().length,0,'polling is cached GET only');assert(h.calls.length<=17,'automatic refresh has finite budget');
    h.setSaved(view({...config,enabled:true},{...st,state:'online',reachable:true,authenticated:true,readable:true,failure:null}));
    await h.click('refresh');assert.match(h.text(),/读取连接在线/);assert.match(h.text(),/不证明目录完整/);
    h.setSaved(view({...config,enabled:true},{...st,state:'backoff',failure:'nas_unavailable',retry_in_seconds:null}));
    await h.click('refresh');assert.match(h.text(),/连接检查已过期/);assert(!h.text().includes('自动重试。'));
  }else throw Error('Unknown scenario '+scenario);
  console.log('PASS synthetic Console '+scenario);
}
run().catch(error=>{console.error(error);process.exitCode=1;});
