// Feed the original stage a production game packet and return its exact requests.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const {makeClient}=require('./game_stage_harness.cjs'),settle=()=>new Promise(setImmediate);
async function main(){
 const input=JSON.parse(fs.readFileSync(0,'utf8')),calls=[];
 const client=makeClient({meta:{game:input.game},selected:input.game.selected_mode,
  scenery:{receive(){},ready:()=>input.artReady!==false},winterReady:()=>input.artReady!==false,
  fetch:async(path,request)=>{calls.push({path,body:JSON.parse(request.body)});return new Promise(()=>{});}});
 const scope=client.c;scope.document.querySelector=selector=>selector==='#gameTutorial canvas'?{dataset:{motion:'ready'}}:client.node('actions');
 vm.runInContext(fs.readFileSync('homeplane/embodied/static/game-presentation.js','utf8'),scope);
 scope.presentationClient=scope.createGamePresentation({state:()=>scope.meta.game,current:scope.gameViewCurrent,visible:()=>true,assetsReady:scope.gameSceneReady,
  randomId:()=> '7'.repeat(32),clock:()=>0,schedule:()=>1,cancel(){},send:async(body)=>{calls.push({path:'/api/game/presentation',body});return input.ack||new Promise(()=>{});}});
 scope.presentationClient.tick();await settle();
 if(input.start){assert.equal(scope.gameDisplayReady(),true);void scope.startGame();await settle();}
 else{assert.equal(scope.gameDisplayReady(),false);assert.equal(await scope.startGame(),false);}
 console.log(JSON.stringify(calls));scope.presentationClient.close();
}
main().catch(error=>{console.error(error);process.exitCode=1;});
