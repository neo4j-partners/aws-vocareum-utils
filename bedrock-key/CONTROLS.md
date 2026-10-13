# The spend circuit breaker

Reference for `controls.template` and the controls deployed through
`uv run bedrock-key/admin.py deploy`. `README.md` is the quick start and
class-day runbook. This file explains why the breaker is built the way it is.

## What it protects against

Forty students on Vocareum lab accounts share one Bedrock credential issued by
instructor account `${BEDROCK_KEY_ACCOUNT_ID}`. Some pooled lab accounts are not
entitled to the Anthropic models, so the class uses a shared long-term Bedrock
API key, delivered to every student as `AWS_BEARER_TOKEN_BEDROCK`. The credential id
every control here is pinned to lives in `BEDROCK_KEY_CREDENTIAL_ID` in
`arns.env`. No key exists at present. `KEY-LIFECYCLE.md` issues one.

That design has no per-student quota and no per-student attribution. One `while
True` loop in one notebook bills the instructor account at the account's full
Bedrock rate. A leaked token does the same thing from outside the class.

The breaker is the fast control. CloudWatch alarms measure estimated spend rates
and the rolling four-hour total, publish to SNS, and a Lambda sets that one
credential to `Inactive`.
Every student stops at once. That is the intended blast radius. Stopping forty
people for two minutes is cheaper than an unattended runaway.

## Every resource the stack creates

Stack name `graphrag-bedrock-controls` in `us-east-1`. Fifteen resources.

**`BreakerTopic`** is the SNS topic all four alarms publish to. It is named
`graphrag-bedrock-controls-breaker`.

**`BreakerTopicPolicy`** lets `cloudwatch.amazonaws.com` publish to that topic,
and only from this account.

The email subscription is managed by `admin.py`, not CloudFormation. Deploy
creates it when missing, `subscriptions --repair` replaces a deleted or
unprotected subscription, and `subscriptions --confirm-token TOKEN` confirms it
with authenticated unsubscribe protection. It mails `BEDROCK_KEY_ALERT_EMAIL`
when an alarm fires. See **The manual step** below.

**`BreakerFunctionLogGroup`** is `/aws/lambda/graphrag-bedrock-controls-breaker`,
created explicitly so the execution role can name one log group instead of every
group in the account. Retention is 14 days.

**`BreakerFunctionRole`** grants exactly two things. The first is
`iam:UpdateServiceSpecificCredential` on
`arn:aws:iam::${BEDROCK_KEY_ACCOUNT_ID}:user/workshop/bedrock-api-user`, conditioned on
`iam:ServiceSpecificCredentialServiceName` equal to `bedrock.amazonaws.com`. The
second is `logs:CreateLogStream` and `logs:PutLogEvents` on its own log group.
IAM publishes no condition key for a credential id, so one action on one user for
one service name is the tightest grant the policy language can express. The
credential id is pinned in the function's environment instead.

**`BreakerFunction`** is the Python 3.13 Lambda that makes the IAM call. It reads
`KEY_USER_NAME` and `KEY_CREDENTIAL_ID` from its environment and never from the
event, so a malformed or spoofed SNS message cannot point it at a different
credential. It catches no IAM error on purpose. A failed deactivation surfaces as
a Lambda error rather than a success that did nothing.

**`BreakerFunctionInvokePermission`** lets `sns.amazonaws.com` invoke the
function, restricted to `BreakerTopic` as the source.

**`BreakerFunctionSubscription`** subscribes the Lambda to the topic. It declares
`DependsOn` the permission, so the topic never holds a subscription it is not yet
allowed to deliver to. It carries a message-body filter policy naming the three
spend alarms and only those three. That filter is what keeps
`BreakerFailureAlarm` from invoking the breaker. All names are generated from
the alarm resources, so the filter cannot drift away from the alarms it names.

**`BurstSpendAlarm`** is `graphrag-bedrock-controls-burst-spend`. One minute above
`BEDROCK_KEY_BURST_USD_PER_MINUTE` trips it. The default is `$30`.

