"""Video-only preview in an owned subprocess, separate from recognition pause."""
import asyncio
import base64
import json
import os
import secrets
import sys

from .paths import DATA, PROJECT, PYTHON, camera


class ManualPreview:
    def __init__(self):self.process=None;self.cameras=set();self.tokens={};self.lock=asyncio.Lock()

    def has(self,camera_id):return camera_id in self.cameras and self.process is not None and self.process.returncode is None

    async def command(self,action,camera_id,**fields):
        camera(camera_id)
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                self.cameras.clear()
                if action!='start':raise ValueError('camera_preview_stopped')
                self.process=await asyncio.create_subprocess_exec(str(PYTHON),'-B','-m','homeplane.embodied.manual_preview',
                    cwd=str(PROJECT),env=dict(os.environ,PYTHONPATH=str(PROJECT),FRIDAY_EMBODIED_DATA=str(DATA)),
                    stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,limit=3_000_000)
            try:
                self.process.stdin.write((json.dumps(dict(action=action,camera=camera_id,**fields))+'\n').encode())
                await self.process.stdin.drain()
                line=await asyncio.wait_for(self.process.stdout.readline(),20)
                if len(line)>2_800_000:raise ValueError('camera_preview_unavailable')
                result=json.loads(line)
                if result.get('error'):raise ValueError('camera_preview_unavailable')
                if action=='start':
                    self.cameras.add(camera_id);self.tokens[camera_id]=secrets.token_hex(16)
                    result['preview_token']=self.tokens[camera_id]
                if action=='stop':self.cameras.discard(camera_id);self.tokens.pop(camera_id,None)
                return result
            except BaseException:
                await self._close();raise

    async def _close(self):
        process,self.process=self.process,None;self.cameras.clear();self.tokens.clear()
        if process is not None and process.returncode is None:
            process.stdin.close()
            try:await asyncio.wait_for(process.wait(),7)
            except asyncio.TimeoutError:
                process.kill();await process.wait()

    async def close(self):
        async with self.lock:await self._close()


async def worker(pool_factory=None):
    if pool_factory is None:
        from .previews import PreviewPool
        pool_factory=PreviewPool
    pool=pool_factory(recognize=False,control_camera=True)
    try:
        while line:=await asyncio.to_thread(sys.stdin.buffer.readline,2049):
            try:
                if len(line)>2048:raise ValueError('request_budget')
                request=json.loads(line);selected=camera(request['camera']);action=request['action']
                if action=='start':result=await pool.start(selected['id'])
                elif action=='status':result=pool.status(selected['id'])
                elif action=='frame':
                    frame=pool.frame(selected['id'])
                    result=dict(frame=base64.b64encode(frame).decode() if frame and len(frame)<=2_000_000 else None)
                elif action=='stop':await pool.stop(selected['id']);result=dict(state='stopped')
                elif action=='control':result=await pool.control(selected['id'],direction=request.get('direction'),mode='hold' if request.get('mode')=='hold' else None)
                else:raise ValueError('unsupported_preview_operation')
                print(json.dumps(result),flush=True)
            except Exception:print(json.dumps(dict(error='camera_preview_unavailable')),flush=True)
    finally:await pool.close()


if __name__=='__main__':asyncio.run(worker())
