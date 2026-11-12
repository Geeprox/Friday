const fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const source=fs.readFileSync('homeplane/static/app.js','utf8');
const navigation=source.slice(source.indexOf('function navigation()'),source.indexOf('function expireSnapshotStates()'));
const all=node=>[node,...node.children.flatMap(all)];

function setup(page='Games'){
 const document={activeElement:null};
 class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.dataset={};this.events={};this.open=false;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(){this.children=[];}
  setAttribute(k,v){this.attrs[k]=String(v);if(k==='class')this.className=v;if(k==='open')this.open=true;if(k.startsWith('data-'))this.dataset[k.slice(5)]=String(v);}
  getAttribute(k){return this.attrs[k]??null;}
  addEventListener(name,fn){this.events[name]=fn;}
  contains(node){return all(this).includes(node);}
  focus(){document.activeElement=this;}
  querySelector(selector){return all(this).find(n=>selector==='summary'?n.tagName==='summary':selector==='button'?n.tagName==='button':selector==='.navigation-secondary'?n.className==='navigation-secondary':selector==='[aria-current="page"]'?n.attrs['aria-current']==='page':selector==='button[aria-current="page"]'?n.tagName==='button'&&n.attrs['aria-current']==='page':false);}
 }
 const nav=new Element('nav'),utility=new Element('nav'),developer={checked:false};let change;
 const media={matches:false,addEventListener:(event,callback)=>{assert.equal(event,'change');change=callback;}};
 const el=(tag,text,attrs={})=>{const n=new Element(tag);n.textContent=text;for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
 const labels=Object.fromEntries(['Home','Devices','Cameras','Automations','Activity','Safety','People','Files','Interaction','Games','Settings','System','Developer'].map(x=>[x,[x,'','icon']]));
 const scope=vm.createContext({document,matchMedia:()=>media,page,peopleAvailable:true,filesAvailable:true,
  pageInfo:labels,$:id=>id==='navigation'?nav:id==='utility-navigation'?utility:developer,el,icon:()=>el('span'),add:(parent,...nodes)=>{parent.append(...nodes);return parent;},button:(text,fn,attrs)=>el('button',text,attrs),go(){}});
 vm.runInContext(navigation,scope);
 return{scope,nav,media,document,secondary:()=>nav.querySelector('.navigation-secondary'),resize:matches=>{media.matches=matches;assert.equal(typeof change,'function','viewport changes must update existing navigation');change({matches});}};
}

test('desktop open state is not mistaken for a mobile user choice',()=>{
 const s=setup();s.scope.navigation();assert.equal(s.secondary().open,true);
 s.media.matches=true;s.scope.navigation();assert.equal(s.secondary().open,false);
});

test('crossing a breakpoint updates the existing menu without rebuilding the page',()=>{
 const s=setup();s.scope.navigation();const secondary=s.secondary(),home=s.nav.children[0];
 s.resize(true);assert.equal(secondary.open,false);assert.equal(s.nav.children[0],home);
 secondary.open=true;s.scope.navigation();assert.equal(s.secondary().open,true,'mobile refresh preserves an explicit open menu');
 s.resize(false);assert.equal(s.secondary().open,true);
});

test('Escape closes a menu created on desktop and resized to mobile',()=>{
 const s=setup();s.scope.navigation();const secondary=s.secondary();s.resize(true);
 secondary.open=true;secondary.events.keydown({key:'Escape'});
 assert.equal(secondary.open,false);assert.equal(s.document.activeElement,secondary.querySelector('summary'));
});

test('focus moves to a visible navigation control when compact mode hides its previous item',()=>{
 for(const page of ['Games','People']){
  const s=setup(page);s.scope.navigation();const secondary=s.secondary();secondary.querySelector('button').focus();
  s.resize(true);assert.equal(s.document.activeElement,secondary.querySelector('summary'));
  s.resize(false);assert.equal(s.document.activeElement.tagName,'button');
  assert.equal(s.document.activeElement.getAttribute('data-page'),page);
 }
});
