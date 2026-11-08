import importlib.util
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("ok_init", ROOT / "__init__.py")
init = importlib.util.module_from_spec(spec)
sys.modules["ok_init"] = init
spec.loader.exec_module(init)
oath = init._oath
actions = init._actions


class FakeCtx:
    def __init__(self, config=None):
        self.config = config or {}
        self.hooks = {}
        self.tools = {}
        self.commands = {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_hook(self, name, cb, **kw):
        self.hooks[name] = cb

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = handler

    def register_command(self, name, cb, description=""):
        self.commands[name] = cb

    def register_cli_command(self, *a, **kw):
        pass


def setup_module():
    import tempfile

    os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="oathkeeper-test-")
    init.register(FakeCtx())


def _write_oath(text):
    path = oath.oath_path()
    path.write_text(text, encoding="utf-8")
    return path


def _home():
    return Path(os.environ["HERMES_HOME"])


def test_parse_oath_rules():
    parsed = oath.parse_oath_text(
        "# comment\n"
        "standing mission: keep the owner healthy\n"
        "never delete: ~/memory/**\n"
        "always require backup before modify: ~/skills/**\n"
        "bogus line here\n"
    )
    assert parsed["missions"] == ["keep the owner healthy"]
    assert len(parsed["rules"]) == 2
    assert parsed["rules"][0]["kind"] == "never" and parsed["rules"][0]["verb"] == "delete"
    assert parsed["rules"][1]["kind"] == "require" and parsed["rules"][1]["evidence"] == "backup"
    assert len(parsed["errors"]) == 1


def test_pattern_matching():
    assert oath.pattern_matches("/a/b/**", "/a/b/c/d.txt")
    assert oath.pattern_matches("/a/b/**", "/a/b")
    assert not oath.pattern_matches("/a/b/**", "/a/bc/x.txt")
    assert oath.pattern_matches("/a/b", "/a/b/c.txt")
    assert not oath.pattern_matches("/a/b", "/a/bc.txt")
    assert oath.pattern_matches("/a/*.md", "/a/x.md")
    assert not oath.pattern_matches("/a/*.md", "/a/x.txt")


def test_never_delete_blocks_terminal_rm():
    home = _home()
    protected = home / "memory"
    protected.mkdir(parents=True, exist_ok=True)
    (protected / "note.md").write_text("precious", encoding="utf-8")
    _write_oath(f"never delete: {protected}/**\n")
    verdict = init.on_pre_tool_call(
        "terminal", {"command": f"rm {protected}/note.md"}, session_id="s1"
    )
    assert verdict and verdict["action"] == "block"
    assert "oath line 1" in verdict["message"]
    assert (protected / "note.md").exists()


def test_unrelated_action_passes():
    home = _home()
    _write_oath(f"never delete: {home}/memory/**\n")
    verdict = init.on_pre_tool_call(
        "terminal", {"command": f"rm {home}/scratch/tmp.txt"}, session_id="s1"
    )
    assert verdict is None


def test_require_backup_blocks_then_passes_after_evidence():
    home = _home()
    skills = home / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    target = skills / "demo.md"
    target.write_text("v1", encoding="utf-8")
    _write_oath(f"always require backup before modify: {skills}/**\n")
    blocked = init.on_pre_tool_call(
        "terminal", {"command": f"sed -i s/v1/v2/ {target}"}, session_id="s2"
    )
    assert blocked and blocked["action"] == "block"
    # Record backup evidence through the real post_tool_call path.
    init.on_post_tool_call(
        "terminal", {"command": f"cp {target} {target}.bak"}, session_id="s2", status="success"
    )
    allowed = init.on_pre_tool_call(
        "terminal", {"command": f"sed -i s/v1/v2/ {target}"}, session_id="s2"
    )
    assert allowed is None
    # Evidence from another session must not satisfy this session.
    blocked_other = init.on_pre_tool_call(
        "terminal", {"command": f"sed -i s/v1/v2/ {target}"}, session_id="s3"
    )
    assert blocked_other and blocked_other["action"] == "block"


def test_oath_file_itself_protected():
    _write_oath("never delete: /nothing/**\n")
    verdict = init.on_pre_tool_call(
        "terminal", {"command": f"rm {oath.oath_path()}"}, session_id="s1"
    )
    assert verdict and verdict["action"] == "block"
    verdict2 = init.on_pre_tool_call(
        "write_file", {"path": str(oath.oath_path()), "content": "never delete: /x/**"},
        session_id="s1",
    )
    assert verdict2 and verdict2["action"] == "block"


def test_snapshot_detects_outside_change():
    home = _home()
    sworn = home / "sworn"
    sworn.mkdir(parents=True, exist_ok=True)
    target = sworn / "keep.md"
    target.write_text("original", encoding="utf-8")
    _write_oath(f"never modify: {sworn}/**\n")
    init._refresh_snapshot("s1", report=False)
    # An approved in-gate change is baselined silently, not reported.
    target.write_text("approved edit", encoding="utf-8")
    init._refresh_snapshot("s1", report=False)
    # Change the file outside any tool call; the next turn must see it.
    target.write_text("tampered", encoding="utf-8")
    ctx = init.on_pre_llm_call(is_first_turn=False, session_id="s1")
    assert ctx and "outside the gate" in ctx["context"]


def test_first_turn_brief_has_mission():
    _write_oath("standing mission: guard the owner's mornings\nnever delete: /x/**\n")
    ctx = init.on_pre_llm_call(is_first_turn=True, session_id="fresh-session")
    assert ctx and "guard the owner's mornings" in ctx["context"]
    assert "never delete: /x/**" in ctx["context"]


def test_oath_check_tool_dry_run():
    home = _home()
    _write_oath(f"never delete: {home}/memory/**\n")
    out = json.loads(
        init.oath_check_tool(
            {"tool_name": "terminal", "args": {"command": f"rm -rf {home}/memory"}},
            session_id="s9",
        )
    )
    assert out["allowed"] is False and out["violations"]
    out_ok = json.loads(
        init.oath_check_tool(
            {"tool_name": "terminal", "args": {"command": "echo hello"}},
            session_id="s9",
        )
    )
    assert out_ok["allowed"] is True


def test_command_scan_is_bounded():
    long_cmd = "echo " + ("x" * 5000) + " && rm /tmp/should-not-be-seen.txt"
    found = actions.command_actions(long_cmd)
    assert all("should-not-be-seen" not in a["target"] for a in found)
    found2 = actions.command_actions("rm -rf /tmp/victim && echo done")
    assert any(a["verb"] == "delete" and a["target"].endswith("/tmp/victim") for a in found2)


def test_ledger_records_blocks():
    home = _home()
    _write_oath(f"never delete: {home}/memory/**\n")
    init.on_pre_tool_call("terminal", {"command": f"rm {home}/memory/a.md"}, session_id="sL")
    rows = oath.read_ledger()
    assert any(r.get("event") == "blocked" for r in rows)
