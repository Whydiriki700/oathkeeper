"""Oathkeeper action extraction — what a tool call intends to change.

Tool calls arrive in two shapes:

  - Structured file tools (write_file, patch, read_file, ...) carry an
    explicit path argument; the verb is known from the tool itself.
  - The terminal tool carries a shell command string. Oathkeeper scans
    the bounded head of the command (first 4096 chars, linear scan) for
    mutation verbs (rm, mv, cp, dd, truncate, shred, tee, sed -i,
    in-place editors, output redirection) and extracts their targets.

Inline code (python -c, heredocs, scripts) is intentionally not parsed
deeper: that is a disclosed limit of the plugin, and the fingerprint
snapshot is the second line of defence that catches what static
extraction cannot see.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Dict, List, Tuple

try:
    from . import oath as _oath
except ImportError:  # loaded as a top-level module in tests
    import oath as _oath  # type: ignore

MAX_COMMAND_SCAN = 4096

_PATH_KEYS = ("path", "file_path", "filepath", "target_path", "destination", "dest", "filename", "file")

_TOOL_VERBS: List[Tuple[str, str]] = [
    ("write", "modify"),
    ("create", "create"),
    ("patch", "modify"),
    ("edit", "modify"),
    ("delete", "delete"),
    ("remove", "delete"),
    ("move", "modify"),
    ("rename", "modify"),
    ("copy", "create"),
]

_DELETE_CMDS = {"rm", "rmdir", "unlink", "shred"}
_MODIFY_CMDS = {"mv", "truncate", "chmod", "chown", "touch", "tee"}
_CREATE_CMDS = {"cp", "mkdir", "install", "ln"}
_EDITOR_CMDS = {"sed", "perl", "ed", "ex", "vim", "nvim", "nano", "code"}
_BACKUP_CMDS = {"cp", "rsync", "tar", "zip", "7z", "scp"}

_SEGMENT_SPLIT = re.compile(r"(?:&&|\|\||;|\||\n)")
_REDIRECT = re.compile(r"(?<![0-9])>>?\s*([^\s;|&]+)")
_GIT_MUTATION = re.compile(r"\bgit\s+(checkout|reset|clean|rm|mv)\b")


def _tokens(segment: str) -> List[str]:
    try:
        return shlex.split(segment, posix=True)
    except Exception:
        return segment.split()


def _operands(tokens: List[str], skip_values_for: Tuple[str, ...] = ()) -> List[str]:
    out: List[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in skip_values_for:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _looks_like_path(token: str) -> bool:
    if not token or token in {"-", "/dev/null", "/dev/stdout", "/dev/stderr"}:
        return False
    if any(ch in token for ch in "*?"):
        return False
    return "/" in token or "." in token or token.startswith("~")


def command_actions(command: str) -> List[Dict[str, str]]:
    """Extract (verb, target) pairs from a shell command string."""
    text = (command or "")[:MAX_COMMAND_SCAN]
    actions: List[Dict[str, str]] = []

    def add(verb: str, target: str) -> None:
        target = target.strip().strip("'\"")
        if _looks_like_path(target):
            normalized = _oath.normalize_path(target)
            entry = {"verb": verb, "target": normalized, "source": "terminal"}
            if entry not in actions:
                actions.append(entry)

    for match in _REDIRECT.finditer(text):
        add("modify", match.group(1))
    if _GIT_MUTATION.search(text):
        actions.append({"verb": "modify", "target": ".", "source": "terminal"})

    for segment in _SEGMENT_SPLIT.split(text):
        tokens = _tokens(segment.strip())
        if not tokens:
            continue
        cmd = tokens[0].rsplit("/", 1)[-1]
        rest = tokens[1:]
        if cmd in _DELETE_CMDS:
            for target in _operands(rest):
                add("delete", target)
        elif cmd == "mv":
            operands = _operands(rest, skip_values_for=("-t",))
            for target in operands[:-1]:
                add("modify", target)
            if operands:
                add("create", operands[-1])
        elif cmd == "cp":
            operands = _operands(rest, skip_values_for=("-t",))
            for target in operands:
                add("create", target)
        elif cmd in _MODIFY_CMDS:
            operands = _operands(rest, skip_values_for=("-t",))
            for target in operands:
                add("modify", target)
            if cmd == "tee":
                for target in operands:
                    add("create", target)
        elif cmd in _CREATE_CMDS:
            for target in _operands(rest, skip_values_for=("-t", "-m", "-o", "-g")):
                add("create", target)
        elif cmd in _EDITOR_CMDS:
            in_place = any(t == "-i" or t.startswith("-i") or t == "--in-place" for t in rest)
            if in_place or cmd not in {"sed", "perl"}:
                operands = _operands(rest, skip_values_for=("-e", "-f"))
                for target in operands:
                    add("modify", target)
        elif cmd == "dd":
            for tok in rest:
                if tok.startswith("of="):
                    add("modify", tok[3:])
        elif cmd in {"python", "python3", "node", "ruby"}:
            # Inline interpreters are not statically parsed (disclosed
            # limit); the snapshot diff covers their side effects.
            continue
    return actions


def extract_actions(tool_name: str, args: Any) -> List[Dict[str, str]]:
    """All state-changing actions a tool call intends, as
    [{"verb": ..., "target": normalized_abs_path, "source": ...}]."""
    args = args if isinstance(args, dict) else {}
    name = (tool_name or "").lower()

    if name == "terminal" or "command" in args and name.endswith("terminal"):
        command = str(args.get("command") or args.get("cmd") or "")
        return command_actions(command)

    actions: List[Dict[str, str]] = []
    verb = None
    for fragment, candidate in _TOOL_VERBS:
        if fragment in name:
            verb = candidate
            break
    targets: List[str] = []
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            targets.append(value)
    for key in ("old_path", "source_path", "src"):
        value = args.get(key)
        if isinstance(value, str) and value:
            targets.append(value)
    if verb is None and targets:
        # A tool that carries a path but is not a known mutator is most
        # likely a read; only classify when the name says mutation.
        return []
    for target in targets:
        actions.append(
            {"verb": verb or "modify", "target": _oath.normalize_path(target), "source": tool_name}
        )
    return actions


def is_backup_action(tool_name: str, args: Any) -> List[str]:
    """Targets this call copies or archives away (backup evidence)."""
    args = args if isinstance(args, dict) else {}
    name = (tool_name or "").lower()
    targets: List[str] = []
    if name == "terminal" or "command" in args:
        command = str(args.get("command") or args.get("cmd") or "")[:MAX_COMMAND_SCAN]
        for segment in _SEGMENT_SPLIT.split(command):
            tokens = _tokens(segment.strip())
            if not tokens:
                continue
            cmd = tokens[0].rsplit("/", 1)[-1]
            if cmd in _BACKUP_CMDS:
                operands = _operands(tokens[1:], skip_values_for=("-t",))
                for target in operands[:-1] if len(operands) > 1 else operands:
                    if _looks_like_path(target):
                        targets.append(_oath.normalize_path(target))
    return targets
