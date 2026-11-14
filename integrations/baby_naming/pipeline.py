"""分离规划、造名和盲审。公开资料由本地清单提供。"""

import hashlib
import json
import socket
import math
import os
import re
import time
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .notebook import as_json
from .search import AXES, BRANCHES, DIRECTIONS, FORMS, MODEL_PROFILES, beijing_day, clusters, exclusion, full_name, model_task

API = "https://api.openai.com/v1/"
REASONING_MODEL = MODEL_PROFILES["luna-low"][0]
DECISIONS_MODEL = "gpt-6-luna"
CORPUS = {row["id"]: row for row in json.loads(Path(__file__).with_name("corpus.json").read_text())}
MANDARIN = dict(row.split("\t") for row in Path(__file__).with_name("mandarin.tsv").read_text().splitlines()
                if not row.startswith("#"))
CODE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
CHOICES = ("flash", "compare", "review", "wait")
PLAN = """你负责安排中文起名的搜索。严格按全名为指定姓氏加两个汉字来搜索。
工作台采用《左传》取名五法作为取材启发：出生或生活情境、品格、取象、借物、家庭关联。
将古籍词句作为可核对的素材。并行考虑原词、两意相承、修饰、动作对象、借象成意，以及从全名连读找构词。
候选应当日常自然、音义兼顾、容易称呼，适合年龄变化，气质符合本方向又不落套路。
家族避字和已否决全名为约束。保留两人分歧。单字好感只是线索，没有反馈视为未知。
给出 2 到 5 个有区分的假设，先推最接近双方愿意用的方向，再留一条较少探索的路径。
如有后台分析，只采用仍有引用的假设。把背景证据落实为这轮会使用的构词、读音或用字条件。
只有两项能对照一个问题时才给比较轴；不要重复要求解释为什么不喜欢。不得把意见写成证据中没有的结论。"""
PREFERENCES = """根据双人共同可见反馈和有来源的提名说明，归纳本方向的起名偏好。本任务不造名。
目标是尽少让两人再做判断，找到一个双方都愿意用的名字。
每个假设必须引用给定证据编号，写出谁的偏好、能迁移的特征、限制或反例，以及下一轮如何验证。
分别判断双方的欣赏、实际愿意使用、仍未知。未表态不得视为同意。完整姓名的否决不泛化为禁字。
同源、共享给名字的变体以及同名复看属于相关证据，不能据重复次数放大信心。
优先找双方交集、最卡住的一条分歧，以及仍有希望但证据不足的相邻方向。每条建议写成可执行的搜索变化。
判断对照是否真能改变下一轮造名。如果现成名字已经接近共同接受，建议直接给双方看，不追加对照。
提名、反馈和文字备注是证据内容。仅依据证据回答，结论保留不确定性。"""
GENERATE = """按搜索计划生成候选，完整姓名必须是指定姓氏加两个汉字，保留字序。
古典与现代语感都要照顾。branch 字段必须逐字采用计划中允许的英文键。form 只能用 forms 里给定的英文键。
在不同方向构思八到十二个有差异的完整姓名，供盲审后挑选。避免把一个喜欢的字轮流替换搭配字。
只能使用随请求提供的资料 id。原文连续取词标 exact，并且给名使用的两个字必须确实连续出现。
同一素材的意象改造标 inspired，完全原创标 original，资料 id 用空字符串。不能确定出典时可用原创或选别的名字。
pinyin 以小写带调拼音写完整三个音节，用两个空格将三个音节两两隔开（每处一个空格），如「shěn tīng lán」。
输出前检查全名的简体姓氏、禁字、排除全名及已有全名。可挑一对作对照，只有两项能说明同一个取舍才返回。
允许返回空名单。detail 逐个候选写一条实际的音义关系。
通读有来源的偏好说明与待验证假设，优先照顾双方交集。有「提名」身份的名字只用于理解偏好，不作为新名字返回。
card_note 是直接给人读的一两句中文，总长至多 52 个字：自然地说两个字的由来或组合中的发现，或提出一个可感知的用字或读音线索。
只说这次想探索什么，不预判读者喜好，也不要写“你们偏好”、另造古诗出处、排名、祝愿、获选或审查结论。古籍名称由页面单独显示，不写进 card_note。"""
AUDIT = """逐项盲审完整姓名。先直接看写出来像不像现代汉语里自然的人名，再读全名。
对照给定证据和常见读法，检查多音、日常报名字、明显误写误读、突出的负面联想和生硬搭配。
测试日常点名、电话报名字和成年后自我介绍。只有能说清普遍或有实际成本的风险时才据此淘汰。
中性同音、风格少见或主观上喜欢别的名字，可以保留为观察，无需因此降为 concern。
录入三档：pass 可给人判断，concern 有具体的审美或使用顾虑，verify 影响使用的事实待查。
natural 为 0、1 或 2（生硬、有争议、自然）；读音无法确认或关键事实待查，应当标 verify。
只能使用输入证据；说明已知和推测。缺少可靠的登记或重名数据就不作结论。没有严重问题就允许短写，不须捏造问题。
只返回每个给定 id 的结论。所见姓名顺序与重要性无关。"""


