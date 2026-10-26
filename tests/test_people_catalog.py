import sqlite3

import pytest

from homeplane.people.catalog import PeopleCatalog, PeopleError
from homeplane.people.contracts import Correct, Edit, Merge, Name, ReviewVoice, Split
from homeplane.people.fixture import MODEL, POLICY, demo_factory, inputs, partition_factory, sample, seed
from homeplane.people.recognition_bridge import SpeakerAgreement
from homeplane.recognition.contracts import VerificationPolicy


def catalog(path=":memory:"):
    return PeopleCatalog(sqlite3.connect(path), _cluster_factory=demo_factory())


def group(page, sid):
    return next(c for c in page.clusters if sid in c.sample_ids)


def test_persistence_label_survives_reorder_and_missing_observations(tmp_path):
    c = catalog(tmp_path / "people.db")
    p = seed(c)
    named = next(x for x in p.people if x.name)
    c.connection.close()
    c = catalog(tmp_path / "people.db")
    assert c.page().people[0].person_id == named.person_id
    p = c.reindex(tuple(reversed(inputs())), POLICY, expected_revision=p.revision)
    assert group(p, inputs()[0].sample_id).person_id == named.person_id
    assert next(x for x in p.people if x.person_id == named.person_id).name == "林同学"
    p = c.reindex((), POLICY, expected_revision=p.revision)
    assert p.people[0].name == "林同学" and p.people[0].sample_count == 0
    assert all(not x.active for x in p.samples)
    assert not p.undo_available  # reindex never lets undo erase fresh producer data


def test_split_survives_reindex_then_merge_and_undo():
    c = catalog()
    p = seed(c)
    old = group(p, inputs()[0].sample_id)
    moved = old.sample_ids[:2]
    p = c.split(Split(expected_revision=p.revision, cluster_id=old.cluster_id, sample_ids=moved))
    separate = group(p, moved[0])
    retained = group(p, next(x for x in old.sample_ids if x not in moved))
    assert separate.person_id != retained.person_id
    assert all(v.state == "stale" for v in p.voice_links)
    p = c.reindex(inputs(), POLICY, expected_revision=p.revision)
    assert group(p, moved[0]).person_id != group(p, retained.sample_ids[0]).person_id
    selected = [group(p, moved[0]), group(p, retained.sample_ids[0])]
    before = p.model_dump()
    p = c.merge(
        Merge(
            expected_revision=p.revision,
            cluster_ids=[x.cluster_id for x in selected],
            target_person_id=retained.person_id,
        )
    )
    assert group(p, moved[0]).person_id == retained.person_id
    p = c.undo(Edit(expected_revision=p.revision))
    assert p.revision > before["revision"]
    assert [x.model_dump() for x in p.clusters] == before["clusters"]


def test_conflicting_names_merge_needs_explicit_survivor_and_keeps_alias():
    c = catalog()
    p = seed(c)
    other = next(x for x in p.clusters if x.state == "candidate")
    p = c.name(Name(expected_revision=p.revision, cluster_id=other.cluster_id, name="周同学"))
    named = [x for x in p.clusters if x.state == "named"]
    with pytest.raises(PeopleError, match="merge_target_required"):
        c.merge(Merge(expected_revision=p.revision, cluster_ids=[x.cluster_id for x in named]))
    keep = named[0].person_id
    p = c.merge(
        Merge(expected_revision=p.revision, cluster_ids=[x.cluster_id for x in named], target_person_id=keep)
    )
    aliases = [x for x in p.people if x.merged_into]
    assert len(aliases) == 1 and aliases[0].name and aliases[0].merged_into == keep
    assert p.original_media_modified is False


def test_correction_exclude_restore_and_move():
    c = catalog()
    p = seed(c)
    named = next(x for x in p.clusters if x.state == "named")
    sid = next(x for x in p.clusters if x.state == "candidate").sample_ids[0]
    p = c.correct(
        Correct(expected_revision=p.revision, sample_id=sid, person_id=named.person_id, operation="move")
    )
    assert group(p, sid).person_id == named.person_id
    p = c.correct(Correct(expected_revision=p.revision, sample_id=sid, operation="exclude"))
    assert not any(sid in x.sample_ids for x in p.clusters)
    p = c.reindex(inputs(), POLICY, expected_revision=p.revision)
    assert next(x for x in p.samples if x.sample_id == sid).state == "excluded"
    p = c.correct(Correct(expected_revision=p.revision, sample_id=sid, operation="restore"))
    assert group(p, sid).person_id == named.person_id


