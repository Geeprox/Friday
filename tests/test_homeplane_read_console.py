"""Truthful cached-state labels; no scans, authentication or source read calls."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_console_cached_read_sources_preserve_unknown_and_unconfigured():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the existing Console DOM fixture")
    source = Path(__file__).resolve().parents[1] / "homeplane/static/app.js"
    program = r"""
const fs=require('fs'),assert=require('assert'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const start=source.indexOf('function renderLocalReadSources(');
const end=source.indexOf('\nasync function refreshHaStatus',start);
assert(start>=0&&end>start);
const calls=[];
class Element {
 constructor(tag,text,attrs={}){this.tag=tag;this.text=text??'';this.attrs=attrs;this.children=[];this.isConnected=true;}
 append(...children){this.children.push(...children);}
 replaceChildren(...children){this.children=[...children];}
 allText(){return this.text+' '+this.children.map(n=>n.allText()).join(' ');}
}
const context={Promise,el:(tag,text,attrs)=>new Element(tag,text,attrs),api:null};vm.createContext(context);
vm.runInContext(source.slice(start,end),context);
async function render(values){
 calls.length=0;context.api=async path=>{calls.push(path);if(values===null)throw Error('SIMULATED offline');return values[path];};
 const root=new Element('section');context.renderLocalReadSources(root);
 await new Promise(resolve=>setImmediate(resolve));
 assert.deepEqual(calls,['/api/settings/nas/connection','/api/family-media/context','/api/query/contracts']);
 assert(!calls.some(path=>/scan|resolve|probe|original|resume|source-review/.test(path)));
 return root.allText();
}
(async()=>{
 const values={'/api/settings/nas/connection':{state:'not_configured'},'/api/family-media/context':{source_ref:null,source_online:false},'/api/query/contracts':{consumer_adoption:{configured:false,peer_verified:null}}};
 let text=await render(values);assert(text.includes('硬盘直连 · 未配置'));assert(text.includes('尚未配置直连来源'));assert(text.includes('尚未接入读取许可'));
 text=await render(null);assert(text.includes('状态暂不可确认'));assert(!text.includes('尚未配置直连来源'));assert(!text.includes('尚未接入读取许可'));
 values['/api/settings/nas/connection']={state:'paused',failure:'nas_auth_failed'};
 values['/api/family-media/context']={source_ref:'SIMULATED-family',source_online:false};
 values['/api/query/contracts']={consumer_adoption:{configured:true,peer_verified:null}};
 text=await render(values);assert(text.includes('连接已暂停'));assert(text.includes('来源暂不可读取'));assert(text.includes('每次读取时核验'));assert(!text.includes('验证通过。Mio'));
 values['/api/settings/nas/connection']={state:'online'};values['/api/family-media/context'].source_online=true;
 text=await render(values);assert(text.includes('显式扫描后进入目录'));assert(text.includes('尚未核验'));assert(!text.includes('播放已验证'));
})().catch(error=>{console.error(error);process.exit(1);});
"""
    result = subprocess.run([node, "-e", program, str(source)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
