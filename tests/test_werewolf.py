"""Complete synthetic games, night privacy and deterministic rules; no devices."""

import json

import pytest

from homeplane.werewolf import GameError, SPEAKER_SECONDS, WerewolfGame, role_pack


class Clock:
    now = 100.0
    def __call__(self):
        return self.now


class FixedRoles:
    def shuffle(self, values):
        pass


def confirm_deal(game):
    for seat in game.players:
        game.confirm_identity(seat, game.revision)


def game(count=9, *, discussion_mode="free"):
    clock = Clock()
    g = WerewolfGame(count, clock=clock, rng=FixedRoles(), discussion_mode=discussion_mode)
    for seat in range(1, count + 1):
        g.join(seat, f"玩家 {seat}")
        g.ready(seat, True)
    g.start()
    confirm_deal(g)
    g.advance()
    return g, clock


def role(g, name):
    return next(s for s, p in g.players.items() if p.role == name)


def act(g, seat, action, target=None):
    g.act(seat, action, target, g.revision)


def until(g, phase):
    for _ in range(16):
        if g.phase == phase:
            return
        g.advance()
    raise AssertionError((g.phase, phase))


def test_ordered_discussion_only_gives_runoff_candidates_turns_and_rotates_next_day():
    g, clock = game(6, discussion_mode="turns")
    until(g, "discussion")
    assert g.speaker_order == (1, 2, 3, 4, 5, 6)
    assert g.public()["speaking_turn"] == dict(position=1, total=6, next_seat=2)
    with pytest.raises(GameError, match="speaker_unavailable"):
        g.set_speaker(4)
    until(g, "vote")
    for seat, target in [(1, 2), (2, 3), (3, 2), (4, 3)]:
        act(g, seat, "vote", target)
    g.advance()
    assert g.phase == "runoff_discussion" and g.speaker_order == (2, 3)
    assert g.speaker_seat == 2 and g.public()["seconds_left"] == SPEAKER_SECONDS
    g.finish_speaking(2, g.revision)
    assert g.public()["speaking_turn"] == dict(position=2, total=2, next_seat=None)
    clock.now += SPEAKER_SECONDS
    g.tick()
    assert g.phase == "runoff" and g.speaker_seat is None and "speaking_turn" not in g.public()
    assert not g.actions(2) and not g.actions(3)
    act(g, 1, "vote", 2)
    g.advance()
    until(g, "discussion")
    assert g.round == 2 and g.speaker_order == (3, 4, 5, 6, 1)


@pytest.mark.parametrize("count", range(6, 13))
def test_presets_are_playable_and_private(count):
    g, _ = game(count)
    assert len(role_pack(count)) == count
    public = g.public()
    assert all("role" not in p for p in public["players"])
    for seat, player in g.players.items():
        private = g.private(seat)
        assert private["role"] == player.role
        if player.role != "wolf":
            assert "team" not in private
        assert "checks" not in private or player.role == "seer"
        assert "potions" not in private or player.role == "witch"


def test_lobby_and_stale_command_guards():
    g = WerewolfGame(6)
    for n in range(1, 7):
        g.join(n, str(n))
    with pytest.raises(GameError, match="players_not_ready"):
        g.start()
    for n in range(1, 7):
        g.ready(n, True)
    g.start()
    with pytest.raises(GameError, match="join_closed"):
        g.join(1, "intruder")
    confirm_deal(g)
    g.advance()
    with pytest.raises(GameError, match="phase_changed"):
        g.act(1, "pass", None, g.revision - 1)
    with pytest.raises(GameError, match="action_unavailable"):
        act(g, role(g, "villager"), "attack", 1)
    with pytest.raises(GameError, match="target_invalid"):
        act(g, role(g, "wolf"), "attack", True)


