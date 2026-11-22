# Fleet Copilot

An agentic copilot for IT administrators. It answers fleet-health and compliance questions from device telemetry,
detects 30-day trends, and proposes operational actions - with every answer traceable to telemetry snapshots and
every state-changing action gated by deterministic evidence checks and an explicit human approval.

Built for Rayda's take-home assessment: Python, LangGraph, SQLite, an OpenRouter-hosted model, and a CLI.

## For reviewers: start here

**See it without running anything**

| I want to see... | Open |
|---|---|
| a real session: grounded answers, a zero result, trends, an approved upgrade, a rejected replacement, a blocked cross-company lookup | [docs/evidence/demo-transcript.md](docs/evidence/demo-transcript.md) |
| the evaluation results (46 cases x 3 repeats, real model, independent oracle) | [docs/evidence/eval-live-report-deepseek.md](docs/evidence/eval-live-report-deepseek.md) |
| the approval flow proven end to end (0 outbox rows before approval, exactly 1 after, still 1 after replaying the approval) | [docs/evidence/slice-live-deepseek.md](docs/evidence/slice-live-deepseek.md) |

**Run the tests (no API key, about a minute)**

```bash
uv sync
uv run pytest                                   # 150+ deterministic tests; a scripted model drives the real graph
uv run python -m evals.smoke --fake             # offline end-to-end rehearsal of the approval flow
```

**Run the CLI (needs an OpenRouter key in `.env`; copy `.env.example`)**

```bash
cp .env.example .env     # then paste the key after OPENROUTER_API_KEY=   (.env is gitignored)
uv run fleet-copilot --company acme-001 --admin admin-acme-1
```

> **API key for reviewers:** a temporary, spend-capped OpenRouter key is sent to you separately by the candidate. It is
> deliberately not in this repository or its history. Put it in your local `.env` only; the candidate will revoke it
> after the review. You can also use any OpenRouter key of your own. The tests and `python -m evals.smoke --fake` need
> no key at all.

Good first questions: *"Show me laptops failing high-severity compliance checks."* (a grounded zero),
*"Which Acme batteries are approaching end of life?"* then `/trace`, and
*"Order a RAM upgrade to 32GB for M4XVHUV1MEPZ."* (approval prompt; then `/approve <id>` again to see the replay is a no-op).
The rest of this README covers the architecture, tool catalog, grounding strategy, guardrails and trade-offs.

