# Driving the Vocareum REST API from code

Every call this repository makes against `api.vocareum.com`, the request shapes that work, and the ones that look like they should and do not. Confirmed 2026-07-27. Every row is measured against the course, assignment, and part recorded in `.env` unless a line says otherwise.

This document covers the Vocareum side: how to make Vocareum do something. The companion is [`permissions.md`](permissions.md), which covers the AWS side: what a student account is permitted to create.

Identifiers appear as `${PLACEHOLDER}`, resolved from `.env`.

## Basics

| Property | Value |
| --- | --- |
| Base URL | `https://api.vocareum.com/api/v2` |
| Auth header | `Authorization: Token <personal access token>` |
| Token source | Vocareum account settings, Personal Access Token tab. Requires Organization Admin. Displayed once at creation and never again |
| Rate limit | 800 requests per minute, not configurable |
| Content type | `application/json` on every request carrying a body |

Successful writes are frequently asynchronous. They answer `202` with a `transactionid` rather than `200`, and the work has not happened yet when the response arrives. See [Transactions](#transactions).

## Obstacles, each of which will recur

| Obstacle | Behavior | Resolution |
| --- | --- | --- |
| Cloudflare User-Agent ban | Cloudflare fronts `api.vocareum.com` and bans the default `Python-urllib/x.y` User-Agent, answering `HTTP 403` with a full marketing HTML page and Cloudflare error 1010 before the request reaches the API. The token is never evaluated, so it reads as an authentication failure and is not one | Send any other User-Agent value. `src/vocareum_tools/client.py` sets `vocareum-tools/1.0` on every call |
| Trailing slashes | `/courses/{id}/assignments/` returns `Invalid Request`. `/courses/{id}/assignments` returns 200 | Never append a trailing slash to a v2 path |
| `include_dropped=1` | Rejected. The parameter takes `true` or `false` | Pass `true` or `false` |
| Bodyless `HTTP 500` | A malformed request body produces `500` with a zero-length response body and no error text at all. It is a request shape problem masquerading as a server fault | Vary the body shape, not the endpoint. The [file upload](#uploading-a-file-into-the-workspace) section is a worked example |
| No self-identity endpoint | The v2 surface has no call that returns the caller's own identity, and the course roster listed nobody at all before a student was enrolled, leaving the instructor's own user ID undiscoverable. The cause is that Vocareum does not list the course owner under Add Teachers. Re-measured 2026-08-06: the roster now returns the enrolled student, and `role=teacher` and `role=instructor` still return `total_records: 0`, so the roster gap is specific to staff rather than general | A diagnostic student was enrolled through `POST /courses/${VOCAREUM_COURSE_ID}/users` to obtain a usable user ID. That account is `${VOCAREUM_USER_ID}` and it is still enrolled. Keep it, since `VOCAREUM_USER_ID` needs a real user |

The User-Agent ban catches only the `Python-urllib` signature, so a descriptive name is enough and there is no reason to impersonate a browser. Verified against `GET /courses/{id}` with a valid token:

| User-Agent | Response |
| --- | --- |
| `Python-urllib/3.13` | 403, blocked by Cloudflare |
| `Python-requests/2.32` | 200 |
| `curl/8.7.1` | 200 |
| `vocareum-lab-diagnostic/1.0` | 200 |
| none sent | 200 |

`requests` is unaffected only because its own default User-Agent is not the banned one.

## The workspace file tree

Every path below is under one part. The instructor workspace contains:

```
/voc/course      /voc/data       /voc/docs      /voc/private
/voc/scripts     /voc/solution   /voc/startercode
```

`lab.template` lives at `/voc/private/lab.template`. That is the only location Vocareum reads it from at lab start.

**The same endpoint uses two different path conventions, and mixing them fails.** Directory listing takes an absolute path rooted at `/voc`. File addressing takes a path relative to it, with the `/voc/` prefix removed:

| Parameter | Correct form | Wrong form and its error |
| --- | --- | --- |
| `dir=` | `/voc/private` | `private` returns `private doesn't exist` |
| `filename=` | `private/lab.template` | `/voc/private/lab.template` returns `specified source does not exist /voc/private/lab.template` |

Both errors are `HTTP 400` and both name a path, so they read as "the file is missing" when the file is present and the convention is wrong.

### Listing a directory

```
GET /courses/{courseId}/assignments/{assignmentId}/parts/{partId}/files?dir=/voc/private&list=true
```

```json
{"assignmentid": "${VOCAREUM_ASSIGNMENT_ID}", "courseid": "${VOCAREUM_COURSE_ID}",
 "directory": "/voc/private", "files": ["lab.template"], "partid": "${VOCAREUM_PART_ID}"}
```

The listing gives names only, with no size, hash, or modified time, so it cannot tell you whether the file up there is the version you meant to upload. Download it and compare hashes.

### Downloading a file

```
GET /courses/{courseId}/assignments/{assignmentId}/parts/{partId}/files?filename=private/lab.template
```

Returns a `download_url` on `labs.vocareum.com`, not the file. The URL carries an `expires` epoch and a `signature`, and it needs no `Authorization` header. Fetch it as a second request.

The `file` parameter inside that URL is `asnlib.0/private/lab.template`, so `private` is stored under an `asnlib` root internally. That internal name is not accepted by the `filename` parameter, which was the basis for two of the failed upload attempts below.

### Uploading a file into the workspace

`uv run vocareum-upload` does everything in this section, and [`deploying.md`](deploying.md) is the runbook for it. Reach for the command first, and read the rest when it breaks or when you need to write something other than a template.

**There is no upload endpoint.** All three documented `files` operations are `GET`. The only write path for workspace content is the part update:

```
PUT /courses/{courseId}/assignments/{assignmentId}/parts/{partId}
```

`content` must be a **JSON array**. The object form returns a bodyless `500`. This is the working request, verified by replacing `lab.template`:

```json
{"content": [{"target": "private", "zipcontent": "<base64 of a zip>"}]}
```

`zipcontent` is base64 of a zip archive. Archive members are written into the folder named by `target`, so a member named `lab.template` with `target: "private"` lands at `/voc/private/lab.template`. Existing files of the same name are replaced. Keep member names flat: a member carrying its own directory prefix was rejected, as rows 6 and 7 below show.

Pin the zip member's modification time. Zip entries carry a timestamp, so the same file zipped twice produces two different archives and two different payload hashes. `src/vocareum_tools/upload.py` pins every member to 1980-01-01, the zero of the DOS timestamp the format uses, which makes an upload reproducible and lets two runs be compared. This has no effect on what Vocareum stores. It only matters for hashing the request.

Seven body shapes were tried against the same 3675-byte zip. Only one was accepted:

| Body | Result |
| --- | --- |
| `{"content": [{"target": "private", "zipcontent": ...}]}` | **202**, `{"status": "success", "message": "Started", "transactionid": "450659", "objid": "${VOCAREUM_PART_ID}"}` |
| `{"content": {"target": "private", "zipcontent": ...}}` | 500, empty body |
| `{"content": {"target": "private", "zipcontent": ..., "reset": 0}}` | 500, empty body |
| `{"content": {"target": "/voc/private", "zipcontent": ...}}` | 500, empty body |
| `{"content": {"target": "private", "zipcontent": ...}, "update": 1}` | 500, empty body |
| `{"content": {"target": "asnlib", "zipcontent": ...}}`, member `private/lab.template` | 500, empty body |
| `{"content": {"target": "asnlib/private", "zipcontent": ...}}` | 500, empty body |
| `{"content": {"zipcontent": ...}}` | 500, empty body |

Rows 1 and 2 differ by the array wrapper and nothing else, which isolates the wrapper as the cause. `target: "private"` was correct from the first attempt and the `500` said nothing about it, which is what made this expensive to find.

Two fields are documented and remain untested here. `url` is an alternative to `zipcontent` and presumably fetches a zip Vocareum can reach. `reset` is documented as part of the same block, and its scope is unknown: `/voc/private` held exactly one file, so an overwrite and a wipe-then-write are indistinguishable in this measurement. Assume `reset` can delete the folder until someone proves otherwise.

The write is scoped to `target`. `/voc/scripts` and `/voc/startercode` were listed before and after and were unchanged.

#### Verifying an upload by hand

The listing cannot confirm content, and the `202` only confirms the request was queued. Confirm by hash. `vocareum-upload` does this automatically and exits `3` when the hashes disagree. By hand it is three steps:

```bash
# 1. PUT the part, then poll the transaction to state=success
# 2. GET the download_url for private/lab.template
# 3. curl it and compare
shasum downloaded.template lab.template
```

`shasum` with no arguments prints SHA-1. The upload script prints SHA-256, so the two sets of digests are not comparable with each other.

## Transactions

Asynchronous writes answer `202` with a `transactionid`. Poll it:

```
GET /transaction/{transactionid}
```

```json
{"status": "success", "state": "success", "objid": "${VOCAREUM_PART_ID}", "message": "Part updated"}
```

`state` is `pending` until the work finishes. `message` is the only place some errors ever appear, so print it in full rather than truncating it.

## The course roster

```
GET /courses/{courseId}/users
```

The only call that enumerates students. Every session call is addressed `/resources/{userId}`, so this is where the user IDs come from, and it is what makes a cohort operation possible at all. For a cost figure spanning users rather than a list of them, see the org route at the end of this document. Measured 2026-08-06 against `${VOCAREUM_COURSE_ID}`:

```json
{"status": "success", "total_records": 1,
 "users": [{"id": "${VOCAREUM_USER_ID}", "courseid": "${VOCAREUM_COURSE_ID}",
            "role": "student", "name": "Test Student 1",
            "email": "...", "lti_user_id": null, "dropped": false,
            "client_key": "", "client_userid": ""}]}
```

**The `page` parameter is zero-indexed, and getting it wrong is silent.** On a course holding one student:

| Query | `total_records` | Rows returned |
| --- | --- | --- |
| none | 1 | 1 |
| `?page=0` | 1 | 1 |
| `?page=1` | 1 | **0** |
| `?page=2` | 1 | **0** |
| `?per_page=100`, `?limit=100`, `?offset=0&limit=100`, `?page_size=100` | 1 | 1 |
| `?include_dropped=true` | 1 | 1 |
| `?role=teacher`, `?role=instructor` | 0 | 0 |

A caller that starts at page 1, which is the ordinary convention, gets an empty `users` array and an HTTP 200. It does not error. So a cohort tool built that way reports an empty course and ends nobody's session, which reads exactly like success. Omit the parameter for the first page.

`total_records` stays truthful on every page, including pages past the end, so it is the completeness check: collect until the count is covered, and treat a short read as an error. `src/vocareum_tools/endall.py` does both.

The page size is unmeasured, because one student fits on any page. `limit`, `per_page`, `offset`, and `page_size` all changed nothing here and cannot be distinguished from being ignored until a larger course exists. Do not assume one page holds a cohort. Rely on `total_records`.

## Starting a lab session

```
PUT /courses/{courseId}/assignments/{assignmentId}/parts/{partId}/resources/{userId}
```

Documented as: starts resources, extends the session if already started, launches CloudFormation templates, and issues temporary credentials.

**The start call does not always return a `transactionid`.** When the session comes back with `state: "Starting"`, the response carries the full resource record and no transaction, so there is nothing to poll and no signal that CloudFormation has finished. The stack does not exist yet at that moment.

This produced a false negative on 2026-07-25. `src/vocareum_tools/diagnose.py` skipped its poll for want of a transaction id, listed stacks immediately, found only the previous run's failed stack, and reported its denial as though it were the current result. The new stack appeared roughly 90 seconds later and had reached `CREATE_COMPLETE`.

Anything that starts a session and then inspects the stack has to wait on the AWS side rather than the Vocareum side. Poll `list_stacks` for a stack whose `CreationTime` is later than the moment the start call was made. Do not trust the first listing.

`src/vocareum_tools/diagnose.py` implements this. Three details in it are worth copying:

- **Stamp the launch instant before the call, not after.** A stack CloudFormation begins while the request is still in flight is genuinely new, and comparing against the return time would discard it.
- **Allow for clock skew.** `CreationTime` comes from CloudFormation's clock and the launch instant from yours. The script subtracts two minutes from its threshold. The cost of the slack is that a stack created in the two minutes before launch would also qualify, and nothing creates stacks in that window.
- **Warn and continue on timeout rather than exiting.** The older stack still carries information. It just must not be read as the current result.

### Reading session credentials

```
GET /courses/{courseId}/assignments/{assignmentId}/parts/{partId}/resources/{userId}
```

Temporary AWS credentials sit at `$.resources[0].cloud[0].aws_data`. The same payload carries a federated console sign-in URL with a live `SigninToken` and an unencrypted SSH private key, so never print the body unredacted. `src/vocareum_tools/diagnose.py` redacts by field-name fragment rather than by an explicit field list, because Vocareum does not document the shape.

**A terminated session returns empty strings, not absent fields.** boto3 reads empty credential strings as absent, falls through its default provider chain, and silently uses the operator's own ambient credentials. Assert all three values are non-empty before building a client, and confirm the account with `sts:GetCallerIdentity`. A probe that skipped both checks once reported on the wrong AWS account. [Ending a session](#ending-a-session) lists the exact fields as they read after a termination.

### Which AWS account the session is in

The same GET answers this, and it has to be asked before every measuring run, because the answer changes.

**Vocareum holds a pool of AWS accounts per part and moves the allocation between sessions.** The pool is published in the session record:

```
$.resources[0].cloud[0].aws_data.aws_linked_accounts
```

Read 2026-07-29 for course `${VOCAREUM_COURSE_ID}`, assignment `${VOCAREUM_ASSIGNMENT_ID}`, part `${VOCAREUM_PART_ID}`, user `${VOCAREUM_USER_ID}`:

```json
[
  {"account": "${LAB_AWS_ACCOUNT_POOL_A}", "inuse": "false", "status": "ready"},
  {"account": "${LAB_AWS_ACCOUNT_POOL_B}", "inuse": "true",  "status": "ready"}
]
```

Both accounts are real and both belong to this part. `${LAB_AWS_ACCOUNT_POOL_A}` carried Phase 0 through Phase 2 and was idle at this reading. `${LAB_AWS_ACCOUNT_POOL_B}` carried Phase 3 and was the one allocated. Neither is "the" account for the course. **The account for a run is the pool entry whose `inuse` is `"true"`.**

The A and B names are fixed to the accounts, not to the rotation. `.env` carries a separate `LAB_AWS_ACCOUNT_ID` naming whichever member is live, and that is the only one anything reads.

A flat convenience field carries the same value:

```
$.resources[0].cloud[0].aws_data.account_id     ->  "${LAB_AWS_ACCOUNT_POOL_B}"
```

Both are strings, and `inuse` is the string `"true"`, not a boolean.

#### The verification chain

Three sources have to agree, and each one catches a different failure:

| Source | What it proves | Failure it catches |
| --- | --- | --- |
| `aws_linked_accounts[].inuse == "true"` | which pool member Vocareum has allocated | a document naming an account that is now idle |
| `aws_data.account_id` | the same answer, flat | a malformed or partly-populated pool array |
| `sts:GetCallerIdentity` with the session credentials | which account the credentials actually reach | credentials that are not this session's, including the operator's own ambient credentials |

If the first two disagree with the third, stop. The credentials are not the session's and nothing measured with them describes the lab.

#### Why `LAB_AWS_ACCOUNT_ID` is not the answer

The account guard in `probe.py` compares `sts:GetCallerIdentity` against `LAB_AWS_ACCOUNT_ID` from `.env`, and nothing compares `.env` against the API. So the guard proves the credentials match the number someone typed; it cannot tell you the number is still current. When the allocation moved, the guard refused the run, and the fix was to edit `.env` to the new account. That is the right fix, but it means a hand-maintained value sits between the pool and the guard, and a stale value degrades the guard to a typo check. Read the pool first, then set `.env`, then let the guard confirm.

Nothing in the package reads `aws_linked_accounts` or `account_id` yet. Until something does, this is a by-hand step.

#### The stack name moves with it

Vocareum names the per-student stack `c{course}a{part}l{labid}t1w{account}`. Only the trailing account changed across the reallocation; the lab ID stayed the same. Read back from `list_stacks` in the live account on 2026-07-29:

```
${LAB_STACK_NAME}
```

`.env` holds it whole rather than assembled, because the lab ID is not one of the IDs the Vocareum UI shows and cannot be derived from the others.

`uv run vocareum-diagnose --no-start` prints this without starting a session, so it is the cheap way to confirm both the stack name and, through the guard, the account.

### Reading spend so far

The same GET, and the same `aws_data` block the credentials come from, carries what the session has cost:

```json
"totalspend": "0.000244",
"allowedspend": "200"
```

US dollars, as strings, cumulative for one user on one part. Measured 2026-07-27. It is one number for the whole account with no breakdown by service, resource, or session, and `totaltime`, one level up on the resource itself, is the matching figure in minutes.

**Both counters survive termination and the credentials do not**, as [Ending a session](#ending-a-session) sets out. `src/vocareum_tools/diagnose.py` prints them in step 3 before it decides whether the credentials are usable, so a run against a torn-down session still reports spend rather than only reporting that it can go no further.

The v2 endpoint reference describes this call as returning session times and cloud data and names no spend field at all, so the counters have to be found by reading a response.

Spend is the meter. The limits it runs against are configured elsewhere:

| Where | Field | Value for this part |
| --- | --- | --- |
| `GET .../parts/{p}` | `total_dollar` | `200`, and `allowedspend` on the session matches it |
| `GET .../parts/{p}` | `monthly_dollar` | `100` |
| `GET .../parts/{p}` | `total_time`, `monthly_time` | `0`, meaning no cap configured |
| `GET .../parts/{p}` | `session_length` | `240` minutes |
| `POST .../resources/{userId}` | `{"state": "active|inactive", "budget": "{float}", "apitype": "{int}"}` | Documented as the write path for the allowance. Untested here |

### Two different principals

The credentials this endpoint hands out are not the principal CloudFormation deploys as. The consequence is that a probe run with session credentials measures what a student may do, and says nothing about what `lab.template` may do.

### Ending a session

```
DELETE /courses/{courseId}/assignments/{assignmentId}/parts/{partId}/resources/{userId}
{"action": "end"}
```

The reference documents this in one line, "End or terminate resources", with a body of `{"action":"end|terminate"}` and no account of how the two differ. Only `end` has been exercised here. Treat `terminate` as unknown and destructive until someone measures it.

**This call is synchronous.** It answers `200` with `{"status": "success", "message": "Resources ended/terminated"}` and no `transactionid`, so there is nothing to poll. The part update is asynchronous and this is not, which means a client that assumes every write returns a transaction will wait forever on a field that never arrives.

Ending a 4-hour session about an hour in, on 2026-07-27, moved these fields:

| Field | Before | After |
| --- | --- | --- |
| `state` | `Ready` | `Terminated` |
| `cloud[0].status` | `active` | `inactive` |
| `sessionend` | the scheduled 4-hour mark | the moment of the call |
| `expected_termination_time` | the scheduled 4-hour mark | the moment of the call |

`sessionend` is rewritten rather than left at its original value, so it records when the session actually ended and cannot be used to recover what the scheduled length was.

The CloudFormation teardown runs asynchronously on the AWS side after this returns. The stack moves `CREATE_COMPLETE` to `DELETE_COMPLETE` over the following minutes, so a stack listing taken immediately after the `200` still shows the stack alive. A `DELETE_COMPLETE` stack is the durable signature of a session that has ended.

**Ending a session gives up the AWS account.** This was measured on 2026-07-29 by ending a session and immediately starting a new one. The next session did not come back to the same account. Reading `aws_linked_accounts` before and after shows the whole mechanism:

| Pool member | Before the DELETE | After the next PUT |
| --- | --- | --- |
| The account the session was using | `status: ready`, `inuse: true` | `status: cleanup`, `inuse: false` |
| The other pool member | `status: ready`, `inuse: false` | `status: ready`, `inuse: true` |

So the pool is not a queue that hands the same account back. Ending a session returns the account for wiping and allocates a different member, and `status: cleanup` is the account being erased. `cleanup` is transient: read again later the same day, the released member was back to `status: ready, inuse: false`, so a member that has been wiped rejoins the pool and can be allocated again. The practical consequences:

- **Ending a session to get a clean slate also gets a different account.** There is no "restart my lab in place". Any account-scoped fact recorded from the old session, including the number in `LAB_AWS_ACCOUNT_ID`, is stale the moment the new one starts.
- **Anything left in the old account is unreachable, not merely untidy.** It gets wiped, but it cannot be inspected or cleaned by hand afterwards, so a leaked resource discovered after a session ends can never be confirmed removed. Sweep before ending, not after.
- **The account guard will fire on the first call of the new session.** That is the correct outcome and it is how this was found. Do not respond by editing `LAB_AWS_ACCOUNT_ID` reflexively; a guard that adopts whatever account it lands in has stopped being a guard. Confirm the new allocation against `aws_linked_accounts` first, then set it deliberately.
- **The pool has at least three members, and do not read the rotation as an alternation.** The account allocated on 2026-07-29 was the same one that carried Phases 0 through 2, which the permission documents call `${LAB_AWS_ACCOUNT_POOL_A}`, and two members trading places like that is why this section used to say the pool held two. A third, `${LAB_AWS_ACCOUNT_POOL_C}`, was live on 2026-08-06. Three is a lower bound rather than a count, because a member becomes visible only when a session lands in it. No member is ever retired, which is why they are named A, B and C rather than current and prior: a positional name would have to be rewritten on every rotation, and rewriting it would silently change what a finished measurement claims.

**`aws_data` survives termination as a populated dict.** Every credential field inside it becomes an empty string, which is the behavior described under [Reading session credentials](#reading-session-credentials), but the dict itself stays present and truthy. Testing `if aws_data:` therefore reports a dead session as live. Test the individual fields:

```
url, ssh_key, account_id                              -> ""
aws_access_key_id, aws_secret_access_key, aws_session_token  -> ""
readonly_aws_access_key_id, readonly_aws_secret_access_key   -> ""
readonly_aws_session_token, readonly_expiration              -> ""
totalspend, allowedspend                              -> still populated
```

`state` and `cloud[].status` are the fields that answer whether a session is live. `totalspend` and `allowedspend` outlive the session, so spend is readable after the fact.

`account_id` emptying on termination is why the pool array matters: it is the field that names the account, and it is gone exactly when you most want to know which account the last run was in. Whether `aws_linked_accounts` survives termination is unmeasured. It was read from a live session on 2026-07-29 and has not been read from a terminated one, so do not assume the pool is still legible after the session ends. Record the account while the session is up.

### Ending every session on a part

**There is no bulk endpoint.** Every session call is `/resources/{userId}`, so ending a cohort is [the roster](#the-course-roster) plus the same `DELETE` once per student. `src/vocareum_tools/endall.py` is that loop.

This matters because the student's AWS view has no End Lab button. Nobody ends those sessions. A thirty-student workshop leaves thirty sessions running to the four-hour `session_length`, each holding a pool account until it expires.

Three properties the loop has to have, each one a failure that would otherwise be silent:

- **Read every session before ending any of them.** `account_id` empties on termination, so a report assembled afterwards cannot say which accounts were given up.
- **Do not stop on the first failure.** A cohort where one student returns a `500` still has the rest holding accounts. Record the outcome, continue, and let the exit code say something was left unknown.
- **Re-read each session after its `DELETE`.** The call answers `200` on acceptance, not on the session being down, and that is per student rather than once.

Sweeping a cohort is the harder half and is not solved here. `vocareum-sweep` needs that student's AWS credentials, which exist only while their session is live, so a fleet sweep has to interleave sweep and end per student. Whether it is needed at all depends on how thoroughly Vocareum's own `status: cleanup` wipe works, and the evidence is mixed: the wipe is documented above, but `src/vocareum_tools/sweep.py` records an AgentCore gateway that survived four sessions.

## Endpoints this repository uses

| Method and path | Used for | Caller |
| --- | --- | --- |
| `GET /courses/{c}/assignments/{a}/parts/{p}` | Read part configuration: `labtype`, `container_image`, `cloud_labs`, `instant_aws_access`, `session_length`, budgets | by hand |
| `PUT /courses/{c}/assignments/{a}/parts/{p}` | Upload workspace content through `content` | `upload.py` |
| `GET .../parts/{p}/files?dir=/voc/...&list=true` | List a workspace directory | by hand |
| `GET .../parts/{p}/files?filename=private/lab.template` | Get a signed download URL for one file | `upload.py` |
| `PUT .../parts/{p}/resources/{userId}` | Start or extend a lab session | `diagnose.py`, `probe.py` |
| `GET .../parts/{p}/resources/{userId}` | Read session times, spend against the allowance, the allocated AWS account and the account pool, and temporary AWS credentials | `diagnose.py`, `probe.py`, and by hand for the account |
| `DELETE .../parts/{p}/resources/{userId}` | End a session early, with `{"action": "end"}` | `endlab.py`, `endall.py` |
| `GET /transaction/{id}` | Poll an asynchronous write or session start | all three scripts |
| `GET /courses/{c}/users` | List the roster, which is the only source of student user IDs | `endall.py` |
| `POST /courses/{c}/users` | Enrol the diagnostic student | by hand |

Three other paths look promising, and only one of them is worth chasing:

- **Org AWS usage**, `GET /orgs/{orgid}/aws_usage`, returns unused, used, closed, discarded, and fraud counts, a payer count, and a total cost. It is the only published route to a figure spanning more than one user. Measured 2026-08-27: the existing API key answered it with no extra grant. `{orgid}` is `VOCAREUM_ORG_NUMBER`, a different identifier from the AWS organization recorded as `VOCAREUM_ORG_ID`. The count is org-wide, not per course. It reports pool checkout state, not which AWS resources are still running. For per-student spend, the session record above is the direct answer.
- **Data Pipeline**, `GET /orgs/{orgid}/s3_labs`, is aggregate reporting. It returns temporary AWS credentials for an S3 prefix that holds daily CSVs of sessions and cost, refreshed every 6 hours. The response `path` repeats the bucket name as its first segment, so strip it to get the key prefix. Measured 2026-08-27: the existing API key answered it, but the prefix held no objects yet.
- **Batch script**, `GET .../parts/{p}/batch_script/{id}`, returns base64 execution output across student environments. It presumes an environment that already came up.

## References

- [Vocareum REST API](https://help.vocareum.com/en/articles/3658972-vocareum-rest-api): Personal Access Token generation, scopes, rate limit, status codes
- [Vocareum REST API v2 Endpoint Reference](https://help.vocareum.com/en/articles/14542929-vocareum-rest-api-v2-endpoint-reference): all 66 endpoints. The `PUT Part - Update` entry names `content (target, url/zipcontent, reset)` in one line and documents no example, which is the whole of the published guidance on uploading a file
- [Vocareum API v2 Postman documentation](https://documenter.getpostman.com/view/6736336/S11Exg4b): request format and pagination
- [Vocareum: AWS Cloud Labs](https://help.vocareum.com/en/articles/9425419-aws-cloud-labs): per-student AWS accounts and the lab lifecycle
- [Vocareum: Data Pipeline](https://help.vocareum.com/en/articles/6010883-data-pipeline): session log fields and the `s3_labs` export
