"""Prepare a KTV-only derivative of the installed Friday build56; never install/run.

The default prints a plan. --prepare owns one disposable staging bundle and
exports one candidate after synthetic tests. Development signing uses the exact
explicit identity and preserves original metadata; no account/password is requested.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.ktv_build_workspace import build_workspace  # noqa: E402

EVIDENCE = ROOT / '.artifacts/ktv-audio-offline-20261104/game-library'
PATCH_ID = 'build56-ktv-game-library-20261104'
OPERATIONS = ('manage/preview', 'manage/delete', 'manage/policy', 'scheduler', 'source/login',
              'source/poll', 'source/settings', *('manage/room/' + op for op in
              ('theme', 'controls', 'command', 'order', 'queue-remove', 'songs', 'retry', 'preferences')))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inventory(app):
    result = {}
    for path in sorted(app.rglob('*')):
        if path.is_symlink():
            raise ValueError('baseline_symlink_refused')
        if path.is_file():
            result[path.relative_to(app).as_posix()] = digest(path.read_bytes())
    return result


def inventory_digest(records):
    return digest(json.dumps(records, sort_keys=True, separators=(',', ':')).encode())


def run(command, *, env=None, cwd=None, timeout=45):
    result = subprocess.run(command, capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout)
    return {'returncode': result.returncode, 'stdout': result.stdout[-12000:], 'stderr': result.stderr[-4000:]}


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('reviewed_baseline_context_changed')
    return text.replace(old, new, 1)


def patch_backend(text):
    constant = 'KTV_OWNER_WRITE_PATHS = frozenset(' + repr(tuple('/api/games/ktv/' + op for op in OPERATIONS)) + ')\n'
    text = replace_once(text, "READ_ONLY_REQUEST_BYTES = {'/api/rules/core/plans': 16384}\n",
                        "READ_ONLY_REQUEST_BYTES = {'/api/rules/core/plans': 16384}\n" + constant)
    anchor = "            if scope['method']=='POST' and self.inner is not None and (\n                path.startswith('/api/embodied/')"
    gate = """            if scope['method'] == 'POST' and path in KTV_OWNER_WRITE_PATHS:
                if origin != ORIGIN.encode():
                    await JSONResponse({'code': 'origin_required'}, status_code=403)(scope, receive, send)
                    return
                if self.inner is None:
                    await JSONResponse({'code': 'home_not_connected'}, status_code=503)(scope, receive, send)
                    return
                await self.inner(scope, receive, send)
                return
"""
    return replace_once(text, anchor, gate + anchor)


def patch_games(text):
    text = "import {mountKtv} from './ktv-console/app.js';\n" + text
    text = replace_once(text, " let disposed=false,selection=null,report=null,cameras=[],busy=false,chosenCamera=null,stageApp=null,ui={};",
        " let disposed=false,selection=null,report=null,cameras=[],busy=false,chosenCamera=null,stageApp=null,ui={},ktv=null,ktvGeneration=0,lastGame=null;\n const ktvEntry={id:'ktv',name:'家庭 KTV',description:'搜歌、排歌单，和家人一起唱。',kind:'music'};")
    text = replace_once(text, "if(!busy){selection=app;draw();}", "if(!busy){if(selection?.id!=='ktv')lastGame=selection;selection=app;draw();}")
    text = replace_once(text, "app.kind==='interaction'?'全屋':app.players[0]", "app.id==='ktv'?'音乐聚会':app.kind==='interaction'?'全屋':app.players[0]")
    text = replace_once(text, " function draw(){if(disposed||!selection)return;stageApp=null;ui={};", " function draw(){if(disposed||!selection)return;ktvGeneration++;ktv?.dispose();ktv=null;wrap.dataset.ktvView=String(selection.id==='ktv');list.hidden=selection.id==='ktv';stageApp=null;ui={};")
    text = replace_once(text, "  detail.replaceChildren();detail.append(", "  detail.replaceChildren();if(selection.id==='ktv'){void openKtv();return;}detail.append(")
    insert = """ function returnToLibrary(){
  const next=report.apps.find(a=>a.id===lastGame?.id)||report.apps.find(a=>a.id!=='ktv');
  if(next){selection=next;draw();}else{ktvGeneration++;ktv?.dispose();ktv=null;wrap.dataset.ktvView='false';list.hidden=false;detail.replaceChildren(h('h2','家庭游戏'),h('p','体感游戏暂未连接，家庭 KTV 仍可打开。','muted'));}
  const row=[...list.querySelectorAll('button')].find(n=>n.textContent.includes('家庭 KTV'));row?.focus();
 }
 async function openKtv(){
  const generation=ktvGeneration;detail.append(h('p','正在连接家庭 KTV…','muted'));
  try{
   if(!document.querySelector('link[data-ktv-styles]')){const css=h('link');css.rel='stylesheet';css.href='/static/ktv-console/style.css';css.setAttribute('data-ktv-styles','');document.head.append(css);}
   if(!globalThis.FridayPartyAudio)await import('/api/games/ktv/party-audio.js');
   if(disposed||generation!==ktvGeneration||selection?.id!=='ktv')return;
   detail.replaceChildren();ktv=mountKtv(detail,{endpoint:'/api/games/ktv',embedded:true,partyAudio:globalThis.FridayPartyAudio,readOnly:document.body.dataset.consoleReadOnly==='true',onBack:returnToLibrary});
  }catch{if(!disposed&&generation===ktvGeneration){detail.replaceChildren(h('h2','家庭 KTV'),h('p','页面暂时无法载入，请重试。','muted'),button('重新载入',()=>{detail.replaceChildren();void openKtv();}),button('返回游戏库',returnToLibrary));}}
 }
