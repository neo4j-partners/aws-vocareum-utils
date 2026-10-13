# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Response style

- **Condensed:** No preamble, no throat clearing. Only what is necessary.
- **Bullets:** Use `**term:** description` form.
- **Small words, short sentences, short paragraphs.** If a big word is unavoidable, define it right after.
- **Report shape:** what you did, whether it worked, what to do now.
- **Decisions:** 2 options max, the context needed to pick fast, and which one you would take.
- **Exactness:** Keep paths and commands verbatim.

## What this repository is

CloudFormation and setup assets for a Neo4j AWS workshop that runs on Vocareum. Vocareum gives each student their own AWS account and runs `lab.template` in it at Start Lab. The Python package `vocareum_tools` exists to get that template up there, to recover why it failed, and to measure what the student account actually permits.

Nothing here is a web service. Everything is a command-line tool plus a body of measured evidence.

## Commands

```bash
uv sync --group dev            # resolve the locked dev environment
uv run pytest                  # focused management suite
uv run pytest tests/management/test_probe.py::test_name   # one test
./scripts/validate_all.py      # complete offline CI and pre-release gate
uv run ruff check .
uv run ruff format --check .
cfn-lint courses/neo4j-fundamentals/lab.template  # before any upload
```

Operational tools. Each Vocareum tool is a `uv run vocareum-*` entry point; `scripts/` holds only the stateful runners and the release and validation scripts:

```bash
uv run vocareum-upload --dry-run                       # what would be sent
uv run vocareum-upload --course neo4j-fundamentals
uv run vocareum-diagnose --no-start                    # why a stack said CREATE_FAILED
uv run vocareum-probe --self-test                      # offline classifier check, no AWS
uv run vocareum-probe --probe bedrock:ListFoundationModels
./scripts/run_phase3_agentcore.py --block memory
./scripts/sync_workshop_lab.py --check                 # is the published helper package stale?
uv run vocareum-expect --no-start                      # did the lab come up, as an exit code
uv run vocareum-refresh --dry-run                      # recover rotated account and stack pins
uv run vocareum-expect --no-start --course graphrag    # the same, for one course
```

**Starting a lab consumes one launch against the budget, and some answers cost exactly that.** A live session is the only instrument that measures what the account permits, so start one when the question has no offline answer instead of treating it as a last resort. `--no-start` is the right default for reusing a session that is already open, which is most runs.

**AWS CLI:** use `rtk proxy aws ...`, per the global RTK instruction.

## Configuration

`.env` in the repo root, layered under real environment variables, which always win. `Environment.get` in `src/vocareum_tools/config.py` implements that precedence. `.env.example` documents every variable and why it exists. `.env` and `support/` are git-ignored because they carry real account, organization, and SCP identifiers; committed docs write those as `${PLACEHOLDER}`.

`LAB_AWS_ACCOUNT_ID` expires. Vocareum rotates a pool of at least three accounts and ending a session moves the allocation. `${LAB_AWS_ACCOUNT_POOL_A}` / `_B` / `_C` name fixed members for finished evidence rows; `${LAB_AWS_ACCOUNT_ID}` names whichever is live. Never write the live one into a finished row.

## Architecture

Layered package under `src/vocareum_tools/`, packaged console entry points, and stateful phase runners in `scripts/` that drive the same engine.

