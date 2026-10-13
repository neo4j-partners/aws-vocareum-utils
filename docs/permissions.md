# What the student AWS account permits

Detailed evidence for each row is kept privately.

This file records what each principal in a student lab account can and cannot do. Every row was measured in a live lab session in `us-east-1`. It is the one file in the repository that states a permission result. To record a new measurement, add a row here.

## Status values

The Status column uses the same words the probe tools print, so a tool result can be copied into a row as it is.

- **Allowed:** The call passed authorization. A not-found, throttled, or model-access answer still counts, because authorization passed first.
- **Denied (IAM):** No identity policy grants the action, or one denies it. A change to the identity policy could change the result.
- **Denied (SCP):** An organization policy denies the action. Nothing inside the account can change it.
- **Blocked:** The account must subscribe or opt in before the call can succeed.
- **Untested:** No session has measured the action yet.

## The two principals

| Principal | Role | What it runs |
| --- | --- | --- |
| Deployer role | `assumed-role/vocareum/${LAB_DEPLOYER_ROLE}` | `lab.template`, through CloudFormation |
| Student session role | `assumed-role/voclabs/user*` | Notebook code and the student console |

A result for one principal says nothing about the other. A notebook that calls Bedrock proves the student role may call Bedrock. It proves nothing about what `lab.template` may create. The two tables below stay separate for that reason.

Grants for the student role live in `lab.template` as `AWS::IAM::RolePolicy` resources. The deployer role attaches them to `voclabs` when the stack is created. Grants delivered this way were measured to take effect on 2026-08-04.

## Deployer role

Each row comes from a resource in a stack the lab actually ran. A row names the calls that resource needs, so it is evidence for the resource as declared.

| Action | Status | Measured by | Date |
| --- | --- | --- | --- |
| `s3:CreateBucket`, `s3:PutBucketPublicAccessBlock`, `s3:PutBucketTagging` | Allowed | An S3 bucket reached `CREATE_COMPLETE` | 2026-07-25 |
| `ssm:PutParameter`, `ssm:AddTagsToResource` | Allowed | A Standard tier parameter reached `CREATE_COMPLETE` | 2026-07-25 |
| `logs:CreateLogGroup`, `logs:PutRetentionPolicy`, `logs:TagResource` | Allowed | A log group with 7 day retention reached `CREATE_COMPLETE` | 2026-07-25 |
| `iam:CreateRole` | Allowed | A role with a generated name and a Lambda trust policy reached `CREATE_COMPLETE` | 2026-08-06 |
| `lambda:CreateFunction` | Allowed | A Python function using that role reached `CREATE_COMPLETE` and read back as `Active` | 2026-08-06 |
| `iam:PutRolePolicy` on a role the same stack created | Allowed | A separate `AWS::IAM::RolePolicy` resource reached `CREATE_COMPLETE` | 2026-08-06 |
| `iam:PassRole` | Allowed | A Lambda function that names a stack-created role reached `CREATE_COMPLETE` | 2026-08-06 |
| Invoking a Lambda-backed custom resource | Allowed | The custom resource reached `CREATE_COMPLETE` and its output resolved. Reproduced in a second pool account | 2026-08-06 |
| `logs:DescribeLogGroups`, as the sweep function's own role | Allowed | The custom resource output reported a count from the call | 2026-08-06 |
| `logs:DeleteLogGroup`, as the sweep function's own role | Untested | The sweep reported nothing to delete, so no delete was attempted | 2026-08-06 |
| Everything else | Untested | | |

Two notes on this table:

- **The sweep function is a third principal:** It runs as its own execution role. Its rows describe that role only, and they sit here because the deployer creates it.
- **The ship gate infers the nine grants from the stack status:** The student role cannot read the inline policies on `voclabs`. A `CREATE_COMPLETE` or `UPDATE_COMPLETE` stack means CloudFormation created all nine `AWS::IAM::RolePolicy` resources. The gate treats that status as evidence the grants landed. It does not claim to have read them.

## Student session role

Measured as `assumed-role/voclabs/user*`, after the credential, account, and Region checks passed. The rows span several accounts from the same pool. Each account sits under the same organization policy and the same role template.

