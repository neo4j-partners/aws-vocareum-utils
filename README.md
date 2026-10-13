# aws-vocareum

CloudFormation and setup assets for the Neo4j AWS workshops that run on Vocareum.

Vocareum gives each student their own AWS account. When a student starts the lab, Vocareum runs a CloudFormation template in that account, so the environment can be built for them instead of assembled by hand during the session. That template is a file named `lab.template`, uploaded to `/voc/private/` in the instructor workspace.

## How this differs from vocareum-publisher

The right tool depends on what you need. If you want to publish course content from Git, [vocareum-publisher](https://github.com/ddlin/vocareum-publisher), also called `vocgit`, is ideal. It syncs files, assignment and part settings, and rubrics. This repository goes beyond publishing to cover the full lifecycle of an AWS cloud lab course. It gives coding agents the tools to build, test, publish, run, inspect, probe, and clean up the lab environment in each student's AWS account. Agents can scaffold a course, measure what the account permits, and confirm that a live lab came up. They can also find out why a stack failed, manage student sessions, and remove leftover resources at the end. Every upload is read back and compared by hash, so each release is verified end to end.

## What a course needs

A course is a small set of files. CloudFormation sets up the AWS account, starter code gives the student their files, and a manifest says what a working lab holds.

- **`lab.template`:** CloudFormation runs this template in the student's AWS account at Start Lab. It grants the permissions the notebooks need. It must stay at or under 51,200 bytes, because Vocareum sends it inline.
- **Starter code:** These are the notebooks and setup files a student sees in their workspace. Vocareum copies them to `/voc/work` only the first time a student opens the lab.
- **`course.json`:** This file names the course and its Vocareum settings. It lists the starter-code files and the commit they are built from. It also lists the name prefixes the cleanup sweep deletes.
- **`expected.json`:** This file lists what a working lab must hold, such as the CloudFormation stack. `vocareum-expect` checks a live lab against it.
- **`courses/NAME.env`:** This file holds the Vocareum ids for the course and the current lab account. It stays out of git because the ids are real.
- **Neo4j database:** Students connect to their own Neo4j Aura database. It runs outside the lab account. [`docs/neo4j-aura.md`](docs/neo4j-aura.md) explains why.

## How the hotel booking workshop does it

The `graphrag` course keeps its files in its own public repository, `neo4j-hotel-booking-agent-workshop`. One line in `courses/graphrag.env` tells this repository where to find them.

- **Asset folder:** The folder `environment/vocareum/` holds `course.json`, `lab.template`, and `expected.json`.
- **Pointer:** The line `COURSE_ASSET_DIR=../neo4j-hotel-booking-agent-workshop/environment/vocareum` in `courses/graphrag.env` points every tool here at that folder.
- **Template:** The template grants the student's `voclabs` session role access to the Bedrock models the notebooks call. It also creates one no-cost placeholder resource, because AWS rejects a template with no resources.
- **Starter code:** The student package holds `CONFIG.txt`, `README.md`, `notebooks/`, and `environment/verify.py`. The release builds it from one pinned commit with `git archive`.
- **Pin:** The pinned commit appears in two places: `startercode.commit` in `course.json` and `Metadata.SourceCommit` in `lab.template`. `environment/vocareum/bump_pin.py` updates both at once. The release refuses to run when they differ.
- **Release runbook:** `environment/vocareum/quick-release-workshop.md` lists every step from commit to live check.
- **One-way link:** The workshop repository depends on nothing here. Students clone and run it without this tooling.

## How a course goes live

Each step below is one subsection with commands you can paste. Replace `NAME` with the course key, such as `graphrag`. Only a running lab proves the student account allows what the course needs, so steps 6 and 7 are required.

### 1. Create the course in Vocareum

An instructor does this step in the Vocareum web UI. No command does it for you.

- **Course, assignment, and part:** Create all three. The part must have cloud labs turned on.
- **Part settings:** Set the lab type, session length, and budget on the part. Step 2 reads them back.
- **Test student:** Enroll one student so the session tools have a user to act as. A plus address such as `you+student1@example.com` keeps it separate from your instructor account.

### 2. Connect this repository

Give `vocareum-course-setup` the course ID. It finds the assignment, the part, and the enrolled student, then prints the lines for `courses/NAME.env`.

```bash
cp courses/course.env.example courses/NAME.env       # first time only
uv run vocareum-course-setup NAME --course-id COURSE_ID
```

Paste the `COURSE ENV` block it prints into `courses/NAME.env`. When the course has more than one assignment or part, the command stops and lists them. Rerun it with `--assignment-id` and `--part-id`. When more than one student is enrolled, add `--user-email`.

The main options:

| Option | What it does |
| --- | --- |
| *(none)* | Reads the live course and compares it with `course.json`. It prints a `MISMATCH` line per difference and exits 3 when there is one. It writes nothing. |
| `--course-id ID` | Looks up the assignment and part for that course. It ignores the ones saved in the env file, which still name the old course after a retarget. |
| `--refresh-local` | Rewrites the `vocareum` block of an existing `course.json` to match the live course. Use it after you change a part setting on purpose. |
| `--write-local` | Creates a new course: `course.json`, `lab.template`, `expected.json`, and `courses/NAME.env`. It refuses to overwrite any file that exists. |

Run `uv run vocareum-course-setup --help` for the rest.

### 3. Write the course files

The course needs `lab.template`, `expected.json`, and `course.json`. [What a course needs](#what-a-course-needs) explains each one. A course that keeps its files in another repository sets `COURSE_ASSET_DIR` in `courses/NAME.env`.

Commit the content first. Then move the starter-code pin to that commit. For the hotel booking workshop, run this in the workshop repository:

```bash
uv run environment/vocareum/bump_pin.py --check      # report drift, write nothing
uv run environment/vocareum/bump_pin.py              # pin HEAD in both files
```

### 4. Check offline

These checks make no AWS or Vocareum call. For each course, they also check that `lab.template` is 51,200 bytes or smaller, has every output `expected.json` names, and pins the same commit as `startercode.commit`.

```bash
./scripts/validate_all.py
```

### 5. Publish

The release uploads the template and the starter code. It then downloads both back and compares bytes.

```bash
uv run vocareum-release --course NAME                # preview, no API call
uv run vocareum-release --course NAME --publish
```

To pause for approval between saving and uploading, split the publish in two:

```bash
uv run vocareum-release --course NAME --prepare
uv run vocareum-release --course NAME --apply RELEASE_ID
```

- **Rollback bundle:** Before it uploads, the release saves the current remote copies under the ignored `.vocareum/releases/NAME/` directory.
- **Drift check:** `--apply` stops when a local or remote artifact changed after `--prepare`.
- **No lab use:** No release mode starts or extends a lab.
- **Starter code alone:** `uv run vocareum-package-startercode --course NAME` builds the student package from the pinned commit. It writes `startercode.zip` and an inventory under `.vocareum/build/NAME/` and makes no API call.

### 6. Test a live lab

Sign in as the test student and start the lab in Vocareum. Each new session can land in a different AWS account, so refresh the pins before you check anything.

```bash
uv run vocareum-refresh --course NAME --dry-run
uv run vocareum-refresh --course NAME
uv run vocareum-expect --no-start --course NAME
```

`vocareum-expect` exits 0 when the lab holds everything in `expected.json`. Then walk through the course as a student would.

### 7. Verify later

This compares the live course with the release. It is cheap, so run it after the live test and again later.

```bash
uv run vocareum-release --course NAME --verify-live
```

### 8. Update

Commit the content change and repeat steps 3 through 7. The starter code is built from the pinned commit, so an edit that is not pinned never reaches a student.

### 9. Roll back

A restore puts back the copies saved before that release. Release IDs are the folder names under `.vocareum/releases/NAME/`.

```bash
uv run vocareum-release --course NAME --restore RELEASE_ID
```

- **Keep the release directory:** Keep it until the new release passes the live student test.
- **Unrelated changes:** A restore refuses to replace remote content that changed outside that release.

## What is in this repository

This repository holds the tools that publish, check, and clean up a course. It also holds a small example course, `neo4j-fundamentals`, and the record of what the student account allows.

- **`src/vocareum_tools/`:** This Python package holds every command-line tool, such as upload, release, expect, diagnose, and sweep.
- **`scripts/`:** This folder holds the scripts that have no `uv run vocareum-*` command. These are the stateful permission tests and the offline gate `validate_all.py`.
- **`courses/`:** This folder holds one env file per course. It also holds the asset folder for `neo4j-fundamentals`.
- **`docs/`:** These guides cover deploying, diagnosing, permissions, the Vocareum API, and cost.
- **`tests/`:** Every test runs offline. `tests/management/` covers the tools.
- **Probe templates:** `lab-probe.template` and `lab-bedrock.template` run test calls from a temporary Lambda role. They find the limits the AWS organization sets on the account.

## Admin scripts

### Day to day

A handful of `uv run vocareum-*` commands cover the rest of the work.

- **Safe by default:** Each command shows what it would do and changes nothing until you pass the flag.
- **Order matters:** Sweep before you end a session. Ending a session gives the AWS account back to the pool, which wipes it and allocates a different one next time. Anything left behind becomes unreachable.

#### Upload the template

`vocareum-upload` puts a file into the Vocareum workspace. `--dry-run` shows what would be sent.

```bash
uv run vocareum-upload --dry-run --course neo4j-fundamentals
uv run vocareum-upload --course neo4j-fundamentals
```

#### Sweep leftovers from the account

`vocareum-sweep` deletes this workshop's leftover resources from a student account. Without `--delete`, it only lists them.

```bash
uv run vocareum-sweep --course neo4j-fundamentals
uv run vocareum-sweep --delete --course neo4j-fundamentals
```

- **Stack resources:** The sweep never touches resources the lab CloudFormation stack owns. Vocareum deletes that stack itself at session end.
- **How it finds resources:** It finds everything else by tag and by name prefix. It polls each delete instead of trusting one that returned.
- **Selection rule:** `src/vocareum_tools/selection.py` decides what counts as a workshop resource.

#### End lab sessions

`vocareum-end-lab` ends one session, and `vocareum-end-all` ends every student's session. Without `--yes`, both only preview.

```bash
uv run vocareum-end-lab --course neo4j-fundamentals
uv run vocareum-end-lab --yes --course neo4j-fundamentals

uv run vocareum-end-all --course neo4j-fundamentals
uv run vocareum-end-all --yes --course neo4j-fundamentals
```

- **One session:** `vocareum-end-lab` ends the one session named by `VOCAREUM_USER_ID` in `.env`.
- **Every session:** `vocareum-end-all` reads the course roster and ends every student's session. Students have no End Lab button, so a cohort otherwise runs the full four hours.
- **Keep your own:** Use `--exclude USER_ID` to keep your own session up.

#### Restart one student

`vocareum-restart-student` ends one student's cloud session and starts a fresh one with a new AWS account.

```bash
# Preview the exact student whose cloud session would be restarted.
uv run vocareum-restart-student --course graphrag \
  --student-email student@example.com

# End that session and start a fresh cloud session. This preserves /voc/work.
uv run vocareum-restart-student --course graphrag \
  --student-email student@example.com --yes
```

- **Finding the student:** It finds exactly one active roster entry by email. It changes nothing until you pass `--yes`.
- **Workspace files:** It keeps the student's `/voc/work` files on purpose. So it does **not** distribute a starter-code update.
- **Distributing an update:** Vocareum copies starter code to `/voc/work` only on first access. To distribute an update to this student, reset their workarea in Vocareum. That permanently replaces `/voc/work` with the current `/voc/startercode` tree.

#### Refresh the account and stack pins

After a new session opens, `vocareum-refresh` recovers the rotated account and stack pins. It does not start or extend the lab.

```bash
uv run vocareum-refresh --dry-run --course neo4j-fundamentals
uv run vocareum-refresh --course neo4j-fundamentals
```

- **Refusals:** It refuses unless exactly one non-deleting stack exists. Both pins must belong to the same selected env file.
- **Preview first:** Run `--dry-run` first.
- **New course:** Leave `LAB_AWS_ACCOUNT_ID=` and `LAB_STACK_NAME=` blank in `courses/NAME.env`. Refresh fills in both values from the open session.
- **No open session:** Refresh refuses with `Credential guard refused the run`. The session has ended, so start the lab and run refresh again.

#### Check that the lab came up

`vocareum-expect` is the ship gate. It checks a live lab against the course's `expected.json`, and the exit code is the answer.

```bash
uv run vocareum-expect --no-start --course neo4j-fundamentals
```

- **Exit codes:** `0` means met, `3` not met, `2` refused by a guard, and `1` a bad manifest.
- **How it checks:** Each course's `expected.json` lists what a finished lab holds. The gate looks up every entry read-only.
- **Why it exists:** "Did the lab come up" used to be answered by reading `diagnose` output. That works while one person knows what the stack should contain, and it stops working once there is a second course.
- **Guards:** It runs the same three guards as the probe. It treats a wrong region as fatal, because a pass against the wrong account is worse than no answer.
- **Absent versus unreadable:** A denied read fails the gate too, but it is reported apart from a missing resource. It is classified with the same SCP-versus-IAM vocabulary the probe uses, so a gate failure that is really a missing grant says so.
- **No IAM read in the manifest:** This is a measurement, not an oversight. The first live run, 2026-08-23, showed that the session role is not permitted `iam:GetRole` or `iam:GetRolePolicy` on `voclabs`. A line naming the nine `LabTemplate*Grant` policies could never pass.
- **How the grants are covered instead:** The stack cannot reach `CREATE_COMPLETE` unless CloudFormation created all nine `AWS::IAM::RolePolicy` resources.

#### Report spend

`vocareum-report-spend` totals AWS spend for the whole cohort. It is read-only: it starts, extends, and ends nothing.

```bash
uv run vocareum-report-spend --course neo4j-fundamentals
```

- **What it reads:** It reads the course roster and every student's own session record.
- **Why not `vocareum-diagnose`:** `vocareum-diagnose` only answers for the one student named by `VOCAREUM_USER_ID`. That is often an instructor id that was never enrolled as a student, which the API refuses with `HTTP 400 The user is not enrolled as a student in the course`.
- **Unreadable sessions:** A student whose session cannot be read is reported, not silently dropped, and the exit code says so. A total after a dropped read is a lower bound, not the cohort's true spend.

#### Remove students from a course

`remove_course_students.py` unenrolls every student from a course whose session has already ended. Without `--yes`, it lists who would be removed.

```bash
./scripts/remove_course_students.py --course-id COURSE_ID
./scripts/remove_course_students.py --course-id COURSE_ID --yes
```

- **How it works:** It pages the roster and issues one `DELETE /courses/{id}/users/{user_id}` per row where `role == "student"`.
- **Course id:** It takes a raw `--course-id`, not `--course NAME`. A course being cleared out this way need not have a `courses/*.env` file.
- **No bulk delete:** There is no bulk-delete endpoint. `DELETE /courses/{id}/users` with no id answers `Invalid Request - userid missing`, confirmed live 2026-08-27.
- **Skipped rows:** Instructor and TA rows are left alone. A row already `dropped: true` is skipped, because deleting it again answers `The user is not enrolled as a student in the course` rather than succeeding.

### More than one course

Every tool takes `--course NAME`, which layers `courses/NAME.env` over the root `.env`. [`courses/README.md`](courses/README.md) explains what goes in each file.

## Where to start

| I want to | Read |
| --- | --- |
| Know what the student account is allowed to create | [`docs/permissions.md`](docs/permissions.md) |
| Get a template up to Vocareum | [`docs/deploying.md`](docs/deploying.md) |
| Find out why a lab said `CREATE_FAILED` | [`docs/diagnosing.md`](docs/diagnosing.md) |
| Check whether a lab really came up, without reading output | [`courses/neo4j-fundamentals/expected.json`](courses/neo4j-fundamentals/expected.json) and `uv run vocareum-expect` |
| Run this repository against more than one course | [`courses/README.md`](courses/README.md) |
| Call the Vocareum REST API from code | [`docs/vocareum-api.md`](docs/vocareum-api.md) |
| Know where Neo4j runs, and why it is not in the lab account | [`docs/neo4j-aura.md`](docs/neo4j-aura.md) |

## Files

| File | What it is |
| --- | --- |
| `courses/neo4j-fundamentals/lab.template` | The example course's CloudFormation template. It creates the no-cost proof resources and the inline session-role grants required by the verification notebook |
| `lab-bedrock.template` | A Bedrock probe that runs as a temporary Lambda execution role. It diagnoses the account and organization ceiling, not the student or template-deployer role |
| `lab-probe.template` | The same temporary-Lambda approach generalized. Takes API calls as a parameter and reports a verdict per call as a stack Output |
| `docs/` | The reference documents linked above |
| `uv run vocareum-upload` | Replaces a file in the Vocareum workspace over the API, waits for the write, then downloads it back and compares hashes |
| `uv run vocareum-diagnose` | Recovers the CloudFormation failure reason from a lab that reported `CREATE_FAILED`, with no console access |
| `uv run vocareum-refresh` | Reads an already-open session and atomically refreshes its account and stack pins. Refuses zero or multiple active stacks and never starts or extends a session |
| `uv run vocareum-expect` | Checks a live lab against the selected course's `expected.json` and exits non-zero on anything missing, not ready, or unreadable |
| `courses/NAME/` | One committed `course.json`, `lab.template`, and `expected.json` asset set per course |
| `courses/NAME.env` | Ignored course coordinates layered over the root `.env`; `course.env.example` is the committed template |
| `uv run vocareum-probe` | Probes a list of API calls as the student session role, refusing to report at all when the credentials or the account are not the expected ones |
| `scripts/run_phase1_bedrock.py` | Runs the guarded student-role Bedrock checks, consumes streaming responses, and saves the complete model catalog |
| `uv run vocareum-sweep` | Empties a student AWS account of everything the workshop created, skipping anything the lab CloudFormation stack owns, and polling each delete rather than trusting one that returned |
| `uv run vocareum-end-lab` | Ends the one lab session named in `.env`, which is the only way to force the CloudFormation Delete the sweep hangs off |
| `uv run vocareum-end-all` | Ends every student's session on the part, read from the course roster, since there is no bulk endpoint and no End Lab button in the student's AWS view |
| `uv run vocareum-report-spend` | Reads the course roster and every student's own session record to total AWS spend for the whole cohort. Read-only: starts, extends, and ends nothing |
| `uv run vocareum-restart-student` | Safely restarts one student's cloud session by roster email; dry run by default and requires `--yes` because it gives up the student's AWS account, while preserving their workarea |
| `src/vocareum_tools/` | Installable Python package behind every `vocareum-*` command, including shared configuration, API, transaction, redaction, and phase-runner code |
| `tests/management/` | Focused offline tests for course definitions, inspection, scaffolding, release, rollback, and the supporting management tools |
| `pyproject.toml` | Python dependencies, command entry points, and Ruff/pytest configuration |
| `.env.example` | Template for the `.env` the scripts read. Copy it, fill it in, and never commit the result |

`support/` stays out of git, because it holds private working notes and logs that carry real account identifiers. The committed documents write those identifiers as `${PLACEHOLDER}` and resolve them from `.env`.

## Quickstart

```bash
cp .env.example .env        # then fill in the token, course, assignment, part, and user IDs
cfn-lint courses/neo4j-fundamentals/lab.template
uv run vocareum-upload --dry-run --course graphrag
uv run vocareum-diagnose --no-start
uv run vocareum-probe --self-test
```

The tools are a [uv](https://docs.astral.sh/uv/) project. The first command resolves the locked environment automatically. Every `vocareum-*` command is an entry point in `pyproject.toml`.

For development:

```bash
uv sync --group dev
uv run pytest                     # focused management suite
./scripts/validate_all.py         # complete CI and pre-release gate
uv run ruff check .
uv run ruff format --check .
```

A run without `--no-start` consumes one lab launch against the budget, so `--no-start` is the right default for reusing a session that is already open, which is most runs. It is not a rule to follow reflexively: a live session is the only instrument that measures what the account permits, so start one when the question has no offline answer.

## Scanning the account pool for Bedrock entitlement

Bedrock model access can differ between accounts in a pool. An account without
access answers an invocation with `ValidationException: Error 002: Access to
Bedrock models is not allowed for this account`. Read APIs such as
`GetFoundationModelAvailability` do not show the difference, so the only test
is a real invocation. An invocation needs a session, and each session lands in
one pool account.

`vocareum-scan-pool` walks that rotation: start, discover the account, wait for
the lab stack, invoke once, end, repeat. It replaces clicking Student View once
per account.

```bash
uv run vocareum-scan-pool --course graphrag                      # dry run, spends nothing
uv run vocareum-scan-pool --course graphrag --limit 1 --max-rounds 1 --yes
uv run vocareum-scan-pool --course graphrag --limit 6 --yes      # a real pass
```

**A dry run is the default and `--yes` is what spends.** The dry run reads the
open session and prints how many launches a real run could cost. Every start
costs one launch against the budget, so `--max-rounds` is a hard ceiling on
starts and defaults to twice `--limit`, because reallocation is not round-robin
and repeats are expected.

Reuse an open session rather than paying for one. Round 1 measures whatever is
already up for free, so leaving the previous scan's session open makes the next
scan one launch cheaper.

### The flags that matter

| Flag | Why it exists |
|---|---|
| `--limit N` | Stop once N distinct accounts have answered. Rounds whose stack never came up do not count toward it. |
| `--max-rounds N` | Hard cap on session starts. This is the spend ceiling. |
| `--end-existing` | Allow the scan to end a session it did not start. Sweep that account first: ending it wipes the member. |
| `--model ID` | Which model to invoke. Defaults to `anthropic.claude-sonnet-4-6`. |
| `--stack-timeout N` | How long to let CloudFormation finish before giving up on a round. Defaults to 900 seconds. |
| `--cleanup-retries N` | How many times to wait for pool cleanup before a start. Defaults to 8. |
| `--cleanup-wait S` | Seconds between those retries. Defaults to 60. |
| `--end-retries N` | How many times to wait for lab setup to finish before an end. Defaults to 5. |
| `--end-wait S` | Seconds between those retries. Defaults to 30. |
| `--log PATH` / `--no-log` | Where the audit log lands, or none. |
| `--json` | Emit the result as JSON instead of a table. |

### Why it waits for the stack

The `bedrock:InvokeModel` grant is an `AWS::IAM::RolePolicy` inside
`lab.template`, and it does not exist until CloudFormation applies it.
Credentials appear the moment a member is allocated, which is minutes earlier.
Measured 2026-08-25: an invocation two seconds after
the start was accepted returned `AccessDeniedException ... no identity-based
policy allows bedrock:InvokeModel`, and six minutes later the same account,
model, and session returned Error 002. A scan that did not wait would spend its
whole budget on rounds that all answer `IAM_DENY`.

The stack is discovered with `cloudformation:ListStacks` rather than read from
`LAB_STACK_NAME`, because the pin names the previous rotation's account and this
scan is what rotates. A build that fails ends the wait on the first poll instead
of burning the timeout, and the round is recorded as `STACK_FAILED` so a spent
launch that measured nothing is visible as one.

### Why it waits out a cleaning pool

Ending a session returns its account to the pool, and Vocareum cleans the
account before it is allocated again. Until that finishes, a start answers
`HTTP 400 ... Failed operation: Start. account is still in cleanup, expect
another 5 minutes`. With a pool of three accounts, ending one session can leave every account in
cleanup for a few minutes.

A start that waits for cleanup costs no launch, so waiting it out is free.
The scan retries only that answer, matched on the message body, because the
status is a plain `400` that a wrong part or a rotated token also returns, and
a config error does not become true by asking again. When the retries run out
the scan says how many times it asked and that nothing was spent.

### Why it waits before ending a session

Vocareum finishes its own lab setup a little after the stack does. Until then,
an end answers `HTTP 400 ... Failed operation: End. Lab is setting up. Please
wait for it to finish`. This can happen after CloudFormation reports
`CREATE_COMPLETE`, so the scan retries the end instead of relying on the stack
status.

This retry is not free the way the start-side one is. The launch is already
spent and the session is already open, so exhausting the retries stops the scan
and names the account to end by hand rather than rotating past a pool member it
never released. The three rounds already measured are still reported.

### Reading the audit log

Each run appends JSON Lines to `support/poolscan-<UTC-stamp>.jsonl`, created
`0600`, flushed per line so a run that dies keeps the evidence its spent
launches already bought. `support/` is git-ignored, which matters because the
log names real account IDs.

```bash
# What did the most recent run conclude?
jq -c 'select(.event == "run_end")' "$(ls -t support/poolscan-*.jsonl | head -1)"

# Every account ever measured, and how it answered.
jq -r 'select(.event == "measurement") | [.account, .entitlement] | @tsv' \
  support/poolscan-*.jsonl | sort -u

# Has any account ever answered two different ways?
jq -r 'select(.event == "run_end") | .unstableAccounts[]' support/poolscan-*.jsonl
```

That last question is the one the scan exists to settle. An account that answers
both ways means model access is not a fixed property of the account.
