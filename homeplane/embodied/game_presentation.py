"""Short, round-bound display availability. Never device or player authority."""
from collections import deque
import secrets

from pydantic import BaseModel, ConfigDict, Field, model_validator
from homeplane.embodied.catalog import MotionGameMode


class GameViewReport(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_context: str = Field(pattern=r'^[a-f0-9]{32}$')
    expected_round: int = Field(ge=1, le=2**53-1)
    expected_view: str = Field(pattern=r'^[a-f0-9]{32}$')
    mode: MotionGameMode
    view_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    sequence: int = Field(ge=0, le=2**53-1)
    window: str = Field(pattern=r'^[a-f0-9]{32}$')
    visible: bool
    ready: bool

    @model_validator(mode='after')
    def hidden_cannot_be_ready(self):
        if self.ready and not self.visible:
            raise ValueError('invalid_game_view')
        return self


class GamePresentation:
    TTL = 2.5
    WINDOW = 2.0
    MAX_LIVE = 8
    MAX_VIEWS = 128

    def __init__(self, clock):
        self.clock, self.scope, self.context = clock, None, None
        self.views, self.windows = {}, deque(maxlen=3)
        self.handoff_at = None

    def synchronize(self, scope):
        if self.scope != scope:
            self.handoff_at = None
            if (self.scope is not None and self.scope[:3] == scope[:3]
                    and scope[2] in {'quest', 'hiit'} and type(self.scope[3]) is int
                    and scope[3] == self.scope[3]+1):
                now = self.clock()
                # 只保留上一画面尚余的可见期限，不继承就绪资格。
                self.handoff_at = max((v['at'] for v in self.views.values()
                                       if v['visible'] and not v['closed'] and 0 <= now-v['at'] <= self.TTL),
                                      default=None)
            self.scope = scope
            self.context = secrets.token_hex(16)
            self.views.clear()
            self.windows.clear()

    def ready(self, scope):
        self.synchronize(scope)
        now = self.clock()
        return any(value['visible'] and value['ready'] and not value['closed'] and 0 <= now-value['at'] <= self.TTL
                   for value in self.views.values())

    def visible(self, scope):
        self.synchronize(scope)
        now = self.clock()
        return any(value['visible'] and not value['closed'] and 0 <= now-value['at'] <= self.TTL
                   for value in self.views.values())

    def transition_visible(self, scope):
        return self.visible(scope) or (self.handoff_at is not None and 0 <= self.clock()-self.handoff_at <= self.TTL)

    def public(self, scope):
        self.synchronize(scope)
        now = self.clock()
        if not self.windows or not 0 <= now-self.windows[-1][1] < 1:
            self.windows.append((secrets.token_hex(16), now))
        return dict(required=True, ready=self.ready(scope), context=self.context, window=self.windows[-1][0])

    def observe(self, scope, report):
        self.synchronize(scope)
        now, old = self.clock(), self.views.get(report.view_id)
        if report.visible:
            if not any(token == report.window and 0 <= now-at <= self.WINDOW for token, at in self.windows):
                raise ValueError('game_view_expired')
            if old and (old['closed'] or not 0 <= now-old['at'] <= self.TTL):
                raise ValueError('game_view_expired')
        signature = (report.visible, report.ready)
        if old and report.sequence <= old['sequence']:
            if report.sequence != old['sequence'] or signature != old['signature']:
                raise ValueError('game_view_changed')
            # An identical retry does not renew its deadline.
            return self.reply(scope, report)
        if old is None:
            if len(self.views) >= self.MAX_VIEWS:
                raise ValueError('game_view_capacity')
            if report.visible and report.ready and report.sequence != 0:
                raise ValueError('game_view_changed')
        if report.visible and (old is None or not old['visible']):
            if sum(value['visible'] and not value['closed'] and 0 <= now-value['at'] <= self.TTL
                   for value in self.views.values()) >= self.MAX_LIVE:
                raise ValueError('game_view_capacity')
        self.views[report.view_id] = dict(at=now, sequence=report.sequence, signature=signature,
                                         visible=report.visible, ready=report.ready, closed=not report.visible)
        self.handoff_at = None
        return self.reply(scope, report)

    def reply(self, scope, report):
        return dict(accepted=True, view_id=report.view_id, sequence=report.sequence,
                    expected_context=scope[0], expected_round=scope[1], mode=scope[2],
                    expected_view=self.context,
                    view_ready=report.visible and report.ready, ready=self.ready(scope))
