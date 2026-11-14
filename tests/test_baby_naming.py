import json
import threading

import pytest

from integrations.baby_naming.notebook import Notebook, Rejected
from integrations.baby_naming.pipeline import APIError, OpenAI, Pipeline, current_analysis, evidence_for, evidence_versions
from integrations.baby_naming.search import estimate


@pytest.fixture
def book(tmp_path):
    clock = [1000.0]
    store = Notebook(tmp_path / "private", now=lambda: clock[0], review_delay=86400)
    store.clock = clock
    store.import_history({"surname": "沈", "avoided": ["山"], "history": [
        {"name": "沈初音", "direction": "girl", "ratings": {"husband": "like", "wife": "dislike"}},
        {"name": "沈雨音", "direction": "girl", "ratings": {"husband": "dislike", "wife": "dislike"}},
    ]})
    return store


def add(book, *names, direction="girl", branch="scene"):
    with book.transaction() as db:
        return [book.insert_name(db, direction, {"name": name, "branch": branch, "form": "image",
                                                "pinyin": "shěn xíng yuǎn"}) for name in names]


def respond(book, role, verdict, direction="girl"):
    task = book.assign(role, direction)["task"]
    return book.answer(role, direction, task["id"], verdict)


def test_async_devices_independently_resume_and_reveal_only_after_both_vote(book):
    first, second = add(book, "沈星阑", "沈雪初")
    first_task = book.assign("husband", "girl")["task"]
    other_task = book.assign("wife", "girl")["task"]
    assert first_task["names"][0]["id"] == other_task["names"][0]["id"] == first
    book.answer("husband", "girl", first_task["id"], "dislike")
    book.answer("husband", "girl", first_task["id"], "dislike")
    assert book.assign("husband", "girl")["task"]["names"][0]["id"] == second
    assert book.state("wife", "girl")["task"] == other_task
    assert not [row for row in book.model_context("girl")["examples"] if row["name"] == "沈星阑"]
    restarted = Notebook(book.root, now=book.now)
    assert restarted.assign("wife", "girl")["task"] == other_task
    with pytest.raises(Rejected, match="task_not_current"):
        restarted.answer("wife", "girl", first_task["id"], "like")
    saved = restarted.answer("wife", "girl", other_task["id"], "like")
    assert saved["common"] == []
    assert saved["personal"][0]["partner_rating"] == "dislike"
    assert restarted.model_context("girl")["denied"] == ["沈初音", "沈雨音"]
    assert restarted.state("husband", "boy")["personal"] == []


def test_answer_returns_confirmed_next_and_retry_reuses_it(book):
    add(book, "沈星阑", "沈雪初", "沈知临")
    first = book.assign("husband", "girl")
    other = book.assign("wife", "girl")
    answer = book.answer("husband", "girl", first["task"]["id"], "later", advance=True)
    assert first["preview"] == answer["task"]["names"][0]
    assert book.answer("husband", "girl", first["task"]["id"], "later", advance=True) == answer
    assert book.state("wife", "girl")["task"] == other["task"]
    assert book.undo("husband", "girl")["task"]["names"] == first["task"]["names"]


def test_next_group_can_be_buffered_before_the_current_group_runs_out(book):
    ids = add(book, "沈知春", "沈春水", "沈日禾", "沈禾风", "沈风言", "沈言清", "沈清词", "沈词月")
    current = book.assign("husband", "girl")
    for _ in range(5):
        current = book.answer("husband", "girl", current["task"]["id"], "later", advance=True)
    assert current["task"]["names"][0]["id"] in ids[:6]
    assert current["preview"]["id"] in ids[6:]
    following = book.answer("husband", "girl", current["task"]["id"], "like", advance=True,
                            prepared=current["preview"]["id"])
    assert following["task"]["names"][0] == current["preview"]
    assert following["preview"]["id"] != current["preview"]["id"]


def test_refill_begins_with_cards_left_and_can_recover_from_provider_failure(book):
    refill = {"daily": 6, "auto_refill": True}
    add(book, "沈知春", "沈星阑")
    opened = book.assign("husband", "girl", **refill)
    first, direction = book.claim()
    assert direction == "girl" and opened["model"]["state"] == "queued"
    assert opened["available_names"] == 2
    assert book.assign("wife", "girl", **refill)["model"]["state"] == "running"
    assert book.claim() is None

    first_task = opened["task"]["id"]
    remaining = book.answer("husband", "girl", first_task, "later", advance=True, **refill)
    last = book.answer("husband", "girl", remaining["task"]["id"], "dislike", advance=True, **refill)
    assert last["task"] is None and last["model"]["state"] == "running"
    book.finish_job(first, "failed", "provider_connection")
    assert book.state("husband", "girl", **refill)["model"]["state"] == "failed"
    assert book.claim() is None
    second = book.request_generation("husband", "girl", daily=6)
    assert second != first and book.claim() == (second, direction)
    book.clock[0] += 1
    add(book, "沈知云")
    book.finish_job(second, "complete", "ready")
    assert book.state("husband", "girl", **refill)["available_names"] == 1
    assert book.assign("husband", "girl", **refill)["task"]["names"][0]["name"] == "沈知云"
    assert book.claim() is None


