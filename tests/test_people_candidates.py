import sqlite3
from pathlib import Path
import pytest
from homeplane.people.live import IdentityStore
from homeplane.recognition.contracts import Embedding
from homeplane.recognition.artifacts import SFACE_SHA256
from homeplane.people.catalog import PeopleCatalog,PeopleError

def face(a=1.,b=0.):return Embedding('face',SFACE_SHA256,(a,b)+(0.,)*126,False)
PHOTO=b'\xff\xd8synthetic-face-only\xff\xd9'

@pytest.fixture
def local(tmp_path):
    p=tmp_path/'private';p.mkdir(mode=0o700);clock=[100.]
    store=IdentityStore(p/'identities.sqlite',clock=lambda:clock[0])
    yield store,clock
    store.close()

def collect(store,clock,v=None):
    for at in [100.,105.,110.]:
        clock[0]=at;store.collect_candidate([v or face()],PHOTO,100.,at)

def test_natural_observations_form_candidate_without_enrollment_or_live_session(local):
    store,clock=local
    store.collect_candidate([face()],PHOTO,100.,100.)
    assert not store.candidates() and not store.registered()
    clock[0]=105;store.collect_candidate([face()],PHOTO,100.,105.)
    assert not store.candidates()
    clock[0]=110;store.collect_candidate([face()],PHOTO,100.,110.)
    candidate=store.candidates()[0]
    assert candidate['observations']==3 and not store.registered()
    assert store.candidate_photo(candidate['id'])==PHOTO
    store.promote_candidate(candidate['id'],'person_a')
    assert store.portrait('person_a')==PHOTO and not store.candidates()
    assert store.match(face())[0]=='person_a'
    assert 'vectors' not in str(candidate)

def test_simultaneous_duplicate_does_not_count_as_independent_appearance(local):
    store,clock=local
    for _ in range(5):store.collect_candidate([face()],PHOTO,100.,100.)
    assert not store.candidates()

def test_two_distinct_natural_faces_remain_separate(local):
    store,clock=local;collect(store,clock)
    for at in [111.,116.,121.]:
        clock[0]=at;store.collect_candidate([face(0.,1.)],PHOTO,100.,at)
    assert len(store.candidates())==2 and not store.registered()

def test_expired_candidate_cannot_be_named(local):
    store,clock=local;collect(store,clock);cid=store.candidates()[0]['id']
    clock[0]+=31*86400
    assert not store.candidates() and store.candidate_photo(cid) is None
    with pytest.raises(ValueError):store.promote_candidate(cid,'person_a')

def test_queued_observation_rechecks_recent_explicit_enrollment(local):
    store,clock=local;store.enroll('person_a',[face()]*3,portrait=PHOTO)
    collect(store,clock)
    assert not store.candidates()
    store.forget('person_a');assert store.portrait('person_a') is None

def test_collector_runs_off_calling_thread_and_closes_cleanly(local):
    store,clock=local;store.submit_candidate([face()],PHOTO,100.)
    store._candidate_jobs.join()
    assert store._candidate_thread.is_alive()
    assert store.db.execute('SELECT count(*) FROM face_candidates').fetchone()[0]==1
    assert not store.registered()

def test_name_candidate_commits_catalog_and_identity_together(local):
    store,clock=local;collect(store,clock)
    catalog=PeopleCatalog(sqlite3.connect(':memory:'))
    try:
        cid=store.candidates()[0]['id']
        pid=store.name_candidate(catalog,cid,'Synthetic',0)
        assert catalog.page().revision==1 and catalog.page().people[0].person_id==pid
        assert store.match(face())[0]==pid and store.portrait(pid)==PHOTO and not store.candidates()
        assert len(list(catalog.connection.execute('PRAGMA database_list')))==1
    finally:catalog.connection.close()

