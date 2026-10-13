"""Drive `workshop_lab.build_source` with fake ECR and S3 clients, offline.

The assertions that matter are about the archive layout and about what teardown
is allowed to delete. A zip whose Dockerfile is one directory down builds nothing
and reports a CodeBuild failure that says only "COPY failed", and a teardown that
deletes the shared source bucket takes out every other run in the account.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from botocore.exceptions import ClientError

from workshop_lab.build_source import DOCKERFILE, SERVER, BuildSource, bundle
from workshop_lab.harness import FAIL, PASS, Harness
from workshop_lab.naming import Names

ACCOUNT = "123456789012"
REGION = "us-east-1"


def denied(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "explicit deny"}}, operation
    )


class FakeEcr:
    def __init__(self, refuse: set[str] = frozenset()) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.refuse = set(refuse)

    def create_repository(self, **kwargs):
        self.calls.append(("create_repository", kwargs))
        if "create_repository" in self.refuse:
            raise denied("CreateRepository")
        return {"repository": {"repositoryName": kwargs["repositoryName"]}}

    def delete_repository(self, **kwargs):
        self.calls.append(("delete_repository", kwargs))
        return {}


class FakeS3:
    def __init__(
        self, buckets: set[str] = frozenset(), refuse: set[str] = frozenset()
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.buckets = set(buckets)
        self.refuse = set(refuse)

    def head_bucket(self, **kwargs):
        self.calls.append(("head_bucket", kwargs))
        if kwargs["Bucket"] not in self.buckets:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadBucket"
            )
        return {}

    def create_bucket(self, **kwargs):
        self.calls.append(("create_bucket", kwargs))
        if "create_bucket" in self.refuse:
            raise denied("CreateBucket")
        self.buckets.add(kwargs["Bucket"])
        return {}

    def put_bucket_tagging(self, **kwargs):
        self.calls.append(("put_bucket_tagging", kwargs))
        if "put_bucket_tagging" in self.refuse:
            raise denied("PutBucketTagging")
        return {}

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        if "put_object" in self.refuse:
            raise denied("PutObject")
        return {}

    def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))
        return {}


class FakeCredentials:
    access_key, secret_key, token = "AKIA", "secret", "token"

    def get_frozen_credentials(self):
        return self


class FakeSession:
    def __init__(self, ecr: FakeEcr, s3: FakeS3) -> None:
        self.clients = {"ecr": ecr, "s3": s3}
        self.region_name = REGION

    def get_credentials(self):
        return FakeCredentials()

    def client(self, name: str, region_name: str | None = None):
        return self.clients[name]


def lab_with(ecr: FakeEcr, s3: FakeS3) -> Harness:
    """A harness past step 2, without the STS call that normally gets it there."""
    lab = Harness(prefix="verify", session=FakeSession(ecr, s3), echo=lambda _: None)
    lab.account_id = ACCOUNT
    lab._names = Names(prefix="verify", account_id=ACCOUNT, region=REGION)
    return lab


# --- the archive --------------------------------------------------------------
def test_both_source_files_sit_at_the_archive_root():
    """CodeBuild builds the extracted directory. A nested Dockerfile cannot COPY
    a sibling that is no longer a sibling."""
    with zipfile.ZipFile(io.BytesIO(bundle())) as archive:
        assert sorted(archive.namelist()) == ["Dockerfile", "server.py"]


def test_the_archive_carries_the_source_verbatim():
    with zipfile.ZipFile(io.BytesIO(bundle())) as archive:
        assert archive.read("Dockerfile").decode() == DOCKERFILE
        assert archive.read("server.py").decode() == SERVER


def test_the_base_image_is_the_one_that_measures_egress():
    """An ECR Public base would build without ever leaving AWS, and step 8 would
    report success while measuring nothing about outbound internet."""
    assert "ghcr.io/astral-sh/uv:python3.12-bookworm-slim" in DOCKERFILE


def test_the_server_answers_both_routes_the_runtime_contract_requires():
    assert "/ping" in SERVER
    assert "/invocations" in SERVER
    assert "8080" in DOCKERFILE and "8080" in SERVER


def test_the_server_is_valid_python():
    """It is a string here and a file in the container, so nothing else compiles
    it before AgentCore tries to start it."""
    compile(SERVER, "server.py", "exec")


# --- ECR ----------------------------------------------------------------------
def test_the_repository_is_created_under_the_derived_name():
    ecr, s3 = FakeEcr(), FakeS3()
    lab = lab_with(ecr, s3)
    BuildSource(lab).create_repository()
    assert ecr.calls[0][1]["repositoryName"] == "bedrock-agentcore-verify"
    assert ecr.calls[0][1]["tags"] == lab.names.tags_list


def test_the_repository_delete_forces_past_the_images_in_it():
    """By teardown the repository holds the image step 8 pushed, and an ordinary
    delete on a non-empty repository is refused."""
    ecr, s3 = FakeEcr(), FakeS3()
    lab = lab_with(ecr, s3)
    BuildSource(lab).create_repository()
    for _, delete in lab.cleanups:
        delete()
    assert (
        "delete_repository",
        {"repositoryName": "bedrock-agentcore-verify", "force": True},
    ) in ecr.calls


def test_a_refused_repository_registers_no_cleanup():
    ecr, s3 = FakeEcr(refuse={"create_repository"}), FakeS3()
    lab = lab_with(ecr, s3)
    assert BuildSource(lab).create_repository() is False
    assert lab.cleanups == []


# --- the shared bucket --------------------------------------------------------
def test_an_existing_bucket_passes_and_is_not_created_again():
    """One bucket per account and region, reused. Already there is the ordinary
    case, not a failure."""
    s3 = FakeS3(buckets={f"bedrock-agentcore-codebuild-sources-{ACCOUNT}-{REGION}"})
    lab = lab_with(FakeEcr(), s3)
    BuildSource(lab).ensure_bucket()

    assert [name for name, _ in s3.calls] == ["head_bucket", "put_bucket_tagging"]
    assert lab.results[0] == (
        "s3:CreateBucket source bucket",
        PASS,
        "already exists, reused",
    )


def test_a_missing_bucket_is_created():
    s3 = FakeS3()
    lab = lab_with(FakeEcr(), s3)
    BuildSource(lab).ensure_bucket()
    assert [name for name, _ in s3.calls] == [
        "head_bucket",
        "create_bucket",
        "put_bucket_tagging",
    ]


def test_a_refused_bucket_is_reported_as_a_failure():
    s3 = FakeS3(refuse={"create_bucket"})
    lab = lab_with(FakeEcr(), s3)
    BuildSource(lab).ensure_bucket()
    assert [verdict for _, verdict, _ in lab.results] == [FAIL]


def test_a_bucket_that_could_not_be_created_is_not_then_tagged():
    """PutBucketTagging on a bucket that does not exist reports NoSuchBucket, and
    a second failure for one refusal reads as two separate problems."""
    s3 = FakeS3(refuse={"create_bucket"})
    lab = lab_with(FakeEcr(), s3)
    BuildSource(lab).ensure_bucket()
    assert "put_bucket_tagging" not in [name for name, _ in s3.calls]


def test_the_shared_bucket_carries_the_workshop_tag():
    """CreateBucket takes no tags of its own, so the sweep would only ever find
    this bucket by its computed name without the second call."""
    s3 = FakeS3()
    lab = lab_with(FakeEcr(), s3)
    BuildSource(lab).ensure_bucket()
    tagging = next(kwargs for name, kwargs in s3.calls if name == "put_bucket_tagging")
    assert tagging["Tagging"] == {"TagSet": lab.names.tags_list}


def test_no_cleanup_ever_deletes_the_shared_bucket():
    """It belongs to the account, not to this run. Deleting it takes out every
    other run's build source."""
    s3 = FakeS3()
    lab = lab_with(FakeEcr(), s3)
    BuildSource(lab).prepare()
    for _, delete in lab.cleanups:
        delete()
    assert "delete_bucket" not in [name for name, _ in s3.calls]