def test_empty_deck_reports_budget_and_automatically_resumes_next_day(book):
    refill = {"daily": 3, "auto_refill": True}
    add(book, "沈知春")
    opened = book.assign("husband", "girl", **refill)
    job, direction = book.claim()
    for stage in ("plan", "generate", "audit"):
        assert book.reserve(job, stage, "1970-01-01", 3)
    book.finish_job(job, "complete", "ready")
    book.clock[0] += 1
    exhausted = book.answer("husband", direction, opened["task"]["id"], "dislike", advance=True, **refill)
    assert exhausted["task"] is None and exhausted["refill_wait"] == "budget"
    assert book.claim() is None
    book.clock[0] += 86400
    reopening = book.state("husband", direction, **refill)
    assert reopening["refill_wait"] is None and reopening["model"]["state"] == "queued"


def test_preference_analysis_uses_shared_evidence_without_locking_a_card_save(book):
    add(book, "沈星阑", "沈知临", "沈雪初", "沈从容")
    book.clock[0] += 100
    one_sided = None
    for role in ("husband", "wife"):
        for opinion in ("like", "later", "dislike"):
            respond(book, role, opinion)
        if role == "husband":
            one_sided = {key for key in evidence_for(book.model_context("girl")) if key.startswith("n")}
    assert len(one_sided) == 2
    book.clock[0] += 91
    called = []

    class Provider:
        def respond(self, stage, instructions, data, schema, ceiling):
            assert stage == "preferences"
            example = next(row for row in data["evidence"] if row.get("branch") and row["ratings"]["husband"] == "dislike")
            next_task = book.assign("husband", "girl")["task"]
            book.answer("husband", "girl", next_task["id"], "later", advance=True)
            called.append(example["id"])
            return {"hypotheses": [{"scope": "both", "branch": "scene", "hypothesis": "keep distinction",
                                     "limitation": "only one example", "next_search": "try shorter vowels",
                                     "evidence_ids": [example["id"]]}],
                    "next_interaction": "name", "compare_axis": "none"}, {"model": "fixture", "tokens": 9}

    pipeline = Pipeline(book, None, high_client=Provider(), daily=4)
    pipeline.analyze_if_due()
    prior, result = book.latest_analysis("girl")
    before = evidence_versions(evidence_for(book.model_context("girl")))
    assert current_analysis(prior, result, before)["next_interaction"] == "name"
    pipeline.analyze_if_due()
    assert len(called) == 1
    assert book.enough_budget("1970-01-01", 4, 3)
    assert not book.enough_budget("1970-01-01", 4, 4)
    book.undo("wife", "girl")
    after = evidence_versions(evidence_for(book.model_context("girl")))
    assert current_analysis(prior, result, after) is None


def test_confirm_one_name_stops_search_and_revokes_stale_cards(book):
    first, _ = add(book, "沈星阑", "沈雪初")
    respond(book, "husband", "like")
    respond(book, "wife", "like")
    old = book.assign("wife", "girl")["task"]
    pending = book.name_action("husband", "girl", first, "ready")
    assert not pending["paused"]
    assert book.state("wife", "girl")["common"][0]["my_ready"] is False
    confirmed = book.name_action("wife", "girl", first, "ready")
    assert confirmed["paused"] == first
    assert confirmed["common"][0]["confirmed"]
    assert book.assign("husband", "girl")["task"] is None
    with pytest.raises(Rejected, match="paused"):
        book.request_generation("husband", "girl")
    with pytest.raises(Rejected, match="task_not_current"):
        book.answer("wife", "girl", old["id"], "later")
    book.resume("wife", "girl")
    book.name_action("wife", "girl", first, "ready")
    assert not book.state("wife", "girl")["paused"]


