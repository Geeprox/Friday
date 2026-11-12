function element(tag, text, className) {
 const node = document.createElement(tag);
 if (text != null) node.textContent = text;
 if (className) node.className = className;
 return node;
}

const seatLabel = seat => seat + ' 号';
const seatList = seats => seats.map(seatLabel).join('、');

function ballot(result) {
 const runoff = result.ballot === 'runoff';
 const title = runoff ? '加赛投票' : '放逐投票';
 let outcome = '无人被放逐';
 if (result.eliminated) outcome = seatLabel(result.eliminated) + '被放逐';
 else if (result.tied.length) {
  outcome = runoff ? '再次平票，无人被放逐' : seatList(result.tied) + '平票';
 }
 const details = element('details', null, 'ww-recap-ballot');
 const votes = element('ul', null, 'ww-recap-votes');
 votes.setAttribute('role', 'list');
 for (const vote of result.votes) {
  const action = vote.target == null ? '弃票' : '投给 ' + seatLabel(vote.target);
  votes.append(element('li', seatLabel(vote.seat) + action));
 }
 details.append(element('summary', runoff ? '查看加赛票型' : '查看放逐票型'), votes);
 return {title, outcome, details};
}

function roundEvent(event) {
 let view;
 if (event.kind === 'dawn') {
  view = {title: '天亮', outcome: event.seats.length ? seatList(event.seats) + '出局' : '无人出局'};
 } else if (event.kind === 'hunter') {
  view = {title: '猎人行动', outcome: '猎人带走 ' + seatLabel(event.seat)};
 } else if (event.kind === 'ballot') {
  view = ballot(event.result);
 } else return null;
 const row = element('li', null, 'ww-recap-event');
 row.append(element('span', view.title, 'ww-recap-event-name'), element('strong', view.outcome));
 if (view.details) row.append(view.details);
 return row;
}

function roundsSection(rounds, heading) {
 const section = element('section', null, 'ww-recap-rounds');
 const header = element('div', null, 'ww-recap-section-heading');
 const list = element('ol', null, 'ww-recap-round');
 list.setAttribute('role', 'list');
 const roundName = element('span');
 let index = rounds.length - 1;
 function showRound() {
  const round = rounds[index];
  roundName.textContent = '第 ' + round.round + ' 轮';
  list.replaceChildren(...round.events.map(roundEvent).filter(Boolean));
 }
 header.append(element(heading, '对局复盘'));
 if (rounds.length > 1) {
  const control = element('div', null, 'ww-recap-round-nav');
  const previous = element('button');
  const next = element('button');
  previous.type = next.type = 'button';
  previous.setAttribute('aria-label', '上一轮');
  next.setAttribute('aria-label', '下一轮');
  roundName.setAttribute('aria-live', 'polite');
  function updateControls() {
   previous.setAttribute('aria-disabled', String(index === 0));
   next.setAttribute('aria-disabled', String(index === rounds.length - 1));
  }
  function step(offset) {
   const target = index + offset;
   if (target < 0 || target >= rounds.length) return;
   index = target;
   showRound();
   updateControls();
  }
  previous.onclick = () => step(-1);
  next.onclick = () => step(1);
  updateControls();
  control.append(previous, roundName, next);
  header.append(control);
 } else header.append(roundName);
 showRound();
 section.append(header, list);
 return section;
}

function identitiesSection(players, heading) {
 const section = element('section', null, 'ww-recap-identities');
 const header = element('div', null, 'ww-recap-section-heading');
 const roles = element('ol', null, 'ww-recap-roles');
 roles.setAttribute('role', 'list');
 for (const player of players) {
  const row = element('li');
  const label = element('div');
  const status = player.role_name + (player.alive ? '' : ' · 出局');
  label.append(element('strong', player.name), element('small', status));
  row.append(element('span', String(player.seat), 'ww-recap-seat'), label);
  roles.append(row);
 }
 header.append(element(heading, '身份揭晓'));
 section.append(header, roles);
 return section;
}

export function makeWerewolfRecap(game, {art, heading = 'h1'}) {
 const recap = element('article', null, 'ww-recap');
 const hero = element('div', null, 'ww-recap-hero');
 const copy = element('div', null, 'ww-recap-summary');
 const title = {village: '好人阵营获胜', wolves: '狼人阵营获胜'}[game.winner] || '本局已结束';
 const reason = {
  all_wolves_out: '全部狼人出局',
  wolves_parity: '存活狼人达到好人人数',
  host_ended: '主持人结束了这局',
 }[game.end_reason];
 const titleNode = element(heading, title, 'ww-recap-title');
 titleNode.setAttribute('tabindex', '-1');
 copy.append(titleNode);
 if (reason) copy.append(element('p', reason, 'ww-recap-reason'));
 const meta = game.player_count + ' 人局' + (game.round ? ' · ' + game.round + ' 轮' : '');
 copy.append(element('p', meta, 'ww-recap-meta'));
 const image = element('img', null, 'ww-recap-art');
 image.src = art;
 image.alt = '';
 hero.append(copy, image);
 recap.append(hero);

 const body = element('div', null, 'ww-recap-body');
 const sectionHeading = heading === 'h3' ? 'h4' : 'h2';
 const players = game.players.filter(player => player.role_name);
 const rounds = game.recap || [];
 if (players.length) body.append(identitiesSection(players, sectionHeading));
 if (rounds.length) body.append(roundsSection(rounds, sectionHeading));
 if (players.length || rounds.length) recap.append(body);
 return recap;
}
