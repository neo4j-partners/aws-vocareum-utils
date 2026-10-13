# Deploying a template to Vocareum

`lab.template` has to reach `/voc/private/lab.template` in the instructor workspace. That is the only location Vocareum reads it from at lab start, and the filename must be exact with no extension added.

## lab.policy: retired on 2026-08-04

A `lab.policy` file was the first design. In testing, the live student session
did not receive all of its Bedrock, AgentCore, ECR, and Secrets Manager
permissions.

The same statements work as `AWS::IAM::RolePolicy` resources in
`lab.template`. CloudFormation applies them with the deployer role. Every grant
now lives in `lab.template`. The repository no longer uses a separate
`lab.policy` file.

Everything below in this file now applies to `lab.template` only.

## Through the API

```bash
uv run vocareum-upload --course neo4j-fundamentals
uv run vocareum-upload lab-bedrock.template --name lab.template --course neo4j-fundamentals
uv run vocareum-upload --dry-run --course neo4j-fundamentals
uv run vocareum-upload --force --course neo4j-fundamentals
```

The script hashes the local file, reads what is up there now, skips the write when the two already match, sends the upload, polls the transaction to a terminal state, then downloads the file back and compares hashes. It reports success only when those hashes match.

| Exit code | Meaning |
| --- | --- |
| `1` | Configuration is incomplete |
| `2` | An API call failed |
| `3` | The upload was accepted, but what came back does not match what was sent |

Underneath it is one call:

```
PUT /courses/{c}/assignments/{a}/parts/{p}
{"content": [{"target": "private", "zipcontent": "<base64 zip containing lab.template>"}]}
```

`content` must be a JSON array. The object form returns `HTTP 500` with an empty body and no hint as to why. [`vocareum-api.md`](vocareum-api.md) has the full request shape, the seven variants that were tried, and the manual verification steps.

Setup is the same `.env` the diagnostic script reads. [`diagnosing.md`](diagnosing.md#one-time-setup) covers it.

## Through the UI

1. Open the assignment part in Vocareum and click Configure Workspace.
2. In the file browser, go to the `private` folder under `voc`.
3. Upload `lab.template`. The filename must be exact, with no extension added.
4. Save the part, publish the assignment, then verify through Student View.

The UI leaves no hash to check against, and the file listing returns names only with no size or modified time, so it cannot tell you whether what is up there is the version you meant to upload.

## Validate before uploading

```bash
cfn-lint courses/neo4j-fundamentals/lab.template
cfn-lint lab-bedrock.template
```

A template that cannot parse uploads exactly as successfully as one that can, and the failure then surfaces minutes later inside a student session.

`cfn-lint` cannot see the Service Control Policy. A template that lints clean and deploys in an ordinary AWS account can still fail in Vocareum on an explicit organization-level deny. Linting proves the template is well formed. Only a student session proves it is permitted, and [`permissions.md`](permissions.md) records what is permitted so far.

## After uploading

Open Student View and start the lab. In the AWS console, open CloudFormation and watch the stack create, then confirm the resources exist in the services they belong to.

If the stack fails, [`diagnosing.md`](diagnosing.md) recovers the reason without console access. If nothing happens at all, check the Vocareum session log for the lab, which records events like `CFN creation successful`.