def test_revisit_only_once_and_conflict_does_not_leak_until_second_revisit(book):
    first, = add(book, "沈星阑")
    respond(book, "husband", "like")
    respond(book, "wife", "like")
    assert book.assign("husband", "girl")["task"] is None
    book.clock[0] += 86400
    assert book.assign("husband", "girl")["task"]["kind"] == "review"
    respond(book, "husband", "dislike")
    assert book.state("wife", "girl")["common"][0]["partner_rating"] == "like"
    respond(book, "wife", "like")
    assert not book.state("wife", "girl")["common"]
    assert book.assign("husband", "girl")["task"] is None
    assert book.state("husband", "girl")["personal"][0]["id"] == first
    assert book.state("husband", "girl")["personal"][0]["my_rating"] == "later"


def test_wait_then_like_can_join_shared_candidates_after_review(book):
    first, = add(book, "沈星阑")
    respond(book, "husband", "later")
    respond(book, "wife", "like")
    book.clock[0] += 86400
    respond(book, "husband", "like")
    assert not book.state("wife", "girl")["common"]
    respond(book, "wife", "like")
    assert book.state("wife", "girl")["common"][0]["id"] == first
    assert book.state("husband", "girl")["common"][0]["stable"]


def test_undo_and_explicit_exclusion_preserve_the_other_open_task(book):
    first, _ = add(book, "沈星阑", "沈雪初")
    first_task = book.assign("husband", "girl")["task"]
    other = book.assign("wife", "girl")["task"]
    respond(book, "husband", "dislike")
    reopened = book.undo("husband", "girl")["task"]
    assert reopened["id"] != first_task["id"]
    with pytest.raises(Rejected):
        book.answer("husband", "girl", first_task["id"], "dislike")
    respond(book, "husband", "dislike")
    book.name_action("husband", "girl", first, "veto")
    assert book.assign("wife", "girl")["task"] == other
    assert "沈星阑" not in book.model_context("girl")["denied"]
    respond(book, "wife", "like")
    assert "沈星阑" in book.model_context("girl")["denied"]


def test_branch_pruning_requires_independent_feedback_and_recovers():
    raw = [("沈" + chr(0x4e00 + i) + chr(0x4f00 + i), "source_" + str(i), "dislike")
           for i in range(6)]
    assert not estimate(raw[:-1])["reduced"]
    assert estimate(raw)["reduced"]
    assert not estimate(raw + [("沈星阑", "new", "like")])["reduced"]
    repeated = [("沈山" + chr(0x4e00 + i), None, "dislike") for i in range(7)]
    assert estimate(repeated)["negative"] == 1
    assert not estimate(repeated)["reduced"]
    same_source = [(name, "same", value) for name, _, value in raw]
    assert estimate(same_source)["negative"] == 1


def test_two_workers_get_one_job_and_receipts_enforce_persistent_budget(book):
    result = []
    threads = [threading.Thread(target=lambda role=role: result.append(book.request_generation(role, "girl")))
               for role in ("husband", "wife")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(result) == 2 and len(set(result)) == 1
    job, direction = book.claim()
    assert direction == "girl"
    assert book.claim() is None
    assert book.reserve(job, "plan", "2026-11-14", 1)
    assert not book.reserve(job, "generate", "2026-11-14", 1)
    book.interrupt_running()
    assert book.proof(job)["job"]["outcome"] == "interrupted"


class MockProvider:
    def __init__(self, *, incomplete=False):
        self.calls = []
        self.incomplete = incomplete

    @staticmethod
    def name(name, branch="scene", source="", method="original"):
        return {"name": name, "branch": branch, "form": "image", "source_id": source,
                "pinyin": {"沈音晴": "shěn yīn qíng", "沈晓夏": "shěn xiǎo xià"}.get(name, "shěn qīng lín"),
                "derivation": method, "detail": "生成时写的推介说明",
                "card_note": "可以留意两个字连读时的停顿。"}

    def __call__(self, route, body):
        self.calls.append((route, body))
        if route == "decisions":
            raise APIError("permission", 403)
        stage = body["text"]["format"]["name"]
        if stage == "naming_plan":
            obj = {"hypotheses": [
                {"branch": "scene", "hypothesis": "清亮的景象", "evidence_ids": []},
                {"branch": "sound", "hypothesis": "口语读音", "evidence_ids": []}], "compare_axis": "sound"}
        elif stage == "naming_generate":
            obj = {"candidates": [self.name("沈初音"), self.name("沈青山"), self.name("沈音晴"),
                                  self.name("沈清林", "sound"), self.name("沈晓夏", "sound"),
                                  self.name("沈月眠", "sound", "xietiao-wandeng", "exact")],
                   "pair": ["沈音晴", "沈清林"]}
        else:
            assert stage == "naming_audit"
            candidate = json.loads(body["input"])["candidates"]
            assert all(set(x) == {"id", "name", "pinyin", "evidence", "pronunciation"} for x in candidate)
            assert {x["name"] for x in candidate} == {"沈音晴", "沈清林", "沈晓夏"}
            obj = {"reviews": [{"id": x["id"], "verdict": "verify" if x["name"] == "沈晓夏" else "pass",
                                "natural": 2, "reason": "读音核对"} for x in candidate]}
            if self.incomplete:
                obj["reviews"].pop()
        return {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(obj)}]}], "usage": {"total_tokens": 20}}, "fake_req"


