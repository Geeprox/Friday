/* Inert 2D command recorder; no browser, media or household I/O. */
const fs=require('node:fs'),vm=require('node:vm');
function scene(reduced=false,{pending=[]}={}){
 const calls=[],stack=[],images=new Map(),motion={matches:reduced};let translation=[0,0],rotation=0;
 const c=new Proxy({}, {get(target,key){
  if(key in target)return target[key];
  if(key==='save')return()=>stack.push({translation:[...translation],rotation});
  if(key==='restore')return()=>{({translation,rotation}=stack.pop());};
  if(key==='translate')return(x,y)=>{translation=[translation[0]+x,translation[1]+y];};
  if(key==='rotate')return angle=>{rotation+=angle;};
  if(key==='drawImage')return(im,...args)=>{
   if(args.length!==8)return;
   const name=new URL(im.url).pathname.split('/').at(-1);
   if(name==='tennis-near-v1.png'||name==='tennis-far-v1.png'){
    const frame=Math.round(args[0]/(im.naturalWidth/4))+Math.round(args[1]/(im.naturalHeight/3))*4;
    calls.push(['player',name.includes('near')?0:1,...translation,rotation,frame]);
   }else if(name==='summer-props.png'){
    if(args[0]===800&&args[1]===400)calls.push(['ball',...translation]);
    if(args[1]===400&&[0,400].includes(args[0]))calls.push(['player',args[0]/400,...translation,rotation,0]);
   }
  };
  if(key==='ellipse')return(...args)=>calls.push(['ellipse',...args]);
  if(key==='fillText')return(...args)=>calls.push(['text',...args]);
  if(key==='measureText')return label=>({width:[...label].length*parseFloat(target.font.split(' ')[1])*.7});
  return()=>{};
 }});
 class Image{
  constructor(){this.complete=true;this.naturalWidth=1600;this.naturalHeight=800;}
  set src(value){this.url=value;const name=new URL(value).pathname.split('/').at(-1);images.set(name,this);this.name=name;
   this.complete=!pending.includes(name);this.naturalWidth=this.complete?(name.startsWith('tennis-')?1448:1600):0;this.naturalHeight=name.startsWith('tennis-')?1086:800;}
  get src(){return this.url;}
 }
 const context=vm.createContext({Image,URL,matchMedia:()=>motion});
 vm.runInContext(fs.readFileSync('homeplane/embodied/static/game-scenes.js','utf8'),context);
 const api=context.createGameScenery(c,{base:'https://example.invalid/art/'});
 return{...api,images,motion,
  finish(name,error=false){const image=images.get(name);if(!image)throw Error('asset_was_not_requested: '+name);
   image.complete=true;image.naturalWidth=error?0:name.startsWith('tennis-')?1448:1600;(error?image.onerror:image.onload)?.();},
  paint(g,at){calls.length=0;api.tennis(g,at);return structuredClone(calls);}};
}
module.exports={scene};
