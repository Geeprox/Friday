const SIZE=512,HOP=160,RATE=8000,BANDS=24;
const edges=Array.from({length:BANDS+1},(_,i)=>Math.round(100*Math.pow(36,i/BANDS)*SIZE/RATE));
export class FeatureStream{
 constructor(rate,emit){this.rate=rate;this.emit=emit;this.samples=new Float32Array(SIZE);this.at=0;this.count=0;this.phase=0;this.sum=0;this.group=0;this.total=0;this.power=0;}
 push(input){for(const sample of input){this.sum+=sample;this.group++;this.phase+=RATE;if(this.phase>=this.rate){this.phase-=this.rate;const v=this.sum/this.group;this.sum=0;this.group=0;this.samples[this.at]=v;this.at=(this.at+1)%SIZE;this.count++;this.total++;if(this.count>=SIZE&&(this.count-SIZE)%HOP===0)this.frame();}}}
 frame(){const real=new Float64Array(SIZE),imag=new Float64Array(SIZE);let energy=0;
  for(let i=0;i<SIZE;i++){const v=this.samples[(this.at+i)%SIZE];energy+=v*v;real[i]=v*(.5-.5*Math.cos(2*Math.PI*i/(SIZE-1)));}
  for(let i=1,j=0;i<SIZE;i++){let bit=SIZE>>1;for(;j&bit;bit>>=1)j^=bit;j^=bit;if(i<j){const t=real[i];real[i]=real[j];real[j]=t;}}
  for(let n=2;n<=SIZE;n<<=1){const angle=-2*Math.PI/n;for(let base=0;base<SIZE;base+=n)for(let i=0;i<n/2;i++){const c=Math.cos(angle*i),s=Math.sin(angle*i),a=base+i,b=a+n/2,r=real[b]*c-imag[b]*s,v=real[b]*s+imag[b]*c;real[b]=real[a]-r;imag[b]=imag[a]-v;real[a]+=r;imag[a]+=v;}}
  const bands=new Float32Array(BANDS);for(let b=0;b<BANDS;b++){let sum=0;for(let i=edges[b];i<Math.max(edges[b]+1,edges[b+1]);i++)sum+=real[i]*real[i]+imag[i]*imag[i];bands[b]=Math.log(1e-10+sum/Math.max(1,edges[b+1]-edges[b]));}
  this.emit({time:(this.total-SIZE/2)/RATE,bands,rms:Math.sqrt(energy/SIZE)});
 }
 clear(){this.samples.fill(0);this.sum=this.group=0;}
}
function* referenceSteps(buffer,start,end){
 const rows=[],first=Math.max(0,Math.floor(start*buffer.sampleRate)),last=Math.min(buffer.length,Math.floor(end*buffer.sampleRate));
 const stream=new FeatureStream(buffer.sampleRate,row=>rows.push({...row,time:row.time+first/buffer.sampleRate}));
 const a=buffer.getChannelData(0),b=buffer.numberOfChannels>1?buffer.getChannelData(1):a,block=new Float32Array(4096);let blocks=0,leftEnergy=0,rightEnergy=0;
 // Pick one stable channel over this excerpt: averaging stereo can erase a
 // phase-inverted reference, while switching channels per block invents edges.
 for(let start=first;start<last;start+=block.length){const stop=Math.min(last,start+block.length);for(let p=start;p<stop;p++){leftEnergy+=a[p]*a[p];rightEnergy+=b[p]*b[p];}if(++blocks%4===0)yield;}blocks=0;
 const channel=rightEnergy>leftEnergy?b:a;
 try{for(let p=first;p<last;p+=block.length){const count=Math.min(block.length,last-p);for(let i=0;i<count;i++)block[i]=channel[p+i];stream.push(block.subarray(0,count));if(++blocks%4===0)yield;}return rows;}
 finally{block.fill(0);stream.clear();}
}
function finishSteps(steps){let next;do{next=steps.next();}while(!next.done);return next.value;}
async function finishStepsAsync(steps,{check=()=>{},yieldTask=()=>new Promise(resolve=>setTimeout(resolve,0))}={}){
 try{for(;;){check();const next=steps.next();if(next.done){check();return next.value;}await yieldTask();}}
 finally{steps.return();}
}
export function referenceFeatures(buffer,start,end){return finishSteps(referenceSteps(buffer,start,end));}
export function referenceFeaturesAsync(buffer,start,end,options){return finishStepsAsync(referenceSteps(buffer,start,end),options);}
function deltas(rows){return rows.slice(3).map((r,i)=>Array.from(r.bands,(v,b)=>v-rows[i].bands[b]));}
function similarity(reference,recorded,at,start=0,end=recorded.length){let total=0,used=0;for(let b=0;b<BANDS;b++){let xy=0,xx=0,yy=0;for(let i=start;i<end;i++){const x=reference[at+i][b],y=recorded[i][b];xy+=x*y;xx+=x*x;yy+=y*y;}if(xx>1&&yy>1){total+=xy/Math.sqrt(xx*yy);used++;}}return used>=12?total/used:-1;}
function* matchSteps(reference,recorded){
 if(recorded.length<180||reference.length<recorded.length||recorded.filter(r=>r.rms>.002).length<recorded.length*.55)return {ok:false,reason:"quiet"};
 const ref=deltas(reference),mic=deltas(recorded),scores=[];let best=-1,at=-1;
 for(let i=0;i<=ref.length-mic.length;i++){const score=similarity(ref,mic,i);scores.push(score);if(score>best){best=score;at=i;}if(i%16===15)yield;}
 let rival=-1;for(let i=0;i<scores.length;i++)if(Math.abs(i-at)>8)rival=Math.max(rival,scores[i]);
 const middle=Math.floor(mic.length/2);let first=-1,second=-1,firstAt=-1,secondAt=-1;
 for(let i=Math.max(0,at-6);i<=Math.min(scores.length-1,at+6);i++){const a=similarity(ref,mic,i,0,middle),b=similarity(ref,mic,i,middle,mic.length);if(a>first){first=a;firstAt=i;}if(b>second){second=b;secondAt=i;}}
 const ok=best>=.35&&best-rival>=.045&&Math.abs(firstAt-secondAt)<=4&&first>.25&&second>.25;
 return {ok,reason:ok?"matched":"ambiguous",time:reference[Math.max(0,at)].time,score:best,margin:best-rival};
}
export function matchFeatures(reference,recorded){return finishSteps(matchSteps(reference,recorded));}
export function matchFeaturesAsync(reference,recorded,options){return finishStepsAsync(matchSteps(reference,recorded),options);}
