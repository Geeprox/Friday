# Friday People: live-camera identity review

This source-only module reviews candidate people from camera identity observations.
It does not replace Synology photo organization, train a model, enroll household
security credentials, or touch NAS originals. Construction creates only dedicated
local People SQLite tables. No import starts a camera, microphone, NAS reader,
recognition provider, model loader, scheduler, or home-control operation.

## Product surface

The compact Chinese People workspace follows Friday's light editorial-tech
console: restrained sidebar, quiet row list, one observation inspector, precise
sample selection and short action/status copy. It supports naming/registration,
explicit survivor selection when merging different labels, selected-observation
split, single-observation move, exclusion/restoration, monotonic-revision undo,
and candidate voice confirm/reject/unlink. Geometric tiles are labeled synthetic;
they are not face photos or evidence that an actual tracker works.

An automatically proposed cluster remains a candidate. A user naming it saves a
media identity label only. Every person/page has `action_authorized=false` and
`security_authentication=false`; no household membership or trust grant exists.

## Finite contracts and persistence

`contracts.py` supplies strict version-1 public metadata contracts and finite
review commands. Public JSON has no embedding/template/media field, upload,
inference, enrollment-template or activation endpoint. Extra request fields are
rejected. All successful review/page responses use `Cache-Control: no-store`.
No model, media or NAS paths are accepted.

| Bound | Maximum |
| --- | --- |
| Face input per synthetic batch | 256 observations, existing 128-dimensional face vectors |
| Samples per supplied camera/track | 3; no tracker is inferred when track is absent |
| Retained local observation metadata | 512 |
| Human person records / candidate voice links | 128 / 256 |
| Pairwise split constraints / undo snapshots | 4,096 / 32 |
| Voice evidence per link | 32 deduplicated windows |
| Merge selection / representative samples | 8 groups / 3 per group |

Use an owner-supplied **dedicated local SQLite connection**, not a NAS location or
shared uncommitted TraceStore transaction. Synchronous API use requires
`check_same_thread=False`; this catalog serializes its connection with an RLock.
`BEGIN IMMEDIATE` plus `expected_revision` rejects stale edits across connections.
Undo restores only the last review snapshot and always advances the revision.
Reindex/evidence ingestion clears undo, preventing an old undo from erasing new
producer data. Interrupted transactions roll back. No original catalog table is
read or mutated.

Observation IDs derive from source observation provenance and a frame/slot
locator, independent of library label/order/model version. The recognition bridge
uses the consumer's exact observation ID. Person IDs and naming/correction anchors
persist separately from automatic partitions. Missing observations become inactive;
labels remain. Different human groups cannot be automatically merged. Split
constraints remain across reindex. Explicit merge keeps superseded named people
as aliases, and undo restores the previous labels and memberships.

## Clustering adapter and license

The optional adapter uses mature scikit-learn
`AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="complete",
distance_threshold=1 - accept_similarity, compute_full_tree=True)` over normalized
vectors sorted by canonical sample IDs. The caller never builds a dense distance
matrix. The library's internal pairwise memory remains bounded by the 256-sample
cap. Complete-link output is defensively checked against all pair similarities;
review constraints only filter proposals. Singleton/below-threshold observations
remain unknown; observations close to competing named groups within the qualified
margin remain ambiguous. Similarity is never a probability.

The owner must supply a model-bound qualified threshold/margin policy. The demo's
`.90/.04` values describe synthetic vectors only, not SFace accuracy or a deployment
default. Model/simulation/quality mismatch prevents matching. Model family and
vector dimensions reuse `homeplane.recognition.contracts`; face and speaker spaces
are never compared. A provider/model/dimension replacement requires a reviewed
contract change.

