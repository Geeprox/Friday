"""Immutable round settings; recognition confidence and motion geometry stay fixed."""

import math
from dataclasses import dataclass, replace
from typing import Literal

DifficultyId = Literal["easy", "standard", "challenge"]
_ALTERNATING = frozenset({"high_knee", "lunge", "side_step", "side_bend", "reach_up"})


@dataclass(frozen=True)
class GameDifficulty:
    id: DifficultyId
    label: str
    hold: float
    repetitions: float
    ski_speed: float
    ski_gate: float
    tennis_speed: float
    tennis_max_speed: float
    tennis_window: float

    def reps(self, action, count):
        value = max(1, math.ceil(count * self.repetitions))
        return value + value % 2 if action in _ALTERNATING else value

    def quest_plan(self, stages):
        if self.id == "standard":
            return stages
        result = []
        for stage in stages:
            old = f"{stage.hold:g} 秒" if stage.hold else f"{stage.reps} 次"
            count = self.reps(stage.action, stage.reps) if stage.reps else 0
            new = f"{self.hold:g} 秒" if stage.hold else f"{count} 次"
            if old not in stage.instruction:
                raise ValueError("game_stage_instruction_mismatch")
            result.append(
                replace(
                    stage,
                    hold=self.hold if stage.hold else 0.0,
                    reps=count,
                    instruction=stage.instruction.replace(old, new),
                )
            )
        return tuple(result)

    def training_plan(self, plan):
        if self.id == "standard":
            return plan
        return tuple(
            (action, name, self.reps(action, count), instruction) for action, name, count, instruction in plan
        )

    def public(self):
        notes = {
            "easy": ("速度放慢，木门更宽", "来球更慢，回击时间更充裕", "动作次数较少"),
            "standard": ("循序穿门，完成三段雪道", "来球节奏随回合渐进", "完成整轮动作"),
            "challenge": ("速度更快，木门更窄", "来球更快，回击时机更紧", "增加动作次数"),
        }[self.id]
        return dict(
            id=self.id,
            label=self.label,
            description=dict(
                ski=notes[0], tennis=notes[1], quest=f"保持 {self.hold:g} 秒，{notes[2]}", hiit=notes[2]
            ),
        )


_DIFFICULTIES = (
    GameDifficulty("easy", "轻松", 2.0, 0.5, 0.8, 1.2, 0.34, 0.54, 1.4),
    GameDifficulty("standard", "标准", 3.0, 1.0, 1.0, 1.0, 0.42, 0.65, 1.0),
    GameDifficulty("challenge", "挑战", 4.0, 1.5, 1.15, 0.85, 0.5, 0.78, 0.8),
)


def game_difficulty(value):
    if type(value) is str:
        for item in _DIFFICULTIES:
            if item.id == value:
                return item
    raise ValueError("invalid_game_difficulty")


def difficulty_options():
    return [item.public() for item in _DIFFICULTIES]
