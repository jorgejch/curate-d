# Security policy

## Reporting a vulnerability

Report it privately through
[GitHub's private vulnerability reporting](https://github.com/jorgejch/curate-d/security/advisories/new).
Don't open a public issue, pull request or discussion for it.

Include what you can of:

- the affected component (API, I/O worker, CPU worker, staging, a provider adapter, a
  migration) and the commit or version;
- how to reproduce it, and what an attacker gains;
- whether the provider credentials, the ledger, staged content or the originals are
  exposed.

You'll get an acknowledgement within 7 days. A fix is developed in a private fork of the
advisory, and the advisory is published, crediting you unless you'd rather not be named,
once a release carries the fix.

## Supported versions

curate.d is pre-release. Fixes land on `dev` and ship in the next release; no earlier
version receives backports.

| Version | Supported |
| --- | --- |
| `dev` and the latest release | yes |
| anything older | no |

## Scope

curate.d holds credentials for a person's cloud accounts and can, by design, release the
originals once a copy is verified. In scope are, in particular:

- reading or exfiltrating provider credentials, rclone remotes or staging-bucket keys;
- reaching the control-plane API or its write commands without authorization;
- making a source eligible for deletion without a verified copy, or moving content to a
  destination the policy doesn't name;
- reading another provider's or person's content through staging, the CPU worker's
  read-only identity or the classification path, including content sent to the LLM tier;
- tampering with the ledger or the audit trail so that it misreports what happened.

Out of scope: a provider's own vulnerabilities (report those to the provider), weaknesses
that need an already-compromised Kubernetes cluster or database superuser, and findings
from automated scanners with no demonstrated impact.

## Deployment

Deployment manifests live outside this repository. Running curate.d securely is the
deploying operator's responsibility: keep the API off the public internet, give each worker
profile only its own credentials, and store secrets in a secret manager rather than in
manifests.
