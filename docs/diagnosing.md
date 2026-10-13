# Diagnosing a failed lab

When a student clicks Start Lab and gets `Lab is in CREATE_FAILED state`, Vocareum tears the environment down within a few minutes. The AWS console is often unreachable before then, which leaves the CloudFormation error out of reach through the UI. `uv run vocareum-diagnose` recovers it from the API instead.

It works because deleted CloudFormation stacks stay listed for 90 days, and `describe-stack-events` accepts a deleted stack by its full StackId ARN even though it rejects the same stack by name.

For the API mechanics underneath the script, see [`vocareum-api.md`](vocareum-api.md).

## One-time setup

Copy the environment template and fill it in:

```bash
cp .env.example .env
```

| Variable | Where to get it |
| --- | --- |
| `VOCAREUM_API_KEY` | Vocareum account settings, Personal Access Token. It displays once, so capture it immediately |
| `VOCAREUM_COURSE_ID` | The info icon in the Vocareum UI on the course |
| `VOCAREUM_ASSIGNMENT_ID` | The info icon on the assignment |
| `VOCAREUM_PART_ID` | The info icon on the part |
| `VOCAREUM_USER_ID` | Your own Vocareum user ID. Instructors get a session too, so you can reproduce the failure against yourself instead of waiting for a student |
| `VOCAREUM_API_BASE` | Optional. Defaults to `https://api.vocareum.com/api/v2` |
| `AWS_REGION` | Optional. Defaults to `us-east-1` |

`.env` is gitignored. `.env.example` is committed and carries placeholders only. The identifiers recorded at the bottom of `.env.example` are read by nothing. They exist so the real account, organization, and SCP IDs live in one ignored place rather than scattered through committed documentation.

The script is part of the repository's [uv](https://docs.astral.sh/uv/) project. `pyproject.toml` declares the Python version and dependencies, and `uv.lock` pins the resolved environment. The first invocation builds that environment automatically.

## Running it

```bash
uv run vocareum-diagnose
```

Or, equivalently, `uv run vocareum-diagnose`.

That starts a lab session, follows the launch, pulls the session's temporary AWS credentials, finds the stack, and prints the first failing event.

| Flag | When to use it |
| --- | --- |
| `--no-start` | The session is already running, or you want to inspect a previous failure without burning a fresh lab launch |
| `--all-stacks` | Read events from every stack found instead of only the most recent |
| `--region REGION` | Override `AWS_REGION` |
| `--env-file PATH` | Point at a `.env` somewhere other than the repo root |

Reach for `--no-start` by default once you have a failure on record. Each run without it consumes a lab launch and counts against the budget.

Exit code 0 means it retrieved a reason or found a healthy stack. 1 means configuration is incomplete. 2 means the API or AWS calls could not complete.

## What it prints

Six labeled sections, in order:

1. **Start.** The `PUT` to the resources endpoint that launches the template and issues credentials.
2. **Transaction.** The poll on the returned transaction ID. Its `message` field is the first place a CloudFormation error can surface, and it is printed in full.
3. **Spend and credentials.** The dollars this user has spent on this part, against the allowance they spend against, then whether the session returned usable AWS credentials. The secret access key and session token are masked. Spend prints even when the credentials come back empty, since a terminated session still reports it.
4. **Stacks.** Every stack in the student account including deleted ones, newest first.
5. **Events.** The first event whose status ends in `FAILED`, with its complete `ResourceStatusReason`. Later failures are usually cascades from that one, so they are listed underneath in short form.
6. **Interpretation.** The failure reason matched against the known causes below.

## Two ways this reports the wrong run

Both of these produced a confident false answer, and both are now handled by the script. Read them before trusting a result taken by hand.

**A start call is not a finished stack.** The start call returns no `transactionid` when the session comes back in state `Starting`, so there is nothing to poll, and the stack takes roughly 90 seconds to appear. A script that lists stacks as soon as the start returns sees only the stack from the last run. That produced a false negative on 2026-07-25, where a template that had just succeeded was reported as still failing on the denial the previous run had hit.

