const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const traces=JSON.parse(fs.readFileSync(0,'utf8')),context=vm.createContext({});vm.runInContext(fs.readFileSync('homeplane/embodied/static/game-sound.js','utf8'),context);
const results=[];
for(const trace of traces){let before=null;const cues=[];for(const packet of trace.packets){const after=context.gameSoundFrame(packet);const cue=context.gameSoundCue(before,after);if(cue)cues.push(cue);before=after;}assert.deepEqual(cues,trace.expected,trace.name);results.push({name:trace.name,packets:trace.packets.length,cues});}
process.stdout.write(JSON.stringify(results));
