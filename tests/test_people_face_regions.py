from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

from homeplane.people.live import FaceIdentityPipeline
from homeplane.recognition.contracts import Embedding
from homeplane.recognition.artifacts import SFACE_SHA256


def body(track=1,**values):
    return {'id':track,'box':dict(x=.25,y=.25,w=.2,h=.5,**values)}


def test_native_head_crop_retains_source_pixels_and_has_bounded_contiguous_input():
    image=np.random.default_rng(3).integers(0,256,(2160,3840,3),np.uint8)
    small={'id':7,'box':dict(x=.3,y=.3,w=.1,h=.3)}
    crop,transform,owner=next(FaceIdentityPipeline._face_views(image,[small]))
    left,top,sx,sy=transform
    assert sx==sy==1 and owner=='7'
    assert crop.shape[0]<=720 and crop.shape[1]<=1280 and crop.flags.c_contiguous
    np.testing.assert_array_equal(crop,image[top:top+crop.shape[0],left:left+crop.shape[1]])
    full,t,owner=next(FaceIdentityPipeline._face_views(image,None))
    assert full.shape==(720,1280,3) and t==(0,0,3.,3.) and owner is None


def test_region_budget_and_invalid_geometry_are_bounded():
    image=np.zeros((100,200,3),np.uint8)
    assert list(FaceIdentityPipeline._face_views(image,[body()]*9))==[]
    for invalid in [float('nan'),float('inf'),1e308,-10]:
        item=body();item['box']['x']=invalid
        assert list(FaceIdentityPipeline._face_views(image,[item]))==[]


def pipeline(rows):
    store=Mock();store.match.return_value=(None,'unknown')
    p=FaceIdentityPipeline(store,'camera','session','.',clock=lambda:100.)
    p.provider=Mock(status=SimpleNamespace(state='ready'))
    p.provider.detect.return_value=SimpleNamespace(rows=rows)
    p.provider.embed_slot.return_value=SimpleNamespace(embedding=Embedding('face',SFACE_SHA256,(1.,)+(0.,)*127,False))
    return p


ROW=(150.,140.,80.,90.,170.,165.,210.,165.,190.,185.,175.,210.,205.,210.,.99)


def test_detector_and_embedding_use_same_crop_and_map_back_to_global_body(monkeypatch):
    image=np.random.default_rng(4).integers(0,256,(2160,3840,3),np.uint8)
    p=pipeline([ROW]);b=body()
    jpeg=cv2.imencode('.jpg',image)[1].tobytes()
    result=p.process(jpeg,[b])
    assert len(result)==1 and result[0]['track']=='1'
    detected=p.provider.detect.call_args.args[0]
    embedded=p.provider.embed_slot.call_args.args[0]
    assert detected is embedded and p.provider.embed_slot.call_args.args[3].qualified
    _,t,_=next(p._face_views(image,[b]));left,top,sx,sy=t
    assert result[0]['box']==pytest.approx(dict(x=(left+150*sx)/3840,y=(top+140*sy)/2160,w=80*sx/3840,h=90*sy/2160))


@pytest.mark.parametrize('two_faces',[False,True])
def test_overlapping_bodies_or_multiple_faces_never_choose_first(two_faces):
    image=np.full((720,1280,3),128,np.uint8);jpeg=cv2.imencode('.jpg',image)[1].tobytes()
    p=pipeline([ROW,ROW] if two_faces else [ROW])
    people=[{'id':1,'box':dict(x=0,y=0,w=1,h=1)}]
    if not two_faces:people.append({'id':2,'box':dict(x=0,y=0,w=1,h=1)})
    assert p.process(jpeg,people)==[]
    p.provider.embed_slot.assert_not_called()


def test_low_resolution_gap_keeps_samples_without_refreshing_face_time():
    p=pipeline([]);p.last=100.
    row=dict(track='1',box=dict(x=.3,y=.3,w=.1,h=.1),observed_monotonic=100.)
    p.samples={'1':[(100.,'sample')]};p.results={'1':row};p.votes={'1':('person',1,100.)};p._published=(row,)
    p.clock=lambda:100.4
    assert p.process(b'', [body()],sample=False)==[row]
    assert p.last==100 and p.samples['1']==[(100.,'sample')]
    p.provider.detect.assert_not_called()
    p.clock=lambda:101.1
    assert p.process(b'', [body()],sample=False)==[]
    assert p.last==100


def test_gap_still_revokes_departed_body_and_motion():
    p=pipeline([])
    row=dict(track='1',box=dict(x=.3,y=.3,w=.1,h=.1),observed_monotonic=100.)
    p.samples={'1':[(100.,'sample')]};p.results={'1':row};p._published=(row,)
    assert p.process(b'',[body(2)],sample=False)==[]
    assert not p.samples and not p.results
    p.samples={'2':[(100.,'sample')]}
    assert p.process(b'',[body(2)],moving=True,sample=False)==[]
    assert not p.samples and not p._published
    p.store.clear_camera.assert_called_once()


def test_duplicate_body_ids_and_no_body_never_create_regions():
    image=np.zeros((100,200,3),np.uint8)
    assert list(FaceIdentityPipeline._face_views(image,[body(1),body(1)]))==[]
    assert list(FaceIdentityPipeline._face_views(image,[]))==[]
