'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const {install}=require('./copy_layout_dom.cjs');
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.listeners=[];this._text='';}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}
  set innerHTML(_){throw Error('No copy or provider HTML is interpreted');}
  setAttribute(k,v){this.attrs[k]=String(v);}
  getAttribute(k){return this.attrs[k]??null;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this._text='';this.children=nodes;}
  addEventListener(event,listener){this.listeners.push({event,listener});}
}
const sourceRoot=process.argv[2], approved=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
const body=new Element('body'),document={createElement:tag=>new Element(tag)};
const dom=install(Element,document,body);
const context=vm.createContext({...dom,document,console});
vm.runInContext(fs.readFileSync(path.join(sourceRoot,'homeplane/static/copy-layout.js'),'utf8'),context);
const copy=context.FridayCopyLayout;
const factories=new Map();
for(const [file,factory,end] of [
  ['homeplane/static/app.js','el','const add ='],
  ['homeplane/static/file-steward.js','node','const append ='],
  ['homeplane/static/home-conversation.js','element','function button('],
  ['homeplane/people/static/people.js','node','const add='],
]) {
  let text=fs.readFileSync(path.join(sourceRoot,file),'utf8').replace(/^import '[^']*copy-layout.js';\n/m,'');
  assert(text.includes(end),'Real factory boundary');
  const prefix=text.slice(0,text.indexOf(end));
  const sandbox=vm.createContext({...dom,document,FridayCopyLayout:copy,console});
  vm.runInContext(prefix+'\nthis.factory='+factory+';',sandbox);
  factories.set(file,sandbox.factory);
}
let values=0;
for(const [id,name,compound,file] of approved.filter(row=>row[3].endsWith('.js'))) {
  const factory=factories.get(file),scope=document.createElement('section');body.append(scope);
  const named=copy.name(id),description=compound.slice(name.length+3);
  assert.equal(String(named),name);
  assert.equal(JSON.stringify({name:named}),JSON.stringify({name}),'Display object serializes only its short name');
  const button=factory('button',named,{id:'finite-control-'+values,'aria-label':named,disabled:''});
  const handler=()=>{};button.addEventListener('click',handler);button.disabled=true;
  scope.append(button);copy.flush();
  assert.equal(button.textContent,name,'Real factory button short name');
  assert.equal(button.getAttribute('aria-label'),name,'Short accessible name');
  assert.equal(button.getAttribute('disabled'),'');assert(button.disabled);
  assert.equal(button.listeners[0].listener,handler,'Original control and callback identity');
  const note=scope.querySelectorAll('[data-copy-description="'+id+'"]')[0];
  assert(note&&note.parentNode===button.parentNode,'Description is an adjacent sibling');
  assert.equal(note.textContent,description);assert.equal(note.style.display,'block');
  assert(button.getAttribute('aria-describedby').split(' ').includes(note.id));
  const select=factory('select',null,{'aria-label':'finite selection'}),option=factory('option',named,{value:'unchanged-protocol-value'});
  select.append(option);scope.append(select);copy.flush();
  assert.equal(option.textContent,name);assert.equal(option.getAttribute('value'),'unchanged-protocol-value');
  const optionNotes=scope.children.filter(n=>n.getAttribute('data-copy-description')===id&&n!==note);
  assert.equal(optionNotes.length,1);assert.equal(optionNotes[0].textContent,name+'：'+description);
  assert(optionNotes[0].parentNode===select.parentNode,'Option scope is visible beside the select');
  scope.remove();copy.flush();values++;
}
// Existing headings may be rebound as navigation changes; no stale caption.
const factory=factories.get('homeplane/static/app.js'),heading=factory('h2',copy.name('Friday-1033'));
body.append(heading);copy.flush();assert.equal(body.querySelectorAll('[data-copy-description]').length,1);
heading.textContent='我的家';copy.decorate(heading,'我的家');copy.flush();
assert.equal(body.querySelectorAll('[data-copy-description]').length,0);assert.equal(heading.getAttribute('aria-describedby'),null);
// Household strings that happen to equal a short display name are still plain text.
const provider=factory('button','状态匹配');body.append(provider);copy.flush();
assert.equal(provider.getAttribute('data-friday-copy'),null);
assert.equal(body.querySelectorAll('[data-copy-description]').length,0);
// Composed dynamic facts keep their fixed descriptions outside the text name.
const composed=factory('p',copy.withCopy('SYNTHETIC folder · 扫描完成',copy.name('Friday-1843')));
body.append(composed);copy.flush();assert.equal(composed.textContent,'SYNTHETIC folder · 扫描完成');
assert(body.textContent.includes(approved.find(row=>row[0]==='Friday-1843')[2].slice('扫描完成'.length+3)));
console.log(JSON.stringify({approved_web_values:values,actual_source_factories:4,short_names_and_adjacent_descriptions:true,
  control_value_disabled_and_callback_preserved:true,stale_description_removed:true,provider_strings_plain_text:true,synthetic_only:true}));
