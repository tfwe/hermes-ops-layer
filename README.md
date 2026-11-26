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
- `config.yaml`: placeholder-only example of the route config the gates read.
- `watchdogs.json`: example health checks. Set `WATCHDOG_CONFIG` to point at another file. Alerts go to the chat in `WATCHDOG_ALERT_CHAT`, which is unset by default.
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
```

Run the same commands locally from the repo root. All six exit 0 with no Hermes install and no Raspberry Pi.

### Plugin selftests

`bin/claim-gate-hook.test.sh`, `bin/dispatch-gate-selftest.py`, and `bin/writer-gate-selftest.py` each import one of the gate plugins from `HERMES_PLUGINS_DIR` (default: `~/.hermes/plugins`, the local Hermes install). CI points the variable at the in-repo `plugins/` tree, so the checks exercise the published plugin code.

### Gate-catch example

`examples/run_example.sh` runs two offline demos and shows the gates refusing bad input:

- `examples/demo_gates.py malformed-dispatch` sends a placeholder `delegate_task` dispatch to the dispatch-envelope gate. The gate returns a block directive, and the case prints `CAUGHT`.
- `examples/demo_gates.py config-drift` builds a throwaway Hermes home (`config.yaml`, `SOUL.md`, `memories/`), confirms the claim gate is silent on it, then appends a model id that `config.yaml` does not carry to `SOUL.md`. The claim gate exits 1 with a `violation` line, and the case prints `CAUGHT`.

The script exits 0 only when both defects are caught. Its stdout is committed as `examples/malformed-dispatch.log` and `examples/config-drift.log`. A fresh run reproduces both logs byte for byte.

## Verified results (clean checkout, this commit)

| Command | Exit | Result |
|---|---|---|
| `bin/hermes-claim-gate.test.sh` | 0 | 14 of 14 cases pass |
| `bin/hermes-skill-refs.test.sh` | 0 | all cases pass |
| `WATCHDOG_CONFIG=watchdogs.json python3 scripts/watchdog.py --dry-run` | 0 | 5 checks validated, 0 problems |
| `bin/claim-gate-hook.test.sh` | 0 | 19 of 19 cases pass |
| `python3 bin/dispatch-gate-selftest.py` | 0 | 15 of 15 checks pass |
| `python3 bin/writer-gate-selftest.py` | 0 | pass (silent on success, per design) |

The plugin selftests were updated to the current gate semantics: the dispatch selftest's conformant fixture uses the tool `coder` route (a tool-free route with named artifacts and no context directive is refused by design), and the writer selftest pins a known `writer-gate.json` into a throwaway home for the relay-regression case, so the check no longer depends on the author's live policy file.

`hermes-claim-gate.test.sh` needs PyYAML on the interpreter. Without it, the test fails with `No module named 'yaml'`.

## Author's host (not this repo's output)

These figures describe the author's own Raspberry Pi, not what this repository produces or checks.

> **Author's host, not this repo's output**
> - 60+ zero-token health checks running on the Pi.
> - 6 enabled cron jobs on the Pi.

## What the layer does

The layer runs unattended on a Raspberry Pi as an operations wrapper around the Hermes Agent framework. The figures for that host are in the block above; this repository carries the machinery.

- Health monitoring: each check tests whether a service still does its job, not merely whether its process is running. A healthy check prints nothing and no check calls a model. The `watchdogs.json` here is a five-check example with placeholder targets; the author's own file is larger.
- Configuration: the gates read a route config. The `config.yaml` here is a placeholder carrying no real routes, models or paths.
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
