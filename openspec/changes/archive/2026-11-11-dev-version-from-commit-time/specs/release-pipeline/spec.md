## MODIFIED Requirements

### Requirement: A dev image is published on every push to dev
A push to `dev` MUST build the container image from that commit and push it to the
registry tagged `dev-<UTC timestamp>-<sha>-<environment hash>` and with the commit alias
`dev-<UTC timestamp>-<sha>`, where the timestamp is the commit's committer time. The hashed
tag MUST be the image's build revision and its DBOS application version. The build MUST NOT
wait on any test job. No other branch push MUST publish an image.

#### Scenario: Push to dev
- **WHEN** a commit lands on `dev`
- **THEN** one image tagged `dev-<commit timestamp>-<sha>-<environment hash>` and
  `dev-<commit timestamp>-<sha>` is pushed, with the hashed tag as its build revision, and the
  image runs as DBOS application version `dev-<commit timestamp>-<sha>-<environment hash>`

#### Scenario: Rebuild of an older commit
- **WHEN** the build of an older `dev` commit that was never published runs after a newer commit
  was published
- **THEN** its tag sorts before the newer commit's, so the dev environment keeps the newer image

#### Scenario: Push to main without a tag
- **WHEN** a commit lands on `main` and no tag is pushed
- **THEN** no image is built or pushed

#### Scenario: Pull request
- **WHEN** a pull request is opened or updated
- **THEN** no image is pushed to the registry

## ADDED Requirements

### Requirement: A dev tag names the image's system packages
A dev tag's environment hash MUST be the first 12 hex digits of the SHA-256 of the image's
sorted list of installed system packages, the one image input the commit does not fix. An
image whose own list does not hash to its tag MUST fail to build, and MUST refuse to run as
that tag.

#### Scenario: Tag hashed from the image's own packages
- **WHEN** the image is built with a dev tag whose environment hash is its package list's
- **THEN** the build succeeds and the image runs as that tag

#### Scenario: Tag hashed from other packages
- **WHEN** the image is built with a dev tag whose environment hash is not its package list's
- **THEN** the build fails and nothing is pushed

#### Scenario: Wrong tag at run time
- **WHEN** an image runs with a dev tag whose environment hash is not its package list's, or
  without a readable package list
- **THEN** the application fails at import

### Requirement: A published image is never pushed again
A run MUST push nothing when its build is already published: on `dev`, when the commit alias
exists in the registry; on a release, when the `vMAJOR.MINOR.PATCH` tag exists. Only a "not
found" answer MUST count as unpublished.

#### Scenario: Rebuild of a dev commit
- **WHEN** the build of a commit whose commit alias is already published runs again, with the
  same system packages or with others
- **THEN** it pushes nothing, the commit keeps the image it was first published with, and no
  two dev tags share a timestamp

#### Scenario: Release build runs again
- **WHEN** the build of `v1.4.0` runs again after its image was published
- **THEN** it pushes nothing, and `v1.4.0` and `v1.4` keep the first image

#### Scenario: Registry lookup fails
- **WHEN** the registry answers the lookup with anything but "not found"
- **THEN** the run fails and nothing is pushed
