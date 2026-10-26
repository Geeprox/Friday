'use strict';
// Actual product controller with a minimal DOM and a recording transport.
// Verify spatial actions use the same single-command owner and fresh state.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.events={};this.dataset={};this.hidden=false;this.style={};this.textContent='';this.classList={add:n=>this.setAttribute('class',this.className+' '+n),remove:n=>this.setAttribute('class',this.className.split(' ').filter(x=>x!==n).join(' ')),contains:n=>this.className.split(' ').includes(n)};}
 setAttribute(k,v){this.attrs[k]=String(v);} getAttribute(k){return this.attrs[k]??null;} removeAttribute(k){delete this.attrs[k];}
 get className(){return this.attrs.class||'';} set className(v){this.attrs.class=v;}
 append(...cs){cs.forEach(c=>{this.children.push(c);c.parent=this;});}replaceChildren(...cs){this.children=[];this.append(...cs);}
 addEventListener(t,cb){(this.events[t]??=[]).push(cb);}remove(){if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);}
 contains(n){return n===this||this.children.some(c=>c.contains(n));}
 matches(s){if(s.startsWith('.'))return this.classList.contains(s.slice(1));if(s.startsWith('['))return this.getAttribute(s.slice(1,-1))!=null;return this.tagName===s;}
 querySelectorAll(s){const parts=s.split(' '),tail=parts.pop(),all=this.children.flatMap(c=>[c,...c.querySelectorAll('*')]);return all.filter(c=>(tail==='*'||c.matches(tail))&&(!parts.length||(()=>{for(let p=c.parent;p;p=p.parent)if(p.matches(parts[0]))return true;return false;})()));}
 querySelector(s){return this.querySelectorAll(s)[0]||null;}
 get isConnected(){return true;}
}
(async()=>{
 const appearance=await import('data:text/javascript;base64,'+fs.readFileSync('homeplane/static/house-appearance.js').toString('base64'));
 const body=new Element('body'),root=new Element('main');body.append(root);
 const doc={body,activeElement:null,createElement:t=>new Element(t),getElementById:id=>body.querySelectorAll('*').find(n=>n.getAttribute('id')===id)};
 const channel=(id,name)=>({id,name,kind:'light',available:true,value:'off',state_version:'version-1',attributes:{},capabilities:[{name:'light.set'}],presentation:{emits_light:true,lighting_role:'main',fixture:'main_light'}});
 const lamp={id:'lamp-owner',name:'客厅主灯',room:'客厅',kind:'light',controls:[channel('lamp','客厅主灯')],readings:[]};
 const panel={id:'panel-owner',name:'两路灯',room:'客厅',kind:'light',controls:[channel('left','左灯'),channel('right','右灯')],readings:[]};
 const overview={devices:[lamp,panel],rooms:['客厅'],presentation:{revision:0,climate_style:'auto',overrides:{}},preferences:{}};
 const layout={rooms:[{id:'room',name:'客厅',polygon:[[0,0],[1,0],[1,1],[0,1]]}],devices:[]};
 const writes=[];let hooks,latest=null;
 const context=vm.createContext({document:doc,console,Map,Set,Promise,structuredClone,crypto:require('node:crypto'),setInterval:()=>0,clearInterval:()=>{},setTimeout:()=>0,clearTimeout:()=>{},...appearance,
  mountHouse:(container,opts)=>{hooks=opts;return{focus(){},updateDevices(ds){latest=ds;},setBusy(){},dispose(){},place(){}};},
  fetch:async(url,options)=>({ok:true,json:async()=>{
   if(url==='/api/home/overview')return structuredClone(overview);
   if(url==='/api/home/layout')return structuredClone(layout);
   if(url==='/api/home/control'){
    const request=JSON.parse(options.body);writes.push(request);
    const c=overview.devices.flatMap(d=>d.controls).find(c=>c.id===request.target);assert.ok(c);c.value=request.desired.power;c.state_version='version-'+(writes.length+1);
    return {status:'reported_match'};
   }
   throw Error('unexpected path '+url);
  }})});
 const source=fs.readFileSync('homeplane/static/home-experience.js','utf8').replace(/^import .*;$/gm,'').replace('export function mountExperience','function mountExperience');
 vm.runInContext(source,context);context.root=root;
 const owner=vm.runInContext("mountExperience(root,{page:'Home',go(){},snapshot:{}})",context);
 await new Promise(setImmediate);assert.ok(hooks);
 const pending=hooks.onQuick({control:'lamp'});hooks.onQuick({control:'lamp'});await pending;await new Promise(setImmediate);
 assert.equal(writes.length,1,'double click must not send a second command');
 assert.equal(writes[0].target,'lamp');assert.equal(writes[0].operation,'light.set');assert.equal(writes[0].desired.power,'on');assert.equal(writes[0].expected_state_version,'version-1');
 assert.equal(latest.find(d=>d.id==='lamp-owner').controls[0].value,'on');
 await hooks.onQuick({control:'lamp'});assert.equal(writes.length,2);assert.equal(writes[1].desired.power,'off');assert.equal(writes[1].expected_state_version,'version-2');
 await hooks.onQuick({control:'not-registered'});assert.equal(writes.length,2);
 hooks.onDevice(panel);assert.equal(writes.length,2,'multi-circuit panel click must open controls without actuating both');
 const inspector=root.querySelector('.spatial-panel');assert.equal(inspector.hidden,false);assert.equal(inspector.querySelectorAll('.type-control').length,2);
 hooks.onRoomDevices('客厅');assert.equal(inspector.querySelectorAll('.spatial-device-row').length,2);
 owner.dispose();console.log('spatial controls: single command, fresh state, synchronized readback, unknown target, multi-circuit selection OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
