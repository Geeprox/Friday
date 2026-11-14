"""保存在 NAS 的异步任务、原始反馈与生成请求。"""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .search import (
    AXES, DEFAULT_PROFILE, DIRECTIONS, MODEL_PROFILES, RATINGS, ROLES, beijing_day, combined_rating, exclusion, rank,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS names (
    id INTEGER PRIMARY KEY, direction TEXT NOT NULL, name TEXT NOT NULL,
    branch TEXT NOT NULL, form TEXT NOT NULL, source TEXT, pinyin TEXT,
    detail TEXT, card_note TEXT, quality INTEGER NOT NULL, evidence TEXT NOT NULL,
    visibility TEXT NOT NULL, origin TEXT NOT NULL, UNIQUE(direction, name)
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY, role TEXT NOT NULL, direction TEXT NOT NULL,
    kind TEXT NOT NULL, first INTEGER NOT NULL REFERENCES names(id), second INTEGER REFERENCES names(id),
    axis TEXT, state TEXT NOT NULL DEFAULT 'open', answer TEXT, created REAL NOT NULL, done REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_task ON tasks(role, direction) WHERE state='open';
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY, role TEXT NOT NULL, direction TEXT NOT NULL,
    names TEXT NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS batches_role ON batches(role,direction,id);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY, task_id INTEGER UNIQUE REFERENCES tasks(id), role TEXT NOT NULL,
    name_id INTEGER NOT NULL REFERENCES names(id), stage TEXT NOT NULL, value TEXT NOT NULL,
    created REAL NOT NULL, retracted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS feedback_name ON feedback(name_id,role,stage,retracted);
CREATE TABLE IF NOT EXISTS comparisons (
    id INTEGER PRIMARY KEY, direction TEXT NOT NULL, first INTEGER NOT NULL REFERENCES names(id),
    second INTEGER NOT NULL REFERENCES names(id), axis TEXT NOT NULL, UNIQUE(direction,first,second,axis)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY, direction TEXT NOT NULL, state TEXT NOT NULL,
    created REAL NOT NULL, started REAL, ended REAL, outcome TEXT, profile TEXT NOT NULL DEFAULT 'luna-low'
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(direction) WHERE state IN ('queued','running');
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY, job INTEGER NOT NULL REFERENCES jobs(id), stage TEXT NOT NULL,
    date TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1, tokens INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL, model TEXT, latency_ms INTEGER, provider_request TEXT, result TEXT, checkpoint TEXT
);
CREATE INDEX IF NOT EXISTS receipts_job ON receipts(job,id);
CREATE TABLE IF NOT EXISTS preference_runs (
    id INTEGER PRIMARY KEY, direction TEXT NOT NULL, day TEXT NOT NULL,
    started REAL NOT NULL, status TEXT NOT NULL, evidence TEXT NOT NULL,
    model TEXT, tokens INTEGER, result TEXT, UNIQUE(direction,day)
);
"""
REVIEW_DELAY = 24 * 60 * 60
KEY_PAUSE = "paused_"
# 每轮最多有六个新名字，库存降到两轮的量就开始补牌。
REFILL_STOCK = 12
CONTINUE_WINDOW = 30 * 60
CONTINUE_OUTCOMES = ("provider_connection", "provider_timeout", "interrupted")


class Rejected(ValueError):
    pass


def as_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def decode(value, fallback=None):
    return json.loads(value) if value else fallback


class Notebook:
    def __init__(self, root, *, now=time.time, review_delay=REVIEW_DELAY):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.resolve() != self.root.absolute():
            raise ValueError("noncanonical_data_directory")
        self.path = self.root / "notebook.sqlite3"
        if self.path.is_symlink():
            raise ValueError("invalid_database_path")
        self.now, self.review_delay = now, review_delay
        with self.transaction() as db:
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            fields = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            if "profile" not in fields:
                db.execute("ALTER TABLE jobs ADD COLUMN profile TEXT NOT NULL DEFAULT 'luna-low'")
            if "card_note" not in {row["name"] for row in db.execute("PRAGMA table_info(names)")}:
                db.execute("ALTER TABLE names ADD COLUMN card_note TEXT")
            if "checkpoint" not in {row["name"] for row in db.execute("PRAGMA table_info(receipts)")}:
                db.execute("ALTER TABLE receipts ADD COLUMN checkpoint TEXT")
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self, write=True):
        db = sqlite3.connect(self.path, timeout=8, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            if db.in_transaction:
                db.commit()
        finally:
            db.close()

    @staticmethod
    def check(role, direction):
        if role not in ROLES or direction not in DIRECTIONS:
            raise Rejected("invalid_scope")

    @staticmethod
    def setting(db, key, default=None):
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return decode(row["value"], default) if row else default

    @staticmethod
    def put_setting(db, key, value):
        db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, as_json(value)))

    def names(self, db, direction):
        return {row["id"]: dict(row) for row in db.execute(
            "SELECT * FROM names WHERE direction=? ORDER BY id", (direction,))}

    def opinions(self, db, direction):
        state = {}
        for row in db.execute("""SELECT f.* FROM feedback f JOIN names n ON n.id=f.name_id
                              WHERE n.direction=? AND f.retracted=0 ORDER BY f.id""", (direction,)):
            state.setdefault(row["name_id"], {}).setdefault(row["role"], {})[row["stage"]] = (
                row["value"], row["created"], row["id"])
        return state

    @staticmethod
    def shared(state):
        return all("first" in state.get(role, {}) for role in ROLES)

    @staticmethod
    def released(state, stage):
        return all(stage in state.get(role, {}) for role in ROLES)

    def effective(self, record, reveal_review=True):
        return combined_rating(record.get("first", (None,))[0],
                               record.get("review", (None,))[0] if reveal_review else None)

    def vetoes(self, opinions, visible_to=None):
        return {key for key, state in opinions.items() if any(
            state.get(role, {}).get("veto", ("",))[0] == "veto"
            and (role == visible_to or self.shared(state)) for role in ROLES)}

    def candidate_payload(self, candidate, details=False):
        result = {"id": candidate["id"], "name": candidate["name"]}
        if details:
            result.update(pinyin=candidate["pinyin"], detail=candidate["detail"],
                          evidence=decode(candidate["evidence"], []), origin=candidate["origin"])
        return result

    def projected_names(self, role, names, opinions):
        other = next(person for person in ROLES if person != role)
        personal, common = [], []
        for key, state in opinions.items():
            own, partner = state.get(role, {}), state.get(other, {})
            if key not in names or not own:
                continue
            first = own.get("first", (None,))[0]
            mutually_seen = self.shared(state)
            review_shared = self.released(state, "review")
            visible_pair = [self.effective(state.get(person, {}), review_shared) for person in ROLES]
            rec = self.candidate_payload(names[key], details=True)
            rec.update(my_rating=self.effective(own), first=first, excluded=own.get("veto", ("",))[0] == "veto",
                       partner_rating=self.effective(partner, review_shared) if mutually_seen else None,
                       partner_excluded=mutually_seen and partner.get("veto", ("",))[0] == "veto",
                       my_ready=own.get("ready", ("",))[0] == "ready", can_exclude=bool(first))
            personal.append(rec)
            shared_veto = mutually_seen and any(state[person].get("veto", ("",))[0] == "veto" for person in ROLES)
            # 同时复看后才改变双方可见的共同状态，避免由列表变化猜出私下反馈。
            if visible_pair == ["like", "like"] and not shared_veto:
                rec = dict(rec)
                rec["confirmed"] = self.released(state, "ready") and all(
                    state[person]["ready"][0] == "ready" for person in ROLES)
                rec["stable"] = review_shared
                rec["can_confirm"] = self.effective(own) == "like"
                common.append(rec)
        return list(reversed(personal)), list(reversed(common))

    def task_payload(self, task, names):
        if not task:
            return None
        kind = task["kind"]
        candidate = names[task["first"]]
        result = {"id": task["id"], "kind": kind, "names": [self.candidate_payload(candidate)]}
        if kind != "compare":
            info = result["names"][0]
            evidence = decode(candidate["evidence"], [])
            cited = next((item for item in evidence if item.get("title") and
                          item.get("derivation") in ("exact", "inspired")), None)
            if candidate["card_note"]:
                info["card_note"] = candidate["card_note"]
            if cited:
                info["card_source"] = ("取自 " if cited["derivation"] == "exact" else "借意自 ") + cited["title"]
            elif not candidate["card_note"]:
                # 老候选的说明可能包含盲审措辞。首看只提供观察线索。
                info["card_note"] = f"留意「{candidate['name'][1:]}」两个字放在一起的感觉。"
            if kind == "review":
                info["pinyin"] = candidate["pinyin"]
        if kind == "compare":
            ordered = [result["names"][0], self.candidate_payload(names[task["second"]])]
            if task["id"] % 2 == 0:
                ordered.reverse()
            result["names"] = ordered
            result["question"] = AXES[task["axis"]]
        return result

    def preview_card(self, db, role, direction, task, names, opinions, denied, completed):
        # 到期复看和明确的对照题优先于备用名字。
        if self.review_candidate(role, names, opinions, denied, skip=task["first"]):
            return None
        is_flash = task["kind"] == "flash"
        skip = task["first"] if is_flash else None
        if self.compare_choice(db, role, direction, names, opinions, denied, completed + int(is_flash)):
            return None
        prepared = self.batch_card(db, role, direction, names, opinions, denied, skip=skip)
        if prepared:
            return prepared
        # 跨组时按同一套排序先算下一张；这里只读，正式派卡仍在保存事务内。
        choices = self.rank_flash(db, role, direction, names, opinions, denied, completed + int(is_flash),
                                  skip=skip, limit=1)
        return next(iter(choices), None)

    def snapshot(self, db, role, direction, *, daily=None):
        names = self.names(db, direction)
        opinions = self.opinions(db, direction)
        personal, common = self.projected_names(role, names, opinions)
        task = db.execute("SELECT * FROM tasks WHERE role=? AND direction=? AND state='open'",
                          (role, direction)).fetchone()
        job = self.latest_generation(db, direction)
        paused = self.setting(db, KEY_PAUSE + direction)
        count = db.execute("SELECT count(*) FROM tasks WHERE role=? AND direction=? AND state='done'",
                           (role, direction)).fetchone()[0]
        last = db.execute("SELECT 1 FROM tasks WHERE role=? AND direction=? AND state='done' LIMIT 1",
                          (role, direction)).fetchone()
        denied = self.vetoes(opinions, role)
        review_available = not task and not paused and self.review_candidate(
            role, names, opinions, denied) is not None
        inventory = sum(self.unseen_card(key, role, names, opinions, denied) for key in names)
        waiting_for_budget = (
            daily is not None and not paused and not task and not inventory and not review_available
            and (not job or (job["state"] == "complete" and job["outcome"] == "ready"))
            and not self.generation_budget_available(db, daily)
        )
        preview = None
        if task and task["kind"] != "compare" and not paused:
            flash_count = self.completed_flashes(db, role, direction)
            next_name = self.preview_card(db, role, direction, task, names, opinions, denied, flash_count)
            if next_name:
                preview = self.task_payload({"id": None, "kind": "flash", "first": next_name}, names)["names"][0]
        model = {key: job[key] for key in ("state", "outcome", "profile")} if job else None
        if model and model["state"] == "running":
            stage = db.execute("""SELECT stage FROM receipts WHERE job=? AND stage IN ('plan','generate','audit')
                                  ORDER BY id DESC LIMIT 1""", (job["id"],)).fetchone()
            model["stage"] = stage["stage"] if stage else "plan"
        return {"role": role, "direction": direction, "task": self.task_payload(task, names),
                "preview": preview, "available_names": inventory, "refill_wait": "budget" if waiting_for_budget else None,
                "personal": personal, "common": common, "paused": paused, "completed": count,
                "can_undo": bool(last), "review_available": review_available,
                "model": model,
                "model_profile": self.selected_profile(db, role), "simulation": self.setting(db, "simulation", False)}

    def state(self, role, direction, *, auto_refill=False, daily=None):
        self.check(role, direction)
        with self.transaction(write=False) as db:
            report = self.snapshot(db, role, direction, daily=daily)
            # 搜索完成时当前卡可能还开着。只要有新反馈且库存偏低，轮询也可以接着补牌。
            should_refill = (auto_refill and report["available_names"] <= REFILL_STOCK
                             and self.refill_eligible(db, direction) and self.generation_budget_available(db, daily))
        if should_refill:
            return self.assign(role, direction, auto_refill=True, daily=daily)
        return report

    def review_candidate(self, role, names, opinions, denied, *, skip=None):
        ready = []
        for key, state in opinions.items():
            if key == skip or key in denied or not self.shared(state) or key not in names:
                continue
            own = state[role]
            if "review" in own or "ready" in own:
                continue
            answers = [state[person]["first"][0] for person in ROLES]
            both_liked = answers == ["like", "like"]
            mixed = set(answers) == {"like", "later"}
            audit = key % 8 == 0 and "dislike" in answers and "like" in answers
            if not (both_liked or mixed or audit):
                continue
            due = max(state[person]["first"][1] for person in ROLES) + self.review_delay
            if self.now() >= due:
                ready.append((2 if both_liked else 1 if mixed else 0, due, key))
        return max(ready, key=lambda item: (item[0], -item[1], -item[2]))[2] if ready else None

    def preferences(self, names, opinions, role, shared_only=False):
        groups = {}
        for key, state in opinions.items():
            candidate = names[key]
            if candidate["origin"] == "history" or (shared_only and not self.shared(state)):
                continue
            value = self.effective(state.get(role, {}), reveal_review=not shared_only or self.released(state, "review"))
            if value:
                groups.setdefault(candidate["branch"], []).append((candidate["name"], candidate["source"], value))
        return groups

    def compare_choice(self, db, role, direction, names, opinions, denied, completed):
        # 至少完成几张单名卡。对照题保留为低频任务，已有独立结果才用于对照。
        if completed < 3:
            return None
        prior = list(db.execute("SELECT first,second,axis,done FROM tasks WHERE role=? AND direction=? AND kind='compare' ORDER BY id",
                                (role, direction)))
        if prior:
            since = db.execute("SELECT count(*) FROM tasks WHERE role=? AND direction=? AND kind='flash' AND state='done' AND done>=?",
                               (role, direction, prior[-1]["done"] or self.now())).fetchone()[0]
            if since < 4:
                return None
        seen = {(item["first"], item["second"], item["axis"]) for item in prior}
        for row in db.execute("SELECT * FROM comparisons WHERE direction=? ORDER BY id DESC", (direction,)):
            pair = (row["first"], row["second"], row["axis"])
            if pair in seen or any(key in denied or names[key]["visibility"] != "approved" for key in pair[:2]):
                continue
            records = [opinions.get(key, {}).get(role, {}) for key in pair[:2]]
            accepted_together = any(self.shared(opinions.get(key, {})) and all(
                self.effective(opinions[key][person], self.released(opinions[key], "review")) == "like"
                for person in ROLES) for key in pair[:2])
            if accepted_together:
                continue
            # 只根据当前人的已看记录选对照。另一方何时提交不会改变当前对照。
            if any(record.get("first", (None,))[0] == "dislike" for record in records):
                continue
            if any("first" in record for record in records):
                return pair
        return None

    def preference_bonus(self, db, role, direction, names):
        bonus = {}
        for row in db.execute("SELECT first,second,answer FROM tasks WHERE role=? AND direction=? AND kind='compare' AND state='done' ORDER BY id DESC LIMIT 4",
                              (role, direction)):
            if row["answer"] in ("both", "neither", "later"):
                continue
            for key in (row["first"], row["second"]):
                feature = (names[key]["branch"], names[key]["form"])
                add = .045 if str(key) == row["answer"] else -.045
                bonus[feature] = max(-.09, min(.09, bonus.get(feature, 0) + add))
        return bonus

    def batch_card(self, db, role, direction, names, opinions, denied, *, skip=None):
        batch = db.execute("SELECT names FROM batches WHERE role=? AND direction=? ORDER BY id DESC LIMIT 1",
                           (role, direction)).fetchone()
        return next((key for key in decode(batch["names"], []) if key != skip and key in names and
                     self.unseen_card(key, role, names, opinions, denied)), None) if batch else None

    @staticmethod
    def unseen_card(key, role, names, opinions, denied):
        return (names[key]["visibility"] == "approved" and key not in denied
                and "first" not in opinions.get(key, {}).get(role, {}))

    @staticmethod
    def completed_flashes(db, role, direction):
        return db.execute("SELECT count(*) FROM tasks WHERE role=? AND direction=? AND kind='flash' AND state='done'",
                          (role, direction)).fetchone()[0]

    def rank_flash(self, db, role, direction, names, opinions, denied, completed, *, skip=None, limit=6, first=None):
        other = next(person for person in ROLES if person != role)
        recent = [row[0] for row in db.execute(
            """SELECT n.branch FROM tasks t JOIN names n ON t.first=n.id
               WHERE t.role=? AND t.direction=? AND t.state='done' ORDER BY t.id DESC LIMIT 4""",
            (role, direction))]
        mine = self.preferences(names, opinions, role)
        partner = self.preferences(names, opinions, other, shared_only=True)
        bonus = self.preference_bonus(db, role, direction, names)
        candidates = [item for key, item in names.items()
                      if key != skip and self.unseen_card(key, role, names, opinions, denied)]
        choices = []
        while candidates and len(choices) < limit:
            if first is not None and not choices:
                card = names[first]
            else:
                card = rank(candidates, mine, partner, recent, bonus, completed + len(choices))
            choices.append(card["id"])
            recent = (recent + [card["branch"]])[-4:]
            candidates.remove(card)
        return choices

    def flash_candidate(self, db, role, direction, names, opinions, denied, completed, *, prepared=None):
        batched = self.batch_card(db, role, direction, names, opinions, denied)
        if batched:
            return batched
        if prepared not in names or not self.unseen_card(prepared, role, names, opinions, denied):
            prepared = None
        choices = self.rank_flash(db, role, direction, names, opinions, denied, completed, first=prepared)
        if choices:
            db.execute("INSERT INTO batches(role,direction,names,created) VALUES(?,?,?,?)",
                       (role, direction, as_json(choices), self.now()))
            return choices[0]
        return None

    def assign(self, role, direction, *, auto_refill=False, daily=None):
        self.check(role, direction)
        with self.transaction() as db:
            self.assign_task(db, role, direction)
            if auto_refill:
                self.refill(db, role, direction, daily)
            return self.snapshot(db, role, direction, daily=daily)

    def assign_task(self, db, role, direction, *, prepared=None):
        if (self.setting(db, KEY_PAUSE + direction) or
                db.execute("SELECT 1 FROM tasks WHERE role=? AND direction=? AND state='open'",
                           (role, direction)).fetchone()):
            return
        names, opinions = self.names(db, direction), self.opinions(db, direction)
        denied = self.vetoes(opinions, role)
        completed = self.completed_flashes(db, role, direction)
        review = self.review_candidate(role, names, opinions, denied)
        second = axis = None
        if review:
            kind, first = "review", review
        elif pair := self.compare_choice(db, role, direction, names, opinions, denied, completed):
            kind, (first, second, axis) = "compare", pair
        else:
            kind = "flash"
            first = self.flash_candidate(db, role, direction, names, opinions, denied, completed, prepared=prepared)
        if first:
            db.execute("INSERT INTO tasks(role,direction,kind,first,second,axis,created) VALUES(?,?,?,?,?,?,?)",
                       (role, direction, kind, first, second, axis, self.now()))

    def answer(self, role, direction, task_id, value, *, advance=False, prepared=None, auto_refill=False, daily=None):
        self.check(role, direction)
        if (type(task_id) is not int or not isinstance(value, str) or
                (prepared is not None and type(prepared) is not int)):
            raise Rejected("invalid_answer")
        with self.transaction() as db:
            task = db.execute("SELECT * FROM tasks WHERE id=? AND role=? AND direction=?",
                              (task_id, role, direction)).fetchone()
            if not task:
                raise Rejected("task_not_current")
            if task["state"] == "done" and task["answer"] == value:
                if advance:
                    self.assign_task(db, role, direction)
                    if auto_refill:
                        self.refill(db, role, direction, daily)
                return self.snapshot(db, role, direction, daily=daily)
            if task["state"] != "open" or self.setting(db, KEY_PAUSE + direction):
                raise Rejected("task_not_current")
            if advance and prepared is not None and task["kind"] != "compare":
                names, opinions = self.names(db, direction), self.opinions(db, direction)
                preview = self.preview_card(db, role, direction, task, names, opinions,
                                            self.vetoes(opinions, role), self.completed_flashes(db, role, direction))
                if prepared != preview:
                    prepared = None
            else:
                prepared = None
            now = self.now()
            if task["kind"] == "compare":
                valid = ("both", "neither", "later", str(task["first"]), str(task["second"]))
                if value not in valid:
                    raise Rejected("invalid_answer")
            else:
                if value not in RATINGS:
                    raise Rejected("invalid_answer")
                stage = "first" if task["kind"] == "flash" else "review"
                db.execute("""INSERT INTO feedback(task_id,role,name_id,stage,value,created) VALUES(?,?,?,?,?,?)""",
                           (task_id, role, task["first"], stage, value, now))
                if stage == "review" and value != "like":
                    db.execute("INSERT INTO feedback(role,name_id,stage,value,created) VALUES(?,?,?,?,?)",
                               (role, task["first"], "ready", "unready", now))
            db.execute("UPDATE tasks SET state='done',answer=?,done=? WHERE id=?", (value, now, task_id))
            if advance:
                self.assign_task(db, role, direction, prepared=prepared)
                if auto_refill:
                    self.refill(db, role, direction, daily)
            return self.snapshot(db, role, direction, daily=daily)

    def undo(self, role, direction):
        self.check(role, direction)
        with self.transaction() as db:
            if self.setting(db, KEY_PAUSE + direction):
                raise Rejected("task_not_current")
            task = db.execute("SELECT * FROM tasks WHERE role=? AND direction=? AND state='done' ORDER BY done DESC,id DESC LIMIT 1",
                              (role, direction)).fetchone()
            if not task:
                raise Rejected("nothing_to_undo")
            # 保留已提交回执。重开时用新的任务编号阻止旧网页的重发恢复旧判断。
            db.execute("UPDATE tasks SET state='void' WHERE role=? AND direction=? AND state='open'", (role, direction))
            db.execute("UPDATE tasks SET state='undone' WHERE id=?", (task["id"],))
            if task["kind"] != "compare":
                db.execute("UPDATE feedback SET retracted=1 WHERE task_id=?", (task["id"],))
                affected = ("review", "ready", "veto") if task["kind"] == "flash" else ("ready",)
                placeholders = ",".join("?" for _ in affected)
                db.execute(f"""UPDATE feedback SET retracted=1 WHERE role=? AND name_id=?
                               AND stage IN ({placeholders}) AND retracted=0""",
                           (role, task["first"], *affected))
            db.execute("INSERT INTO tasks(role,direction,kind,first,second,axis,created) VALUES(?,?,?,?,?,?,?)",
                       (role, direction, task["kind"], task["first"], task["second"], task["axis"], self.now()))
            return self.snapshot(db, role, direction)

    def name_action(self, role, direction, key, action):
        self.check(role, direction)
        if type(key) is not int or action not in ("ready", "unready", "veto", "unveto"):
            raise Rejected("invalid_answer")
        with self.transaction() as db:
            state = self.opinions(db, direction).get(key, {})
            own = state.get(role, {})
            if "first" not in own:
                raise Rejected("name_not_available")
            stage = "veto" if action in ("veto", "unveto") else "ready"
            if action == "ready" and (not self.shared(state) or
                    any(self.effective(state[person], self.released(state, "review")) != "like"
                        for person in ROLES) or self.effective(own) != "like" or
                    key in self.vetoes({key: state}, role)):
                raise Rejected("name_not_ready")
            if own.get(stage, (None,))[0] == action:
                return self.snapshot(db, role, direction)
            if action == "ready" and self.setting(db, KEY_PAUSE + direction):
                raise Rejected("paused")
            if action == "ready":
                db.execute("""UPDATE tasks SET state='void' WHERE role=? AND direction=? AND kind='review'
                              AND first=? AND state='open'""", (role, direction, key))
            db.execute("INSERT INTO feedback(role,name_id,stage,value,created) VALUES(?,?,?,?,?)",
                       (role, key, stage, action, self.now()))
            if action in ("veto", "unready"):
                if action == "veto":
                    db.execute("INSERT INTO feedback(role,name_id,stage,value,created) VALUES(?,?,?,?,?)",
                               (role, key, "ready", "unready", self.now()))
                if self.setting(db, KEY_PAUSE + direction) == key:
                    self.put_setting(db, KEY_PAUSE + direction, None)
            if action == "ready" and all(
                person == role or state[person].get("ready", ("",))[0] == "ready" for person in ROLES):
                self.put_setting(db, KEY_PAUSE + direction, key)
                db.execute("UPDATE tasks SET state='void' WHERE direction=? AND state='open'", (direction,))
                db.execute("UPDATE jobs SET state='cancelled',ended=?,outcome='confirmed' WHERE direction=? AND state IN ('queued','running')",
                           (self.now(), direction))
            return self.snapshot(db, role, direction)

    def resume(self, role, direction):
        self.check(role, direction)
        with self.transaction() as db:
            self.put_setting(db, KEY_PAUSE + direction, None)
            return self.snapshot(db, role, direction)

    def insert_name(self, db, direction, item, origin="generated", visibility="approved"):
        result = db.execute("""INSERT INTO names(direction,name,branch,form,source,pinyin,detail,card_note,quality,
                             evidence,visibility,origin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                             ON CONFLICT(direction,name) DO NOTHING""",
                            (direction, item["name"], item["branch"], item["form"], item.get("source"),
                             item.get("pinyin"), item.get("detail"), item.get("card_note"), item.get("quality", 1),
                             as_json(item.get("evidence", [])), visibility, origin))
        return result.lastrowid if result.rowcount else None

    def import_history(self, seed):
        surname, avoided, history = seed["surname"], seed["avoided"], seed["history"]
        if not isinstance(surname, str) or len(surname) != 1 or not isinstance(avoided, list):
            raise ValueError("invalid_history")
        identifier = hashlib.sha256(as_json(seed).encode()).hexdigest()
        with self.transaction() as db:
            existing = self.setting(db, "seed")
            if existing == identifier:
                return 0
            if existing or db.execute("SELECT 1 FROM names LIMIT 1").fetchone():
                raise ValueError("existing_notebook")
            self.put_setting(db, "seed", identifier)
            self.put_setting(db, "surname", surname)
            self.put_setting(db, "avoided", avoided)
            self.put_setting(db, "context", seed.get("context", {}))
            self.put_setting(db, "simulation", seed.get("simulation", False))
            total = 0
            for item in history:
                direction = item["direction"]
                self.check("husband", direction)
                if exclusion(item["name"], surname, [], set()):
                    raise ValueError("invalid_history_name")
                key = self.insert_name(db, direction, dict(item, branch="life", form="image"),
                                       origin="history", visibility="archived")
                if not key or set(item["ratings"]) != set(ROLES):
                    raise ValueError("invalid_history_ratings")
                for role, rating in item["ratings"].items():
                    if rating not in RATINGS:
                        raise ValueError("invalid_history_rating")
                    db.execute("INSERT INTO feedback(role,name_id,stage,value,created) VALUES(?,?,?,?,?)",
                               (role, key, "first", rating, self.now()))
                    if rating == "dislike":
                        db.execute("INSERT INTO feedback(role,name_id,stage,value,created) VALUES(?,?,?,?,?)",
                                   (role, key, "veto", "veto", self.now()))
                total += 1
            return total

    def selected_profile(self, db, role):
        stored = self.setting(db, "model_profile_" + role, DEFAULT_PROFILE)
        return stored if isinstance(stored, str) and stored in MODEL_PROFILES else DEFAULT_PROFILE

    def set_generation_profile(self, role, direction, profile):
        self.check(role, direction)
        if not isinstance(profile, str) or profile not in MODEL_PROFILES:
            raise Rejected("invalid_model_profile")
        with self.transaction() as db:
            self.put_setting(db, "model_profile_" + role, profile)
            return self.snapshot(db, role, direction)

    @staticmethod
    def latest_generation(db, direction):
        return db.execute("SELECT * FROM jobs WHERE direction=? ORDER BY id DESC LIMIT 1", (direction,)).fetchone()

    def enqueue_generation(self, db, direction, profile):
        # 入队固定档位，同方向只需一轮，双方可以接着看。
        return db.execute("INSERT INTO jobs(direction,state,created,profile) VALUES(?,'queued',?,?)",
                          (direction, self.now(), profile)).lastrowid

    def generation_budget_available(self, db, daily):
        if daily is None:
            return True
        day = beijing_day(self.now())
        active = db.execute("""
            SELECT sum(max(0, 3 - (SELECT count(DISTINCT stage) FROM receipts r
                       WHERE r.job=j.id AND (r.status='complete' OR (r.status='pending' AND r.date=?))
                       AND r.stage IN ('plan','generate','audit'))))
            FROM jobs j WHERE state IN ('queued','running')
        """, (day,)).fetchone()[0] or 0
        return self.used_budget(db, day) + active + 3 <= daily

    def refill_eligible(self, db, direction):
        if (not self.setting(db, "seed") or self.setting(db, "simulation", False)
                or self.setting(db, KEY_PAUSE + direction)):
            return False
        latest = self.latest_generation(db, direction)
        if not latest or latest["state"] == "cancelled":
            return True
        if latest["state"] != "complete" or latest["outcome"] != "ready":
            return False
        return db.execute(
            "SELECT 1 FROM tasks WHERE direction=? AND kind='flash' AND state='done' AND done>? LIMIT 1",
            (direction, latest["created"])).fetchone() is not None

    def refill(self, db, role, direction, daily):
        if not self.refill_eligible(db, direction):
            return
        names, opinions = self.names(db, direction), self.opinions(db, direction)
        denied = self.vetoes(opinions, role)
        remaining = sum(self.unseen_card(key, role, names, opinions, denied) for key in names)
        if remaining <= REFILL_STOCK and self.generation_budget_available(db, daily):
            self.enqueue_generation(db, direction, self.selected_profile(db, role))

    def request_generation(self, role, direction, profile=None, *, daily=None):
        self.check(role, direction)
        if profile is not None and (not isinstance(profile, str) or profile not in MODEL_PROFILES):
            raise Rejected("invalid_model_profile")
        with self.transaction() as db:
            profile = profile if profile is not None else self.selected_profile(db, role)
            if not self.setting(db, "seed"):
                raise Rejected("seed_missing")
            if self.setting(db, KEY_PAUSE + direction):
                raise Rejected("paused")
            latest = self.latest_generation(db, direction)
            if latest and latest["state"] in ("queued", "running"):
                return latest["id"]
            if not self.generation_budget_available(db, daily):
                raise Rejected("budget_or_stale")
            if (latest and latest["state"] == "failed" and latest["outcome"] in CONTINUE_OUTCOMES
                    and profile == latest["profile"] and self.now() - latest["created"] <= CONTINUE_WINDOW
                    and db.execute("""SELECT 1 FROM receipts WHERE job=? AND stage='plan' AND status='complete'
                                      AND (checkpoint IS NOT NULL OR result IS NOT NULL) LIMIT 1""",
                                   (latest["id"],)).fetchone()):
                # 保留失败回执和阶段结果。只有明确点击继续时，才再次提交未完成的阶段。
                db.execute("UPDATE jobs SET state='queued',started=NULL,ended=NULL,outcome=NULL WHERE id=?",
                           (latest["id"],))
                return latest["id"]
            db.execute("""UPDATE receipts SET checkpoint=NULL
                          WHERE checkpoint IS NOT NULL AND job IN (SELECT id FROM jobs WHERE direction=?)""",
                       (direction,))
            return self.enqueue_generation(db, direction, profile)

    def generation_profile(self, job):
        with self.transaction(write=False) as db:
            row = db.execute("SELECT profile FROM jobs WHERE id=?", (job,)).fetchone()
            if not row or row["profile"] not in MODEL_PROFILES:
                raise Rejected("invalid_model_profile")
            return row["profile"]

    def claim(self, direction=None):
        with self.transaction() as db:
            query = "SELECT id,direction FROM jobs WHERE state='queued'"
            args = ()
            if direction is not None:
                query += " AND direction=?"
                args = (direction,)
            row = db.execute(query + " ORDER BY id LIMIT 1", args).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET state='running',started=? WHERE id=?", (self.now(), row["id"]))
            return row["id"], row["direction"]

    def interrupt_running(self):
        with self.transaction() as db:
            db.execute("""UPDATE receipts SET status='interrupted' WHERE status='pending'
                          AND job IN (SELECT id FROM jobs WHERE state='running')""")
            db.execute("UPDATE jobs SET state='failed',ended=?,outcome='interrupted' WHERE state='running'",
                       (self.now(),))
            db.execute("UPDATE preference_runs SET status='interrupted' WHERE status='pending'")

    def finish_job(self, job, state, outcome):
        with self.transaction() as db:
            db.execute("UPDATE jobs SET state=?,outcome=?,ended=? WHERE id=? AND state='running'",
                       (state, outcome, self.now(), job))
            if state == "complete" or outcome not in CONTINUE_OUTCOMES:
                db.execute("UPDATE receipts SET checkpoint=NULL WHERE job=? AND checkpoint IS NOT NULL", (job,))

    def enough_budget(self, date, daily, needed):
        if daily is None:
            return True
        with self.transaction(write=False) as db:
            return self.used_budget(db, date) + needed <= daily

    @staticmethod
    def used_budget(db, day):
        return db.execute("""SELECT (SELECT count(*) FROM receipts WHERE date=?) +
                             (SELECT count(*) FROM preference_runs WHERE day=?)""", (day, day)).fetchone()[0]

    def reserve(self, job, stage, date, daily):
        with self.transaction() as db:
            row = db.execute("SELECT state FROM jobs WHERE id=?", (job,)).fetchone()
            if not row or row["state"] != "running" or (daily is not None and self.used_budget(db, date) >= daily):
                return None
            return db.execute("INSERT INTO receipts(job,stage,date,status) VALUES(?,?,?,'pending')",
                              (job, stage, date)).lastrowid

    def receipt(self, receipt, status, latency, meta, result=None, *, checkpoint=None):
        with self.transaction() as db:
            db.execute("""UPDATE receipts SET status=?,latency_ms=?,model=?,tokens=?,provider_request=?,result=?,checkpoint=?
                          WHERE id=?""", (status, latency, meta.get("model"), meta.get("tokens", 0),
                                         meta.get("request_id"), as_json(result) if result else None,
                                         as_json(checkpoint) if checkpoint else None, receipt))

    def completed_stage(self, job, stage):
        with self.transaction(write=False) as db:
            row = db.execute("""SELECT checkpoint,result FROM receipts
                                WHERE job=? AND stage=? AND status='complete' ORDER BY id DESC LIMIT 1""",
                             (job, stage)).fetchone()
            return (decode(row["checkpoint"]), decode(row["result"])) if row else (None, None)

    def set_receipt_result(self, job, stage, value):
        with self.transaction() as db:
            db.execute("""UPDATE receipts SET result=? WHERE id=(
                          SELECT max(id) FROM receipts WHERE job=? AND stage=? AND status='complete')""",
                       (as_json(value), job, stage))

    def publish(self, job, direction, candidates, pair, plan):
        with self.transaction() as db:
            row = db.execute("SELECT state FROM jobs WHERE id=? AND direction=?", (job, direction)).fetchone()
            if not row or row["state"] != "running" or self.setting(db, KEY_PAUSE + direction):
                return 0
            opinions = self.opinions(db, direction)
            existing = self.names(db, direction)
            denied = {existing[key]["name"] for key in self.vetoes(opinions)}
            surname, avoided = self.setting(db, "surname"), self.setting(db, "avoided", [])
            inserted = {}
            for item in candidates:
                if exclusion(item["name"], surname, avoided, denied):
                    continue
                saved = dict(item)
                if item.get("audit_note"):
                    saved["detail"] = f'{saved.get("detail", "")}\n{item["audit_note"]}'.strip()
                key = self.insert_name(db, direction, saved)
                if key:
                    inserted[item["name"]] = key
            axis = plan["compare_axis"]
            if (axis in AXES and len(pair) == 2 and all(isinstance(name, str) and name in inserted for name in pair)
                    and pair[0] != pair[1]):
                first, second = sorted(inserted[name] for name in pair)
                db.execute("INSERT INTO comparisons(direction,first,second,axis) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                           (direction, first, second, axis))
            self.put_setting(db, "last_published_" + direction, {"count": len(inserted), "time": self.now()})
            return len(inserted)

    def model_context(self, direction, *, current_job=None):
        from .search import estimate

        with self.transaction(write=False) as db:
            surname = self.setting(db, "surname")
            if not surname:
                return None
            names, opinions = self.names(db, direction), self.opinions(db, direction)
            recent, historical = [], []
            last_feedback = 0
            for key, state in opinions.items():
                if not self.shared(state):
                    continue
                name = names[key]
                ratings = {role: self.effective(state[role], self.released(state, "review")) for role in ROLES}
                archived = name["origin"] == "history"
                example = {"id": f"n{key}", "name": name["name"], "ratings": ratings,
                           "branch": None if archived else name["branch"], "form": None if archived else name["form"],
                           "source": name["source"],
                           "veto": any(state[role].get("veto", ("",))[0] == "veto" for role in ROLES)}
                if self.released(state, "ready"):
                    example["willing"] = {role: state[role]["ready"][0] == "ready" for role in ROLES}
                stages = ("first", "review") if self.released(state, "review") else ("first",)
                seen_at = max(state[role][stage][1] for role in ROLES for stage in stages)
                if archived:
                    historical.append(example)
                else:
                    recent.append((seen_at, key, example))
                    last_feedback = max(last_feedback, seen_at)
            recent.sort(key=lambda item: item[:2], reverse=True)
            historical.sort(key=lambda item: ("like" in item["ratings"].values(),
                                               "later" in item["ratings"].values()), reverse=True)
            examples = [example for _, _, example in recent[:18 if historical else 24]]
            examples.extend(historical[:24 - len(examples)])
            pair_outcomes = []
            grouped = {}
            for row in db.execute("""SELECT role,first,second,axis,answer,done FROM tasks
                                     WHERE direction=? AND kind='compare' AND state='done' ORDER BY id DESC""",
                                  (direction,)):
                identity = (row["first"], row["second"], row["axis"])
                grouped.setdefault(identity, {}).setdefault(row["role"], (row["answer"], row["done"]))
            for (first, second, axis), answers in grouped.items():
                if set(answers) == set(ROLES):
                    last_feedback = max(last_feedback, *(done or 0 for _, done in answers.values()))
                    pair_outcomes.append({"id": f"c{first}-{second}-{axis}", "axis": axis,
                                          "names": [names[first]["name"], names[second]["name"]],
                                          "answers": {role: names[int(value)]["name"] if value.isdecimal() else value
                                                      for role, (value, _) in answers.items()}})
            plans = [decode(row["result"], {}) for row in db.execute("""
                SELECT r.result FROM receipts r
                WHERE r.id IN (
                    SELECT max(r.id) FROM receipts r JOIN jobs j ON j.id=r.job
                    WHERE j.direction=? AND (? IS NULL OR j.id!=?) AND r.stage='plan'
                    AND r.status='complete' AND r.result IS NOT NULL GROUP BY r.job
                )
                ORDER BY r.id DESC LIMIT 2
                """, (direction, current_job, current_job))]
            context = self.setting(db, "context", {})
            stats = {}
            for role in ROLES:
                groups = self.preferences(names, opinions, role, shared_only=True)
                stats[role] = {}
                for branch, items in groups.items():
                    view = estimate(items)
                    stats[role][branch] = {k: view[k] for k in ("positive", "negative", "reduced")}
            return {"surname": surname, "avoided": self.setting(db, "avoided", []), "examples": examples,
                    "signals": context.get(direction, []), "branch_stats": stats, "comparisons": pair_outcomes[:6],
                    "known": [row["name"] for row in names.values()], "previous_plans": plans,
                    "sources": [row["source"] for row in names.values() if row["source"]],
                    "denied": [names[key]["name"] for key in self.vetoes(opinions)],
                    "last_shared_feedback": last_feedback}

    def analysis_state(self, direction, day, daily):
        with self.transaction(write=False) as db:
            if (self.setting(db, KEY_PAUSE + direction) or self.setting(db, "simulation", False) or
                    (daily is not None and self.used_budget(db, day) + 4 > daily) or
                    db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running') LIMIT 1").fetchone() or
                    db.execute("SELECT 1 FROM preference_runs WHERE direction=? AND day=?", (direction, day)).fetchone()):
                return None
            row = db.execute("""SELECT evidence FROM preference_runs WHERE direction=? AND status='complete'
                                ORDER BY id DESC LIMIT 1""", (direction,)).fetchone()
            return decode(row["evidence"], {}) if row else {}

    def reserve_analysis(self, direction, day, daily, evidence):
        with self.transaction() as db:
            if (self.setting(db, KEY_PAUSE + direction) or
                    (daily is not None and self.used_budget(db, day) + 4 > daily) or
                    db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running') LIMIT 1").fetchone()):
                return None
            result = db.execute("""INSERT INTO preference_runs(direction,day,started,status,evidence)
                                   VALUES(?,?,?,'pending',?) ON CONFLICT(direction,day) DO NOTHING""",
                                (direction, day, self.now(), as_json(evidence)))
            return result.lastrowid if result.rowcount else None

    def finish_analysis(self, run, status, meta, result=None):
        with self.transaction() as db:
            db.execute("UPDATE preference_runs SET status=?,model=?,tokens=?,result=? WHERE id=? AND status='pending'",
                       (status, meta.get("model"), meta.get("tokens", 0), as_json(result) if result else None, run))

    def latest_analysis(self, direction):
        with self.transaction(write=False) as db:
            row = db.execute("""SELECT evidence,result FROM preference_runs WHERE direction=? AND status='complete'
                               ORDER BY id DESC LIMIT 1""", (direction,)).fetchone()
            return (decode(row["evidence"], {}), decode(row["result"], {})) if row else ({}, {})

    def decision_features(self, direction):
        with self.transaction(write=False) as db:
            names, opinions = self.names(db, direction), self.opinions(db, direction)
            shared = {key: state for key, state in opinions.items() if self.shared(state)}
            denied = self.vetoes(shared)
            eligible = [key for key, row in names.items() if key not in denied and row["visibility"] == "approved"]
            pair = db.execute("SELECT 1 FROM comparisons WHERE direction=? LIMIT 1", (direction,)).fetchone()
            return {"paused": bool(self.setting(db, KEY_PAUSE + direction)),
                    "review_due": any(self.review_candidate(role, names, shared, denied) for role in ROLES),
                    "useful_pair": bool(pair and len(shared) >= 3),
                    "unseen": sum(key not in shared for key in eligible)}

    def proof(self, job):
        with self.transaction(write=False) as db:
            item = db.execute("SELECT id,direction,state,outcome,profile FROM jobs WHERE id=?", (job,)).fetchone()
            receipts = [dict(row) for row in db.execute(
                "SELECT stage,status,model,tokens,latency_ms,provider_request,result FROM receipts WHERE job=? ORDER BY id",
                (job,))]
            for receipt in receipts:
                data = decode(receipt.pop("result"), {})
                # 规划正文可含本次家庭偏好。验收只读计数与固定字段。
                if receipt["stage"] == "plan" and "hypotheses" in data:
                    data = {"hypotheses": len(data["hypotheses"]), "compare_axis": data["compare_axis"]}
                receipt["result"] = data
            return {"job": dict(item) if item else None, "receipts": receipts}
