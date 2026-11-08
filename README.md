# Oathkeeper

**An owner's oath, enforced by the machine.**

> Hermes gives an agent capabilities. Oathkeeper gives it an oath.

Prompt instructions are advice: an agent can forget them, a context
compaction can compress them away, and a confident model can talk
itself into an exception. An oath is the opposite of advice. It is the
short list of things the owner says must never happen, and Oathkeeper
enforces it with a deterministic gate at the tool boundary, not with
the agent's goodwill.

## The oath

The owner writes the oath once, in plain lines, at
`<HERMES_HOME>/oathkeeper/oath.txt`:

```
# my agent's oath
standing mission: keep the owner's work moving without being asked twice
never delete: ~/.hermes/memories/**
never modify: ~/.hermes/skills/**
always require backup before modify: ~/.hermes/skills/**
```

Three line kinds:

- `standing mission: <text>` is briefed into the first turn of every
  session, as a named mission that outlives any single task.
- `never <delete|modify|create|any>: <path pattern>` is an invariant.
  A matching tool call is blocked, and the block message cites the
  exact oath line that was violated.
- `always require <evidence> before <verb>: <path pattern>` is a
  prerequisite gate. The action passes only when matching evidence
  (for example a backup copy made with `cp`, `rsync`, `tar`, `zip`, or
  `scp`) was recorded earlier in the same session.

Patterns match normalized absolute paths. A pattern ending in `/**`
covers a whole tree, a pattern containing `*` is a glob, and any other
pattern matches the path itself or anything under it. `~` and
`$HERMES_HOME` are expanded.

## How it enforces

- **`pre_tool_call`** extracts the state-changing actions a tool call
  intends (structured path tools directly; terminal commands via a
  bounded linear scan for `rm`, `mv`, `cp`, `dd`, `truncate`, `tee`,
  in-place editors, redirection, and mutating `git` verbs) and
  evaluates each against the oath. Any violation blocks the whole
  call, fail-closed, with the oath line cited.
- **`post_tool_call`** records backup evidence and refreshes the
  fingerprint snapshot of every sworn surface: SHA-256 for files up to
  the configured hash limit, size and mtime above it.
- **`pre_llm_call`** injects the standing missions and the active rules
  on the first turn, and reports fingerprint changes that happened
  **outside the gate** since the last snapshot: an edit by another
  process, a manual edit, a second agent. Changes made through
  approved tool calls are re-baselined, so only out-of-band movement
  is reported.

The oath file itself is protected by default: the agent cannot modify
or delete it through any tool call, and Oathkeeper deliberately
registers **no tool that can create, edit, weaken, or delete the
oath**. Changing the oath is an owner act, done outside the agent.

Every block, every piece of evidence, and every detected outside
change is appended to `<HERMES_HOME>/oathkeeper/ledger.jsonl`.
Inspect everything with `/oath`, the `oath_status` tool, or
`hermes oathkeeper status`. The `oath_check` tool dry-checks a
hypothetical call against the oath without executing it.

## Configuration

```yaml
plugins:
  entries:
    oathkeeper:
      settings:
        enabled: true            # false disables enforcement entirely
        hash_limit_bytes: 1000000  # files up to this size are content-hashed
```

If no oath file exists, Oathkeeper is inert: nothing is blocked and
nothing is injected. Enforcement starts when the owner writes the oath.

## Honest limits

- Terminal commands are scanned in one bounded linear pass over their
  first 4096 characters. Inline interpreter code (`python -c`,
  heredocs, scripts) is not statically parsed; the fingerprint
  snapshot is the second line of defence that catches those side
  effects after the fact.
- Files above the hash limit are fingerprinted by metadata (size,
  mtime) only, so a same-size, same-mtime content swap would not be
  detected.
- Snapshots are compared at turn boundaries. A change made and
  reverted entirely between two turns leaves no trace.
- Evidence is session-scoped: a backup made in a previous session does
  not satisfy an `always require` rule in this one. That is deliberate;
  prerequisites must be fresh.

## Disclosure

Oathkeeper reads tool call arguments (to extract intended file
actions), reads the files covered by oath patterns (to fingerprint
them), and writes only under `<HERMES_HOME>/oathkeeper/`. It makes no
network calls, starts no processes, and calls no model. Declared
capabilities in `plugin.yaml` match the registered hooks and tools
exactly.

## Origin

The design is distilled from a real long-running personal agent whose
owner wrote it a permanent oath: never delete its own memory, its
conscience, or the files its owner wrote for it, and whose evolution
safety layer refuses any self-modification that would break that
promise. Oathkeeper turns that one agent's promise into a mechanism
any Hermes owner can swear for their own agent.

## Verification

- `hermes plugins validate`: passed, security scan safe.
- `hermes plugins doctor`: OK, 2 tools and 3 hooks registered under
  the real runtime contracts.
- Unit tests cover oath parsing, pattern matching, `never` blocks on
  terminal and structured tools, `always require` evidence gating
  (including cross-session evidence rejection), oath-file
  self-protection, outside-change snapshot detection, first-turn
  mission briefing, and the bounded command scan.
- Live test in a real Hermes session: deleting a sworn file is blocked
  with the oath line cited; modifying a sworn skill is blocked until a
  backup is recorded, then allowed; a sworn file changed outside the
  session is reported on the next turn from the snapshot diff.
