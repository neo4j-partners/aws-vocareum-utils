# Neo4j runs on Aura, not in the lab account

## The decision

The Graph-RAG module connects to **Neo4j Aura free tier**. Participants sign up at
[console.neo4j.io](https://console.neo4j.io) and create their own instance. Aura is fully
managed and external to AWS.

**Aura sign-up happens separately, outside Vocareum, and nothing has to be done for it
here.** No AWS resource, no template change, no permission, no Vocareum request. The lab
account never knows the instance exists.

## What follows from that

- **No AWS resource.** No ECS Fargate service, no network load balancer on port 7687, no VPC endpoint, no self-managed database instance. The lab account never hosts a database.
- **No permission row.** Aura needs no AWS action from either principal, so it produces nothing to measure and nothing to ask Vocareum for.
- **No secret to store.** The instance is the participant's, and so are its credentials. The workshop does not write them into Secrets Manager and does not read them back out.
- **No stack output.** Connection details cannot come out of CloudFormation, because the instance does not exist when the stack runs and the lab account knows nothing about it.
- **No lab-side setup at all.** Nothing about Aura is provisioned, checked, or torn down by the lab. It is a prerequisite the participant brings with them.
- **Region does not apply.** Aura chooses its own cloud region at sign-up. It has no bearing on the lab's `us-east-1` constraint, which comes from AgentCore and the Nova 2 embeddings model.

## The one change this forces in the content

The workshop content connects to `bolt://localhost:7687`, which was the self-hosted
assumption. Aura is TLS only, so every connection string becomes:

```
neo4j+s://<instance-id>.databases.neo4j.io
```

The instance ID, the username `neo4j`, and the generated password all come from the
credentials file Aura offers for download at instance creation. The participant has them
before the AWS lab starts and pastes them into the notebook.