def test_two_connections_revision_conflict_rolls_back(tmp_path):
    path = tmp_path / "people.db"
    first, second = catalog(path), catalog(path)
    p = first.reindex(inputs(), POLICY, expected_revision=0)
    candidate = next(x for x in p.clusters if x.state == "candidate")
    newer = first.name(Name(expected_revision=p.revision, cluster_id=candidate.cluster_id, name="A"))
    with pytest.raises(PeopleError, match="revision_conflict"):
        second.split(
            Split(
                expected_revision=p.revision,
                cluster_id=candidate.cluster_id,
                sample_ids=candidate.sample_ids[:1],
            )
        )
    assert second.page() == newer
    assert len(second.page().people) == 1


def test_invalid_edit_is_atomic_and_unknown_not_named():
    c = catalog()
    p = seed(c)
    before = p.model_dump()
    unknown = next(x for x in p.clusters if x.state == "unknown")
    with pytest.raises(PeopleError, match="sample_review_required"):
        c.name(Name(expected_revision=p.revision, cluster_id=unknown.cluster_id, name="访客"))
    with pytest.raises(PeopleError, match="split_subset_required"):
        c.split(
            Split(expected_revision=p.revision, cluster_id=unknown.cluster_id, sample_ids=unknown.sample_ids)
        )
    assert c.page().model_dump() == before


def test_no_vectors_templates_or_media_in_sqlite_public_or_repr():
    c = catalog()
    p = seed(c)
    c.connection.execute("CREATE TABLE family_media_items (id TEXT PRIMARY KEY, body TEXT)")
    c.connection.execute("INSERT INTO family_media_items VALUES ('sentinel','unchanged')")
    c.connection.commit()
    candidate = next(x for x in p.clusters if x.state == "candidate")
    c.name(Name(expected_revision=p.revision, cluster_id=candidate.cluster_id, name="另一人物"))
    stored = "\n".join(
        row[0]
        for table in ("people_state", "people_undo")
        for row in c.connection.execute(f"SELECT body FROM {table}")
    )
    public = p.model_dump_json()
    assert all(
        token not in stored + public for token in ('"values"', '"embedding"', '"template"', '"pixels"')
    )
    assert "values=" not in repr(inputs())
    assert c.connection.execute("SELECT body FROM family_media_items").fetchone()[0] == "unchanged"
    assert all(not x.action_authorized and not x.security_authentication for x in p.people)
    assert not p.embeddings_persisted and not p.action_authorized


def test_adapter_calls_mature_contract_with_normalized_vectors():
    observations = (sample(20, 0.0), sample(21, 0.1))
    calls = {}

    class Fake:
        def __init__(self, **kwargs):
            calls.update(kwargs)

        def fit_predict(self, vectors):
            assert all(abs(sum(x * x for x in vector) - 1) < 1e-12 for vector in vectors)
            return [0, 0]

    c = PeopleCatalog(sqlite3.connect(":memory:"), _cluster_factory=Fake)
    p = c.reindex(observations, POLICY, expected_revision=0)
    assert len(p.clusters) == 1
    assert calls == dict(
        n_clusters=None,
        metric="cosine",
        linkage="complete",
        distance_threshold=1 - POLICY.accept_similarity,
        compute_full_tree=True,
    )
    assert p.last_run.backend == "synthetic-contract-fake"


def test_dependency_missing_is_explicit(monkeypatch):
    import builtins

    original = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("sklearn"):
            raise ImportError()
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    c = PeopleCatalog(sqlite3.connect(":memory:"))
    with pytest.raises(PeopleError, match="clustering_dependency_unavailable"):
        c.reindex(inputs(), POLICY, expected_revision=0)
    assert c.page().revision == 0


def test_batch_model_quality_bounds_and_chain_output_rejected():
    c = catalog()
    with pytest.raises(PeopleError, match="batch_budget"):
        c.reindex(tuple(sample(i, 0.1) for i in range(257)), POLICY, expected_revision=0)
    uncalibrated = VerificationPolicy("face", MODEL, 0.9, 0.04)
    with pytest.raises(PeopleError, match="policy_unqualified"):
        c.reindex(inputs(), uncalibrated, expected_revision=0)
    observations = (sample(30, 0), sample(31, 0.4), sample(32, 0.8))
    factory = partition_factory(observations, [[v.sample_id for v in observations]])
    c = PeopleCatalog(sqlite3.connect(":memory:"), _cluster_factory=factory)
    p = c.reindex(observations, POLICY, expected_revision=0)
    assert all(x.state == "ambiguous" and len(x.sample_ids) == 1 for x in p.clusters)
    p = c.reindex((sample(40, 0, model="3" * 64),), POLICY, expected_revision=p.revision)
    assert p.clusters[0].state == "unknown"


