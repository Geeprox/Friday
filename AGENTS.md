# Working on Friday

Read README.md, docs/deployment.md and docs/privacy.md before setup. Keep secrets,
household exports and generated reports under ignored data/ or .artifacts/.
Never request passwords, tokens, pairing codes or family exports in chat/commits.

## Deployment workflow

1. Run `python3 scripts/setup-dependencies.py plan`. Inspect OS, Python, uv, Docker,
   Node and already configured services. Reuse an existing HA instance.
2. Run `scripts/install-home.sh` and verify `bin/home doctor` and a Simulator
   snapshot. Simulator is the default and must remain visibly simulated.
3. For new HA, prepare with setup-dependencies.py. Container deployment requires
   Linux Docker Engine; macOS uses a Linux VM/remote HA. Configure timezone locally.
4. Install the pinned Xiaomi Home integration; complete account login in HA UI.
   Miloco is optional: download its pinned source with the helper, inspect official
   install instructions and use a supported agent platform. Do not invent a Codex
   or Docker installer for the current Miloco plugin architecture.
5. Discover rooms/entities/devices through HA registries. MIoT property IDs come
   from imported specs. Never identify a target by an author's model, IP, name or
   coordinates. Unknown/ambiguous capabilities must fail closed.
6. Import the user's Mi Home layout with the import script. Preview first; retain
   stable IDs, holes, missing positions and source coordinate basis. Never ship
   a captured home map, texture, compass direction or default household geometry.
7. Report each gate separately: dependency installed, service started, login,
   readback, requested control and physical user confirmation. Do not infer success
   from HTTP 200, ACKs, playing, decoded frame counts or synthetic tests.

## Development

Do not start cameras, microphones, models, audio, television or device actions as
part of ordinary tests. Optional hardware work needs scope from the user. Do not
expand a setup request into cloud inference or recurring device automation.

Use isolated, temporary paths for tests. Test receipts belong under .artifacts/.
Run the locked test environment and publication scanner before a release. Scan a
fresh exported clone as well as the source worktree. A clean current tree does
not sanitize old commits/branches/releases: keep private history private and
publish only reviewed clean history. Never push `--mirror` or `--all` to public.