**`SustainedSpendAlarm`** is `graphrag-bedrock-controls-sustained-spend`.
`BEDROCK_KEY_SUSTAINED_PERIODS` consecutive five-minute periods, each averaging
above `BEDROCK_KEY_SUSTAINED_USD_PER_MINUTE`, trip it. The defaults are three
periods and `$25`, which is fifteen minutes.

**`WorkshopTotalSpendAlarm`** is
`graphrag-bedrock-controls-workshop-total-spend`. It sums the same price-weighted
token metrics over a sliding four-hour window and trips above
`BEDROCK_KEY_TOTAL_USD`. The default is `$850`, leaving `$150` of headroom below
an intended `$1,000` maximum for delayed metrics and in-flight calls.

**`BreakerFailureAlarm`** is `graphrag-bedrock-controls-breaker-failed`. It
watches the breaker Lambda's `Errors` metric and fires on any error in a
60-second period. It mails the operator and takes no other action. It cannot
reach the Lambda, because the Lambda's subscription filter names only the three
spend alarms.

**`SpendDashboard`** is the class-day view named `graphrag-bedrock-controls`.
Three widgets: estimated USD per minute with both rate thresholds, tokens per
minute by model, and calls with throttles and client errors. The rolling total is
read from `WorkshopTotalSpendAlarm`, because dashboard metric periods are not
sliding alarm windows.

**`InvocationLogGroup`** is `/aws/bedrock/graphrag-workshop-invocations`. Bedrock
writes prompts and completions here verbatim. Retention is 14 days.

**`InvocationLogRole`** is the role Bedrock assumes to write that group. Its trust
policy is confused-deputy guarded with `aws:SourceAccount` and `aws:SourceArn`.

**`BackstopBudget`** is the `$800` monthly cost budget. It sends mail and nothing
else.

One control is not in the stack. `PutModelInvocationLoggingConfiguration` has no
CloudFormation resource type, so `admin.py deploy` makes that call after the
stack settles.

## The metric math

All three spend alarms evaluate the same price-weighted token expression over
four dimension-pinned metrics.

```
(FILL(chat_in,0)  * 3
 + FILL(chat_out,0) * 15
 + FILL(embed_in,0) * 0.135
 + FILL(memory_in,0) * 0.02) / 1000000
```

In words: take the per-period sum of input tokens for the chat model, output
tokens for the chat model, input tokens for the retrieval embedding model, and
input tokens for the memory embedding model. Multiply each by that model's
published dollars per million tokens. Add them. Divide by a million to get
dollars. The burst alarm reads 60-second periods and divides by `1000000`. The
sustained alarm reads 300-second periods and divides by `5000000`, so both
rate thresholds are read in the same unit of dollars per minute. The total alarm
reads a sliding 14,400-second period and divides by `1000000`, yielding dollars
over the trailing four hours.

Every metric names `ModelId` as a dimension. The account carries unrelated
Sonnet 5 traffic, so the account-wide rollup would count money this workshop did
not spend. Cross-region inference publishes under `us.anthropic.claude-sonnet-4-6`
in `us-east-1` only, so one alarm in one region sees the whole chat spend.

`FILL(x,0)` turns a period with no datapoint into a zero. It also handles a
metric with no datapoints at all, which matters because the memory embedding
model is only used in Module 6 and may publish nothing for most of the class.
Verified on 2026-08-25 with `cloudwatch:GetMetricData` against three model ids
that have never published a metric: the expression returned a full 360-point
zero series rather than an empty one. The alarms therefore never sit in
`INSUFFICIENT_DATA` for want of an embedding metric.

All three alarms set `TreatMissingData: notBreaching`, so a genuinely empty window
cannot trip the breaker during a quiet moment.

### Why token counts and not Invocations

The burn is denominated in tokens. One `InvokeModel` call can carry two hundred
tokens or two hundred thousand, so an alarm on `Invocations` has no fixed
relationship to money.