scikit-learn is [BSD-3-Clause](https://github.com/scikit-learn/scikit-learn/blob/main/COPYING),
and its [official complete-link API](https://scikit-learn.org/stable/modules/generated/sklearn.cluster.AgglomerativeClustering.html)
supports this configuration. No third-party implementation or model weights are
vendored. No dependency is installed or downloaded in this packet. The optional
runtime is absent in the inspected environment: actual library execution remains
unqualified. Contract tests and the demo use an explicit predetermined synthetic
partition fake and record `last_run.backend="synthetic-contract-fake"`; there is no
custom substitute clustering algorithm. Missing library support returns
`people_clustering_dependency_unavailable` atomically.

## Exact recognition dependency and remaining gap

`recognition_bridge.face_input` consumes the recognition owner's exact
`RecognitionResult`/`RecognitionSession` from source commit
`02c8575eefc9c8f2c86950663aa5288f667ef3f2`. Frozen
`homeplane/recognition_contracts.py` SHA-256:
`c8652e52da327da648196d3513819214cbbf35508ab36c8cd5ffffdb0c069e19`.
Its dependency snapshot is committed separately in this isolated checkout and is
**excluded from the People adoption patch**. Adopt it through its existing owner.

The bridge fences exact session/generation, modality, model hash, simulation,
quality and embedding production. It retains the observation ID and worker
monotonic observation time, leaves source clock unverified, and keeps track null.
The current consumer returns no vectors and discards its ephemeral embeddings.
Passing its serialized report alone fails with
`people_embedding_handoff_unavailable`. Consequently this packet does **not** wire
actual camera clustering. The separately supplied feature-handoff proposal defines
an internal worker-local callback; it is unwired and changes no public JSON.

`FaceInput` accepts trusted synthetic embeddings only. There is no HTTP admission
path for feature arrays or runtime selection. Embeddings are consumed during one
batch and never enter SQLite, API output, repr, logs or screenshot artifacts.
No person templates are persisted by default. Python reference release is not a
memory-zeroization guarantee; process lifetime remains owner-managed.

## Voice association

The real active-speaker dependency remains explicitly unavailable. A recognition
speaker embedding, RMS activity, a shared timestamp, simultaneous appearance,
or `av_association="unavailable"` cannot produce a link. No active-speaker model or
checkpoint is adopted; model-code terms alone do not qualify weights.

The trusted `SpeakerAgreement` seam is a **synthetic-only evidence fixture**.
All speaker/acoustic qualification, single-speaker, overlap-free, active-speaker,
AV-alignment, track-continuity and competitor-margin flags must be true. Three
distinct windows from three distinct admitted utterance events are necessary.
Window replay cannot increase agreement. Competing person evidence holds links
stale until the contradictory link is explicitly rejected; no nearest-person
fallback occurs. Confirmation is a separate revision-fenced user decision.
Rejection and unlink survive fresh evidence. Membership changes invalidate old
evidence and confirmation. Face↔voice links convey neither liveness/presence nor
device/security authority, including for perfectly lip-synced television content.

## Run synthetic checks

Use Friday's already installed Python environment; no installs:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_people_catalog.py tests/test_people_recognition_bridge.py tests/test_people_api.py -q
.venv/bin/ruff check homeplane/people tests/test_people_*.py
```

`python -m homeplane.people.demo` supplies an opt-in loopback-only **synthetic**
in-memory FastAPI fixture. It never imports Friday's actual app/SDK composition.
The session environment disallowed loopback binding, and browser policy disallowed
local-file rendering. Neither restriction was escalated or bypassed. The separate
`output/playwright/people-preview.html` embeds the exact product UI with a read-only
public metadata fixture; screenshot capture was unavailable. API mutations are
verified in-process with FastAPI TestClient, including stale revisions and strict
route schemas. Source fixtures contain procedural vectors, synthetic labels and
geometric placeholders only.

## Owner integration proposal (not applied)

1. `homeplane/api.py`: after independent adoption, construct a dedicated People
   connection in Friday's owned output/data directory and mount
   `create_router(catalog)` under existing authentication/origin/body guards.
   Add `/api/people` to `local_boundary`'s strict Origin-required path prefix;
   merely mounting the router does not opt it into that protected write list.
   Use owner-managed shutdown to close that connection. Do not mount the demo.
2. `homeplane/settings.py`: keep recognition disabled. No automatic visitor
   enrollment, activation permit, feature upload, grant or live-source setting.
   Optional clustering dependency/lock changes require their own version/ABI
   qualification; this packet changes neither pyproject nor lockfile.
3. `homeplane/static/index.html`: load the additive People stylesheet/module.
   `app.js`: add a 人物 navigation entry, call
   `mountPeople(content,{endpoint:"/api/people",embedded:true})`, and destroy its view on leave.
   Scope page refresh to its own revision, not Core device state.
4. Existing recognition worker: no hook is installed. Review the separate
   worker-local ephemeral callback/metadata proposal first. A single current
   observation yields unknown; batching/tracking/enrollment/calibration require
   additional finite contracts. Never add vectors to public RecognitionResult.

The original repository, shared API/settings/console files, held actor, native
media boundaries, original/index/HEAD and deployments are unchanged. No private
records, camera/microphone/NAS access, model runtime, new permissions or pushes
were used.
