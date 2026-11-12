import assert from 'node:assert/strict';
import {test} from 'node:test';
import {homeServiceSummary,homeAttention} from '../homeplane/static/home-status.js';
import {safetyEventTitle} from '../homeplane/static/safety-event-labels.js';

test('a stale or missing service report never becomes a healthy agent',()=>{
 const missing=homeServiceSummary();assert.equal(missing.state,'unknown');
 const known=homeServiceSummary({worker:{configured:true,owned:true,state:'running',voice_input:true},infrastructure:{},rules:{available:true,automatic_enabled:true},arl:{status:'ready',ready:true,metadata_freshness:'last_known'}});
 assert.equal(known.rows.at(-1).state,'waiting');assert.equal(known.rows.at(-1).text,'上次记录');
 const stopped=homeServiceSummary({worker:{configured:true,capture_enabled:false},rules:{available:false,automatic_enabled:true}});
 assert.equal(stopped.rows[0].text,'已暂停');assert.equal(stopped.rows[2].text,'未就绪');
});

test('household perception counts the whole camera fleet and ignores deliberately closed sources',()=>{
 const ready={camera:'living',state:'live',recognition:'running',latency_ms:240};
 const row=(sessions,state='live',patch={})=>homeServiceSummary({worker:{
  configured:true,owned:true,state,camera_sessions:sessions,...patch,
 }}).rows[0];
 const mixed=row([ready,{camera:'hall',state:'stale',recognition:'waiting',latency_ms:1300}]);
 assert.equal(mixed.state,'degraded');
 assert.equal(mixed.text,'1 / 2 台感知中');
 assert.deepEqual(mixed.source,{camera:'hall',text:'画面中断'});
 assert.deepEqual(mixed.latency,{ms:240,slow:false,multiple:false});
 const closed={camera:'main',state:'shutter_closed',latency_ms:1300};
 const side=row([closed,ready],'shutter_closed',{error_class:'old_capture_failure'});
 assert.equal(side.text,'1 台感知中');
 assert.equal(side.state,'on');
 assert.equal(side.source,null);
 assert.equal(row([closed]).text,'镜头已关闭');
 assert.equal(row([closed], 'unavailable', {owned:false}).text,'暂不可用');
 assert.equal(row([ready], 'live', {capture_enabled:false}).text,'已暂停');
 assert.equal(row([ready], 'live', {recovery_pending:true}).state,'warning');
 assert.equal(row([]).text,'未更新');
 assert.equal(row([{camera:'preview',state:'live',recognition:'waiting'}]).text,'等待画面');
 assert.equal(row([{camera:'hall',state:'connecting'}]).source.text,'正在连接');
 const recovering=row([ready,{camera:'hall',state:'connecting',recognition:'recovering',frame_available:true}]);
 assert.equal(recovering.state,'degraded');
 assert.equal(recovering.text,'1 / 2 台感知中');
 assert.deepEqual(recovering.source,{camera:'hall',text:'感知恢复中'});
});

test('processing latency follows the slowest current recognition source',()=>{
 const ready={state:'live',recognition:'running'};
 const row=homeServiceSummary({worker:{configured:true,owned:true,state:'live',latency_ms:99,camera_sessions:[
  {...ready,camera:'main',latency_ms:210},
  {...ready,camera:'hall',latency_ms:1220},
  {...ready,camera:'other',latency_ms:-1},
 ]}}).rows[0];
 assert.equal(row.state,'degraded');
 assert.equal(row.text,'3 台感知中');
 assert.deepEqual(row.source,{camera:'hall',text:'处理较慢'});
 assert.deepEqual(row.latency,{ms:1220,slow:true,multiple:true});
});

test('enabled voice preference cannot replace the actual speech readiness report',()=>{
 const base={configured:true,owned:true,state:'running',voice_input:true,interaction_enabled:true};
 const row=patch=>homeServiceSummary({worker:{...base,...patch}}).rows[1];
 assert.equal(row({}).state,'waiting');
 assert.equal(row({home:{enabled:true,voice:'loading_local_models'}}).text,'正在准备');
 assert.equal(row({voice_sessions:[{enabled:true,voice:'unavailable'}]}).state,'warning');
 assert.equal(row({voice_sessions:[{enabled:true,voice:'ready'}]}).text,'已就绪');
 assert.equal(row({voice_sessions:[{enabled:true,voice:'transcribing'}]}).text,'处理中');
 assert.equal(row({voice_sessions:[{enabled:true}]}).state,'unknown');
 assert.equal(row({owned:false,voice_sessions:[{enabled:true,voice:'ready'}]}).state,'waiting');
 for(const pause of [{voice_input:false},{interaction_enabled:false},{capture_enabled:false}]){
  assert.equal(row({...pause,voice_sessions:[{enabled:true,voice:'ready'}]}).text,'已暂停');
 }
});

test('all active voice sources contribute to readiness without treating closed cameras as failures',()=>{
 const base={configured:true,owned:true,voice_input:true};
 const row=voice_sessions=>homeServiceSummary({worker:{...base,voice_sessions}}).rows[1];
 const ready={enabled:true,voice:'ready'},closed={enabled:false,voice:'unavailable'};
 assert.equal(row([ready,closed]).state,'on');
 assert.equal(row([closed]).state,'waiting');
 assert.equal(row([ready,{enabled:true,voice:'unavailable'}]).text,'部分可用');
 assert.equal(row([ready,{enabled:true,voice:'loading_local_models'}]).text,'部分就绪');
 for(const fault of [{audio_input:{status:'unavailable'}},{audio_analysis:{status:'unavailable'}}]){
  assert.equal(row([{...ready,...fault}]).state,'warning');
  assert.equal(row([ready,{...ready,...fault}]).text,'部分可用');
 }
});