A raw token count is closer and still wrong. Claude Sonnet 4.6 output costs `$15`
per million tokens. Titan Text Embeddings V2 input costs `$0.02` per million.
That is a ratio of 750 to 1. A token-count threshold sized for chat would never
see an embedding runaway. A token-count threshold sized for embeddings would trip
on the Module 1 ingest burst, which is legitimate work costing under a cent per
minute.

Price weighting puts the threshold, the dashboard, and the budget in one unit.

## The spend thresholds

The legitimate peak is an estimate. Every per-call figure below was assumed, not
measured. The prices and the quotas were measured.

```
Per chat model call        6,000 input tokens, 800 output tokens
Model calls per question   4, covering tool selection, retrieval, and answer
Questions per student      3 per minute, sustained

Per student per minute   4 x 3 x 6,000 =    72,000 input tokens
                         4 x 3 x   800 =     9,600 output tokens
40 students              40 x 72,000   = 2,880,000 input tokens
                         40 x  9,600   =   384,000 output tokens

Chat input     2.880 M x $3     per M = $ 8.64
Chat output    0.384 M x $15    per M = $ 5.76
Nova embeds    0.600 M x $0.135 per M = $ 0.08
Titan embeds   0.300 M x $0.02  per M = $ 0.006
--------------------------------------------------
Legitimate peak                         $14.49 per minute
```

**Burst, `$30` per minute over one 60-second period.** That is 2.07 times the
estimated peak. In call terms it is about 1,000 chat calls in one minute, because
the modelled call costs `$0.030`. Forty students would each have to sustain 25
model calls inside the same minute. Two sanity checks bound it from the other
side. The 6,000,000 TPM Sonnet quota caps an all-output minute at `$90`, so the
breaker fires at one third of the physical maximum. About 90 seconds pass between
the trip and the key going dead, which bounds a worst-case runaway near `$135`.

**Sustained, `$25` per minute over fifteen minutes.** A burn sitting just under
`$30` for a three-hour class costs `$5,400` and never touches the burst alarm.
The sustained alarm is what catches that. Fifteen minutes above `$25` trips after
roughly `$375` has been spent.

**Workshop total, `$850` over a sliding four-hour window.** This alarm sums the
same price-weighted token counts without dividing by minutes. A sliding window
advances on every evaluation, so the total does not reset at a wall-clock
boundary during class. The threshold is deliberately `$150` below the intended
`$1,000` maximum. CloudWatch metric publication, alarm delivery, IAM propagation,
and already-running calls can all add spend after the estimate crosses `$850`.
The margin reduces that exposure, but it cannot make this an invoice-grade hard
cap.

The total includes all account usage of the three configured model IDs. Bedrock's
token metrics do not expose the bearer credential ID as a dimension, so the
alarm cannot distinguish workshop-key calls from other calls to those exact
models in the account. That matches the scope of the two rate alarms and errs on
the side of stopping early.

An alarm that fires on a normal class is worse than no alarm. The operator
disables it mid-class and then nothing is watching.

### Why the sustained figure is `$25` and not `$15`

`$15` was the original number and it is too tight. Three things push it up.

The margin was 3.5%. `$15` sits `$0.51` above the `$14.49` estimated peak. The
peak is an estimate whose load-bearing input, three questions per student per
minute, was never measured. A 4% error in that one assumption puts a normal class
over the line.

Module 1 ingest is a second path to the same ceiling and the estimate does not
model it. At the 16,000-token extraction ceiling and `GRAPH_BUILD_CONCURRENCY=1`,
forty students starting the graph build together run near `$12.45` per minute.
That is 86% of the chat peak from a completely different code path. It does not
add to the chat peak, because a student running the build cell is not
simultaneously asking three questions a minute. It does mean two independent ways
to approach `$14.49` exist, rather than one.

Fifteen minutes is a long time to hold a rate. Module 1's build runs about four
minutes per student, and forty students do not start in lockstep. The realistic
worst case is a ragged fifteen minutes where the class-wide rate wanders between
ingest load and chat load without dropping.

