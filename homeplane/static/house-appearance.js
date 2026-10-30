// Friday presentation semantics. Imported coordinates and control IDs stay intact.
export const MODEL_SCALE=16;
export const modelPoint=p=>[(.5-p[0])*MODEL_SCALE,(p[1]-.5)*MODEL_SCALE];
export const sourcePoint=p=>[.5-p[0]/MODEL_SCALE,p[1]/MODEL_SCALE+.5];
export function placedDevice(devices,id){
 return devices.find(d=>d.id===id||d.bindings?.some(b=>b.source==='xiaomi-identity'&&(b.external_id===id||/^\d+$/.test(id)&&b.external_id.split('_').pop()===id)));
}
export function powerOperation(control){
 return control?.capabilities?.find(c=>['light.set','switch.set','fan.set'].includes(c.name))?.name||null;
}
export function directPower(device){
 return device?.controls?.length===1&&powerOperation(device.controls[0])?device.controls[0]:null;
}
// Room anchors are grouped controls, not invented fixture coordinates.
export function roomQuickControls(devices,room){
 const list=devices.filter(d=>d.room===room),result=[];
 const lamps=list.flatMap(d=>(d.controls||[]).filter(c=>c.presentation?.emits_light&&powerOperation(c)).map(c=>({device:d,control:c})))
  .sort((a,b)=>Number(b.control.available)-Number(a.control.available)||Number(b.control.presentation.lighting_role==='main')-Number(a.control.presentation.lighting_role==='main'));
 if(lamps[0])result.push({device:lamps[0].device.id,control:lamps[0].control.id,kind:'light',name:lamps[0].control.name,on:lamps[0].control.value==='on',available:lamps[0].control.available});
 for(const kind of ['cover','climate','camera','lock']){
  const d=list.find(d=>d.kind===kind);
  if(d)result.push({device:d.id,kind,name:d.name,on:d.controls.some(c=>c.available&&!['off','closed','unavailable','unknown'].includes(c.value)),available:d.available!==false});
 }
 return result;
}
// Resolve projected room annotations without changing source coordinates.
export function arrangeRoomControls(items,width,height,topInset=54){
 const rows=items.map(p=>({...p,x:p.anchorX,y:p.anchorY}));
 const clamp=p=>{p.x=Math.max(p.w/2+12,Math.min(width-p.w/2-12,p.x));p.y=Math.max(p.h/2+topInset,Math.min(height-p.h/2-36,p.y));};
 for(let pass=0;pass<32;pass++){
  for(const p of rows){if(pass<20){p.x+=(p.anchorX-p.x)*.045;p.y+=(p.anchorY-p.y)*.045;}clamp(p);}
  for(let i=0;i<rows.length;i++)for(let j=i+1;j<rows.length;j++){
   const a=rows[i],b=rows[j],dx=b.x-a.x,dy=b.y-a.y,ox=(a.w+b.w)/2+8-Math.abs(dx),oy=(a.h+b.h)/2+7-Math.abs(dy);
   if(ox>0&&oy>0){if(ox<oy){const shift=(ox+.5)/2*(dx>=0?1:-1);a.x-=shift;b.x+=shift;}else{const shift=(oy+.5)/2*(dy>=0?1:-1);a.y-=shift;b.y+=shift;}clamp(a);clamp(b);}
  }
 }
 const overlap=(a,b)=>Math.abs(a.x-b.x)<(a.w+b.w)/2+5&&Math.abs(a.y-b.y)<(a.h+b.h)/2+5;
 if(rows.some((p,i)=>rows.slice(i+1).some(q=>overlap(p,q)))){
  const packed=[];
  for(const p of rows){
   let best=null,score=Infinity;
   for(let y=p.h/2+topInset;y<=height-p.h/2-36;y+=5)for(let x=p.w/2+12;x<=width-p.w/2-12;x+=5){
    const candidate={...p,x,y};if(packed.some(q=>overlap(candidate,q)))continue;
    const cost=(x-p.anchorX)**2+(y-p.anchorY)**2;if(cost<score){best=candidate;score=cost;}
   }
   packed.push(best||p);
  }
  return packed;
 }
 return rows;
}
export function roomLighting(devices,room){
 const result={main:0,strip:0,downlight:0,spotlight:0,accent:0,kelvin:3000,lights:[]};let temperature=0,weight=0;
 for(const device of devices.filter(d=>d.room===room))for(const c of device.controls||[]){
  const role=c.presentation?.lighting_role;
  if(!c.available||c.value!=='on'||!c.presentation?.emits_light||!(role in result))continue;
  const raw=Number(c.attributes?.brightness),level=Number.isFinite(raw)&&c.attributes?.brightness!=null?Math.max(0,Math.min(1,raw/255)):1;
  result[role]=1-(1-result[role])*(1-level);
  let k=Number(c.attributes?.color_temp_kelvin);if(!k&&c.attributes?.color_temp)k=1e6/Number(c.attributes.color_temp);
  if(k>=1800&&k<=9000){temperature+=k*level;weight+=level;}
  result.lights.push({device:device.id,control:c.id,role,level});
 }
 if(weight)result.kelvin=temperature/weight;
 return result;
}