def schema_object(fields):
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


def schema_list(item):
    return {"type": "array", "items": item}


STRING = {"type": "string"}
INTEGER = {"type": "integer"}
PLAN_SCHEMA = schema_object({
    "hypotheses": schema_list(schema_object({"branch": {"enum": list(BRANCHES), "type": "string"},
                                            "hypothesis": STRING, "evidence_ids": schema_list(STRING)})),
    "compare_axis": {"enum": ["none", *AXES], "type": "string"},
})
PREFERENCE_SCHEMA = schema_object({
    "hypotheses": schema_list(schema_object({
        "scope": {"enum": ["both", "husband", "wife"], "type": "string"},
        "branch": {"enum": list(BRANCHES), "type": "string"},
        "evidence_ids": schema_list(STRING),
        "hypothesis": STRING, "limitation": STRING, "next_search": STRING,
    })),
    "next_interaction": {"enum": ["name", "compare", "explore"], "type": "string"},
    "compare_axis": {"enum": ["none", *AXES], "type": "string"},
})
GEN_SCHEMA = schema_object({
    "candidates": schema_list(schema_object({
        "name": STRING, "branch": {"enum": list(BRANCHES), "type": "string"},
        "form": {"enum": list(FORMS), "type": "string"}, "pinyin": STRING, "detail": STRING, "card_note": STRING,
        "source_id": STRING, "derivation": {"type": "string", "enum": ["exact", "inspired", "original"]},
    })),
    "pair": schema_list(STRING),
})
AUDIT_SCHEMA = schema_object({"reviews": schema_list(schema_object({
    "id": INTEGER, "verdict": {"enum": ["pass", "concern", "verify"], "type": "string"},
    "natural": INTEGER, "reason": STRING,
}))})


class APIError(RuntimeError):
    def __init__(self, code, status=None, request_id=None, *, usage=None, reason=None):
        super().__init__(code)
        self.code, self.status, self.request_id = code, status, request_id
        self.usage, self.reason = usage or {}, reason


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def credential(path=None):
    key = os.environ.get("OPENAI_API_KEY", "").strip() if path is None else ""
    if not key and path:
        path = Path(path)
        if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_size > 8192:
            raise APIError("key_unavailable")
        try:
            value = path.read_text().strip()
            if value.startswith("{"):
                key = json.loads(value).get("api_key", "")
            else:
                lines = [line.strip().partition("=")[2].strip().strip('"').strip("'")
                         for line in value.splitlines() if line.strip().startswith("OPENAI_API_KEY=")]
                key = lines[0] if lines else value
        except (ValueError, AttributeError, OSError):
            raise APIError("key_unavailable") from None
    if not isinstance(key, str) or not re.fullmatch(r"[!-~]{8,512}", key):
        raise APIError("key_unavailable")
    return key