```
Estimated chat peak                       $14.49 /min
Module 1 ingest at the token ceiling      $12.45 /min
Higher of the two, since they exclude
  each other per student                  $14.49 /min

Sustained threshold  1.7 x $14.49  =      $24.63 /min, rounded to $25
Burst threshold      2.07 x $14.49 =      $30.00 /min, unchanged
```

`$25` applies a 1.7x safety factor to the same peak the burst alarm scales by
2.07x. It leaves a `$5` gap below the burst line, so the two alarms stay
distinguishable. An operator seeing the sustained alarm alone knows the rate never
crossed `$30`.

**What the change costs.** A sustained trip now happens after about `$375`
instead of `$225`. The exposure this adds is bounded and narrow. It is only a burn
that sits between `$15` and `$25` per minute for fifteen unbroken minutes.
Anything above `$30` per minute is still stopped by the burst alarm in about 90
seconds.

**The burst threshold stays at `$30`.** `GRAPH_BUILD_CONCURRENCY=8` multiplies the
ingest minute eightfold, to roughly `$99` per minute, which trips the burst alarm.
That is correct behavior. The fix for a class at concurrency 8 is to not run at
concurrency 8. It is not to raise the breaker's ceiling.

### Tuning the thresholds

All four values live in `arns.env`. Change them there and redeploy with
`uv run bedrock-key/admin.py deploy`. Never edit `controls.template`.

| Variable | Default | What raising it means |
| --- | --- | --- |
| `BEDROCK_KEY_BURST_USD_PER_MINUTE` | `30` | A one-minute runaway burns more before the key dies. Each `$1` here is about `$1.50` of extra worst-case exposure, because the trip takes about 90 seconds. |
| `BEDROCK_KEY_SUSTAINED_USD_PER_MINUTE` | `25` | A slow burn runs hotter before the key dies. Multiply the change by fifteen to get the extra spend before a trip. |
| `BEDROCK_KEY_SUSTAINED_PERIODS` | `3` | The slow burn runs longer before the key dies. Each period is five minutes. |
| `BEDROCK_KEY_TOTAL_USD` | `850` | The trailing four-hour estimate can rise further before the key dies. This is the setting to lower for a smaller workshop allowance. |

Validation guards stand between a typo and a deployed breaker. The deploy
command refuses an empty value, non-numeric values, a sustained threshold at or
above the burst threshold, and a non-positive total. `controls.template` carries
`MinValue` and `MaxValue` on all four values, so `0` and any negative are refused
there as well.

Raising a threshold on class morning to stop a false trip is a reasonable move.
Lowering one mid-class is not. Verify rate changes with the dashboard and total
changes with `WorkshopTotalSpendAlarm`.

## Why the budget is email only

Cost Explorer data refreshes every 8 to 12 hours. A budget cannot fire during a
four-hour class and is not meant to. It is the line you read the morning after.

Wiring stale cost data to an automatic deactivation would let yesterday's spend
switch off today's class. The budget has four notifications and all four go to
email. None of them touch `BreakerTopic`.

The `$800` figure is `$400` of estimated legitimate class spend, plus `$270` for
two independent runaways at the `$135` ceiling, plus `$50` of estimated instructor
rehearsal, rounded up. The filter names two Cost Explorer service values and
covers the whole account, so unrelated work under either value counts toward it.
Read the month-to-date baseline before class.

## What happens when the breaker fires

1. Estimated spend in one 60-second period crosses `$30`.
2. CloudWatch closes the period and evaluates the alarm. Expect a few seconds to
   most of a minute, depending on where in the period the spend landed.
3. The alarm enters `ALARM` and publishes one message to `BreakerTopic`.
4. SNS fans out to two subscriptions at once. One is the email. One is the
   Lambda.
5. The Lambda calls `iam:UpdateServiceSpecificCredential` with status `Inactive`.
   This takes well under a second.
6. IAM records `Inactive` immediately. Bedrock honors it 11.9 seconds later,
   measured on 2026-08-26.

