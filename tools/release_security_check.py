"""Bounded release checks; report locations, never credential contents."""
import json
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEY = re.compile(r'''(?:YOUDAO_(?:TTS_)?APP_SECRET|youdao_(?:tts_)?app_secret|api_key)["']?\s*[=:]\s*["']([^"'\r\n]+)["']''', re.I)
BAD_PATH = re.compile(r'(^|/)(\.env(?:\.[^/]*)?|config\.json|data/.*|.*\.dpapi|.*\.log)$', re.I)

def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)

def suspicious(data):
    values = KEY.findall(data.decode("utf-8", errors="ignore"))
    return any(len(value) >= 12 and not any(token in value.lower() for token in
               ("synthetic", "sample", "test", "your", "example", "placeholder", "dummy", "这里", "填入"))
               and value not in ("tts-app-secret", "app-secret") for value in values)

def main():
    report = {"history_revisions": 0, "findings": [], "archives": []}
    for revision in git("rev-list", "--all").decode().splitlines():
        report["history_revisions"] += 1
        for name in git("ls-tree", "-r", "--name-only", revision).decode().splitlines():
            if BAD_PATH.search(name):
                report["findings"].append({"revision": revision[:12], "path": name, "reason": "private runtime file"})
            if name.endswith((".py", ".ahk", ".json", ".md", ".txt", ".env")) and suspicious(git("show", f"{revision}:{name}")):
                report["findings"].append({"revision": revision[:12], "path": name, "reason": "possible literal credential; manual review"})
    tracked = git("ls-files").decode().splitlines()
    source_files = set(tracked + git("ls-files", "--others", "--exclude-standard").decode().splitlines())
    report["worktree_source_files"] = len(source_files)
    for name in source_files:
        path = ROOT / name
        if BAD_PATH.search(name):
            report["findings"].append({"path": name, "reason": "tracked private runtime file"})
        if path.is_file() and path.suffix in (".py", ".ahk", ".json", ".md", ".txt") and suspicious(path.read_bytes()):
            report["findings"].append({"path": name, "reason": "possible literal credential; manual review"})
    for path in (ROOT / "release").glob("TranEasy*portable.zip"):
        with zipfile.ZipFile(path) as archive:
            bad = [name for name in archive.namelist()
                   if BAD_PATH.search(name.partition("/")[2])
                   and not name.partition("/")[2].startswith("_internal/")]
        report["archives"].append({"path": str(path.relative_to(ROOT)), "private_entries": bad})
    destination = ROOT / "release" / "security-check.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(bool(report["findings"] or any(item["private_entries"] for item in report["archives"])))

if __name__ == "__main__":
    raise SystemExit(main())
