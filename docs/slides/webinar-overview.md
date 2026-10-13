# Webinar proposal: Neo4j Courses on Vocareum

This is a proposed outline for a joint webinar with Vocareum. It is about 40 minutes long, with about 10 minutes for questions.

## The one-sentence pitch

This repository gives coding agents the tools and scripts to build, test, publish, run, inspect, probe, and clean up Vocareum AWS cloud labs. With it, an agent can automate the whole lifecycle of building and running a course.

## Audience

- **Course authors:** Course authors build hands-on AWS labs on Vocareum and want fewer surprises on launch day.
- **Platform and DevOps teams:** These teams support many courses and want each step to be repeatable and checkable.
- **Teams using coding agents:** These teams want an agent such as Claude Code to do the operational work safely.

## What the audience should leave with

- **A mental model:** Publishing content and operating a cloud lab are two different jobs. Both can be automated.
- **A pattern for agent-safe tools:** Each tool previews by default, answers with an exit code, and refuses to report on the wrong account.
- **A lifecycle they can copy:** Every step from an empty course to a finished cohort maps to one command.

---

## Slide 1: Title: Neo4j Courses on Vocareum

**Overview of Neo4j Courses on Vocareum and Built Utils**

Speaker: Ryan Knight. Co-presented with Vocareum.

- **Part 1:** The talk covers how Neo4j hosts its cloud lab courses on Vocareum.
- **Part 2:** The talk covers the tools we built so coding agents can run those courses.

## Slide 2: Neo4j + hyperscaler integration courses on Vocareum

- **Joint integration stories:** Each course shows Neo4j working with one hyperscaler's services. Students build the integration end to end, in a real cloud account.
- **One platform for every cloud:** Neo4j hosts all of its Neo4j + hyperscaler courses on Vocareum.
- **Four clouds today:** We run Neo4j + AWS, Neo4j + Databricks, Neo4j + Google Cloud, and Neo4j + Azure workshops on the platform.
- **The same student experience:** A student clicks Start Lab and gets a working cloud environment. The cloud behind it changes from course to course.

## Slide 3: Featured course: Grounded AI Agents with Neo4j and AWS

