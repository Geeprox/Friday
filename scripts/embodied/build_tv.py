"""Build only this ordinary TV viewer with the existing local Android toolchain."""
from pathlib import Path
import argparse,hashlib,json,os,subprocess,zipfile

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'native/FridayTV/android'
HERE=ROOT/'.artifacts/embodied-migration/tv'
SDK=ROOT/'.artifacts/embodied-toolchain/android'
JAVA=ROOT/'.artifacts/embodied-toolchain/jdk'
BUILD=HERE/'build'
BUILD.mkdir(parents=True,exist_ok=True)
PRIVATE=ROOT/'data/embodied/private'
args=argparse.ArgumentParser(description=__doc__)
args.add_argument('--next-key',action='store_true',help='Build the coordinated next viewer capability without changing the running service')
options=args.parse_args()
config=json.loads((PRIVATE/'config.json').read_text())
key_path=PRIVATE/('viewer-key-next' if options.next_key else 'viewer-key')
GENERATED=BUILD/'src'
import shutil
if GENERATED.exists():shutil.rmtree(GENERATED)
shutil.copytree(SOURCE/'src',GENERATED)
p=GENERATED/'local/tv/camera/MainActivity.java'
s=p.read_text().replace('__FRIDAY_VIEW_ORIGIN__','http://'+config['viewer_host']+':'+str(config['viewer_port'])).replace('__FRIDAY_VIEW_KEY__',key_path.read_text().strip())
p.write_text(s);p.chmod(0o600)
ENV=dict(os.environ,JAVA_HOME=str(JAVA))
ENV['PATH']=str(JAVA/'bin')+os.pathsep+ENV.get('PATH','')
def one(name):
    paths=list(SDK.rglob(name))
    if len(paths)!=1:raise RuntimeError('toolchain_ambiguous_'+name)
    return paths[0]
def run(*args):
    result=subprocess.run([str(x) for x in args],env=ENV,capture_output=True,text=True)
    if result.returncode:raise RuntimeError('tv_build_tool_failed_'+Path(str(args[0])).name)
    return result.stdout
android=one('android.jar');aapt=one('aapt2');d8=one('d8');align=one('zipalign');signer=one('apksigner')
for name in ['classes','dex']:
    folder=BUILD/name
    if folder.is_symlink():raise RuntimeError('build_symlink_refused')
    if folder.exists():shutil.rmtree(folder)
    folder.mkdir()
run(JAVA/'bin/javac','--release','8','-classpath',android,'-d',BUILD/'classes',*sorted(GENERATED.rglob('*.java')))
run(d8,'--min-api','23','--lib',android,'--output',BUILD/'dex',*sorted((BUILD/'classes').rglob('*.class')))
run(aapt,'compile','--dir',SOURCE/'res','-o',BUILD/'resources.zip')
run(aapt,'link','--auto-add-overlay','-o',BUILD/'resources.apk','-I',android,'--manifest',SOURCE/'AndroidManifest.xml','-R',BUILD/'resources.zip')
with zipfile.ZipFile(BUILD/'resources.apk') as source,zipfile.ZipFile(BUILD/'unsigned.apk','w',zipfile.ZIP_DEFLATED) as out:
    for item in source.infolist():out.writestr(item,source.read(item.filename))
    out.write(BUILD/'dex/classes.dex','classes.dex')
run(align,'-f','4',BUILD/'unsigned.apk',BUILD/'aligned.apk')
store=PRIVATE/'camera-signing.p12'
if not store.exists():raise RuntimeError('existing_viewer_signing_key_required')
apk=HERE/'friday-tv-3.2.apk'
run(signer,'sign','--ks',store,'--ks-pass','pass:local-camera-demo','--key-pass','pass:local-camera-demo','--out',apk,BUILD/'aligned.apk')
verify=run(signer,'verify','--verbose',apk)
apk.chmod(0o600)
permissions=run(aapt,'dump','permissions',apk)
badging=run(aapt,'dump','badging',apk)
assert "name='local.tv.camera'" in badging
assert "name='android.permission.INTERNET'" in permissions
assert permissions.count('uses-permission:')==1
report={'package':'local.tv.camera','version':'3.2','permissions':['android.permission.INTERNET'],
    'sha256':hashlib.sha256(apk.read_bytes()).hexdigest(),'bytes':apk.stat().st_size,'signature_verified':True,
    'installed':False,'real_camera_detection_verified':False,
    'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted(SOURCE.rglob('*')) if p.is_file()}}
(HERE/'tv-build.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report))
