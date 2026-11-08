"""Oathkeeper oath file — parsing, matching, fingerprints, ledger.

The oath is a plain line-based file the owner writes at
<HERMES_HOME>/oathkeeper/oath.txt:

  # comments start with #
  standing mission: <text injected on the first turn of each session>
  never <verb>: <path pattern>
  always require <evidence> before <verb>: <path pattern>

Verbs: delete, modify, create, any. Path patterns are matched against
normalized absolute paths; a pattern ending in "/**" covers a whole
tree, a pattern containing "*" is a glob, anything else matches the
exact path or anything under it as a directory prefix.

Evidence kinds for "always require" are recorded by the hook layer in
an evidence log (see ledger rows with event="evidence"): a requirement
passes when a matching evidence kind was recorded for a target at or
under the action's target earlier in the same session.

Fingerprints: for every distinct path pattern in the oath, a snapshot
maps each covered file to (size, mtime_ns, sha256 for small files).
Comparing snapshots across turns detects changes that never passed
the gate: edits by another process, a manual edit, another agent.

Everything is local under <HERMES_HOME>/oathkeeper/: oath.txt (owner
written), ledger.jsonl (append-only), snapshot.json (latest
fingerprint state). No network, no LLM.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOCK = threading.Lock()
_MAX_HASH_BYTES = 1_000_000
_MAX_SNAPSHOT_FILES = 5000

VERBS = ("delete", "modify", "create", "any")


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        pass
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path.home() / ".hermes"


def data_dir() -> Path:
    path = hermes_home() / "oathkeeper"
    path.mkdir(parents=True, exist_ok=True)
    return path


def oath_path() -> Path:
    return data_dir() / "oath.txt"


def ledger_path() -> Path:
    return data_dir() / "ledger.jsonl"


def snapshot_path() -> Path:
    return data_dir() / "snapshot.json"


# ---------------------------------------------------------------------------
# Oath parsing
# ---------------------------------------------------------------------------


def _expand(pattern: str) -> str:
    pattern = pattern.strip()
    if pattern.startswith("~"):
        pattern = str(Path.home()) + pattern[1:]
    pattern = pattern.replace("$HERMES_HOME", str(hermes_home()))
    pattern = pattern.replace("${HERMES_HOME}", str(hermes_home()))
    return pattern


def parse_oath_text(text: str) -> Dict[str, Any]:
    """Parse oath text into rules. Returns {missions, rules, errors}."""
    missions: List[str] = []
    rules: List[Dict[str, Any]] = []
    errors: List[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        low = line.lower()
        if low.startswith("standing mission:"):
            missions.append(line.split(":", 1)[1].strip())
            continue
        if low.startswith("never "):
            rest = line[len("never "):]
            verb, sep, pattern = rest.partition(":")
            verb = verb.strip().lower()
            if not sep or verb not in VERBS or not pattern.strip():
                errors.append(f"line {lineno}: cannot parse never rule: {raw!r}")
                continue
            rules.append(
                {"kind": "never", "verb": verb, "pattern": _expand(pattern),
                 "line": lineno, "raw": line}
            )
            continue
        if low.startswith("always require "):
            rest = line[len("always require "):]
            evidence, sep, tail = rest.partition(" before ")
            verb, sep2, pattern = tail.partition(":")
            verb = verb.strip().lower()
            if not sep or not sep2 or verb not in VERBS or not evidence.strip() or not pattern.strip():
                errors.append(f"line {lineno}: cannot parse require rule: {raw!r}")
                continue
            rules.append(
                {"kind": "require", "verb": verb, "pattern": _expand(pattern),
                 "evidence": evidence.strip().lower(), "line": lineno, "raw": line}
            )
            continue
        errors.append(f"line {lineno}: unrecognized oath line: {raw!r}")
    return {"missions": missions, "rules": rules, "errors": errors}


def load_oath() -> Dict[str, Any]:
    path = oath_path()
    if not path.exists():
        return {"missions": [], "rules": [], "errors": [], "exists": False}
    try:
        parsed = parse_oath_text(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"missions": [], "rules": [], "errors": [f"oath file unreadable: {exc}"], "exists": True}
    parsed["exists"] = True
    return parsed


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def normalize_path(value: str) -> str:
    value = os.path.expanduser(str(value))
    try:
        value = str(Path(value).resolve())
    except Exception:
        value = os.path.normpath(value)
    return value.replace("\\", "/")


def pattern_matches(pattern: str, target: str) -> bool:
    pat = normalize_path(pattern) if not _looks_globby(pattern) else pattern.replace("\\", "/")
    tgt = target.replace("\\", "/")
    if pat.endswith("/**"):
        base = pat[:-3]
        return tgt == base or tgt.startswith(base + "/")
    if _looks_globby(pat):
        return fnmatch.fnmatch(tgt, pat)
    return tgt == pat or tgt.startswith(pat + "/")


def _looks_globby(pattern: str) -> bool:
    body = pattern[:-3] if pattern.endswith("/**") else pattern
    return any(ch in body for ch in "*?[")


def rule_matches(rule: Dict[str, Any], verb: str, target: str) -> bool:
    if rule["verb"] not in (verb, "any"):
        return False
    return pattern_matches(rule["pattern"], target)


# ---------------------------------------------------------------------------
# Ledger (append-only) and evidence
# ---------------------------------------------------------------------------


def append_ledger(row: Dict[str, Any]) -> None:
    row = {"ts": time.time(), **row}
    with _LOCK:
        with ledger_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_ledger(limit: int = 200) -> List[Dict[str, Any]]:
    path = ledger_path()
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return rows


def evidence_satisfied(session_id: str, kind: str, target: str) -> bool:
    """True when evidence of `kind` was recorded this session for a
    target at or under the action target (same tree or an ancestor)."""
    for row in read_ledger(limit=500):
        if row.get("event") != "evidence":
            continue
        if row.get("session_id") != session_id:
            continue
        if str(row.get("kind") or "") != kind:
            continue
        ev_target = str(row.get("target") or "")
        if not ev_target:
            continue
        if target == ev_target or target.startswith(ev_target + "/") or ev_target.startswith(target + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Fingerprint snapshots of sworn surfaces
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return None


def _covered_files(pattern: str) -> List[Path]:
    pat = pattern.replace("\\", "/")
    if pat.endswith("/**"):
        base = Path(pat[:-3])
        if base.is_file():
            return [base]
        if not base.is_dir():
            return []
        out = []
        for item in sorted(base.rglob("*")):
            if item.is_file() and "__pycache__" not in str(item):
                out.append(item)
                if len(out) >= _MAX_SNAPSHOT_FILES:
                    break
        return out
    if _looks_globby(pat):
        parent = Path(pat).parent
        if not parent.is_dir():
            return []
        return [p for p in sorted(parent.glob(Path(pat).name)) if p.is_file()]
    target = Path(pat)
    if target.is_file():
        return [target]
    if target.is_dir():
        out = []
        for item in sorted(target.rglob("*")):
            if item.is_file() and "__pycache__" not in str(item):
                out.append(item)
                if len(out) >= _MAX_SNAPSHOT_FILES:
                    break
        return out
    return []


def build_snapshot(oath: Dict[str, Any]) -> Dict[str, Any]:
    files: Dict[str, Any] = {}
    patterns = []
    for rule in oath.get("rules") or []:
        if rule["pattern"] not in patterns:
            patterns.append(rule["pattern"])
    for pattern in patterns:
        for path in _covered_files(pattern):
            key = str(path).replace("\\", "/")
            if key in files:
                continue
            try:
                stat = path.stat()
            except Exception:
                continue
            entry: Dict[str, Any] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            if stat.st_size <= _MAX_HASH_BYTES:
                entry["sha256"] = _hash_file(path)
            files[key] = entry
    oath_hash = None
    opath = oath_path()
    if opath.exists():
        oath_hash = _hash_file(opath)
    return {"files": files, "oath_sha256": oath_hash}


def load_snapshot() -> Optional[Dict[str, Any]]:
    path = snapshot_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            return data
    except Exception:
        pass
    return None


def save_snapshot(snapshot: Dict[str, Any]) -> None:
    path = snapshot_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def diff_snapshots(old: Dict[str, Any], new: Dict[str, Any]) -> List[Dict[str, str]]:
    """Changes between two snapshots: added, removed, modified files,
    plus a sworn-oath-changed entry when the oath file itself moved."""
    changes: List[Dict[str, str]] = []
    old_files = old.get("files") or {}
    new_files = new.get("files") or {}
    for key in sorted(set(new_files) - set(old_files)):
        changes.append({"path": key, "change": "added"})
    for key in sorted(set(old_files) - set(new_files)):
        changes.append({"path": key, "change": "removed"})
    for key in sorted(set(old_files) & set(new_files)):
        before, after = old_files[key], new_files[key]
        if before.get("sha256") and after.get("sha256"):
            if before["sha256"] != after["sha256"]:
                changes.append({"path": key, "change": "modified"})
        elif before.get("size") != after.get("size") or before.get("mtime_ns") != after.get("mtime_ns"):
            changes.append({"path": key, "change": "modified"})
    if old.get("oath_sha256") != new.get("oath_sha256"):
        changes.append({"path": str(oath_path()), "change": "oath_file_changed"})
    return changes