- **`config.py`** loads env plus dotenv. `VOCAREUM_PART_VARS` vs `VOCAREUM_SESSION_VARS` mirrors the endpoint pair below: upload needs only the part, since a workspace belongs to a part rather than to a student. It also owns course selection: `Environment.from_file(path, course)` layers `courses/<name>.env` over the base file, and `add_course_argument` puts the same `--course NAME` on every tool. Layered rather than substituted, because the API token and the region are the same for every course and a substituted file would force a rotated token into every course file.
- **`endpoints.py`** holds every Vocareum URL shape. `PartEndpoints` and `SessionEndpoints`. Centralized because a wrong path returns a bare HTTP 400 naming nothing.
- **`client.py`** is the HTTP and transaction layer: `request_json`, `poll_transaction`, `wait_for_payload`. Vocareum writes are asynchronous, answering `202` with a `transactionid`, so nearly everything polls.
- **`security.py`** redacts. Every response body printed anywhere goes through `redact_text` / `redact_payload` first. Session payloads carry temp AWS credentials, a console sign-in token, and an unencrypted SSH private key. Access key IDs stay visible on purpose: they identify a session without authorizing one.
- **`aws.py`** parses the free-text of a denial message. AWS names the refused action only in the body, never in a structured field.
- **`probe.py`** is the measurement engine: the `Verdict` enum, `classify`, `TRACKER_STATUS`, `NEXT_ACTION`, and the guards.
- **`selftest.py`** holds the classifier case tables, shipped inside the package so `--self-test` runs in a lab where pytest is not installed. The pytest suite imports the same tables.
- **`phase.py`** is the shared scaffolding for `scripts/run_phase*.py`: `Recorder`, `wait_until`, `wait_until_gone`, `teardown_guaranteed`. It exists because these were copy-pasted per runner and drifted into three incompatible `Recorder` classes, one of which had lost `call_in_teardown`.
- **`expect.py`** is the ship gate. `expected.json` states what a finished lab holds, `Gate` looks every entry up read-only, and the exit code is the answer. Three things it does not share with its neighbors: it hangs off `probe`'s guards rather than `diagnose`, because `diagnose` enforces no account guard and a gate that passes against the wrong account is worse than no gate; it makes a wrong region fatal, for the same reason; and it carries its own `READY_STACK_STATUSES`, because `diagnose.SUCCESS_STACK_STATUSES` counts `DELETE_COMPLETE`, which is a success for a teardown and a failure for a gate. `Presence` keeps absent and unreadable apart, and a refused read is classified with `probe.classify` so a gate failure that is really a missing grant says so.
- **`refresh.py`** repairs the account and stack pins after a pool rotation. It reads an open session without starting or extending it, refuses unless exactly one non-deleting stack exists, and atomically rewrites both values in the one env file that supplied them.
- **`upload.py`, `diagnose.py`** are the two large user-facing commands.
- **`sweep.py`** deletes this workshop's leftovers out of a student account as an instructor. It does not define what counts as a leftover: that rule is `workshop_lab.selection`, which the notebook's own step 14 teardown imports too. See below.

### The one rule two programs share

`vocareum_tools.sweep` and `workshop_lab.teardown` both delete this workshop's resources, under different principals, with different reporting, and neither can become the other. What they must agree on is which resource is this workshop's, and that rule is **`src/workshop_lab/selection.py`** alone: the tag, the three tag shapes AWS answers a tag read with, the tag-or-name union with `require_prefix` inverting it for IAM, the AgentCore log path, the S3 codes that mean a bucket is gone, and the CloudFormation physical-id keys.

**`vocareum_tools` imports `workshop_lab`, never the reverse.** `workshop_lab` is what a student notebook installs from the public workshop repo; `aws-vocareum` is private and a notebook cannot install from it at all, so an import the other way resolves on this machine and fails in every lab. `pyproject.toml` builds both packages from the one wheel, which is what makes the allowed direction resolve.

Scope is not shared and should not be. `sweep.NAME_PREFIXES` is the union across every generation of names that could still be sitting in a pooled account; the notebook knows the one prefix its own run used. `tests/management/test_sweep.py` asserts the first stays a superset of the second.

### The three guards

`probe.py` refuses to report before all three pass, because a confident report about the wrong AWS account is worse than no report:

1. **Credential guard.** All three credential fields must be non-empty. A terminated Vocareum session returns empty strings, boto3 reads those as absent, falls through its provider chain, and reports on whatever ambient credentials the operator holds. That has already produced one false answer.
2. **Account guard.** `sts:GetCallerIdentity` must equal `LAB_AWS_ACCOUNT_ID`.
3. **Region guard.** Must be `us-east-1`. AgentCore and the Nova multimodal embedding model exist nowhere else.

Anything that touches a live session must keep these. `scripts/run_phase*.py` import them from `probe.py` rather than reimplementing.

### Verdicts

A probe names an **IAM action**, not a boto3 method (`bedrock:ListFoundationModels`, not `list_foundation_models`), so results line up with the permission tracker without translation. Outcomes map to one `Verdict`, and `TRACKER_STATUS` maps each verdict to a tracker Status cell.

The distinction the whole vocabulary exists for: an organization SCP/RCP deny, which nothing inside the account can lift, versus an ordinary IAM shortfall, which can be fixed in the account. Both arrive as `AccessDenied`, and only the message body tells them apart. A verdict carrying no authorization signal maps to `Untested`, never to a guess.

