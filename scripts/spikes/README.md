# Historical direct-provider experiments

These files preserve the earlier Laya/Jev/Codex implementation and its synthetic tests.
They are not imported by Home Console or the legacy Friday application and are not
the current provider integration. Do not use their tests/benchmark as ARL acceptance.

The product uses `homeplane/arl.py` and the upstream `arl-runtime` SDK. Home task
calibration, unknown, action authority and verification remain in Home Core.
The legacy local perception host is retired; ARL's current Laya HTTP compatibility
adapter may therefore report unavailable until ARL owns a usable model service.
Existing benchmark JSON remains historical evidence, not current runtime readiness.

The old `check_runtime` and `benchmark_home_decision` commands are also archived here.
Their former public entrypoints refuse direct execution and point to the ARL check.
The archived benchmark writes to `data/spikes/`, not the current acceptance evidence.