The script now stamps the launch instant before the start call and then polls, for up to five minutes, until a stack with a later `CreationTime` shows up. `CreationTime` is stamped by CloudFormation's clock and the launch instant by yours, so the comparison allows two minutes of skew. If nothing newer arrives before the timeout it prints a warning and carries on rather than exiting, because the older stack is still worth reading. Believe nothing under that warning without checking `CreationTime` yourself.

**Only the newest stack decides the verdict.** Older stacks are read and printed, because their history is worth having, but each is labeled `older, context only` and none can reach the RESULT. Deleted stacks stay listable for 90 days, so without this rule a denial from a stack that failed days ago outranks a current success. That happened on 2026-07-27: a run whose newest stack was `CREATE_COMPLETE` was reported as blocked by an SCP denial that a stack from two days earlier had hit. Read `CreationTime` on each stack header to see which run you are looking at.

## Reading the result

This table mirrors `INTERPRETATIONS` in `src/vocareum_tools/diagnose.py`. Change both together.

| Reason contains | Cause | Fix |
| --- | --- | --- |
| `CreateTags` | Tag-on-create denied by the lab policy | Move tags out of the creation call |
| `explicit deny in a service control policy` | A Service Control Policy in Vocareum's AWS Organization forbids the action outright. This is the confirmed cause of the first `CREATE_FAILED` | Stop using the denied action, or ask Vocareum to change the SCP. Editing IAM cannot help. See below |
| `UnauthorizedOperation`, `AccessDenied` | The lab policy is missing a permission the template needs, and the message says nothing about a service control policy | Read the denied action out of the message and either grant it or drop the resource |

The table is ordered, so `CreateTags` and the service control policy rule both win over the generic `UnauthorizedOperation` rule rather than being masked by it.

### An SCP denial is not an IAM problem

An SCP sets a permission ceiling on the student account from Vocareum's organization management account. IAM inside the account can only subtract from that ceiling, never add to it. When the reason string contains `explicit deny in a service control policy`, none of the following will change the outcome:

- Adding the action to `lab.template`.
- Creating an IAM role in the template that grants the action.
- Requesting the same capability through a different resource type that still calls the denied API.

The two real options are to write the template so it never calls the denied action, or to ask Vocareum to modify the policy. The reason string names the policy, so quote its ARN in the support request.

## When it cannot help

Step 3 depends on the session living long enough to hand back credentials. Vocareum does tear the account down the instant CloudFormation fails, so the response arrives with no usable credentials and the script says so and exits non-zero. The transaction message from step 2 is the fallback. If that is also empty, the error is being swallowed on Vocareum's side and only their support can retrieve it.

**Empty credentials silently become your own credentials.** A terminated session returns empty strings for the access key, secret key, and session token rather than omitting them. boto3 treats empty credential strings as absent, falls back through its default provider chain, and picks up whatever ambient SSO credentials the operator has. A probe written this way ran cleanly and reported on an entirely different AWS account. Anything that consumes Vocareum session credentials has to assert all three values are non-empty before constructing a client, and confirm the account ID with `sts:GetCallerIdentity` before trusting any result. The practical consequence is that a failed student account cannot be probed interactively at all. Mapping what the account permits requires a stack that succeeds.

**An empty stack list may not be a script problem.** The script assumes the failed stack lives in the account the current session hands you credentials for. If Vocareum assigns a different AWS account per session rather than pinning one account per student for the course, the failed stack sits in an account those credentials no longer reach, and `list-stacks` returns nothing even though every call succeeded. The script reports a region or account mismatch in that case. Pinning is the likelier behavior on current evidence.

The recovery is CloudTrail. Find the `CreateStack` event in whichever account ran the template, take the stack ID from it, and query `describe-stack-events` by that ARN. Reaching CloudTrail requires credentials for that account, so this only works if the account is still assigned to someone you can launch as.

Two other API paths look promising and are not. Data Pipeline is aggregate reporting with no failure codes, and batch script presumes an environment that already came up. [`vocareum-api.md`](vocareum-api.md) records why each one is a dead end.

## References

- [AWS: DescribeStackEvents](https://docs.aws.amazon.com/AWSCloudFormation/latest/APIReference/API_DescribeStackEvents.html): why a deleted stack requires the StackId ARN
- [AWS: CloudFormation stack status codes](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-cfn-describing-stacks.html)
