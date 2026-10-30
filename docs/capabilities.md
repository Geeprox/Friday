# Capabilities and acceptance limits

| Area | Available code path | Additional acceptance required |
| --- | --- | --- |
| Simulator | Synthetic snapshots, device previews and reviewed sample rules | Does not control hardware |
| Home Assistant | Registry discovery, OAuth, readback and scoped device operations | Your instance, permissions and physical effects |
| Mi Home layout | BakeData/floormap validation, preview, revisioned import, 3D rendering | Your export, accessible files and device positions |
| Cameras | Optional Miloco media/SDK adapters, worker lifetime, bounded frame pipelines | Supported SDK/platform/device and actual video/audio |
| Voice/gestures | Optional local models and explicit bindings to discovered targets | Models, microphones, room conditions and real-person acceptance |
| People/safety | Local feature/review stores and experimental suspected-event paths | No medical, accuracy or household safety guarantee |
| NAS | Bounded read-only indexes and qualified playback paths | Your codecs, clocks, share access and privacy preview |
| Privacy playback | Local protected derivatives, bounded ranges and revocable leases; full-frame protection by default | Optional local detector assets, licenses and scene accuracy; see privacy-playback.md |
| Event evidence | Scoped journal, explicit source/trace/rule relationships and qualified camera/time references | Unrecorded causes remain unknown; metadata and readback are not physical confirmation |
| Agent | Optional ARL connection and bounded proposals | Trusted identity, supported task contract and explicit permissions |

No real household acceptance or installed-build receipt is shipped as generic
compatibility evidence. See deployment.md for prerequisites and privacy.md for
local storage and publication audit scope.
