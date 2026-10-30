const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
 const m=await import('data:text/javascript;base64,'+fs.readFileSync('homeplane/static/house-appearance.js').toString('base64'));
 for(const point of [[0,0],[1,1],[.2,.7]]){
  const recovered=m.sourcePoint(m.modelPoint(point));
  point.forEach((x,i)=>assert.ok(Math.abs(x-recovered[i])<1e-9));
 }
 assert.ok(m.modelPoint([.2,.5])[0]>m.modelPoint([.8,.5])[0]);
 const make=(id,role,on=true)=>({id,room:'客厅',bindings:[{source:'xiaomi-identity',external_id:'cn_'+id}],controls:[{id:'c_'+id,available:true,value:on?'on':'off',attributes:{},presentation:{emits_light:Boolean(role),lighting_role:role}}]});
 const supply=make('1',null),main=make('2','main'),strip=make('3','strip'),offline=make('4','downlight');offline.controls[0].available=false;
 assert.deepEqual(m.roomLighting([supply],'客厅').lights,[]);
 const lit=m.roomLighting([supply,main,strip,offline],'客厅');
 assert.equal(lit.main,1);assert.equal(lit.strip,1);assert.equal(lit.downlight,0);assert.equal(lit.lights.length,2);
 assert.equal(m.placedDevice([main],'cn_2').id,'2');assert.equal(m.placedDevice([main],'2').id,'2');
 const lamp={...main,controls:[{...main.controls[0],name:'客厅主灯',kind:'light',capabilities:[{name:'light.set'}]}]};
 const offlineLamp={...lamp,id:'offline',controls:[{...lamp.controls[0],id:'offline-light',available:false}]};
 const lock={id:'lock',room:'客厅',kind:'lock',controls:[{id:'lock-control',capabilities:[{name:'lock.set'}]}]};
 assert.equal(m.directPower(lamp).id,'c_2');assert.equal(m.directPower(lock),null);
 assert.equal(m.directPower({...lamp,controls:[...lamp.controls,...lamp.controls]}),null);
 const quick=m.roomQuickControls([supply,offlineLamp,lamp,lock],'客厅');
 assert.equal(quick.length,2);assert.equal(quick[0].control,'c_2');assert.equal(quick[0].available,true);
 assert.equal(quick[1].device,'lock');assert.equal(quick[1].control,undefined);
 const bounds=m.arrangeRoomControls(Array.from({length:6},(_,i)=>({anchorX:300+(i%3)*40,anchorY:170+Math.floor(i/3)*40,w:145,h:70})),775,340);
 for(const p of bounds){assert.ok(p.x>=p.w/2+12&&p.x<=775-p.w/2-12);assert.ok(p.y>=p.h/2+54&&p.y<=340-p.h/2-36);}
 for(let i=0;i<bounds.length;i++)for(let j=i+1;j<bounds.length;j++)assert.ok(Math.abs(bounds[i].x-bounds[j].x)>144||Math.abs(bounds[i].y-bounds[j].y)>69,'room controls overlap');
 console.log('house appearance: coordinate inverse, supply exclusion, roles, offline state, stable placement OK');
})();