def test_speaker_selection_respects_alive_seats_runoff_and_the_existing_timer():
    g, clock = game(6)
    with pytest.raises(GameError, match="speaker_unavailable"):
        g.set_speaker(4)
    act(g, 1, "attack", 6)
    until(g, "discussion")
    assert g.eligible_speakers() == [1, 2, 3, 4, 5]
    with pytest.raises(GameError, match="speaker_unavailable"):
        g.set_speaker(6)
    deadline = g.deadline
    g.set_speaker(4)
    assert g.public()["speaker_seat"] == 4 and g.deadline == deadline
    called = g.public()
    g.set_speaker(4)
    assert g.public() == called
    g.pause(True)
    assert g.eligible_speakers() == []
    with pytest.raises(GameError, match="speaker_unavailable"):
        g.set_speaker(5)
    g.pause(False)
    assert g.public()["speaker_seat"] == 4 and g.deadline == deadline
    clock.now = deadline
    with pytest.raises(GameError, match="speaker_unavailable"):
        g.set_speaker(5)
    g.tick()
    assert g.phase == "vote" and g.public()["speaker_seat"] is None
    act(g, 3, "vote", 4)
    act(g, 5, "vote", 1)
    g.advance()
    assert g.phase == "runoff_discussion" and g.eligible_speakers() == [1, 4]
    with pytest.raises(GameError, match="speaker_unavailable"):
        g.set_speaker(2)
    deadline = g.deadline
    g.set_speaker(1)
    g.set_speaker(None)
    assert g.public()["speaker_seat"] is None and g.deadline == deadline
    g.set_speaker(4)
    g.advance()
    assert g.phase == "runoff" and g.public()["speaker_seat"] is None
    assert g.actions(4) == [] and g.targets(5, "vote") == [1, 4]


def test_night_choices_and_seer_result_never_reach_public_or_other_phones():
    g, _ = game()
    public_before = g.public()
    act(g, 1, "attack", 7)
    assert g.public() == public_before
    assert g.private(2)["team_choices"] == [dict(seat=1, action="attack", target=7)]
    assert "team_choices" not in g.private(7)
    g.advance()
    seer = role(g, "seer")
    act(g, seer, "check", 1)
    g.advance()
    assert g.private(seer)["checks"] == [dict(round=1, seat=1, wolf=True)]
    assert "checks" not in g.private(role(g, "witch"))
    forbidden = {"choices", "team_choices", "attacked_seat", "checks", "potions", "role"}
    def keys(value):
        if isinstance(value, dict):
            yield from value
            for item in value.values():
                yield from keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys(item)
    # The published role composition is intentionally public; player assignments
    # and all choices remain absent until the game ends.
    public = g.public()
    public.pop("roles")
    assert not forbidden.intersection(keys(public))


def test_wolves_tie_means_no_attack_and_witch_cannot_invent_victim():
    g, _ = game()
    act(g, 1, "attack", 7)
    act(g, 2, "attack", 8)
    until(g, "witch")
    witch = role(g, "witch")
    assert g.private(witch)["attacked_seat"] is None
    with pytest.raises(GameError, match="action_unavailable"):
        act(g, witch, "save", 7)
    g.advance()
    assert all(p.alive for p in g.players.values())
    assert g.phase == "dawn"


def test_antidote_and_poison_are_single_resources_and_one_action_per_night():
    g, _ = game()
    victim, witch = 7, role(g, "witch")
    act(g, 1, "attack", victim)
    until(g, "witch")
    act(g, witch, "save", victim)
    g.advance()
    assert g.players[victim].alive and not g.antidote
    until(g, "vote")
    g.advance()  # No votes, next night.
    act(g, 1, "attack", victim)
    until(g, "witch")
    private = g.private(witch)
    assert "attacked_seat" not in private and "save" not in g.actions(witch)
    act(g, witch, "poison", 1)
    g.advance()
    assert not g.players[1].alive and not g.players[victim].alive and not g.poison


def test_guard_consecutive_target_and_double_protection():
    g, _ = game(12)
    guard, witch = role(g, "guard"), role(g, "witch")
    act(g, guard, "guard", 12)
    g.advance()
    act(g, 1, "attack", 12)
    until(g, "witch")
    act(g, witch, "save", 12)
    g.advance()
    assert not g.players[12].alive  # The published double-protection rule.
    until(g, "vote")
    g.advance()
    with pytest.raises(GameError, match="target_invalid"):
        act(g, guard, "guard", 12)


def test_guard_cannot_repeat_a_still_living_target():
    g, _ = game(12)
    guard = role(g, "guard")
    act(g, guard, "guard", 11)
    until(g, "vote")
    g.advance()
    assert g.players[11].alive
    with pytest.raises(GameError, match="target_invalid"):
        act(g, guard, "guard", 11)


