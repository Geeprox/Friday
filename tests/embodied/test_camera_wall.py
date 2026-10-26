import base64
import pytest
from homeplane.embodied.camera_wall import CameraWall

CAMERAS=[dict(id='cam_1111111111',name='A',room='one'),dict(id='cam_2222222222',name='B',room='two')]
JPEG=b'\xff\xd8hello\xff\xd9'
def packet(at):return dict(type='frame',preview_at=at,preview=base64.b64encode(JPEG).decode())

def test_video_does_not_wait_for_recognition_and_closed_cameras_are_absent():
 now=[10.];wall=CameraWall(CAMERAS,clock=lambda:now[0]);epoch=wall.begin(CAMERAS[0]['id'])
 wall.offer(CAMERAS[0]['id'],epoch,packet(9.7))
 view=wall.view({CAMERAS[0]['id']:dict(state='unavailable',recognition='waiting',people=2),CAMERAS[1]['id']:dict(state='shutter_closed')})
 assert len(view['cameras'])==1
 assert view['cameras'][0]['state']=='live' and view['cameras'][0]['people']==0
 assert wall.frame(CAMERAS[0]['id'])[0]==JPEG
 now[0]=11.7
 assert wall.frame(CAMERAS[0]['id']) is None

def test_close_fences_inflight_frames_and_prior_session_cleanup():
 wall=CameraWall(CAMERAS,clock=lambda:10.);cid=CAMERAS[0]['id'];old=wall.begin(cid)
 wall.offer(cid,old,packet(9.8));wall.clear(cid,old);wall.offer(cid,old,packet(9.9))
 assert wall.frame(cid) is None
 new=wall.begin(cid);wall.offer(cid,new,packet(9.9));wall.clear(cid,old)
 assert wall.frame(cid)[1]==9.9

@pytest.mark.parametrize('at',[True,None,'10',float('nan'),float('inf'),11,8])
def test_invalid_or_stale_timestamp_never_publishes(at):
 wall=CameraWall(CAMERAS,clock=lambda:10.);cid=CAMERAS[0]['id'];epoch=wall.begin(cid)
 wall.offer(cid,epoch,packet(at));assert wall.frame(cid) is None

def test_duplicate_or_malformed_frame_does_not_extend_freshness():
 wall=CameraWall(CAMERAS,clock=lambda:10.);cid=CAMERAS[0]['id'];epoch=wall.begin(cid)
 wall.offer(cid,epoch,packet(9.8));wall.offer(cid,epoch,packet(9.7))
 broken=packet(9.9);broken['preview']='invalid';wall.offer(cid,epoch,broken)
 assert wall.frame(cid)[1]==9.8

def test_hd_fallback_reuses_source_jpeg_and_expires_at_original_capture_time():
 now=[10.];wall=CameraWall(CAMERAS,clock=lambda:now[0]);cid=CAMERAS[0]['id'];epoch=wall.begin(cid)
 full=b'\xff\xd8synthetic-hd-source\xff\xd9';p=packet(9.8)
 p.update(jpeg=base64.b64encode(full).decode(),received_at=9.8)
 wall.offer(cid,epoch,p)
 assert wall.still(cid)==(full,9.8) and wall.frame(cid)==(JPEG,9.8)
 now[0]=11.81;assert wall.still(cid) is None

def test_hd_fallback_is_revoked_with_privacy_and_rejects_mismatched_source_time():
 wall=CameraWall(CAMERAS,clock=lambda:10.);cid=CAMERAS[0]['id'];old=wall.begin(cid)
 p=packet(9.8);p.update(jpeg=base64.b64encode(JPEG).decode(),received_at=9.7)
 wall.offer(cid,old,p);assert wall.still(cid) is None
 p=packet(9.9);p.update(jpeg=base64.b64encode(JPEG).decode(),received_at=9.9)
 wall.offer(cid,old,p);assert wall.still(cid)
 wall.clear(cid,old);wall.offer(cid,old,p);assert wall.still(cid) is None
