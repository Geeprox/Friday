const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.textContent='';this.value='';this.disabled=false;}
 setAttribute(k,v){this.attrs[k]=String(v);if(k==='value')this.value=String(v);}
 append(...nodes){for(const n of nodes){this.children.push(n);n.parent=this;}if(this.tagName==='select'&&!this.value&&this.children.length)this.value=this.children[0].value;}
 replaceChildren(...nodes){for(const n of this.children)n.parent=null;this.children=[];this.append(...nodes);}
 remove(){if(this.parent){this.parent.children=this.parent.children.filter(n=>n!==this);this.parent=null;}}
 get isConnected(){return this.root||!!this.parent?.isConnected;}
}
(async()=>{
 const root=new Element('main'),head=new Element('head');root.root=head.root=true;
 const all=n=>[n,...n.children.flatMap(all)],find=(tag,text)=>all(root).find(n=>n.tagName===tag&&n.textContent===text),label=text=>all(root).find(n=>n.attrs['aria-label']===text);
 const base='/api/embodied/speech-output',calls=[];let current={output:'panel',quiet:true,homepod:{configured:false}},found=[];
 const api=async(path,body)=>{calls.push({path,body});if(path===base&&body){current={...current,output:body.output,homepod:{configured:true,name:'客厅 HomePod'}};return current;}if(path===base)return current;if(path.startsWith(base+'/devices'))return {devices:found};if(path===base+'/test')return {status:'accepted'};if(path===base+'/probe')return {status:'ready'};throw Error(path);};
 const document={head,createElement:t=>new Element(t),getElementById:id=>all(head).find(n=>n.attrs.id===id)};
 const context=vm.createContext({document,URLSearchParams,console});context.root=root;context.options={api};
 vm.runInContext(fs.readFileSync('homeplane/static/speech-output.js','utf8').replace('export function mountSpeechOutput','function mountSpeechOutput'),context);
 let owner=vm.runInContext('mountSpeechOutput(root,options)',context);const settle=()=>new Promise(setImmediate);await settle();
 assert.equal(calls.length,1,'opening settings must not discover, connect or play');
 assert.ok(find('button','试听播报').disabled);
 const output=label('播报到');output.value='homepod';await output.onchange();await settle();
 assert.equal(output.value,'panel','a missing HomePod must not appear selected');assert.equal(calls.filter(c=>c.body).length,0);
 found=[{name:'客厅 HomePod',identifier:'POD-01',homepod:true}];await find('button','查找 HomePod').onclick();
 await find('button','客厅 HomePod').onclick();await settle();
 assert.equal(calls.find(c=>c.body)?.body.identifier,'POD-01');assert.equal(current.output,'homepod');
 assert.ok(find('button','试听播报').disabled,'choosing a speaker must not turn quiet mode off');
 assert.equal(calls.filter(c=>c.path===base+'/test').length,0);
 await find('button','检查连接').onclick();assert.ok(all(root).some(n=>n.textContent==='HomePod 已连接，可以播报。'));
 owner.dispose();current.quiet=false;owner=vm.runInContext('mountSpeechOutput(root,options)',context);await settle();
 assert.equal(head.children.length,1,'stylesheet is inserted only once');
 assert.equal(find('button','试听播报').disabled,false);await find('button','试听播报').onclick();
 assert.equal(calls.filter(c=>c.path===base+'/test').length,1);
 assert.ok(calls.every(c=>c.path.startsWith(base)),'speech settings never restart cameras or change a system audio route');
 owner.dispose();assert.equal(root.children.length,0);
 console.log('Speech output: silent settings, actual receiver selection, preserved quiet mode, explicit test and camera-independent lifecycle: OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