def test_switching_to_unlimited_resumes_a_depleted_deck_after_the_old_daily_limit(book):
    direction, day = "girl", "1970-01-01"
    add(book, "沈知春")
    for _ in range(8):
        old_job = book.request_generation("husband", direction, daily=24)
        assert book.claim() == (old_job, direction)
        for stage in ("plan", "generate", "audit"):
            receipt = book.reserve(old_job, stage, day, 24)
            book.receipt(receipt, "complete", 1, {"model": "fixture", "tokens": 10})
        book.finish_job(old_job, "complete", "ready")
    book.clock[0] += 1
    respond(book, "husband", "dislike", direction)

    assert book.state("husband", direction, auto_refill=True, daily=24)["refill_wait"] == "budget"
    assert book.assign("husband", direction)["model"]["state"] == "complete"
    assert book.claim() is None
    resumed = book.state("husband", direction, auto_refill=True)
    assert resumed["refill_wait"] is None and resumed["model"]["state"] == "queued"
    job, _ = book.claim()
    assert job != old_job
    assert book.assign("wife", direction, auto_refill=True)["model"]["state"] == "running"
    assert book.claim() is None
    provider = MockProvider()
    Pipeline(book, None, high_client=OpenAI("mock-key", transport=provider)).execute(job, direction)
    book.finish_job(job, "complete", "ready")
    assert [(row["stage"], row["status"]) for row in book.proof(job)["receipts"]] == [
        ("plan", "complete"), ("generate", "complete"), ("audit", "complete"),
    ]
    assert book.assign("husband", direction, auto_refill=True)["task"]["names"][0]["name"] in {"沈音晴", "沈清林"}
    with book.transaction(write=False) as db:
        assert book.used_budget(db, day) == 27


def test_generation_filters_exact_denials_and_audit_does_not_see_sales_pitch(book):
    provider = MockProvider()
    job = book.request_generation("husband", "girl", "luna-low")
    assert book.claim() == (job, "girl")
    Pipeline(book, OpenAI("mock-key", transport=provider), decisions_shadow=True).execute(job, "girl")
    assert [key for key, _ in provider.calls] == ["responses", "responses", "responses", "decisions"]
    assert all(body.get("store") is False for route, body in provider.calls if route == "responses")
    assert set(json.loads(provider.calls[-1][1]["input"])) == {"paused", "review_due", "useful_pair", "unseen"}
    outcome = book.proof(job)
    assert outcome["receipts"][1]["result"]["excluded"] == {
        "explicit_veto": 1, "avoided_character": 1, "false_quote": 1,
    }
    assert outcome["receipts"][2]["result"]["approved"] == 2
    assert outcome["receipts"][3]["status"] == "permission"
    card = book.assign("wife", "girl")["task"]["names"][0]
    assert card["name"] in {"沈音晴", "沈清林"}
    assert card["card_note"] == "可以留意两个字连读时的停顿。"
    assert "detail" not in card


def test_incomplete_blind_audit_never_publishes_unreviewed_names(book):
    job = book.request_generation("husband", "girl", "luna-low")
    assert book.claim() == (job, "girl")
    provider = MockProvider(incomplete=True)
    with pytest.raises(APIError, match="incomplete_audit"):
        Pipeline(book, OpenAI("mock-key", transport=provider)).execute(job, "girl")
    assert book.assign("wife", "girl")["task"] is None


def test_comparison_is_asynchronous_and_does_not_count_as_a_flash_vote(book):
    keys = add(book, "沈星阑", "沈雪初", "沈清林", "沈月眠", "沈雨晴")
    with book.transaction() as db:
        db.execute("INSERT INTO comparisons(direction,first,second,axis) VALUES('girl',?,?,'sound')",
                   (keys[0], keys[1]))
    for _ in range(3):
        respond(book, "husband", "later")
    compare = book.assign("husband", "girl")["task"]
    assert compare["kind"] == "compare"
    book.answer("husband", "girl", compare["id"], str(compare["names"][0]["id"]))
    assert book.model_context("girl")["comparisons"] == []
    for _ in range(3):
        respond(book, "wife", "later")
    compare = book.assign("wife", "girl")["task"]
    assert compare["kind"] == "compare"
    book.answer("wife", "girl", compare["id"], "both")
    assert len(book.model_context("girl")["comparisons"]) == 1
    assert book.assign("wife", "girl")["task"]["kind"] == "flash"