| Action | Status | Measured by | Date |
| --- | --- | --- | --- |
| `sts:GetCallerIdentity` | Allowed | Returned the student account and the session role ARN | 2026-07-28 |
| `bedrock:ListFoundationModels`, `bedrock:ListInferenceProfiles` | Allowed | Both calls returned results. The model catalog is saved in [`bedrock-foundation-models-us-east-1.json`](bedrock-foundation-models-us-east-1.json) | 2026-07-28 |
| `bedrock:GetFoundationModel`, `bedrock:GetFoundationModelAvailability` | Allowed | Both returned details for the required models | 2026-07-28 |
| `bedrock:InvokeModel` | Allowed | Nova 2 returned an embedding. Claude calls passed authorization. See [Bedrock models](#bedrock-models) for which models answer | 2026-07-28, 2026-08-25 |
| `bedrock:InvokeModelWithResponseStream` | Allowed | Streaming calls passed authorization and later returned output | 2026-07-28, 2026-08-25 |
| `aws-marketplace:ViewSubscriptions`, `aws-marketplace:Subscribe` | Untested | Neither action can be called on its own. See [Bedrock models](#bedrock-models) | |
| `ssm:GetParameters` | Denied (IAM) | The refusal said no identity-based policy allows the action | 2026-07-28 |
| `iam:CreateRole`, `iam:DeleteRole`, `iam:GetRole`, `iam:PutRolePolicy`, `iam:DeleteRolePolicy`, `iam:AttachRolePolicy`, `iam:DetachRolePolicy`, `iam:TagRole`, `iam:ListAttachedRolePolicies`, `iam:ListRolePolicies`, `iam:GetRolePolicy` | Allowed | A temporary role was created, inspected, tagged, changed, and deleted. Re-measured on a second name prefix | 2026-07-29 |
| `iam:GetRole`, `iam:GetRolePolicy` on `voclabs` itself | Denied (IAM) | The refusal named an explicit deny in an identity-based policy | 2026-08-23 |
| `iam:PutRolePolicy` on `voclabs` itself | Denied (IAM) | No identity policy grants it on that role | 2026-08-04 |
| `iam:DeleteRolePolicy` on `voclabs` itself | Denied (SCP) | The refusal named a service control policy | 2026-08-04 |
| `iam:PassRole` | Allowed | `lambda:CreateFunction` succeeded with a student-created role | 2026-07-29 |
| `iam:CreateServiceLinkedRole`, scoped to `*agentcore*` | Allowed | The AgentCore service-linked role was requested directly | 2026-07-29 |
| `lambda:CreateFunction`, `lambda:DeleteFunction`, `lambda:GetFunction`, `lambda:InvokeFunction`, `lambda:UpdateFunctionCode`, `lambda:UpdateFunctionConfiguration`, `lambda:AddPermission`, `lambda:RemovePermission`, `lambda:TagResource`, `lambda:PublishLayerVersion`, `lambda:GetLayerVersion`, `lambda:DeleteLayerVersion`, `lambda:ListLayerVersions` | Allowed | A temporary function and layer were created, invoked, updated, tagged, and deleted | 2026-07-29 |
| `lambda:ListFunctions` | Allowed | Returned an empty list | 2026-07-29 |
| `s3:CreateBucket`, `s3:PutBucketPolicy` | Allowed | Passed for both the `workshop-` and `bedrock-agentcore-` bucket prefixes | 2026-07-29 |
| `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`, `s3:ListBucket`, `s3:DeleteBucket`, `s3:GetBucketLocation`, `s3:PutLifecycleConfiguration`, `s3:GetLifecycleConfiguration`, `s3:PutBucketVersioning`, `s3:GetBucketVersioning` | Allowed | Each call ran against a temporary bucket, which was then deleted | 2026-07-29 |
| `s3:PutBucketTagging`, `s3:GetBucketTagging`, `s3:PutObjectTagging` | Untested | Granted in `lab.template` on 2026-08-07. Step 7 of the notebook calls them | |
| `dynamodb:ListTables`, `dynamodb:CreateTable`, `dynamodb:DescribeTable`, `dynamodb:TagResource` | Allowed | An on-demand `workshop-` table was created, described, and tagged | 2026-07-29 |
| `dynamodb:PutItem`, `dynamodb:GetItem`, `dynamodb:UpdateItem`, `dynamodb:Scan`, `dynamodb:Query`, `dynamodb:BatchWriteItem`, `dynamodb:DeleteItem` | Allowed | Each call ran against that table once it was `ACTIVE` | 2026-07-29 |
| `dynamodb:DeleteTable` | Allowed | The table was deleted and then read back as not found | 2026-07-29 |
| `bedrock-agentcore:ListAgentRuntimes`, `bedrock-agentcore:ListGateways`, `bedrock-agentcore:ListMemories` | Allowed | Each call returned an empty list | 2026-07-28 |
| `bedrock-agentcore:ListAgentRuntimeEndpoints`, `bedrock-agentcore:ListGatewayTargets` | Allowed | Each returned not found for an identifier that does not exist | 2026-07-28 |
| `bedrock-agentcore:CreateWorkloadIdentity`, `bedrock-agentcore:GetWorkloadIdentity`, `bedrock-agentcore:DeleteWorkloadIdentity` | Allowed | One identity was created, read, and deleted | 2026-07-28 |
| `bedrock-agentcore:CreateMemory`, `bedrock-agentcore:GetMemory`, `bedrock-agentcore:UpdateMemory`, `bedrock-agentcore:DeleteMemory` | Allowed | One short-term memory was created, read, updated, and deleted | 2026-07-28 |
| `bedrock-agentcore:CreateGateway`, `bedrock-agentcore:GetGateway`, `bedrock-agentcore:DeleteGateway` | Allowed | Two MCP gateways were created and deleted. One used `AWS_IAM` and one used `SEMANTIC` search with no authorizer | 2026-07-28 |
| `bedrock-agentcore:CreateGatewayTarget`, `bedrock-agentcore:GetGatewayTarget`, `bedrock-agentcore:DeleteGatewayTarget` | Allowed | One Lambda target was created, read, and deleted | 2026-07-28 |
| `bedrock-agentcore:InvokeGateway` | Allowed | A signed MCP `tools/list` request returned HTTP 200 and the registered tool | 2026-07-28 |
| `bedrock-agentcore:CreateAgentRuntime`, `bedrock-agentcore:GetAgentRuntime`, `bedrock-agentcore:UpdateAgentRuntime`, `bedrock-agentcore:DeleteAgentRuntime`, `bedrock-agentcore:CreateAgentRuntimeEndpoint`, `bedrock-agentcore:GetAgentRuntimeEndpoint`, `bedrock-agentcore:DeleteAgentRuntimeEndpoint`, `bedrock-agentcore:InvokeAgentRuntime` | Allowed | A runtime built from an ARM64 image reached `READY`, answered an invocation, and was deleted with its endpoint | 2026-07-29 |
| `ecr:GetAuthorizationToken`, `ecr:CreateRepository`, `ecr:DescribeRepositories`, `ecr:DeleteRepository` | Allowed | A private repository was created, described, and deleted | 2026-07-29 |
| `ecr:BatchCheckLayerAvailability`, `ecr:InitiateLayerUpload`, `ecr:UploadLayerPart`, `ecr:CompleteLayerUpload`, `ecr:PutImage` | Allowed | Each call ran on its own, and a real `docker push` used the same path | 2026-07-29 |
| `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer` | Allowed | The pushed manifest and a layer URL were read back | 2026-07-29 |
| `codebuild:CreateProject`, `codebuild:StartBuild`, `codebuild:BatchGetBuilds`, `codebuild:ListProjects`, `codebuild:DeleteProject` | Allowed | An `ARM_CONTAINER` project ran to `SUCCEEDED`. On 2026-08-07 step 8 of the notebook built and pushed the workshop image the same way | 2026-07-29, 2026-08-07 |
| `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`, `logs:DescribeLogGroups`, `logs:DescribeLogStreams` | Allowed | A log group, stream, and event were created under `/aws/lambda/hotel-booking-` | 2026-07-29 |
| `logs:PutResourcePolicy`, `logs:PutDeliverySource`, `logs:PutDeliveryDestination`, `logs:CreateDelivery` | Allowed | A resource policy, delivery source, destination, and delivery were created and deleted | 2026-07-29 |
| `logs:GetLogEvents` | Allowed | A CodeBuild log stream was read back in two accounts | 2026-07-29, 2026-08-07 |
| `logs:ListTagsForResource` | Untested | Granted in `lab.template` on 2026-08-07 | |
| `xray:UpdateTraceSegmentDestination`, `xray:UpdateIndexingRule` | Untested | CloudWatch Transaction Search needs both. No grant in `lab.template` includes them | |
| `ec2:DescribeVpcs`, `ec2:DescribeSubnets`, `ec2:DescribeSecurityGroups`, `ec2:DescribeNetworkInterfaces` | Allowed | All four network lookups returned results | 2026-07-29 |
| `secretsmanager:GetSecretValue` | Allowed | Read a missing secret and a real one the run created | 2026-07-29 |
| `events:ListTargetsByRule` | Denied (SCP) | The refusal named a service control policy | 2026-07-29 |
| `cloudformation:ListStacks`, `cloudformation:ListStackResources` | Untested | No grant gives either to this role. The notebook teardown calls both and continues if they are refused | |
| `organizations:DescribeOrganization` | Allowed | Returned the organization. The workshop does not call it | 2026-08-12 |
| `aws-marketplace:ListEntities` | Allowed | The call succeeded. The workshop does not call it | 2026-08-12 |
| Everything else | Untested | | |

`bedrock-agentcore:SynchronizeGatewayTargets` is not tracked. The service does not support it for Lambda targets, and no workshop code calls it.

## Bedrock models

Every call below passed authorization for `bedrock:InvokeModel` or `bedrock:InvokeModelWithResponseStream`. What differs is model access on the account. Model access is set per account, so a result for one pool account may not hold for another.

A model is recorded as working only after repeated calls in one session succeed. A single passing call is not enough.

| Model or profile | Result | Date |
| --- | --- | --- |
| `us.anthropic.claude-sonnet-4-6` | Works on `Converse` and `ConverseStream`, 4 of 4 each | 2026-08-25 |
| `us.anthropic.claude-opus-4-6-v1` | Works on `Converse` and `ConverseStream`, 4 of 4 each | 2026-08-25 |
| `amazon.nova-2-multimodal-embeddings-v1:0` | Works. Returns one 1,024-dimension embedding, 4 of 4 | 2026-08-25 |
| `amazon.titan-embed-text-v2:0` | Works. Returns one 1,024-dimension embedding, 4 of 4 | 2026-08-25 |
| `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, `us.anthropic.claude-opus-4-5-20251101-v1:0`, `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Works on `Converse`. 90 of 90 calls across these and the two Claude models above returned output | 2026-08-13 |
| `us.anthropic.claude-sonnet-5`, `us.anthropic.claude-opus-5`, `us.anthropic.claude-opus-4-7`, `us.anthropic.claude-opus-4-8` | Not entitled. The answer says the model is not available for this account | 2026-08-12 to 2026-08-25 |
| `us.anthropic.claude-sonnet-4-20250514-v1:0`, `us.anthropic.claude-opus-4-1-20250805-v1:0` | Not available. The provider marks both as Legacy, and the account has not used them recently | 2026-08-12 |
| `global.anthropic.*` profiles | Blocked. The answer asks for an AWS Marketplace subscription. Not measured again after 2026-08-12 | 2026-08-12 |

## Reading a result

- **A late denial may mean the session ended:** A denial that names `voc-cancel-cred` means the session's credentials were revoked. It says nothing about the action. Re-run a call that worked earlier before recording any denial that arrives late in a session.
- **Empty credentials are not a result:** An ended session can return blank credential fields. The probe tools refuse to run on blank credentials or on the wrong account, so a printed result always names the expected account.
- **Pool accounts rotate:** Ending a session can move the student to another account in the pool. The accounts share one organization policy and one role template, so a permission row holds across them. Model access is the exception, because it is set per account.
