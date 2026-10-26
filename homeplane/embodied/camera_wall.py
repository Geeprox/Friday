"""Fresh video fan-out, independent of recognition completion and UI lifetime."""
import base64
import math
import time
from .video_relay import VideoRelay


class CameraWall:
    def __init__(self, cameras, *, clock=time.time):
        self.cameras = tuple(dict(id=c['id'], name=c['name'], room=c['room']) for c in cameras)
        self.clock = clock
        self.frames = {}
        self.stills = {}
        self.epochs = {c['id']: 0 for c in cameras}
        self.video = {c['id']: VideoRelay(clock=clock) for c in cameras}
        self.observations = {}
        self.labels = {}
        self.toasts = {}
        self.gestures = {}
        self.toast_sequence=0

    def begin(self, camera):
        self.epochs[camera] += 1
        self.frames.pop(camera, None)
        self.stills.pop(camera,None)
        self.video[camera].clear()
        self.observations.pop(camera,None)
        self.toasts.pop(camera,None);self.gestures.pop(camera,None)
        self.labels[camera]={}
        return self.epochs[camera]

    def clear(self, camera, epoch):
        if self.epochs[camera] == epoch:
            self.epochs[camera] += 1
            self.frames.pop(camera, None)
            self.stills.pop(camera,None)
            self.video[camera].clear()
            self.observations.pop(camera,None)
            self.toasts.pop(camera,None);self.gestures.pop(camera,None)

    def offer(self, camera, epoch, packet):
        if self.epochs[camera] != epoch:return
        if packet.get('type')=='video':
            self.video[camera].offer(packet)
            return
        if packet.get('type') != 'frame':return
        at = packet.get('preview_at')
        if type(at) not in (int, float) or not math.isfinite(at) or not 0 <= self.clock()-at <= 1.5:return
        value = packet.get('preview')
        if not isinstance(value, str) or len(value) > 350000:return
        try:jpeg = base64.b64decode(value, validate=True)
        except (ValueError, TypeError):return
        if not jpeg.startswith(b'\xff\xd8') or not jpeg.endswith(b'\xff\xd9'):return
        old = self.frames.get(camera)
        if old and old[1] >= at:return
        self.frames[camera] = (jpeg, at)
        # Reuse the JPEG already encoded for perception. This fallback needs
        # no extra encoder, source connection or recognition delay.
        full=packet.get('jpeg');captured=packet.get('received_at')
        if (type(captured) not in (int,float) or not math.isfinite(captured)
                or captured!=at or not 0<=self.clock()-captured<=1.5 or not isinstance(full,str)
                or len(full)>2796208):return
        try:full=base64.b64decode(full,validate=True)
        except (ValueError,TypeError):return
        if len(full)<=2097152 and full.startswith(b'\xff\xd8') and full.endswith(b'\xff\xd9'):
            self.stills[camera]=(full,captured)

    def notice(self,camera,text):
        self.toast_sequence+=1
        self.toasts[camera]=dict(id=self.toast_sequence,text=text,at_ms=int(self.clock()*1000))

    def observe(self, camera, session, people, at, *, moving=False, hands=()):
        if moving or not 0 <= self.clock()-at <= 1.5:
            self.observations.pop(camera,None);self.gestures.pop(camera,None);self.toasts.pop(camera,None);return
        from .interaction import body_gestures,GESTURES
        recognized=set()
        for p in people:
            recognized.update(body_gestures(p))
        recognized.update('hand.'+h['gesture'] for h in hands if h.get('gesture') in {'ok','yeah','open_palm'})
        added=recognized-self.gestures.get(camera,set())
        self.gestures[camera]=recognized
        if added:self.notice(camera,'已识别 · '+ '、'.join(GESTURES[g] for g in sorted(added) if g in GESTURES))
        rows=[]
        labels=self.labels.setdefault(camera,{})
        for p in people[:16]:
            key=(session,str(p['id']))
            if key not in labels:
                if len(labels)>=512:continue
                labels[key]=len(labels)+1
            box=p.get('box') or {}
            if not all(type(box.get(k)) in (int,float) and math.isfinite(box[k]) for k in ('x','y','w','h')):continue
            joints={}
            for name,j in (p.get('joints') or {}).items():
                if (len(name)<=32 and all(type(j.get(k)) in (int,float) and math.isfinite(j[k]) for k in ('x','y','confidence'))
                        and 0<=j['x']<=1 and 0<=j['y']<=1 and j['confidence']>=.4):
                    joints[name]={k:j[k] for k in ('x','y','confidence')}
            identity=p.get('identity') or {}
            rows.append(dict(track=str(p['id']),label='人物 '+str(labels[key]),box={k:box[k] for k in ('x','y','w','h')},
                joints=joints,person_id=identity.get('person_id') if identity.get('state') in {'matched','tracked'} else None))
        self.observations[camera]=dict(at_ms=int(at*1000),people=rows)

    def frame(self, camera):
        value = self.frames.get(camera)
        if not value or not 0 <= self.clock()-value[1] < 2:return None
        return value

    def still(self,camera):
        value=self.stills.get(camera)
        return value if value and 0<=self.clock()-value[1]<2 else None

    def view(self, states):
        result = []
        for camera in self.cameras:
            status = states.get(camera['id'], {})
            source = status.get('state', 'connecting')
            if source in {'shutter_closed', 'stopped'}:continue
            frame = self.frame(camera['id'])
            recognition = status.get('recognition') == 'running'
            result.append(dict(camera, state='live' if frame else 'connecting',
                frame_age_ms=round((self.clock()-frame[1])*1000) if frame else None,
                recognition='running' if recognition else 'waiting',
                people=status.get('people', 0) if recognition else 0,
                hands=status.get('hands', 0) if recognition else 0,
                toast=self.toasts.get(camera['id']) if self.toasts.get(camera['id']) and 0<=self.clock()*1000-self.toasts[camera['id']]['at_ms']<3000 else None,
                overlay=self.observations.get(camera['id']) if recognition and self.observations.get(camera['id'])
                    and 0 <= self.clock()*1000-self.observations[camera['id']]['at_ms'] <= 1500 else None))
        return dict(version=1, server_time_ms=int(self.clock()*1000), cameras=result, configured=len(self.cameras))