# --- the upload ---------------------------------------------------------------
def test_the_object_lands_at_the_key_codebuild_is_pointed_at():
    s3 = FakeS3()
    lab = lab_with(FakeEcr(), s3)
    source = BuildSource(lab)
    source.upload()

    put = next(kwargs for name, kwargs in s3.calls if name == "put_object")
    assert put["Bucket"] == lab.names.source_bucket
    assert put["Key"] == "verify/source.zip"
    assert put["Body"] == source.zip_bytes


@pytest.mark.parametrize(
    "operation", ["head_bucket", "put_bucket_tagging", "put_object", "delete_object"]
)
def test_every_s3_call_names_the_expected_owner(operation):
    """A bucket name is global. Without ExpectedBucketOwner a typo can read from
    or write to somebody else's bucket and report a pass."""
    s3 = FakeS3(buckets={f"bedrock-agentcore-codebuild-sources-{ACCOUNT}-{REGION}"})
    lab = lab_with(FakeEcr(), s3)
    BuildSource(lab).prepare()
    for _, delete in lab.cleanups:
        delete()
    calls = [kwargs for name, kwargs in s3.calls if name == operation]
    assert calls
    assert all(kwargs["ExpectedBucketOwner"] == ACCOUNT for kwargs in calls)


def test_no_s3_call_is_left_out_of_the_owner_check():
    """The parametrised list above is only a guard while it names every call.

    A call added to `BuildSource` without an entry there would be unguarded and
    the suite would still be green, which is the failure this test exists for.

    `create_bucket` is the one exemption and it is not a gap: the API takes no
    `ExpectedBucketOwner`, because there is no owner to expect until the call
    itself succeeds. Everything after it names one.
    """
    guarded = {"head_bucket", "put_bucket_tagging", "put_object", "delete_object"}
    s3 = FakeS3()
    lab = lab_with(FakeEcr(), s3)
    BuildSource(lab).prepare()
    for _, delete in lab.cleanups:
        delete()
    assert {name for name, _ in s3.calls} <= guarded | {"create_bucket"}


def test_teardown_removes_only_this_runs_object():
    s3 = FakeS3()
    lab = lab_with(FakeEcr(), s3)
    BuildSource(lab).prepare()
    for _, delete in lab.cleanups:
        delete()
    deleted = [kwargs for name, kwargs in s3.calls if name == "delete_object"]
    assert deleted == [
        {
            "Bucket": f"bedrock-agentcore-codebuild-sources-{ACCOUNT}-{REGION}",
            "Key": "verify/source.zip",
            "ExpectedBucketOwner": ACCOUNT,
        }
    ]


def test_a_refused_upload_registers_no_cleanup_and_fails_prepare():
    s3 = FakeS3(refuse={"put_object"})
    lab = lab_with(FakeEcr(), s3)
    source = BuildSource(lab)
    assert source.prepare() is False
    assert [label for label, _ in lab.cleanups] == [
        "ecr repository bedrock-agentcore-verify"
    ]


def test_prepare_is_true_only_when_both_the_repository_and_the_object_exist():
    lab = lab_with(FakeEcr(), FakeS3())
    assert BuildSource(lab).prepare() is True
