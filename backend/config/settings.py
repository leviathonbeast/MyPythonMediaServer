"""
Configuration management for the Muse music server.

WHY a dedicated config module:
    Every other module imports from here. Centralising configuration means we
    can swap the source (env vars, YAML, DB) without touching call sites, and
    we have a single, type-checked surface for "what are the knobs of this
    system". Pydantic Settings gives us validation, defaults, and env-var
    parsing for free.

Resolution order (highest priority wins):
    1. Environment variables (prefixed MUSE_)
    2. config.yaml in the working directory (if present)
    3. Defaults defined on the Settings class

Env var examples:
    MUSE_MUSIC_FOLDERS='["/mnt/music", "/mnt/nas/audio"]'
    MUSE_DATABASE_PATH=/var/lib/muse/library.db
    MUSE_JWT_SECRET=change-me-in-production
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Central settings object. Instantiate once via `get_settings()` (cached).

    Every field below documents WHY it exists, not just what it does, so future
    contributors can decide whether to keep, change, or remove it.
    """

    # ---- Paths -------------------------------------------------------------
    # Music folders are roots scanned recursively. Multiple roots = multiple
    # "music folders" in the Subsonic sense (getMusicFolders returns them).
    music_folders: List[str] = Field(
        default_factory=lambda: ["./music"],
        description="Absolute paths to music libraries. Local or NAS mount points.",
    )

    # Database backend.
    #
    # database_url is the authoritative knob and supports two URL schemes:
    #   sqlite:///./data/library.db    (default; relative path supported)
    #   sqlite:////absolute/path.db    (note the four slashes for absolute)
    #   postgresql://user:pass@host:port/dbname
    #
    # database_path is the LEGACY field and still works — if database_url is
    # unset and database_path is provided, the loader synthesises a sqlite://
    # URL from it. This means existing deployments don't need to touch their
    # config when upgrading.
    database_url: Optional[str] = Field(
        default=None,
        description=(
            "Database URL. sqlite:///path/to/file.db or "
            "postgresql://user:pass@host:port/dbname. Falls back to "
            "database_path when unset."
        ),
    )
    database_path: str = Field(
        default="./data/library.db",
        description=(
            "Legacy SQLite file path. Ignored when database_url is set. "
            "Parent dir is created at startup."
        ),
    )

    # Cover art cache directory. Extracted artwork is written here as JPEG/PNG
    # so we never re-extract from the audio file on every request.
    artwork_cache_dir: str = Field(
        default="./data/artwork",
        description="Where extracted/cached cover art is stored on disk.",
    )

    # FFmpeg binary. We resolve via PATH by default; override for static builds
    # or non-standard locations (e.g. NAS containers).
    ffmpeg_binary: str = Field(
        default="ffmpeg",
        description="Path to ffmpeg. Must be in PATH or absolute.",
    )
    ffprobe_binary: str = Field(
        default="ffprobe",
        description="Path to ffprobe. Used as a metadata fallback.",
    )

    # ---- Server ------------------------------------------------------------
    host: str = Field(default="0.0.0.0", description="Bind host.")
    port: int = Field(
        default=4040, description="Bind port. Subsonic uses 4040 by tradition."
    )

    # CORS origins for the frontend dev server. Vite defaults to 5173.
    cors_origins: List[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"],
        description="Origins allowed to call the API from a browser.",
    )

    # ---- Auth --------------------------------------------------------------
    # JWT secret. MUST be overridden in production. We don't ship a real default
    # because shipping a secret is a footgun.

    auth_rate_limits: str = Field(
        default="5/minute", description="Login attempts over 60 seconds before limits"
    )

    jwt_secret: str = Field(
        default="muse-dev-secret-change-me",
        description="HMAC secret for JWT signing. Override in production.",
    )
    jwt_algorithm: str = Field(default="HS256")
    jwt_expiry_hours: int = Field(
        default=24,
        description=(
            "Token lifetime in hours. Shorter limits the blast radius of a "
            "stolen JWT and the window during which a disabled account can "
            "still use the web UI. Override in config.yaml if needed."
        ),
    )

    # Off by default — /docs lists every endpoint and is useful to attackers
    # mapping the surface. Flip to true in development if you want Swagger UI.
    expose_docs: bool = Field(
        default=False,
        description="Serve FastAPI's /docs, /redoc, /openapi.json publicly.",
    )

    # Initial admin credentials. These are used to bootstrap the user table on
    # first run. They are NOT re-applied on subsequent runs (so changing them
    # later won't reset the admin password — that's intentional).
    #
    # `admin_password` has no default on purpose: shipping a known default
    # ("admin") combined with the default 0.0.0.0 bind is how home servers
    # end up trivially owned. When unset on first run the migration refuses
    # to seed the admin user and the operator must set MUSE_ADMIN_PASSWORD
    # (or admin_password in config.yaml) before starting.
    admin_username: str = Field(default="admin")
    admin_password: Optional[str] = Field(default=None)

    # ---- Scanner -----------------------------------------------------------
    # Extensions we even bother to look at. Anything else is skipped at the
    # walker level for speed (no stat, no hash, no tag read).
    audio_extensions: List[str] = Field(
        default_factory=lambda: [
            ".mp3",
            ".flac",
            ".ogg",
            ".opus",
            ".m4a",
            ".wav",
            ".aac",
            ".wma",
        ],
    )

    # When True, scan on startup. Useful in dev. In production you'd typically
    # rely on the manual trigger or a cron-scheduled rescan.
    scan_on_startup: bool = Field(default=False)

    # Workers for parallel metadata extraction. Tag reading is I/O-heavy so
    # threads work well here even with the GIL.
    scanner_workers: int = Field(default=4)

    # Batch size for DB writes during scanning. Bigger = faster but more memory.
    scanner_batch_size: int = Field(default=200)

    # Periodic-rescan watcher. We use polling (not inotify) because the typical
    # deployment has music on an NFS/SMB share modified from another machine —
    # the Linux kernel doesn't fire inotify events for remote writes, so a
    # filesystem-event watcher would silently miss everything. A periodic full
    # scan is cheap thanks to the mtime+size short-circuit in phase 1: a
    # no-change pass over 50k files takes only a few seconds.
    scanner_watch_enabled: bool = Field(
        default=False,
        description=(
            "If true, a background thread triggers a library scan every "
            "`scanner_watch_interval_seconds`. Safe to leave off and rely on "
            "manual scans."
        ),
    )
    scanner_watch_interval_seconds: int = Field(
        default=300,
        description=(
            "Seconds between automatic rescans when scanner_watch_enabled is "
            "true. Floor of 30s is enforced at runtime to avoid pathological "
            "configs hammering the scanner."
        ),
    )

    # ---- Streaming ---------------------------------------------------------
    # Default chunk size for HTTP range responses. 64KB is a reasonable sweet
    # spot — small enough that seeking feels instant, large enough that we
    # don't drown in syscalls on long playback.
    stream_chunk_size: int = Field(default=64 * 1024)

    # Default transcode bitrate for clients that ask for "mp3" without specifying.
    default_transcode_bitrate: int = Field(default=192)

    # Default streaming format when the client doesn't ask for a specific one.
    # Use "raw" to stream files in their original format by default — the
    # right choice if you mostly listen on the same network as your library.
    # Set to "mp3" / "opus" / "ogg" to default to a transcode for everyone.
    default_transcode_format: str = Field(default="raw")

    # Hard cap on the bitrate the server will ever serve. If a client asks
    # for higher (or asks for raw on a high-bitrate FLAC), we clamp down
    # to this and transcode. None = no cap. Useful when streaming over a
    # constrained uplink to a phone.
    max_streaming_bitrate: Optional[int] = Field(default=None)

    # Master kill-switch. When False, every stream request returns the
    # original file regardless of what the client asked for. Handy in
    # local-network installs where ffmpeg-induced CPU isn't worth it.
    transcoding_enabled: bool = Field(default=True)

    # ---- External integrations --------------------------------------------
    # Optional Last.fm API key. When present, the artist page enriches
    # itself with a short bio fetched from Last.fm's artist.getInfo.
    # Without it the artist page still works; it just shows no bio.
    # Get a key (free, instant) at https://www.last.fm/api/account/create
    lastfm_api_key: Optional[str] = Field(default=None)

    # ---- Misc --------------------------------------------------------------
    # Subsonic API version we claim to implement. Real Subsonic is at 1.16.1
    # at time of writing; we report what we actually support.
    subsonic_api_version: str = Field(default="1.16.1")
    server_name: str = Field(default="Muse")
    server_version: str = Field(default="0.1.0")

    # Logging verbosity for the root "muse" logger and its children.
    # Set to DEBUG when troubleshooting scanner / streaming issues —
    # the scanner walker emits one debug line per directory visited,
    # which is exactly what you want when diagnosing "why did my scan
    # find nothing on virtiofs / NFS / SMB".
    log_level: str = Field(default="INFO")

    # Pydantic Settings config — env_prefix means MUSE_PORT sets `port`, etc.
    model_config = SettingsConfigDict(
        env_prefix="MUSE_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("music_folders")
    @classmethod
    def _expand_music_folders(cls, v: List[str]) -> List[str]:
        """Expand ~ and resolve to absolute paths so downstream code never has to."""
        return [str(Path(p).expanduser().resolve()) for p in v]

    def resolved_database_url(self) -> str:
        """Return the effective database URL.

        Prefers `database_url` when set; falls back to `database_path` and
        synthesises a `sqlite://` URL from it. Lets old configs that only
        set `database_path` keep working without modification.
        """
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.database_path}"

    @field_validator("audio_extensions")
    @classmethod
    def _normalize_extensions(cls, v: List[str]) -> List[str]:
        """
        Normalize audio extensions to a canonical form: lowercase, with a
        leading dot. Users naturally write either `mp3` or `.mp3` in YAML;
        the walker compares against the form returned by `os.path.splitext`
        (which always has a dot), so we coerce to that here. Without this,
        config like `audio_extensions: [mp3, flac]` silently scans nothing.
        """
        out: List[str] = []
        for ext in v:
            e = ext.strip().lower()
            if not e:
                continue
            if not e.startswith("."):
                e = "." + e
            out.append(e)
        return out


