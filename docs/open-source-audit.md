# Open-source review · 2026-10-26–27

This report describes the reviewed source release, not physical acceptance of a
particular home, device, camera, microphone, speaker, television or AI model.

## Publication boundary

The development branches were consolidated in a private integration history.
The public repository starts from a newly exported source tree with a fresh root
commit. Historical private branches, tags, backups, household exports, runtime
state, screenshots and field reports are excluded. The original repository and
local recovery bundles remain private; their histories are not public inputs.

The frozen publication contains 1,405 reviewed files, including source, tests, deployment scripts, workflow files,
documentation, dependency locks and included assets. The only video fixture is a
hash-pinned generated color-bar sample. App icons and third-party artwork/license
notices were retained. No model weights or restricted native SDKs are distributed.
A fresh export and its complete reachable Git history are scanned before public
visibility is enabled. The publication scanner also supports operator-private
literal checks without publishing the literal list or matched values.

## Generic deployment and discovery

- Default installation uses a visibly simulated home, without a household login.
- HA entities, stable identifiers, areas and capabilities come from registries.
  Unknown or ambiguous targets fail closed. MIoT numeric property identifiers and
  motor values come from imported specs rather than a particular device model.
- Actual house boundaries, holes, device coordinates and source coordinate basis
  come from the user's Mi Home import. There is no built-in household geometry or
  compass direction. Missing positions remain unplaced.
- Background recognition requires an explicit saved enable action. Account login,
  camera use, model installation and real device actions are separate steps.
- Agent deployment instructions reuse existing services. New HA runs on Linux
  Docker Engine with private persistent configuration and pinned Xiaomi Home.
  Miloco is optional and follows its pinned upstream OpenClaw/Hermes guides.

## Verification

- Locked Python 3.12 regression: the full frozen-export suite and the later
  integration regressions passed; external integration tests
  requiring unavailable SDKs, model artifacts or physical inputs are skipped.
- Audio logic and Geek cryptographic handshake/negative cases: 7 Node tests pass.
- Native SwiftPM release compilation, ad-hoc signature verification and packaged
  source hash checks pass. No App installation, launch or hardware claim follows.
- Fresh-directory base install, doctor, Simulator snapshot and loopback server
  were exercised. Browser review confirmed the example label, no imported layout,
  unavailable real services and explicit simulation on the connection screen.
- Publication scanner and Gitleaks report no findings on the publication inputs.
  Upstream notices and digest allowlists are narrow and retained in source.
- npm audit reports 0 known vulnerabilities. Official OSV queries cover all 117
  registry package/version pairs in uv.lock with 0 vulnerable packages at review.
  The installed Python environment also passed pip-audit. Earlier findings in
  ws, elliptic, certifi, Pillow, Paramiko, filelock and setuptools were addressed.
- CI repeats regression on Linux/macOS, dependency audits, publication/history
  scanning and the macOS source build. Workflow actions use fixed commit pins.

Automatic scanners cannot prove that every possible personal detail is absent,
nor certify upstream software. This review covers the submitted clean source and
its reachable public history. It does not make private development history safe
to publish, validate every optional platform, or replace physical user acceptance.

See [deployment](deployment.md), [privacy](privacy.md), [layout import](layout.md)
and [desktop build](FRIDAY-DESKTOP-BUILD.md) for installation and evidence limits.
