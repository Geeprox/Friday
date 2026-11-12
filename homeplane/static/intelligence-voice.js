export function mountVoiceInput(container,input,{api=async path=>{
  const response=await fetch(path,{cache:'no-store'});if(!response.ok)throw new Error('unavailable');return response.json();
}}={}) {
  const control=document.createElement('button');control.type='button';control.textContent='语音输入';control.hidden=true;
  const status=document.createElement('span');status.setAttribute('role','status');status.setAttribute('aria-live','polite');
  container.append(control,status);
  let disposed=false,session=null;
  api('/api/settings/intelligence').then(report=>{if(!disposed)control.hidden=report?.config?.cloud_voice!==true||report?.config?.provider==='local'||!navigator.mediaDevices?.getUserMedia||!globalThis.AudioWorkletNode;}).catch(()=>{});
  function clean(current,close=true) {
    if(!current)return;clearTimeout(current.timer);clearTimeout(current.flushTimer);
    current.stream?.getTracks().forEach(track=>track.stop());current.source?.disconnect();current.processor?.disconnect();
    void current.context?.close().catch(()=>{});
    if(close&&current.socket?.readyState<2)current.socket.close();
    if(session===current){session=null;input.readOnly=current.readOnlyBefore;control.textContent='语音输入';}
  }
  async function capture(current) {
    let stream;
    try {
      stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true},video:false});
      if(disposed||session!==current){stream.getTracks().forEach(track=>track.stop());return;}
      current.stream=stream;current.context=new AudioContext();await current.context.audioWorklet.addModule('/static/intelligence-voice-worklet.js');
      if(disposed||session!==current){clean(current);return;}
      current.source=current.context.createMediaStreamSource(stream);current.processor=new AudioWorkletNode(current.context,'friday-voice-input');
      const silent=current.context.createGain();silent.gain.value=0;
      current.processor.port.onmessage=message=>{
        if(session!==current||current.socket.readyState!==WebSocket.OPEN)return;
        if(message.data instanceof ArrayBuffer)current.socket.send(message.data);
        else if(message.data?.event==='flushed'){clearTimeout(current.flushTimer);current.socket.send('{"event":"finish"}');current.source.disconnect();current.processor.disconnect();void current.context.close();}
      };
      current.source.connect(current.processor);current.processor.connect(silent);silent.connect(current.context.destination);await current.context.resume();
      current.recording=true;clearTimeout(current.timer);control.textContent='结束录音';status.textContent='正在听，最长 8 秒。';current.timer=setTimeout(()=>finish(current),8000);
    }catch(_){stream?.getTracks().forEach(track=>track.stop());if(session===current){status.textContent='暂时无法使用麦克风，可以直接输入文字。';clean(current);}}
  }
  function finish(current) {
    if(session!==current||!current.recording)return;
    current.recording=false;clearTimeout(current.timer);current.stream?.getTracks().forEach(track=>track.stop());
    control.textContent='取消语音';status.textContent='正在转写…';current.processor.port.postMessage('flush');
    current.flushTimer=setTimeout(()=>{if(session===current){status.textContent='语音输入暂不可用，可以直接输入文字。';clean(current);}},15000);
  }
  function click() {
    if(session){if(session.recording)finish(session);else{status.textContent='语音输入已取消。';clean(session);}return;}
    if(input.disabled||input.readOnly){status.textContent='当前输入暂不可编辑。';return;}
    const current={recording:false,initial:input.value,start:input.selectionStart??input.value.length,end:input.selectionEnd??input.value.length,readOnlyBefore:input.readOnly};session=current;input.readOnly=true;control.textContent='取消语音';status.textContent='正在准备语音输入…';
    const url=new URL('/api/settings/intelligence/voice',location.href);url.protocol=url.protocol==='https:'?'wss:':'ws:';
    current.socket=new WebSocket(url);
    current.socket.onmessage=message=>{
      if(disposed||session!==current)return;
      let value;try{value=JSON.parse(message.data);}catch(_){status.textContent='语音输入暂不可用。';clean(current);return;}
      if(value.event==='ready')void capture(current);
      else if(value.event==='transcript'){
        if(typeof value.text!=='string'||value.text.length>1024){status.textContent='这段语音过长，请缩短后重试。';clean(current);return;}
        const combined=current.initial.slice(0,current.start)+value.text+current.initial.slice(current.end);
        if(input.value!==current.initial){status.textContent='输入内容已变化，请重新使用语音输入。';clean(current);return;}
        if(combined.length>1024){status.textContent='输入内容过长，请缩短后重试。';clean(current);return;}
        input.value=combined;input.dispatchEvent(new Event('input',{bubbles:true}));status.textContent='已转写，请核对后发送。';clean(current);input.focus();input.setSelectionRange?.(current.start+value.text.length,current.start+value.text.length);
      }else if(value.event==='unavailable'){status.textContent='语音输入暂不可用，可以直接输入文字。';clean(current);}
    };
    current.socket.onerror=()=>{if(session===current){status.textContent='语音连接暂不可用，可以直接输入文字。';clean(current);}};
    current.socket.onclose=()=>{if(session===current){status.textContent='语音输入已结束，可以继续输入文字。';clean(current,false);}};
    current.timer=setTimeout(()=>{if(session===current){status.textContent='语音输入超时，可以直接输入文字。';clean(current);}},25000);
  }
  const hidden=()=>{if(document.hidden&&session){status.textContent='已停止语音输入。';clean(session);}};
  const cancel=()=>{if(session){status.textContent='已停止语音输入。';clean(session);}};
  const escape=event=>{if(event.key==='Escape'&&session){event.preventDefault();event.stopPropagation();cancel();}};
  const form=input.closest?.('form');form?.addEventListener('submit',cancel,true);
  input.addEventListener('keydown',escape);control.addEventListener('keydown',escape);
  control.addEventListener('click',click);document.addEventListener('visibilitychange',hidden);
  return {cancel,dispose(){disposed=true;clean(session);form?.removeEventListener('submit',cancel,true);input.removeEventListener('keydown',escape);control.removeEventListener('keydown',escape);control.removeEventListener('click',click);document.removeEventListener('visibilitychange',hidden);control.remove();status.remove();}};
}
