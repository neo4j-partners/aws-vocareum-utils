# Webinar proposal: Letting coding agents run a Vocareum cloud lab course

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

## Slide 1: Title

**Letting coding agents run a Vocareum cloud lab course**

Speaker: Ryan Knight. Co-presented with Vocareum.

## Slide 2: The opportunity

- **Each student gets an AWS account:** Vocareum gives every student their own AWS account. At Start Lab, it runs a CloudFormation template in that account.
- **A real account means real power:** Students build with real AWS services, such as Bedrock and AgentCore. The course author decides exactly what each account can do.
- **Every step can be automated:** Vocareum's API covers publishing, sessions, and rosters. AWS covers everything inside the account.
- **Agents can do the operational work:** With the right tools, a coding agent can build, check, and run a course end to end.

## Slide 3: Pick the tool that fits your needs

- **To publish a course:** [vocareum-publisher](https://github.com/ddlin/vocareum-publisher), also called `vocgit`, is ideal. It syncs files, settings, and rubrics from Git.
- **To build and run the whole cloud lab lifecycle:** This repository goes beyond publishing. It gives agents the tools to build, test, publish, run, inspect, probe, and clean up the lab environment in each student's AWS account.

## Slide 4: The goal is agent-driven operations

- **The agent is the operator:** The primary user of these tools is a coding agent. A person sets the goal and approves the risky steps.
- **The repository is the agent's manual:** `CLAUDE.md` tells the agent which commands exist, which rules matter, and why.
- **The docstrings carry the reasoning:** Most modules record the failure that shaped their design. That stops an agent from "simplifying" away a guard that exists for a reason.
- **Every answer is checkable:** Tools exit with codes an agent can branch on. Several also emit `--json`.

## Slide 5: What makes a tool safe for an agent

- **Preview by default:** Commands that change state do nothing without a flag such as `--publish`, `--delete`, or `--yes`.
- **Three guards before any report:** The credentials must be non-empty, the account must match, and the region must be `us-east-1`. A confident answer about the wrong account is worse than no answer.
- **Exit codes as answers:** For example, `vocareum-expect` exits `0` when the lab is ready, `3` when it is not, and `2` when a guard refused.
- **Proof, not trust:** Every upload is downloaded back and compared by hash. A write that returned is not a write that finished, so deletes are polled until they are gone.
- **Offline tests:** More than 800 offline tests run with no AWS or Vocareum call. `./scripts/validate_all.py` is the full gate.

## Slide 6: The course lifecycle, one command per step

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

## Slide 7: Inspecting and probing the account

- **Probe one action:** `vocareum-probe --probe bedrock:ListFoundationModels` names an IAM action and returns a verdict.
- **Tell two kinds of deny apart:** An organization-level deny cannot be lifted inside the account. An ordinary IAM shortfall can be fixed in `lab.template`. AWS reports both as `AccessDenied`, so the tool reads the message body.
- **Explain a failed stack:** `vocareum-diagnose` recovers the CloudFormation failure reason without console access.
- **Measure stateful features:** The phase runners create a resource, use it, and delete it. Each delete is recorded as a measurement.
- **One record of results:** `docs/permissions.md` is the only place a permission result is written down.

## Slide 8: Demo

The demo shows an agent running the lifecycle. Each step is safe to run on screen.

1. **Offline self-test:** Run `uv run vocareum-probe --self-test`. It checks the classifier with no AWS call.
2. **Release preview:** Run `uv run vocareum-release --course graphrag`. It validates and builds the package without calling the API.
3. **Ship gate:** Run `uv run vocareum-expect --no-start --course graphrag` against an open lab. Show the exit code.
4. **Sweep preview:** Run `uv run vocareum-sweep --course graphrag`. It lists leftovers and deletes nothing.
5. **Agent prompt:** Ask Claude Code, "Is the graphrag lab healthy, and what would you clean up?" Show it choose the commands from `CLAUDE.md`.

## Slide 9: Tips for building on the platform

- **Send content as a JSON array:** The part update takes `content` as an array. The tools build that shape for you.
- **Put grants in the template:** Every permission is an `AWS::IAM::RolePolicy` inside `lab.template`. CloudFormation applies these grants at Start Lab.
- **Verify after you publish:** `--verify-live` compares the live course with the release. It is cheap, so run it again any time.
- **Plan for account pools:** Each session can land in a different pooled account. Sweep leftovers before you end a session, and refresh the account pin after a new one starts.
- **Wait for the stack:** Credentials appear before the template's grants are applied. Checks wait for the stack to finish before they run.

## Slide 10: How to adopt the pattern

- **Start with the guards:** Refuse to report until the account and region are confirmed.
- **Make every tool preview first:** Let the agent look before it acts.
- **Answer with exit codes:** Let the agent branch on results, not parse prose.
- **Write down why:** Put the reasoning in the code, so the agent keeps it.
- **Keep one source of truth:** Record each measured result in one file and link to it.

## Slide 11: Questions

---

## Speaker notes

- **Hide real identifiers:** Do not show `.env`, `courses/*.env`, or `support/`. They hold real account, organization, and policy identifiers. Use placeholders on slides.
- **Mind the repository access:** This repository is private. Point attendees to the public workshop repository, or decide before the webinar whether to open this one.
- **Plan the live lab:** Starting a lab spends one launch from the budget. Open a session before the webinar, and use `--no-start` during the demo.