def test_active_batch_preserves_remaining_tasks_when_new_names_arrive(book):
    first, second = add(book, "沈星阑", "沈雪初")
    task = book.assign("wife", "girl")["task"]
    assert task["names"][0]["id"] == first
    with book.transaction() as db:
        book.insert_name(db, "girl", {"name": "沈云舒", "branch": "object", "form": "image", "quality": 2})
    book.answer("wife", "girl", task["id"], "dislike")
    assert book.assign("wife", "girl")["task"]["names"][0]["id"] == second


def test_portal_plan_changes_only_lan_route_and_keeps_existing_tiles(tmp_path):
    from integrations.baby_naming.portal_update import patches

    node = "function route(req,res,host){const url=new URL(req.url,'http://'+host);return url.pathname}"
    html = '<script>const APPS=[{"id":"home","name":"家","url":"/home","icon":"old","pending":false}];function urlFor(a){return a.url;}</script>'
    server, page = patches(node, html, tmp_path / "proof", ["http://friday.local"], 19146)
    assert server.count("if(babyNamingRoute(") == 1
    cards, _ = json.JSONDecoder().raw_decode(page.split("const APPS=")[1])
    assert cards[0]["icon"] == "old"
    assert cards[1]["url"] == "/apps/baby-naming/"
    assert cards[1]["pending"] is False
    assert "if(a.id==='babynaming')return a.url;" in page
    assert "o0x0o" not in server


def test_pronunciation_is_independently_checked():
    from integrations.baby_naming.pipeline import pronunciation

    assert pronunciation("沈音晴", "shěn yīn qíng")["needs_check"] == []
    assert pronunciation("沈音晴", "shěn yīn lín")["needs_check"] == ["晴"]


def test_high_profile_stays_with_queued_job_and_reaches_all_three_openai_requests(book):
    ordinary = MockProvider()
    deep = MockProvider()
    high = OpenAI("mock-key", "gpt-6.1-sol", effort="high", transport=deep)
    pipeline = Pipeline(book, OpenAI("mock-key", transport=ordinary), high_client=high)
    job = book.request_generation("wife", "girl", "sol-high")
    assert book.request_generation("husband", "girl", "luna-low") == job
    assert book.state("husband", "girl")["model"]["profile"] == "sol-high"
    assert Notebook(book.root).generation_profile(job) == "sol-high"
    assert book.claim() == (job, "girl")
    pipeline.execute(job, "girl")
    assert not ordinary.calls
    assert len(deep.calls) == 3
    assert all(route == "responses" and body["model"] == "gpt-6.1-sol"
               and body["reasoning"]["effort"] == "high" for route, body in deep.calls)
    assert [r["result"]["approved"] for r in book.proof(job)["receipts"] if r["stage"] == "audit"] == [2]
    with pytest.raises(Rejected, match="invalid_model_profile"):
        book.request_generation("husband", "boy", "arbitrary")


def test_settings_default_high_and_next_job_reads_each_saved_role_without_restart(book):
    assert book.state("wife", "girl")["model_profile"] == "sol-high"
    assert book.set_generation_profile("wife", "girl", "luna-low")["model_profile"] == "luna-low"
    assert book.state("husband", "girl")["model_profile"] == "sol-high"
    old = book.request_generation("wife", "girl")
    assert book.claim() == (old, "girl")
    book.set_generation_profile("wife", "boy", "sol-high")
    assert book.generation_profile(old) == "luna-low"
    new = book.request_generation("wife", "boy")
    assert book.generation_profile(new) == "sol-high"
    assert Notebook(book.root).state("wife", "girl")["model_profile"] == "sol-high"


def test_existing_card_shows_verified_source_without_revealing_audit_text(book):
    with book.transaction() as db:
        book.insert_name(db, "girl", {"name": "沈星阑", "branch": "scene", "form": "image",
            "detail": "这是旧审核内容，不用于首看。",
            "evidence": [{"title": "已核对的合成出处", "derivation": "inspired"}]})
    card = book.assign("husband", "girl")["task"]["names"][0]
    assert card["card_source"] == "借意自 已核对的合成出处"
    assert "card_note" not in card
    assert "detail" not in card
