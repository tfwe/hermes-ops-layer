# hermes-ops-layer

Guardrail toolkit around the Hermes Agent framework: a claim gate that keeps model claims in prompt files equal to `config.yaml`, a skill-reference checker, a dispatch-envelope gate, a writer-route audit, and a zero-token watchdog. Each check runs offline with no model calls.

The Hermes Agent framework (Nous Research) is not included. Everything here is standalone: the three gate plugins ship in `plugins/`, so all six checks run on a clean checkout (see below).

## Layout

- `bin/hermes-claim-gate.py`: flags model ids and route names in memory, user and SOUL prompt files that disagree with `config.yaml`. Loads `bin/hermes-prompt-sync.py` from its own directory.
- `bin/hermes-prompt-sync.py` and `bin/hermes-prompt-sync.allow`: config parser and allowlist used by the claim gate. Needs PyYAML.
- `bin/hermes-skill-refs.py`, `bin/skill-refs-gate.py`: check that skill files reference existing, executable paths.
- `bin/dispatch-gate-selftest.py`, `bin/dispatch-marker-lint.py`: dispatch envelope gate and marker lint.
- `bin/writer-gate-selftest.py`, `bin/writer-route-audit.py`: writer route gate and audit.
- `bin/claim-gate-hook.test.sh`: tests for the claim-gate hook plugin.
- `plugins/`: the three gate plugins the selftests exercise — `claim-gate-hook`, `dispatch-envelope-gate`, `writer-route-gate`. Sanitized copies of the author's installed plugins; the dispatch plugin's own pytest suite ships with it. The selftests read `HERMES_PLUGINS_DIR` (CI points it at `plugins/`) and fall back to a local Hermes install at `~/.hermes/plugins`.
- `bin/hermes-lint-reply.py`, `bin/hermes-audit.py`, `bin/hermes-unit-guard-check.sh`, `bin/hermes-tmp-unit`: reply lint, audit, and systemd unit guard.
- `scripts/watchdog.py`: zero-token health checks driven by `watchdogs.json`. Supports `--dry-run`, which validates the config without running checks or sending alerts.
- `scripts/gate-health.py`, `scripts/check_delegate_failures.py`, `scripts/writer_route_report.py`: gate health and report helpers.
- `config.yaml`: the route config the gates read — real route names, model ids and prices. A model id named anywhere else is config drift, which is what the claim gate catches.
- `watchdogs.json`: real health checks for a Linux Hermes host: scheduler heartbeat, agent-log freshness, cron schedule sanity, `ssh.service`, the local web-chat port, free disk and available memory. Set `WATCHDOG_CONFIG` to point at another file. Alerts go to the chat in `WATCHDOG_ALERT_CHAT`, which is unset by default.
- `.github/workflows/ci.yml`: CI.

## Running the checks

CI runs exactly this set, on a clean checkout:

```
python3 -m pip install pyyaml
bin/hermes-claim-gate.test.sh
bin/hermes-skill-refs.test.sh
WATCHDOG_CONFIG=watchdogs.json python3 scripts/watchdog.py --dry-run
export HERMES_PLUGINS_DIR="$PWD/plugins"
bin/claim-gate-hook.test.sh
python3 bin/dispatch-gate-selftest.py
python3 bin/writer-gate-selftest.py
bash examples/run_example.sh
```

Run the same commands locally from the repo root. All seven exit 0 with no Hermes install and no Raspberry Pi.

### Plugin selftests

`bin/claim-gate-hook.test.sh`, `bin/dispatch-gate-selftest.py`, and `bin/writer-gate-selftest.py` each import one of the gate plugins from `HERMES_PLUGINS_DIR` (default: `~/.hermes/plugins`, the local Hermes install). CI points the variable at the in-repo `plugins/` tree, so the checks exercise the published plugin code.

### Gate-catch examples and real-world failure cases

`examples/run_example.sh` runs the two offline demos and the real-world failure suite, showing the guards refusing bad input:

- `examples/demo_gates.py malformed-dispatch` sends a placeholder `delegate_task` dispatch to the dispatch-envelope gate. The gate returns a block directive, and the case prints `CAUGHT`.
- `examples/demo_gates.py config-drift` builds a throwaway Hermes home (`config.yaml`, `SOUL.md`, `memories/`), confirms the claim gate is silent on it, then appends a model id that `config.yaml` does not carry to `SOUL.md`. The claim gate exits 1 with a `violation` line, and the case prints `CAUGHT`.
- `examples/real_world_cases.py` replays incidents modeled on real failure patterns — cases the guards exist for — against the real guards, and asserts each one is caught (with a healthy control passing). These are not synthetic marker strings; they are the shapes of failures that actually happened.

The script exits 0 only when every defect is caught. Its stdout is committed as `examples/malformed-dispatch.log`, `examples/config-drift.log` and `examples/real-world.log`. A fresh run reproduces all three logs byte for byte.

#### Real-world cases, actual caught output

Verbatim from a real run of `examples/real_world_cases.py` (committed as `examples/real-world.log`):

- **Unexpanded template marker in a dispatch goal.** The same failure that cost a real turn on 2026-11-21, when `delegate_task` refused a goal carrying `<UTC-ts>`. The marker lint catches the shape before the call, and passes the goal once the timestamp is substituted:

  ```
  CAUGHT  unexpanded '<UTC-ts>' in a dispatch goal
      MARKER goal: <UTC-ts> … Report on the <UTC-ts> deploy window and list the failing services 
      
      1 unexpanded marker(s). Substitute real values (a timestamp from `date -u +%Y%m%dT%H%MZ`), describe a format in words, or move a literal template placeholder into `context` — only `goal` is scanned.
  CAUGHT  expanded timestamp goal
      (silent, exit 0)
  ```

