# Spec Delta

## ADDED Requirements

### Requirement: The image installs third-party dependencies from wheels only
Every `uv sync` in `docker/Dockerfile` MUST pass `--locked` and `--no-build`, so the image
build installs each third-party dependency from a wheel `uv.lock` pins and never runs a
dependency's build script. The project's own package is still built from source by its
declared backend. A unit test MUST pin the flags on every `uv sync` line.

#### Scenario: Image build with wheel-only dependencies
- **WHEN** the image is built from a `uv.lock` in which every third-party package has a wheel
  for the image's platform
- **THEN** the build installs the dependencies and the project, and runs no third-party build
  script

#### Scenario: A dependency ships only as a source distribution
- **WHEN** a lock update adds a third-party package with no wheel for the image's platform
- **THEN** the image build fails at the `uv sync` step naming the package, and no image is
  published

#### Scenario: A uv sync loses the flag
- **WHEN** a `uv sync` line in `docker/Dockerfile` omits `--no-build` or `--locked`
- **THEN** the unit suite fails naming the line
