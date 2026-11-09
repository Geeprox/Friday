/* Display availability only. The original game owner decides all progress. */
function createGamePresentation({state,current,visible,assetsReady,send,onChange=()=>{},clock=()=>performance.now(),schedule=setTimeout,cancel=clearTimeout,randomId=()=>{
 const bytes=new Uint8Array(16);globalThis.crypto.getRandomValues(bytes);return [...bytes].map(n=>n.toString(16).padStart(2,'0')).join('');
}}){
 let alive=true,owner=null;
 const scope=g=>g?.presentation?.required===true&&typeof g.control_context==='string'&&Number.isSafeInteger(g.round_id)&&/^[a-f0-9]{32}$/.test(g.presentation.context)?g.control_context+':'+g.round_id+':'+g.selected_mode+':'+g.presentation.context:null;
 function transmit(slot,ready,shown,releasing=false){
  const g=state(),sequence=++slot.sequence,controller=new AbortController();
  const body={expected_context:slot.context,expected_round:slot.round,expected_view:slot.viewContext,mode:slot.mode,view_id:slot.id,sequence,window:g?.presentation?.window||slot.window,visible:shown,ready};
  const op={controller,ready,at:clock(),timer:null};
  op.timer=schedule(()=>{controller.abort();if(!releasing&&alive&&owner===slot&&slot.pending===op){slot.pending=null;slot.accepted=false;slot.failedWindow=body.window;onChange();}},1200);
  if(!releasing){slot.pending=op;slot.sentAt=clock();slot.lastReady=ready;}
  let response;try{response=send(body,{signal:controller.signal,keepalive:releasing});}catch(error){response=Promise.reject(error);}
  return Promise.resolve(response).then(result=>{
   if(releasing||!alive||owner!==slot||slot.pending!==op)return;
   if(result?.accepted!==true||result.view_id!==slot.id||result.sequence!==sequence||result.expected_context!==slot.context||result.expected_round!==slot.round||result.expected_view!==slot.viewContext||result.mode!==slot.mode||result.view_ready!==ready)throw Error('game_view_changed');
   slot.accepted=ready;slot.ackAt=clock();slot.failedWindow=null;onChange();
  }).catch(()=>{
   if(!releasing&&alive&&owner===slot&&slot.pending===op){slot.accepted=false;slot.failedWindow=body.window;onChange();}
  }).finally(()=>{cancel(op.timer);if(slot.pending===op)slot.pending=null;});
 }
 function retire(){
  const old=owner;owner=null;
  if(old){old.pending?.controller.abort();old.accepted=false;void transmit(old,false,false,true);}
 }
 function tick(){
  if(!alive)return;const g=state(),key=scope(g);
  if(!key||!current()||!visible()){retire();return;}
  if(owner?.key!==key)retire();
  if(owner?.failedWindow){if(owner.failedWindow===g.presentation.window)return;retire();}
  if(!owner){let id;try{id=randomId();}catch{return;}if(!/^[a-f0-9]{32}$/.test(id))return;owner={id,key,context:g.control_context,round:g.round_id,viewContext:g.presentation.context,mode:g.selected_mode,window:g.presentation.window,sequence:-1,pending:null,sentAt:-Infinity,ackAt:-Infinity,accepted:false,lastReady:null,failedWindow:null};}
  const slot=owner,ready=Boolean(assetsReady());
  if(slot.pending){if(slot.pending.ready===ready)return;slot.pending.controller.abort();}
  if(ready!==slot.lastReady||clock()-slot.sentAt>=700)void transmit(slot,ready,true);
 }
 return{tick,ready(){const g=state(),key=scope(g);return alive&&(g?.presentation===undefined||g?.presentation?.required===false||Boolean(key&&owner&&owner.key===key&&owner.accepted&&current()&&visible()&&assetsReady()&&0<=clock()-owner.ackAt&&clock()-owner.ackAt<=2000));},release:retire,close(){alive=false;retire();}};
}
