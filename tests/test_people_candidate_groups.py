import json
import math
import sqlite3

import pytest

from homeplane.people.candidate_groups import groups
from homeplane.people.catalog import PeopleCatalog
from homeplane.people.live import IdentityStore
from homeplane.recognition.artifacts import SFACE_SHA256


def row(key,angle,first=0):
    value=[math.cos(angle),math.sin(angle)]+[0.]*126
    return dict(id=key,vectors=[value]*3,first_seen=first,last_seen=10+first,
                observations=3,portrait_revision=100+first)


def test_pose_groups_hide_vectors_and_do_not_chain_different_people():
    a,b,c=row('a',0),row('b',.7,1),row('c',1.4,2)
    # A resembles B and B resembles C, but A and C are distinct. A bridge
    # must never collapse all three into one group.
    result=groups([a,b,c])
    assert len(result)==2
    assert max(len(g['members']) for g in result)==2
    assert 'vectors' not in json.dumps(result)
    assert groups([a,row('b',.15,1),row('c',.3,2)])[0]['members']==['a','b','c']


def test_two_independent_views_are_required_for_similarity():
    a,b=row('a',0),row('b',math.pi/2)
    b['vectors'][0]=a['vectors'][0]
    assert len(groups([a,b]))==2


@pytest.fixture
def store(tmp_path):
    private=tmp_path/'private';private.mkdir(mode=0o700)
    owner=IdentityStore(private/'identities.sqlite',clock=lambda:100.)
    with owner.db:
        for index,angle in enumerate((0.,.3,.6,math.pi)):
            r=row('candidate_'+format(index,'024x'),angle,index)
            owner.db.execute('INSERT INTO face_candidates VALUES (?,?,?,?,?,?,?,?)',
                (r['id'],SFACE_SHA256,json.dumps(r['vectors']),b'\xff\xd8synthetic\xff\xd9',r['first_seen'],90.,3,100.+index))
    yield owner
    owner.close()


def test_group_split_persists_across_refresh_and_undo_keeps_original_samples(store):
    group=next(g for g in store.candidate_groups() if len(g['members'])==3)
    before=list(store.db.execute('SELECT id,vectors,jpeg FROM face_candidates ORDER BY id'))
    store.edit_candidate_group(group['members'],separate=True)
    assert len(store.candidate_groups())==4 and store.group_undo_available()
    store.undo_candidate_group()
    assert len(store.candidate_groups())==2 and not store.group_undo_available()
    assert list(store.db.execute('SELECT id,vectors,jpeg FROM face_candidates ORDER BY id'))==before


def test_one_registration_promotes_every_view_and_keeps_other_person(store):
    catalog=PeopleCatalog(sqlite3.connect(':memory:'))
    try:
        group=next(g for g in store.candidate_groups() if len(g['members'])==3)
        assert not store.registered()
        pid=store.name_candidate(catalog,group['id'],'测试家人',0,
                                 members=group['members'],group_revision=group['group_revision'])
        assert len(store.candidates())==1 and len(store.registered())==1
        assert catalog.page().people[0].person_id==pid
        assert len(json.loads(store.db.execute('SELECT vectors FROM face_templates').fetchone()[0]))==9
        assert catalog.page().people[0].action_authorized is False
    finally:catalog.connection.close()


def test_changed_group_or_failed_enrollment_rolls_back_all_faces_and_name(store,monkeypatch):
    catalog=PeopleCatalog(sqlite3.connect(':memory:'))
    try:
        group=next(g for g in store.candidate_groups() if len(g['members'])==3)
        with pytest.raises(ValueError):
            store.name_candidate(catalog,group['id'],'测试',0,members=group['members'],group_revision='0'*64)
        original=store.enroll
        def fail(*args,**kwargs):
            original(*args,**kwargs)
            raise ValueError('identity_capacity')
        monkeypatch.setattr(store,'enroll',fail)
        with pytest.raises(ValueError):
            store.name_candidate(catalog,group['id'],'测试',0,members=group['members'],group_revision=group['group_revision'])
        assert len(store.candidates())==4 and not store.registered() and catalog.page().revision==0
    finally:catalog.connection.close()
