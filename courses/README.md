# One file per course

Every tool here reads `.env` at the repository root. A file in this directory is
layered over it, so a course states only what makes it a different course and
inherits the rest.

```bash
cp courses/course.env.example courses/graphrag.env    # fill in the ids
```

`--course NAME` reads `courses/NAME.env`. `VOCAREUM_COURSE=NAME` in `.env` or in
the real environment does the same thing without the flag, and the flag wins
when both are set. Passing no course at all reads `.env` alone, which is what
this repository did before the directory existed.

## What goes where

| Value | Where it lives | Why |
| --- | --- | --- |
| API token, region, organization identifiers | `.env` | The same for every course. A rotated token should be pasted once |
| Course, assignment, part, user ids | `courses/<name>.env` | These are what make a course a course |
| Lab account id, stack name, deployer role | `courses/<name>.env` | Properties of that course's part, and they expire with the session |

Precedence is real environment variables, then the course file, then `.env`. So
exporting a variable in a shell overrides both files, which is what keeps CI
runs working with no file at all.

## Rules

**Every `*.env` here is git-ignored.** These files carry real account and
Vocareum identifiers. `course.env.example` is committed and is the only file in
here that may hold no real value.

**A named course with no file is an error, not a fallback.** `--course brazil`
with no `courses/brazil.env` refuses and lists the courses that do have a file.
The alternative is a tool that runs against `.env` and reports success for a
course it never touched.

**The account and stack values expire.** Vocareum rotates a pool of accounts and
ending a session moves the allocation, so `LAB_AWS_ACCOUNT_ID` and
`LAB_STACK_NAME` describe the session that is open now. On a new course, both
lines can start blank. See
[Refresh the account and stack pins](../README.md#refresh-the-account-and-stack-pins)
for the commands that fill them in.

**The gate manifest is per course too.** `vocareum-expect --course graphrag`
reads only that course's own `expected.json`. A missing named-course manifest is
an error. It never falls back to another course's manifest. A manifest carries
no secrets, so unlike the env files it is committed.

**The lab template is per course.** `vocareum-upload --dry-run --course
graphrag` selects that course's own `lab.template`. A named course can upload
`lab.template` only from its own directory. Every real upload requires a named
course. This keeps the legacy first-course template from reaching another
course by mistake.

**The template must fit the inline CloudFormation limit.** Vocareum submits
`lab.template` as `TemplateBody`, so the file must be 51,200 bytes or smaller.
The GraphRAG validator checks the byte count before an upload. CloudFormation
lint alone does not catch this delivery limit.

**The GraphRAG course uses Vocareum Notebook/Jupyter.** Its template creates a
no-cost CloudFormation launch sentinel because AWS rejects an empty `Resources`
map, plus the tested inline policy that lets the existing `voclabs` session role
invoke the required Bedrock models. It creates no service workload. Vocareum
provides the notebook environment and AWS session credentials. Students create
an AuraDB Free database through the separate signup flow and add their own
connection values. Workshop notebooks and supporting files belong in Vocareum
starter code, not in an EC2 bootstrap.

**Every course owns one asset directory.** The original course is
`neo4j-fundamentals`; GraphRAG is `graphrag`. Each directory contains
`course.json`, `lab.template`, and `expected.json`. The ignored
`courses/NAME.env` file contains its Vocareum coordinates.

**A course may keep that directory in its own repository.** Set
`COURSE_ASSET_DIR` in `courses/NAME.env` to the path of that repository's
Vocareum asset directory. Every asset lookup then reads from there.

```
# courses/graphrag.env
COURSE_ASSET_DIR=../neo4j-hotel-booking-agent-workshop/environment/vocareum
```

- **The path is read relative to this repository's root.** An absolute path
  works too, and a leading `~` expands.
- **A missing directory is an error.** The tooling names the key, the value, and
  the resolved path, then stops. It never reads `courses/NAME/` instead, because
  that would upload one course's template under another course's name and report
  success.
- **A fresh clone does not know where the course lives.** `courses/*.env` is
  git-ignored, so this line has to be written once per checkout.
  `course.env.example` documents it.
- **The dependency runs one way.** This repository reads the course repository.
  The course repository depends on nothing here, so it works for everyone who
  clones it without this tooling.
- **The directory has to agree that it belongs to the course.** Every lookup
  checks the `course_key` in the `course.json` it lands in. Exporting
  `COURSE_ASSET_DIR` in a shell sets it for every course, not one, and `upload`
  and `expect` resolve a path without reading `course.json`, so nothing else
  would catch it. The check makes that a refusal instead of one course's
  template published under another course's name.

`neo4j-fundamentals` sets no pointer and stays here. It is the offline test case
for the default path.

**Each course states its own starter-code source.** The `startercode` block in
`course.json` holds the Git remote to check, the pinned commit to read, the
checkout path relative to the asset directory, and the file list.

```json
"startercode": {
  "repository": "https://github.com/neo4j-partners/neo4j-hotel-booking-agent-workshop.git",
  "commit": "19cb2333e7e86ca4f9bc9b0a766ad52b19665619",
  "source": "..",
  "paths": [".env.example", "README.md", "notebooks", "setup/verify_setup.py"]
}
```

- **A course with no `startercode` block ships no student package.**
  `vocareum-package-startercode` and `vocareum-release` say that rather than
  claim packaging works for one course only.
- **Either spelling of the remote is accepted.** A clone over SSH and a clone
  over HTTPS name the same repository.
- **The commit has to be bumped after a content change.** The package is built
  from the pinned commit, so an uncommitted edit never reaches a student and an
  unbumped commit ships the old files. Check `paths` in the same edit. A bump
  that renames or moves a listed path fails the build.
- **`lab.template` pins the same commit.** Its `Metadata.SourceCommit` must
  equal `startercode.commit`. `./scripts/validate_all.py` compares them.
- **Vocareum's extractor drops dotfiles.** A course names a visible replacement
  in `startercode.COURSE_ADAPTERS` and adapts whatever reads the file. Packaging
  an unrenamed dotfile stops the build rather than shipping a workspace that is
  quietly missing it.

**A course file does not make a course.** It names an existing Vocareum part.
Add the course to `LINTED_COURSES` in `scripts/validate_all.py`, and run that
script before any upload.

## Management scope

`vocareum-course-setup` inspects an existing course, compares it with the
committed definition, and can write or refresh local scaffold files. Its remote
access is read-only. It does not clone courses, publish assignments, enroll
students, or start student sessions. Those shared-state operations are deferred
until a concrete workflow requires them.

Publishing course assets and restoring a prior release belong to
`vocareum-release`, which is preview-first and requires an explicit prepared
release ID before it writes.

For normal development, `uv run pytest` runs the focused management suite.
Run `./scripts/validate_all.py` before a release or in CI to add the separate
student-runtime suite and repository-wide static checks.

## Building and publishing

[How a course goes live](../README.md#how-a-course-goes-live) in the main README
covers building, publishing, verifying, and rolling back a course.
