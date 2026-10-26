"""Bounded official release audit. No execution of downloaded installers.

The daily maintenance chat reviews candidates, runs acceptance and installs
compatible valuable upgrades. This fetch step never mutates the live runtime.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import urllib.request
import urllib.error
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ["laya-mlx", "miloco", "miloco-miot", "sherpa-onnx", "pyatv", "trackers"]
REPOSITORIES = {"laya-mlx":"mizorewww/laya-mlx", "miloco":"XiaoMi/xiaomi-miloco"}


def fetch(url):
    request = urllib.request.Request(url,headers={"User-Agent":"Friday-dependency-audit/1"})
    with urllib.request.urlopen(request,timeout=20) as response:
        raw = response.read(4*1024*1024+1)
    if len(raw)>4*1024*1024: raise ValueError("metadata_budget_exceeded")
    return json.loads(raw)


def release_latest(repository):
    try:
        return fetch(f"https://api.github.com/repos/{repository}/releases/latest")
    except urllib.error.HTTPError as error:
        if error.code not in {403, 429}:
            raise
    # Same official publisher, read-only Atom feed; no token or limit bypass.
    with urllib.request.urlopen(urllib.request.Request(f"https://github.com/{repository}/releases.atom",
             headers={"User-Agent":"Friday-dependency-audit/1"}),timeout=20) as response:
        raw = response.read(1024*1024+1)
    if len(raw)>1024*1024: raise ValueError("metadata_budget_exceeded")
    namespace = {"a":"http://www.w3.org/2005/Atom"}
    for entry in ET.fromstring(raw).findall('a:entry',namespace):
        link = entry.find('a:link',namespace).attrib['href']
        prefix = f"https://github.com/{repository}/releases/tag/"
        tag = link.removeprefix(prefix)
        if link.startswith(prefix) and re.fullmatch(r'v?\d+\.\d+\.\d+',tag):
            return {"tag_name":tag,"html_url":link,"body":entry.findtext('a:content','',namespace),
                    "metadata_transport":"official_atom"}
    raise ValueError("stable_release_not_found")


def main():
    parser = argparse.ArgumentParser();parser.add_argument("--output",type=Path,default=ROOT/".artifacts/dependency-maintenance/latest.json")
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to(ROOT/".artifacts"):
        raise ValueError("report_must_be_project_artifact")
    code = "import importlib.metadata as m,json;print(json.dumps({p:m.version(p) for p in "+repr(PACKAGES)+"}))"
    installed = json.loads(subprocess.check_output([str(ROOT/"data/embodied/runtime/bin/python"),"-c",code],text=True))
    report = {"schema":1,"checked_at":datetime.now(timezone.utc).isoformat(),"packages":[],"upgrades_applied":False}
    for package in PACKAGES:
        row = {"package":package,"installed":installed[package]}
        try:
            if package in {"miloco", "miloco-miot"}:
                release = release_latest("XiaoMi/xiaomi-miloco")
                latest = release["tag_name"].removeprefix('v')
                row.update(latest=latest,changed=latest!=installed[package],source=release["html_url"],
                           release_tag=release["tag_name"],release_url=release["html_url"],notes=release.get("body","")[:20000],
                           review="candidate_review_required" if latest!=installed[package] else "current")
                report["packages"].append(row)
                continue
            metadata = fetch(f"https://pypi.org/pypi/{package}/json")
            latest = metadata["info"]["version"]
            row.update(latest=latest,changed=latest!=installed[package],source=f"https://pypi.org/project/{package}/{latest}/",
                       files=[{"filename":x["filename"],"sha256":x["digests"]["sha256"],"url":x["url"]} for x in metadata["urls"] if not x.get("yanked")])
            if package in REPOSITORIES:
                try:
                    release = release_latest(REPOSITORIES[package])
                    row.update(release_tag=release["tag_name"],release_url=release["html_url"],notes=release.get("body","")[:20000])
                except Exception as error:
                    row.update(notes_status="unavailable", notes_error=type(error).__name__)
            row["review"] = "candidate_review_required" if row["changed"] else "current"
        except Exception as error:
            row.update(review="source_unavailable",error_type=type(error).__name__)
        report["packages"].append(row)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    pending = args.output.with_suffix('.pending');pending.write_text(json.dumps(report,ensure_ascii=False,indent=2));pending.chmod(0o600)
    pending.replace(args.output)
    print(json.dumps({"checked_at":report["checked_at"],"packages":[{k:r.get(k) for k in ["package","installed","latest","review"]} for r in report["packages"]]},ensure_ascii=False))


if __name__ == "__main__": main()
