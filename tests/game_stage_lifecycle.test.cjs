const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {makeClient,game,packet}=require('./game_stage_harness.cjs');

const source = fs.readFileSync('homeplane/embodied/static/games.js', 'utf8');
function actualFunction(begin, end) {
  const from = source.indexOf(begin), to = source.indexOf(end, from);
  assert(from >= 0 && to > from);
  return source.slice(from, to);
}

test('source identity survives incomplete packets and clears only on explicit real input', () => {
  const label={hidden:true},body={dataset:{}};
  const scope=vm.createContext({$:id=>{assert.equal(id,'sourceMode');return label;},document:{body}});
  vm.runInContext(actualFunction('function updateSourceMode(', '\nfunction completedGame('),scope);
  scope.updateSourceMode({simulated:true});
  assert.equal(label.hidden,false);assert.equal(body.dataset.simulated,'true');
  for(const packet of [null,{}, {simulated:'false'}])scope.updateSourceMode(packet);
  assert.equal(label.hidden,false);
  scope.updateSourceMode({simulated:false});
  assert.equal(label.hidden,true);assert.equal(body.dataset.simulated,undefined);
});

test('ending consumes the returned game state before resetting the completed overlay', async () => {
  const calls = [];
  const stopped = game({mode: 'off',status:'off',round_id:2,control_revision:2});
  const owner = makeClient({
    meta: {game:game({status:'complete'})}, active: false,
    fetch: async (path, options) => {
      assert.equal(path, '/api/game');
      assert.deepEqual(JSON.parse(options.body), {mode: 'off',expected_context:'a'.repeat(32),expected_revision:1});
      return {ok: true, json: async () => stopped};
    },
    selectMode: mode => calls.push([mode, owner.c.meta.game.mode]),
    updateHUD: () => calls.push('updated'),
  });
  const context=owner.c;
  await context.stopGame();
  assert.equal(context.meta.game, stopped);
  assert.deepEqual(calls, [['quest', 'off'], 'updated']);
  assert.equal(owner.node('gameNotice').textContent, '');
});

for (const response of [{ok: false}, {ok: true, json: async () => ({mode: 'quest'})}]) {
  test('failed or conflicting end keeps current state and offers recovery', async () => {
    const old = {game:game()};
    const owner = makeClient({
      meta: old, active: true,
      fetch: async () => response,
      selectMode: () => assert.fail('must not reset an unconfirmed round'),
      updateHUD: () => assert.fail('must not report stopped'),
    });
    const context=owner.c;
    await context.stopGame();
    assert.equal(context.meta, old);
    assert.equal(context.active, true);
    assert.equal(owner.node('gameNotice').textContent, '暂时无法结束本局，请重试。');
  });
}

test('peer end with the same selected game replaces a completed overlay once', () => {
  const selections = [];
  const owner=makeClient({selectMode:mode=>selections.push(mode)}),context=owner.c;
  context.connect();
  const socket=owner.sockets[0];socket.onopen();
  const data=packet(game({mode:'off',round_id:2,control_revision:2}));
  socket.onmessage({data});
  socket.onmessage({data});
  assert.deepEqual(selections, ['quest']);
  assert.equal(context.lastBackendRound, 'off');
});