def test_failed_persistence_rolls_back_name_revision_and_candidate(local,monkeypatch):
    store,clock=local;collect(store,clock);cid=store.candidates()[0]['id']
    catalog=PeopleCatalog(sqlite3.connect(':memory:'))
    original=store.enroll
    def fail_after_write(*args,**kwargs):
        original(*args,**kwargs)
        raise ValueError('identity_capacity')
    monkeypatch.setattr(store,'enroll',fail_after_write)
    try:
        with pytest.raises(ValueError):store.name_candidate(catalog,cid,'Synthetic',0)
        assert catalog.page().revision==0 and not catalog.page().people
        assert not store.registered() and store.candidate_photo(cid)==PHOTO
        assert len(list(catalog.connection.execute('PRAGMA database_list')))==1
    finally:catalog.connection.close()

def test_conflict_keeps_candidate_and_catalog_unchanged(local):
    store,clock=local;collect(store,clock);cid=store.candidates()[0]['id']
    catalog=PeopleCatalog(sqlite3.connect(':memory:'))
    try:
        catalog.register_profile('Existing',0)
        with pytest.raises(PeopleError,match='people_revision_conflict'):
            store.name_candidate(catalog,cid,'Synthetic',0)
        with pytest.raises(PeopleError,match='people_name_conflict'):
            store.name_candidate(catalog,cid,'Existing',1)
        assert catalog.page().revision==1 and not store.registered() and store.candidate_photo(cid)==PHOTO
    finally:catalog.connection.close()

def test_expired_candidates_are_removed_from_storage(local):
    store,clock=local;collect(store,clock);clock[0]+=31*86400
    assert not store.candidates()
    assert store.db.execute('SELECT count(*) FROM face_candidates').fetchone()[0]==0


def distinct_face(index):
    return Embedding('face',SFACE_SHA256,tuple(1. if i==index else 0. for i in range(128)),False)

def fill_fragments(store,clock):
    for i in range(32):
        clock[0]=100.+i
        store.collect_candidate([distinct_face(i)],PHOTO,100.,clock[0])

def test_full_fragment_gallery_accepts_new_view_without_growing(local):
    store,clock=local;fill_fragments(store,clock)
    first=store.db.execute('SELECT id FROM face_candidates ORDER BY seen LIMIT 1').fetchone()[0]
    for at in (500.,501.,502.):
        clock[0]=at;store.collect_candidate([distinct_face(32)],PHOTO,200.,at)
    assert store.db.execute('SELECT count(*) FROM face_candidates').fetchone()[0]==32
    assert store.candidate_photo(first) is None
    assert len(store.candidates())==1 and store.candidates()[0]['observations']==3

def test_capacity_preserves_presented_candidates_and_fresh_fragments(local):
    store,clock=local;fill_fragments(store,clock)
    first=store.db.execute('SELECT id FROM face_candidates ORDER BY seen LIMIT 1').fetchone()[0]
    with store.db:store.db.execute('UPDATE face_candidates SET observations=3,first=seen-1 WHERE id=?',(first,))
    clock[0]=132.
    assert store.collect_candidate([distinct_face(32)],PHOTO,200.,132.) is False
    assert store.db.execute('SELECT count(*) FROM face_candidates').fetchone()[0]==32
    clock[0]=500.;store.collect_candidate([distinct_face(32)],PHOTO,200.,500.)
    assert store.candidate_photo(first)==PHOTO
    with store.db:store.db.execute('UPDATE face_candidates SET observations=3,first=seen-1')
    clock[0]=600.
    assert store.collect_candidate([distinct_face(33)],PHOTO,200.,600.) is False
    assert store.db.execute('SELECT count(*) FROM face_candidates').fetchone()[0]==32

def test_budget_refusal_does_not_delete_fragment_or_registered_portrait(local):
    store,clock=local;fill_fragments(store,clock)
    with store.db:store.db.execute('INSERT INTO face_portraits VALUES (?,?)',('person_kept',b'x'*(6*1024*1024)))
    ids=list(store.db.execute('SELECT id FROM face_candidates ORDER BY id'));clock[0]=500.
    assert store.collect_candidate([distinct_face(32)],PHOTO,200.,500.) is False
    assert list(store.db.execute('SELECT id FROM face_candidates ORDER BY id'))==ids
    assert len(store.portrait('person_kept'))==6*1024*1024
