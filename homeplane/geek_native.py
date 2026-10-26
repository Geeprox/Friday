"""Build the legitimate local responsible application; never changes TCC."""
import json
import plistlib
from pathlib import Path

from .geek_setup import ROOT
from .settings import run_bounded

BUNDLE_ID = "com.friday.home.geek-connector"
BUNDLE = ROOT / "dist/FridayGeekConnector.app"
REGISTER = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"


async def build_helper(directory, node, *, runner=run_bounded):
    directory = Path(directory).absolute()
    contents = BUNDLE / "Contents"
    binary = contents / "MacOS/FridayGeekConnector"
    info = {"CFBundleIdentifier": BUNDLE_ID, "CFBundleName": "FridayGeekConnector",
        "CFBundleDisplayName": "Friday 家庭连接", "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "FridayGeekConnector", "CFBundleVersion": "1",
        "CFBundleShortVersionString": "0.2.0", "NSPrincipalClass": "NSApplication",
        "LSMinimumSystemVersion": "13.0", "LSUIElement": True,
        "NSLocalNetworkUsageDescription": "持续读取你已配对的米家极客版网关，关闭 Friday 控制台后仍可同步设备和规则状态。",
        "FridayRuntimeRoot": str(ROOT), "FridayDataDirectory": str(directory),
        "FridayNodeDirectory": str(Path(node).parent)}
    sources = list((ROOT / "native/GeekConnector").rglob("*.swift"))
    if binary.exists() and (contents / "Info.plist").exists():
        if plistlib.loads((contents / "Info.plist").read_bytes()) == info and all(p.stat().st_mtime <= binary.stat().st_mtime for p in sources):
            code, _ = await runner(["/usr/bin/codesign", "--verify", "--strict", str(BUNDLE)], timeout=10)
            if code == 0:
                code, _ = await runner([REGISTER, "-f", str(BUNDLE)], timeout=10)
                if code != 0:
                    raise ValueError("native_helper_registration_unverified")
                return BUNDLE
    if any(p.is_symlink() for p in (BUNDLE, *BUNDLE.parents)):
        raise ValueError("native_bundle_path_required")
    code, output = await runner(["/usr/bin/xcrun", "swift", "build", "--package-path", str(ROOT / "native/GeekConnector"),
        "--scratch-path", str(ROOT / "data/build-geek-native"), "--configuration", "release"], timeout=60)
    if code != 0:
        raise ValueError("native_helper_build_failed")
    code, output = await runner(["/usr/bin/xcrun", "swift", "build", "--package-path", str(ROOT / "native/GeekConnector"),
        "--scratch-path", str(ROOT / "data/build-geek-native"), "--configuration", "release", "--show-bin-path"], timeout=10)
    if code != 0:
        raise ValueError("native_helper_build_failed")
    executable = Path(output.decode().strip()) / "FridayGeekConnector"
    if not executable.is_file():
        raise ValueError("native_helper_artifact_required")
    import shutil
    binary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(executable, binary)
    binary.chmod(0o755)
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    code, _ = await runner(["/usr/bin/codesign", "--force", "--sign", "-", "--identifier", BUNDLE_ID, str(BUNDLE)], timeout=10)
    if code != 0:
        raise ValueError("native_helper_sign_failed")
    code, _ = await runner(["/usr/bin/codesign", "--verify", "--strict", str(BUNDLE)], timeout=10)
    if code != 0:
        raise ValueError("native_helper_signature_unverified")
    code, _ = await runner([REGISTER, "-f", str(BUNDLE)], timeout=10)
    if code != 0:
        raise ValueError("native_helper_registration_unverified")
    # This is a locally signed debug artifact, never a notarization claim.
    (ROOT / "docs/evidence/geek-native-build.json").write_text(json.dumps({
        "bundle": str(BUNDLE), "bundle_id": BUNDLE_ID, "build": "PASS", "local_signature": "ad_hoc_verified",
        "apple_issued_identity": False, "notarized": False, "device_writes": 0, "permissions_granted": False,
    }, indent=2) + "\n")
    return BUNDLE
