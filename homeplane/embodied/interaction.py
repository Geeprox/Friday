"""Configurable, session-bound gesture and calibrated image-ray controls.

Display floor-plan geometry never becomes pointing evidence. Targets must be
marked in the active camera image; a held ray selects, an OK confirms once.
"""
import json
import math
import os
import time
from pydantic import BaseModel, ConfigDict, Field
from .paths import PRIVATE
from .smart_home import WindowGesture

GESTURES={'body.arms_open':'双臂打开','body.arms_close':'双臂合上','body.hands_up':'双手举高','body.left_hand_up':'举起左手','body.right_hand_up':'举起右手','body.squat':'下蹲','body.lean_left':'身体向左倾','body.lean_right':'身体向右倾','hand.open_palm':'张开手掌','hand.yeah':'比出 Yeah','hand.ok':'OK'}
ACTIONS={'on','off','open','close','position:0','position:25','position:50','position:75','position:100','mode:cool','mode:heat','mode:off','mode:auto','mode:heat_cool','mode:dry','mode:fan_only'}

class Binding(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:str=Field(pattern=r'^[a-f0-9]{32}$')
    # Only image-ray targets are camera scoped. Old body bindings containing a
    # camera continue to work, now throughout the home, without duplicating them.
    camera:str|None=Field(default=None,pattern=r'^cam_[a-f0-9]{10}$')
    gesture:str
    target:str=Field(min_length=1,max_length=256)
    action:str
    enabled:bool=False
    anchor:list[float]|None=Field(default=None,min_length=2,max_length=2)

class Configuration(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision:int=Field(ge=0)
    enabled:bool=False
    bindings:list[Binding]=Field(default_factory=list,max_length=48)


def load():
    try:
        p=PRIVATE/'interactions.json'
        if p.is_symlink()or p.stat().st_size>32768:raise ValueError('invalid_interactions')
        return Configuration.model_validate_json(p.read_text())
    except FileNotFoundError:
        return Configuration(revision=0)


def save(edit,targets,cameras):
    current=load()
    if edit.revision!=current.revision:raise ValueError('interaction_config_changed')
    seen=set()
    for b in edit.bindings:
        if b.id in seen or b.target not in targets or b.camera is not None and b.camera not in cameras or b.action not in ACTIONS:
            raise ValueError('invalid_interaction_binding')
        seen.add(b.id)
        if b.gesture=='hand.point':
            if b.camera not in cameras or b.anchor is None or any(not math.isfinite(v)or not 0<=v<=1 for v in b.anchor):
                raise ValueError('pointing_calibration_required')
        elif b.gesture not in GESTURES or b.anchor is not None:
            raise ValueError('invalid_gesture')
        from .household import supported_actions
        if b.action not in supported_actions(targets[b.target]):
            raise ValueError('unsupported_binding_action')
    edit=edit.model_copy(update={'revision':current.revision+1})
    PRIVATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    p=PRIVATE/'interactions.json';temp=p.with_suffix('.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w')as f:f.write(edit.model_dump_json())
    os.replace(temp,p)
    return edit


def ensure_defaults(targets, cameras):
    """Bindings are chosen by each household; discovery never enables actions."""
    return load()


def body_gestures(person):
    """Body-relative signals from confident joints in the unmirrored image."""
    joints=person.get('joints',{})
    def q(name):
        p=joints.get(name)
        return p if p and p.get('confidence',0)>=.5 else None
    ls,rs,lh,rh=[q(k) for k in ('leftShoulder','rightShoulder','leftHip','rightHip')]
    if not all((ls,rs,lh,rh)):return set()
    cy=(ls['y']+rs['y'])/2;hy=(lh['y']+rh['y'])/2
    torso=hy-cy
    if torso<.04:return set()
    lw,rw=q('leftWrist'),q('rightWrist')
    left=lw and lw['y']<ls['y']-torso*.45
    right=rw and rw['y']<rs['y']-torso*.45
    result=set()
    if left and right:result.add('body.hands_up')
    elif lw and rw:
        if left and rw['y']>rs['y']:result.add('body.left_hand_up')
        if right and lw['y']>ls['y']:result.add('body.right_hand_up')
    lean=((ls['x']+rs['x'])-(lh['x']+rh['x']))/2/torso
    if lean<-.35:result.add('body.lean_left')
    if lean>.35:result.add('body.lean_right')
    from .pose_rules import angle
    legs=[[q(s+k) for k in ('Hip','Knee','Ankle')] for s in ('left','right')]
    if all(all(leg) for leg in legs):
        angles=[angle(*leg) for leg in legs]
        if all(a is not None and 60<a<135 for a in angles) and all(abs(leg[0]['y']-leg[1]['y'])<torso*.65 for leg in legs):result.add('body.squat')
    return result


class InteractionEngine:
    def __init__(self,camera,session):
        self.camera,self.session=camera,session
        self.arms=WindowGesture();self.config=Configuration(revision=0)
        self.loaded=-100.;self.owner=None;self.holds={};self.last={};self.pending=None;self.sequence=0
        self.latched=set();self.released={};self.observed=None
        self.public={'enabled':False,'selection':None,'reason':'not_enabled'}

    def reset(self):
        self.arms.reset();self.holds.clear();self.pending=None;self.owner=None
        self.latched.clear();self.released.clear();self.observed=None

    def update(self,people,hands,owner,registration,active,now):
        if now-self.loaded>1:
            try:
                next_config=load()
                if next_config.revision!=self.config.revision:self.reset()
                self.config=next_config
            except (OSError,ValueError):self.config=Configuration(revision=0)
            self.loaded=now
        if not self.config.enabled or not active or registration.get('status') in {'moving','settling','lost','stale'} or owner is None:
            self.reset();self.public={'enabled':False,'selection':None,'reason':'bind_operator'if active else 'not_enabled'};return []
        if owner!=self.owner:self.reset();self.owner=owner
        if self.observed is not None and now-self.observed>1:
            self.reset();self.owner=owner
        self.observed=now
        person=next((p for p in people if p['id']==owner),None)
        if person is None:self.reset();return []
        bindings=[b for b in self.config.bindings if b.enabled and (b.gesture!='hand.point' or b.camera==self.camera)]
        labels={h.get('gesture')for h in hands if h.get('person_id')==owner}
        event=self.arms.update(person,now)
        gestures={'body.arms_'+event['action'] if event else ''}
        gestures.update('hand.'+x for x in labels if x in {'ok','yeah','open_palm'})
        gestures.update(body_gestures(person))
        result=[]
        pointing_ready=registration.get('status')=='locked' and not registration.get('compensated',False)
        if not pointing_ready:self.pending=None
        for b in bindings:
            if b.gesture=='hand.point':continue
            key=(owner,b.id)
            if b.gesture not in gestures:
                self.holds.pop(key,None)
                self.released.setdefault(key,now)
                if now-self.released[key]>=.25:self.latched.discard(key)
                continue
            self.released.pop(key,None)
            self.holds.setdefault(key,now)
            moving_arms=b.gesture in {'body.arms_open','body.arms_close'}
            if key not in self.latched and (moving_arms or now-self.holds[key]>=.7) and now-self.last.get(key,-100)>3:
                self.last[key]=now;self.latched.add(key);result.append(b)
        candidates=[]
        for hand in hands:
            if not pointing_ready or hand.get('person_id')!=owner or hand.get('gesture')!='pointing' or not hand.get('ray'):continue
            ray=hand['ray'];ox,oy=ray['origin'];dx,dy=ray['direction'];norm=math.hypot(dx,dy)
            if norm<.001:continue
            for b in bindings:
                if b.gesture!='hand.point':continue
                tx,ty=b.anchor[0]-ox,b.anchor[1]-oy;dist=math.hypot(tx,ty)
                if dist<.045 or tx*dx+ty*dy<=0:continue
                angle=abs(tx*dy-ty*dx)/norm/dist
                if angle<.09:candidates.append((angle,b))
        candidates.sort(key=lambda x:x[0])
        unique=candidates and (len(candidates)==1 or candidates[1][0]-candidates[0][0]>.035)
        if unique:
            b=candidates[0][1]
            key=('ray',owner,b.id)
            self.holds.setdefault(key,now)
            if now-self.holds[key]>=.8:self.pending=(b,now+3)
            for k in list(self.holds):
                if k[0]=='ray'and k!=key:self.holds.pop(k,None)
        else:
            for k in list(self.holds):
                if k[0]=='ray':self.holds.pop(k,None)
        if self.pending and now>self.pending[1]:self.pending=None
        if self.pending and 'ok'in labels:
            b=self.pending[0];key=('point',owner,b.id)
            if now-self.last.get(key,-100)>3:self.last[key]=now;result.append(b)
            self.pending=None
        self.public={'enabled':True,'selection':self.pending[0].target if self.pending else None,'reason':'confirm_ok'if self.pending else 'ready'}
        events=[]
        for b in result:
            self.sequence+=1
            events.append(dict(revision=self.config.revision,binding=b.id,camera=self.camera,session=self.session,track=owner,target=b.target,action=b.action,event_id=f'interaction:{self.session}:{owner}:{self.sequence}',observed_mono=now))
        return events
