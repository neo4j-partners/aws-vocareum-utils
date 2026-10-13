---
marp: true
theme: default
paginate: true
title: Vocareum Utils
description: Letting coding agents run a Vocareum cloud lab course
---

<style>
section {
  --marp-auto-scaling-code: false;
  font-size: 28px;
  padding: 56px 72px;
}

h1 {
  color: #0a6190;
}

h2 {
  color: #0a6190;
  font-size: 40px;
}

code {
  font-size: 0.9em;
}

table {
  font-size: 22px;
}

/* Disable all fragment animations */
li,
.marp-fragment {
  opacity: 1 !important;
  animation: none !important;
  visibility: visible !important;
}

/* Title and closing slides. */
section.lead {
  justify-content: center;
}

section.lead h1 {
  font-size: 64px;
  margin-bottom: 0;
}

section.lead p.subtitle {
  font-size: 32px;
  color: #444;
}

/* A slide with a long table or many lines. */
section.dense {
  font-size: 24px;
}

section.dense table {
  font-size: 20px;
}

section.dense pre {
  font-size: 18px;
}
</style>

<!-- _class: lead -->
<!-- _paginate: false -->

# Vocareum Utils

<p class="subtitle">Letting coding agents run a Vocareum cloud lab course</p>

**Ryan Knight**, Neo4j. Co-presented with **Vocareum**.

- **What it is:** Tools for coding agents
- **What it covers:** Build, test, evaluate, run, and inspect courses

<!--
Vocareum Utils is a set of tools for coding agents. The tools build, test, evaluate, run, and inspect courses. They cover the full course lifecycle.

