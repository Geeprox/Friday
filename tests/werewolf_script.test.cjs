const fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');
const host=fs.readFileSync('homeplane/static/werewolf-host.js','utf8');
const display=fs.readFileSync('homeplane/static/werewolf-party.js','utf8');
const extract=(source,start,end)=>source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));
const packets=JSON.parse(execFileSync('.venv/bin/python',['-B','-c',`
import json
from tests.test_werewolf import game,until,act,role
g,_=game();until(g,'vote')
for seat in g.players:
    if seat!=1:act(g,seat,'vote',1)
g.advance();vote=g.public()
h,_=game();hunter=role(h,'hunter');act(h,1,'attack',hunter);until(h,'hunter')
act(h,hunter,'shoot',1);h.advance()
print(json.dumps([vote,h.public()]))
`],{encoding:'utf8'}));

class Element{
 constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.attrs={};this._text='';}
 setAttribute(key,value){this.attrs[key]=String(value);}
 getAttribute(key){return key.startsWith('data-')?this.dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]??null:this.attrs[key]??null;}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text+this.children.map(n=>n.textContent).join(' ');}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this._text='';this.children=[];this.append(...nodes);}
 querySelector(selector){return all(this).find(n=>selector==='[data-timer]'&&n.dataset.timer);}
}
const all=node=>[node,...node.children.flatMap(all)];

for(const [index,packet]of packets.entries())test('original public outcome reaches host and TV: '+index,()=>{
 const document={createElement:tag=>new Element(tag),body:new Element('body')};
 const report={active:true,game:packet,camera_hints:[]},root=new Element('main'),roomLabel=new Element('p');
 const scope=vm.createContext({document,current:report,report,root,roomLabel,viewKey:''});
 vm.runInContext(host.split('\n').find(line=>line.startsWith('const node=')),scope);
 vm.runInContext(display.split('\n').find(line=>line.startsWith('const h=')),scope);
 vm.runInContext(extract(host,'function board(game)','\n function live()'),scope);
 vm.runInContext(extract(display,'function phaseHeader(g)','\nfunction seatButton('),scope);
 vm.runInContext(extract(display,'function updateTimer(g)','\nasync function poll()'),scope);
 const board=scope.board(packet),hostLine=all(board).find(n=>n.className==='ww-host-line');
 assert.equal(hostLine.textContent,packet.host_script.text);
 scope.renderDisplay(report);
 assert.equal(all(root).find(n=>n.className==='public-script').textContent,packet.host_script.text);
 assert.match(packet.host_script.text,index===0?/1 号被放逐。 第 2 夜。/:/猎人带走了 1 号。 现在开始自由发言。/);
 assert(packet.players.every(p=>!Object.hasOwn(p,'role')),'individual roles remain private');
 const retained=root.children[0];packet.seconds_left-=1;scope.renderDisplay(report);
 assert.equal(root.children[0],retained,'timer refresh keeps the same public board');
});
