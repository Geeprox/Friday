"""Local, explicitly enrolled face identities shared by every camera worker.

PeopleCatalog owns names and person IDs. This private database owns only face
templates, face-only candidate portraits and bounded last-seen metadata.
No full frames, audio, or automatic naming. Matching is similarity
evidence, never action authority.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import queue
import secrets
import sqlite3
import stat
import threading
import time
from collections import deque
from contextlib import nullcontext
from functools import wraps
from pathlib import Path

from homeplane.recognition.artifacts import SFACE_SHA256
from homeplane.recognition.contracts import Embedding
from homeplane.runtime_log import observe

REF = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
POLICY = "sface-household-experimental-v1"
THRESHOLD, MARGIN = 0.65, 0.08


async def analyze(pipeline, *args, **kwargs):
    """Cancellation waits for the native call before its owner closes models/DB."""
    task = asyncio.create_task(asyncio.to_thread(pipeline.process, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def similarity(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True)) / (math.hypot(*a) * math.hypot(*b))


def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._gate:
            return method(self, *args, **kwargs)
    return call


class IdentityStore:
    """One owner-local store; cross-process writers use SQLite transactions."""

    def __init__(self, path: Path, *, clock=time.time):
        self.clock, self.lock = clock, threading.RLock()
        self._candidate_jobs = None
        self._candidate_thread = None
        self._candidate_stop = threading.Event()
        path = Path(path).absolute()
        self._candidate_log_path=path.parent/'identity-logs'
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("identity_private_path_required")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = path.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise ValueError("identity_private_directory_required")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 16 * 1024 * 1024):
                raise ValueError("identity_private_database_required")
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self.path = path
        with self.db:
            self.db.execute("PRAGMA secure_delete=ON")
            self.db.execute("CREATE TABLE IF NOT EXISTS face_templates "
                            "(person TEXT PRIMARY KEY, model TEXT NOT NULL, vectors TEXT NOT NULL, updated REAL NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS presence "
                            "(camera TEXT NOT NULL, session TEXT NOT NULL, track TEXT NOT NULL, person TEXT, "
                            "state TEXT NOT NULL, seen REAL NOT NULL, facing INTEGER, "
                            "PRIMARY KEY(camera,session,track))")
            self.db.execute("CREATE TABLE IF NOT EXISTS identity_aliases "
                            "(person TEXT PRIMARY KEY, canonical TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS face_portraits "
                            "(person TEXT PRIMARY KEY, jpeg BLOB NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS identity_labels "
                            "(person TEXT PRIMARY KEY, name TEXT NOT NULL)")
            self.db.execute('CREATE TABLE IF NOT EXISTS face_candidates '
                '(id TEXT PRIMARY KEY, model TEXT NOT NULL, vectors TEXT NOT NULL, jpeg BLOB NOT NULL, '
                'first REAL NOT NULL, seen REAL NOT NULL, observations INTEGER NOT NULL, quality REAL NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS face_candidate_links (candidate TEXT PRIMARY KEY, leader TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS face_candidate_separate (left_id TEXT NOT NULL, right_id TEXT NOT NULL, PRIMARY KEY(left_id,right_id))')
            self.db.execute('CREATE TABLE IF NOT EXISTS face_candidate_undo (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
            self.db.execute('DELETE FROM face_candidates WHERE seen<?',(self.clock()-30*86400,))
        self._candidate_pruned_at=self.clock()

    def close(self):
        with self.lock:self._candidate_stop.set()
        if self._candidate_thread:self._candidate_thread.join()
        if self._candidate_jobs:
            while True:
                try:self._candidate_jobs.get_nowait();self._candidate_jobs.task_done()
                except queue.Empty:break
        with self.lock:
            self.db.close()

    def submit_candidate(self, embeddings, portrait, quality):
        """Copy bounded evidence to one background collector, never block vision."""
        if type(portrait) is not bytes or len(portrait)>262144:return
        job=(tuple(embeddings),portrait,float(quality),self.clock())
        with self.lock:
            if self._candidate_stop.is_set():return
            if self._candidate_jobs is None:
                self._candidate_jobs=queue.Queue(maxsize=4)
                self._candidate_thread=threading.Thread(target=self._collect_candidates,daemon=True)
                self._candidate_thread.start()
            try:self._candidate_jobs.put_nowait(job)
            except queue.Full:pass

    def _collect_candidates(self):
        from homeplane.runtime_log import RuntimeLog
        log=RuntimeLog(self._candidate_log_path)
        while not self._candidate_stop.is_set():
            try:job=self._candidate_jobs.get(timeout=.2)
            except queue.Empty:
                try:self.expire_candidates()
                except sqlite3.Error:log.record('people.collect','result',result='unavailable',code='runtime_error')
                continue
            try:
                correlation=secrets.token_hex(16);started=time.monotonic()
                if 0<=self.clock()-job[3]<5:
                    log.record('people.collect','start',correlation=correlation)
                    accepted=self.collect_candidate(*job)
                    log.record('people.collect','result',correlation=correlation,
                        result='refused' if accepted is False else 'ok',
                        code='request_budget_exceeded' if accepted is False else 'none',
                        elapsed_ms=(time.monotonic()-started)*1000)
            except (ValueError,sqlite3.Error):
                log.record('people.collect','result',result='unavailable',code='runtime_error',correlation=correlation,elapsed_ms=(time.monotonic()-started)*1000)
            finally:self._candidate_jobs.task_done()

    def collect_candidate(self, embeddings, portrait, quality, observed):
        if (not 1<=len(embeddings)<=5 or type(portrait) is not bytes or not 4<=len(portrait)<=262144
                or not portrait.startswith(b'\xff\xd8') or not portrait.endswith(b'\xff\xd9')
                or type(quality) not in (int,float) or not math.isfinite(quality) or quality<=0
                or type(observed) not in (int,float) or not math.isfinite(observed)
                or not 0<=self.clock()-observed<5):
            raise ValueError('identity_samples_required')
        if any(not isinstance(e,Embedding) or e.simulated or e.modality!='face' or e.model_sha256!=SFACE_SHA256 for e in embeddings):
            raise ValueError('identity_model_mismatch')
        values=[e.values for e in embeddings]
        if any(similarity(a,b)<THRESHOLD for a in values for b in values):raise ValueError('identity_samples_inconsistent')
        with self.lock,self.db:
            self.db.execute('DELETE FROM face_candidates WHERE seen<?',(self.clock()-30*86400,))
            # Recheck after queuing: a newly named member must not leave a new
            # anonymous duplicate while the asynchronous collector catches up.
            if any(self.match(e)[0] is not None for e in embeddings):return
            candidates=list(self.db.execute('SELECT id,vectors,quality,seen FROM face_candidates WHERE model=?',(SFACE_SHA256,)))
            scored=sorted([(min(sorted((similarity(v,t) for t in json.loads(raw)),reverse=True)[min(1,len(json.loads(raw))-1)]
                for v in values),cid,old_quality) for cid,raw,old_quality,seen in candidates],reverse=True)
            matches=[s for s in scored if s[0]>=.72]
            if len(matches)>1 and matches[0][0]-matches[1][0]<MARGIN:return
            if matches:
                _,cid,old_quality=matches[0]
                _,raw,_,last=next(r for r in candidates if r[0]==cid)
                if observed-last<.4:return
                old_values=json.loads(raw)
                merged=(old_values+values)[-5:]
                if any(similarity(a,b)<THRESHOLD for a in merged for b in merged):return
                self.db.execute('UPDATE face_candidates SET seen=?,observations=observations+1,vectors=? WHERE id=?',(observed,json.dumps(merged),cid))
                if quality>old_quality:
                    total=self.db.execute('SELECT coalesce(sum(length(jpeg)),0) FROM face_candidates WHERE id!=?',(cid,)).fetchone()[0]
                    total+=self.db.execute('SELECT coalesce(sum(length(jpeg)),0) FROM face_portraits').fetchone()[0]
                    if total+len(portrait)<=6*1024*1024:
                        self.db.execute('UPDATE face_candidates SET jpeg=?,quality=? WHERE id=?',(portrait,quality,cid))
            else:
                victim=None
                if len(candidates)>=32:
                    # Old, never-presented fragments must not occupy the whole
                    # gallery for30 days. Visible candidates and registered
                    # members are never evicted to make room for a new face.
                    victim=self.db.execute('SELECT id,length(jpeg) FROM face_candidates '
                        'WHERE observations<3 AND seen<? ORDER BY observations,seen LIMIT 1',
                        (observed-60,)).fetchone()
                    if victim is None:return False
                total=self.db.execute('SELECT coalesce(sum(length(jpeg)),0) FROM face_candidates').fetchone()[0]
                total+=self.db.execute('SELECT coalesce(sum(length(jpeg)),0) FROM face_portraits').fetchone()[0]
                if total-(victim[1] if victim else 0)+len(portrait)>6*1024*1024:return False
                if victim:self.db.execute('DELETE FROM face_candidates WHERE id=?',(victim[0],))
                cid='candidate_'+secrets.token_hex(12)
                self.db.execute('INSERT INTO face_candidates VALUES (?,?,?,?,?,?,?,?)',
                    (cid,SFACE_SHA256,json.dumps(values),portrait,observed,observed,1,quality))

    def expire_candidates(self):
        with self.lock,self.db:
            if 0<=self.clock()-self._candidate_pruned_at<60:return
            self.db.execute('DELETE FROM face_candidates WHERE seen<?',(self.clock()-30*86400,))
            self._candidate_pruned_at=self.clock()

    def candidates(self):
        self.expire_candidates()
        with self.lock:
            return [dict(id=cid,first_seen=first,last_seen=seen,observations=n,portrait_revision=quality)
                for cid,first,seen,n,quality in self.db.execute('SELECT id,first,seen,observations,quality FROM face_candidates WHERE seen>=? AND observations>=3 AND seen-first>=.8 ORDER BY seen DESC',
                    (self.clock()-30*86400,))]

    def candidate_photo(self,candidate):
        with self.lock:
            row=self.db.execute('SELECT jpeg FROM face_candidates WHERE id=? AND seen>=?',(candidate,self.clock()-30*86400)).fetchone()
            return bytes(row[0]) if row else None

    def candidate_groups(self):
        from .candidate_groups import groups
        visible=self.candidates()
        with self.lock:
            vectors=dict(self.db.execute('SELECT id,vectors FROM face_candidates WHERE model=?',(SFACE_SHA256,)))
            links={}
            for candidate,leader in self.db.execute('SELECT candidate,leader FROM face_candidate_links'):
                links.setdefault(leader,[]).append(candidate)
            separate=list(self.db.execute('SELECT left_id,right_id FROM face_candidate_separate'))
            return groups([dict(row,vectors=json.loads(vectors[row['id']])) for row in visible if row['id'] in vectors],
                          joined=links.values(),separate=separate)

    def edit_candidate_group(self,members,*,separate=False):
        if len(set(members))!=len(members) or not 2<=len(members)<=8:
            raise ValueError('identity_candidate_unavailable')
        with self.lock,self.db:
            visible={row['id'] for row in self.candidates()}
            if not set(members)<=visible:raise ValueError('identity_candidate_unavailable')
            before=dict(links=list(self.db.execute('SELECT candidate,leader FROM face_candidate_links')),
                        separate=list(self.db.execute('SELECT left_id,right_id FROM face_candidate_separate')))
            self.db.execute('INSERT OR REPLACE INTO face_candidate_undo VALUES (1,?)',(json.dumps(before),))
            for member in members:self.db.execute('DELETE FROM face_candidate_links WHERE candidate=?',(member,))
            for left in members:
                for right in members:
                    if left>=right:continue
                    self.db.execute('DELETE FROM face_candidate_separate WHERE left_id=? AND right_id=?',(left,right))
                    if separate:self.db.execute('INSERT INTO face_candidate_separate VALUES (?,?)',(left,right))
            if not separate:
                self.db.executemany('INSERT OR REPLACE INTO face_candidate_links VALUES (?,?)',[(member,min(members)) for member in members])

    def undo_candidate_group(self):
        with self.lock,self.db:
            row=self.db.execute('SELECT body FROM face_candidate_undo WHERE id=1').fetchone()
            if not row:raise ValueError('identity_candidate_undo_unavailable')
            before=json.loads(row[0]);self.db.execute('DELETE FROM face_candidate_links');self.db.execute('DELETE FROM face_candidate_separate')
            self.db.executemany('INSERT INTO face_candidate_links VALUES (?,?)',before['links'])
            self.db.executemany('INSERT INTO face_candidate_separate VALUES (?,?)',before['separate'])
            self.db.execute('DELETE FROM face_candidate_undo')

    def group_undo_available(self):
        with self.lock:return bool(self.db.execute('SELECT 1 FROM face_candidate_undo').fetchone())

    def promote_candidate(self,candidate,person):
        with self.lock:
            row=self.db.execute('SELECT model,vectors,jpeg FROM face_candidates WHERE id=? AND seen>=?',
                (candidate,self.clock()-30*86400)).fetchone()
            if not row or row[0]!=SFACE_SHA256:raise ValueError('identity_candidate_unavailable')
            embeddings=[Embedding('face',SFACE_SHA256,tuple(v),False) for v in json.loads(row[1])]
            self.enroll(person,embeddings,portrait=bytes(row[2]))
            with self.db:self.db.execute('DELETE FROM face_candidates WHERE id=?',(candidate,))

    @observe('people.enroll')
    def name_candidate(self,catalog,candidate,name,revision,person=None,*,members=None,group_revision=None):
        """One SQLite transaction covers naming, face persistence and promotion."""
        with catalog._lock,self.lock:
            members=[candidate] if members is None else members
            if not 1<=len(members)<=8 or len(set(members))!=len(members) or candidate not in members:
                raise ValueError('identity_candidate_unavailable')
            if len(members)>1:
                group=next((g for g in self.candidate_groups() if g['id']==candidate),None)
                if not group or group['members']!=members or group['group_revision']!=group_revision:
                    raise ValueError('identity_candidate_unavailable')
            connection=catalog.connection
            connection.execute('ATTACH DATABASE ? AS live_identity',(str(self.path),))
            try:
                def persist(pid,transaction):
                    rows=[];embeddings=[]
                    for member in members:
                        row=transaction.execute('SELECT model,vectors,jpeg,quality FROM live_identity.face_candidates '
                            'WHERE id=? AND seen>=? AND observations>=3 AND seen-first>=.8',
                            (member,self.clock()-30*86400)).fetchone()
                        if not row or row[0]!=SFACE_SHA256:raise ValueError('identity_candidate_unavailable')
                        values=json.loads(row[1])
                        if len(values)<3 or any(similarity(a,b)<THRESHOLD for a in values for b in values):
                            raise ValueError('identity_samples_inconsistent')
                        embeddings.extend(Embedding('face',SFACE_SHA256,tuple(v),False) for v in values[-3:]);rows.append(row)
                    # Remove inside this transaction so the portrait budget does
                    # not count the same face twice. A failed enrollment rolls back.
                    transaction.executemany('DELETE FROM live_identity.face_candidates WHERE id=?',[(member,) for member in members])
                    self.enroll(pid,embeddings,portrait=bytes(max(rows,key=lambda row:row[3])[2]),_transaction=transaction,_confirmed_group=len(members)>1)
                    transaction.execute('INSERT OR REPLACE INTO live_identity.identity_labels VALUES (?,?)',(pid,name.strip()))
                return catalog.register_profile(name,revision,person,_before_commit=persist)
            finally:
                connection.execute('DETACH DATABASE live_identity')

    def enroll(self, person, embeddings, *, portrait=None, _transaction=None,_confirmed_group=False):
        if not REF.fullmatch(person) or not 3 <= len(embeddings) <= (24 if _confirmed_group else 5):
            raise ValueError("identity_samples_required")
        if any(not isinstance(e, Embedding) or e.modality != "face" or e.simulated
               or e.model_sha256 != SFACE_SHA256 for e in embeddings):
            raise ValueError("identity_model_mismatch")
        if not _confirmed_group and any(similarity(a.values, b.values) < THRESHOLD for a in embeddings for b in embeddings):
            raise ValueError("identity_samples_inconsistent")
        if portrait is not None and (type(portrait) is not bytes or not 4<=len(portrait)<=262144
                or not portrait.startswith(b'\xff\xd8') or not portrait.endswith(b'\xff\xd9')):
            raise ValueError('identity_portrait_invalid')
        db=self.db if _transaction is None else _transaction
        prefix='' if _transaction is None else 'live_identity.'
        with self.lock, self.db if _transaction is None else nullcontext():
            previous = db.execute(f"SELECT model,vectors FROM {prefix}face_templates WHERE person=?", (person,)).fetchone()
            values = [e.values for e in embeddings]
            if previous:
                if previous[0] != SFACE_SHA256:
                    raise ValueError("identity_model_mismatch")
                old = json.loads(previous[1])
                if not _confirmed_group and any(max(similarity(value, target) for target in old) < THRESHOLD for value in values):
                    raise ValueError("identity_samples_inconsistent")
                # Adding a view in another room preserves two earlier views;
                # it must not silently replace the only evidence from that room.
                values = values+old[-6:] if _confirmed_group else values[-3:]+old[-2:]
            if db.execute(f"SELECT count(*) FROM {prefix}face_templates").fetchone()[0] >= 128:
                if not db.execute(f"SELECT 1 FROM {prefix}face_templates WHERE person=?", (person,)).fetchone():
                    raise ValueError("identity_capacity")
            db.execute(f"INSERT OR REPLACE INTO {prefix}face_templates VALUES (?,?,?,?)",
                            (person, SFACE_SHA256, json.dumps(values), self.clock()))
            if portrait is not None:
                total=db.execute(f'SELECT coalesce(sum(length(jpeg)),0) FROM {prefix}face_candidates').fetchone()[0]
                total+=db.execute(f'SELECT coalesce(sum(length(jpeg)),0) FROM {prefix}face_portraits WHERE person!=?',(person,)).fetchone()[0]
                if total+len(portrait)>8*1024*1024:raise ValueError('identity_capacity')
                db.execute(f'INSERT OR REPLACE INTO {prefix}face_portraits VALUES (?,?)',(person,portrait))

    def portrait(self,person):
        with self.lock:
            row=self.db.execute('SELECT jpeg FROM face_portraits WHERE person=?',(person,)).fetchone()
            return bytes(row[0]) if row else None

    def labels(self):
        with self.lock:return dict(self.db.execute('SELECT person,name FROM identity_labels'))

    def forget(self, person):
        with self.lock, self.db:
            aliases = [p for (p,) in self.db.execute('SELECT person FROM identity_aliases WHERE canonical=?', (person,))]
            for alias in aliases:
                self.db.execute('DELETE FROM face_portraits WHERE person=?',(alias,))
                self.db.execute('DELETE FROM face_templates WHERE person=?', (alias,))
                self.db.execute('DELETE FROM presence WHERE person=?', (alias,))
            self.db.execute('DELETE FROM identity_aliases WHERE person=? OR canonical=?', (person, person))
            self.db.execute("DELETE FROM face_templates WHERE person=?", (person,))
            self.db.execute('DELETE FROM face_portraits WHERE person=?',(person,))
            self.db.execute("DELETE FROM presence WHERE person=?", (person,))

    def registered(self):
        with self.lock:
            aliases = dict(self.db.execute("SELECT person,canonical FROM identity_aliases"))
            return {aliases.get(p, p): t for p, t in self.db.execute("SELECT person,updated FROM face_templates")}

    def sync_people(self, people):
        """Consume explicit PeopleCatalog merges; never cluster/name a visitor.

        Keep original templates so catalog undo can restore separate identities.
        Canonicalization happens at match/observation time, independent of UI reads.
        """
        rows = {p.person_id: p for p in people}
        aliases = {}
        for pid in rows:
            current, visited = pid, set()
            while current in rows and rows[current].merged_into and current not in visited:
                visited.add(current)
                current = rows[current].merged_into
            if current in rows and current not in visited and current != pid:
                aliases[pid] = current
        with self.lock, self.db:
            self.db.execute("DELETE FROM identity_aliases")
            self.db.executemany("INSERT INTO identity_aliases VALUES (?,?)", aliases.items())
            self.db.execute('DELETE FROM identity_labels')
            self.db.executemany('INSERT INTO identity_labels VALUES (?,?)',
                [(p.person_id,p.name) for p in people if p.name and not p.merged_into])

    def match(self, embedding):
        if (not isinstance(embedding, Embedding) or embedding.modality != "face"
                or embedding.simulated or embedding.model_sha256 != SFACE_SHA256):
            return None, "unavailable"
        with self.lock:
            rows = list(self.db.execute("SELECT person,vectors FROM face_templates WHERE model=?", (SFACE_SHA256,)))
            aliases = dict(self.db.execute("SELECT person,canonical FROM identity_aliases"))
        scores_by_person = {}
        for person, body in rows:
            values = json.loads(body)
            # At least two independent enrollment views must agree.
            score = sorted((similarity(embedding.values, v) for v in values), reverse=True)[1]
            person = aliases.get(person, person)
            scores_by_person[person] = max(scores_by_person.get(person, -1), score)
        scores = [(score, person) for person, score in scores_by_person.items()]
        scores.sort(reverse=True)
        if not scores or scores[0][0] < THRESHOLD:
            return None, "unknown"
        if len(scores) > 1 and scores[0][0] - scores[1][0] < MARGIN:
            return None, "ambiguous"
        return scores[0][1], "candidate"

    def observe(self, camera, session, track, person, state, facing=None, *, source_age=None):
        if any(not REF.fullmatch(str(x)) for x in (camera, session, track)):
            raise ValueError("identity_source_invalid")
        if state not in {"unknown", "ambiguous", "candidate", "matched", "tracked"}:
            raise ValueError("identity_state_invalid")
        def age():
            value = source_age() if source_age is not None else 0.
            return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1 else None
        with self.lock:
            with self.db:
                # Acquire the SQLite write lock before checking the source;
                # lock contention must not turn an old frame into a new sighting.
                self.db.execute("DELETE FROM presence WHERE seen<?", (self.clock() - 86400,))
                elapsed = age()
                if elapsed is None:
                    self.db.execute("UPDATE presence SET state='unknown',facing=NULL WHERE camera=? AND session=? AND track=?",
                                    (camera, session, str(track)))
                    return False
                self.db.execute("INSERT OR REPLACE INTO presence VALUES (?,?,?,?,?,?,?)",
                                (camera, session, str(track), person if state in {"matched", "tracked"} else None,
                                 state, self.clock() - elapsed, None if facing is None else int(facing)))
                self.db.execute("DELETE FROM presence WHERE rowid NOT IN "
                                "(SELECT rowid FROM presence ORDER BY seen DESC LIMIT 256)")
            if age() is None:
                # Commit/fsync may also have waited. Keep last-seen history,
                # but retract current presence before releasing this owner lock.
                with self.db:
                    self.db.execute("UPDATE presence SET state='unknown',facing=NULL WHERE camera=? AND session=? AND track=?",
                                    (camera, session, str(track)))
                return False
        return True

    def clear_camera(self, camera, session):
        with self.lock, self.db:
            # Retain last-seen history, but never expose an ended session as live.
            self.db.execute("UPDATE presence SET state='unknown',facing=NULL WHERE camera=? AND session=?",
                            (camera, session))

    def clear_tracks(self, camera, session, tracks):
        with self.lock,self.db:
            self.db.executemany("UPDATE presence SET state='unknown',facing=NULL WHERE camera=? AND session=? AND track=?",
                                [(camera,session,str(track)) for track in tracks])

    def presence(self):
        with self.lock:
            rows = list(self.db.execute("SELECT camera,session,track,person,state,seen,facing "
                                        "FROM presence ORDER BY seen DESC LIMIT 256"))
        now = self.clock()
        return [dict(camera=c, session=s, track=t, person_id=p, state=st, last_seen=seen,
                     present=st in {"matched", "tracked"} and 0 <= now - seen <= 2.5,
                     facing=bool(f) if f is not None and 0 <= now - seen <= 1 else None)
                for c, s, t, p, st, seen, f in rows]


class FaceIdentityPipeline:
    """Low-rate face recognition fused with current body tracks/orientation.

    Samples stay in memory until explicit enrollment. Lost/recycled tracks need
    fresh face evidence; neither a body ID nor camera motion transfers identity.
    """

    def __init__(self, store, camera, session, models, *, clock=time.monotonic):
        self.store, self.camera, self.session = store, camera, session
        self.models, self.clock = Path(models), clock
        self.provider = None
        self.reason = "not_started"
        self.last = -100.0
        self.samples, self.votes, self.results = {}, {}, {}
        self.portraits = {}
        self.candidate_submitted = {}
        self._published = ()
        self._face_tracks, self._next_face = {}, 0
        self._gate = threading.RLock()
        self._diagnostics={key:0 for key in ('frames','moving','no_body','waiting_highres','rate_limited',
            'detected_faces','ambiguous_body','quality_rejected','embeddings','unknown','ambiguous_identity',
            'candidate','matched','carried','unavailable','source_rejected')}

    def _count(self,key,amount=1):
        self._diagnostics[key]=min(1000000000,self._diagnostics[key]+amount)

    def diagnostics(self):
        return dict(self._diagnostics)

    def _fresh(self, observed):
        if 0 <= self.clock()-observed <= 1:
            return True
        self._count('source_rejected')
        self.discard()
        return False

    def _observe(self, observed, track, person, state, facing=None):
        if not self._fresh(observed):
            return False
        accepted = self.store.observe(self.camera, self.session, track, person, state, facing,
                                      source_age=lambda: self.clock()-observed)
        if accepted is False:
            self._count('source_rejected')
            self.discard()
            return False
        return self._fresh(observed)

    def activate(self):
        from homeplane.recognition.artifacts import YUNET_SHA256, LocalArtifact
        from homeplane.recognition.face import OpenCVFaceProvider
        self.provider = OpenCVFaceProvider(
            LocalArtifact(self.models / "face_detection_yunet_2023mar.onnx", YUNET_SHA256),
            LocalArtifact(self.models / "face_recognition_sface_2021dec.onnx", SFACE_SHA256), enabled=True)
        status = self.provider.activate()
        self.reason = status.reason
        return status.state == "ready"

    @serialized
    def discard(self):
        """Revoke stale observations without destroying the resident model."""
        self.store.clear_camera(self.camera, self.session)
        self.samples.clear()
        self.portraits.clear()
        self.votes.clear()
        self.results.clear()
        self._published = ()
        self.last = -100.0
        self._face_tracks.clear()

    @serialized
    def close(self):
        self.discard()
        if self.provider:
            self.provider.close()
            self.provider = None

    @staticmethod
    def _bodies(box, people):
        x, y = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
        return [p for p in people if p["box"]["x"] <= x <= p["box"]["x"] + p["box"]["w"]
                    and p["box"]["y"] <= y <= p["box"]["y"] + p["box"]["h"] * 0.65]

    @staticmethod
    def _body(box, people):
        possible=FaceIdentityPipeline._bodies(box,people)
        return str(possible[0]["id"]) if len(possible) == 1 else None

    @staticmethod
    def _face_views(image, people):
        """Use original head pixels instead of shrinking every face with the room.

        Each model input retains its own pixel/digest contract. Regions overlap
        only as search areas; global unique-body association still decides use.
        """
        import cv2
        import numpy as np
        height,width=image.shape[:2]
        regions=[]
        if people is None:
            regions=[(0,0,width,height,None)]
        elif len(people)<=8 and len({str(p['id']) for p in people})==len(people):
            for body in people:
                box=body.get('box',{})
                values=[box.get(k) for k in ('x','y','w','h')]
                if any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in values):continue
                x,y,w,h=values
                if not (0<=x<1 and 0<=y<1 and 0<w<=1 and 0<h<=1):continue
                left=max(0,int((x-w*.15)*width));top=max(0,int((y-h*.08)*height))
                right=min(width,int((x+w*1.15)*width));bottom=min(height,int((y+h*.65)*height))
                if right>left and bottom>top:regions.append((left,top,right,bottom,str(body['id'])))
        for left,top,right,bottom,owner in regions:
            crop=image[top:bottom,left:right]
            scale=min(1,1280/crop.shape[1],720/crop.shape[0])
            if scale<1:crop=cv2.resize(crop,(max(1,round(crop.shape[1]*scale)),max(1,round(crop.shape[0]*scale))))
            crop=np.ascontiguousarray(crop)
            yield crop,(left,top,(right-left)/crop.shape[1],(bottom-top)/crop.shape[0]),owner

    @serialized
    def process(self, jpeg, people=None, faces=(), *, moving=False, sample=True, captured_at=None):
        now = self.clock()
        self._count('frames')
        # This mode requires a unique body association for every face. An
        # explicitly empty body set cannot yield an identity; skip native work
        # and revoke old observations. None keeps standalone-face mode intact.
        if moving or (people is not None and not people):
            self._count('moving' if moving else 'no_body')
            self.samples.clear()
            self.portraits.clear()
            self.votes.clear()
            self.results.clear()
            self._published = ()
            self._face_tracks.clear()
            self.store.clear_camera(self.camera, self.session)
            return []
        if not sample:
            self._count('waiting_highres')
            # 4K face frames have a lower cadence than body frames. A small
            # preview between them must not erase a valid high-res sample or
            # advance its clock. Departed body tracks are still revoked now.
            ids={str(p['id']) for p in people or ()}
            self.samples={k:v for k,v in self.samples.items() if k in ids}
            self.portraits={k:v for k,v in self.portraits.items() if k in ids}
            self.votes={k:v for k,v in self.votes.items() if k in ids}
            self.results={k:v for k,v in self.results.items() if k in ids}
            self._published=tuple(r for r in self._published if r['track'] in ids)
            return self.public()
        # Sampling and voting follow original camera arrivals, not variable
        # queue completion times. Never count a replay as an independent face.
        observed = now if captured_at is None else captured_at
        if (type(observed) not in (int, float) or not math.isfinite(observed)
                or not 0 <= now-observed <= 1 or observed <= self.last):
            self._count('source_rejected')
            return self.public()
        now = observed
        if now - self.last < 0.5:
            self._count('rate_limited')
            return self.public()
        self.last = now
        if not self.provider and not self.activate():
            return []
        if self.provider.status.state != "ready":
            return []
        import cv2
        import numpy as np

        from homeplane.recognition.face import FaceQuality
        image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return []
        full_h, full_w = image.shape[:2]
        current, used, next_faces = {}, set(), {}
        staged=[];ambiguous_tracks=set();region_owners=set()
        for face_image, transform, owner in self._face_views(image, people):
            if owner is not None:region_owners.add(owner)
            detections=self.provider.detect(face_image)
            self._count('detected_faces',len(detections.rows))
            mapped=[]
            for slot,row in enumerate(detections.rows):
                x,y,fw,fh=row[:4]
                left,top,sx,sy=transform
                box = dict(x=(left+x*sx)/full_w, y=(top+y*sy)/full_h,
                           w=fw*sx/full_w, h=fh*sy/full_h)
                if people is None:mapped.append((slot,row,box))
                else:
                    matches=self._bodies(box,people)
                    if len(matches)>1:ambiguous_tracks.update(str(p['id']) for p in matches)
                    elif len(matches)==1 and str(matches[0]['id'])==owner:mapped.append((slot,row,box))
            if people is not None and len(mapped)>1:ambiguous_tracks.add(owner)
            staged.append((face_image,detections,mapped,owner))
        if people is not None:
            ambiguous_tracks.update(str(p['id']) for p in people if str(p['id']) not in region_owners)
        if not 0 <= self.clock()-observed <= 1:
            self._count('source_rejected')
            self.discard()
            return []
        if ambiguous_tracks:
            self._count('ambiguous_body',len(ambiguous_tracks))
            for track in ambiguous_tracks:
                self.samples.pop(track,None);self.votes.pop(track,None);self.results.pop(track,None)
                self.portraits.pop(track,None)
            self.store.clear_tracks(self.camera,self.session,ambiguous_tracks)
        for face_image,detections,mapped,owner in staged:
            if people is not None and (len(mapped)!=1 or owner in ambiguous_tracks):continue
            for slot,row,box in mapped:
                x,y,fw,fh=row[:4]
                if people is not None:
                    track = self._body(box, people)
                    if track!=owner:continue
                else:
                    center = (box["x"] + box["w"] / 2, box["y"] + box["h"] / 2)
                    nearby = [key for key, (old, at) in self._face_tracks.items()
                              if now - at <= 1 and math.dist(center, old) < min(0.08, box["w"] * 0.5)]
                    if len(nearby) == 1:
                        track = nearby[0]
                    elif not nearby:
                        self._next_face += 1
                        track = "face_" + str(self._next_face)
                    else:
                        continue
                    next_faces[track] = (center, now)
                if track is None or track in used:
                    continue
                used.add(track)
                orientation = [f for f in faces if f.get("sample_age_ms", 1e6) <= 250
                               and abs(f["box"]["x"] + f["box"]["w"] / 2 - (box['x']+box['w']/2)) < box['w']/2
                               and abs(f["box"]["y"] + f["box"]["h"] / 2 - (box['y']+box['h']/2)) < box['h']/2]
                facing = orientation[0].get("facing_candidate") if len(orientation) == 1 else None
                crop = face_image[int(y):int(y + fh), int(x):int(x + fw)]
                eye_span = abs(row[4] - row[6])
                centered = abs(row[8] - (row[4] + row[6]) / 2) < max(1, eye_span * 0.35)
                # Five-landmark visibility is a quality heuristic, not an occlusion
                # detector or liveness proof. Detection confidence alone is not one.
                visible = (all(x <= row[i] <= x + fw and y <= row[i + 1] <= y + fh
                               for i in range(4, 14, 2))
                           and max(row[5], row[7]) < row[9] < min(row[11], row[13]))
                quality = FaceQuality(pose_ok=bool(centered and eye_span > fw * 0.2 and facing is not False),
                                      sharpness_ok=bool(crop.size and cv2.Laplacian(crop, cv2.CV_64F).var() >= 60),
                                      occlusion_ok=bool(visible and row[14] >= 0.9))
                result = self.provider.embed_slot(face_image, detections, slot, quality)
                if not 0 <= self.clock()-observed <= 1:
                    self._count('source_rejected')
                    self.discard()
                    return []
                if result.embedding is None:
                    self._count('quality_rejected')
                    self.samples.pop(track, None)
                    self.portraits.pop(track,None)
                    self.votes.pop(track, None)
                    continue
                self._count('embeddings')
                old = self.samples.get(track)
                if old and now - old[-1][0] > 1:
                    old = None
                history = old if old is not None else deque(maxlen=5)
                if history and similarity(history[-1][1].values, result.embedding.values) < THRESHOLD:
                    history.clear()
                history.append((now, result.embedding))
                self.samples[track] = history
                # Use original sensor pixels, tightly bounded by the face box.
                # No body crop or preview upscale; only qualified face portraits
                # enter the bounded asynchronous candidate collector.
                px=max(0,int(box['x']*full_w));py=max(0,int(box['y']*full_h))
                right=min(full_w,int((box['x']+box['w'])*full_w))
                bottom=min(full_h,int((box['y']+box['h'])*full_h))
                portrait=image[py:bottom,px:right]
                if portrait.size:
                    ph,pw=portrait.shape[:2]
                    if max(pw,ph)>512:
                        scale=512/max(pw,ph);portrait=cv2.resize(portrait,(round(pw*scale),round(ph*scale)))
                    ok,jpg=cv2.imencode('.jpg',portrait,[cv2.IMWRITE_JPEG_QUALITY,95])
                    if ok and len(jpg)<=262144:self.portraits[track]=(now,jpg.tobytes())
                person, status = self.store.match(result.embedding)
                if not self._fresh(observed):
                    return []
                vote = self.votes.get(track)
                count = vote[1] + 1 if vote and vote[0] == person and 0 <= now - vote[2] <= 1 else 1
                self.votes[track] = (person, count, now)
                if (person is None and status=='unknown' and track in self.portraits
                        and now-self.candidate_submitted.get(track,-100)>=1):
                    self.candidate_submitted[track]=now
                    self.store.submit_candidate([result.embedding],self.portraits[track][1],box['w']*full_w*box['h']*full_h)
                status = "matched" if person and count >= 3 else status
                self._count('ambiguous_identity' if status=='ambiguous' else status)
                if not self._observe(observed, track, person, status, facing):
                    return []
                current[track] = dict(track=track, person_id=person if status == "matched" else None,
                                      state=status, facing=facing, box=box, enroll_ready=len(history) >= 3,
                                      observed_monotonic=now, face_observed_monotonic=now,
                                      basis="face_similarity")
        # Keep a just-recognized identity through a brief turn/occlusion on one
        # continuously observed body. A new camera still needs its own face.
        for body in people or ():
            track = str(body["id"])
            old = self.results.get(track)
            if (track not in ambiguous_tracks and track not in current and old and old.get("person_id")
                    and now - old["observed_monotonic"] <= 1
                    and now - old["face_observed_monotonic"] <= 5
                    and old["person_id"] in self.store.registered()):
                carried = dict(old, state="tracked", facing=None, box=body["box"], enroll_ready=False,
                               observed_monotonic=now, basis="body_continuity")
                current[track] = carried
                self._count('carried')
                if not self._observe(observed, track, old["person_id"], "tracked"):
                    return []
        self._face_tracks = next_faces
        self.results = current
        # Immutable publication lets the async status endpoint read a completed
        # observation without waiting for a native detector's thread lock.
        self._published = tuple(dict(r, box=dict(r['box'])) for r in current.values())
        self.samples = {t: s for t, s in self.samples.items() if t in current and now - s[-1][0] < 1}
        self.portraits = {t:s for t,s in self.portraits.items() if t in self.samples and now-s[0]<1}
        self.candidate_submitted={t:s for t,s in self.candidate_submitted.items() if t in current}
        self.votes = {t: v for t, v in self.votes.items() if t in current}
        return self.public()

    def public(self):
        snapshot, now = self._published, self.clock()
        return [dict(r, box=dict(r['box'])) for r in snapshot
                if 0 <= now-r['observed_monotonic'] <= 1]

    @serialized
    def enroll(self, track, person):
        history = self.samples.get(str(track), ())
        if len(history) < 3 or self.clock() - history[-1][0] > 1 or history[-1][0] - history[0][0] < 0.8:
            raise ValueError("identity_samples_required")
        portrait=self.portraits.get(str(track))
        if not portrait or not 0 <= self.clock()-portrait[0] <= 1:
            raise ValueError('identity_samples_required')
        self.store.enroll(person, [e for _, e in history],portrait=portrait[1])
        return dict(status="enrolled", person_id=person, samples=len(history), policy=POLICY)
