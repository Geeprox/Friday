"""在同一采集进程内协调各路镜头的普通巡航。"""


class CameraPatrol:
    FRAME_MAX_AGE = 1.5
    MOTION_SETTLE = 2
    # 云台回执最多等 3 秒，收到回执后最多再核对 8 秒的新视角。
    MOTION_MAX_WAIT = 11

    def __init__(self):
        self.views = {}
        self.moves = {}

    def forget_view(self, camera):
        self.views.pop(camera, None)

    def observe(self, camera, sample, control, now):
        captured = sample.get('_local_received')
        if (sample.get('status') != 'live' or type(captured) not in (int, float)
                or not 0 <= now - captured <= self.FRAME_MAX_AGE):
            self.forget_view(camera)
            return
        previous = self.views.get(camera)
        if previous is not None and captured < previous[0]:
            return
        usable = (sample.get('scene_visible') is True and sample.get('tracking_current') is True
                  and (control.get('view_usable') is True or control.get('mode') == 'hold'))
        started = self.moves.get(camera)
        if started is not None:
            usable = usable and captured >= started + self.MOTION_SETTLE
            if usable:
                self.moves.pop(camera)
        self.views[camera] = (captured, usable)

    def allows(self, camera, now):
        if any(other != camera and 0 <= now - started < self.MOTION_MAX_WAIT
               for other, started in self.moves.items()):
            return False
        peers = [usable for other, (captured, usable) in self.views.items()
                 if other != camera and 0 <= now - captured <= self.FRAME_MAX_AGE]
        return not peers or any(peers)

    def dispatched(self, camera, now):
        # 实际写入前撤销原视角。无回执时等新画面或到达上限。
        self.moves[camera] = now
        self.forget_view(camera)