test('review services surface only the current worker report and distinguish pressure from waiting',()=>{
 const worker={configured:true,owned:true,state:'live',safety_recording:{state:'ready'},
  camera_sessions:[{camera:'camera',state:'live',recognition:'running'}]};
 const check=(safety_review,patch={})=>homeServiceSummary({worker:{...worker,...patch,safety_review}});
 const summary=check({visual:'resource_limited',sound:'waiting_input'});
 assert.equal(summary.state,'warning');
 assert.deepEqual(summary.rows.filter(row=>row.review).map(row=>[row.label,row.text,row.state]),
  [['画面复核','资源不足','degraded'],['声音复核','等待声音','waiting']]);
 assert.equal(check({sound:'delayed'}).rows.at(-1).text,'复核延迟');
 assert.equal(check({sound:'paused'}).rows.at(-1).state,'off');
 assert.equal(check({sound:'running'}).rows.at(-1).text,'运行中');
 assert.equal(check({sound:'unavailable'}).rows.at(-1).state,'warning');
 for(const patch of [{owned:false},{capture_enabled:false},{state:'unavailable'}])
  assert.equal(check({sound:'running'},patch).rows.some(row=>row.review),false);
 assert.equal(check({visual:'constructor'}).rows.some(row=>row.review),false);
});

test('attention prioritizes unreviewed incidents and excludes simulated/reviewed events',()=>{
 const devices=[{id:'offline',name:'离线设备',room:'合成空间',available:false,readings:[]},{id:'low',name:'低电量设备',room:'合成空间',available:true,readings:[{device_class:'battery',value:12,unit:'%',available:true}]},{id:'stale',available:true,readings:[{device_class:'battery',value:1,available:false}]}];
 const events=[{id:'test',simulated:true,review:'unreviewed'},{id:'read',review:'acknowledged'},{id:'fall',review:'unreviewed',kind:'fall'}];
 const rows=homeAttention(devices,events,[]);
 assert.deepEqual(rows.map(r=>r.key),['safety:fall','battery:low','device:offline']);
 assert.equal(rows[0].title,'疑似跌倒');assert(!rows.some(r=>r.key.includes('stale')));
});

test('automatic intent alone does not report a running automation service',()=>{
 const row=rules=>homeServiceSummary({rules}).rows[2];
 assert.equal(row({available:true,automatic_enabled:true,mode_revision_valid:false}).text,'等待家庭状态');
 for(const[state,text]of [['awaiting_mode','等待家庭状态'],['manual','手动控制中'],['sleeping','睡眠模式'],['starting','正在启动']]){
  const value=row({available:true,automatic_enabled:true,execution_state:state});
  assert.equal(value.text,text);assert.notEqual(value.state,'on');
 }
 assert.equal(row({available:true,automatic_enabled:true,execution_state:'ready'}).state,'on');
 assert.equal(row({available:true,automatic_enabled:false,execution_state:'ready'}).text,'已暂停');
 const recovering=row({available:true,automatic_enabled:true,execution_state:'ready',clock:{scheduled_count:1,state:'recovering'}});
 assert.equal(recovering.text,'定时任务恢复中');assert.equal(recovering.state,'waiting');
});

test('home and event review preserve each actual evidence kind without inventing a fall or identity',()=>{
 const cases=[
  ['sound_review','crying','疑似哭声'],['sound_review','scream','疑似尖叫声'],
  ['sound_review','glass_break','疑似破碎声'],['sound_review','alarm','疑似警报声'],
  ['sound_review','unknown','声音待核对'],['visual_review',null,'画面待复核'],
  ['unknown',null,'待核对提醒'],
 ];
 for(const[kind,sound_kind,title]of cases){
  const event={id:'source-event',kind,sound_kind,review:'unreviewed',simulated:false,camera_name:'示例客厅',person_name:'不得推断此人的身份'};
  const [row]=homeAttention([],[event]);
  assert.equal(row.title,title);assert.equal(safetyEventTitle(event),title);
  assert.equal(row.detail,'示例客厅');assert.equal(row.page,'Safety');
 }
 const fall={id:'fall',kind:'fall',review:'unreviewed',person_name:'Example'};
 assert.equal(homeAttention([],[fall])[0].title,'疑似跌倒');
 assert.equal(safetyEventTitle(fall),'Example · 疑似跌倒');
 assert.deepEqual(homeAttention([],[{...fall,simulated:true,status:'test'}]),[]);
 assert.equal(safetyEventTitle({...fall,simulated:true,status:'test'}),'播报测试');
});

test('recording recovery stays quiet while exhausted persistence becomes actionable',()=>{
 const worker={configured:true,owned:true,state:'live',capture_enabled:true};
 const row=state=>homeServiceSummary({worker:{...worker,safety_recording:{state}}}).rows.find(r=>r.label==='异常记录');
 assert.equal(row('ready').state,'on');assert.equal(row('recovering').text,'正在恢复');
 assert.equal(row('saving').text,'正在保存');assert.equal(row('off').state,'off');
 assert.equal(row('unavailable').state,'warning');
 assert.equal(homeServiceSummary({worker}).rows.find(r=>r.label==='异常记录').state,'unknown');
 assert.deepEqual(homeAttention([],[],[],{state:'recovering',pending:1}),[]);
 const [notice]=homeAttention([],[],[],{state:'unavailable',dropped:1});
 assert.equal(notice.title,'有提醒未能保存');assert.equal(notice.page,'Safety');assert.equal(notice.eventId,undefined);
 assert.equal(homeAttention([],[],[],{state:'unavailable',unconfirmed:1})[0].title,'有提醒的保存结果待核对');
 assert.equal(homeAttention([],[],[],{state:'unavailable',partial:1})[0].title,'有提醒画面未完整保存');
});