"""
    text = replace_once(text, ' function update(){', insert + ' function update(){')
    text = replace_once(text, 'report=next;cameras=inventory.cameras;', "report={...next,apps:[...next.apps.filter(a=>a.id!=='ktv'),ktvEntry]};cameras=inventory.cameras;")
    text = replace_once(text, "catch{if(!disposed)notice.textContent='连接中断，正在重试。';}", "catch{if(!disposed){notice.textContent='连接中断，正在重试。';if(!report){report={apps:[ktvEntry],service:{configured:false,owned:false}};selection=ktvEntry;draw();}}}")
    text = replace_once(text, 'dispose(){disposed=true;clearInterval(timer);', 'dispose(){disposed=true;ktvGeneration++;ktv?.dispose();clearInterval(timer);')
    return text


def text_section(path):
    raw = path.read_bytes()
    if struct.unpack_from('<I', raw)[0] != 0xFEEDFACF:
        raise ValueError('reviewed_arm64_macho_required')
    count = struct.unpack_from('<I', raw, 16)[0]
    at = 32
    for _ in range(count):
        command, size = struct.unpack_from('<II', raw, at)
        if command == 0x19:
            sections = struct.unpack_from('<I', raw, at + 64)[0]
            for index in range(sections):
                start = at + 72 + index * 80
                if raw[start:start + 16].rstrip(b'\0') == b'__text':
                    length = struct.unpack_from('<Q', raw, start + 40)[0]
                    offset = struct.unpack_from('<I', raw, start + 48)[0]
                    return digest(raw[offset:offset + length])
        at += size
    raise ValueError('macho_text_section_missing')


def prepare(args):
    baseline, output = args.baseline.absolute(), args.output.absolute()
    if baseline.resolve() != baseline or not baseline.is_dir() or output.exists() or output.is_symlink():
        raise ValueError('new_candidate_and_canonical_baseline_required')
    if not output.is_relative_to(EVIDENCE / 'candidate') or any(p.is_symlink() for p in output.parents):
        raise ValueError('fixed_private_candidate_destination_required')
    before = inventory(baseline)
    info = plistlib.loads((baseline / 'Contents/Info.plist').read_bytes())
    if str(info.get('CFBundleVersion')) != '56' or info.get('CFBundleShortVersionString') != '0.6.0':
        raise ValueError('reviewed_build56_baseline_required')
    signature = run(['codesign', '-dvv', str(baseline)])
    baseline_adhoc = 'Signature=adhoc' in signature['stderr'] or 'flags=0x2(adhoc)' in signature['stderr']
    if args.sign == 'development' and (not args.identity or not re.fullmatch(r'[A-Fa-f0-9]{40}', args.identity)):
        raise ValueError('exact_authorized_development_identity_required')
    baseline_requirements = run(['codesign', '-d', '-r-', str(baseline)])
    baseline_entitlements = run(['codesign', '-d', '--entitlements', ':-', str(baseline)])
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    tool = f'python{sys.version_info.major}{sys.version_info.minor}-codesign-node'
    with build_workspace(tool, 'ktv-only-build56-candidate', configuration='ktv-game-library') as (area, record):
        stage = area / 'Friday.app'
        shutil.copytree(baseline, stage)
        if inventory(stage) != before or inventory(baseline) != before:
            raise ValueError('baseline_changed_during_consistent_copy')
        app = stage / 'Contents/Resources/product/app'
        changed = set()
        def write(relative, data):
            path = stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            changed.add(relative)
        copies = {
            'homeplane/ktv_game_api.py': 'homeplane/ktv_game_api.py',
            **{'homeplane/static/ktv-console/' + name: 'homeplane/static/ktv-console/' + name
               for name in ('app.js', 'index.html', 'style.css')},
            'homeplane/static/ktv-console/party-audio.js': 'integrations/ktv_public/web/party-audio.js',
            'homeplane/static/icons/microphone.svg': 'homeplane/static/icons/microphone.svg',
        }
        for target, source in copies.items():
            data = (ROOT / source).read_bytes()
            if target == 'homeplane/ktv_game_api.py':
                data = replace_once(data.decode(), 'Path(__file__).resolve().parents[1] / "integrations/ktv_public/web/party-audio.js"',
                                    'Path(__file__).with_name("static") / "ktv-console/party-audio.js"').encode()
            write('Contents/Resources/product/app/' + target, data)
        old = (app / 'homeplane/api.py').read_text()
        anchor = '    app = FastAPI(title="Home Control Plane", version="1", lifespan=lifespan)\n'
        new = replace_once(old, anchor, anchor + '    from .ktv_game_api import create_router as ktv_game_router\n    app.include_router(ktv_game_router(read_only=console_read_only))\n')
        write('Contents/Resources/product/app/homeplane/api.py', new.encode())
        write('Contents/Resources/backend/desktop_backend.py', patch_backend((stage / 'Contents/Resources/backend/desktop_backend.py').read_text()).encode())
        write('Contents/Resources/product/app/homeplane/static/games-library.js', patch_games((app / 'homeplane/static/games-library.js').read_text()).encode())
        extra_css = '\n.games-library[data-ktv-view=true]{display:block}.games-library[data-ktv-view=true]>.games-list{display:none}.games-library[data-ktv-view=true]>.game-detail{padding:0;border:0;background:transparent}.games-library[data-ktv-view=true] .ktv-console{padding:24px 28px;background:#fff;border-radius:12px}.games-library[data-ktv-view=true] .ktv-navigation{flex-direction:row;align-items:center;justify-content:flex-start}.games-library[data-ktv-view=true] .ktv-navigation button{width:auto;flex:0 0 auto}@media(max-width:640px){.games-library[data-ktv-view=true] .ktv-console{padding:18px 16px}}\n'
        write('Contents/Resources/product/app/homeplane/static/games-library.css', (app / 'homeplane/static/games-library.css').read_bytes() + extra_css.encode())
        mapping = re.search(r'\.icon-microphone\{[^}]+\}', (ROOT / 'homeplane/static/icons.css').read_text())
        if not mapping:
            raise ValueError('existing_microphone_mapping_required')
        write('Contents/Resources/product/app/homeplane/static/icons.css', (app / 'homeplane/static/icons.css').read_bytes() + ('\n' + mapping[0] + '\n').encode())
        info['FridayKTVPatchIdentifier'] = PATCH_ID
        write('Contents/Info.plist', plistlib.dumps(info))
        content = inventory(stage)
        patch = {'schema': 1, 'patch_id': PATCH_ID, 'baseline_version': '0.6.0', 'baseline_build': '56',
                 'full_source_rebuild': False, 'native_recompiled': False, 'baseline_inventory_sha256': inventory_digest(before),
                 'baseline_source_integration_sha256': before['Contents/Resources/FRIDAY-SOURCE-INTEGRATION.json'],
                 'changed_resources': {name: content[name] for name in sorted(changed)},
                 'owner_credentials_packaged': False, 'new_features': ['KTV game-library workspace only'],
                 'package_asset_adaptation': 'One shared party-audio.js under ktv-console; adapter points to that same packaged asset.'}
        write('Contents/Resources/FRIDAY-KTV-PATCH.json', (json.dumps(patch, indent=2) + '\n').encode())
        validation = validate(stage, area)
        if not validation['passed']:
            (EVIDENCE / 'candidate-validation-failure.json').write_text(json.dumps(validation, indent=2) + '\n')
            raise ValueError('candidate_synthetic_validation_failed')
        signed = False
        if args.sign == 'development':
            result = run(['codesign', '--force', '--deep', '--sign', args.identity, '--timestamp=none',
                          '--preserve-metadata=identifier,requirements,entitlements,flags', str(stage)])
            if result['returncode']:
                raise ValueError('candidate_local_signing_failed')
            signed = True
        verify = run(['codesign', '--verify', '--strict', '--deep', str(stage)])
        details = run(['codesign', '-dvv', str(stage)])
        entitlements = run(['codesign', '-d', '--entitlements', ':-', str(stage)])
        requirements = run(['codesign', '-d', '-r-', str(stage)])
        leaf_sha1 = None
        if signed:
            certificate_prefix = area / 'candidate-public-cert-'
            extraction = run(['codesign', '-d', '--extract-certificates=' + str(certificate_prefix), str(stage)])
            leaf = Path(str(certificate_prefix) + '0')
            if extraction['returncode'] or not leaf.is_file():
                raise ValueError('signed_candidate_certificate_unavailable')
            leaf_sha1 = hashlib.sha1(leaf.read_bytes()).hexdigest().upper()
            if leaf_sha1 != args.identity.upper():
                raise ValueError('candidate_identity_mismatch')
            baseline_team = re.search(r'TeamIdentifier=([^\n]+)', signature['stderr'])
            candidate_team = re.search(r'TeamIdentifier=([^\n]+)', details['stderr'])
            def flags(value):
                return re.search(r'flags=([^ ]+)', value).group(1)
            if (not baseline_team or not candidate_team or baseline_team[1] != candidate_team[1]
                    or baseline_requirements['stdout'] != requirements['stdout']
                    or baseline_entitlements['stdout'] != entitlements['stdout']
                    or flags(signature['stderr']) != flags(details['stderr']) or verify['returncode']):
                raise ValueError('candidate_signature_or_metadata_incompatible')
        after = inventory(stage)
        differences = [{'path': name, 'change': 'added' if name not in before else 'removed' if name not in after else 'modified',
                        'baseline_sha256': before.get(name), 'candidate_sha256': after.get(name)}
                       for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)]
        allowed = changed | {'Contents/MacOS/Friday', 'Contents/_CodeSignature/CodeResources'}
        if any(row['path'] not in allowed for row in differences):
            raise ValueError('unreviewed_candidate_difference')
        native_text_unchanged = text_section(baseline / 'Contents/MacOS/Friday') == text_section(stage / 'Contents/MacOS/Friday')
        if not native_text_unchanged or inventory(baseline) != before:
            raise ValueError('baseline_or_native_logic_changed')
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, output)
        if inventory(output) != after:
            raise ValueError('candidate_export_changed')
        receipt = {'schema': 1, 'patch_id': PATCH_ID, 'candidate': str(output), 'baseline': str(baseline),
                   'baseline_inventory_sha256': inventory_digest(before), 'candidate_inventory_sha256': inventory_digest(after),
                   'baseline_file_count': len(before), 'candidate_file_count': len(after),
                   'baseline_bytes': sum((baseline / name).stat().st_size for name in before),
                   'baseline_signature': signature, 'baseline_signature_adhoc': baseline_adhoc,
                   'candidate_signing': 'same-authority Apple Development local candidate' if signed else 'pending signature; copied baseline signature is invalid after resource edits',
                   'signature_identity_sha1': leaf_sha1, 'signature_verify': verify, 'signature_details': details,
                   'entitlements': entitlements, 'designated_requirements': requirements,
                   'baseline_designated_requirements': baseline_requirements,
                   'metadata_preserved': signed, 'external_trust_limit': 'No notarization, Gatekeeper execution or external revocation service check was performed.',
                   'version': '0.6.0', 'build': '56', 'version_rule': 'Original build retained; explicit FridayKTVPatchIdentifier and FRIDAY-KTV-PATCH receipt mark the derivative.',
                   'differences': differences, 'untouched_baseline_files': sum(1 for name in before if before[name] == after.get(name)),
                   'native_text_unchanged': native_text_unchanged, 'private_runtime_not_copied': True,
                   'installed_app_unchanged': True, 'installed_or_launched': False, 'notarized': False,
                   'synthetic_validation': validation, 'cache_manifest': str(area / 'manifest.json'),
                   'recovery_conditions': ['Keep the installed build56 and all private state/config paths unchanged until root approves installation.',
                       'Do not replace a running app/service; the existing active locked-venv path remains required.',
                       'If later installation/readback fails, stop only the named household service gracefully and restore the retained baseline through the approved installer.',
                       'Preserved designated requirements, TeamIdentifier, entitlements and flags were checked locally; installation/runtime consent remains root acceptance.']}
        (output.parent / 'MANIFEST.json').write_text(json.dumps(receipt, indent=2) + '\n')
        record.update(candidate_exported=True, candidate_path=str(output), signature_verified=verify['returncode'] == 0)
        return receipt


def validate(stage, area):
    # Runtime-only synthetic validator lives outside the signed bundle.
    validator = area / 'validate.py'
    validator.write_text(PY_VALIDATION)
    app = stage / 'Contents/Resources/product/app'
    python = stage / 'Contents/Resources/product/runtime/bin/python3'
    safe_env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TMPDIR') if key in os.environ}
    safe_env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(app), FRIDAY_HOME=str(area / 'profile'),
                    FRIDAY_EMBODIED_DATA=str(area / 'profile/embodied'), FRIDAY_SETTINGS=str(area / 'profile/settings.json'))
    result = run([str(python), '-B', str(validator), str(stage), str(area / 'profile')], env=safe_env, cwd=area, timeout=60)
    for relative in ('Contents/Resources/backend/desktop_backend.py', 'Contents/Resources/product/app/homeplane/api.py', 'Contents/Resources/product/app/homeplane/ktv_game_api.py'):
        ast.parse((stage / relative).read_text())
    node_results = []
    for name in ('games-library.js', 'ktv-console/app.js', 'ktv-console/party-audio.js'):
        node_results.append(run(['node', '--check', str(app / 'homeplane/static' / name)]))
    closure = []
    for relative in ('games-library.js', 'ktv-console/app.js'):
        path = app / 'homeplane/static' / relative
        for target in re.findall(r"(?:from\s*|import\s*\()\s*['\"](\.[^'\"]+)['\"]", path.read_text()):
            dependency = path.parent / target
            closure.append({'importer': relative, 'relative_import': target, 'present': dependency.is_file()})
    return {'passed': result['returncode'] == 0 and all(x['returncode'] == 0 for x in node_results) and all(x['present'] for x in closure),
            'runtime_contract': result, 'js_parse': node_results, 'relative_import_closure': closure,
            'upstream': 'all fake; no lifespan, service, device or model started'}


PY_VALIDATION = r'''
import asyncio,importlib.util,json,os,sys
from pathlib import Path
from types import SimpleNamespace
stage,profile=map(Path,sys.argv[1:]);profile.mkdir(parents=True,exist_ok=True)
app_root=stage/'Contents/Resources/product/app'
from fastapi import FastAPI
import httpx
import homeplane.api,homeplane.ktv_game_api as ktv
assert Path(homeplane.api.__file__).is_relative_to(app_root)
assert Path(ktv.__file__).is_relative_to(app_root)
spec=importlib.util.spec_from_file_location('candidate_desktop',stage/'Contents/Resources/backend/desktop_backend.py')
backend=importlib.util.module_from_spec(spec);spec.loader.exec_module(backend)
backend.arl_runtime=lambda:SimpleNamespace()
config=profile/'synthetic-owner.json';token='SYNTHETIC_'+ 'x'*40
config.write_text(json.dumps({'token':token}));config.chmod(0o600)
seen=[]
def owner(request):
 assert str(request.url).startswith('http://127.0.0.1:8769/v1/')
 assert request.headers['X-KTV-Key']==token
 seen.append(request.url.path)
 return httpx.Response(200,json={'accepted':True,'mode':'simulator','upstream':'fake','token':token})
def compose(readonly):
 inner=FastAPI();inner.include_router(ktv.create_router(config,transport=httpx.MockTransport(owner),read_only=readonly))
 inner.state.core=SimpleNamespace(actions=SimpleNamespace(policy_report=lambda:{'enabled':False,'configured':False}))
 wrapper=backend.DesktopService();wrapper.inner=inner
 return wrapper
async def main():
 assertions=0;origin='http://127.0.0.1:8740'
 wrapper=compose(False)
 async with httpx.AsyncClient(transport=httpx.ASGITransport(wrapper),base_url=origin)as client:
  schema=await client.get('/openapi.json');assert schema.status_code==200;assertions+=1
  for operation in sorted(ktv.WRITES):
   response=await client.post('/api/games/ktv/'+operation,json={},headers={'Origin':origin})
   assert response.status_code==200,(operation,response.text)
   assert token not in response.text;assertions+=2
  script=await client.get('/api/games/ktv/party-audio.js');assert script.status_code==200
  assert script.content==(app_root/'homeplane/static/ktv-console/party-audio.js').read_bytes();assertions+=2
  for headers in ({},{'Origin':'https://example.invalid'},{'Origin':origin,'Host':'example.invalid:8740'}):
   assert(await client.post('/api/games/ktv/manage/preview',json={},headers=headers)).status_code==403;assertions+=1
  assert(await client.post('/api/games/ktv/unreviewed',json={},headers={'Origin':origin})).status_code==409;assertions+=1
 async with httpx.AsyncClient(transport=httpx.ASGITransport(compose(True)),base_url=origin)as client:
  assert(await client.post('/api/games/ktv/manage/delete',json={},headers={'Origin':origin})).status_code==403;assertions+=1
 async with httpx.AsyncClient(transport=httpx.ASGITransport(wrapper,client=('192.0.2.10',9999)),base_url=origin)as client:
  assert(await client.post('/api/games/ktv/manage/preview',json={},headers={'Origin':origin})).status_code==403;assertions+=1
 print(json.dumps({'passed':True,'assertions':assertions,'profile':'simulator','upstream':'all fake','lifespan_started':False,'owner_requests':len(seen),'modules_from_candidate':True}))
asyncio.run(main())
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--baseline', type=Path, default=Path.home() / 'Applications/Friday.app')
    parser.add_argument('--output', type=Path, default=EVIDENCE / 'candidate/Friday.app')
    parser.add_argument('--sign', choices=('pending', 'development'), default='pending')
    parser.add_argument('--identity', help='Exact authorized certificate SHA1, never a password or private key')
    args = parser.parse_args()
    if not args.prepare:
        print(json.dumps({'baseline': str(args.baseline), 'candidate': str(args.output), 'patch_id': PATCH_ID,
                          'compile_native': False, 'install_or_run': False, 'signing': args.sign}, indent=2))
        return
    receipt = prepare(args)
    print(json.dumps({'candidate_prepared': True, 'patch_id': receipt['patch_id'],
                      'changes': len(receipt['differences']), 'unchanged_files': receipt['untouched_baseline_files'],
                      'synthetic_passed': receipt['synthetic_validation']['passed'],
                      'signature_verified': receipt['signature_verify']['returncode'] == 0,
                      'installed_or_launched': False, 'manifest': str(args.output.parent / 'MANIFEST.json')}))


if __name__ == '__main__':
    main()
