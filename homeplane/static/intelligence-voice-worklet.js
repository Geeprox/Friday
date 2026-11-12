class FridayVoiceInput extends AudioWorkletProcessor {
  constructor() {
    super();this.ratio=sampleRate/24000;this.weight=0;this.sum=0;this.output=new Int16Array(2048);this.count=0;
    this.port.onmessage=message=>{if(message.data==='flush'){this.flush();this.port.postMessage({event:'flushed'});}};
  }
  flush(){if(this.count){const data=this.output.slice(0,this.count);this.port.postMessage(data.buffer,[data.buffer]);this.count=0;}}
  process(inputs) {
    const input=inputs[0]?.[0];if(!input)return true;
    for(const sample of input){let remaining=1;
      while(remaining>0){const part=Math.min(remaining,this.ratio-this.weight);this.sum+=sample*part;this.weight+=part;remaining-=part;
        if(this.weight>=this.ratio-1e-9){const value=Math.max(-1,Math.min(1,this.sum/this.ratio));this.output[this.count++]=Math.round(value*(value<0?32768:32767));this.sum=0;this.weight=0;if(this.count===this.output.length)this.flush();}
      }
    }return true;
  }
}
registerProcessor('friday-voice-input',FridayVoiceInput);
