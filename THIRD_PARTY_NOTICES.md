# Third-party components

Friday's MIT license covers original project code. It does not relicense external
software, model weights, generated upstream protocol schemas or vendor SDKs.

- Three.js is distributed under MIT. Its original notice is retained at
  `homeplane/static/vendor/THREE-LICENSE.txt`.
- Tabler Icons are distributed under MIT. Their original notice is retained at
  `homeplane/static/icons/LICENSE`.
- The optional T-Cast diagnostic tooling references the MIT-licensed protocols in
  DusKing1/ha-tcl-tv and pengchujin/aerial-tv; upstream attribution remains in source.
- The adapted Geek read-only client is MIT-licensed by oh-my-sage. Its original
  notice is retained at `workers/geek/vendor/LICENSE`. The curve adapter uses
  independently installed MIT-licensed noble-curves and bn.js.
- Python/Node dependencies are independently installed from `uv.lock` and
  `workers/geek/package-lock.json`; licenses remain with their respective authors.
- The optional local KTV source adapter installs MIT-licensed
  `@neteasecloudmusicapienhanced/api` 4.41.1 and its dependencies from
  `integrations/ktv_source/package-lock.json`. Their notices remain in the
  independently installed packages. This is an unofficial API implementation;
  it does not grant music rights or change upstream account permissions.
- Home Assistant, Xiaomi Home, Miloco and ARL are external applications. Review
  their upstream licenses and account terms before installing. Friday does not
  vendor their applications, account material or restricted native libraries.
- A desktop candidate may include a reviewed MIT ARL Python SDK/shared-library
  subset from [Geeprox/ARL commit a48e8bf](https://github.com/Geeprox/ARL/commit/a48e8bfd15b9ff7716a6a52a3ef9fcdbe915a585).
  It retains upstream `LICENSE`, `NOTICE` and `pyproject.toml` beside
  `ARL-SDK-MANIFEST.json` in `Contents/Resources/product/vendor`. The manifest pins
  each unmodified source file. This does not distribute the ARL app, interpreter,
  model weights, private adapters, credentials or external command-line binaries.
  The canonical import contract is documented in `docs/arl-application.md`.
- Detection, face, VAD, ASR and language model weights are not included. Model
  licenses can impose additional terms, including on commercial use. Install only
  weights appropriate for your use; a model digest is not a distribution license.
- Source-only macOS and Android clients contain original app artwork. Household
  screenshots, floorplan textures, photos, video and audio are excluded.
- The optional KTV native audio library is compiled with Android NDK r27d. Its
  LLVM/libc++ runtime uses Apache-2.0 with the LLVM exception; the notice is
  retained in `native/KTVTV/android/assets/LLVM-NOTICE.txt` and the APK assets.
- KTV's bounded duplex diagnostic uses Google Oboe 1.11.0 under Apache-2.0.
  The exact commit and source hashes are pinned in `native/KTVTV/dependencies/oboe.json`;
  its license is retained in `native/KTVTV/android/assets/OBOE-LICENSE.txt` and the APK.
  Upstream: https://github.com/google/oboe/tree/1.11.0.

Upstream sources: [Home Assistant](https://github.com/home-assistant/core),
[Xiaomi Home](https://github.com/XiaoMi/ha_xiaomi_home),
[Miloco](https://github.com/XiaoMi/xiaomi-miloco),
[T-Cast reference](https://github.com/DusKing1/ha-tcl-tv),
[Aerial TV reference](https://github.com/pengchujin/aerial-tv).

Friday KTV's short applause and cheering effects are adapted from BigSoundBank
recordings released under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/):
[Applause #1](https://bigsoundbank.com/applause-1-s2363.html), by Dorian CLAIR,
and [Shouts and Applauses of Teens #2](https://bigsoundbank.com/shouts-and-applauses-of-teens-2-s0237.html),
by DenisChardonnet. Changes: shortened, loudness normalized, faded at the edges,
and converted to 48 kHz stereo PCM16. Exact asset hashes are recorded in
`native/KTVTV/dependencies/sfx.json`.
Friday's gesture teaching uses the Godot XR Tools hand model by DigitalN8m4r3
(Miodrag Sejic), released under CC0-1.0, and the project's original hand-pose
resources and code under MIT. Source is pinned to
`6c6189bdfcdf855052ef1aeb9f3e0ec4c7a7eb60`. The source mesh is subdivided once
to smooth its contour; source finger-pose quaternions are preserved. Friday adds
orthographic line rendering, transitions, hold/reset timing, and rotates the same
Thumb pose for the up/down demonstrations. Files, exact origins and hashes are
in `homeplane/static/motion-art/godot-xr/`; both upstream notices are retained
there. Rendering reuses the existing MIT-licensed Three.js distribution.
# Continuous body tutorials

The continuous instructional mesh and anatomical weights in
`homeplane/static/motion-art/body/body.gltf` derive from bundled MakeHuman
assets at commit `1f508f6083b2f823dab15de924b3bde72e08d77c`, released under
CC0. No MakeHuman application code is included or executed. The same file
contains retargeted motion data from Desk Break at commit
`8d475aa476b2c0e9ec6d09d8558faf17161e19ef`, MIT, copyright (c) 2026 Shlomy
Reshef (parametric.co.il). The adapter is Friday MIT code and reuses the
existing Three.js runtime. Original notices and exact conversion details are
adjacent to the asset; graphics do not establish recognition accuracy.

The teaching avatar wears three real CC0 MakeHuman garment meshes: the
Elvaerwyn `elvs_crude_t-shirt_male` T-shirt, MargaretToigo `toigo_wool_pants`
trousers, and system `shoes02` sneakers (Data Collection AB, Joel Palmius,
Jonas Hauquier). Exact official sources, file hashes and fitting adaptations
are recorded in `homeplane/static/motion-art/body/CLOTHING-LICENSES.md` and
`provenance.json`. Covered anatomical faces are hidden; the clothes use their
own source geometry and the existing skeleton. Three additional motion clips
are explicitly Friday-authored MIT adaptations, identified in
`authored-poses.json`; they are not attributed to Desk Break.
