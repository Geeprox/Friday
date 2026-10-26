"""Execute the existing Console's actual HomeFlow mount/lifecycle functions."""

import asyncio
import json
from pathlib import Path

import pytest
from test_homeplane_home_conversation_widget import HARNESS, NODE
from test_homeplane_home_flow import application

CONSOLE = HARNESS.split("const config = JSON.parse(process.argv[3]);")[0] + r"""
const input=readline.createInterface({input:process.stdin}), pending=new Map(), delayed=[];
let serial=0, holdBootstrap=false;const seen=[];
const emit=value=>process.stdout.write(JSON.stringify(value)+'\n');
input.on('line',line=>{const value=JSON.parse(line), accept=pending.get(value.bridge_id);
  pending.delete(value.bridge_id); accept(value.response);});
async function api(path,options={}) {
  const bridge_id=++serial;
  const promise=new Promise(resolve=>pending.set(bridge_id,resolve));
  emit({type:'request',bridge_id,path,body:options.body?JSON.parse(options.body):null});
  const result=await promise;
  seen.push({path,code:result.code});
  if(path.endsWith('/bootstrap')&&holdBootstrap)
    return await new Promise(resolve=>delayed.push(()=>resolve(result)));
  return result;
}
const el=(tag,text=null,attrs={})=>{const value=document.createElement(tag);
  if(text!==null)value.textContent=text;for(const [key,item] of Object.entries(attrs))value.setAttribute(key,item);return value;};
const button=(label,action,attrs={})=>{const value=el('button',label,attrs);value.addEventListener('click',action);return value;};
const note=text=>el('p',text);
const app=fs.readFileSync(process.argv[3],'utf8');
const functions=app.slice(app.indexOf('function retainHomeConversationDraft()'),app.indexOf('\nfunction render()'))
  .replace("import('/static/home-conversation.js')",'Promise.resolve(HomeConversationModule)');
const scope=new Function('api','el','button','note','HomeConversationModule',`
  let homeConversationView=null,homeConversationSlot=null,homeConversationSession=null,
      homeConversationVersion=0,homeConversationBoot=null,homeConversationCheck=null,homeConversationDraft='',page='Ask Home';
  ${functions}
  return {mount:mountHomeConversation,clear:clearHomeConversation,check:checkHomeConversation,
    current:()=>({session:homeConversationSession,view:homeConversationView,slot:homeConversationSlot})};
`)(api,el,button,note,{HomeConversation});
const settle=()=>new Promise(resolve=>setTimeout(resolve,2));
async function until(predicate){for(let n=0;n<500;n++){if(predicate())return;await settle();}throw Error('Console lifecycle timeout: '+host.textContent+' '+JSON.stringify(seen));}
const host=el('main'), original=el('form','Existing structured Ask');host.append(original);document.body.append(host);
try {
  scope.mount(host);await until(()=>scope.current().session);
  const session=scope.current().session, root=host.querySelector('.hc-root');
  root.querySelector('[data-hc="input"]').value='以后我回家不要自动开空调';
  root.querySelector('form').fire('submit');
  await until(()=>root.querySelector('[data-hc="approve"]')&&!root.querySelector('[data-hc="approve"]').disabled);
  const transcript=root.textContent;
  await scope.check();assert.equal(root.textContent,transcript);assert.equal(scope.current().session,session);
  scope.clear();assert.equal(host.children.length,1);assert.equal(host.children[0],original);
  holdBootstrap=true;scope.mount(host);await until(()=>delayed.length===1);
  scope.clear();delayed.shift()();holdBootstrap=false;await settle();
  await until(()=>pending.size===0);
  assert.equal(host.children.length,1);assert.equal(scope.current().session,null);
  assert.equal(writes,0);
  emit({type:'done',checked:['actual_console_mount','actual_http_preview','context_preserves_transcript',
    'clear_disposes_session','late_bootstrap_disposed','structured_ask_preserved']});
}catch(error){scope.clear();emit({type:'failure',error:error.stack});process.exitCode=1;}
finally{input.close();}
"""


@pytest.mark.asyncio
async def test_console_mount_real_http_context_and_late_bootstrap_cleanup(tmp_path):
    assert NODE is not None
    async with application(tmp_path) as (app, client):
        script = tmp_path / "actual-console.mjs"
        script.write_text(CONSOLE)
        static = Path(__file__).parents[1] / "homeplane/static"
        process = await asyncio.create_subprocess_exec(NODE, str(script),
            str(static / "home-conversation.js"), str(static / "app.js"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={"PATH":"/opt/homebrew/bin:/usr/bin:/bin", "TMPDIR":"/tmp"})
        result, requests = None, []
        try:
            async with asyncio.timeout(15):
                while line := await process.stdout.readline():
                    message = json.loads(line)
                    if message["type"] == "failure":
                        pytest.fail(message["error"])
                    if message["type"] == "done":
                        result = message
                        break
                    response = await client.post(message["path"], json=message["body"])
                    assert response.status_code == 200, response.text
                    requests.append(message["path"])
                    process.stdin.write((json.dumps({"bridge_id":message["bridge_id"],
                                                   "response":response.json()})+"\n").encode())
                    await process.stdin.drain()
                assert result and await process.wait() == 0, (await process.stderr.read()).decode()
            assert requests.count("/api/home-flow/bootstrap") == 2
            assert requests.count("/api/home-flow/dispose") == 2
            assert not app.state.home_flow.sessions
            assert "late_bootstrap_disposed" in result["checked"]
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()
