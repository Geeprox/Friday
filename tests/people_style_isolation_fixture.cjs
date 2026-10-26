'use strict';
// In-process DOM contract fixture: actual app/module source, no browser/network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = process.argv[2];
const appSource = fs.readFileSync(path.join(root,'homeplane/static/app.js'),'utf8');
const peopleSource = fs.readFileSync(path.join(root,'homeplane/people/static/people.js'),'utf8');
const liveSource = fs.readFileSync(path.join(root,'homeplane/people/static/live.js'),'utf8');
const copySource = fs.readFileSync(path.join(root,'homeplane/static/copy-layout.js'),'utf8');

class Element {
  constructor(tag) { this.tagName=tag;this.children=[];this.attributes={};this.events={};this.dataset={};
    const names=new Set();this.classList={add:name=>names.add(name),remove:name=>names.delete(name),contains:name=>names.has(name)}; }
  setAttribute(key,value) { this.attributes[key]=String(value); }
  getAttribute(key) { return this.attributes[key]??null; }
  append(...children) { for(const child of children){this.children.push(child);child.parent=this;} }
  replaceChildren(...children) { this.children=[];this.append(...children); }
  addEventListener(type, callback) { (this.events[type]??=[]).push(callback); }
  fire(type) { for(const callback of this.events[type]??[])callback({}); }
  remove() { if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this); }
}
function document(available) {
  const ids=new Map(['navigation','utility-navigation','developer-mode'].map(id=>[id,new Element('div')]));
  ids.get('developer-mode').checked=false;
  return {head:new Element('head'),body:{dataset:available?{peopleEnabled:'true'}:{}},
    createElement:tag=>new Element(tag),getElementById:id=>ids.get(id)};
}
function shared(available) {
  const doc=document(available),context=vm.createContext({document:doc,console,Promise,Map,Set,MutationObserver:class{observe(){}},queueMicrotask:()=>{}});
  vm.runInContext(copySource,context);
  // Execute the actual declarations/navigation without starting full console polling.
  vm.runInContext(appSource.slice(0,appSource.indexOf('function expireSnapshotStates()')),context);
  const helperStart=appSource.indexOf('function loadPeopleStyles()'),helperEnd=appSource.indexOf('function render()');
  vm.runInContext(appSource.slice(helperStart,helperEnd),context);
  vm.runInContext('navigation()',context);
  const entries=[...doc.getElementById('navigation').children,...doc.getElementById('utility-navigation').children];
  return {context,doc,entries};
}
async function run() {
  const absent=shared(false),enabled=shared(true);
  assert(!absent.entries.some(e=>e.getAttribute('data-page')==='People'));
  assert(enabled.entries.some(e=>e.getAttribute('data-page')==='People'));
  assert.equal(absent.doc.head.children.length+enabled.doc.head.children.length,0);
  const styleReady=vm.runInContext('loadPeopleStyles()',enabled.context);
  const link=enabled.doc.head.children[0];assert.equal(link.getAttribute('href'),'/people-static/people.css');
  link.fire('load');await styleReady;
  vm.runInContext('clearPeopleView()',enabled.context);assert.equal(enabled.doc.head.children.length,0);
  const doc=document(true),panel=new Element('section');let resolveJSON;
  const context=vm.createContext({document:doc,console,Promise,Map,Set,MutationObserver:class{observe(){}},queueMicrotask:()=>{},
    fetch:async()=>({ok:true,json:()=>new Promise(resolve=>{resolveJSON=resolve;})})});
  vm.runInContext(copySource,context);
  vm.runInContext(liveSource.replace('export function mountLivePeople','function mountLivePeople'),context);
  vm.runInContext(peopleSource.replace(/^import .*;$/gm,'').replace('export function mountPeople','function mountPeople'),context);
  context.panel=panel;
  const view=vm.runInContext('mountPeople(panel,{embedded:true})',context);
  assert(panel.classList.contains('people-root'));
  assert(!panel.children.some(child=>child.tagName==='header'));
  await new Promise(setImmediate);
  assert.equal(typeof resolveJSON,'function');
  view.destroy();assert(!panel.classList.contains('people-root'));assert.equal(panel.children.length,0);
  resolveJSON({people:[],clusters:[],samples:[],voice_links:[],revision:0,undo_available:false});
  await view.ready;assert.equal(panel.children.length,0);
  console.log(JSON.stringify({absent_people_nav:true,enabled_people_nav:true,outside_styles_before_open:0,
    scoped_style_while_open:true,outside_styles_after_leave:0,root_marker_removed:true,late_refresh_ignored:true,
    embedded_heading_omitted:true}));
}
run().catch(error=>{console.error(error);process.exitCode=1;});
