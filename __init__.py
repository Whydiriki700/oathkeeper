"""Oathkeeper — an owner's oath, enforced by the machine.

Prompt instructions are advice an agent can forget, compress away, or
talk itself out of. An oath is different: it is the short list of
things the owner says must never happen, and it is enforced by a
deterministic gate, not by the agent's goodwill.

The owner writes the oath once, in plain lines, at
<HERMES_HOME>/oathkeeper/oath.txt:

  standing mission: <text, briefed into the first turn of each session>
  never <delete|modify|create|any>: <path pattern>
  always require <evidence> before <verb>: <path pattern>

Oathkeeper then works in three places:

  - `pre_tool_call` evaluates every intended state-changing action
    against the oath. A `never` match is blocked with the violated
    oath line cited as the reason. An `always require` match is
    blocked until matching evidence (for example a backup copy) was
    recorded earlier in the same session.
  - `post_tool_call` records evidence (backup copies and archives)
    and refreshes the fingerprint snapshot of every sworn surface.
  - `pre_llm_call` injects the standing missions on the first turn,
    and reports fingerprint changes that happened outside the gate
    since the last snapshot: edits by another process, a manual edit,
    a second agent. Changes made through the gate are baselined, so
    only out-of-band movement is reported.

The oath file itself is protected by default: modifying or deleting
it is always blocked, and the agent is given no tool that can create,
edit, or delete the oath. Every verdict and every detected change is
written to an append-only ledger at
<HERMES_HOME>/oathkeeper/ledger.jsonl.

Everything is local. No network, no LLM, deterministic matching only.

Honest limits, also stated in the README: shell commands are scanned
in a bounded linear pass over their first 4096 characters; inline
interpreter code (python -c and friends) is not statically parsed, so
the snapshot diff is the line of defence there. Files above the hash
size limit are fingerprinted by metadata only. A change made and
reverted entirely between two turns leaves no trace.

Settings (config: plugins.entries.oathkeeper.settings):
  - enabled: true (default) enforces the oath; false disables it.
  - hash_limit_bytes: files up to this size are content-hashed
    (default 1000000).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

try:
    from . import actions as _actions
    from . import oath as _oath
except ImportError:  # loaded as a top-level module in tests
    import actions as _actions  # type: ignore
    import oath as _oath  # type: ignore

logger = logging.getLogger(__name__)

_CTX: Any = None
_PENDING_REPORTS: Dict[str, List[str]] = {}
_SNAPSHOT_READY = False


def _enabled() -> bool:
    if _CTX is not None:
        try:
            value = _CTX.get_config("enabled", True)
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() not in {"false", "0", "no", "off"}
        except Exception:
            pass
    return True


def _hash_limit() -> int:
    if _CTX is not None:
        try:
            value = _CTX.get_config("hash_limit_bytes", 1_000_000)
            if isinstance(value, int) and value > 0:
                return value
        except Exception:
            pass
    return 1_000_000


def _load() -> Optional[Dict[str, Any]]:
    try:
        return _oath.load_oath()
    except Exception:
        logger.debug("Oathkeeper: oath load failed", exc_info=True)
        return None


def evaluate_actions(
    tool_name: str, args: Any, session_id: str = ""
) -> List[Dict[str, Any]]:
    """Evaluate a tool call against the oath. Returns a list of
    violation dicts {rule, action, reason}; empty means it may pass."""
    parsed = _load()
    if parsed is None:
        return [{"rule": None, "action": None,
                 "reason": "Oathkeeper could not load the oath file, so this call was blocked fail-closed."}]
    violations: List[Dict[str, Any]] = []
    oath_file = _oath.normalize_path(str(_oath.oath_path()))
    intended = _actions.extract_actions(tool_name, args)
    for action in intended:
        verb, target = action["verb"], action["target"]
        if target == oath_file and verb in {"modify", "delete", "create"}:
            violations.append(
                {"rule": {"raw": "(built-in) the oath file itself is never modifiable by the agent"},
                 "action": action,
                 "reason": "the target is the oath file itself, which only the owner may change"}
            )
            continue
        for rule in parsed["rules"]:
            if not _oath.rule_matches(rule, verb, target):
                continue
            if rule["kind"] == "never":
                violations.append(
                    {"rule": rule, "action": action,
                     "reason": f"oath line {rule['line']} forbids {verb} on this target"}
                )
            elif rule["kind"] == "require":
                if not _oath.evidence_satisfied(session_id, rule["evidence"], target):
                    violations.append(
                        {"rule": rule, "action": action,
                         "reason": (f"oath line {rule['line']} requires evidence "
                                    f"'{rule['evidence']}' for this target earlier in this session")}
                    )
    return violations


def _block_message(violations: List[Dict[str, Any]]) -> str:
    lines = ["Oathkeeper blocked this tool call: it would break the owner's oath."]
    for violation in violations:
        rule = violation.get("rule") or {}
        action = violation.get("action") or {}
        lines.append(
            f"- {violation.get('reason')}. Rule: \"{rule.get('raw', 'fail-closed')}\". "
            f"Action: {action.get('verb', '?')} {action.get('target', '?')}."
        )
    lines.append(
        "Do not retry the same action. Either choose a different approach that "
        "respects the oath, or produce the required evidence first (for an "
        "'always require' rule) and then retry. The oath itself can only be "
        "changed by the owner, outside the agent. This attempt was recorded "
        "in the Oathkeeper ledger."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def on_pre_tool_call(tool_name: str, args: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    if not _enabled():
        return None
    parsed = _load()
    if parsed is not None and not parsed.get("exists"):
        return None
    session_id = str(kwargs.get("session_id") or "")
    try:
        violations = evaluate_actions(tool_name, args, session_id=session_id)
    except Exception:
        logger.warning("Oathkeeper: evaluation failed", exc_info=True)
        _oath.append_ledger(
            {"event": "blocked_eval_error", "tool": tool_name, "session_id": session_id}
        )
        return {
            "action": "block",
            "message": (
                "Oathkeeper could not evaluate this tool call against the oath "
                "(internal error), so the call was blocked fail-closed rather "
                "than shipped unchecked."
            ),
        }
    if not violations:
        return None
    _oath.append_ledger(
        {"event": "blocked", "tool": tool_name, "session_id": session_id,
         "violations": [{"rule": (v.get("rule") or {}).get("raw"), "reason": v.get("reason"),
                         "action": v.get("action")} for v in violations]}
    )
    return {"action": "block", "message": _block_message(violations)}


def on_post_tool_call(tool_name: str, args: Any = None, result: Any = None, **kwargs: Any) -> None:
    if not _enabled():
        return
    session_id = str(kwargs.get("session_id") or "")
    status = str(kwargs.get("status") or "")
    try:
        if status not in {"error", "failed", "failure"}:
            for target in _actions.is_backup_action(tool_name, args):
                _oath.append_ledger(
                    {"event": "evidence", "kind": "backup", "target": target,
                     "tool": tool_name, "session_id": session_id}
                )
        _refresh_snapshot(session_id, report=False)
    except Exception:
        logger.debug("Oathkeeper: post_tool_call handling failed", exc_info=True)


def _refresh_snapshot(session_id: str = "", report: bool = True) -> List[Dict[str, str]]:
    """Refresh the sworn-surface snapshot and return any changes.

    With report=True (called at the start of a turn, before the agent
    acts) differences against the last baseline count as out-of-band
    movement and are queued for reporting. With report=False (called
    after an approved tool call) the new state is baselined silently:
    changes the gate let through are not false alarms."""
    global _SNAPSHOT_READY
    parsed = _load()
    if parsed is None or not parsed.get("exists"):
        return []
    _oath._MAX_HASH_BYTES = _hash_limit()
    new = _oath.build_snapshot(parsed)
    old = _oath.load_snapshot()
    changes: List[Dict[str, str]] = []
    if old is not None:
        changes = _oath.diff_snapshots(old, new)
        if report:
            for change in changes:
                _oath.append_ledger(
                    {"event": "outside_change_detected", "session_id": session_id, **change}
                )
                _PENDING_REPORTS.setdefault(session_id or "*", []).append(
                    f"{change['change']}: {change['path']}"
                )
    _oath.save_snapshot(new)
    _SNAPSHOT_READY = True
    return changes if report else []


def on_pre_llm_call(
    is_first_turn: bool = False,
    user_message: str = "",
    session_id: str = "",
    platform: str = "",
    **kwargs: Any,
) -> Optional[Dict[str, str]]:
    if not _enabled():
        return None
    parsed = _load()
    if parsed is None or not parsed.get("exists"):
        return None
    try:
        _refresh_snapshot(session_id, report=True)
    except Exception:
        logger.debug("Oathkeeper: turn-start snapshot failed", exc_info=True)
    parts: List[str] = []
    key = session_id or "*"
    pending = _PENDING_REPORTS.pop(key, []) + _PENDING_REPORTS.pop("*", [])
    if pending:
        lines = [
            "Oathkeeper report: sworn surfaces changed outside the gate since "
            "the last check (not through a tool call this gate approved):"
        ]
        lines += [f"- {item}" for item in pending[:10]]
        lines.append(
            "Mention this to the owner in your reply and do not silently "
            "overwrite the changed files."
        )
        parts.append("\n".join(lines))
    if is_first_turn:
        brief = [
            "Oathkeeper is active in this profile. The owner has sworn an oath "
            "that a deterministic gate enforces on every tool call; blocks cite "
            "the oath line and are final, and the oath file itself can only be "
            "changed by the owner."
        ]
        if parsed["missions"]:
            brief.append("Standing missions from the oath:")
            brief += [f"- {mission}" for mission in parsed["missions"]]
        never = [r for r in parsed["rules"] if r["kind"] == "never"]
        require = [r for r in parsed["rules"] if r["kind"] == "require"]
        if never:
            brief.append("Never rules:")
            brief += [f"- {rule['raw']}" for rule in never]
        if require:
            brief.append("Evidence required before acting:")
            brief += [f"- {rule['raw']}" for rule in require]
        parts.append("\n".join(brief))
    if not parts:
        return None
    return {"context": "\n\n".join(parts)}


# ---------------------------------------------------------------------------
# User surfaces: oath_check / oath_status tools, /oath command, CLI
# ---------------------------------------------------------------------------


def oath_check_tool(params: Dict[str, Any] | None = None, **kwargs: Any) -> str:
    params = params or {}
    tool_name = str(params.get("tool_name") or "terminal")
    args = params.get("args") or {}
    session_id = str(kwargs.get("session_id") or "")
    violations = evaluate_actions(tool_name, args, session_id=session_id)
    return json.dumps(
        {"allowed": not violations,
         "violations": [{"rule": (v.get("rule") or {}).get("raw"), "reason": v.get("reason")}
                        for v in violations]}
    )


def _ledger_summary() -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in _oath.read_ledger(limit=500):
        event = str(row.get("event") or "?")
        counts[event] = counts.get(event, 0) + 1
    return counts


def _format_status() -> str:
    parsed = _load()
    if parsed is None:
        return "Oathkeeper: the oath could not be loaded."
    if not parsed.get("exists"):
        return ("Oathkeeper is installed but no oath file exists yet. The owner "
                f"creates one at {_oath.oath_path()} to activate enforcement.")
    lines = [f"Oathkeeper oath at {_oath.oath_path()}:"]
    if parsed["missions"]:
        lines.append("Standing missions:")
        lines += [f"- {mission}" for mission in parsed["missions"]]
    for rule in parsed["rules"]:
        lines.append(f"- line {rule['line']}: {rule['raw']}")
    if parsed["errors"]:
        lines.append("Parse errors (these lines are ignored):")
        lines += [f"- {error}" for error in parsed["errors"]]
    counts = _ledger_summary()
    if counts:
        lines.append("Ledger counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    else:
        lines.append("Ledger: no events yet.")
    return "\n".join(lines)


def oath_status_tool(params: Dict[str, Any] | None = None, **kwargs: Any) -> str:
    parsed = _load() or {}
    return json.dumps(
        {"success": True,
         "oath_exists": bool(parsed.get("exists")),
         "missions": parsed.get("missions") or [],
         "rules": parsed.get("rules") or [],
         "parse_errors": parsed.get("errors") or [],
         "ledger_counts": _ledger_summary()}
    )


def oath_command(raw_args: str = "", **kwargs: Any) -> str:
    return _format_status()


def _cli_setup(subparser: Any) -> None:
    sub = subparser.add_subparsers(dest="oathkeeper_action")
    sub.add_parser("status", help="Show the active oath and ledger summary")


def _cli_handler(args: Any) -> int:
    print(_format_status())
    return 0


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(ctx: Any) -> None:
    global _CTX
    _CTX = ctx

    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)

    ctx.register_command(
        "oath",
        oath_command,
        description="Show the active Oathkeeper oath and enforcement ledger",
    )
    ctx.register_tool(
        name="oath_check",
        toolset="oathkeeper",
        schema={
            "name": "oath_check",
            "description": (
                "Dry-check a hypothetical tool call against the owner's oath "
                "without executing it. Returns whether it would be allowed "
                "and which oath lines it would break."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "description": "Tool that would run, e.g. terminal or write_file"},
                    "args": {"type": "object", "description": "The arguments the tool would receive"},
                },
                "required": ["tool_name", "args"],
            },
        },
        handler=oath_check_tool,
    )
    ctx.register_tool(
        name="oath_status",
        toolset="oathkeeper",
        schema={
            "name": "oath_status",
            "description": "Return the active oath (missions, rules, parse errors) and the enforcement ledger summary.",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=oath_status_tool,
    )
    ctx.register_cli_command(
        "oathkeeper",
        "Inspect the Oathkeeper oath and enforcement ledger",
        _cli_setup,
        _cli_handler,
        description="Show the active oath recorded on this machine",
    )
