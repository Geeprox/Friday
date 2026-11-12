/* 舞台只画当前对局的准星和命中回执。屏幕不会自行判断得分。 */
const beamUnit=n=>Number.isFinite(n)&&n>=0&&n<=1;
const beamPoint=value=>Array.isArray(value)&&value.length===2&&value.every(beamUnit)?value:null;
const BEAM_HIT_MS=660;

function beamView(game){
 const g=game?.mode==='beam'?game.beam:null,valid=Number.isInteger(g?.hits)&&g.hits>=0&&g.hits<=6;
 const steps=g?.calibration,step=Number.isInteger(steps?.step)&&steps.step>=0&&steps.step<=5?steps.step:0;
 const phase=['waiting','calibrate','ready','aim','lock','release','complete'].includes(g?.phase)?g.phase:'waiting';
 const event=g?.event,aim=phase==='aim'||phase==='lock'?beamPoint(g?.aim):null;
 return{phase,hits:valid?g.hits:0,total:6,target:beamPoint(g?.target),aim,charge:aim&&beamUnit(g?.charge)?g.charge:0,
  radius:beamUnit(g?.radius)&&g.radius>=.05&&g.radius<=.2?g.radius:.108,
  frameAge:Number.isFinite(g?.frame_age)&&g.frame_age>=0?g.frame_age*1000:Infinity,
  step,mark:step<5?beamPoint(steps?.mark):null,hold:beamUnit(steps?.hold)?steps.hold:0,retry:steps?.retry===true,
  event:valid&&event?.kind==='hit'&&event.sequence===g.hits&&beamPoint(event.point)&&
   Number.isFinite(event.age)&&event.age>=0&&event.age<=.4?event:null};
}