_settings_instance: Optional[Settings] = None


def get_settings() -> Settings:
    """
    Return the singleton Settings instance.

    WHY a function (not a module-level constant):
        Tests can monkeypatch this. Also, building Settings reads files and env
        vars — doing that lazily means importing this module is cheap.
    """
    global _settings_instance
    if _settings_instance is not None:
        return _settings_instance

    # Try loading config.yaml first; env vars will still override per Pydantic.
    yaml_data: dict = {}
    yaml_path = Path("config.yaml")
    if yaml_path.exists():
        with yaml_path.open("r", encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f) or {}

    _settings_instance = Settings(**yaml_data)
    return _settings_instance


def ensure_directories(settings: Settings) -> None:
    """
    Create directories the server needs at runtime.

    Called once at startup. Idempotent. Failing here means the user gave us a
    path we can't write to — fail loudly rather than later mid-request.

    For Postgres URLs there's no filesystem path to create — that's the DB
    server's problem. We only create the parent dir for `sqlite://` URLs.
    """
    url = settings.resolved_database_url()
    if url.startswith("sqlite://"):
        # Strip the scheme. SQLAlchemy-style sqlite:///./relative or
        # sqlite:////absolute both work — slice off the 'sqlite://' and
        # the remaining string is the path (possibly with a leading slash
        # for absolute paths).
        path = url[len("sqlite://"):]
        if path.startswith("/") and not path.startswith("//"):
            # sqlite:///./rel.db → "/./rel.db" → strip leading slash for relative
            path = path[1:]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.artwork_cache_dir).mkdir(parents=True, exist_ok=True)
