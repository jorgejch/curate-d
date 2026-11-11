## MODIFIED Requirements

### Requirement: A dev image is published on every push to dev
A push to `dev` MUST build the container image from that commit and push it to the
registry with a tag of the form `dev-<UTC timestamp>-<sha>`, where the timestamp is the
commit's committer time, so every build of one commit gets the same tag. The tag MUST be the
image's build revision and its DBOS application version. The build MUST NOT wait on any test
job. No other branch push MUST publish an image.

#### Scenario: Push to dev
- **WHEN** a commit lands on `dev`
- **THEN** one image tagged `dev-<commit timestamp>-<sha>` is pushed, with the tag as its build
  revision, and the image runs as DBOS application version `dev-<commit timestamp>-<sha>`

#### Scenario: Rebuild of a commit
- **WHEN** the build of a commit already on `dev` runs again
- **THEN** it pushes the same tag the first build pushed, and runs as the same DBOS version

#### Scenario: Rebuild of an older commit
- **WHEN** the build of an older `dev` commit runs again after a newer commit was published
- **THEN** its tag sorts before the newer commit's, so the dev environment keeps the newer image

#### Scenario: Push to main without a tag
- **WHEN** a commit lands on `main` and no tag is pushed
- **THEN** no image is built or pushed

#### Scenario: Pull request
- **WHEN** a pull request is opened or updated
- **THEN** no image is pushed to the registry