**About 90 seconds from the start of the runaway minute to student calls
failing.** Most of that is the metric period itself. Students see an
authentication or access-denied error on any cell that calls the model.

No spend alarm has an `OKAction`. Bringing the key back is a human decision,
because an automatic recovery would restart the loop the breaker just stopped.

One failure mode is worth knowing. The email and the Lambda are independent
subscriptions. If the Lambda's IAM call fails, the spend alarm's email still
arrives, and an operator reading only that email would believe the key is off when
it is live.

`BreakerFailureAlarm` covers this. Any Lambda error produces a second email inside
about a minute, subject line
`graphrag-bedrock-controls-breaker-failed`. **Two emails means the key may still
be live.** Go and stop it by hand.

That alarm publishes to `BreakerTopic` like the other three, so it needs no second
email subscription. It cannot invoke the breaker, because the Lambda's
subscription filter policy names only the three spend alarms. A breaker that
re-triggered itself on its own failure would loop.

One email is not proof of success either. After any trip, read the status before
telling the class anything:

```bash
uv run bedrock-key/admin.py status
```

## Recovering during a live class

Fix the cause first. The breaker trips again on the same loop, and the second
trip costs another minute of burn.

```bash
uv run bedrock-key/admin.py resume
```

IAM records `Active` immediately. The script reads the status back and fails hard
if IAM disagrees. Bedrock honors the change 10.4 seconds later, measured on
2026-08-26. The script waits 15 seconds and then tells you to release the class.
**Under 30 seconds from typing the command to students being able to re-run their
cell.** Add the time it took you to diagnose the cause.

The alarm does not need resetting. A rate alarm returns to `OK` after spend
drops. The total alarm remains in `ALARM` until enough spend rolls out of its
four-hour window or its threshold is raised. Never resume while the total alarm
still reflects spend above its configured threshold.

## The manual step

Confirming the SNS email subscription is not optional. Deploy requests a new
confirmation when the configured endpoint is missing or deleted. Do not click
the link in the email. Copy only its `Token` query-string value and confirm it
through the authenticated admin session:

```bash
uv run bedrock-key/admin.py subscriptions --confirm-token 'TOKEN'
```

This confirmation sets `AuthenticateOnUnsubscribe=true`, so opening an
unauthenticated unsubscribe link cannot delete the subscription. If status shows
`Deleted`, `PendingConfirmation`, or an unauthenticated confirmation, repair it:

```bash
uv run bedrock-key/admin.py subscriptions --repair
```

The repair command requests a new confirmation email. Confirmation tokens expire
after two days. Check the final state before class:

```bash
uv run bedrock-key/admin.py subscriptions
```

The command succeeds only when the email row has an ARN and reports `confirmed
and protected from unauthenticated unsubscribe`. A pending or deleted email
subscription delivers nothing. The breaker still works, the Lambda still runs,
and the key still goes dead, but nobody is told.

## The estimates this rests on

Marked clearly, because none of them were measured against a real class.

**Three questions per minute per student is an estimate and it is the load-bearing
one.** It sets the `$14.49` peak, which sets both thresholds. Four model calls per
question, 6,000 input tokens, and 800 output tokens are estimates too. Eighty
questions per student across six modules is the estimate behind the budget.

That per-call shape describes a chat turn. It does not describe Module 1's graph
build, which is the one place in the workshop where a single cell fans out over
the model. `graph_builder.py` runs extraction with
`asyncio.gather` under a semaphore, and `graph_config.py` sets
`EXTRACTION_MAX_TOKENS = 16000`. One extraction call at that ceiling costs about
`$0.25`, roughly eight times the modelled chat call. The class-day path is the
additive build over five documents at about four minutes per student, so forty
students starting together produce roughly 50 extraction calls per minute. At the
full 16,000-token ceiling that is about `$12.45` per minute from ingest alone,
86% of the estimated chat peak and under both thresholds. It is the reason the
sustained threshold is `$25` rather than `$15`.

