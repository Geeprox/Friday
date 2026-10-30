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
- Protected playback uses separately installed PyAV/FFmpeg, NumPy and OpenCV.
  Their licenses and any codec-specific terms remain with the installed
  distributions; source publication does not bundle those libraries or codecs.
- Optional selective privacy detection can use an operator-provided NudeNet
  3.4.2 model and a pinned Tiny YOLOv3 model. NudeNet's package metadata says MIT
  while its wheel includes AGPL-3.0 license text. No weights or third-party
  detector implementation are redistributed here. Operators must review the
  actual model assets and terms before explicitly configuring them; a hash is
  not a license grant.
- Home Assistant, Xiaomi Home, Miloco and ARL are external applications. Review
  their upstream licenses and account terms before installing. Friday does not
  vendor their implementations, account material or restricted native libraries.
- Detection, face, VAD, ASR and language model weights are not included. Model
  licenses can impose additional terms, including on commercial use. Install only
  weights appropriate for your use; a model digest is not a distribution license.
- Source-only macOS and Android clients contain original app artwork. Household
  screenshots, floorplan textures, photos, video and audio are excluded.

Upstream sources: [Home Assistant](https://github.com/home-assistant/core),
[Xiaomi Home](https://github.com/XiaoMi/ha_xiaomi_home),
[Miloco](https://github.com/XiaoMi/xiaomi-miloco),
[T-Cast reference](https://github.com/DusKing1/ha-tcl-tv),
[Aerial TV reference](https://github.com/pengchujin/aerial-tv).
