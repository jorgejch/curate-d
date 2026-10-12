"""Application settings.

Everything the application needs to reach the outside world arrives through the
environment; nothing here has a default that would silently work against production.
"""

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Final, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dbos._croniter import croniter  # type: ignore[attr-defined]
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ModelWrapValidatorHandler,
    PostgresDsn,
    SecretStr,
    UrlConstraints,
    ValidationError,
    WebsocketUrl,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails
from pydantic_settings import BaseSettings, SettingsConfigDict

from curate_d.domain.source import Provider

_LOCAL_LEDGER_DSN = "postgresql+psycopg://curate_d:curate_d@localhost:5433/curate_d"
_HIDDEN_INPUT: Final = "<hidden>"


def _without_input(error: ValidationError) -> ValidationError:
    """Return `error` rebuilt with every error's input replaced by a placeholder."""
    details: list[InitErrorDetails] = []
    for line in error.errors():
        detail: InitErrorDetails = {
            "type": line["type"],
            "loc": line["loc"],
            "input": _HIDDEN_INPUT,
        }
        if "ctx" in line:
            detail["ctx"] = line["ctx"]
        details.append(detail)
    return ValidationError.from_exception_data(error.title, details)


def _is_loopback(host: str | None) -> bool:
    """Return whether a URL host, as pydantic renders it, names this machine.

    Pydantic lowercases host names and keeps the brackets on an IPv6 literal, so
    `localhost`, any 127/8 address and `[::1]` all count.
    """
    if host is None:
        return False
    try:
        return ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return host == "localhost"


class StagingRole(StrEnum):
    """Which staging identity a process is configured to use.

    The two roles are the security boundary the profile split exists to enforce, so the
    role is configuration rather than something inferred from which credentials happen to
    be present. A CPU worker that has somehow been handed writer credentials still refuses
    to write, and the mismatch is visible in configuration rather than at the first PUT.
    """

    WRITER = "writer"
    READER = "reader"


class S3AddressingStyle(StrEnum):
    """How bucket names are placed in request URLs.

    RustFS is reached by path style locally; OVH serves virtual-hosted style. The design
    forbids inferring one backend's wire behavior from the other, so this is configured
    per environment and verified by the contract suite rather than auto-detected.
    """

    VIRTUAL = "virtual"
    PATH = "path"


class DeploymentEnvironment(StrEnum):
    """Identify the deployment to label on process telemetry."""

    LOCAL = "local"
    DEV = "dev"
    PROD = "prod"


