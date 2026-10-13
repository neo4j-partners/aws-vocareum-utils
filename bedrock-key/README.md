# Bedrock API key operations

This directory creates and operates the shared Bedrock bearer key used by the
workshop. One `uv` command covers key creation, status, spend, stop and resume,
breaker rehearsals, logs, and teardown:

```bash
uv run bedrock-key/admin.py --help
```

The bearer secret is written to `bedrock-key/bedrock-key.env`, which is
gitignored and created with mode `0600`. No command prints it.

## Quick start

You need [`uv`](https://docs.astral.sh/uv/) and an AWS profile that can manage
IAM, CloudFormation, CloudWatch, SNS, Lambda, Bedrock logging, and Cost Explorer
in the instructor account.

1. Create the local configuration on a fresh checkout:

   ```bash
   cp bedrock-key/arns.env.example bedrock-key/arns.env
   ```

2. Edit `bedrock-key/arns.env`. Replace every `000000000000`, set
   `BEDROCK_KEY_ADMIN_PROFILE`, and set `BEDROCK_KEY_ALERT_EMAIL`. Keep the other
   defaults unless the workshop uses different models or resource names.

3. Preview and create the IAM user, scoped policy, and 14-day key:

   ```bash
   uv run bedrock-key/admin.py create --dry-run
   uv run bedrock-key/admin.py create
   ```

4. Verify bearer authentication with one small model call:

   ```bash
   uv run bedrock-key/admin.py test-key
   ```

5. Preview and deploy the spend controls, then confirm the email subscription:

   ```bash
   uv run bedrock-key/admin.py deploy --dry-run
   uv run bedrock-key/admin.py deploy
   ```

Deploy requests an SNS confirmation when the configured email subscription is
missing or deleted. In that email, copy the `Token` value from the confirmation
link without opening the link, then run:

```bash
uv run bedrock-key/admin.py subscriptions --confirm-token 'TOKEN'
uv run bedrock-key/admin.py subscriptions
```

The authenticated confirmation prevents unauthenticated unsubscribe links from
deleting the subscription. The breaker can deactivate the key without a
confirmed email subscription, but the operator will receive no alert.

## Admin commands

Run every command from the repository root.

| Command | Purpose |
| --- | --- |
| `uv run bedrock-key/admin.py create` | Create or verify the IAM user, apply `policy.json`, and issue the key. |
| `uv run bedrock-key/admin.py deploy` | Deploy the controls and enable model invocation logging. |
| `uv run bedrock-key/admin.py status` | Show the user, policy statement IDs, every Bedrock key, and controls stack state. |
| `uv run bedrock-key/admin.py cost` | Show month-to-date account cost grouped by AWS service. |
| `uv run bedrock-key/admin.py stop` | Deactivate the configured key and wait 30 seconds for propagation. |
| `uv run bedrock-key/admin.py pause` | Alias for `stop`. |
| `uv run bedrock-key/admin.py resume` | Reactivate the key and wait 15 seconds for propagation. |
| `uv run bedrock-key/admin.py subscriptions` | Verify that the breaker email is confirmed and protected. |
| `uv run bedrock-key/admin.py subscriptions --repair` | Replace a missing, deleted, or unprotected email subscription. |
| `uv run bedrock-key/admin.py subscriptions --confirm-token TOKEN` | Confirm an emailed token with authenticated unsubscribe protection. |
| `uv run bedrock-key/admin.py alarm burst` | Rehearse the end-to-end spend breaker and report key status. |
| `uv run bedrock-key/admin.py alarm total` | Rehearse the rolling-total breaker and report key status. |
| `uv run bedrock-key/admin.py alarm failure` | Rehearse the notification-only breaker failure alarm. |
| `uv run bedrock-key/admin.py logs` | Read recent breaker Lambda logs. Add `--follow` to continue. |
| `uv run bedrock-key/admin.py invoke-breaker --yes` | Invoke the breaker Lambda directly and deactivate the key. |
| `uv run bedrock-key/admin.py delete-key --yes` | Permanently delete the configured key. |
| `uv run bedrock-key/admin.py teardown --yes` | Delete the key, invocation logging configuration, and controls stack. |

Use `--profile PROFILE` before the subcommand to override the profile in
`arns.env`:

```bash
uv run bedrock-key/admin.py --profile instructor-admin status
```

Useful command-specific options:

```text
create --credential-age-days DAYS   Key lifetime; default: 14
create --output PATH                Ignored secret output path
create --dry-run                    Validate without calling AWS or writing files
deploy --dry-run                    Validate and print without deploying
subscriptions --repair              Request a replacement confirmation email
subscriptions --confirm-token TOKEN Confirm with unsubscribe protection
cost --start YYYY-MM-DD             Cost range start; default: first of this month
cost --end YYYY-MM-DD               Exclusive end; default: tomorrow
stop|pause|resume --no-wait         Skip the propagation countdown
alarm TYPE --wait SECONDS           Wait before reading key status; default: 20
logs --since-minutes MINUTES        Log window; default: 30
```

The script guards every AWS operation against `BEDROCK_KEY_ACCOUNT_ID`. It
refuses to create a second Bedrock credential, refuses secret output that Git
does not ignore, and requires `--yes` for permanent deletion or direct breaker
invocation.

## Email subscription recovery

The spend breaker and email delivery are independent SNS subscriptions. The key
can be deactivated successfully even when the email endpoint is deleted or still
awaiting confirmation.

Check the configured endpoint:

```bash
uv run bedrock-key/admin.py subscriptions
```

A healthy result ends with:

```text
email status: confirmed and protected from unauthenticated unsubscribe
```

Any other status exits unsuccessfully and must be repaired before class:

| Status | Meaning |
| --- | --- |
| `Deleted` | The endpoint was unsubscribed and receives no alarm mail. |
| `PendingConfirmation` | The endpoint has not been confirmed and receives no alarm mail. |
| `confirmed but unauthenticated` | Mail works, but an unauthenticated unsubscribe link can delete it again. |

Request or resend the confirmation email:

```bash
uv run bedrock-key/admin.py subscriptions --repair
```

Do not open the confirmation link. Copy only its `Token` query-string value. For
a link shaped like `...?TopicArn=...&Token=abc123&Endpoint=...`, copy `abc123`
and run:

```bash
uv run bedrock-key/admin.py subscriptions --confirm-token 'abc123'
uv run bedrock-key/admin.py subscriptions
```

The script confirms through the signed administrator session with
`AuthenticateOnUnsubscribe=true`. This prevents email security scanners or other
unauthenticated clients from removing the subscription through its unsubscribe
link. Confirmation tokens expire after two days. If the token is expired or the
email is missing, run `subscriptions --repair` again.

After confirmation, test delivery without invoking the key-deactivation Lambda:

```bash
uv run bedrock-key/admin.py alarm failure
```

The failure alarm should send email and leave an active key active. If the key is
already inactive from a spend-alarm rehearsal, restore the class threshold and
resume it before running this test.

## Before class

Do this the day before:

```bash
uv run bedrock-key/admin.py status
uv run bedrock-key/admin.py cost
uv run bedrock-key/admin.py deploy --dry-run
uv run bedrock-key/admin.py deploy
uv run bedrock-key/admin.py subscriptions
```

Rehearse all alarm paths:

```bash
# The spend alarm must deactivate the key.
uv run bedrock-key/admin.py alarm burst
uv run bedrock-key/admin.py resume

# The rolling-total alarm must also deactivate the key.
uv run bedrock-key/admin.py alarm total
uv run bedrock-key/admin.py resume

# The failure alarm must send email and leave the key Active.
uv run bedrock-key/admin.py alarm failure
uv run bedrock-key/admin.py status
```

Open the CloudWatch dashboard named `graphrag-bedrock-controls` in `us-east-1`
and leave it visible during class. The widgets show estimated dollars per
minute, tokens by model, and calls, throttles, and client errors. Read the
rolling-total alarm itself for the four-hour control.

Normal human-driven workshop traffic is uneven. A runaway is usually a flat,
sustained spend line or a call rate that keeps climbing. The default burst
alarm trips after one minute above `$30`. The sustained alarm trips after three
consecutive five-minute periods above `$25`.

## Stop and resume

Stop the key immediately when spend is unexplained or the breaker reports a
failure:

```bash
uv run bedrock-key/admin.py stop
```

IAM records the change immediately, but Bedrock takes about 12 seconds to honor
it. The command waits 30 seconds and verifies IAM state before reporting
success. While the key is inactive, every student sharing it receives an
authentication or access-denied error.

Tell the class:

> The shared Bedrock key is temporarily off. Leave `CONFIG.txt` and the kernel
> unchanged. Do not paste the key again. I will tell you when to retry the
> failed cell.

Find and stop the loop before resuming:

```bash
uv run bedrock-key/admin.py logs --since-minutes 30
uv run bedrock-key/admin.py resume
```

Resume takes about 10 seconds to propagate. The command waits 15 seconds before
reporting that students can retry.

## After a breaker alarm

1. Read which alarm fired. `-burst-spend` means one minute above the burst
   threshold. `-sustained-spend` means sustained high spend.
   `-workshop-total-spend` means the trailing four-hour estimate crossed the
   configured total.
   `-breaker-failed` means the Lambda errored and the key may still be active.
2. Confirm the actual state with `uv run bedrock-key/admin.py status`.
3. If the key is active or uncertain, run `uv run bedrock-key/admin.py stop`.
4. Inspect `uv run bedrock-key/admin.py logs` and the invocation log group named
   by `BEDROCK_KEY_INVOCATION_LOG_GROUP` in `arns.env`.
5. Fix the cause, then resume deliberately.

There is no automatic recovery. Automatically reactivating the key would restart
the same runaway that caused the stop.

## End of class

Deactivate the key as soon as the session ends:

```bash
uv run bedrock-key/admin.py stop
```

After the workshop is confirmed complete, permanently delete the key, the
account-wide invocation logging configuration, and the controls stack:

```bash
uv run bedrock-key/admin.py teardown --yes
```

The stack owns the invocation log group and its student-authored prompts and
responses. Export anything needed for a postmortem before teardown.

## Rotate a key

The creator refuses to leave two Bedrock credentials on the workshop user,
because the breaker targets only the ID stored in `arns.env`. Rotation therefore
has a short, deliberate outage:

```bash
uv run bedrock-key/admin.py stop
uv run bedrock-key/admin.py delete-key --yes
uv run bedrock-key/admin.py create
uv run bedrock-key/admin.py test-key
uv run bedrock-key/admin.py deploy
uv run bedrock-key/admin.py alarm burst
uv run bedrock-key/admin.py resume
```

Creating a key updates its credential ID and expiration in the ignored
`arns.env`. Redeploying moves the new ID into the breaker Lambda configuration.

## Change spend thresholds

Edit these values in `arns.env`, then rerun
`uv run bedrock-key/admin.py deploy`:

| Variable | Meaning |
| --- | --- |
| `BEDROCK_KEY_BURST_USD_PER_MINUTE` | Trips on one minute above this value. |
| `BEDROCK_KEY_SUSTAINED_USD_PER_MINUTE` | Trips after consecutive periods above this value. |
| `BEDROCK_KEY_SUSTAINED_PERIODS` | Number of five-minute periods. |
| `BEDROCK_KEY_TOTAL_USD` | Trips when estimated spend in the trailing four hours exceeds this value. |

The deploy validation requires a positive total and a positive sustained
threshold below the burst threshold. `CONTROLS.md` explains the metric math and
current values.

### Test the total threshold with real usage

Use the template's minimum value of one millionth of a dollar. Any normal chat
call costs more than this:

```bash
# In bedrock-key/arns.env:
BEDROCK_KEY_TOTAL_USD=0.000001

uv run bedrock-key/admin.py deploy
uv run bedrock-key/admin.py test-key
```

If the workshop models already have usage in the trailing four hours, deployment
can trip the alarm before `test-key` runs. Allow about two minutes, then verify:

```bash
uv run bedrock-key/admin.py status
uv run bedrock-key/admin.py logs --since-minutes 10
```

Restore the class value before reactivating the key. For an intended maximum of
about `$1,000`, the default `$850` trip point leaves `$150` for delayed metrics
and in-flight requests:

```bash
# In bedrock-key/arns.env:
BEDROCK_KEY_TOTAL_USD=850

uv run bedrock-key/admin.py deploy
uv run bedrock-key/admin.py resume
```

Do not resume while the tiny threshold is deployed. The total is a rolling,
estimated four-hour control rather than a guaranteed billing cap. It includes
all account usage of the three configured model IDs, just like the rate alarms.

## Files

| File | Purpose |
| --- | --- |
| `admin.py` | Unified operator CLI. |
| `create-key.py` | Secret-safe key creation implementation used by `admin.py create`. |
| `policy.json` | Model-scoped inline policy attached to the key user. |
| `arns.env` | Ignored local configuration and current credential metadata. |
| `bedrock-key.env` | Ignored bearer secret and runtime variables, mode `0600`. |
| `deploy-controls.sh` | Compatibility wrapper for `admin.py deploy`. |
| `controls.template` | CloudFormation alarms, breaker, dashboard, logging, and budget. |
| `CONTROLS.md` | Detailed spend-control design and threshold arithmetic. |
| `KEY-LIFECYCLE.md` | Detailed key and breaker maintenance reference. |
| `stop-key.sh` / `resume-key.sh` | Compatibility wrappers for the admin CLI. |