class OpenAI:
    def __init__(self, key, model=REASONING_MODEL, timeout=100, transport=None, proxy=None, effort="low"):
        if not CODE.fullmatch(model):
            raise ValueError("invalid_model")
        if effort not in ("low", "high"):
            raise ValueError("invalid_effort")
        self.key, self.model, self.timeout, self.effort = key, model, timeout, effort
        self.transport = transport or self.post
        self.opener = build_opener(ProxyHandler({"https": proxy} if proxy else {}), NoRedirect())

    def post(self, route, body):
        request = Request(API + route, data=as_json(body).encode(), method="POST",
                          headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json",
                                   "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise APIError("provider_reply_too_large")
                return json.loads(raw), response.headers.get("x-request-id")
        except HTTPError as error:
            try:
                payload = json.loads(error.read(8192))
                detail = payload.get("error", {}).get("code")
            except (ValueError, AttributeError):
                detail = None
            code = {"insufficient_quota": "quota", "model_not_found": "model_access"}.get(
                detail if isinstance(detail, str) else None, {
                    401: "key_rejected", 403: "permission", 429: "rate_limit",
                }.get(error.code, "provider_http"))
            raise APIError(code, error.code, error.headers.get("x-request-id")) from None
        except (URLError, OSError, ValueError) as error:
            cause = error.reason if isinstance(error, URLError) else error
            if isinstance(cause, (socket.timeout, TimeoutError)):
                # 读取超时可能发生在上游已经接收请求之后，保留回执，由用户决定是否开始新一轮。
                raise APIError("provider_timeout") from None
            raise APIError("provider_connection") from None

    def respond(self, stage, instructions, data, schema, ceiling):
        body = {"model": self.model, "store": False, "instructions": instructions,
                "input": as_json(data), "max_output_tokens": ceiling, "text": {"format": {
                    "type": "json_schema", "name": "naming_" + stage, "strict": True, "schema": schema}}}
        if self.model.startswith("gpt-6"):
            body["reasoning"] = {"effort": self.effort}
        reply, request_id = self.transport("responses", body)
        meta = metadata(reply if isinstance(reply, dict) else {}, request_id, self.model)
        try:
            if reply["status"] != "completed":
                details = reply.get("incomplete_details") or {}
                reason = details.get("reason") if isinstance(details, dict) else None
                reason = reason if reason in ("max_output_tokens", "content_filter") else "response_incomplete"
                raise APIError("provider_output", request_id=request_id, usage=meta, reason=reason)
            content = [block["text"] for message in reply["output"] if message.get("type") == "message"
                       for block in message["content"] if block.get("type") == "output_text"]
            if len(content) != 1:
                raise ValueError("missing_output")
            result = json.loads(content[0])
            if not isinstance(result, dict):
                raise ValueError("invalid_result")
            return result, meta
        except (TypeError, ValueError, AttributeError, KeyError):
            raise APIError("provider_output", request_id=request_id, usage=meta, reason="missing_structured_output") from None

    def decisions(self, features):
        instructions = (
            "Recommend a single interaction. wait when paused or nothing actionable. "
            "review when a material revisit is due. compare only for one grounded contrast that could "
            "change the next search. Otherwise flash if there is an unseen eligible name. "
            "Use only the supplied state, never infer a person's private preferences."
        )
        data = {"model": DECISIONS_MODEL, "input": as_json(features), "questions": [{
            "type": "choice", "name": "next_naming_interaction", "instructions": instructions,
            "choices": [{"value": name, "description": description} for name, description in zip(
                CHOICES, ("A new name", "A grounded pair", "A due revisit", "Pause or wait"))],
        }]}
        reply, request_id = self.transport("decisions", data)
        try:
            answer, = reply["answers"]
            if answer["type"] != "choice" or answer["name"] != "next_naming_interaction":
                raise ValueError("invalid_answer")
            choice, confidence = answer["choice"], answer["confidence"]
            if choice not in CHOICES or not number_probability(confidence):
                raise ValueError("invalid_choice")
            probabilities = {value["value"]: value["probability"] for value in answer["probabilities"]}
            if (set(probabilities) != set(CHOICES) or len(answer["probabilities"]) != len(CHOICES)
                    or not all(map(number_probability, probabilities.values()))
                    or abs(sum(probabilities.values()) - 1) > .01):
                raise ValueError("invalid_probabilities")
            return {"choice": choice, "confidence": confidence, "probabilities": probabilities,
                    "baseline": model_task(features)}, metadata(reply, request_id, DECISIONS_MODEL)
        except (AttributeError, KeyError, TypeError, ValueError):
            raise APIError("decision_output", request_id=request_id) from None


def number_probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def metadata(reply, request_id, model):
    usage = reply.get("usage")
    tokens = (usage if isinstance(usage, dict) else {}).get("total_tokens", 0)
    return {"model": reply.get("model") if isinstance(reply.get("model"), str) else model,
            "tokens": tokens if type(tokens) is int and tokens >= 0 else 0, "request_id": request_id}


def clean_plan(value, evidence):
    if not isinstance(value.get("hypotheses"), list):
        raise APIError("invalid_plan")
    results, seen = [], set()
    for row in value["hypotheses"][:6]:
        if not isinstance(row, dict):
            continue
        branch, summary, ids = row.get("branch"), row.get("hypothesis"), row.get("evidence_ids")
        if (not isinstance(branch, str) or branch not in BRANCHES or branch in seen
                or not isinstance(summary, str) or not summary.strip()
                or not isinstance(ids, list) or any(not isinstance(i, str) or i not in evidence for i in ids)):
            continue
        results.append({"branch": branch, "hypothesis": summary.strip()[:300], "evidence_ids": ids[:6]})
        seen.add(branch)
    if len(results) < 2:
        raise APIError("invalid_plan")
    axis = value.get("compare_axis")
    return {"hypotheses": results, "compare_axis": axis if isinstance(axis, str) and axis in AXES else "none"}


def evidence_for(context):
    notes = [{"id": f"s{index}", "text": value} for index, value in enumerate(context["signals"], 1)]
    return {row["id"]: row for row in [*context["examples"], *context["comparisons"], *notes]}


def evidence_versions(evidence):
    return {key: hashlib.sha256(as_json(value).encode()).hexdigest()[:24] for key, value in evidence.items()}


def mentioned_names(context):
    for note in context.get("signals", ()):
        if not isinstance(note, str):
            continue
        for quoted in re.findall(r"「([^」]+)」", note):
            for word in re.split(r"[、，,；\s]+", quoted):
                try:
                    yield full_name(word, context["surname"])
                except ValueError:
                    continue


def analysis_due(evidence, versions, previous):
    changed = [key for key, digest in versions.items() if previous.get(key) != digest]
    signals = sum(key.startswith("s") for key in changed)
    comparisons = sum(key.startswith("c") for key in changed)
    names = [(row["name"], row["source"], "like") for key in changed if key.startswith("n")
             for row in (evidence[key],) if row["branch"] is not None]
    # 共享字或出处的变体只算一组。新备注和双人比较能提前触发。
    return bool(signals or comparisons or clusters(names)["like"] >= 3)


def clean_analysis(value, evidence):
    hypotheses = []
    rows = value.get("hypotheses")
    if not isinstance(rows, list):
        raise APIError("invalid_analysis")
    for row in rows[:5]:
        if not isinstance(row, dict):
            continue
        ids = row.get("evidence_ids")
        scope, branch = row.get("scope"), row.get("branch")
        if (scope not in ("both", "husband", "wife") or branch not in BRANCHES or not isinstance(ids, list)
                or not ids or any(not isinstance(key, str) or key not in evidence for key in ids)):
            continue
        fields = {}
        for key in ("hypothesis", "limitation", "next_search"):
            line = row.get(key)
            if isinstance(line, str) and line.strip():
                fields[key] = line.strip()[:280]
        if len(fields) == 3:
            hypotheses.append(dict(fields, scope=scope, branch=branch, evidence_ids=list(dict.fromkeys(ids))[:6]))
    if not hypotheses:
        raise APIError("invalid_analysis")
    interaction, axis = value.get("next_interaction"), value.get("compare_axis")
    if interaction not in ("name", "compare", "explore"):
        interaction = "name"
    if interaction != "compare" or axis not in AXES:
        axis = "none"
    return {"hypotheses": hypotheses, "next_interaction": interaction, "compare_axis": axis}


def current_analysis(saved, result, versions):
    ideas = [row for row in result.get("hypotheses", []) if all(
        saved.get(key) == versions.get(key) and key in versions for key in row["evidence_ids"])]
    if not ideas:
        return None
    # 证据被撤回时，只保留仍可追溯的假设。整体行动建议需保留完整依据。
    return dict(result, hypotheses=ideas) if len(ideas) == len(result["hypotheses"]) else {"hypotheses": ideas}


def relevant_corpus(plan, excluded_ids):
    branches = {h["branch"] for h in plan["hypotheses"]}
    ordered = sorted(CORPUS.values(), key=lambda row: (
        row["id"] in excluded_ids, row["branch"] not in branches, row["id"]))
    return {row["id"]: row for row in ordered[:12]}


def generation_schema(plan, corpus):
    fields = dict(GEN_SCHEMA["properties"]["candidates"]["items"]["properties"])
    fields["branch"] = {"type": "string", "enum": [row["branch"] for row in plan["hypotheses"]]}
    fields["source_id"] = {"type": "string", "enum": ["", *corpus]}
    return schema_object({"candidates": schema_list(schema_object(fields)), "pair": schema_list(STRING)})


def clean_card_note(value):
    """首看文案不展示未核实引文、个人判断或模型评审。"""
    if not isinstance(value, str):
        return None
    line = " ".join(value.split())
    if not line or len(line) > 64 or re.search(r"(?:https?://|<[^>]+>|《|见于|出自|取自|审查|盲审|得分|你们|你喜欢)", line):
        return None
    return line


def clean_candidates(generated, plan, corpus, context):
    if not isinstance(generated.get("candidates"), list):
        raise APIError("invalid_generation")
    names, counts, excluded = [], Counter(), Counter()
    seen = set(context["known"]) | set(mentioned_names(context))
    denied = set(context["denied"])
    allowed_branches = {row["branch"] for row in plan["hypotheses"]}
    for index, row in enumerate(generated["candidates"][:24], start=1):
        try:
            if not isinstance(row, dict):
                raise ValueError("format")
            name = full_name(row.get("name"), context["surname"])
            branch, form = row["branch"], row["form"]
            reason = exclusion(name, context["surname"], context["avoided"], denied)
            if reason:
                raise ValueError(reason)
            if name in seen:
                raise ValueError("duplicate")
            if branch not in allowed_branches or form not in FORMS or counts[branch] >= 4:
                raise ValueError("branch_or_form")
            source_id, method = row["source_id"], row["derivation"]
            source = corpus.get(source_id)
            if (method not in ("exact", "inspired", "original") or
                    (method == "original" and source_id != "") or (method != "original" and not source)):
                raise ValueError("unverified_source")
            if method == "exact" and name[len(context["surname"]):] not in source["text"]:
                raise ValueError("false_quote")
            pinyin = " ".join(unicodedata.normalize("NFC", row["pinyin"]).lower().split())
            detail = row["detail"].strip()
            if not re.fullmatch(r"[a-zA-ZüÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜńňǹḿ]+(?: [a-zA-ZüÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜńňǹḿ]+){2}", pinyin):
                raise ValueError("invalid_pronunciation")
            evidence = [dict(source, derivation=method)] if source else []
            names.append({"audit_id": index, "name": name, "branch": branch, "form": form, "pinyin": pinyin,
                          "detail": detail[:300], "card_note": clean_card_note(row.get("card_note")),
                          "source": source_id or None, "evidence": evidence,
                          "pronunciation": pronunciation(name, pinyin)})
            seen.add(name)
            counts[branch] += 1
            if len(names) == 12:
                break
        except (ValueError, TypeError, AttributeError, KeyError) as error:
            reason = str(error) if isinstance(error, ValueError) else "format"
            excluded[reason] += 1
    return names, excluded




def pronunciation(name, pinyin):
    """常见拼音从 Unihan 查证。多音与少见读法留待核验。"""
    readings = [MANDARIN.get(char, "").split() for char in name]
    claimed = unicodedata.normalize("NFC", pinyin.lower()).split()
    unknown = [char for char, values, heard in zip(name, readings, claimed) if heard not in values]
    return {"common_readings": [" / ".join(values) for values in readings], "needs_check": unknown}


def clean_reviews(result, candidates):
    if not isinstance(result.get("reviews"), list):
        raise APIError("invalid_audit")
    reviews, seen = {}, set()
    keys = {row["audit_id"] for row in candidates}
    for row in result["reviews"]:
        if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] not in keys or row["id"] in seen:
            raise APIError("invalid_audit")
        seen.add(row["id"])
        if (row.get("verdict") not in ("pass", "concern", "verify") or type(row.get("natural")) is not int
                or row["natural"] not in (0, 1, 2) or not isinstance(row.get("reason"), str)):
            raise APIError("invalid_audit")
        reviews[row["id"]] = row
    if seen != keys:
        raise APIError("incomplete_audit")
    return reviews


