"""Deterministic Werewolf rules. Public state never contains private night inputs.

No camera, network, model, speech or device code belongs in this module. A seat is
claimed by its phone credential, never by a face or a tracked skeleton.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import random
import time
import unicodedata
from typing import Callable


ROLE_NAMES = {
    "wolf": "狼人", "seer": "预言家", "witch": "女巫",
    "hunter": "猎人", "guard": "守卫", "villager": "村民",
}
PHASE_NAMES = {
    "lobby": "等待大家入座", "guard": "守卫行动", "wolves": "狼人行动",
    "seer": "预言家行动", "witch": "女巫行动", "dawn": "天亮了",
    "discussion": "自由发言", "vote": "放逐投票", "runoff_discussion": "平票发言",
    "runoff": "再次投票", "hunter": "猎人行动", "finished": "本局结束",
}
RULES = (
    "好人找出全部狼人获胜；存活狼人达到其余存活人数时，狼人获胜。",
    "不设警长，不翻出局身份。发言后手机匿名投票，公布票型。一次平票加赛，再平票则无人出局。",
    "狼人每晚分别投票，唯一最高票为袭击目标；平票或全部放弃则不袭击。",
    "女巫每晚最多使用一瓶药，仅首夜可以自救；解药用尽后不再获知袭击目标。",
    "守卫不能连续两晚守护同一人；同守同救会出局。猎人被毒杀时不能开枪。",
    "夜间各角色环节均按固定时长进行；未提交行动视为放弃。摄像头指认不直接计入投票。",
)


class GameError(ValueError):
    pass


def role_pack(count: int) -> list[str]:
    if type(count) is not int or not 6 <= count <= 12:
        raise GameError("player_count_invalid")
    wolves = 2 if count <= 8 else 3 if count <= 11 else 4
    roles = ["wolf"] * wolves + ["seer", "witch"]
    if count >= 7:
        roles.append("hunter")
    if count >= 11:
        roles.append("guard")
    return roles + ["villager"] * (count - len(roles))


@dataclass
class Player:
    seat: int
    name: str
    ready: bool = False
    role: str | None = None
    alive: bool = True


class WerewolfGame:
    def __init__(self, count: int, *, clock: Callable[[], float] = time.monotonic,
                 rng=None, night_seconds=30, discussion_seconds=90, vote_seconds=45):
        self.pack = role_pack(count)
        self.count = count
        self.clock = clock
        self.rng = rng or random.SystemRandom()
        self.durations = dict.fromkeys(("guard", "wolves", "seer", "witch", "hunter"), night_seconds)
        self.durations.update(dawn=10, discussion=discussion_seconds,
                              runoff_discussion=45, vote=vote_seconds, runoff=vote_seconds)
        if any(type(v) is not int or not 10 <= v <= 600 for v in self.durations.values()):
            raise GameError("duration_invalid")
        self.players: dict[int, Player] = {}
        self.phase = "lobby"
        self.revision = 0
        self.deadline = None
        self.paused = False
        self.remaining = None
        self.round = 0
        self.winner = None
        self.end_reason = None
        self.announcements: list[dict] = []
        self.choices: dict[int, tuple[str, int | None]] = {}
        self.seer_results: dict[int, list[dict]] = {}
        self.antidote = True
        self.poison = True
        self.guard_last = None
        self.guard_target = None
        self.wolf_target = None
        self.witch_choice = ("pass", None)
        self.vote_result = None
        self.runoff_candidates: list[int] = []
        self.pending_hunter = None
        self.after_hunter = None
        self.recent_deaths: list[int] = []
        self._announce("一起入座，准备开始。")

    def _announce(self, text):
        self.host_script = {"id": len(self.announcements) + 1, "text": text}
        self.announcements.append(dict(self.host_script))

    def join(self, seat, name):
        if self.phase != "lobby":
            raise GameError("join_closed")
        if type(seat) is not int or not 1 <= seat <= self.count or seat in self.players:
            raise GameError("seat_unavailable")
        if not isinstance(name, str):
            raise GameError("name_invalid")
        name = unicodedata.normalize("NFC", name).strip()
        if not 1 <= len(name) <= 20 or any(unicodedata.category(c).startswith("C") for c in name):
            raise GameError("name_invalid")
        self.players[seat] = Player(seat, name)
        self.revision += 1

    def remove(self, seat):
        if self.phase != "lobby" or seat not in self.players:
            raise GameError("seat_cannot_remove")
        del self.players[seat]
        self.revision += 1

    def ready(self, seat, value):
        if self.phase != "lobby" or type(value) is not bool or seat not in self.players:
            raise GameError("ready_unavailable")
        self.players[seat].ready = value
        self.revision += 1

    def start(self):
        if self.phase != "lobby" or len(self.players) != self.count or not all(p.ready for p in self.players.values()):
            raise GameError("players_not_ready")
        pack = list(self.pack)
        self.rng.shuffle(pack)
        for seat, role in zip(sorted(self.players), pack):
            self.players[seat].role = role
        self._night()

    def _phase(self, phase, text):
        self.phase = phase
        self.revision += 1
        self.choices = {}
        self.deadline = self.clock() + self.durations[phase] if phase in self.durations else None
        self._announce(text)

    def _night_phases(self):
        return (["guard"] if "guard" in self.pack else []) + ["wolves", "seer", "witch"]

    def _night(self):
        self.round += 1
        self.guard_target = None
        self.wolf_target = None
        self.witch_choice = ("pass", None)
        self.recent_deaths = []
        self.runoff_candidates = []
        phase = self._night_phases()[0]
        self._phase(phase, f"第 {self.round} 夜。天黑请闭眼。{PHASE_NAMES[phase]}，请在手机上完成选择。")

    def _role_actor(self, seat):
        p = self.players[seat]
        return bool(p.alive and {"guard": "guard", "wolves": "wolf", "seer": "seer", "witch": "witch"}.get(self.phase) == p.role)

    def actions(self, seat):
        p = self.players[seat]
        if self.paused or self.phase in {"lobby", "finished"}:
            return []
        if self.phase == "hunter":
            return ["shoot", "pass"] if seat == self.pending_hunter else []
        if not p.alive:
            return []
        if self.phase in {"vote", "runoff"}:
            return ["vote", "pass"] if seat not in self.runoff_candidates or self.phase == "vote" else []
        if not self._role_actor(seat):
            return []
        if self.phase == "witch":
            result = ["pass"]
            if self.antidote and self.wolf_target is not None and (self.wolf_target != seat or self.round == 1):
                result.append("save")
            if self.poison:
                result.append("poison")
            return result
        return [{"guard": "guard", "wolves": "attack", "seer": "check"}[self.phase], "pass"]

    def targets(self, seat, action):
        alive = [s for s, p in sorted(self.players.items()) if p.alive]
        if action == "pass":
            return []
        if action == "save":
            return [self.wolf_target] if "save" in self.actions(seat) else []
        if action == "guard":
            return [s for s in alive if s != self.guard_last]
        if action == "check" or action == "shoot":
            return [s for s in alive if s != seat]
        if action == "vote":
            return list(self.runoff_candidates) if self.phase == "runoff" else [s for s in alive if s != seat]
        return alive

    def act(self, seat, action, target, revision):
        if type(revision) is not int or revision != self.revision:
            raise GameError("phase_changed")
        if seat not in self.players or action not in self.actions(seat):
            raise GameError("action_unavailable")
        if action == "pass":
            if target is not None:
                raise GameError("target_invalid")
        elif type(target) is not int or target not in self.targets(seat, action):
            raise GameError("target_invalid")
        # Players may revise their choice before the fixed phase ends. Private
        # choices intentionally do not change the public phase revision.
        self.choices[seat] = (action, target)

    @staticmethod
    def _unique_winner(values):
        counts = Counter(v for v in values if v is not None)
        if not counts:
            return None
        highest = max(counts.values())
        leaders = [seat for seat, count in counts.items() if count == highest]
        return leaders[0] if len(leaders) == 1 else None

    def _close_night_phase(self):
        choices = [(s, choice) for s, choice in self.choices.items() if self._role_actor(s)]
        if self.phase == "guard":
            self.guard_target = next((v[1] for _, v in choices if v[0] == "guard"), None)
            self.guard_last = self.guard_target
        elif self.phase == "wolves":
            self.wolf_target = self._unique_winner(v[1] for _, v in choices if v[0] == "attack")
        elif self.phase == "seer":
            for seat, (action, target) in choices:
                if action == "check":
                    self.seer_results.setdefault(seat, []).append(dict(round=self.round, seat=target,
                                                                      wolf=self.players[target].role == "wolf"))
        elif self.phase == "witch":
            self.witch_choice = next((v for _, v in choices), ("pass", None))
            if self.witch_choice[0] == "save":
                self.antidote = False
            elif self.witch_choice[0] == "poison":
                self.poison = False
        phases = self._night_phases()
        idx = phases.index(self.phase)
        if idx + 1 < len(phases):
            phase = phases[idx + 1]
            self._phase(phase, f"{PHASE_NAMES[phase]}，请在手机上完成选择。其他玩家请闭眼。")
        else:
            self._dawn()

    def _kill(self, seat, cause):
        if seat is None or not self.players[seat].alive:
            return
        p = self.players[seat]
        p.alive = False
        if seat not in self.recent_deaths:
            self.recent_deaths.append(seat)
        if p.role == "hunter" and cause != "poison":
            self.pending_hunter = seat

    def _dawn(self):
        saved = self.witch_choice[0] == "save"
        guarded = self.wolf_target is not None and self.guard_target == self.wolf_target
        # Exactly one protection works; double protection follows the stated rules.
        if not (saved ^ guarded):
            self._kill(self.wolf_target, "attack")
        if self.witch_choice[0] == "poison":
            target = self.witch_choice[1]
            if target == self.pending_hunter:
                self.pending_hunter = None
            self._kill(target, "poison")
        seats = "、".join(f"{s} 号" for s in sorted(self.recent_deaths))
        self._phase("dawn", f"天亮了。昨夜{seats + '出局' if seats else '平安无事'}。")

    def _check_win(self):
        wolves = sum(p.alive and p.role == "wolf" for p in self.players.values())
        others = sum(p.alive and p.role != "wolf" for p in self.players.values())
        if wolves == 0 or wolves >= others:
            self.winner = "village" if wolves == 0 else "wolves"
            self.end_reason = "all_wolves_out" if wolves == 0 else "wolves_parity"
            self._phase("finished", "好人阵营获胜。" if wolves == 0 else "狼人阵营获胜。")
            return True
        return False

    def _interrupt_or_continue(self, continuation):
        if self.pending_hunter is not None:
            self.after_hunter = continuation
            self._phase("hunter", f"{self.pending_hunter} 号猎人，请在手机上选择是否开枪。")
        elif not self._check_win():
            if continuation == "discussion":
                self._phase("discussion", "现在开始自由发言。请依次说明你的判断。")
            else:
                self._night()

    def _close_vote(self):
        voters = [s for s in sorted(self.players) if "vote" in self.actions(s)]
        votes = {s: self.choices.get(s, ("pass", None))[1] for s in voters}
        counts = Counter(v for v in votes.values() if v is not None)
        top = max(counts.values(), default=0)
        leaders = sorted(s for s, n in counts.items() if n == top)
        self.vote_result = dict(votes=[dict(seat=s, target=t) for s, t in votes.items()],
                                counts=[dict(seat=s, count=n) for s, n in sorted(counts.items())],
                                tied=leaders if len(leaders) > 1 else [])
        if len(leaders) > 1 and self.phase == "vote":
            self.runoff_candidates = leaders
            self._phase("runoff_discussion", "、".join(f"{s} 号" for s in leaders) + "平票。请平票玩家发言。")
        else:
            out = leaders[0] if len(leaders) == 1 else None
            self._kill(out, "vote")
            self._announce(f"{out} 号被放逐。" if out else "本轮无人被放逐。")
            self._interrupt_or_continue("night")

    def advance(self):
        if self.paused:
            raise GameError("game_paused")
        first = len(self.announcements)
        if self.phase in self._night_phases():
            self._close_night_phase()
        elif self.phase == "dawn":
            self._interrupt_or_continue("discussion")
        elif self.phase == "discussion":
            self.vote_result = None
            self._phase("vote", "发言结束。请在手机上投票，或选择弃票。")
        elif self.phase == "runoff_discussion":
            self._phase("runoff", "请其他存活玩家在平票玩家中再次投票。")
        elif self.phase in {"vote", "runoff"}:
            self._close_vote()
        elif self.phase == "hunter":
            action, target = self.choices.get(self.pending_hunter, ("pass", None))
            self.pending_hunter = None
            if action == "shoot":
                self._kill(target, "shot")
                self._announce(f"猎人带走了 {target} 号。")
            self._interrupt_or_continue(self.after_hunter or "night")
        else:
            raise GameError("cannot_advance")
        # A transition may include both a public outcome and the next turn.
        # Publish them atomically so the screen and narrator cannot drop the
        # elimination/shot when the following phase is entered immediately.
        messages = self.announcements[first:]
        if messages:
            self.host_script = dict(id=messages[-1]['id'], text=' '.join(m['text'] for m in messages))

    def tick(self):
        # Never fast-forward through several missed phases after suspend/restart.
        if not self.paused and self.deadline is not None and self.clock() >= self.deadline:
            self.advance()

    def pause(self, value):
        if type(value) is not bool or self.phase in {"lobby", "finished"}:
            raise GameError("pause_unavailable")
        if self.paused == value:
            return
        if value:
            self.remaining = max(0, self.deadline - self.clock()) if self.deadline is not None else None
            self.deadline = None
        else:
            self.deadline = self.clock() + self.remaining if self.remaining is not None else None
        self.paused = value
        self.revision += 1

    def end(self):
        self.winner = None
        self.end_reason = "host_ended"
        self._phase("finished", "主持端结束了本局。")
        self.paused = False

    def public(self):
        seconds = self.remaining if self.paused else max(0, self.deadline - self.clock()) if self.deadline else None
        return dict(phase=self.phase, phase_name=PHASE_NAMES[self.phase], revision=self.revision,
                    round=self.round, paused=self.paused, seconds_left=round(seconds) if seconds is not None else None,
                    player_count=self.count, joined=len(self.players),
                    roles=[dict(role=r, name=ROLE_NAMES[r], count=n) for r, n in Counter(self.pack).items()],
                    players=[dict(seat=s, name=p.name, ready=p.ready, alive=p.alive,
                                  **({"role": p.role, "role_name": ROLE_NAMES[p.role]} if self.phase == "finished" and p.role else {}))
                             for s, p in sorted(self.players.items())],
                    announcements=self.announcements[-16:], host_script=dict(self.host_script), vote_result=self.vote_result,
                    runoff_candidates=list(self.runoff_candidates), recent_deaths=list(self.recent_deaths),
                    winner=self.winner, end_reason=self.end_reason, rules=list(RULES))

    def private(self, seat):
        if seat not in self.players:
            raise GameError("player_not_found")
        p = self.players[seat]
        actions = self.actions(seat)
        result = dict(seat=seat, name=p.name, ready=p.ready, alive=p.alive,
                      role=p.role, role_name=ROLE_NAMES.get(p.role),
                      actions=[dict(action=a, targets=self.targets(seat, a)) for a in actions],
                      submitted=self.choices.get(seat), public=self.public())
        if p.role == "wolf":
            result["team"] = [dict(seat=s, name=q.name, alive=q.alive) for s, q in sorted(self.players.items()) if q.role == "wolf"]
            if self.phase == "wolves" and p.alive:
                result["team_choices"] = [dict(seat=s, action=v[0], target=v[1]) for s, v in self.choices.items() if self.players[s].role == "wolf"]
        if p.role == "seer":
            result["checks"] = list(self.seer_results.get(seat, []))
        if p.role == "witch":
            result["potions"] = dict(antidote=self.antidote, poison=self.poison)
            if self.phase == "witch" and p.alive and self.antidote:
                result["attacked_seat"] = self.wolf_target
        return result
