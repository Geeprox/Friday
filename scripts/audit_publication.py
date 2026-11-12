#!/usr/bin/env python3
"""Fail closed on private publication inputs. Prints locations, never matches.

Scan the Git file inventory (including untracked source), or an exported directory.
An optional operator-local JSON list supplies additional literal private strings.
This complements a secret scanner; a passing report is not a proof of anonymity.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

EXCLUDED = {'.git', '.artifacts', '.venv', 'node_modules', '__pycache__', '.pytest_cache',
            '.ruff_cache', '.build', 'build', 'dist', '.DS_Store'}
PRIVATE_ROOTS = {'data', 'evidence', '.private', 'credentials'}
PRIVATE_DOCS = {'evidence', 'context'}
BINARY_PREFIXES = ('native/FridayDesktop/Resources/AppIcon/', 'native/FridayTV/android/res/drawable/')
FORBIDDEN_SUFFIXES = {'.sqlite', '.sqlite3', '.db', '.pem', '.key', '.p12', '.mp4', '.mov', '.wav',
                      '.m4a', '.aiff', '.zip', '.tar', '.gz', '.bundle', '.patch', '.ses', '.onnx', '.safetensors'}
# Exact reviewed artwork and synthetic assets; household photos and changed bytes still fail closed.
SYNTHETIC_MEDIA = {
    # Reviewed Godot XR CC0 hand target frames, exact approved runtime pose.
    'homeplane/static/motion-art/godot-xr/targets/hand-ok.png':
        'b7e7822447c8fe2c20dbbdbb04aedd6e8b307a1387f22e97a28cea6678bc025a',
    'homeplane/static/motion-art/godot-xr/targets/hand-yeah.png':
        '79b00385633b8901f9d8e430b657fab8117a88514c655d7e27bc744e615752ff',
    'homeplane/static/motion-art/godot-xr/targets/hand-open_palm.png':
        'ca2b7fd740f6b6880b7938b528636dba42f101eeb447f0a1f3789b42c077672f',
    'homeplane/static/motion-art/godot-xr/targets/hand-point.png':
        '0496407d52e47dc6b9b430f88224a79b4ea693a9d51a26223ca402894fdcbe32',
    'homeplane/static/motion-art/godot-xr/targets/hand-fist.png':
        'c2a89536f5fe3f73bc2d8ce4aeafdb53813f757b8e59a3117a8d3f84d3b09d78',
    'homeplane/static/motion-art/godot-xr/targets/hand-thumb_up.png':
        'd09b29a0967b2590669e80779888bf03211078551136b583746aed9af030a124',
    'homeplane/static/motion-art/godot-xr/targets/hand-thumb_down.png':
        '9b2466c6c247f7c7b1a424fb2093759ce0f8e25dfbd66114ba1403a1b090e77e',
    'homeplane/static/motion-art/godot-xr/targets/hand-shaka.png':
        'f66f36b0ddb4029f334c143135c404753e8e6625ca826330e283308955790549',
    'homeplane/static/motion-art/godot-xr/targets/hand-horns.png':
        'a11384062bdb82eab230547e8e512630aacf906aa1abbc11e62691e7164c4736',
    # Reviewed CC0/MIT body target frames; graphics only, no household media.
    'homeplane/static/motion-art/body/targets/pose-horizontal.png':
        '865ce752808625a1ccea952a2974b4761d9031364c0f358d3ceacfc7f364e45c',
    'homeplane/static/motion-art/body/targets/pose-overhead.png':
        '4291e5ddf523e006c1572c0bef0b34600b881f87415f379ea8f51cecef089383',
    'homeplane/static/motion-art/body/targets/body-hands_up.png':
        'c7cddf777d25585ac1dbcba8bffca3341b40c59fef0b3bdda5117ee5641494fa',
    'homeplane/static/motion-art/body/targets/body-squat.png':
        '55e317003e65d1c03ffae84313e201d7a1e520293a54a4e6a4c6502bd508d651',
    'homeplane/static/motion-art/body/targets/exercise-squat.png':
        '93218eb8655d34675f42e502e1ff49a8d676659dbe3e14c05d9762f875fa6e94',
    'homeplane/static/motion-art/body/targets/exercise-high_knee.png':
        'e31b4e3cca42243f5732c13819909b0fd2bf8c218fe8dbf6fba460896a09785a',
    'homeplane/static/motion-art/body/targets/body-arms_open.png':
        '3c27a8972de19194a31fe6d07890f59a37e07edb56f320f99e00bc311dd1849a',
    'homeplane/static/motion-art/body/targets/body-arms_close.png':
        '8aecef9c803483a84499f999f98a3cd36d3e9eb261559b7d6ef71099b56e5107',
    'homeplane/static/motion-art/body/targets/body-left_hand_up.png':
        'fe8c238bdf593ce438a32347ef2da091f08600647e90495ded77f05f840a7366',
    'homeplane/static/motion-art/body/targets/body-right_hand_up.png':
        '1d893f9e72b64a1e5ed0ab8ecc1b36eaeb1fa4c7b194700fca836a3493828075',
    'homeplane/static/motion-art/body/targets/body-lean_left.png':
        '4899da4b968d1234c24f4b0492515597996d59f75d6272f27c7fbb8726097934',
    'homeplane/static/motion-art/body/targets/body-lean_right.png':
        '391c7b12b5a9012fe583b8b004fdbc10f99756b3f782fd8330ca8ff6630c761d',
    'homeplane/static/motion-art/body/targets/body-lateral.png':
        'bd4223e80f5bab67b266f234318eb985533ee96273bc49b64f261fb43a528113',
    'homeplane/static/motion-art/body/targets/body-arm_effort.png':
        '0c7e6d68c08d31e13826547f6ea839e443abdbe02c774cf8ad9e089b7b241f4a',
    'homeplane/static/motion-art/body/targets/body-swing.png':
        '43ec0dc00d05867b0b1b1aa52f1623ea490d0d2061c51e63cb8f5339a12db238',
    'homeplane/static/motion-art/body/targets/exercise-jack.png':
        '860c045ef0ef34bb8720b45e2d3733e8806d31362aeda370c46bdf4f5790c477',
    'homeplane/static/motion-art/body/targets/exercise-side_lunge.png':
        'a77f853f9cc3d3212bb5ffff09c1f7d6fd9a9c2ae33ea78d22232c1ceef550a3',
    'homeplane/static/motion-art/body/targets/exercise-side_step.png':
        '2505a47e2a477d3aae76a73063646d1205374fd8f4be6bb1d1d4de424ba759bb',
    'homeplane/static/motion-art/body/targets/exercise-side_bend.png':
        'afff12657b4e11f510a8037b645a3806f89ab31ef3f34c73d17f0b072b39e0ea',
    'homeplane/static/motion-art/body/targets/exercise-balance.png':
        'c1ee438bd4f1b9f16daac38a0e4e9a7c2ffa101efc482c709051815a0e1d9278',
    'homeplane/static/motion-art/body/targets/exercise-cross_knee.png':
        '38005bc9a21ce52217a5987f955dfe418d5cbe4162d0fd6c3cd288e710f82802',
    'homeplane/embodied/static/game-art/quest-lantern.webp':
        '08069e55ec432673bcf9c6e53e3f008162c1a6baa87626525ed2c2777d8f6137',
    'homeplane/static/motion-art/body/targets/exercise-side_raise.png':
        '97466f4b4992631b62631d1c5713adfc15ef326598b0dc4b20048ae50e99859b',
    'homeplane/static/motion-art/body/targets/exercise-overhead_clap.png':
        '810ace5e89e85db9a3a9865f292102626a6f8c561308c30f1ae2f33030cba5c0',
    # Original game environment art; exact prompts/provenance in adjacent JSON.
    'homeplane/embodied/static/game-art/winter-props.png':
        '73bba0b51c57e6470ea71bb87761a3d9b264a7f63844ea6468fb4f26fc6aebb0',
    'homeplane/embodied/static/game-art/winter-scene.png':
        '00cc2c43e17c27d2267b8a10a36c4b306fd8ae8a021ad3ef497909a09675c1a7',
    'homeplane/embodied/static/game-art/summer-props.png':
        '61194860c731159938e91730954b0279cdc0c0958a1d3b9f1f5d7bb38d53172c',
    'homeplane/embodied/static/game-art/summer-scene.png':
        '37b327817aa118f18ab82064f9d4fb3ec53853261880a07d5ae4dfb9f240bafb',
    # Original generated forehand cels; inspected identities, outfits and alpha.
    'homeplane/embodied/static/game-art/tennis-near-v1.png':
        'c81055804c8ddc6746604becfaaa716c2e341113076e46707891c36927ebbd92',
    'homeplane/embodied/static/game-art/tennis-far-v1.png':
        'a4bdd64dcc50ceeea527b68b3c6ddc8f8ecf1f6c5cd296086e581f4f2c18ba67',
    # Original sibling adventure chapters and alpha atlas; adjacent exact prompts.
    'homeplane/embodied/static/game-art/quest-stream-scene.png':
        '8b8acf2a4abcf31746d6615f296773e65f43b970a0a8afad41a5b38d3d43a9ae',
    'homeplane/embodied/static/game-art/quest-camp-scene.png':
        'e74e788f21f6cf86073f8b3ca4e9ae8bae722d832c0ec1c80877ccec1fc9692d',
    'homeplane/embodied/static/game-art/quest-props.png':
        '9ae5941d6cc82ef6e726d494cfd786a8abad0e3949670e762e7021cdc088c17a',
    # Original game-cover illustrations; prompt provenance embedded in each file.
    'homeplane/static/games-art/ski.png':
        'c0b1187185e8aed146aaae4119e4dc7fa85bc708ddf6258d0946e5cb0e27254d',
    'homeplane/static/games-art/tennis.png':
        '2ed81d8cb7d8077b0da4ad33a87b50e37107a16bd962b978ca7e68418c21cd7f',
    'homeplane/static/games-art/quest.png':
        'efc7d75832055e8b4a95a2b30ab962b11af6e97cdb0974d6ead7ecb448bdf7a0',
    'homeplane/static/games-art/hiit.png':
        '9dd54083dd5cf4cc6ba4fb72634b9a7b4e0a7c2adca2590f2749a37190530d10',
    'homeplane/static/games-art/werewolf.png':
        '65c686067b13e7ec6b8d9475c31da9ddeca5253cae95160a41404d49ae827c24',
    'homeplane/static/games-art/ktv.png':
        '11b30030c8ba0cce72f2561b23e7de98f6ae0bc38010fa557e2d7368b2c5e2a4',
    # 光线保卫队原创生成美术；提示词、原图指纹和派生方式记录在相邻 JSON。
    'homeplane/static/games-art/beam.webp':
        '29f03a4aeffb8945117b50e64c68590d15f7b3bc53e93d0031d64bc1e2ef802d',
    'homeplane/embodied/static/game-art/beam-glade.webp':
        '7e4399ee70e6a8bd69b4409fdd5e77b310dd5f541b3d94e53b5a44523b94802d',
    'homeplane/embodied/static/game-art/beam-fluff.webp':
        '10dfe79fb36ff7dc13b12eef413b7c8cfaa230f5306e7c0239e4baabfeb1100c',
    # Approved Friday Icon, unchanged pixels with embedded origin metadata.
    'homeplane/static/brand/friday.png':
        'dba243be53573b3b2ec7572852cf23075c6b851c72c417d2c4d9a7396cca2d6e',
    'native/KTVTV/android/res/mipmap-hdpi/banner_friday_ktv.png':
        '8ed971843fb3bb4ad17627c62d6a5b742e49b33b6b598255c33b1d25928bb10e',
    'native/KTVTV/android/res/mipmap-hdpi/ic_friday_ktv.png':
        'd51d3446233c7dcb109a88ddd8a7bba44b2fc1fc167776dc4596acd6881eb736',
    'native/KTVTV/android/res/mipmap-hdpi/ic_friday_ktv_background.png':
        '3c0ca00a1894f1b82708155c63db46392cf34d5519a7be92cae79d287f51588d',
    'native/KTVTV/android/res/mipmap-hdpi/ic_friday_ktv_foreground.png':
        'a4499032e171c1828353b852d2f27245d595ea0708dafe9e47b5a62a2458ed6c',
    'native/KTVTV/android/res/mipmap-mdpi/banner_friday_ktv.png':
        '89dd3411f17124481a59b914f4948f76c36ee072094bc59725457cec077749ce',
    'native/KTVTV/android/res/mipmap-mdpi/ic_friday_ktv.png':
        '2a1a12aaed3556dc5686476f3c26adc5d7a5cba23713392108ef917a230f1d1b',
    'native/KTVTV/android/res/mipmap-mdpi/ic_friday_ktv_background.png':
        'cc42cda658b3d714869d06ac73d0a5db1965efe2e9dfa73cc0eba33bc86eb949',
    'native/KTVTV/android/res/mipmap-mdpi/ic_friday_ktv_foreground.png':
        '473111f1f97e3487754dacc3633d33743202751739a3240324347f9e7329db07',
    'native/KTVTV/android/res/mipmap-xhdpi/banner_friday_ktv.png':
        '9d1dd706449c680a73425541f0df65f65f5a17d70bec7a859600ca066578816c',
    'native/KTVTV/android/res/mipmap-xhdpi/ic_friday_ktv.png':
        'd0717ba52994ed40672e8aea817cc7b8698aa9def0de24f57c8f33fe0b13fd1f',
    'native/KTVTV/android/res/mipmap-xhdpi/ic_friday_ktv_background.png':
        '0e78db8bb5b5b462c68a8cc1c1fdd1fa27b5f06dd3e0494e79d74ee40303ecdc',
    'native/KTVTV/android/res/mipmap-xhdpi/ic_friday_ktv_foreground.png':
        'c83bcab603f80d60289641f139f9148402d687901df08d175b91882304576740',
    'native/KTVTV/android/res/mipmap-xxhdpi/banner_friday_ktv.png':
        '8487953ee759afde5e64fa256178921c173b7a25117004e40ee593660aeb9e42',
    'native/KTVTV/android/res/mipmap-xxhdpi/ic_friday_ktv.png':
        'e16a495bb297f79ecdc5fe9777627fba0dd57dd64d18f400b6819f5a471c8e92',
    'native/KTVTV/android/res/mipmap-xxhdpi/ic_friday_ktv_background.png':
        '9a7fed6edbb23f5119e4fa0175eeb39f43a0f1600e78f402c3d4fe96930a1887',
    'native/KTVTV/android/res/mipmap-xxhdpi/ic_friday_ktv_foreground.png':
        '0a8c4b1a61c9a8f3c88909ecc508acf502178971f73f0bb5d24bbb7a2e4cdb19',
    'native/KTVTV/android/res/mipmap-xxxhdpi/banner_friday_ktv.png':
        '511b124c9ecde4676c9cbe62254a74cc762eef53396b6f7a2c874356570030f8',
    'native/KTVTV/android/res/mipmap-xxxhdpi/ic_friday_ktv.png':
        '0d5bc19ccd37256c23fe7fef8041fedca3533d3cf84fe7c2d64b7d0653ccec04',
    'native/KTVTV/android/res/mipmap-xxxhdpi/ic_friday_ktv_background.png':
        '15dc377d42fbb5870f91045065cc0e63d8e0a092396a3fbc1b6dfb252a11d696',
    'native/KTVTV/android/res/mipmap-xxxhdpi/ic_friday_ktv_foreground.png':
        '3920b5a84145ab73a0bdb91aca4d84534b00966973e17af6a427d2fb8aba9471',
    'fixtures/homeplane/archive-simulated.mp4':
        'a1f318cbd2d02ce033bc05256938d48edf26cd440abd4df01bb6b2a57999f038',
    'native/KTVTV/android/assets/avatars.png':
        '080c89adc8c4c9101d9ec6199122f7ad95b9a10772bedf2de99bef8eb1a7ee38',
    'native/KTVTV/android/assets/content.png':
        '698c3ff2823457e6d8b560def271f6329e8403761155647022454d98bb5d85a5',
    'native/KTVTV/android/assets/festival.png':
        'b8bf5b6294f9b0052d78ad5405abbf0ec8c29303488312bffe5353aa697b5ac4',
    'native/KTVTV/android/assets/live.png':
        '71c2552210e39c864ed78206c1f747e7cf157870685a9d4374b80e9621defe5b',
    'integrations/ktv_public/web/avatars.png':
        '080c89adc8c4c9101d9ec6199122f7ad95b9a10772bedf2de99bef8eb1a7ee38',
    'integrations/ktv_public/web/content.png':
        '698c3ff2823457e6d8b560def271f6329e8403761155647022454d98bb5d85a5',
    'integrations/ktv_public/web/festival.png':
        'b8bf5b6294f9b0052d78ad5405abbf0ec8c29303488312bffe5353aa697b5ac4',
    'integrations/ktv_public/web/live.png':
        '71c2552210e39c864ed78206c1f747e7cf157870685a9d4374b80e9621defe5b',
}
RULES = {
    'personal_home_path': re.compile(r'(?<![\w/])/(?:Users|home)/[A-Za-z0-9_.-]+/'),
    'private_key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'github_token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})'),
    'cloud_access_key': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'provider_key': re.compile(r'\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}'),
    'jwt': re.compile(r'\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}'),
    'mac_address': re.compile(r'(?i)\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b'),
    'email': re.compile(r'\b[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
}
# Public CC0 assets reviewed with source, author and conversion receipts in
# native/KTVTV/dependencies/sfx.json. These are not household recordings.
LICENSED_AUDIO = {
    'native/KTVTV/android/assets/sfx/applause.wav':
        'e5c7b5f7285168da91fcfde8843e40b0ac0ceb296cb049c632040b16d106c414',
    'native/KTVTV/android/assets/sfx/cheer.wav':
        '987f42917c5e37aa51248062bbd28f9d648306b2c890a20f2edbe2803fa37d8b',
}
# Public upstream attribution is not household data. No real email is required.
SAFE_EMAIL_DOMAINS = {'example.com', 'example.org', 'example.invalid', 'friday.invalid'}


def inventory(root):
    if (root / '.git').exists():
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '-co', '--exclude-standard', '-z'],
                                check=True, capture_output=True)
        return sorted({name for name in result.stdout.decode().split('\0') if name})
    return sorted(p.relative_to(root).as_posix() for p in root.rglob('*')
                  if p.is_file() and not any(part in EXCLUDED for part in p.relative_to(root).parts))


def scan(root, private=()):
    findings, records = [], {}
    for name in inventory(root):
        path, parts = root / name, Path(name).parts
        if not path.exists():
            continue
        if path.is_symlink():
            findings.append({'path': name, 'rule': 'symlink'})
            continue
        if name in LICENSED_AUDIO:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != LICENSED_AUDIO[name]:
                findings.append({'path': name, 'rule': 'licensed_audio_changed'})
            else:
                records[name] = digest
            continue
        if name in SYNTHETIC_MEDIA:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != SYNTHETIC_MEDIA[name]:
                findings.append({'path': name, 'rule': 'synthetic_fixture_changed'})
            else:
                records[name] = digest
            continue
        if (parts[0] in PRIVATE_ROOTS or any(part in EXCLUDED for part in parts)
                or parts[:1] == ('docs',) and len(parts) > 1 and parts[1] in PRIVATE_DOCS
                or path.name.startswith('.env') and path.name != '.env.example'
                or path.name.endswith(('.local.json', '.home.json', '.local.yaml'))
                or path.suffix in FORBIDDEN_SUFFIXES):
            findings.append({'path': name, 'rule': 'private_file'})
            continue
        raw = path.read_bytes()
        records[name] = hashlib.sha256(raw).hexdigest()
        try:
            value = raw.decode('utf-8')
        except UnicodeDecodeError:
            if not name.startswith(BINARY_PREFIXES):
                findings.append({'path': name, 'rule': 'unreviewed_binary'})
            continue
        for rule, pattern in RULES.items():
            for match in pattern.finditer(value):
                if rule == 'email' and match[0].rsplit('@', 1)[1].lower() in SAFE_EMAIL_DOMAINS:
                    continue
                if rule == 'email' and match[0].endswith('@2x.png'):
                    continue
                # Generated/vendor attribution retains the upstream license.
                if rule == 'email' and ('/vendor/' in name or path.name in {'LICENSE', 'THREE-LICENSE.txt'}):
                    continue
                findings.append({'path': name, 'line': value.count('\n', 0, match.start()) + 1, 'rule': rule})
        for index, literal in enumerate(private):
            if literal and literal in value:
                findings.append({'path': name, 'rule': 'operator_private_literal', 'pattern_index': index})
    return {'schema_version': 1, 'files': len(records), 'findings': findings,
            'source_sha256': hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(),
            'records': records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--private-patterns', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    private = json.loads(args.private_patterns.read_text()) if args.private_patterns else []
    if not isinstance(private, list) or any(not isinstance(x, str) for x in private):
        parser.error('private patterns must be a local JSON list of strings')
    result = scan(args.root.resolve(), private)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        args.output.chmod(0o600)
    print(json.dumps({k: v for k, v in result.items() if k != 'records'}, indent=2))
    raise SystemExit(bool(result['findings']))


if __name__ == '__main__':
    main()
