"""Deployment configuration, read from the environment.

12-factor: nothing here is hardcoded or committed; secrets are held in secret types and
never logged (10-deployment-and-operations.md § configuration & secrets).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_ASYNC_DRIVER = "postgresql+psycopg"


class Settings(BaseSettings):
    """Every deployment knob, prefixed `SE_` so it cannot collide with unrelated variables."""

    model_config = SettingsConfigDict(
        env_prefix="SE_",
        env_file=".env",
        env_file_encoding="utf-8",
        # The compose `.env` also carries variables meant for other containers; ignore them
        # rather than failing to start.
        extra="ignore",
    )

    app_env: Literal["development", "production"] = "production"
    log_level: LogLevel = "INFO"

    database_url: SecretStr
    """PostgreSQL DSN. Required — the service must not invent a default datastore."""

    api_docs_enabled: bool = True
    """Serve the OpenAPI schema to authenticated users (08-api-principles.md)."""

    cors_allow_origins: Annotated[tuple[str, ...], NoDecode] = ()
    """Deny by default: an empty list installs no CORS middleware at all.

    `NoDecode` is load-bearing: without it pydantic-settings JSON-decodes the raw
    environment value before any validator runs, so the obvious `SE_CORS_ALLOW_ORIGINS=`
    raises at start-up instead of meaning "no origins".
    """

    host: str = "127.0.0.1"
    """Bind address. Containers set `SE_HOST=0.0.0.0`; the safe default stays loopback."""

    port: int = 8000

    forwarded_allow_ips: str = ""
    """Proxy addresses whose `X-Forwarded-*` headers are trusted. Empty = trust nobody.

    A spoofed client IP would poison rate limiting and audit records
    (ADR-0009, 10-deployment-and-operations.md), so this is opt-in per deployment.
    """

    # ------------------------------------------------------------------ identity

    bootstrap_admin_email: str = ""
    """With `bootstrap_admin_password`, creates the first admin on an instance that has
    **zero** users (07-identity-permissions-sharing.md § users). Ignored once any account
    exists, so leaving it set is not a standing back door."""

    bootstrap_admin_password: SecretStr | None = None

    session_idle_expiry_days: int = Field(default=14, gt=0)
    """Rolling idle lifetime of a web session: use extends it, absence lapses it."""

    session_cookie_secure: bool = True
    """Only ever false for local development over plain HTTP. The cookie name follows:
    the `__Host-` prefix is invalid without `Secure`, so a browser would reject it."""

    login_max_attempts: int = Field(default=10, gt=0)
    """Failed logins per identity and per client address inside the lockout window before
    further attempts are refused (07 § abuse protection)."""

    login_lockout_minutes: int = Field(default=15, gt=0)

    rate_limit_per_minute: int = Field(default=300, gt=0)
    """Per-credential (or per-address, unauthenticated) request ceiling for `/api/v1`.
    Volumetric abuse is the edge's job; this is the app-level backstop."""

    # ------------------------------------------------------------------ operations
    # 12-reliability.md § tuning defaults (Q30). Conservative on purpose, and revisited at
    # phase-2 entry against real extractor runtimes — a re-tune is configuration, not design.

    lease_seconds: int = Field(default=300, gt=0)
    """How long a claim owns its operation before anyone may reclaim it."""

    heartbeat_seconds: int = Field(default=60, gt=0)
    """Renewal cadence — five chances inside one lease, so a slow cycle is survivable."""

    max_attempts: int = Field(default=4, gt=0)
    """Attempts before dead-lettering. Counted on claim, so a worker-killing job converges."""

    retry_base_seconds: float = Field(default=10.0, gt=0)
    retry_max_seconds: float = Field(default=3600.0, gt=0)
    """Exponential backoff bounds; the delay itself is jittered."""

    worker_concurrency: int = Field(default=4, gt=0)
    """Operations one worker process runs at once."""

    worker_poll_seconds: float = Field(default=5.0, gt=0)
    """How long an idle worker waits before looking again. Claiming writes nothing when
    there is nothing to claim, so idling here costs one indexed read."""

    # ------------------------------------------------------------------ app-owned storage
    # 03-storage-and-portability.md § storage layout. Separate from the user's own tree: this
    # is what "the app can be removed at any time" means in practice — deleting this area
    # loses history and previews, never current data.

    app_data_root: Path = Path("/var/lib/store-everything")
    """Root of everything the app owns: `versions/` and the derived store."""

    janitor_grace_hours: int = Field(default=24, gt=0)
    """How long debris is left alone before collection. The window exists so the janitor
    cannot race an in-flight operation between its bytes-write and its row-commit."""

    janitor_interval_minutes: int = Field(default=60, gt=0)

    # ------------------------------------------------------------------ workspace storage
    # 03-storage-and-portability.md § storage layout. The user's own files in their own
    # hierarchy — the data the portability promise is about, and deliberately *not* under
    # `app_data_root`: deleting the app-owned area costs history and previews, while this is
    # the data itself.

    data_root: Path = Path("/srv/store-everything")
    """Where `managed` workspace roots are created (ADR-0018): one directory per workspace,
    named after it, under a directory named after its owner."""

    workspace_scan_interval_minutes: int = Field(default=60, gt=0)
    """The scan cadence a new workspace starts with (12 § tuning defaults). ADR-0019's
    correctness backstop for external changes: the watcher and a manual rescan are the fast
    paths, and this is the one that cannot be missed. Stored per workspace, so changing it
    here affects workspaces created afterwards."""

    adoption_roots: Annotated[tuple[Path, ...], NoDecode] = ()
    """The complete set of locations a workspace may be **adopted** from — an existing tree
    indexed in place (ADR-0018). Empty by default, which disables adoption entirely.

    An allow-list rather than a trusted request field: this is what bounds the blast radius
    of a path-traversal or "which mount is this inside the container" mistake to something an
    operator chose. Adoption additionally requires an admin.
    """

    # ------------------------------------------------------------------ uploads
    # ADR-0017. The three sizes are published to clients in `Upload-Limit`, so a proxy's body
    # limit becomes a negotiated number rather than a mystery failure mid-upload.

    upload_expiry_days: int = Field(default=7, gt=0)
    """How long an interrupted upload can be resumed before its session and staged bytes are
    collected (12 § tuning defaults). Long enough to survive a weekend."""

    upload_max_size: int = Field(default=0, ge=0)
    """Largest single upload, in bytes. `0` means no app-level limit — the storage is the
    limit — and omits `max-size` from `Upload-Limit` rather than publishing a fiction."""

    upload_max_append_size: int = Field(default=64 * 1024 * 1024, gt=0)
    """Largest body one append may carry. Published so a client chunks to fit the edge."""

    upload_min_append_size: int = Field(default=1024 * 1024, gt=0)
    """Smallest body an append should carry — and the line the request ceiling uses.

    An append at least this large does not count against the per-credential ceiling: what
    that ceiling rations is per-request overhead (one `fsync` each), not throughput. The
    asymmetry is what makes this safe: a *small* append only breaches a per-minute count if
    the link is fast, and a fast link has no reason to send small appends, while an attacker
    sending kilobyte appends spends the ordinary budget and stops
    (07 § abuse protection).
    """

    @field_validator("cors_allow_origins", "adoption_roots", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        """Accept `a,b` in the environment, and an empty value as "none"."""
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("adoption_roots", mode="after")
    @classmethod
    def _adoption_roots_are_absolute(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        """A relative allow-list entry would be resolved against the process's directory,
        which is not something an operator can reason about."""
        relative = sorted(str(path) for path in value if not path.is_absolute())
        if relative:
            raise ValueError(f"SE_ADOPTION_ROOTS must be absolute paths; these are not: {relative}")
        return value

    @property
    def sqlalchemy_url(self) -> str:
        """The DSN with the driver this service actually uses (ADR-0012: psycopg 3)."""
        url = self.database_url.get_secret_value()
        scheme, separator, rest = url.partition("://")
        if not separator:
            return url
        if scheme in {"postgresql", "postgres"}:
            return f"{_ASYNC_DRIVER}://{rest}"
        return url

    @model_validator(mode="after")
    def _append_sizes_are_ordered(self) -> Settings:
        """A minimum above the maximum would publish limits no client can satisfy."""
        if self.upload_min_append_size > self.upload_max_append_size:
            raise ValueError("SE_UPLOAD_MIN_APPEND_SIZE must not exceed SE_UPLOAD_MAX_APPEND_SIZE")
        return self

    @model_validator(mode="after")
    def _heartbeat_fits_inside_the_lease(self) -> Settings:
        """A cadence longer than the lease would let an active worker lose its own claim."""
        if self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError(
                "SE_HEARTBEAT_SECONDS must be shorter than SE_LEASE_SECONDS, "
                "or a worker's lease expires while it is still working"
            )
        return self

    @property
    def trust_proxy_headers(self) -> bool:
        return bool(self.forwarded_allow_ips.strip())

    @property
    def versions_root(self) -> Path:
        """Superseded file content, content-addressed. **Not regenerable** — the bytes exist
        nowhere else, which is why this is mandatory backup scope (Q13)."""
        return self.app_data_root / "versions"

    @property
    def derived_root(self) -> Path:
        """Previews, keyframes, transcripts: regenerable by reprocessing, so losing it costs
        CPU rather than data."""
        return self.app_data_root / "derived"

    @property
    def session_cookie_name(self) -> str:
        """The `__Host-` prefix pins a cookie to one origin and forbids a `Domain`
        attribute — but it is only valid alongside `Secure`, and a browser rejects the
        prefixed name without it. Development over plain HTTP therefore gets the plain
        name; production gets the hardened one.
        """
        return "__Host-se_session" if self.session_cookie_secure else "se_session"


def load_settings() -> Settings:
    """Read settings from the environment.

    The ignore is structural: pydantic-settings populates required fields from the
    environment, which a static type checker cannot see.
    """
    return Settings()  # pyright: ignore[reportCallIssue]