- **The logged refusal, replayed.** The verbatim errors.log line from that incident, fed back through `scripts/check_delegate_failures.py`: the detector surfaces it and names the marker.

  ```
  CAUGHT  real refusal line from 2026-11-21 errors.log
      1 new delegate_task refusal(s) or failure(s) in Hermes logs; latest 2026-11-21 16:51:54: Task 0 goal contains an unexpanded template marker ('<UTC-ts>'). Substitute the real value before calling delegate_task — subagents canno...
  ```

- **Process green, data stale.** A writer that keeps appending heartbeats so the file's mtime looks fresh while no real row has landed for hours; and the dead-man file that was never written:

  ```
  CAUGHT  stale data under fresh mtime
      chk_jsonl_fresh -> ok=False: newest row in events.jsonl is 3.0h old (limit 1h)
  CAUGHT  absent dead-man file
      heartbeat <tmp>/ticker_last_success missing — writer never ran
  ```

- **Skill body names a script deployed without its execute bit.** The file exists, so a plain existence check passes; the refs gate still flags it (`<tmp>` is the suite's throwaway home, scrubbed for reproducibility):

  ```
  CAUGHT  deployed without +x
      <tmp>/.hermes/skills/backup/SKILL.md:1: ~/bin/backup.sh -> <tmp>/bin/backup.sh: NOT_EXECUTABLE
      1 problem(s)
  ```

- **A reply that reads as machine-written** — banned phrasing, an em-dash run, a wall of prose — flagged by the reply lint before sending:

  ```
  CAUGHT  AI-flavored draft reply
      WARN  L1     paragraph runs 6 lines (convention: <= 4)
      WARN  L1     3 em dashes in prose (convention: <= 2)
      WARN  L1     3+ em dashes in one paragraph
      WARN  L1     banned phrase: 'delve'
      WARN  L1     banned phrase: "it's worth noting"
      WARN  L1     banned phrase: 'worth noting'
      WARN  L1     banned phrase: 'when it comes to'
      WARN  L1     banned phrase: 'landscape'
      WARN  L1     banned phrase: 'robust'
      WARN  L1     banned phrase: 'leverage'
      
      0 flag(s), 10 warning(s)  --  59 words
  ```

Each incident case also runs with a healthy control (expanded goal, fresh row, executable script, factual reply) that must stay silent, and the suite prints the real `self-test: 10 assertions passed` from the delegate-failure detector. Final line of a real run: `all real-world cases caught`.

## Verified results (clean checkout, this commit)

| Command | Exit | Result |
|---|---|---|
| `bin/hermes-claim-gate.test.sh` | 0 | 14 of 14 cases pass |
| `bin/hermes-skill-refs.test.sh` | 0 | all cases pass |
| `WATCHDOG_CONFIG=watchdogs.json python3 scripts/watchdog.py --dry-run` | 0 | 7 checks validated, 0 problems |
| `bin/claim-gate-hook.test.sh` | 0 | 19 of 19 cases pass |
| `python3 bin/dispatch-gate-selftest.py` | 0 | 15 of 15 checks pass |
| `python3 bin/writer-gate-selftest.py` | 0 | pass (silent on success, per design) |
| `bash examples/run_example.sh` | 0 | both demos catch their defect, and all 11 real-world cases are caught (with healthy controls) |

The plugin selftests were updated to the current gate semantics: the dispatch selftest's conformant fixture uses the tool `coder` route (a tool-free route with named artifacts and no context directive is refused by design), and the writer selftest pins a known `writer-gate.json` into a throwaway home for the relay-regression case, so the check no longer depends on the author's live policy file.

`hermes-claim-gate.test.sh` needs PyYAML on the interpreter. Without it, the test fails with `No module named 'yaml'`.

## Author's host (not this repo's output)

These figures describe the author's own Raspberry Pi, not what this repository produces or checks.

> **Author's host, not this repo's output**
> - 60+ zero-token health checks running on the Pi.
> - 6 enabled cron jobs on the Pi.

## What the layer does

The layer runs unattended on a Raspberry Pi as an operations wrapper around the Hermes Agent framework. The figures for that host are in the block above; this repository carries the machinery.

- Health monitoring: each check tests whether a service still does its job, not merely whether its process is running. A healthy check prints nothing and no check calls a model. The `watchdogs.json` here carries seven real checks for a Linux Hermes host; the author's own file is larger.
- Configuration: the gates read a route config. The `config.yaml` here carries the real route set the examples and selftests validate against.
- Dispatch gate: validates each dispatch against an envelope of task, facts, paths, output, check and ceiling. Its selftest covers a conformant task passing, placeholder and missing context being blocked, a proper goal without context being blocked, a mixed batch identifying the offending task, malformed arguments failing safely, and an unexpected hook error failing open. It runs in CI against `plugins/dispatch-envelope-gate`.
- Unit-approval guard: refuses to install a systemd unit whose exact bytes were not approved in chat, and quarantines unit files that have no valid record.

## Design notes

**Watchdogs.** Each check tests whether the service still does its job, not merely whether its process is running. A healthy check prints nothing. No check calls a model.

**Claim gate.** Model ids and route names belong in `config.yaml` only. The gate reports any prompt file that names one that `config.yaml` does not carry.

**Dispatch gate.** The hook validates each dispatch against an envelope of task, facts, paths, output, check, and ceiling.

## Sanitization

Personal paths, hosts, account identifiers and contact details were removed before publication. Placeholders such as `<USER>` mark the removed values. No secrets, credentials, tokens or API keys are in the tree or anywhere in the history. An early commit carried the author's Telegram chat id in an example cron config; the repository was republished from a sanitized tree, so the history contains exactly one commit and no occurrence of that id.

## License

MIT. See `LICENSE`.
