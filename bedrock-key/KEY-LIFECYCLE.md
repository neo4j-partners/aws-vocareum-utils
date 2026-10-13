# Key lifecycle and breaker maintenance

Use the unified admin CLI for every operator action:

```bash
uv run bedrock-key/admin.py --help
```

`README.md` is the quick start and class-day runbook. This file records the
ordering and safety rules for creation, rotation, breaker maintenance, and
deletion.

## Invariants

- The IAM user may have only one Bedrock service-specific credential. The
  creator refuses a second credential because the breaker can deactivate only
  the ID configured in `arns.env`.
- The one-time bearer secret is stored only in the ignored
  `bedrock-key/bedrock-key.env`, with mode `0600`. It is never printed.
- `BEDROCK_KEY_CREDENTIAL_ID` in `arns.env` must name the live key.
- The controls stack must be redeployed after the credential ID changes. The
  breaker Lambda receives that ID through its environment and does not discover
  it at runtime.
- Every AWS operation is guarded against `BEDROCK_KEY_ACCOUNT_ID`.

The required order is:

```text
create key -> save secret and metadata -> deploy controls -> rehearse breaker
```

## Create a key

Check the current state first:

```bash
uv run bedrock-key/admin.py status
```

An existing credential, including an inactive one, must be resolved before
creation. Preview and create:

```bash
uv run bedrock-key/admin.py create --dry-run
uv run bedrock-key/admin.py create
```

The default lifetime is 14 days. Override it when necessary:

```bash
uv run bedrock-key/admin.py create --credential-age-days 30
```

The create command:

1. verifies the configured account;
2. creates or validates the `/workshop/` IAM user;
3. expands the account placeholder in `policy.json` and applies the inline
   policy;
4. refuses every existing Bedrock credential on the user;
5. creates the expiring key;
6. saves the one-time secret to an ignored file with mode `0600`; and
7. records the credential ID and expiry in `arns.env`.

If the secret cannot be saved, the creator attempts to delete the new
credential immediately. It prints the credential ID and a manual cleanup
warning if rollback fails.

## Verify and deploy

Verify the bearer path without exposing the key:

```bash
uv run bedrock-key/admin.py test-key
```

Then deploy the controls against the new credential ID:

```bash
uv run bedrock-key/admin.py deploy --dry-run
uv run bedrock-key/admin.py deploy
```

Deploy requests a confirmation if the SNS email subscription is missing or
deleted. Copy the `Token` value from the emailed link without opening the link,
then confirm it through the authenticated admin session:

```bash
uv run bedrock-key/admin.py subscriptions --confirm-token 'TOKEN'
uv run bedrock-key/admin.py subscriptions
```

If status reports a deleted, pending, or unprotected subscription, run
`uv run bedrock-key/admin.py subscriptions --repair` and confirm the new token.

## Rehearse the breaker

The spend alarm rehearsal must make the credential `Inactive`:

```bash
uv run bedrock-key/admin.py alarm burst
uv run bedrock-key/admin.py logs --since-minutes 10
uv run bedrock-key/admin.py resume
```

The failure alarm rehearsal must send email while leaving the credential
`Active`:

```bash
uv run bedrock-key/admin.py alarm failure
uv run bedrock-key/admin.py status
```

To exercise only the Lambda, bypassing CloudWatch and SNS:

```bash
uv run bedrock-key/admin.py invoke-breaker --yes
uv run bedrock-key/admin.py resume
```

Run the alarm rehearsals after any deployment that changes alarms, SNS filters,
the breaker Lambda, or the credential ID.

## Stop and resume

```bash
uv run bedrock-key/admin.py stop
uv run bedrock-key/admin.py resume
```

Stop waits 30 seconds after IAM records `Inactive`; resume waits 15 seconds
after IAM records `Active`. The waits cover measured Bedrock propagation delay.
Use `--no-wait` only in automation that performs its own propagation check.

Always fix the cause before resuming. A live loop will immediately begin
spending again and can retrigger the same alarm after it returns to `OK`.

## Respond to a breaker failure

`-breaker-failed` means the Lambda raised an error. It does not mean the key was
stopped.

```bash
uv run bedrock-key/admin.py status
uv run bedrock-key/admin.py stop
uv run bedrock-key/admin.py logs --since-minutes 30
```

Do not resume until the credential is confirmed inactive and the Lambda failure
is understood.

## Rotate a key

Rotation deliberately prevents two simultaneous credentials:

```bash
uv run bedrock-key/admin.py stop
uv run bedrock-key/admin.py delete-key --yes
uv run bedrock-key/admin.py create
uv run bedrock-key/admin.py test-key
uv run bedrock-key/admin.py deploy
uv run bedrock-key/admin.py alarm burst
uv run bedrock-key/admin.py resume
```

This causes a short outage between deletion and creation. It avoids a live key
that the breaker does not target.

To delete an older credential ID that is not the current value in `arns.env`:

```bash
uv run bedrock-key/admin.py delete-key --credential-id OLD_ID --yes
```

## Change thresholds

Edit the three threshold values in `arns.env`, then redeploy:

```bash
uv run bedrock-key/admin.py deploy --dry-run
uv run bedrock-key/admin.py deploy
```

The deployment refuses invalid numbers and requires the sustained threshold to
remain below the burst threshold. `CONTROLS.md` contains the pricing and load
arithmetic behind the defaults.

## Teardown

Stop first if the class is still using the credential. When the workshop and
any postmortem capture are complete:

```bash
uv run bedrock-key/admin.py stop
uv run bedrock-key/admin.py teardown --yes
```

Teardown deletes the configured credential first, then removes the account-wide
model invocation logging configuration and starts deletion of the controls
stack. The stack owns the invocation log group and its prompt and response
records.