def test_ambiguous_between_two_labels_never_nearest_assigned():
    observations = (sample(50, -0.2), sample(51, -0.21), sample(52, 0.2), sample(53, 0.21), sample(54, 0))
    factory = partition_factory(
        observations,
        [
            [v.sample_id for v in observations[:2]],
            [v.sample_id for v in observations[2:4]],
            [observations[4].sample_id],
        ],
    )
    c = PeopleCatalog(sqlite3.connect(":memory:"), _cluster_factory=factory)
    p = c.reindex(observations, POLICY, expected_revision=0)
    for sid, label in [(observations[0].sample_id, "甲"), (observations[2].sample_id, "乙")]:
        candidate = group(p, sid)
        p = c.name(Name(expected_revision=p.revision, cluster_id=candidate.cluster_id, name=label))
    p = c.reindex(tuple(reversed(observations)), POLICY, expected_revision=p.revision)
    middle = group(p, observations[4].sample_id)
    assert middle.state == "ambiguous" and middle.person_id is None


def agreement(sid, index=0, **kwargs):
    values = dict(
        face_sample_id=sid,
        voice_ref="voice_A",
        model_sha256="2" * 64,
        window_id=f"w{index}",
        event_id=f"e{index}",
        speaker_qualified=True,
        single_speaker=True,
        active_speaker_qualified=True,
        av_alignment_qualified=True,
        overlap_free=True,
        track_continuity_qualified=True,
        speaker_margin_qualified=True,
    )
    values.update(kwargs)
    return SpeakerAgreement(**values)


def test_voice_needs_independent_repeated_active_speaker_and_user_review():
    c = catalog()
    p = c.reindex(inputs(), POLICY, expected_revision=0)
    target = group(p, inputs()[0].sample_id)
    p = c.name(Name(expected_revision=p.revision, cluster_id=target.cluster_id, name="甲"))
    with pytest.raises(PeopleError, match="speaker_evidence_unqualified"):
        c.propose_voice(
            agreement(inputs()[0].sample_id, active_speaker_qualified=False), expected_revision=p.revision
        )
    for _ in range(3):
        p = c.propose_voice(agreement(inputs()[0].sample_id), expected_revision=p.revision)
    assert p.voice_links[0].independent_windows == 1 and p.voice_links[0].state == "stale"
    for i in (1, 2):
        p = c.propose_voice(agreement(inputs()[0].sample_id, i), expected_revision=p.revision)
    link = p.voice_links[0]
    assert link.state == "candidate"
    p = c.review_voice(ReviewVoice(expected_revision=p.revision, link_id=link.link_id, decision="confirm"))
    assert p.voice_links[0].state == "confirmed" and p.action_authorized is False
    p = c.review_voice(ReviewVoice(expected_revision=p.revision, link_id=link.link_id, decision="unlink"))
    with pytest.raises(PeopleError, match="decision_retained"):
        c.propose_voice(agreement(inputs()[0].sample_id, 3), expected_revision=p.revision)


def test_voice_competing_person_repeated_evidence_stays_stale_until_rejected():
    c = catalog()
    p = seed(c)
    first_link = p.voice_links[0]
    other = next(x for x in p.clusters if x.state == "candidate")
    p = c.name(Name(expected_revision=p.revision, cluster_id=other.cluster_id, name="乙"))
    for i in range(3):
        p = c.propose_voice(
            agreement(other.sample_ids[0], i + 5, voice_ref="synthetic_voice_A"), expected_revision=p.revision
        )
    assert all(x.state == "stale" for x in p.voice_links)
    for i in range(3):
        p = c.propose_voice(
            agreement(inputs()[0].sample_id, i + 10, voice_ref="synthetic_voice_A"),
            expected_revision=p.revision,
        )
    assert all(x.state == "stale" for x in p.voice_links)
    second = next(x for x in p.voice_links if x.link_id != first_link.link_id)
    p = c.review_voice(ReviewVoice(expected_revision=p.revision, link_id=second.link_id, decision="reject"))
    p = c.propose_voice(
        agreement(inputs()[0].sample_id, 20, voice_ref="synthetic_voice_A"), expected_revision=p.revision
    )
    assert next(x for x in p.voice_links if x.link_id == first_link.link_id).state == "candidate"


def test_snapshot_undo_bound_and_no_stale_revision_reuse():
    c = catalog()
    p = seed(c)
    target = next(x for x in p.clusters if x.state == "named")
    for i in range(35):
        p = c.name(Name(expected_revision=p.revision, cluster_id=target.cluster_id, name=f"人物{i}"))
    assert c.connection.execute("SELECT COUNT(*) FROM people_undo").fetchone()[0] == 32
    previous = p.revision
    p = c.undo(Edit(expected_revision=previous))
    assert p.revision == previous + 1
    with pytest.raises(PeopleError, match="revision_conflict"):
        c.undo(Edit(expected_revision=previous))
