import ast
import hashlib
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request

from homeplane.embodied.game_assets import game_asset_path
from homeplane.embodied.static_page import static_asset


def test_game_graphics_are_an_exact_public_allowlist():
    for name in ('games.js','game-scenes.js','game-teaching.js','summer-props.png',
                 'body-motions.js','motion-art/body/body.gltf','vendor/three.core.js',
                 'quest-stream-scene.png','quest-camp-scene.png','quest-props.png','quest-lantern.webp',
                 'tennis-near-v1.png','tennis-far-v1.png','beam-pose.svg','beam-glade.webp','beam-fluff.webp','beam-scene.js'):
        path=game_asset_path(name)
        assert path is not None and path.is_file()
    for name in ('../config.json','motion-art/body/../../../../data/config.json',
                 'camera.jpg','private/config.json','/etc/passwd','body.gltf.json',
                 'quest-stream-scene.png.json','quest-camp-scene.png.json','quest-props.png.json','quest-lantern.webp.json',
                 'tennis-near-v1.png.json','tennis-far-v1.png.json','beam-glade.webp.json','beam-fluff.webp.json'):
        assert game_asset_path(name) is None


@pytest.mark.asyncio
async def test_original_worker_asset_handler_serves_chapters_and_revalidates_bytes():
    # Extract just the original handler: importing worker would start runtime owners.
    path = Path('homeplane/embodied/worker.py')
    tree = ast.parse(path.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'asset')
    node.decorator_list = []
    scope = dict(__package__='homeplane.embodied', Request=Request, HTTPException=HTTPException,
                 static_asset=static_asset)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(path), 'exec'), scope)
    app = FastAPI()
    app.get('/assets/{name:path}')(scope['asset'])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for name in ('quest-stream-scene.png', 'quest-camp-scene.png', 'quest-props.png',
                     'tennis-near-v1.png', 'tennis-far-v1.png',
                     'motion-art/body/targets/exercise-balance.png',
                     'motion-art/body/targets/exercise-cross_knee.png'):
            response = await client.get('/assets/' + name)
            assert response.status_code == 200
            assert response.headers['content-type'] == 'image/png'
            assert response.headers['cache-control'] == 'no-cache'
            assert hashlib.sha256(response.content).digest() == hashlib.sha256(
                game_asset_path(name).read_bytes()).digest()
            cached = await client.get('/assets/' + name, headers={'if-none-match': response.headers['etag']})
            assert cached.status_code == 304 and not cached.content
        for name in ('quest-props.png.json', 'camera.png', 'private/config.json'):
            assert (await client.get('/assets/' + name)).status_code == 404
        for name in ('beam-glade.webp', 'beam-fluff.webp'):
            response = await client.get('/assets/' + name)
            assert response.headers['content-type'] == 'image/webp'
            assert response.content == game_asset_path(name).read_bytes()