- [Setup](#setup) | [Run the agent](#running-the-agent) | [Run the evaluations](#running-the-evaluations)
- [Architecture](#architecture) | [Tool catalog](#tool-catalog) | [Grounding strategy](#grounding-strategy)
- [Guardrails](#guardrails) | [Data rules](#data-rules-and-thresholds) | [Design decisions and trade-offs](#key-design-decisions-and-trade-offs)
- [Evidence and results](#evidence-and-results) | [Limitations](#known-limitations)

## Setup

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), and an OpenRouter API key (only for running the real
model; the whole test suite runs offline without one).

```bash
# Windows: winget install --id astral-sh.uv -e      macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync                      # installs Python 3.12 if needed, creates .venv, installs dependencies
cp .env.example .env         # then put your key in .env (it is gitignored)
uv run pytest                # offline, deterministic, no API key needed
```

Troubleshooting: after installing `uv`, open a **new terminal** (or restart VS Code) so `uv` is on your PATH. If another
virtualenv is already activated, `uv` prints a harmless `VIRTUAL_ENV ... does not match` warning and uses the project's
own `.venv`. Without a key the CLI exits with a one-line message (the offline tests and `python -m evals.smoke --fake`
need none).

`.env` settings (all optional except the key):

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | - | required to run the real model |
| `FLEET_MODEL` | `deepseek/deepseek-v4-pro` | OpenRouter model slug (must support tool calling; `deepseek/deepseek-v4-flash` is ~10x cheaper) |
| `FLEET_PROVIDER` | empty | pin one upstream provider (disables fallbacks) for reproducibility |
| `FLEET_TEMPERATURE` / `FLEET_SEED` | `0` / `7` | sampling settings |

The dataset is `data/device-telemetry-dataset.ndjson` (750 snapshots, 25 devices, 3 companies, 30 days). It is loaded
into SQLite (`var/fleet.db`) on first run.

## Running the agent

```bash
uv run fleet-copilot --company acme-001 --admin admin-acme-1
```

The company comes from the **application session**, not from anything the model or the user types. Seeded admins
(`data/admins.yaml`): `admin-<company>-1` is an *approver*, `viewer-<company>-1` can ask questions and propose but
cannot approve. Companies: `acme-001`, `globex-002`, `initech-003`. Using an admin with the wrong company is denied.

Ask in natural language. Example session:

```
fleet> Which Acme batteries are approaching end of life?
fleet> Device M4XVHUV1MEPZ is constantly short of memory. Order a RAM upgrade to 32GB for it.
   ... shows the proposal, its evidence and the generated text, then:
Approve P-1a2b3c4d5e6f? [y/N]
```

| Command | What it does |
|---|---|
| `<question>` | ask the copilot; prints a short trace, the validated answer with evidence, and any proposals |
| `/trace` | full plan, tool calls, coverage check and validator result of the last question |
| `/evidence <snapshot_id>` | the raw telemetry snapshot behind any citation (company-scoped; foreign ids look missing) |
| `/proposals`, `/outbox` | proposals and their status; executed actions in the simulated outbox |
| `/approve <id>`, `/reject <id>` | decide a pending proposal later; repeating an approval is a safe no-op |
| `/audit [n]` | the last n audit records for your company |

## Running the evaluations

Two layers, reported separately.

**1. Offline suite (deterministic, no API key).** A scripted fake model drives the real graph, including the approval
interrupt and resume.

```bash
uv run pytest
```

**2. Real-model suite.** 46 cases against the configured model; expected answers come from an independent oracle
(`evals/oracle.py`) computed from the raw NDJSON, not from the system under test.

```bash
uv run python -m evals.run                     # all cases (4 workers)
uv run python -m evals.run --repeats 3         # measure run-to-run stability
uv run python -m evals.run --category trends   # or: --ids qa-low-disk-acme,act-ticket-approved
uv run python -m evals.run --publish           # also copy the report to docs/evidence/
uv run python -m evals.merge a.json b.json     # combine reruns: latest completed run per case wins; errors stay "unmeasured"
uv run python -m evals.smoke                   # one end-to-end flow with the real model (regression check)
uv run python -m evals.demo                    # record an admin session transcript with the real model
```

Reports go to `evals/reports/live-<timestamp>.{md,json}` and always record the model, provider, temperature, seed,
git commit and dataset hash. See [docs/evidence/](docs/evidence/) for committed sample runs.

## Architecture

```
CLI (Session = company_id + admin_id, created by the app)
  |
  v
LangGraph StateGraph  (SQLite checkpointer; thread is bound to its company)
  plan -> agent <-> tools -> coverage -> validate -> respond -> approval(interrupt) -> END
           ^__________|__________|_________|
           (a failed coverage or validation check sends the agent back once, with the diff)
```

| Node | Kind | Responsibility |
|---|---|---|
| `plan` | LLM | records intent, tool steps and `evidence_needed` (what must be fetched to answer / justify an action) |
| `agent` | LLM | one model call with the tools bound; picks tools or calls `final_answer` |
| `tools` | code | dispatches calls through a **session-bound** toolbox; stores structured `ToolResult`s and proposals |
| `coverage` | code | every `evidence_needed` item must be backed by a tool result, else the agent must fetch it or `revise_plan` (once) |
| `validate` | code | checks each claim in the answer against this turn's tool results (devices, counts, snapshot ids) |
| `respond` | code | renders devices, counts, citations and the "what was checked" block **from tool data**, not from model text |
| `approval` | code | `interrupt()` pauses for a human; the decision is applied by application code (`actions.py`) |

The planning step is tied to execution: the plan's `evidence_needed` is enforced by the coverage node, and the plan,
any revision and the coverage result are kept in the trace (`/trace`) and state. The loop is bounded
(`MAX_AGENT_ITERATIONS = 8`), and each retry budget is one.

Source map: `src/fleet_copilot/` - `repository.py` (company-scoped queries), `analytics.py` (trend detectors),
`policy.py` (evidence rules), `notes.py` (generated text), `actions.py` (proposal/approval/execution), `validation.py`,
`coverage.py`, `tools/`, `graph/build.py`, `cli.py`; `data/hardware_catalog.yaml`; `tests/`; `evals/`.

## Tool catalog

**Read tools** (all return a structured `ToolResult`: result id, company, filters, window, as-of time, every device
checked, matched rows with snapshot ids, and excluded devices with reasons):

| Tool | Purpose |
|---|---|
| `query_devices(filters)` | current state (each device's latest snapshot). Typed filters: form factor, OS family + `os_version_lt`, disk free %, memory used %, failing check + severity (matched on the **same** check), software, employee, model |
| `fleet_overview()` | counts by model, OS, form factor and failing check |
| `get_device(device_id)` | one device's details and 30-day history summary |
| `analyze_trends(kind, device_id?)` | `battery_health`, `memory_pressure`, `storage`, `compliance_drift`: per device the finding, measurements, series, an explanation, and the supporting snapshot ids |

**Action tools** (propose only - none executes anything). They accept identifiers and evidence snapshot ids and
**no free text**; the application writes every note, reason and message.

| Tool | Supported when (checked by `policy.py`) | Refused, for example |
|---|---|---|
| `open_remediation_ticket(device_id, check_id, evidence)` | the device's latest, non-stale snapshot is cited and shows that check failing | check passes now; cites an older snapshot; another device's or company's evidence |
| `create_upgrade_order(device_id, component, spec, evidence)` | sustained pressure on that component **and** the catalog says it is upgradeable on that model **and** `spec` is a catalog option larger than today's **and** >=3 cited snapshots support the finding | soldered RAM (Apple silicon, X1 Carbon); spec not offered; pressure only intermittent; evidence does not show the condition |
| `flag_device_for_replacement(device_id, reason_code, evidence)` | `battery_eol` with an end-of-life finding; or `ram`/`storage_capacity_unupgradeable` with sustained pressure on a model whose component cannot be upgraded | healthy or absent battery; component *is* upgradeable (propose an upgrade instead) |
| `notify_employee(employee_id, device_id, topic, evidence)` | employee is in this company, is assigned that device, and the latest snapshot shows the topic's condition | employee from another company; device not theirs; condition does not hold |

Plus `revise_plan` (once) and `final_answer`. There is no execute tool: execution is reachable only from the approval
interrupt, in application code.

## Grounding strategy

1. **Tools return evidence, not prose.** Each row carries a deterministic snapshot id `device_id@collected_at`, and
   every result states what was checked (company, filters, window, as-of, devices considered, devices excluded and
   why). A zero-result answer therefore shows exactly what was searched.
2. **The model's answer is a set of claims** `{result_id, device_ids, count, snapshot_ids}`. `validate` checks each
   claim against this turn's tool results: result exists and belongs to the company; device set and count match (a
   subset must be declared); cited snapshots exist in that result; no device is mentioned in the summary without a
   claim. One retry with the diff; if it still fails, the model's text is **withheld** and the raw tool evidence is
   shown with a warning.
3. **Code renders the evidence** (device tables, counts, citations, "matched N of M checked", exclusions, trend
   findings), so model prose cannot alter what the admin sees as proof. `/evidence <id>` shows the raw snapshot.
4. **All trend findings are deterministic** - finding text, measurements and explanations are templates filled from the
   data; the explanations state what the telemetry shows, never a cause it does not record.
5. **Everything the app writes into a proposal is generated from verified facts** (see Guardrails).

## Guardrails

| Requirement | How it is enforced | Where it is tested |
|---|---|---|
| Tenant isolation | company comes from the session; every repository query takes it; tool schemas have no `company_id` and reject unknown fields; foreign ids are indistinguishable from missing ones; checkpoint threads are bound to their company | `test_data_rules`, `test_analytics`, `test_actions`, `test_graph_flow` (thread hijack), eval `isolation` |
| No cross-company access | as above, plus evidence/employee/device lookups scoped; `/evidence` audited when not found | same |
| Human approval for state changes | LLM has no execute tool; approval is bound to proposal id + company + payload hash + reviewer's *stored* approver role; viewers and forged sessions are denied | `test_actions`, `test_graph_flow`, eval `approval` |
| Safe to retry | execution is one `BEGIN IMMEDIATE` transaction: re-check policy, compare-and-set status, insert outbox row (`UNIQUE(proposal_id)`), write audit row; replays return the recorded outcome and write nothing; a rejected proposal is final and cannot be revived, but asking again creates a *new* proposal (`P-...-r2`) with its own approval and audit trail, while an identical pending/approved/executed one is returned instead of duplicated; failure rolls everything back | `test_actions` (replay, atomicity, stale evidence at execution, re-proposal after rejection) and the live smoke run |
| Audit logs | append-only `audit_log`: questions, proposals (created/refused/duplicate), decisions (approved/rejected/denied/replayed), executions, scope denials | every flow test asserts the events and their order |
| Refuse without sufficient evidence | `policy.py` runs at proposal time **and again inside the execution transaction**; evidence must exist, belong to the device, be fresh, and show the specific condition | `test_policy_matrix` (supported + refused per action), eval `actions_refused` |
| No unsupported claims in proposals | the model cannot supply prose; notes, reasons, messages and justifications are templates over verified facts | `test_notes` (regression for the invented "per company policy" note), eval implied check |
| Act only on what was asked | **target guard:** if the admin names a device or employee id, an action proposal must target those ids; a proposal that substitutes another target is refused and audited (`target_mismatch`), and the model is told to report the conflict instead. A prompt rule also says to mention alternatives rather than propose them unasked | `test_target_guard` (reproduces the live failure), eval `refuse-notify-wrong-employee`, `refuse-upgrade-soldered-ram` |

**Why prose is generated by the app.** In the first live run the model wrote a ticket note ending "...per company
policy". Nothing in the telemetry mentions a policy, so an unsupported claim was reaching an approver and a downstream
system. The fix is structural, not a prompt: action tools no longer accept free text, so it cannot recur. The ticket
note is, for example: *"Review the screen-lock configuration on 1LYSSFD074BB. The latest telemetry snapshot
(2026-06-12T09:47:00Z) reports a failed screen_lock check."*

## Data rules and thresholds

All thresholds live in `src/fleet_copilot/config.py`.

- **"Now"** is the dataset's last timestamp (`2026-06-12T11:00:00Z`), never the wall clock.
- **Stale:** a device whose latest snapshot is more than 48 h older than that is *excluded with a reason* from current
  state and trend results, and cannot support an action. Evidence older than 7 days cannot support an action.
- **Missing data means unknown, never pass/fail.** Devices with no battery in any snapshot (Mac minis) are
  "not applicable"; trends need at least 10 readings spanning 14 days or the device is listed as insufficient data.
- **Compliance:** severity and failure are matched on the same check. The only high-severity check
  (`disk_encryption`) never fails in this dataset, so "laptops failing high-severity checks" is a grounded **zero**.
- **OS versions:** compared only within an OS family (`os_version_lt` requires `os_family`); "older than macOS 15"
  excludes Windows devices and lists them as excluded.
- **Form factor** is derived from the model name (MacBook/ThinkPad/XPS laptop; Mac mini desktop).
- **Trends:** low disk < 10 % free; *sustained* RAM pressure = > 85 % used in >= 50 % of snapshots over >= 14 days
  (25-50 % is reported as *intermittent* and is not actionable); *consistently low* storage = latest < 10 % and >= 50 %
  of snapshots below it; battery end-of-life = "Service Recommended" now, or >= 3 % capacity loss with > 900 cycles;
  compliance drift = a check passing in the first snapshot and failing in the latest.

## Key design decisions and trade-offs

- **Deterministic analytics, LLM for orchestration.** Counting, trends and policy are Python; the model plans, picks
  tools and writes a short summary. Cost: the model cannot invent novel analyses outside the four trend kinds.
- **Claim validation instead of an LLM judge.** Cheap, exact, and not subject to the same failure modes as the thing it
  checks. Cost: it verifies *what the answer claims about tool results*, not the wisdom of the interpretation.
- **Application-written text in proposals.** Safest against unsupported claims, at the cost of fixed, plain wording
  (ticket notes are not tailored to the situation).
- **Plan is enforced, not decorative.** Missing evidence sends the agent back once; if still missing the answer is
  shown with an explicit "planned evidence was not gathered" note rather than silently accepted. The plan call is
  *not* forced with `tool_choice` because the configured model rejects forced tool choice (found by a live probe); a
  skipped plan is flagged and the turn continues without coverage enforcement.
- **Evidence ids are listed newest-first** so that "cite the first few" cites fresh evidence, matching the freshness rule.
- **SQLite, one process.** Transactional guarantees come from `BEGIN IMMEDIATE`, compare-and-set and `UNIQUE`
  constraints - simple and demonstrable. A multi-writer service would use Postgres and a real outbox relay.
- **Simulated outbox.** The brief leaves integrations unspecified, so approved actions write to a local outbox that
  stands in for the ticketing/ordering/notification systems.
- **Hardware catalog is an assumption.** `data/hardware_catalog.yaml` encodes which components are upgradeable per model
  from general spec knowledge; it is not telemetry. Validate it against real asset records before relying on it.
- **Seeded admins, no authentication.** Roles are enforced and re-read from the database at decision time, but there is
  no login; a deployment would sit behind the platform's identity provider.
- **Determinism is "reasonable", not absolute.** Temperature 0, a fixed seed and optional provider pinning reduce
  variance, and everything except the model's choices is deterministic. Run `--repeats` to measure the remainder.

## Evidence and results

Committed artifacts in [docs/evidence/](docs/evidence/):

| File | What it is |
|---|---|
| `eval-live-report-deepseek.md` / `.json` | full 46-case real-model evaluation, 3 repeats each (138 runs), on the default model `deepseek/deepseek-v4-pro` |
| `demo-transcript.md` | a recorded admin session (DeepSeek) through the real CLI code path: grounded answers, a blocked cross-company `/evidence`, a zero result, a trend + `/trace`, an approved upgrade, a rejected replacement, "not found" for another company's device |
| `slice-live-deepseek.md` | end-to-end flow with the real model, CLI transcript and audit records: grounded answer, supported ticket, pause for approval, **0 outbox rows before approval, exactly 1 after, still 1 after replaying the approval twice** (13 checks) |
| `eval-live-report.md` / `.json`, `slice-live.md` | the same evaluation / flow on `anthropic/claude-sonnet-5.5` (comparison; the evaluation report is merged from three runs, see below) |

### Real-model evaluation: `deepseek/deepseek-v4-pro` (temperature 0, seed 7)

**138 of 138 runs passed: all 46 cases, each repeated 3 times, and every case passed all 3 times** (no infrastructure
errors, no unstable case), on the final code (see the hash note below). Device-set precision/recall 100 %/100 %, and
every answer passed claim validation. A plan was recorded in 137 of 138 runs: in one run the model skipped the planning
call (it is offered, not forced, because the configured model rejects forced tool choice); the system flagged it,
continued without coverage enforcement, and the answer was still correct.

| Category | Passed | What it covers |
|---|---|---|
| qa | 11/11 | grounded current-state questions, incl. the brief's examples; device sets compared with the independent oracle |
| zero_result | 4/4 | grounded "none" answers (e.g. laptops failing high-severity checks) that show what was checked |
| trends | 7/7 | battery end-of-life, sustained RAM pressure (the intermittent device correctly *not* reported), low storage, compliance drift |
| isolation | 6/6 | cross-company requests, prompt injection, a `company_id` argument, actions on another company's device/employee |
| approval | 2/2 | "execute immediately" and "mark it approved yourself" never execute (outbox stays empty) |
| actions_supported | 8/8 | all four action types; approved and rejected flows; one outbox row per approval |
| actions_refused | 8/8 | refusals per action type, with an auto-approver that would execute anything that slipped through |

How to read this honestly:

- **This is the second full run on this model, not the first.** The first run passed 41/46 and was useful. Three
  failures were the *scorer* being too naive: the model gave the right devices and also cited a `fleet_overview` result
  for context ("the fleet has 10 devices"), which the scorer counted as 8 extra matches (fixed; regression test in
  `tests/test_oracle.py`). Two were **real behaviour problems** that the suite caught and are now fixed in the system:
  asked for a RAM upgrade it could not do, the model also proposed a replacement flag nobody asked for; asked to notify
  `emp-acme-1000`, it found the device belonged to someone else and quietly proposed notifying **them**. Both were
  policy-valid and disclosed, but not what the admin asked. The fix is the *target guard* (see Guardrails) plus a prompt
  rule, with tests. The tests were not loosened: the same cases now pass.
- **Stability was measured, not assumed:** three repeats of every case (temperature 0, fixed seed) gave identical
  outcomes. That is evidence of stability on this model and dataset, not a guarantee: the provider can change the model
  behind the `deepseek-v4-pro` alias, so pin a dated snapshot with `FLEET_MODEL` when strict reproducibility matters.
- The cases were written by the same team that built the system, and the first run's two real failures were found only
  because the suite compared against an independent oracle. Treat 138/138 as a strong regression signal, not proof that
  no failure mode exists; new questions from real admins are the best next test.
- Results are for one model on this dataset; the earlier Sonnet run (45/46, one case unmeasured because of a provider
  credit limit, no `src/` changes between its runs, merged by `python -m evals.merge`) predates the target guard.
- **This is the second 3x run on the final code.** The first scored 137/138: the one miss was the *scorer* counting a
  context claim (the model also cited an unfiltered 10-device result, "out of 10 devices in the fleet") as devices that
  matched, although the answer named exactly the right 3. The scorer now treats claims on unfiltered results as
  population context, not matches (regression tests in `tests/test_oracle.py` also confirm a sole wrong claim still
  fails), and the full 3x was rerun rather than patched together.
- **Hash note:** the evidence reports record the commit they ran on. The DeepSeek report records `c85bb27`: it ran on
  that commit plus the fixes committed right after it (evidence-header wording, re-proposal after rejection, the
  prompt rule on numbers, the scorer rule). The Sonnet report records `cf40ea4`; git history was rewritten once, before
  anything was pushed, only to edit commit-message trailers, every tree is byte-identical, and that commit is now
  `73adedc`. The reports are left unedited as recorded output.
- Earlier live runs also found: an unsupported "per company policy" ticket note written by the model (see Guardrails),
  and a provider that rejects forced `tool_choice` for the planning call.

## Known limitations

- Each question is its own conversation thread; there is no multi-turn memory between questions.
- The real-model suite measures one model on one 25-device dataset with cases written alongside the system; repeats show
  stability, not coverage of questions nobody thought to ask.
- Trend thresholds are judgement calls documented above; a borderline device (e.g. 43 % of snapshots over the RAM
  threshold) is deliberately reported as *intermittent*, not hidden.
- Only the proposals' supporting facts are verified; the wisdom of acting on them is the approver's call.
- **The one-paragraph summary at the top of an answer is model-written and only partly verified.** Device lists, counts,
  citations and the evidence tables below it are machine-checked or rendered from tool data; other numbers or wording in
  the summary are not. A tester on a Mac caught the summary saying "all 10 laptops were checked" when the evidence said
  10 *devices* (9 laptops, 1 desktop). The prompt now tells the model to state only numbers that appear in tool
  results, but that is a prompt rule, not enforcement. A reliable check needs the summary to be generated from the
  claims instead of free text, which was out of scope; read the evidence block, not just the summary, when it matters.