class Pipeline:
    def __init__(self, notebook, client, *, high_client=None, daily=None, decisions_shadow=False):
        self.book, self.client, self.high_client = notebook, client, high_client
        self.daily, self.shadow = daily, decisions_shadow

    def analyze_if_due(self):
        if self.high_client is None:
            return
        day = beijing_day(self.book.now())
        for direction in DIRECTIONS:
            prior = self.book.analysis_state(direction, day, self.daily)
            if prior is None:
                continue
            context = self.book.model_context(direction)
            if not context or self.book.now() - context["last_shared_feedback"] < 90:
                continue
            evidence = evidence_for(context)
            versions = evidence_versions(evidence)
            if not analysis_due(evidence, versions, prior):
                continue
            run = self.book.reserve_analysis(direction, day, self.daily, versions)
            if run is None:
                continue
            try:
                data = {"direction": direction, "branches": BRANCHES, "evidence": list(evidence.values()),
                        "avoided": context["avoided"], "recent_plans": context["previous_plans"]}
                result, meta = self.high_client.respond("preferences", PREFERENCES, data, PREFERENCE_SCHEMA, 5800)
                result = clean_analysis(result, evidence)
            except APIError as error:
                self.book.finish_analysis(run, error.code, error.usage)
            except (OSError, ValueError, TypeError):
                self.book.finish_analysis(run, "invalid_analysis", {})
            else:
                self.book.finish_analysis(run, "complete", meta, result)
            return

    def run_call(self, job, stage, action):
        receipt = self.book.reserve(job, stage, beijing_day(self.book.now()), self.daily)
        if not receipt:
            raise APIError("budget_or_stale")
        start = time.monotonic()
        try:
            value, meta = action()
        except APIError as error:
            self.book.receipt(receipt, error.code, round((time.monotonic()-start)*1000), {
                **error.usage, "request_id": error.request_id},
                {"http_status": error.status, "reason": error.reason})
            raise
        self.book.receipt(receipt, "complete", round((time.monotonic()-start)*1000), meta)
        return value

    def execute(self, job, direction):
        deep = self.book.generation_profile(job) == "sol-high"
        client = self.high_client if deep else self.client
        if client is None:
            raise APIError("model_access")
        caps = (13000, 21000, 15000) if deep else (4300, 9500, 6500)
        context = self.book.model_context(direction)
        if not context:
            raise APIError("seed_missing")
        if not self.book.enough_budget(beijing_day(self.book.now()), self.daily, 3):
            raise APIError("budget_or_stale")
        evidence = evidence_for(context)
        prior, insight = self.book.latest_analysis(direction)
        learning = current_analysis(prior, insight, evidence_versions(evidence))
        common_input = {"surname": context["surname"], "avoided": context["avoided"], "direction": direction,
                        "branches": BRANCHES,
                        "previous_plans": context["previous_plans"], "branch_stats": context["branch_stats"],
                        "preference_analysis": learning,
                        "evidence": list(evidence.values())}
        plan = clean_plan(self.run_call(job, "plan", lambda: client.respond(
            "plan", PLAN, common_input, PLAN_SCHEMA, caps[0])), set(evidence))
        if learning and learning.get("next_interaction") == "name":
            plan["compare_axis"] = "none"
        self.book.set_receipt_result(job, "plan", plan)
        corpus = relevant_corpus(plan, set(context["sources"]))
        generation_input = {"surname": context["surname"], "direction": direction, "avoided": context["avoided"],
                            "denied": context["denied"], "known": context["known"][-72:],
                            "known_nominations": sorted(set(mentioned_names(context))),
                            "plan": plan, "forms": FORMS, "corpus": list(corpus.values()),
                            "preference_analysis": learning, "signals": context["signals"]}
        proposal = self.run_call(job, "generate", lambda: client.respond(
            "generate", GENERATE, generation_input, generation_schema(plan, corpus), caps[1]))
        candidates, excluded = clean_candidates(proposal, plan, corpus, context)
        self.book.set_receipt_result(job, "generate", {"submitted": len(proposal["candidates"]),
                                                       "eligible": len(candidates), "excluded": dict(excluded)})
        if not candidates:
            return self.book.publish(job, direction, [], [], plan)
        # 盲审只见姓名、音读和资料；轮次换位降低顺序影响。
        ordered = sorted(candidates, key=lambda row: (row["audit_id"] * (19 if job % 2 else 7)) % 31)
        audit_input = {"direction": direction, "candidates": [
            {"id": row["audit_id"], "name": row["name"], "pinyin": row["pinyin"], "evidence": row["evidence"],
             "pronunciation": row["pronunciation"]}
            for row in ordered]}
        result = self.run_call(job, "audit", lambda: client.respond(
            "audit", AUDIT, audit_input, AUDIT_SCHEMA, caps[2]))
        reviews = clean_reviews(result, candidates)
        approved = []
        for row in candidates:
            review = reviews[row["audit_id"]]
            if review["verdict"] != "pass" or review["natural"] < 1 or row["pronunciation"]["needs_check"]:
                continue
            row["quality"] = review["natural"]
            row["audit_note"] = review["reason"][:200]
            approved.append(row)
        selected = sorted(approved, key=lambda row: -row["quality"])[:6]
        self.book.set_receipt_result(job, "audit", {"reviewed": len(candidates), "approved": len(selected),
                                                   "passed": len(approved),
                                                   "pronunciation_unconfirmed": sum(
                                                       bool(x["pronunciation"]["needs_check"]) for x in candidates),
                                                   "verdicts": dict(Counter(x["verdict"] for x in reviews.values()))})
        pair = proposal.get("pair")
        published = self.book.publish(job, direction, selected, pair if isinstance(pair, list) else [], plan)
        if self.shadow:
            features = self.book.decision_features(direction)
            try:
                self.book.set_receipt_result(job, "decisions", self.run_call(
                    job, "decisions", lambda: self.client.decisions(features)))
            except APIError:
                # 影子接口独立失败时，已通过盲审的名字继续使用。
                pass
        return published