- **What students build:** Students build a hotel booking agent grounded in a Neo4j knowledge graph. They deploy it on Amazon Bedrock AgentCore.
- **Six modules:** The modules are build the graph, connected context, the grounded booking agent, AgentCore Gateway, AgentCore Runtime, and inspectable graph memory.
- **The split of work:** Neo4j holds the hotel knowledge, the retrieval indexes, the business rules, and the reservation writes. Amazon Bedrock reasons over the retrieved evidence and embeds queries.
- **What Vocareum provides:** Each student gets a browser-based Visual Studio Code environment and an AWS account with Bedrock access in `us-east-1`.
- **What the student provides:** Each student creates a free Neo4j AuraDB instance and restores the workshop graph into it.
- **Where it lives:** The course and its Vocareum assets live in the public [neo4j-hotel-booking-agent-workshop](https://github.com/neo4j-partners/neo4j-hotel-booking-agent-workshop) repository.

## Slide 4: Coming soon: Hands-On Workshop: Neo4j and Databricks

- **What students build:** Students build an AI agent that answers questions about a commercial aviation fleet. It works over two databases at once.
- **The split of work:** Neo4j Aura holds the fleet's relationships and its maintenance manuals. The Databricks Lakehouse holds the sensor telemetry. A LangGraph supervisor routes each question to the database that can answer it.
- **Six labs:** The labs are Neo4j Aura setup, Databricks ETL to Neo4j, semantic search with GraphRAG, a Genie agent, a LangGraph agent, and agent memory in the graph. An optional appendix covers graph analytics.
- **How Vocareum provisions it:** This lab uses four Vocareum lifecycle hooks instead of a CloudFormation template.
  - `workspace_init.sh` builds the shared catalog, schemas, data, ETL pipeline, and SQL warehouse.
  - `lab_setup.sh` pre-warms each student's cluster.
  - `user_setup.sh` makes sure the cluster is ready and imports the notebooks.
  - `lab_end.sh` reclaims anything billable when the session ends.
- **Where it lives:** The course lives in the public [databricks-neo4j-workshop](https://github.com/neo4j-partners/databricks-neo4j-workshop) repository.
- **TODO:** Add the target date.

## Slide 5: The core value of Vocareum

- **An isolated environment per student:** Each student gets their own cloud account. One student's work cannot touch another student's.
- **Setup scripts at lab start:** Vocareum runs a setup script, such as a CloudFormation template, when the student clicks Start Lab. The student starts in a lab that is already configured.
- **The author controls the scope:** The setup script decides which services and permissions each student account has.

## Slide 6: What else Vocareum provides

Vocareum presents this slide. Confirm each item with them.

- **Lab environments:** Vocareum hosts browser-based environments such as JupyterLab and VS Code.
- **Budgets and session limits:** Each course sets a spend budget and a session length per student.
- **Rosters and enrollment:** Instructors enroll students and see who has started a lab.
- **A REST API:** The API covers publishing, sessions, rosters, and spend. That API is what makes the rest of this talk possible.
- **Other Features:** Add LMS integration, grading, analytics, and any other features Vocareum wants to show.

## Slide 7: Why we need Utils

- **Each student gets an AWS account:** Vocareum gives every student their own AWS account. At Start Lab, it runs a CloudFormation template in that account.
- **A real account means real power:** Students build with real AWS services, such as Bedrock and AgentCore. The course author decides exactly what each account can do.
- **Permissions must be measured:** We need to measure what each account can actually do. Only a measurement shows that the scope is right: wide enough for the labs, and no wider.
- **Every step can be automated:** Vocareum's API covers publishing, sessions, and rosters. AWS covers everything inside the account.
- **Agents can do the operational work:** With the right tools, a coding agent can build, check, and run a course end to end.

## Slide 8: Vocgit or Vocareum Utils?

- **To publish a course:** [vocareum-publisher](https://github.com/ddlin/vocareum-publisher), also called `vocgit`, is ideal. It syncs files, settings, and rubrics from Git.
- **To build and run the whole cloud lab lifecycle:** This repository goes beyond publishing. It gives agents the tools to build, test, publish, run, inspect, probe, and clean up the lab environment in each student's AWS account.

## Slide 9: The goal is agent-driven operations

- **The agent is the operator:** The primary user of these tools is a coding agent. A person sets the goal and approves the risky steps.
- **The repository is the agent's manual:** `CLAUDE.md` tells the agent which commands exist, which rules matter, and why.
- **The docstrings carry the reasoning:** Most modules record the failure that shaped their design. That stops an agent from "simplifying" away a guard that exists for a reason.
- **Every answer is checkable:** Tools exit with codes an agent can branch on. Several also emit `--json`.

## Slide 10: What makes a tool safe for an agent

- **Preview by default:** Commands that change state do nothing without a flag such as `--publish`, `--delete`, or `--yes`.
- **Three guards before any report:** The credentials must be non-empty, the account must match, and the region must be `us-east-1`. A confident answer about the wrong account is worse than no answer.
- **Exit codes as answers:** For example, `vocareum-expect` exits `0` when the lab is ready, `3` when it is not, and `2` when a guard refused.
- **Proof, not trust:** Every upload is downloaded back and compared by hash. A write that returned is not a write that finished, so deletes are polled until they are gone.
- **Offline tests:** More than 800 offline tests run with no AWS or Vocareum call. `./scripts/validate_all.py` is the full gate.

## Slide 11: The key parts of every course

- **The Vocareum structure:** A course holds an assignment, and an assignment holds a part. Each part has its own workspace, settings, and lab sessions.
- **Infrastructure:** `lab.template` is a CloudFormation template that runs in each student's account at Start Lab. It holds every permission grant. It must stay at or under 51,200 bytes.
- **Content:** The starter code holds the notebooks, `CONFIG.txt`, and helper scripts. It is built from one pinned Git commit, so an uncommitted edit never reaches a student.
- **Course definition:** `course.json` names the course, its budget, its session length, its starter code pin, and its cleanup prefixes.
- **Ship gate:** `expected.json` states what a finished lab holds. `vocareum-expect` checks a live lab against it.
- **Settings:** The part sets the lab type, such as JupyterLab or VS Code, the session length, and the spend budget.

## Slide 12: Example course: the AWS workshop and the two-repo model

The AWS hotel booking workshop keeps its content and its Vocareum assets in one public repository. The tools live in a second repository.

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

- **What ships to students:** `course.json` lists four paths: `CONFIG.txt`, `README.md`, `notebooks`, and `environment/verify.py`. Nothing else reaches the student.
- **What `lab.template` holds:** It creates no service workload. It holds a no-cost launch sentinel, because AWS rejects a template with no resources. It also holds three grants on the student session role, for Bedrock, AgentCore, and the workshop's own IAM roles.
- **Course settings:** Each student gets a Visual Studio Code lab, a six-hour session, and a $20 budget.
- **The dependency runs one way:** The tooling repository reads the course repository through one env line. The course repository depends on nothing in the tooling, so students run it without the tools.
- **How a release flows:** Commit the content, then run `bump_pin.py` to move the pin. Publish from the tooling repository with `uv run vocareum-release --course graphrag --publish`.

## Slide 13: What it takes to build a course

1. **Create the shell:** Create the course, the assignment, and the part in the Vocareum web UI. Turn on cloud labs and set the lab type, budget, and session length.
2. **Write the content:** Write the notebooks in the course repository and commit them.
3. **Write the infrastructure:** Write `lab.template`, with every grant as an `AWS::IAM::RolePolicy`.
4. **Define the course:** Write `course.json` and `expected.json`, and pin the starter code commit.
5. **Connect and check:** Connect the course to the tools, then run the offline checks.
6. **Publish:** Release the template and the starter code to Vocareum.
7. **Prove it:** Start a lab, prove it came up, and probe the permissions the notebooks need.
8. **Run and clean up:** Run the cohort, watch spend, and sweep leftovers when it ends.

## Slide 14: The course lifecycle, one command per step

| Step | Command |
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

## Slide 15: Two principals in every student account

```
┌──────────────────────── Student AWS account, one per student ─────────────────────────┐
│                                                                                       │
│   Deployer role                                  Student session role                 │
│   assumed-role/vocareum/...                      assumed-role/voclabs/user*           │
│  ┌─────────────────────────────┐                ┌─────────────────────────────┐       │
│  │ Runs lab.template at        │   attaches     │ Runs the notebook code      │       │
│  │ Start Lab, through          ├───────────────►│ Calls Bedrock, AgentCore,   │       │
│  │ CloudFormation              │  RolePolicy    │ and the workshop IAM roles  │       │
│  └─────────────────────────────┘    grants      └─────────────────────────────┘       │
│                                                                                       │
│   Organization SCPs apply to both roles. Nothing inside the account can lift them.    │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

- **The deployer role builds the lab:** Vocareum assumes this role to run `lab.template`. It needs rights to create every resource and grant in the template.
- **The session role runs the student's code:** The notebooks run as this role. It holds only what the template granted it.
- **A result for one is not a result for the other:** An action that works for the deployer can still fail in a notebook. The tools measure each role on its own, and the permission tracker keeps them in separate tables.
- **Some limits come from above:** An organization policy can deny an action to both roles. No grant in the template can fix that.

## Slide 16: Why cloud labs are hard to troubleshoot

- **A failed lab hides its cause:** When the setup template fails, the student sees only that the lab did not start. The reason sits in a CloudFormation event the student cannot read.
- **Two kinds of deny look the same:** An organization policy deny and a missing grant both arrive as `AccessDenied`. Only one of them can be fixed in the course.
- **The account keeps moving:** Vocareum rotates a pool of accounts. A check against the old account reports on the wrong lab.

The two principals from the previous slide add a fourth problem. A result for one role says nothing about the other.

## Slide 17: Tools for troubleshooting and probing

- **Diagnose:** `vocareum-diagnose` recovers why a stack failed, without console access.
- **Probe:** `vocareum-probe --probe bedrock:ListFoundationModels` names an IAM action and returns a verdict. It tells the two kinds of deny apart. An organization-level deny cannot be lifted inside the account. An ordinary IAM shortfall can be fixed in `lab.template`. AWS reports both as `AccessDenied`, so the tool reads the message body.
- **Gate:** `vocareum-expect` checks that a lab holds what it should.
- **Refresh:** `vocareum-refresh` repairs the account and stack pins after a rotation.
- **Sweep:** `vocareum-sweep` finds and deletes this workshop's leftovers.
- **Measure stateful features:** The phase runners create a resource, use it, and delete it. Each delete is recorded as a measurement.
- **One record of results:** `docs/permissions.md` is the only place a permission result is written down.

## Slide 18: Best practices to avoid failures

Each practice came from something that broke. The tools now enforce the fix.

| Practice | What broke | How we fixed it |
| --- | --- | --- |
| Put every grant where CloudFormation applies it | Grants in an uploaded `lab.policy` did not all reach the student session role. | Every grant moved into `lab.template` as an `AWS::IAM::RolePolicy`. `lab.policy` is retired. |
| Confirm the account before you trust a result | A finished session returned empty credentials. boto3 fell back to the operator's own credentials, and a probe reported on the wrong account. | Three guards run first: non-empty credentials, the right account, and the right region. |
| Wait until a change has finished | A cleanup reported a clean account five times while resources were still being deleted. | Every delete is polled until the resource is gone. |
| Read past the surface of an error | Cloudflare blocked Python's default User-Agent with a 403. It looked like a bad token. | Every call sends its own User-Agent. |
| Treat an empty error as a shape problem | A part update with `content` as an object returned a 500 with an empty body. | The tools always send `content` as a JSON array. |
| Keep paired values in one edit | The starter code pin in `course.json` and `SourceCommit` in `lab.template` could drift apart. | `bump_pin.py` writes both, and the release refuses a mismatch. |
| Check the limits that lint does not | A template over 51,200 bytes passes lint and fails only at upload. | `./scripts/validate_all.py` checks the byte count offline. |
| Check what the platform drops | Vocareum's extractor drops dotfiles, so a student would get a workspace with a file missing. | The package build refuses a dotfile that has no visible name. |

Three more habits:

- **Verify after you publish:** `--verify-live` compares the live course with the release. It is cheap, so run it again any time.
- **Plan for account pools:** Each session can land in a different pooled account. Sweep leftovers before you end a session, and refresh the account pin after a new one starts.
- **Wait for the stack:** Credentials appear before the template's grants are applied. Checks wait for the stack to finish before they run.

## Slide 19: The same tools for Databricks: dbx-vocareum

[dbx-vocareum](https://github.com/neo4j-partners/dbx-vocareum) is the Databricks counterpart to this repository. Its tools are ported from this one, and they follow the same rules.

| Step | AWS: `aws-vocareum-utils` | Databricks: `dbx-vocareum` |
| --- | --- | --- |
| Scaffold a course | `vocareum-course-setup NAME --write-local` | `dbx-vocareum-init lab/` |
| Publish | `vocareum-release --course NAME --publish` | `dbx-vocareum-upload lab/` |
| Prove the lab came up | `vocareum-expect --no-start --course NAME` | `dbx-vocareum-diagnose --expect expected.json` |
| Explain a failure | `vocareum-diagnose` | `dbx-vocareum-diagnose` |
| Measure permissions | `vocareum-probe` | `dbx-vocareum-probe --all` |
| End sessions | `vocareum-end-all` | `dbx-vocareum-session --end --yes` |
| Clean up | `vocareum-sweep --delete --course NAME` | `dbx-vocareum-orphans --delete` |
| Pre-warm compute | None needed | `dbx-vocareum-pool --create --min-idle 5` |

Every command runs with `uv run`.

- **The same rules:** Each tool only reads unless you pass an explicit flag. Every upload is downloaded back and compared by hash. An exit code of `3` means the stored file differs from the one sent.
- **No publish step:** The next Start Lab uses whatever was uploaded. Vocareum caches nothing.
- **Compute is the slow part:** A student waits about 16 minutes for a cluster. An instance pool keeps machines booted on standby and cuts about 10 of those minutes.
- **The course depends on the tooling:** The Databricks course pulls `dbx-vocareum` in as a git dev dependency. The AWS course is the reverse: it depends on nothing, and the tooling reads it.

## Slide 20: Why Databricks labs are different, and what they need

| | AWS lab | Databricks lab |
| --- | --- | --- |
| Isolation | One AWS account per student | One user and one group per student, inside a shared workspace |
| Setup | `lab.template`, a declarative CloudFormation template | Four shell scripts that run step by step |
| Failure record | Stack events that name each failed resource | An exit code and captured output in the console Session log |
| Rollback | CloudFormation rolls the stack back | None. A script that fails halfway leaves half a lab |
| Student credentials | The session hands back temporary AWS keys | The session hands back none. A token must be made inside a live session |
| Denied access | `AccessDenied`, with the action in the message | The object looks missing. "Not found" can mean "not allowed" |
| Workspace | Vocareum assigns an account from a pool | Vocareum creates a workspace per part. The Databricks account tier caps how many can be active |

What a Databricks lab needs:

- **Account admin for Vocareum:** Vocareum's service principal holds the Databricks Account admin role. It uses that role to create workspaces and assign users.
- **Hooks that report their own failures:** A failing script must exit non-zero and write an `error_code` and a `message`. Otherwise the lab fails with no stated reason.
- **Shared work once, student work per student:** `workspace_init.sh` gets no user or group, so it builds only what every student shares. Per-student compute and notebooks go in `user_setup.sh`, the only required script.
- **Idempotent setup:** `workspace_init.sh` runs once per workspace, and one course can have several workspaces. Every step must be safe to run again.
- **Careful cleanup:** `lab_end.sh` must read the end-lab behavior. On `stop`, it keeps the student's work and removes only billable compute. On `terminate`, it can remove everything.
- **The right workspace:** The tools must point at the workspace Vocareum created, not one built by hand. Results from any other workspace describe a lab no student enters.
- **A path to Neo4j:** Vocareum opened port 7687 so clusters can reach Neo4j Aura over Bolt. Neo4j runs in Aura, not inside the workspace.

## Slide 21: Demo

The demo shows an agent running the lifecycle. Each step is safe to run on screen.

1. **Offline self-test:** Run `uv run vocareum-probe --self-test`. It checks the classifier with no AWS call.
2. **Release preview:** Run `uv run vocareum-release --course graphrag`. It validates and builds the package without calling the API.
3. **Ship gate:** Run `uv run vocareum-expect --no-start --course graphrag` against an open lab. Show the exit code.
4. **Sweep preview:** Run `uv run vocareum-sweep --course graphrag`. It lists leftovers and deletes nothing.
5. **Agent prompt:** Ask Claude Code, "Is the graphrag lab healthy, and what would you clean up?" Show it choose the commands from `CLAUDE.md`.

## Slide 22: How to adopt the pattern

- **Start with the guards:** Refuse to report until the account and region are confirmed.
- **Make every tool preview first:** Let the agent look before it acts.
- **Answer with exit codes:** Let the agent branch on results, not parse prose.
- **Write down why:** Put the reasoning in the code, so the agent keeps it.
- **Keep one source of truth:** Record each measured result in one file and link to it.

## Slide 23: Questions

---

## Speaker notes

- **Hide real identifiers:** Do not show `.env`, `courses/*.env`, or `support/`. They hold real account, organization, and policy identifiers. Use placeholders on slides.
- **Share both repositories:** Point attendees to this repository for the tooling. Point them to the public workshop repository for the course itself.
- **Plan the live lab:** Starting a lab spends one launch from the budget. Open a session before the webinar, and use `--no-start` during the demo.