The same vocabulary is duplicated verbatim in `lab-probe.template`'s inline Lambda source so a result means the same thing whichever principal produced it. `tests/management/test_template_drift.py` extracts that `ZipFile` literal, executes it, and drives it with the shared case tables. **If you change `classify` or the `Verdict` enum, change the template copy too or that test fails.**

### Phase runners

`vocareum-probe` takes a flat list of probes with static arguments. That cannot express state, so anything stateful lives in `scripts/run_phase*.py`: `CreateMemory` returns the ID `GetMemory` consumes, `PutItem` needs an `ACTIVE` table, `DeleteTable` is only measurable last. Guards and classifier stay shared; only the ordering is per-phase.

Two rules these runners encode:

- **A mutating call that returns is not a mutating call that finished.** Poll with `wait_until` / `wait_until_gone`. Skipping this claimed a clean account five separate times while resources were still in it.
- **Teardown deletes are measurements.** Use `Recorder.call_in_teardown`, which records the verdict and then re-raises, so `run_cleanups` still reports what was left behind. Plain `Recorder.call` absorbs the error and would report a clean account.

Leaked resources must be swept before the session ends. The old account becomes unreachable after the pool rotates.

## Conventions that are load-bearing

- **Every tool takes `--course NAME`, and a named course with no file is an error.** Falling back to `.env` would run against whatever the base file said and report success for a course it never touched. `courses/README.md` states what belongs in which file. Course definitions, inspection, local scaffolding, release, and rollback are the management core. Cloning, assignment publishing, and enrollment are deferred and are not exposed by `vocareum-course-setup`.
- **A course may keep its assets in another repository, and one env line is the only thing that says so.** `COURSE_ASSET_DIR` in `courses/<name>.env` names that repository's Vocareum asset directory, and `course.course_directory` is the single function that reads it, so `definition_path`, `manifest_path`, and `template_path` all follow together. `graphrag` points at `neo4j-hotel-booking-agent-workshop/environment/vocareum`; `neo4j-fundamentals` sets no pointer and stays here as the offline test case for the default path. A pointer naming a missing directory raises `ConfigError` and never reads `courses/<name>/` instead, because a fall back would upload one course's template under another course's name and report success. `courses/*.env` is git-ignored, so a fresh clone has to be told once. `course.assert_directory_owns_course` checks the `course_key` in the directory it lands in, because the pointer is honored from the real environment too and a real environment variable applies to every course, while `upload` and `expect` resolve a path without ever reading `course.json`. The dependency runs one way: this repository reads the course repository, and the course repository depends on nothing here, because a public repository cannot install a private one.
- **`assets.notebook` resolves against the repository that owns the asset directory,** which `course.course_content_root` returns. `neo4j-fundamentals` names `notebooks/lab-01-verify-environment.ipynb` at this root, and an external course names its own notebook at its own root.
- **Starter-code source facts are per-course data, not module constants.** The `startercode` block in `course.json` holds the remote, the pinned commit, the checkout path, and the file list. `startercode.py` held them as constants, and a constant names one course only, which is what made a second course impossible to package. A course with no block ships no student package, and `vocareum-package-startercode` and `vocareum-release` say that rather than compare the course name to one value. `validate_source` compares repository identity rather than URL text, so an SSH clone and an HTTPS clone of the same repository both pass. `startercode.COURSE_ADAPTERS` is per course for the same reason a constant was not: an adapter names a file, and a file belongs to one workshop, so a course with no entry ships its pinned files unchanged. Vocareum's extractor drops dotfiles, and `assert_extractor_safe` refuses to package one that has no visible name in that course's renames, because the alternative is a student workspace missing a file with no error anywhere. `lab.template`'s `Metadata.SourceCommit` must equal `startercode.commit`; `scripts/validate_all.py` reads the block and checks it for every course in `LINTED_COURSES`.
- **A release record names an out-of-tree path absolutely.** `release.display_path` returns a root-relative path when it can and the absolute path when it cannot, because `Path.relative_to` raises for an external course's template and the plan comparison only needs a spelling that is stable across the prepare read and the apply read.
- **A gate line must not read as stronger than the lookup behind it.** `expect.KIND_ACTIONS` is the whole list of kinds, and models and DynamoDB tables are absent from it on purpose. Add a kind with the measured lookup, not ahead of it, and add its absence codes to `KIND_ABSENT_CODES` in the same edit: the two tables are read by name at lookup time, so a kind in one and not the other raises `KeyError` against a live account. `tests/management/test_expect.py` asserts they match. `expected.json` names no IAM kind because `iam:GetRole` and `iam:GetRolePolicy` on `voclabs` are an explicit deny in Vocareum's own policy on the session role, measured live 2026-08-23; the nine grants are covered by the stack status, which no `CREATE_COMPLETE` can reach without them.
- **`docs/permissions.md` is the only place in the repository that states a permission result.** Measure a new action, add a row there, and change nothing else. Code, tests, and other docs do not cite it. A sentence elsewhere states its own reasoning and leaves the result to the tracker.
- **Two principals, and a result for one is not a result for the other.** The *deployer role* (`assumed-role/vocareum/…`) runs `lab.template`. The *student session role* (`assumed-role/voclabs/user*`) runs notebook code. The tracker keeps them in separate tables on purpose. Do not merge them.
- **`lab.policy` is retired (2026-08-04).** In testing, statements from an uploaded `lab.policy` did not all reach the live session role. Every grant now lives as `AWS::IAM::RolePolicy` resources inside `lab.template`, applied by CloudFormation under the deployer role, which works. Do not reintroduce a separate policy file.
- **`lab.template` must land at `/voc/private/lab.template`,** filename exact, no added extension. `content` in the part `PUT` must be a **JSON array**; the object form returns HTTP 500 with an empty body.
- **The notebook couples to the template by IAM role name prefix,** not by a stack output. `lab.template` scopes IAM management to `role/workshop-*` and `role/AmazonBedrockAgentCoreSDK*`. A role created outside those prefixes is denied.
- **Docstrings here carry the reasoning, and it is not decorative.** Most module headers record a failure that produced the current design. Read the header before changing the module, and keep the same standard when adding code: say why, not what.