@pytest.mark.parametrize("poison", [False, True])
def test_hunter_interrupt_and_poison_disables_shot(poison):
    g, _ = game()
    hunter, witch = role(g, "hunter"), role(g, "witch")
    if not poison:
        act(g, 1, "attack", hunter)
    until(g, "witch")
    if poison:
        act(g, witch, "poison", hunter)
    g.advance()
    g.advance()
    if poison:
        assert g.phase == "discussion" and not g.actions(hunter)
    else:
        assert g.phase == "hunter"
        act(g, hunter, "shoot", 1)
        g.advance()
        assert not g.players[1].alive and g.phase == "discussion"


def test_second_tie_abstains_and_candidate_cannot_vote_in_runoff():
    g, _ = game()
    until(g, "vote")
    act(g, 1, "vote", 7)
    act(g, 2, "vote", 8)
    g.advance()
    assert g.phase == "runoff_discussion" and g.runoff_candidates == [7, 8]
    g.advance()
    assert not g.actions(7) and not g.actions(8)
    act(g, 1, "vote", 7)
    act(g, 2, "vote", 8)
    g.advance()
    assert g.round == 2 and all(p.alive for p in g.players.values())


def test_a_whole_village_victory_reveals_roles_only_at_the_end():
    g, _ = game(9)
    for target in (1, 2, 3):
        until(g, "vote")
        for seat, p in g.players.items():
            if p.alive and seat != target:
                act(g, seat, "vote", target)
        g.advance()
    assert g.phase == "finished" and g.winner == "village"
    assert all(p["role"] in role_pack(9) for p in g.public()["players"])
    assert "好人阵营获胜" in json.dumps(g.public(), ensure_ascii=False)


def test_a_whole_wolf_victory_with_automatic_phase_deadlines():
    g, clock = game(6)
    for victim in (5, 6):
        act(g, 1, "attack", victim)
        while g.phase not in {"discussion", "finished"}:
            clock.now = g.deadline + .1
            g.tick()
        if g.phase == "finished":
            break
        until(g, "vote")
        g.advance()
    assert g.phase == "finished" and g.winner == "wolves"


def test_pause_freezes_private_actions_and_resumes_exact_remaining_time():
    g, clock = game()
    before = g.deadline - clock.now
    g.pause(True)
    clock.now += 1000
    g.tick()
    assert g.phase == "wolves" and g.public()["seconds_left"] == before
    with pytest.raises(GameError, match="action_unavailable"):
        act(g, 1, "attack", 9)
    g.pause(False)
    assert g.deadline - clock.now == before
    clock.now = g.deadline + 1000
    g.tick()
    assert g.phase == "seer"  # A late tick never skips several private turns.


@pytest.mark.parametrize('target',[None,1,6])
def test_public_host_script_keeps_vote_result_with_the_next_phase(target):
    g,_=game();until(g,'vote')
    if target is not None:
        for seat in g.players:
            if seat!=target:act(g,seat,'vote',target)
    first=len(g.announcements);g.advance();public=g.public();script=public['host_script']
    assert script['id']==public['announcements'][-1]['id']
    assert script['text']==' '.join(a['text']for a in g.announcements[first:])
    assert script['text'].startswith(f'{target} 号被放逐。'if target else'本轮无人被放逐。')
    assert ('第 2 夜。'if target!=6 else'6 号猎人')in script['text']
    assert all('role'not in p for p in public['players'])


def test_hunter_result_and_victory_are_not_lost_from_the_current_public_script():
    g,_=game();hunter=role(g,'hunter');act(g,1,'attack',hunter);until(g,'hunter')
    act(g,hunter,'shoot',1);g.advance()
    assert g.public()['host_script']['text']=='猎人带走了 1 号。 现在开始自由发言。'
    for target in [2,3]:
        until(g,'vote')
        for seat,p in g.players.items():
            if p.alive and seat!=target:act(g,seat,'vote',target)
        g.advance()
    assert g.public()['host_script']['text']=='3 号被放逐。 好人阵营获胜。'


def test_private_night_choices_do_not_change_or_extend_the_public_host_script():
    g,_=game();script=dict(g.public()['host_script']);act(g,1,'attack',7)
    assert g.public()['host_script']==script
    g.pause(True);assert g.public()['host_script']==script
    g.pause(False);assert g.public()['host_script']==script
