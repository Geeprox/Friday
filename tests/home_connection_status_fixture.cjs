const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(process.argv[2], 'homeplane/static/app.js'), 'utf8');
const begin = source.indexOf('function showHaStatus(output) {');
const end = source.indexOf('function renderLocalReadSources', begin);
assert(begin >= 0 && end > begin);
const context = vm.createContext({haStatus:null, snapshot:null, location:{search:''},
  settingsMessage:(output,text)=>{output.message=text;}, detail:()=>({}), copyName:value=>value});
vm.runInContext(source.slice(begin,end),context);
const cases = [
  [{configured:true,credential_available:true}, '本机已有登录记录'],
  [{configured:true,credential_available:true,home_readable:true}, '家庭只读连接正常'],
  [{configured:true,credential_available:true,server_acceptance:'unavailable'}, '现有登录暂不可用'],
  [{configured:true,credential_available:false}, '本机登录记录未就绪'],
];
for (const [status, expected] of cases) {
  const output = {setAttribute(){},append(){}};
  context.haStatus=status;
  context.showHaStatus(output);
  assert(output.message.includes(expected));
  assert.notEqual(output.message,'已授权');
}
console.log('PASS actual Settings consumer distinguishes saved record, readable home and unavailable authorization');

const consumerBegin=source.indexOf('function arlRefreshNotice(report) {');
const consumerEnd=source.indexOf('function showHaStatus(output) {',consumerBegin);
assert(consumerBegin>=0&&consumerEnd>consumerBegin);
const consumers=vm.createContext({el:(_tag,text)=>({text}),console});
vm.runInContext(source.slice(consumerBegin,consumerEnd),consumers);
for(const status of ['policy_not_configured','codex_unavailable','planning_contract_unavailable','owned_source_unavailable','review_required']) {
  const output={rows:[],append(row){this.rows.push(row.text);}};
  consumers.showArlConsumers(output,{query:{console_readable:true,configured:false},planning:{status}});
  assert(output.rows.includes('家庭状态可在本机只读查询。'));
  assert(output.rows.includes('跨应用家庭查询尚未接通。'));
  assert(!output.rows.some(text=>text.includes('业务就绪')));
  if(status==='review_required')assert(output.rows.some(text=>text.includes('明确批准')));
}
console.log('PASS actual ARL Settings consumer separates runtime, query scope and reviewed planning');

const configured={rows:[],append(row){this.rows.push(row.text);}};
consumers.showArlConsumers(configured,{query:{console_readable:false,configured:true},planning:{status:'review_required'}});
assert(configured.rows.includes('读取消费者已配置；跨应用调用仍待接通。'));
assert(configured.rows.includes('家庭尚未连接，暂不能查询状态。'));

const knownPending={status:'ready',ready:true,metadata_refresh_pending:true,metadata_freshness:'last_known',metadata_refresh_source:'local_worker'};
const unknownPending={status:'runtime_busy',ready:false,metadata_refresh_pending:true,metadata_freshness:'unknown',metadata_refresh_source:'local_worker'};
assert(consumers.arlRefreshNotice(knownPending).includes('保留上次服务状态'));
assert(consumers.arlRefreshNotice(unknownPending).includes('尚无最近连接记录'));
assert(!consumers.arlRefreshNotice(unknownPending).includes('不可用'));
assert(!consumers.arlRefreshNotice({...knownPending,metadata_refresh_source:'sdk'}).includes('本机正在处理另一项工作'));
assert.equal(consumers.arlRefreshNotice({status:'disconnected',metadata_refresh_pending:false}), '');
const statusBegin=source.indexOf('  let arlConsumerSequence = 0;');
const statusEnd=source.indexOf('  const checkArl = async path',statusBegin);
assert(statusBegin>=0&&statusEnd>statusBegin);
const statusOutput={rows:[],isConnected:true,append(row){this.rows.push(row.text);}};
const statusContext=vm.createContext({arl:{output:statusOutput},document:{body:{dataset:{consoleReadOnly:'false'}}},copyName:value=>value,el:(_tag,text)=>({text}),arlRefreshNotice:consumers.arlRefreshNotice,settingsMessage:(output,text)=>{output.message=text;output.rows=[];}});
vm.runInContext(source.slice(statusBegin,statusEnd)+'\n globalThis.displayArl=showArl;',statusContext);
statusContext.displayArl(knownPending);
assert.equal(statusOutput.message,'ARL 已连接');
assert(statusOutput.rows.some(text=>text.includes('保留上次服务状态')));
statusContext.displayArl(unknownPending);
assert(statusOutput.message.includes('尚无最近连接记录'));
assert(!statusOutput.message.includes('不可用'));
statusContext.displayArl({status:'disconnected',metadata_refresh_pending:false});
assert.equal(statusOutput.message,'共享服务未连接');
console.log('PASS actual ARL display preserves known state during local busy and keeps unknown distinct from disconnection');