## Notebooks

`notebooks/lab-01-verify-environment.ipynb` is student-facing. It builds what the workshop builds, tears it down, and confirms the account is empty again. Ruff excludes `notebooks/`.

- Clear all outputs before committing.
- Cell IDs must be unique and match `^[a-zA-Z0-9-_]+$`.
- Steps print `PASS` / `FAIL` / `SKIP` rather than raising, except steps 1 and 2 (credentials, region), where raising is the point.
- Publishing is manual through Configure Workspace then Publish Notebook. `notebooks/README.md` has the exact click path and why there are no autograded cells.

### The notebook installs its helpers rather than shipping them

Step 0 pip-installs `workshop_lab` from a public GitHub archive, because a `.py` file uploaded beside the notebook does not import from it. The measurements, the dates, and the reasoning are in [`notebooks/README.md`](notebooks/README.md#how-the-helper-code-reaches-a-student). Four rules that are easy to break without noticing:

- **`src/workshop_lab/` is the source of truth; the published copy is not.** Edit under `src/`, run `scripts/sync_workshop_lab.py`, then push the workshop repo. `tests/runtime/test_workshop_lab_drift.py` guards it, and *skips* when that repo is not checked out beside this one, so a skip verifies nothing.
- **The installable copy cannot live here.** `aws-vocareum` is private, and a private repository answers both `raw.githubusercontent.com` and the archive endpoint with **404, not 403**. The public workshop repo hosts it.
- **Bump `__version__` on any published change.** A kernel that already imported the package keeps the old objects through a reinstall, so the version line step 0 prints is the only signal a fix landed. The remedy is Restart Kernel, not re-run.
- **`tests/management/test_notebook_structure.py` checks the notebook as a file:** cell ids, committed JSON shape, that every cell compiles, that nothing carries output, and that the `%pip install` line comes before the first `import workshop_lab`.

**Every step is converted.** Each code cell now imports a class from `workshop_lab` and calls it; none defines its own client, catches its own `ClientError`, or rebinds a `Harness` method as a bare name. The `TEMPORARY SCAFFOLDING` block in `step4-code` that made the unconverted steps work is gone, and `test_no_cell_rebinds_the_harness_as_bare_names` in `tests/management/test_notebook_structure.py` keeps it gone. A new step goes in a module under `src/workshop_lab/`, with its own offline test file, not in the notebook.

## Testing

Everything under `tests/` is offline. No AWS call, no Vocareum call, no lab session. `uv run pytest` runs the focused `vocareum_tools` management suite under `tests/management/`. `tests/runtime/` holds the student-facing `workshop_lab` suite. Run `./scripts/validate_all.py` for both suites plus repository-wide lint, formatting, and CloudFormation checks before a release.