class ScanRoot(BaseModel):
    """One configured subtree to inventory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Provider
    account: str = Field(min_length=1)
    root: str = Field(default="/", pattern=r"^/", description="Provider path of the subtree.")


class Settings(BaseSettings):
    """Configuration resolved from the environment, or a local .env during development."""

    model_config = SettingsConfigDict(
        env_prefix="CURATE_D_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: DeploymentEnvironment | None = Field(
        default=None,
        description="Deployment this process belongs to; labels its telemetry. Unset sends "
        "no label rather than a wrong one, so a deployment that forgets it is not mistaken "
        "for local.",
    )
    ledger_database_url: PostgresDsn = Field(
        default=PostgresDsn(_LOCAL_LEDGER_DSN),
        description="Ledger, audit log, application data, and DBOS's own tables (schema "
        "`dbos`): one database, so a ledger write and a workflow enqueue can share one "
        "transaction.",
    )
    credential_encryption_key: SecretStr | None = Field(
        default=None,
        description="Key protecting stored provider credentials. Required before any "
        "connection can be created.",
    )
    pcloud_client_id: str | None = Field(
        default=None,
        description="OAuth client identifier of the pCloud app. Unset disables pCloud "
        "authorization.",
    )
    pcloud_client_secret: SecretStr | None = Field(
        default=None,
        description="OAuth client secret of the pCloud app.",
    )
    pcloud_redirect_uri: HttpUrl | None = Field(
        default=None,
        description="Redirect URI registered with the pCloud app: the API's address as "
        "reached by the browser.",
    )
    connection_refresh_window_minutes: int = Field(
        default=60,
        ge=1,
        description="How long before expiry an access token is renewed. Kept several times "
        "the refresh schedule's interval, so one failed pass still leaves time for another "
        "before the token lapses.",
    )
    scan_roots: tuple[ScanRoot, ...] = Field(
        default=(),
        description="Subtrees to inventory, as JSON: "
        '[{"provider": "pcloud", "account": "4242", "root": "/Photos"}].',
    )
    scan_schedule: str = Field(
        default="0 * * * *",
        description="Cron schedule of incremental scans of every configured root.",
    )
    scan_folders_per_run: int = Field(
        default=200,
        ge=1,
        description="Folders one scan workflow lists before handing over to a continuation. "
        "Bounds the step history a recovered workflow replays.",
    )
    reconcile_schedule: str = Field(
        default="*/30 * * * *",
        description="Cron schedule of the reconciliation sweep over every content object.",
    )
    reconcile_batch_size: int = Field(
        default=200,
        ge=1,
        description="Content objects one reconciliation step evaluates.",
    )
    reconcile_batches_per_run: int = Field(
        default=10,
        ge=1,
        description="Batches one reconciliation workflow evaluates before handing over to a "
        "continuation. Bounds the step history a recovered workflow replays.",
    )
    destination_check_schedule: str = Field(
        default="15 4 * * *",
        description="Cron schedule of the check that every destination still holds its "
        "registered placements.",
    )
    full_rescan_schedule: str = Field(
        default="30 3 * * *",
        description="Cron schedule of full-rescan runs. Each run re-reads the files not "
        "verified within `full_rescan_interval_days`, so the pass rolls forward from wherever "
        "the last one stopped.",
    )
    full_rescan_interval_days: int = Field(
        default=30,
        ge=1,
        description="How long a file's content may go unverified. Configured independently "
        "of the incremental scan schedule.",
    )
    full_rescan_files_per_run: int = Field(
        default=500,
        ge=1,
        description="Files one full-rescan workflow re-reads before handing over to a "
        "continuation.",
    )
    provider_requests_per_minute: int = Field(
        default=300,
        ge=1,
        description="Request quota per provider, per worker process. pCloud publishes no "
        "quota; this is a conservative pace, not a documented limit.",
    )
    staging_capacity_retry_seconds: float = Field(
        default=60.0,
        gt=0,
        description="First durable wait when staging is full; doubles up to fifteen times this.",
    )
    provider_retry_seconds: float = Field(
        default=2.0,
        gt=0,
        description="First delay before retrying a transient provider failure; doubles "
        "for each of the two further attempts.",
    )
    local_timezone: str | None = Field(
        default=None,
        description="IANA zone that naive timestamps in files and file names are read in, "
        "such as a camera's EXIF clock. Unset leaves such a timestamp unplaced rather than "
        "guessing a zone; timestamps carrying their own offset never need it.",
    )
    classification_confidence_threshold: float = Field(
        default=0.8,
        gt=0.0,
        le=1.0,
        description="Confidence a tier must reach for its facts to be accepted; below it "
        "the next tier runs.",
    )
    openrouter_model: str | None = Field(
        default=None,
        description="Model the remote classification tier calls, as an OpenRouter slug. "
        "Named explicitly, never auto-routed, so a result can be reproduced. Unset disables "
        "the remote tier: what the cheaper tiers cannot settle goes to review.",
    )
    openrouter_api_key: SecretStr | None = Field(
        default=None,
        description="OpenRouter key; held by the I/O profile only, which makes the call.",
    )
    remote_spend_cap_usd: Decimal = Field(
        default=Decimal("5.00"),
        ge=Decimal("0"),
        description="Most the remote tier may spend per calendar month (UTC). Content that "
        "would exceed it is held for the next month rather than misclassified.",
    )
    remote_call_estimate_usd: Decimal = Field(
        default=Decimal("0.02"),
        gt=Decimal("0"),
        description="Reserved against the cap before each call and replaced by the reported "
        "cost after it, so concurrent calls cannot jointly overrun the cap by more than this.",
    )
    rclone_rc_url: HttpUrl | None = Field(
        default=None,
        description="The rclone sidecar's control API. Set on the I/O profile only; unset "
        "means this process performs no transfers.",
    )
    transfer_large_threshold_bytes: int = Field(
        default=256 * 1024**2,
        ge=1,
        description="Files this size or larger go to transfer-large and use multi-threaded "
        "streams; smaller ones to transfer-small.",
    )
    transfer_scratch_budget_bytes: int = Field(
        default=20 * 1024**3,
        ge=1,
        description="Bytes of concurrent transfers a worker admits, a quarter reserved for "
        "small files so large ones never starve them.",
    )
    transfer_retry_seconds: float = Field(
        default=60.0,
        gt=0,
        description="First wait before retrying a deferred or resource-starved transfer.",
    )
    oauth_state_max_age_seconds: int = Field(
        default=600,
        ge=30,
        description="How long an authorization may take from initiation to callback.",
    )
    workflow_retention_days: int = Field(
        default=30,
        ge=1,
        description="How long completed workflow records are kept. Configuration rather "
        "than a constant because the right value depends on how often anyone actually "
        "reads them. Audit entries are unaffected and are never pruned.",
    )
    workflow_retention_schedule: str = Field(
        default="20 5 * * *",
        description="Cron schedule of the workflow-history prune. Daily keeps each run to "
        "one day's worth of newly aged records, short enough not to hold the maintenance "
        "queue's one slot for long.",
    )
    recovery_sweep_schedule: str = Field(
        default="*/10 * * * *",
        description="Cron schedule of the recovery sweep, which forks work stranded on a "
        "retired version and re-enqueues work of executors that never return. Five fields, "
        "or six with seconds first, as DBOS reads them.",
    )
    conductor_enabled: bool = Field(
        default=False,
        description="Whether workers connect to DBOS Conductor. Separate from the key so the "
        "credential can stay provisioned while the switch is flipped in a reviewed commit.",
    )
    conductor_key: SecretStr | None = Field(
        default=None,
        description="DBOS Conductor API key. Used only when `conductor_enabled` is set, and "
        "never handed to DBOS's own configuration, so DBOS_CONDUCTOR_KEY is never read.",
    )
    conductor_url: WebsocketUrl | None = Field(
        default=None,
        description="Conductor websocket endpoint. Unset means the hosted Conductor. `wss://` "
        "unless the host is loopback: DBOS puts the key in the handshake path, so plaintext "
        "is only allowed to this machine.",
    )
    conductor_proxy_url: Annotated[HttpUrl, UrlConstraints(allowed_schemes=["http"])] | None = (
        Field(
            default=None,
            description="Plain-http egress proxy for the Conductor connection, and nothing "
            "else in the process. Set where direct egress is not available. "
            "Plain http is enough: the proxy sees only the CONNECT target, and the handshake "
            "and every frame cross it inside Conductor's TLS.",
        )
    )

    content_staging_root: Path = Field(
        default=Path("/var/lib/curate-d/staging"),
        description="Not read anywhere: content staging is S3-backed (see staging_s3_*).",
    )
    staging_max_bytes: int = Field(
        default=100 * 1024**3,
        ge=1,
        description="Ceiling on total staged bytes, measured from the S3 listing under "
        "the content prefix. Admission and eviction run against this rather than against "
        "whatever the backend would accept.",
    )
    staging_max_age_hours: int = Field(
        default=24,
        ge=1,
        description="Age at which a staged entry becomes evictable.",
    )

    staging_s3_endpoint_url: HttpUrl | None = Field(
        default=None,
        description="S3 endpoint backing content staging: RustFS locally, OVH Standard S3 "
        "in the cluster. Unset disables S3 staging entirely rather than falling back to a "
        "guessed endpoint.",
    )
    staging_s3_bucket: str = Field(
        default="",
        description="Staging bucket for this environment. Dev and prod never share one: a "
        "shared bucket would let either environment's cleanup delete the other's media.",
    )
    staging_s3_region: str = Field(
        default="US-EAST-VA",
        description="Signing region of the object storage endpoint.",
    )
    staging_s3_role: StagingRole = Field(
        default=StagingRole.READER,
        description="Which identity this process uses. Defaults to reader so a profile "
        "that forgets to declare itself gets the weaker of the two, never write authority.",
    )
    staging_s3_access_key_id: SecretStr | None = Field(
        default=None,
        description="Staging access key. Explicit because the SDK's ambient credential "
        "chain would happily fall back to an instance role or a developer's ~/.aws, which "
        "is how a CPU worker ends up holding authority nobody granted it.",
    )
    staging_s3_secret_access_key: SecretStr | None = Field(
        default=None,
        description="Secret for the configured staging access key.",
    )
    staging_s3_addressing_style: S3AddressingStyle = Field(
        default=S3AddressingStyle.VIRTUAL,
        description="URL addressing style for the configured endpoint.",
    )
    staging_s3_verify_tls: bool = Field(
        default=True,
        description="Whether to verify the endpoint's certificate. Only ever disabled for "
        "a local plaintext fixture; OVH requires a verified HTTPS connection.",
    )
    staging_s3_content_prefix: str = Field(
        default="content/",
        description="Key prefix holding staged media. A read-only identity "
        "limited to this prefix, so changing it here without changing the policy "
        "denies every read.",
    )
    staging_transfer_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        description="Ceiling on a single upload or download. A transfer that hangs past "
        "this fails rather than holding the writer lock until the pod is killed.",
    )
    staging_local_scratch_root: Path = Field(
        default=Path("/var/lib/curate-d/scratch"),
        description="Where verified local read snapshots are materialized so decoders get "
        "a seekable file. Disposable: bounded by the reservation below, never a record.",
    )
    staging_local_scratch_max_bytes: int = Field(
        default=20 * 1024**3,
        ge=1,
        description="Ceiling on concurrent local snapshot bytes. Reservations are held "
        "until each handle closes, so this bounds the pod's scratch use across readers "
        "rather than trusting the node's disk to be large enough.",
    )

    @field_validator("recovery_sweep_schedule")
    @classmethod
    def _recovery_sweep_schedule_is_a_cron(cls, value: str) -> str:
        """Reject a cron DBOS would refuse, before the lease and the launch.

        The check is the one DBOS's schedule row builder applies (`stored_form`): failing
        here stops a worker at settings load, where `apply_schedules` would instead fail
        after launch and crash-loop every I/O worker.
        """
        if not croniter.is_valid(value, second_at_beginning=True):
            raise ValueError(f"recovery_sweep_schedule {value!r} is not a valid cron expression")
        return value

    @field_validator("conductor_key", mode="before")
    @classmethod
    def _blank_conductor_key_is_missing(cls, value: object) -> object:
        """Treat an empty or whitespace-only key as unset.

        An unset environment variable and an empty one are the same operator intent: no
        key. Collapsing them here keeps `start_conductor` on its one misconfigured path
        instead of handing a blank credential to the connection gate.
        """
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, SecretStr) and not value.get_secret_value().strip():
            return None
        return value

    @model_validator(mode="after")
    def _validate_conductor_transport(self) -> Self:
        """Keep a plaintext Conductor connection on this machine.

        DBOS sends the key in the websocket handshake path and workflow data in the frames,
        so a `ws://` endpoint exposes both to the network. The local fixtures serve on
        loopback, which is the one place plaintext never leaves the host. A proxy is a
        `CONNECT` tunnel: under `wss://` it carries ciphertext whatever its own scheme, but
        under `ws://` it carries the handshake itself, so it has to be loopback too.

        Raises:
            ValueError: `conductor_url` is `ws://` to a host other than loopback, or is
                `ws://` through a proxy on a host other than loopback.
        """
        if self.conductor_url is None or self.conductor_url.scheme == "wss":
            return self
        if not _is_loopback(self.conductor_url.host):
            raise ValueError(
                f"conductor_url {str(self.conductor_url)!r} is plaintext to a host that is "
                "not loopback. The Conductor key travels in the websocket handshake path, "
                "so use wss://; ws:// is only allowed to localhost, 127.0.0.1 or [::1]."
            )
        if self.conductor_proxy_url is not None and not _is_loopback(self.conductor_proxy_url.host):
            raise ValueError(
                f"conductor_proxy_url {str(self.conductor_proxy_url)!r} is not loopback but "
                "conductor_url is plaintext, so the Conductor key would cross the network in "
                "the handshake to the proxy. Use wss://, or a proxy on this machine."
            )
        return self

    @model_validator(mode="after")
    def _validate_staging_s3(self) -> Self:
        """Reject partial S3 staging configuration rather than failing at first use.

        Raises:
            ValueError: If an endpoint is configured without the bucket and explicit
                credentials it needs, if a plaintext endpoint is used with TLS
                verification left on, or if the content prefix is empty or malformed.
        """
        if self.staging_s3_endpoint_url is None:
            return self

        missing = [
            name
            for name, value in (
                ("staging_s3_bucket", self.staging_s3_bucket),
                ("staging_s3_access_key_id", self.staging_s3_access_key_id),
                ("staging_s3_secret_access_key", self.staging_s3_secret_access_key),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "S3 staging needs an explicit bucket and credentials; missing "
                f"{', '.join(missing)}. There is no ambient credential fallback."
            )

        if self.staging_s3_endpoint_url.scheme != "https" and self.staging_s3_verify_tls:
            raise ValueError(
                "staging_s3_endpoint_url is plaintext but staging_s3_verify_tls is set. "
                "Verification cannot apply to http; disable it deliberately for a local "
                "fixture, or use https."
            )

        if not self.staging_s3_content_prefix:
            raise ValueError(
                "staging_s3_content_prefix must not be empty. It is the readiness "
                "marker's own key and the bound on every listing, so an empty value "
                "would scope staging to the whole bucket."
            )
        if self.staging_s3_content_prefix.startswith("/"):
            raise ValueError("staging_s3_content_prefix must not start with '/'.")
        if not self.staging_s3_content_prefix.endswith("/"):
            raise ValueError(
                "staging_s3_content_prefix must end with '/' so it matches the policy "
                "grant on a prefix rather than on keys that merely share its leading text."
            )
        return self

    @model_validator(mode="after")
    def _validate_local_timezone(self) -> Self:
        """Reject a zone name the system does not know.

        Raises:
            ValueError: `local_timezone` is not an IANA zone.
        """
        if self.local_timezone is not None:
            try:
                ZoneInfo(self.local_timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError(
                    f"local_timezone {self.local_timezone!r} is not an IANA time zone"
                ) from None
        return self

    # Keep this validator last: each model validator wraps the ones defined before it, so
    # one defined after this would raise with the resolved settings still attached.
    @model_validator(mode="wrap")
    @classmethod
    def _hide_input_in_errors(cls, data: object, handler: ModelWrapValidatorHandler[Self]) -> Self:
        """Re-raise a validation error without the input that failed.

        A model validator's error carries every resolved setting as its input, secrets
        unwrapped, and a field's error carries the raw value, which `SecretStr` has not
        wrapped yet. Both would reach the traceback of a worker that fails at startup,
        before any log filter is installed, and stay on the exception for whoever catches
        it.

        Raises:
            ValidationError: the settings are invalid; each error keeps its type, location,
                message and context, with its input replaced.
        """
        try:
            return handler(data)
        except ValidationError as error:
            hidden = _without_input(error)
        # Raised outside the handler so the original error is not kept as its context.
        raise hidden

    @property
    def s3_staging_enabled(self) -> bool:
        """Return whether an S3 staging endpoint is configured for this process."""
        return self.staging_s3_endpoint_url is not None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return process-wide settings. Cached so a misconfigured environment fails once, loudly."""
    return Settings()