Before the webinar:
- Hide real identifiers. Do not show .env, courses/*.env, or support/. They hold real account, organization, and policy identifiers. Use placeholders on slides.
- Plan the live lab. Starting a lab spends one launch from the budget. Open a session before the webinar, and use --no-start during the demo.
-->

---

## Neo4j + Hyperscaler Courses on Vocareum

- **Joint integration stories:** Neo4j plus one cloud's services
- **Built end to end:** In a real cloud account
- **One platform:** Every Neo4j + hyperscaler course runs on Vocareum
- **Four clouds today:** AWS, Databricks, Google Cloud, Azure
- **Same student experience:** Click Start Lab, get a working cloud

<!--
Each course shows Neo4j working with one hyperscaler's services. Students build the integration end to end, in a real cloud account.

Neo4j hosts all of its Neo4j + hyperscaler courses on Vocareum. We run Neo4j + AWS, Neo4j + Databricks, Neo4j + Google Cloud, and Neo4j + Azure workshops on the platform.

A student clicks Start Lab and gets a working cloud environment. The cloud behind it changes from course to course.
-->

---

## Featured Course: Grounded AI Agents with Neo4j and AWS

- **Students build:** A hotel booking agent on Bedrock AgentCore
- **Neo4j holds:** Hotel knowledge, indexes, rules, reservations
- **Bedrock does:** Reasoning over evidence, query embeddings
- **Vocareum provides:** Browser VS Code, an AWS account in `us-east-1`
- **Students provide:** A free Neo4j AuraDB instance
- **Repository:** [neo4j-hotel-booking-agent-workshop](https://github.com/neo4j-partners/neo4j-hotel-booking-agent-workshop)

<!--
Students build a hotel booking agent grounded in a Neo4j knowledge graph. They deploy it on Amazon Bedrock AgentCore.

Six modules: build the graph, connected context, the grounded booking agent, AgentCore Gateway, AgentCore Runtime, and inspectable graph memory.

Neo4j holds the hotel knowledge, the retrieval indexes, the business rules, and the reservation writes. Amazon Bedrock reasons over the retrieved evidence and embeds queries.

Each student gets a browser-based Visual Studio Code environment and an AWS account with Bedrock access in us-east-1. Each student creates a free Neo4j AuraDB instance and restores the workshop graph into it.

The course and its Vocareum assets live in the public neo4j-hotel-booking-agent-workshop repository.
-->

---

## Coming Soon: Hands-On Workshop with Neo4j and Databricks

- **Students build:** A fleet agent over two databases
- **Neo4j Aura:** Fleet relationships and maintenance manuals
- **Databricks Lakehouse:** Sensor telemetry
- **LangGraph supervisor:** Routes each question to the right database
- **Provisioning:** Four Vocareum hooks, no CloudFormation
- **Repository:** [databricks-neo4j-workshop](https://github.com/neo4j-partners/databricks-neo4j-workshop)

<!--
Students build an AI agent that answers questions about a commercial aviation fleet. It works over two databases at once.

Six labs: Neo4j Aura setup, Databricks ETL to Neo4j, semantic search with GraphRAG, a Genie agent, a LangGraph agent, and agent memory in the graph. An optional appendix covers graph analytics.

This lab uses four Vocareum lifecycle hooks instead of a CloudFormation template:
- workspace_init.sh builds the shared catalog, schemas, data, ETL pipeline, and SQL warehouse.
- lab_setup.sh pre-warms each student's cluster.
- user_setup.sh makes sure the cluster is ready and imports the notebooks.
- lab_end.sh reclaims anything billable when the session ends.

TODO: Add the target date.
-->

---

## The Core Value of Vocareum

- **An isolated environment per student:** One cloud account each
- **Setup at Start Lab:** A script such as a CloudFormation template runs first
- **The author controls scope:** Which services and permissions exist

<!--
Each student gets their own cloud account. One student's work cannot touch another student's.

Vocareum runs a setup script, such as a CloudFormation template, when the student clicks Start Lab. The student starts in a lab that is already configured.

The setup script decides which services and permissions each student account has.
-->

---

## What Else Vocareum Provides

- **Lab environments:** JupyterLab and VS Code in the browser
- **Budgets and session limits:** Set per student
- **Rosters and enrollment:** See who has started a lab
- **A REST API:** Publishing, sessions, rosters, spend
- **More:** LMS integration, grading, analytics

<!--
Vocareum presents this slide. Confirm each item with them.

The API covers publishing, sessions, rosters, and spend. That API is what makes the rest of this talk possible.

Add LMS integration, grading, analytics, and any other features Vocareum wants to show.
-->

---

## Why We Need Vocareum Utils

- **One AWS account per student:** CloudFormation runs at Start Lab
- **A real account means real power:** Bedrock, AgentCore, and more
- **Permissions must be measured:** Wide enough, and no wider
- **Every step can be automated:** Vocareum API plus AWS APIs
- **Agents can do the work:** Build, check, and run end to end

<!--
Vocareum gives every student their own AWS account. At Start Lab, it runs a CloudFormation template in that account.

Students build with real AWS services, such as Bedrock and AgentCore. The course author decides exactly what each account can do.

We need to measure what each account can actually do. Only a measurement shows that the scope is right: wide enough for the labs, and no wider.

Vocareum's API covers publishing, sessions, and rosters. AWS covers everything inside the account. With the right tools, a coding agent can build, check, and run a course end to end.
-->

---

## vocgit or Vocareum Utils?

| **Your goal** | **Use** | **What it does** |
| --- | --- | --- |
| Publish a course | [vocareum-publisher](https://github.com/ddlin/vocareum-publisher), `vocgit` | Syncs files, settings, and rubrics from Git |
| Run the whole cloud lab lifecycle | Vocareum Utils | Builds, tests, publishes, runs, inspects, probes, and cleans up each student's AWS lab |

<!--
To publish a course, vocareum-publisher, also called vocgit, is ideal. It syncs files, settings, and rubrics from Git.

To build and run the whole cloud lab lifecycle, this repository goes beyond publishing. It gives agents the tools to build, test, publish, run, inspect, probe, and clean up the lab environment in each student's AWS account.
-->

---

## The Goal Is Agent-Driven Operations

- **The agent is the operator:** A person sets goals, approves risky steps
- **`CLAUDE.md` is the manual:** Commands, rules, and why
- **Docstrings carry the reasoning:** The failure behind each guard
- **Every answer is checkable:** Exit codes, and often `--json`

<!--
The primary user of these tools is a coding agent. A person sets the goal and approves the risky steps.

CLAUDE.md tells the agent which commands exist, which rules matter, and why.

Most modules record the failure that shaped their design. That stops an agent from "simplifying" away a guard that exists for a reason.

Tools exit with codes an agent can branch on. Several also emit --json.
-->

---

## What Makes a Tool Safe for an Agent

- **Preview by default:** `--publish`, `--delete`, or `--yes` to act
- **Three guards first:** Credentials, account, region
- **Exit codes as answers:** `0` ready, `3` not ready, `2` refused
- **Proof, not trust:** Read back every upload, poll every delete
- **Offline tests:** 800+, with no AWS or Vocareum call

<!--
Commands that change state do nothing without a flag such as --publish, --delete, or --yes.

The credentials must be non-empty, the account must match, and the region must be us-east-1. A confident answer about the wrong account is worse than no answer.

For example, vocareum-expect exits 0 when the lab is ready, 3 when it is not, and 2 when a guard refused.

Every upload is downloaded back and compared by hash. A write that returned is not a write that finished, so deletes are polled until they are gone.

More than 800 offline tests run with no AWS or Vocareum call. ./scripts/validate_all.py is the full gate.
-->

---

<!-- _class: dense -->

## The Key Parts of Every Course

| **Part** | **What it is** |
| --- | --- |
| Vocareum structure | A course holds an assignment. An assignment holds a part. |
| Infrastructure | `lab.template`, run at Start Lab. Every grant. At most 51,200 bytes. |
| Content | Notebooks, `CONFIG.txt`, and scripts from one pinned Git commit |
| Course definition | `course.json`: name, budget, session length, pin, cleanup prefixes |
| Ship gate | `expected.json`: what a finished lab holds. `vocareum-expect` checks it. |
| Settings | Lab type, session length, and spend budget, set on the part |

<!--
A course holds an assignment, and an assignment holds a part. Each part has its own workspace, settings, and lab sessions.

lab.template is a CloudFormation template that runs in each student's account at Start Lab. It holds every permission grant. It must stay at or under 51,200 bytes.

The starter code holds the notebooks, CONFIG.txt, and helper scripts. It is built from one pinned Git commit, so an uncommitted edit never reaches a student.

The part sets the lab type, such as JupyterLab or VS Code, the session length, and the spend budget.
-->

---

<!-- _class: dense -->

## Example Course: The AWS Workshop Repository

```
neo4j-hotel-booking-agent-workshop/     public course repository
├── CONFIG.txt                          Neo4j and AWS settings the student fills in
├── notebooks/                          six modules and the shared workshop package
├── environment/
│   ├── verify.py                       live readiness check before Module 1
│   └── vocareum/
│       ├── course.json                 course key, budget, session length, pin, cleanup prefixes
│       ├── lab.template                launch sentinel and three grants
│       ├── expected.json               the stack the ship gate expects
│       └── bump_pin.py                 moves the pin in both files at once
└── site/                               the workshop microsite

aws-vocareum-utils/                     tooling repository
└── courses/graphrag.env                COURSE_ASSET_DIR=../neo4j-hotel-booking-agent-workshop/environment/vocareum
```

<!--
The AWS hotel booking workshop keeps its content and its Vocareum assets in one public repository. The tools live in a second repository.
-->

---

## The Two-Repo Model

- **What ships:** `CONFIG.txt`, `README.md`, `notebooks`, `environment/verify.py`
- **`lab.template`:** A no-cost launch sentinel plus three grants
- **Course settings:** VS Code lab, six-hour session, $20 budget
- **One-way dependency:** Tooling reads the course through one env line
- **Release:** Commit, run `bump_pin.py`, then publish

`uv run vocareum-release --course graphrag --publish`

<!--
course.json lists four paths. Nothing else reaches the student.

lab.template creates no service workload. It holds a no-cost launch sentinel, because AWS rejects a template with no resources. It also holds three grants on the student session role, for Bedrock, AgentCore, and the workshop's own IAM roles.

The tooling repository reads the course repository through one env line. The course repository depends on nothing in the tooling, so students run it without the tools.

To release: commit the content, then run bump_pin.py to move the pin. Publish from the tooling repository.
-->

---

<!-- _class: dense -->

## What It Takes to Build a Course

1. **Create the shell** in the Vocareum UI: course, assignment, part
2. **Write the content:** Notebooks in the course repository, committed
3. **Write the infrastructure:** `lab.template`, each grant an `AWS::IAM::RolePolicy`
4. **Define the course:** `course.json`, `expected.json`, a pinned commit
5. **Connect and check:** Connect to the tools, run offline checks
6. **Publish:** Release the template and the starter code
7. **Prove it:** Start a lab, prove it came up, probe permissions
8. **Run and clean up:** Run the cohort, watch spend, sweep leftovers

<!--
Step 1 also covers turning on cloud labs and setting the lab type, budget, and session length.
-->

---

<!-- _class: dense -->

## The Course Lifecycle, One Command per Step

| **Step** | **Command** |
| --- | --- |
| Connect a course | `uv run vocareum-course-setup NAME --course-id COURSE_ID` |
| Scaffold course files | `uv run vocareum-course-setup NAME --write-local` |
| Check offline | `./scripts/validate_all.py` |
| Publish | `uv run vocareum-release --course NAME --publish` |
| Recover the rotated account | `uv run vocareum-refresh --course NAME` |
| Prove the lab came up | `uv run vocareum-expect --no-start --course NAME` |
| Confirm the live release | `uv run vocareum-release --course NAME --verify-live` |
| Run the cohort | `vocareum-end-all`, `vocareum-restart-student`, `vocareum-report-spend` |
| Clean up | `uv run vocareum-sweep --delete --course NAME` |
| Roll back | `uv run vocareum-release --course NAME --restore RELEASE_ID` |

Creating the course, assignment, and part is still done in the Vocareum web UI.

---

<!-- _class: dense -->

## Two Principals in Every Student Account

```
┌──────────────────── Student AWS account, one per student ─────────────────────┐
│                                                                               │
│   Deployer role                              Student session role             │
│   assumed-role/vocareum/...                  assumed-role/voclabs/user*       │
│  ┌───────────────────────────┐              ┌───────────────────────────┐     │
│  │ Runs lab.template at      │   attaches   │ Runs the notebook code    │     │
│  │ Start Lab, through        ├─────────────►│ Calls Bedrock, AgentCore, │     │
│  │ CloudFormation            │  RolePolicy  │ and the workshop IAM roles│     │
│  └───────────────────────────┘    grants    └───────────────────────────┘     │
│                                                                               │
│   Organization SCPs apply to both roles. Nothing in the account lifts them.   │
└───────────────────────────────────────────────────────────────────────────────┘
```

- **A result for one is not a result for the other:** Measure each role on its own

<!--
Vocareum assumes the deployer role to run lab.template. It needs rights to create every resource and grant in the template.

The notebooks run as the session role. It holds only what the template granted it.

An action that works for the deployer can still fail in a notebook. The tools measure each role on its own, and the permission tracker keeps them in separate tables.

An organization policy can deny an action to both roles. No grant in the template can fix that.
-->

---

## Why Cloud Labs Are Hard to Troubleshoot

- **A failed lab hides its cause:** The student sees only "did not start"
- **Two kinds of deny look the same:** Both arrive as `AccessDenied`
- **The account keeps moving:** Vocareum rotates a pool of accounts

<!--
When the setup template fails, the student sees only that the lab did not start. The reason sits in a CloudFormation event the student cannot read.

An organization policy deny and a missing grant both arrive as AccessDenied. Only one of them can be fixed in the course.

Vocareum rotates a pool of accounts. A check against the old account reports on the wrong lab.

The two principals from the last slide add a fourth problem. A result for one role says nothing about the other.
-->

---

<!-- _class: dense -->

## Tools for Troubleshooting and Probing

| **Capability** | **Command** | **What it does** |
| --- | --- | --- |
| Diagnose | `vocareum-diagnose` | Recovers why a stack failed, no console needed |
| Probe | `vocareum-probe` | Measures one IAM action. Tells the two denies apart. |
| Gate | `vocareum-expect` | Checks that a lab holds what it should |
| Refresh | `vocareum-refresh` | Repairs account and stack pins after a rotation |
| Sweep | `vocareum-sweep` | Finds and deletes this workshop's leftovers |

- **Measure stateful features:** Create, use, delete, record each step
- **One record of results:** `docs/permissions.md`

<!--
vocareum-diagnose recovers the CloudFormation failure reason without console access.

vocareum-probe names an IAM action, such as bedrock:ListFoundationModels, and returns a verdict. An organization-level deny cannot be lifted inside the account. An ordinary IAM shortfall can be fixed in lab.template. AWS reports both as AccessDenied, so the tool reads the message body.

The phase runners create a resource, use it, and delete it. Each delete is recorded as a measurement.

docs/permissions.md is the only place a permission result is written down.
-->

---

<!-- _class: dense -->

## Best Practices from Things That Broke (1 of 2)

| **Practice** | **What broke** | **The fix** |
| --- | --- | --- |
| Put every grant where CloudFormation applies it | Grants in an uploaded `lab.policy` did not all reach the session role | Every grant is an `AWS::IAM::RolePolicy` in `lab.template` |
| Confirm the account before you trust a result | Empty credentials made boto3 fall back to the operator's own account | Three guards: credentials, account, region |
| Wait until a change has finished | A cleanup reported a clean account five times while deletes were running | Every delete is polled until the resource is gone |
| Read past the surface of an error | Cloudflare blocked Python's default User-Agent with a 403 | Every call sends its own User-Agent |

<!--
Each practice came from something that broke. The tools now enforce the fix.

lab.policy is retired. The 403 looked like a bad token.
-->

---

<!-- _class: dense -->

## Best Practices from Things That Broke (2 of 2)

| **Practice** | **What broke** | **The fix** |
| --- | --- | --- |
| Treat an empty error as a shape problem | `content` sent as an object returned a 500 with an empty body | `content` is always a JSON array |
| Keep paired values in one edit | The pin in `course.json` and `SourceCommit` in `lab.template` could drift | `bump_pin.py` writes both. The release refuses a mismatch. |
| Check the limits that lint does not | A template over 51,200 bytes passes lint and fails at upload | `./scripts/validate_all.py` checks the byte count |
| Check what the platform drops | Vocareum's extractor drops dotfiles | The package build refuses a dotfile with no visible name |

- **Verify after you publish:** `--verify-live` is cheap, so run it often
- **Plan for account pools:** Sweep before ending, refresh after starting
- **Wait for the stack:** Credentials arrive before the grants do

<!--
Without the dotfile check, a student would get a workspace with a file missing and no error anywhere.

--verify-live compares the live course with the release. It is cheap, so run it again any time.

Each session can land in a different pooled account. Sweep leftovers before you end a session, and refresh the account pin after a new one starts.

Credentials appear before the template's grants are applied. Checks wait for the stack to finish before they run.
-->

---

<!-- _class: dense -->

## The Same Tools for Databricks: dbx-vocareum

| **Step** | **AWS: `aws-vocareum-utils`** | **Databricks: `dbx-vocareum`** |
| --- | --- | --- |
| Scaffold a course | `vocareum-course-setup NAME --write-local` | `dbx-vocareum-init lab/` |
| Publish | `vocareum-release --course NAME --publish` | `dbx-vocareum-upload lab/` |
| Prove the lab came up | `vocareum-expect --no-start --course NAME` | `dbx-vocareum-diagnose --expect expected.json` |
| Explain a failure | `vocareum-diagnose` | `dbx-vocareum-diagnose` |
| Measure permissions | `vocareum-probe` | `dbx-vocareum-probe --all` |
| End sessions | `vocareum-end-all` | `dbx-vocareum-session --end --yes` |
| Clean up | `vocareum-sweep --delete --course NAME` | `dbx-vocareum-orphans --delete` |
| Pre-warm compute | None needed | `dbx-vocareum-pool --create --min-idle 5` |

Every command runs with `uv run`. [dbx-vocareum on GitHub](https://github.com/neo4j-partners/dbx-vocareum)

<!--
dbx-vocareum is the Databricks counterpart to this repository. Its tools are ported from this one, and they follow the same rules.
-->

---

## dbx-vocareum: Same Rules, Databricks Realities

- **Same rules:** Read-only by default, hash-checked uploads
- **No publish step:** The next Start Lab uses the latest upload
- **Compute is the slow part:** About 16 minutes per cluster
- **Dependency reversed:** The course pulls in `dbx-vocareum`

<!--
Each tool only reads unless you pass an explicit flag. Every upload is downloaded back and compared by hash. An exit code of 3 means the stored file differs from the one sent.

The next Start Lab uses whatever was uploaded. Vocareum caches nothing.

A student waits about 16 minutes for a cluster. An instance pool keeps machines booted on standby and cuts about 10 of those minutes.

The Databricks course pulls dbx-vocareum in as a git dev dependency. The AWS course is the reverse: it depends on nothing, and the tooling reads it.
-->

---

<!-- _class: dense -->

## Why AWS Labs and Databricks Labs Differ

| | **AWS lab** | **Databricks lab** |
| --- | --- | --- |
| Isolation | One AWS account per student | One user and group per student, in a shared workspace |
| Setup | `lab.template`, declarative CloudFormation | Four shell scripts, run step by step |
| Failure record | Stack events name each failed resource | An exit code and output in the Session log |
| Rollback | CloudFormation rolls the stack back | None. A half-failed script leaves half a lab. |
| Student credentials | Temporary AWS keys from the session | None. A token must be made in a live session. |
| Denied access | `AccessDenied`, action named in the message | The object looks missing. "Not found" can mean "not allowed." |
| Workspace | Assigned from an account pool | One per part. The account tier caps active workspaces. |

---

<!-- _class: dense -->

## What a Databricks Lab Needs

- **Account admin for Vocareum:** Creates workspaces, assigns users
- **Hooks that report failures:** Non-zero exit, `error_code`, `message`
- **Shared work once:** `workspace_init.sh` builds what everyone shares
- **Student work per student:** `user_setup.sh`, the only required hook
- **Idempotent setup:** `workspace_init.sh` may run more than once
- **Careful cleanup:** `lab_end.sh` reads `stop` versus `terminate`
- **The right workspace:** The one Vocareum created, not one built by hand
- **A path to Neo4j:** Port 7687 open for Bolt to Aura

<!--
Vocareum's service principal holds the Databricks Account admin role. It uses that role to create workspaces and assign users.

A failing script must exit non-zero and write an error_code and a message. Otherwise the lab fails with no stated reason.

workspace_init.sh gets no user or group, so it builds only what every student shares. Per-student compute and notebooks go in user_setup.sh, the only required script. It also sets the page the student lands on.

workspace_init.sh runs once per workspace, and one course can have several workspaces. Every step must be safe to run again.

lab_end.sh must read the end-lab behavior. On stop, it keeps the student's work and removes only billable compute. On terminate, it can remove everything.

The tools must point at the workspace Vocareum created. Results from any other workspace describe a lab no student enters.

Vocareum opened port 7687 so clusters can reach Neo4j Aura over Bolt. Neo4j runs in Aura, not inside the workspace.
-->

---

## Demo: An Agent Runs the Lifecycle

1. **Offline self-test:** `uv run vocareum-probe --self-test`
2. **Release preview:** `uv run vocareum-release --course graphrag`
3. **Ship gate:** `uv run vocareum-expect --no-start --course graphrag`
4. **Sweep preview:** `uv run vocareum-sweep --course graphrag`
5. **Agent prompt:** "Is the graphrag lab healthy, and what would you clean up?"

<!--
Each step is safe to run on screen.

1. The self-test checks the classifier with no AWS call.
2. The release preview validates and builds the package without calling the API.
3. Run the ship gate against an open lab. Show the exit code.
4. The sweep preview lists leftovers and deletes nothing.
5. Ask Claude Code the question. Show it choose the commands from CLAUDE.md.

Starting a lab spends one launch from the budget. Use the session opened before the webinar, with --no-start.
-->

---

## How to Adopt the Pattern

- **Start with the guards:** Confirm account and region before reporting
- **Preview first:** Let the agent look before it acts
- **Answer with exit codes:** Branch on results, not prose
- **Write down why:** Reasoning in the code, so the agent keeps it
- **Keep one source of truth:** One file per measured result

---

<!-- _class: lead -->
<!-- _paginate: false -->

# Questions

- **The tooling:** [neo4j-partners/aws-vocareum-utils](https://github.com/neo4j-partners/aws-vocareum-utils)
- **The course:** [neo4j-partners/neo4j-hotel-booking-agent-workshop](https://github.com/neo4j-partners/neo4j-hotel-booking-agent-workshop)
- **Databricks tooling:** [neo4j-partners/dbx-vocareum](https://github.com/neo4j-partners/dbx-vocareum)
