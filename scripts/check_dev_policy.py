#!/usr/bin/env python3
# Friday 开发约定检查；源文件由 friday-project-index 维护。
"""离线检查开发约定，显式同步受维护仓库，不操作服务或设备。"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "https://github.com/lijunfeng722/friday-project-index"
CORE_START = "<!-- friday-policy-core:start -->"
CORE_END = "<!-- friday-policy-core:end -->"
START = "<!-- friday-dev-policy:start -->"
END = "<!-- friday-dev-policy:end -->"
SNAPSHOT = ".github/friday-dev-policy.json"
CHECKER = "scripts/check_dev_policy.py"
TEMPLATE = ".github/pull_request_template.md"
WORKFLOW = ".github/workflows/development-policy.yml"
SKILL_REFERENCE = "skills/friday-development/references/development.md"
SIGNATURES = {
    CHECKER: "# Friday 开发约定检查；源文件由 friday-project-index 维护。",
    TEMPLATE: "<!-- Friday 开发约定模板；源文件由 friday-project-index 维护。 -->",
    WORKFLOW: "# Friday 开发约定工作流；源文件由 friday-project-index 维护。",
}
REFERENCE_HEADER = "<!-- 生成副本：在 friday-project-index/docs/development.md 修改，再运行规则同步。 -->\n\n"
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
PUBLIC_ERRORS = frozenset("""invalid_policy_or_event_json git_query_unavailable
git_reference_or_query_unavailable shared_rules_markers_invalid run_sync_from_friday_project_index
target_must_be_repository_root target_repository_not_managed generated_path_is_symlink
existing_file_is_not_managed existing_snapshot_is_not_managed policy_schema_invalid
shared_rules_digest_invalid commit_head_invalid commit_base_invalid event_commit_scope_invalid
event_default_branch_invalid event_commit_scope_unavailable
new_branch_baseline_unavailable""".split())


class PolicyError(ValueError):
    """只向检查日志返回固定错误码，不回显提交或 PR 的任意正文。"""


def digest(value):
    return hashlib.sha256(value).hexdigest()


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise PolicyError("invalid_policy_or_event_json") from None


def git(root, *args):
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        raise PolicyError("git_query_unavailable") from None
    if result.returncode:
        raise PolicyError("git_reference_or_query_unavailable")
    return result.stdout.strip()


def section(text, start, end):
    if text.count(start) != 1 or text.count(end) != 1:
        raise PolicyError("shared_rules_markers_invalid")
    before, remaining = text.split(start, 1)
    body, after = remaining.split(end, 1)
    return before, body.strip(), after


def canonical(root):
    if not (root / "repos.json").is_file():
        raise PolicyError("run_sync_from_friday_project_index")
    try:
        text = (root / "docs/development.md").read_text(encoding="utf-8")
        _, rules, _ = section(text, CORE_START, CORE_END)
        version = re.search(r"^规范版本：([0-9.\-]+)$", rules, re.M).group(1)
        rows = read_json(root / "repos.json")["repositories"]
        urls = {r["id"]: r["url"].rstrip("/") for r in rows}
        managed = {"friday-project-index": SOURCE, "friday": urls["friday"], "user-guide": urls["user-guide"]}
        assets = {p: (root / p).read_bytes() for p in SIGNATURES}
    except (OSError, KeyError, TypeError, AttributeError):
        raise PolicyError("run_sync_from_friday_project_index") from None
    return text, rules, version, managed, assets


def remote_url(value):
    authority, separator, path = value.partition(":")
    user, at, host = authority.partition("@")
    if separator and at and user == "git" and host == "github.com":
        value = "https://" + host + "/" + path
    return value.rstrip("/").removesuffix(".git")


def target_identity(root, managed):
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise PolicyError("target_must_be_repository_root")
    url = remote_url(git(root, "config", "--get", "remote.origin.url"))
    matches = [key for key, expected in managed.items() if remote_url(expected) == url]
    if len(matches) != 1:
        raise PolicyError("target_repository_not_managed")
    return matches[0]


def no_symlinks(root, relative):
    for part in [root / relative, *(root / relative).parents]:
        if part == root.parent:
            break
        if part.is_symlink():
            raise PolicyError("generated_path_is_symlink")


def expected_files(source, target):
    text, rules, version, managed, assets = canonical(source)
    role = target_identity(target, managed)
    agents_path = target / "AGENTS.md"
    no_symlinks(target, "AGENTS.md")
    current = agents_path.read_text(encoding="utf-8") if agents_path.exists() else "# 开发协作说明\n"
    block = START + "\n" + rules + "\n" + END
    if START in current or END in current:
        before, _, after = section(current, START, END)
        agents = before + block + after
    else:
        agents = current + ("\n" if current.endswith("\n") else "\n\n") + block + "\n"
    policy = {"schema_version": 1, "version": version, "repository": role,
              "source_repository": SOURCE, "source_document": "docs/development.md",
              "shared_rules": rules, "shared_rules_sha256": digest(rules.encode()),
              "assets_sha256": {p: digest(value) for p, value in assets.items()}}
    files = {**assets, "AGENTS.md": agents.encode(),
             SNAPSHOT: (json.dumps(policy, ensure_ascii=False, indent=2) + "\n").encode()}
    if role == "friday-project-index":
        files[SKILL_REFERENCE] = (REFERENCE_HEADER + text).encode()
    # 覆盖前先检查所有目的文件，避免发现同名用户文件时已经写入一半。
    for name in files:
        no_symlinks(target, name)
        path = target / name
        if path.exists() and name in SIGNATURES and target != source:
            if SIGNATURES[name] not in path.read_text(encoding="utf-8")[:250]:
                raise PolicyError("existing_file_is_not_managed")
        if path.exists() and name == SNAPSHOT:
            if read_json(path).get("source_repository") != SOURCE:
                raise PolicyError("existing_snapshot_is_not_managed")
    return files


def sync(source, targets, check=False):
    planned = [(root, expected_files(source, root)) for root in dict.fromkeys([source, *targets])]
    errors = []
    changed = 0
    for root, files in planned:
        for name, value in files.items():
            path = root / name
            if path.exists() and path.read_bytes() == value:
                continue
            if check:
                errors.append("generated_file_out_of_sync:" + name)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(value)
            try:
                temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o644)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            changed += 1
    return {"repositories": len(planned), "changed_files": changed, "errors": errors}


def check_repo(root):
    policy = read_json(root / SNAPSHOT)
    errors = []
    if (policy.get("schema_version") != 1 or policy.get("source_repository") != SOURCE
            or policy.get("repository") not in {"friday-project-index", "friday", "user-guide"}):
        raise PolicyError("policy_schema_invalid")
    rules = policy.get("shared_rules")
    if not isinstance(rules, str) or digest(rules.encode()) != policy.get("shared_rules_sha256"):
        raise PolicyError("shared_rules_digest_invalid")
    _, actual, _ = section((root / "AGENTS.md").read_text(encoding="utf-8"), START, END)
    if actual != rules:
        errors.append("agents_shared_rules_drift")
    hashes = policy.get("assets_sha256", {})
    if not isinstance(hashes, dict):
        raise PolicyError("policy_schema_invalid")
    for name in SIGNATURES:
        path = root / name
        if not path.is_file() or digest(path.read_bytes()) != hashes.get(name):
            errors.append("managed_asset_drift:" + name)
    if policy["repository"] == "friday-project-index":
        text, source_rules, version, _, _ = canonical(root)
        if version != policy.get("version") or source_rules != rules:
            errors.append("canonical_rules_not_synced")
        reference = root / SKILL_REFERENCE
        if not reference.is_file() or reference.read_text(encoding="utf-8") != REFERENCE_HEADER + text:
            errors.append("skill_reference_not_synced")
        skill = root / "skills/friday-development/SKILL.md"
        if not skill.is_file() or not re.search(r"^name: friday-development$", skill.read_text(), re.M):
            errors.append("skill_manifest_invalid")
    return {"policy_version": policy.get("version"), "repository": policy["repository"], "errors": errors}


def commit_language_errors(root, base, head="HEAD"):
    tip = git(root, "rev-parse", "--verify", "--end-of-options", head + "^{commit}")
    if not SHA.fullmatch(tip):
        raise PolicyError("commit_head_invalid")
    if base:
        start = git(root, "rev-parse", "--verify", "--end-of-options", base + "^{commit}")
        if not SHA.fullmatch(start):
            raise PolicyError("commit_base_invalid")
        revision = start + ".." + tip
        shas = git(root, "rev-list", "--first-parent", revision).splitlines()
    else:
        shas = [tip]
    return commit_language_errors_for_shas(root, shas)


def commit_language_errors_for_shas(root, shas):
    errors = []
    checked = exempt = 0
    for sha in shas:
        parents, author, email, message = git(root, "show", "-s", "--format=%P%x00%an%x00%ae%x00%B", sha).split("\0", 3)
        subject = message.splitlines()[0] if message else ""
        merge = len(parents.split()) > 1
        bot = author.endswith("[bot]") and email.endswith("[bot]@users.noreply.github.com")
        revert = subject.startswith('Revert "') and re.search(r"^This reverts commit [0-9a-f]{40,64}\.$", message, re.M)
        if merge or bot or revert:
            exempt += 1
        else:
            checked += 1
            if not re.search(r"[\u3400-\u9fff]", subject):
                errors.append("commit_subject_requires_chinese:" + sha[:12])
    return {"checked_commits": checked, "exempt_commits": exempt, "errors": errors}


def documentation_errors(body, bot=False):
    if bot:
        return []
    if not isinstance(body, str):
        return ["pr_documentation_impact_missing"]
    errors = []
    for label, code in [("用户文档", "user"), ("技术文档", "technical"), ("顶级索引", "index")]:
        # 只允许横向空白和 CRLF 行尾，避免空依据跨行吞入下一项内容后被误判为已填写。
        values = re.findall(r"^[ \t]*[-*]?[ \t]*" + label + r"[：:][ \t]*(.*?)[ \t]*\r?$", body, re.M)
        reasons = re.findall(r"^[ \t]*[-*]?[ \t]*" + label + r"依据[：:][ \t]*(.*?)[ \t]*\r?$", body, re.M)
        if len(values) != 1 or values[0] not in {"已更新", "无影响", "不适用"}:
            errors.append("pr_docs_impact_required:" + code)
            continue
        reason = reasons[0].strip() if len(reasons) == 1 else ""
        if not reason or reason in {"已更新", "无影响", "不适用", "无", "同上", "略"} or re.match(r"待填写|待确认|TODO|TBD|pending", reason, re.I):
            errors.append("pr_docs_reason_required:" + code)
        elif values[0] == "已更新" and not re.search(r"https?://\S+|[^\s`]+\.(?:md|rst|png|jpg|jpeg|svg)|#[0-9]+", reason):
            errors.append("pr_docs_reference_required:" + code)
    return errors


def push_event_commit_language_errors(root, event, after):
    commits = event.get("commits")
    if not isinstance(commits, list) or not commits or len(commits) >= 2048:
        raise PolicyError("event_commit_scope_invalid")
    commit_shas = [commit.get("id") for commit in commits if isinstance(commit, dict)]
    if (len(commit_shas) != len(commits)
            or any(not isinstance(sha, str) or not SHA.fullmatch(sha) for sha in commit_shas)
            or len(set(commit_shas)) != len(commit_shas)):
        raise PolicyError("event_commit_scope_invalid")
    shas = set(commit_shas)
    if after not in shas:
        raise PolicyError("event_commit_scope_invalid")
    history = git(root, "rev-list", "--first-parent", "--max-count=" + str(len(shas) + 1), after).splitlines()
    pushed = []
    for sha in history:
        if sha not in shas:
            break
        pushed.append(sha)
    if not pushed:
        raise PolicyError("event_commit_scope_invalid")
    return commit_language_errors_for_shas(root, pushed)


def event_checks(root, event):
    errors = []
    result = {"errors": errors}
    pr = event.get("pull_request")
    if isinstance(pr, dict):
        result.update(commit_language_errors(root, pr["base"]["sha"], pr["head"]["sha"]))
        errors = result["errors"]
        errors.extend(documentation_errors(pr.get("body"), pr.get("user", {}).get("type") == "Bot"))
        return result
    if "after" not in event or event.get("deleted"):
        result["commit_scope"] = "not_requested"
        return result
    before, after = event.get("before"), event["after"]
    if not isinstance(before, str) or not SHA.fullmatch(before) or not isinstance(after, str) or not SHA.fullmatch(after):
        raise PolicyError("event_commit_scope_invalid")
    def default_branch_baseline():
        branch = event.get("repository", {}).get("default_branch", "")
        if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", branch) or branch.startswith("-"):
            raise PolicyError("event_default_branch_invalid")
        ref = "refs/remotes/origin/" + branch
        try:
            baseline = git(root, "merge-base", after, ref)
        except PolicyError:
            if event.get("ref") != "refs/heads/" + branch:
                raise PolicyError("new_branch_baseline_unavailable") from None
            return None
        return None if baseline == after else baseline

    if set(before) == {"0"}:
        # 新分支不回扫已有历史；优先取默认分支的共同祖先，首次导入只检查当前提交。
        before = default_branch_baseline()
    else:
        try:
            git(root, "rev-parse", "--verify", "--end-of-options", before + "^{commit}")
        except PolicyError:
            # 已提供的事件清单必须完整校验，不能用基线回退绕过无效清单。
            if "commits" in event:
                return push_event_commit_language_errors(root, event, after)
            before = default_branch_baseline()
            if before is None:
                raise PolicyError("event_commit_scope_unavailable") from None
    return commit_language_errors(root, before, after)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="只读检查当前仓库与可选的新提交或 PR 信息")
    check.add_argument("--base", help="本次任务开始前的明确提交标识或 Git 引用")
    check.add_argument("--head", default="HEAD", help="本次检查的结束提交")
    check.add_argument("--event-file", type=Path, help="GitHub 提供的事件 JSON 文件")
    updater = commands.add_parser("sync", help="从元仓库显式同步受维护仓库的公共规则")
    updater.add_argument("--repo", action="append", type=Path, default=[], help="目标仓库根目录，可重复")
    updater.add_argument("--check", action="store_true", help="只检查生成副本，不写文件")
    args = parser.parse_args()
    try:
        if args.command == "sync":
            report = sync(ROOT, [p.expanduser().resolve() for p in args.repo], args.check)
        else:
            report = check_repo(ROOT)
            if args.base:
                additional = commit_language_errors(ROOT, args.base, args.head)
            elif args.event_file:
                additional = event_checks(ROOT, read_json(args.event_file))
            else:
                additional = {"commit_scope": "not_requested", "errors": []}
            report["errors"].extend(additional.pop("errors"))
            report.update(additional)
        report["ok"] = not report["errors"]
    except PolicyError as error:
        code = error.args[0] if len(error.args) == 1 and error.args[0] in PUBLIC_ERRORS else "policy_validation_failed"
        report = {"ok": False, "errors": [code]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # 不把文件内容、任意异常消息、提交正文或凭据回显到公开 CI 日志。
        report = {"ok": False, "errors": ["policy_validation_failed_check_inputs_and_scope"]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