Two knobs make that worse and both are worth checking before class.
`GRAPH_BUILD_CONCURRENCY` defaults to `1` and accepts up to `8`, and it multiplies
the ingest minute directly. `prepare_graph.py` is the self-paced rebuild path and
ingests 30 or 300 documents rather than five. Neither belongs on the shared key
during class.

If the burst alarm false-positives, this is where it will come from. It will look
like a legitimate ingest minute, not like a loop.

## Testing the breaker without a live class

Do this the day before, not on the morning of. The first two tests below really
deactivate the key, which is the point. The failure-alarm test does not.

Force the alarm into `ALARM` and let the whole chain run:

```bash
uv run bedrock-key/admin.py alarm burst
uv run bedrock-key/admin.py resume
uv run bedrock-key/admin.py alarm total
```

Then check, in order. The credential should read `Inactive` within about 15
seconds. The Lambda's log group
`/aws/lambda/graphrag-bedrock-controls-breaker` should hold a line reading
`deactivated credential <BEDROCK_KEY_CREDENTIAL_ID>`. The alert email should
arrive.
The email is the part most likely to be missing. `subscriptions` identifies a
pending, deleted, or unprotected endpoint directly.

This rehearsal is the only end-to-end proof that the Lambda's message-body filter
policy passes a real alarm message. A filter that rejected everything would leave
the email working and the breaker silent, and nothing short of a real alarm
message shows that. Run it after every redeploy that touches the alarms.

Bring the key back with `uv run bedrock-key/admin.py resume`. A forced alarm
state clears itself at the next evaluation. `README.md` also gives the real-data
test that temporarily sets `BEDROCK_KEY_TOTAL_USD=0.000001`.

Test the failure alarm the same way, and confirm it stops at the inbox:

```bash
uv run bedrock-key/admin.py alarm failure
```

An email should arrive. The credential should stay `Active`, and the breaker
Lambda's log group should show no new invocation. A deactivated credential here
means the filter policy is wrong and the failure alarm is wired to the breaker.

To test only the Lambda, invoke it with an empty event. The handler ignores the
event payload entirely, so `{}` exercises the real IAM call:

```bash
uv run bedrock-key/admin.py invoke-breaker --yes
```

To check the metric math without touching the key, read the expression directly
with `cloudwatch get-metric-data` over a recent window. It should return one
point per period with no gaps.

## After 2026-08-30

The credential expires at `2026-08-30T02:03:19Z`. After that moment it cannot
authenticate, students get errors, and the breaker has nothing left to switch
off. Expiry is a backstop for a forgotten teardown. Do not rely on it.

The stack keeps running after expiry. The alarms keep evaluating, the budget keeps
mailing, and the invocation log group keeps student-authored text for 14 days.
Delete all of it once the class is confirmed over. `README.md` has the teardown
command.

**Issuing a replacement credential produces a new credential id, and the stack
must be redeployed with it.** `KEY_CREDENTIAL_ID` is pinned in the Lambda's
environment. Point the breaker at an id that no longer exists and the IAM call
returns `NoSuchEntity`, the Lambda errors, and the key stays live. The alert email
still arrives, so the failure looks like a success from the operator's chair.
Update `BEDROCK_KEY_CREDENTIAL_ID` in `arns.env` and rerun
`uv run bedrock-key/admin.py deploy` before trusting the breaker again.

## Secrets

Nothing in this stack handles the 128-character bearer token. The Lambda logs the
credential id and the user name, both of which are identifiers rather than
secrets. Alarm descriptions, SNS messages, metric dimensions, and stack outputs
carry no credential material.

Model invocation logging is the one place to keep watching. It records request and
response bodies verbatim, so
`/aws/bedrock/graphrag-workshop-invocations` holds student-authored prompt and
completion text. It does not capture request headers, and the bearer token
travels in a header. Two consequences are worth stating. Invocation logging is
account-wide and region-wide, so unrelated Bedrock work in `${BEDROCK_KEY_ACCOUNT_ID}` lands in
that group too. Every record's `identity.arn` is the one shared key user, so the
log tells you what ran away and never who ran it.