function createBeamStage(c,{base,onChange,reducedMotion=()=>false}){
 const art=[new Image(),new Image()],names=['beam-glade.webp','beam-fluff.webp'];
 let loading=false,failed=false,packet=null,hit=null;
 const ready=()=>art.every(image=>image.complete&&image.naturalWidth>0);
 function load(retry=false){
  if(loading&&!retry)return;
  loading=true;failed=false;
  for(const [i,image] of art.entries()){
   image.onload=onChange;image.onerror=()=>{failed=true;onChange();};
   image.src=new URL(names[i]+(retry?'?retry='+Date.now():''),base).href;
  }
 }
 function forget(){packet=null;hit=null;}
 function receive(game,at,epoch,live){
  if(game?.mode!=='beam'||!live||game.manually_paused||game.source_recovery?.required||
     !['playing','complete'].includes(game.status)){forget();return;}
  const view=beamView(game),key=epoch+':'+game.control_context+':'+game.round_id;
  const continuous=packet?.key===key&&at>=packet.at&&at-packet.at<=850;
  if(!continuous)hit=null;
  if(continuous&&view.event&&view.hits===packet.view.hits+1){
   hit={key,sequence:view.event.sequence,point:view.event.point,started:at-view.event.age*1000};
  }
  packet={key,at,view};
 }
 function impact(at){
  return hit&&at>=hit.started&&at-hit.started<BEAM_HIT_MS?hit:null;
 }
 function expire(at){
  if(!hit||impact(at))return false;
  hit=null;return true;
 }
 function ellipse(x,y,rx,ry,fill){
  c.beginPath();c.ellipse(x,y,rx,ry,0,0,2*Math.PI);c.fillStyle=fill;c.fill();
 }
 function arc(x,y,r,from,to,width,color){
  c.beginPath();c.arc(x,y,r,from,to);c.strokeStyle=color;c.lineWidth=width;c.lineCap='round';c.stroke();
 }
 function star(x,y,r,color){
  c.beginPath();c.moveTo(x,y-r);c.quadraticCurveTo(x+r*.15,y-r*.18,x+r,y);
  c.quadraticCurveTo(x+r*.15,y+r*.18,x,y+r);c.quadraticCurveTo(x-r*.15,y+r*.18,x-r,y);
  c.quadraticCurveTo(x-r*.15,y-r*.18,x,y-r);c.fillStyle=color;c.fill();
 }
 function marker(mark,hold){
  const [x,y]=[mark[0]*1600,mark[1]*900],start=-Math.PI/2;
  ellipse(x,y,34,34,'#365747');
  star(x,y,22,'#fbf8e5');
  if(hold>0)arc(x,y,47,start,start+2*Math.PI*hold,8,'#fff4bb');
 }
 function creature(x,y,size,shift=0){
  ellipse(x,y+size*.39,size*.28,size*.075,'#25342642');
  c.save();c.shadowColor='#21352256';c.shadowOffsetY=10;c.shadowBlur=21;
  c.drawImage(art[1],x-size*.5,y-size*.52+shift,size,size);c.restore();
 }
 function reticle(aim,charge){
  const x=1600*aim[0],y=900*aim[1],turn=2*Math.PI,start=-Math.PI/2;
  arc(x,y,36,0,turn,9,'#1c382c');
  arc(x,y,36,0,turn,4,'#fffdf0');
  ellipse(x,y,5,5,'#fffdf0');
  if(charge>0){
   arc(x,y,52,0,turn,10,'#214033');
   arc(x,y,52,start,start+turn*charge,8,'#fff0aa');
  }
 }
 function flash(effect,view,at,still){
  const x=effect.point[0]*1600,y=effect.point[1]*900,age=(at-effect.started)/BEAM_HIT_MS;
  const eased=1-Math.pow(1-age,3);
  if(!still&&age<.31){
   const reach=Math.min(1,age/.12),alpha=Math.max(0,1-(age-.07)/.24);
   c.save();c.globalAlpha=Math.min(1,alpha);
   for(const [color,width] of [['#e5a23e',28],['#ffd880',16],['#fffbe0',6]]){
    c.beginPath();c.moveTo(800,886);c.lineTo(800+(x-800)*reach,886+(y-886)*reach);
    c.lineCap='round';c.strokeStyle=color;c.lineWidth=width;c.stroke();
   }
   c.restore();
  }
  if(!still){
   c.save();c.globalAlpha=1-eased;
   creature(x,y,view.radius*900*(2.35+.28*eased),-48*eased);
   c.restore();
   c.save();c.globalAlpha=(1-age)*.92;
   arc(x,y,40+110*eased,0,2*Math.PI,7,'#fff3ac');
   for(let i=0;i<8;i++){
    const theta=i*Math.PI*.25;
    star(x+Math.cos(theta)*(40+120*eased),y+Math.sin(theta)*(40+120*eased),
     11*(1-.55*age),i%2?'#fff7dc':'#ffd883');
   }
   c.restore();
  }
  c.save();c.globalAlpha=still?1:Math.min(1,(1-age)*2.3);
  ellipse(x,y,still?42:32,still?42:32,'#3c664e');
  star(x,y,still?30:24,'#fff6cd');
  c.restore();
 }
 function draw(g,at,width,height,live){
  if(!ready())return;
  const still=reducedMotion(),cover=Math.max(width/art[0].naturalWidth,height/art[0].naturalHeight);
  const dw=art[0].naturalWidth*cover,dh=art[0].naturalHeight*cover;
  c.setTransform(1,0,0,1,0,0);c.drawImage(art[0],(width-dw)/2,(height-dh)/2,dw,dh);
  const scale=Math.min(width/1600,height/900);
  c.setTransform(scale,0,0,scale,(width-1600*scale)/2,(height-900*scale)/2);
  const view=beamView(g),usable=live&&g.status==='playing'&&!g.manually_paused&&!g.source_recovery?.required;
  const fresh=usable&&packet&&at>=packet.at&&at-packet.at+packet.view.frameAge<=600;
  const effect=live&&!g.manually_paused&&!g.source_recovery?.required?impact(at):null;
  if(effect)flash(effect,view,at,still);
  else if(view.target&&g.status!=='complete'){
   const bob=fresh&&!still?Math.sin(at/390)*7:0;
   creature(view.target[0]*1600,view.target[1]*900,view.radius*900*2.35,bob);
  }
  if(fresh&&view.phase==='calibrate'&&view.mark)marker(view.mark,view.hold);
  if(fresh&&view.aim)reticle(view.aim,view.charge);
 }
 return{prepare:()=>load(),reload:()=>load(true),ready,failed:()=>failed,receive,forget,impact,expire,draw};
}

if(typeof module==='object'&&module.exports)module.exports={createBeamStage,beamView};
