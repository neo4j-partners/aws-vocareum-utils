# Notebooks

Notebooks that ship to students through the Vocareum workspace.

| File | What it is |
| --- | --- |
| `lab-01-verify-environment.ipynb` | Student-facing check that the account can actually run the workshop. Builds the same things the workshop's deployment builds — an AgentCore Runtime behind a container CodeBuild compiles, a Gateway, a Memory, a Lambda, a secret, a DynamoDB table — then deletes all of it and confirms the account is empty again |

The classes the notebook runs on are **not** here. They live in [`../src/workshop_lab/`](../src/workshop_lab/) and are installed from GitHub at run time, for the reasons under [How the helper code reaches a student](#how-the-helper-code-reaches-a-student).

## What the notebook does

The template creates the environment, and this notebook proves the environment can build what the workshop needs, by building it for real and tearing it down again. Each step prints `PASS`, `FAIL`, or `SKIP` with a reason rather than raising, so a student with a broken lab sees an explanation instead of a traceback.

The steps run in this order:

0. Install `workshop_lab` from GitHub and build the `Harness` every step below runs through.
1. Stop unless the access key, the secret key, and the session token all carry a value.
2. Print the account and identity from STS, and stop unless the region is `us-east-1`.
2b. Call `us.anthropic.claude-sonnet-4-6` and `us.anthropic.claude-opus-4-6-v1`, each through `Converse` and `ConverseStream`, then invoke `amazon.nova-2-multimodal-embeddings-v1:0` and `amazon.titan-embed-text-v2:0` with their native request schemas. The chat calls prove the agent path, while each embedding call must return exactly one 1,024-dimension vector to prove its retrieval path. `workshop_lab.models` holds the pinned ids and the sorting that matters: an entitlement refusal and an IAM refusal can arrive as the same `AccessDeniedException`, so the step reads the message before the code. Both chat invoke actions are called because they are separate permissions and a session has already been measured where one passed and the other did not. That run used a Sonnet 4.5 id: `InvokeModel` invoked cleanly and `InvokeModelWithResponseStream` did not, same model, same run. It is lettered to keep the established resource and cleanup step numbers stable.
3. Connect to a Neo4j Aura instance and write a throwaway node. Skips if the placeholder credentials are still in the cell.
4. Print the naming and tagging conventions everything below follows.
5. List AgentCore resources to confirm the account can see AgentCore at all.
6. Create the four IAM execution roles CodeBuild, the Runtime, Lambda, and the Gateway each need.
7. Create the ECR repository and upload the container build source to S3.
8. Build the container with CodeBuild and push it to ECR. When the build produces no image, copy the pre-built image into ECR over the registry HTTP API instead. `workshop_lab.registry` holds that copy. Which path runs is decided by asking ECR, not by reading the build status. The build itself was stopped by the platform in every student account until 2026-08-07 and now succeeds, which is why the fallback exists and why it costs nothing now that it is not needed.
9. Create an AgentCore Runtime, wait for it to become ready, add an endpoint, and invoke it.
10. Create an AgentCore Memory with the workshop's memory strategies, tag it, and confirm the tag stuck.
11. Build a Lambda, a Gateway, and a Gateway target, then call the Gateway with a hand-signed request.
12. Create a Secrets Manager secret and a DynamoDB table.
13. Confirm this machine can install `neo4j-graphrag` from PyPI. Creates nothing in AWS, and needs reachable PyPI rather than an AWS permission. Every lab in the workshop opens with a pip install, so a blocked or mirrored index stops all of them at their first cell.
14. Delete everything created above and confirm each resource is actually gone. It runs the registered deletes first as a hint, then enumerates the account itself, because a delete that was never registered is exactly the one that gets left behind. `workshop_lab.teardown` holds the enumeration and the CloudFormation exclusion. The rule for recognising a workshop resource is `workshop_lab.selection`, a module of its own because an instructor-side sweeper reads it too, and the two must not drift apart about which resource is the workshop's.
15. Print a one-line-per-check summary in the order the checks ran.

**It calls Claude, Nova, and Titan first.** Step 2b invokes the two Claude models the workshop's agents run on and requests Nova and Titan embeddings immediately after credentials, account, and region are verified. Whatever step 2b reports is a result for the student session role only. A check against the student role never substitutes for one against the deploying role, which is the principal that actually matters for `lab.template`.

**Step 1 raises rather than prints.** It is the only step that does, and the exception is the point. A terminated Vocareum session still returns all three credential fields, but empty. boto3 reads an empty string as no value, falls through its provider chain, and can report on whoever is logged in locally instead. Every check below would then pass while describing the wrong AWS account, which has already happened once.

**Step 2 also raises, on anything other than `us-east-1`.** AgentCore Runtime also runs in `us-west-2`, but the Nova multimodal embedding model the workshop uses is in `us-east-1` only, so `us-east-1` is the real floor.

**IAM role names carry a `workshop-` prefix, and the name construction adds it rather than the prefix carrying it.** The prefix passed to `Harness` in step 0 is `verify`, and the four roles are `workshop-verify-codebuild-role`, `workshop-verify-runtime-role`, `workshop-verify-lambda-role`, and `workshop-verify-gateway-role`. `Names.role()` in `workshop_lab` produces exactly those strings, step 4 prints the shape from it, and step 6 creates them from it. `lab.template` scopes its own IAM-management statement's `Resource` to `role/workshop-*` and `role/AmazonBedrockAgentCoreSDK*`; a role name outside those prefixes is denied on `iam:PutRolePolicy` and `iam:CreateRole`. That prefix, not a stack output, is the coupling point between this notebook and `lab.template` now. Putting `workshop` inside the prefix instead produces `workshopverify-codebuild-role`, which matches neither granted prefix and silently costs all four roles their policies.

**The prefix must not begin with `ws`, and `Names.__post_init__` raises if it does.** `InvokeAgentRuntime` percent-decodes the request path, so a Runtime named `ws...` puts the substring `/ws` into it, and the AgentCore front door routes the call to its WebSocket handler and answers `Not a WebSocket Upgrade Request` before the request reaches the container. Step 9 names its Runtime `{PREFIX}_runtime`, so the constraint lands on the prefix. It used to be a comment in the notebook; `Names` now raises on it, which is why step 0 is the right place to pass the prefix.

## How the helper code reaches a student

**It is installed, not shipped.** Step 0 runs:

```
%pip install --quiet "https://github.com/neo4j-partners/neo4j-sec-filings-graphrag-workshop/archive/refs/tags/v0.13.5.zip#subdirectory=staging"
```

Vocareum's documentation says everything in `/voc/startercode` is copied into each student's `/voc/work`, and Jupyter puts the notebook's own directory on `sys.path`, so a sibling module should import. **Measured 2026-08-06 in a live session: it does not.** Measured the same day in the same session: the lab account reaches public GitHub over HTTPS and pip installs from it. So the helpers are packaged and installed.

**The package cannot be installed from this repository.** `aws-vocareum` is private, and a private repository answers both `raw.githubusercontent.com` and the archive endpoint with **404, not 403**. From the client, a missing file and a private repository are indistinguishable. The public workshop repository hosts the installable copy.

**`../src/workshop_lab/` is the source of truth, not the published copy.** `../scripts/sync_workshop_lab.py` publishes it into the workshop repository's `staging/`, and `../tests/runtime/test_workshop_lab_drift.py` fails while the two disagree. That test skips when the workshop repository is not checked out beside this one, so a skip there verifies nothing. Edit under `src/`, sync, then commit and push the workshop repository:

```bash
./scripts/sync_workshop_lab.py --check    # exit 1 when the published copy is stale
./scripts/sync_workshop_lab.py            # publish
```

**pip reinstalls from a direct URL even at an unchanged version**, verified empirically. That is the point of the arrangement: a helper fix reaches the next student to run step 0, with no notebook to republish and no Vocareum click path to repeat.

**A student already past step 0 needs a kernel restart, not a re-run.** Verified empirically: once `workshop_lab` is in `sys.modules`, re-running the install replaces the files on disk and the running kernel keeps the old objects. `importlib.reload` does not reliably help either, because a `.pyc` written in the same second as the new source is reused. So the instruction for a mid-class fix is Restart Kernel, then run from step 0, and step 0's markdown says so. Bump `__version__` in `src/workshop_lab/__init__.py` with any published fix, so the line step 0 prints is what tells a student which one they are on.

**The ref is pinned, and pinning it is part of releasing.** The URL above names `refs/tags/v0.13.5`, not `refs/heads/main`, so a push mid-class cannot change what a student installs. Two things make that pin an ordinary step rather than a thing to remember. `../tests/management/test_notebook_install_url.py` holds all three copies of the URL to one string and requires a pinned tag to be `v<__version__>`, so the tag a student installs and the version step 0 prints cannot name different releases. `../scripts/release_check.py` then asks whether that tag exists, whether the published copy behind it is committed and pushed, whether it builds and imports, and with `--fetch` whether GitHub is really serving it.

The order matters, because the URL is written inside the package it installs. Edit the three copies and `__version__` first, sync, then commit, tag, and push. Tagging before the edit produces a tag whose own docstring names a different ref, which is how `v0.11.2` came to be skipped.

## Uploading it to Vocareum

1. Open the assignment part and click **Configure Workspace**.
2. In the file browser, open the `work` folder under `voc`.
3. Click **Upload** and select `lab-01-verify-environment.ipynb`.
4. Open the notebook once inside Vocareum to confirm it renders.
5. Click **Publish Notebook**. The dialog scans `/voc/work` for `.ipynb` files, so the dropdown stays empty until step 3 is done.
6. In the dialog, pick this notebook and publish. Vocareum writes the student copy into `startercode`.
7. Save the part, publish the assignment, then open **Student View** and run every cell top to bottom.

Step 7 is the real test. Student View runs under the student role and the lab policy, so a call that succeeds for you as the instructor can still return `AccessDenied` for a student.

## Why there are no autograded cells

The workspace already has a `grade.sh` in `/voc/scripts`. Publish Notebook generates a grading script and overrides the existing one when the notebook contains autograded cells. This notebook contains none, so publishing it leaves `grade.sh` alone.

To add grading later, back up `grade.sh` first, then use the Vocareum cell toolbar to mark cells:

- **Autograded answer** for the cell the student edits, with the reference solution wrapped in `### BEGIN SOLUTION` and `### END SOLUTION`.
- **Autograded tests** for the cell that checks it, with any hidden checks wrapped in `### BEGIN HIDDEN TESTS` and `### END HIDDEN TESTS`.
- **Manually graded answer** for free-text responses.

Vocareum strips the solution block from the student copy and replaces it with the `TEMPLATE` string, so the student sees a stub instead of the answer.

## Editing it

The notebook is generated code, but it is a normal `.ipynb` and can be edited directly in Jupyter or VS Code. Two rules keep it valid after an edit:

- Clear all outputs before committing. Student copies should ship without prior results.
- Keep cell IDs unique. They must match `^[a-zA-Z0-9-_]+$`.
- Changing behaviour usually means editing `../src/workshop_lab/`, not the notebook. Sync and push the workshop repository afterwards, or students keep installing the old package.

Validate the JSON structure after editing:

```bash
python3 -c "import json,sys; nb=json.load(open('notebooks/lab-01-verify-environment.ipynb')); print(len(nb['cells']),'cells'); assert all(isinstance(c['source'],list) for c in nb['cells'])"
```

## References

- [Vocareum: Setting Up Grading in Vocareum Notebook](https://help.vocareum.com/en/articles/9341352-setting-up-grading-in-vocareum-notebook): cell tags, solution pragmas, and what Publish Notebook generates
- Vocareum: Jupyter Notebook Labs, notebook lab type and the workspace folder layout. Article `3663185-jupyter-notebook-labs` returns 404; [What is Vocareum Notebook?](https://help.vocareum.com/en/articles/6910075-what-is-vocareum-notebook) is the closest live replacement, though it does not cover folder layout
- [Vocareum: AWS Cloud Labs](https://help.vocareum.com/en/articles/9425419-aws-cloud-labs): per-student AWS accounts and lab lifecycle
- Vocareum: Configuring Assignments, Save Part, Publish, and Student View. Article `3653866-configuring-assignments` returns 404 and no replacement has been found yet
- [nbformat format description](https://nbformat.readthedocs.io/en/latest/format_description.html): the notebook JSON spec
- [boto3 documentation](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html): the client calls used in the notebook
