"""
Transcode-decision logic for the OpenSubsonic `transcoding` extension.

Background
----------
`getTranscodeDecision` lets a client ask, *before* it opens a stream,
"given what I (the client) can play, will you serve this file as-is or
transcode it, and to what?". The client describes its own capabilities in
a JSON `ClientInfo` body:

    * directPlayProfiles   — container/codec/protocol/channel combos the
                             client can play untouched.
    * transcodingProfiles  — the targets the client is willing to receive a
                             transcode in, listed in priority order.
    * codecProfiles        — per-codec numeric limits (max bitrate, max
                             samplerate, allowed channel counts, …).
    * maxAudioBitrate /
      maxTranscodingAudioBitrate — global ceilings (bits per second).

We compare the *source track* against those declarations and return a
`transcodeDecision` object:

    canDirectPlay   — does at least one directPlayProfile (plus the global
                      bitrate ceiling and matching codecProfiles) permit the
                      file untouched?
    canTranscode    — can THIS server transcode at all? (purely our own
                      capability — `transcoding_enabled`).
    transcodeReason — one human/string reason per directPlayProfile that
                      rejected the file, for client-side logging. Empty when
                      we can direct-play.
    sourceStream    — what the source actually is.
    transcodeStream — what we'd transcode to, when we transcode.
    transcodeParams — an opaque, URL-escaped query fragment the client can
                      append to /rest/stream to request exactly this target.
    errorReason     — set only when we can neither direct-play nor transcode.

Honesty about our metadata
--------------------------
The scanner stores container (suffix), bitrate and duration — but NOT
channel count, sample rate or bit depth. Any profile/codecProfile rule that
depends on a value we don't have is treated as "can't evaluate → pass"
rather than guessed. As a result:

    * directPlayProfile.maxAudioChannels is only enforced if we ever learn
      the source channel count; today it's skipped.
    * codecProfile limitations on audioSamplerate / audioChannels /
      audioBitdepth are skipped; only audioBitrate is actually checked.

The matching code already handles those fields, so the day the scanner
populates them the decision sharpens automatically — no logic change.

This module is intentionally pure (no FastAPI, no DB). The endpoint in
backend/api/subsonic/streaming.py adapts a track row + request body into the
inputs here and serialises the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from .presets import MAX_TRANSCODE_BITRATE, PRESETS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Codecs this server can actually emit. Derived from the preset table so the
# two never drift: PRESETS keys are (format, bitrate) and the format half is
# exactly the set of encoders we ship ffmpeg args for.
SUPPORTED_TRANSCODE_CODECS = {fmt for (fmt, _br) in PRESETS}  # {"mp3","aac","opus"}

# Our streamer only speaks plain HTTP — no HLS. A transcodingProfile that
# demands "hls" can't be satisfied, so we only consider these protocols.
_OK_PROTOCOLS = {"", "http"}

# Reason strings mirror the spec's example style ("AudioCodecNotSupported").
# One of these is attached to every directPlayProfile we reject.
_R_CONTAINER = "ContainerNotSupported"
_R_CODEC = "AudioCodecNotSupported"
_R_PROTOCOL = "ProtocolNotSupported"
_R_CHANNELS = "AudioChannelsNotSupported"
_R_BITRATE = "AudioBitrateNotSupported"
_R_SAMPLERATE = "AudioSamplerateNotSupported"
_R_BITDEPTH = "AudioBitdepthNotSupported"

# Map a codecProfile limitation name → the reason we emit when it fails.
_LIMITATION_REASON = {
    "audioBitrate": _R_BITRATE,
    "audioSamplerate": _R_SAMPLERATE,
    "audioChannels": _R_CHANNELS,
    "audioBitdepth": _R_BITDEPTH,
}

# File-suffix → codec name. The DB only stores the container (suffix); the
# spec's profiles match on codec, so we derive a best-effort codec. When a
# suffix isn't listed we fall back to using the suffix as the codec name,
# which is correct for the common single-codec containers (mp3, flac, opus).
_SUFFIX_TO_CODEC = {
    "mp3": "mp3",
    "flac": "flac",
    "ogg": "vorbis",
    "oga": "vorbis",
    "opus": "opus",
    "m4a": "aac",
    "m4b": "aac",
    "mp4": "aac",
    "aac": "aac",
    "wav": "pcm",
    "aif": "pcm",
    "aiff": "pcm",
    "wma": "wma",
    "ape": "ape",
    "wv": "wavpack",
    "alac": "alac",
}


def codec_for_suffix(suffix: str) -> str:
    """Best-effort codec name for a file suffix (see _SUFFIX_TO_CODEC)."""
    s = (suffix or "").lower().lstrip(".")
    return _SUFFIX_TO_CODEC.get(s, s)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass
class StreamDetails:
    """One side of the decision (source or target).

    Only `protocol`/`container`/`codec` are always present; the numeric
    audio fields are Optional and omitted from the serialised form when
    unknown, so we never lie with a fabricated 0. Bitrate is bits/second
    and samplerate is Hz, matching the OpenSubsonic StreamDetails spec.
    """

    protocol: str
    container: str
    codec: str
    audio_bitrate: Optional[int] = None      # bits/second
    audio_channels: Optional[int] = None
    audio_samplerate: Optional[int] = None   # Hz
    audio_bitdepth: Optional[int] = None
    audio_profile: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "protocol": self.protocol,
            "container": self.container,
            "codec": self.codec,
        }
        if self.audio_channels is not None:
            out["audioChannels"] = self.audio_channels
        if self.audio_bitrate is not None:
            out["audioBitrate"] = self.audio_bitrate
        if self.audio_profile is not None:
            out["audioProfile"] = self.audio_profile
        if self.audio_samplerate is not None:
            out["audioSamplerate"] = self.audio_samplerate
        if self.audio_bitdepth is not None:
            out["audioBitdepth"] = self.audio_bitdepth
        return out


@dataclass
class TranscodeDecision:
    """The full `transcodeDecision` payload, pre-serialisation."""

    can_direct_play: bool
    can_transcode: bool
    source_stream: StreamDetails
    transcode_stream: Optional[StreamDetails] = None
    transcode_reason: List[str] = field(default_factory=list)
    transcode_params: str = ""
    error_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "canDirectPlay": self.can_direct_play,
            "canTranscode": self.can_transcode,
            # Always present (spec marks them optional, but emitting empties
            # keeps the shape stable for clients that index without guarding).
            "transcodeReason": self.transcode_reason,
            "errorReason": self.error_reason,
            "transcodeParams": self.transcode_params,
            "sourceStream": self.source_stream.to_dict(),
        }
        if self.transcode_stream is not None:
            out["transcodeStream"] = self.transcode_stream.to_dict()
        return out


# ---------------------------------------------------------------------------
# Numeric comparison for codecProfile limitations
# ---------------------------------------------------------------------------


def _satisfies(actual: int, comparison: str, values: List[str]) -> bool:
    """Evaluate one codecProfile limitation.

    `values` arrives as strings (per spec); we coerce to int and ignore any
    that don't parse. An empty/garbage value list is treated as "no
    constraint" (returns True) so a malformed client payload never forces a
    needless transcode.
    """
    nums: List[int] = []
    for v in values:
        try:
            nums.append(int(v))
        except (TypeError, ValueError):
            continue
    if not nums:
        return True

    cmp = comparison or "LessThanEqual"
    if cmp == "LessThanEqual":
        return actual <= max(nums)
    if cmp == "LessThan":
        return actual < max(nums)
    if cmp == "GreaterThanEqual":
        return actual >= min(nums)
    if cmp == "GreaterThan":
        return actual > min(nums)
    if cmp in ("Equals", "EqualsAny"):
        return actual in nums
    if cmp in ("NotEquals", "NotEqualsAny"):
        return actual not in nums
    # Unknown operator → don't penalise the client for our ignorance.
    return True


def _check_codec_profiles(
    codec: str,
    codec_profiles: List[Dict[str, Any]],
    *,
    bitrate_bps: Optional[int],
    samplerate: Optional[int],
    channels: Optional[int],
    bitdepth: Optional[int],
) -> Optional[str]:
    """Return the first failing-limitation reason for `codec`, or None.

    Limitations whose source value we don't have (e.g. samplerate) are
    skipped rather than failed — see the module docstring on honesty.
    """
    actual_by_name = {
        "audioBitrate": bitrate_bps,
        "audioSamplerate": samplerate,
        "audioChannels": channels,
        "audioBitdepth": bitdepth,
    }
    for cp in codec_profiles:
        if (cp.get("type") or "AudioCodec") != "AudioCodec":
            continue
        if (cp.get("name") or "").lower() != codec.lower():
            continue
        for lim in cp.get("limitations") or []:
            name = lim.get("name")
            actual = actual_by_name.get(name)
            if actual is None:
                continue  # can't evaluate without source metadata
            if not _satisfies(actual, lim.get("comparison"), lim.get("values") or []):
                return _LIMITATION_REASON.get(name, _R_CODEC)
    return None


# ---------------------------------------------------------------------------
# Direct-play matching
# ---------------------------------------------------------------------------


def _direct_play_reason(
    profile: Dict[str, Any],
    *,
    container: str,
    codec: str,
    channels: Optional[int],
) -> Optional[str]:
    """Why this single directPlayProfile rejects the source, or None if it
    accepts it.

    An empty list for containers/audioCodecs/protocols is read as "any"
    (the spec's own example leaves protocols empty to mean unrestricted).
    The channel ceiling is only enforced when we actually know the source
    channel count.
    """
    containers = [c.lower() for c in (profile.get("containers") or [])]
    if containers and container.lower() not in containers:
        return _R_CONTAINER

    codecs = [c.lower() for c in (profile.get("audioCodecs") or [])]
    if codecs and codec.lower() not in codecs:
        return _R_CODEC

    protocols = [p.lower() for p in (profile.get("protocols") or [])]
    if protocols and "http" not in protocols:
        return _R_PROTOCOL

    max_ch = profile.get("maxAudioChannels")
    if max_ch and channels is not None and channels > int(max_ch):
        return _R_CHANNELS

    return None


# ---------------------------------------------------------------------------
# Transcode-target selection
# ---------------------------------------------------------------------------


def _pick_transcode_target(
    transcoding_profiles: List[Dict[str, Any]],
    *,
    default_format: str,
) -> Optional[Dict[str, str]]:
    """Choose a transcode target the client accepts and we can produce.

    transcodingProfiles are in client priority order, so we take the first
    one whose codec we have a preset for and whose protocol we can serve
    (http / unspecified). If the client offered none we can satisfy — or
    offered none at all — we fall back to the server's default transcode
    format, provided that's a codec we support.

    Returns a {container, codec} dict, or None when no acceptable target
    exists (e.g. client only accepts an hls/exotic codec we can't emit).
    """
    for prof in transcoding_profiles:
        codec = (prof.get("audioCodec") or "").lower()
        protocol = (prof.get("protocol") or "").lower()
        if codec in SUPPORTED_TRANSCODE_CODECS and protocol in _OK_PROTOCOLS:
            container = (prof.get("container") or codec).lower()
            return {"container": container, "codec": codec}

    fmt = (default_format or "").lower()
    if fmt in SUPPORTED_TRANSCODE_CODECS:
        return {"container": fmt, "codec": fmt}
    return None


# ---------------------------------------------------------------------------
# Top-level decision
# ---------------------------------------------------------------------------


def decide(
    *,
    source_container: str,
    source_codec: str,
    source_bitrate_bps: Optional[int],
    source_channels: Optional[int] = None,
    source_samplerate: Optional[int] = None,
    source_bitdepth: Optional[int] = None,
    client: Dict[str, Any],
    transcoding_enabled: bool,
    default_transcode_format: str,
) -> TranscodeDecision:
    """Compute the transcode decision for one track + one ClientInfo body.

    `client` is the parsed JSON body (already a dict); missing keys default
    to "no constraint". All bitrate inputs/outputs are bits/second.
    """
    source_stream = StreamDetails(
        protocol="http",
        container=source_container.lower(),
        codec=source_codec.lower(),
        audio_bitrate=source_bitrate_bps,
        audio_channels=source_channels,
        audio_samplerate=source_samplerate,
        audio_bitdepth=source_bitdepth,
    )

    direct_profiles = client.get("directPlayProfiles") or []
    codec_profiles = client.get("codecProfiles") or []
    transcode_profiles = client.get("transcodingProfiles") or []

    # Global ceiling, 0/absent means unlimited.
    max_audio_bitrate = int(client.get("maxAudioBitrate") or 0)

    # --- Direct-play evaluation -------------------------------------------
    # A profile permits direct play only if it matches AND the global bitrate
    # ceiling AND the codecProfiles for this codec are all satisfied. We
    # compute the per-profile reason so the response can carry one entry per
    # profile (spec: "one string per direct play profile, for logging").
    global_reason: Optional[str] = None
    if max_audio_bitrate and source_bitrate_bps and source_bitrate_bps > max_audio_bitrate:
        global_reason = _R_BITRATE
    if global_reason is None:
        global_reason = _check_codec_profiles(
            source_codec,
            codec_profiles,
            bitrate_bps=source_bitrate_bps,
            samplerate=source_samplerate,
            channels=source_channels,
            bitdepth=source_bitdepth,
        )

    reasons: List[str] = []
    can_direct_play = False
    for prof in direct_profiles:
        reason = _direct_play_reason(
            prof,
            container=source_container,
            codec=source_codec,
            channels=source_channels,
        )
        if reason is None:
            reason = global_reason  # profile fit, but a global rule may veto
        if reason is None:
            can_direct_play = True
            break  # one accepting profile is enough
        reasons.append(reason)

    # When direct play succeeds we expose no reasons. When the client sent no
    # directPlayProfiles at all, it has declared no direct-play capability, so
    # we must transcode and say so explicitly.
    if can_direct_play:
        reasons = []
    elif not direct_profiles:
        reasons = [_R_CONTAINER]

    # --- Transcode evaluation ---------------------------------------------
    can_transcode = bool(transcoding_enabled)
    transcode_stream: Optional[StreamDetails] = None
    transcode_params = ""

    if not can_direct_play and can_transcode:
        target = _pick_transcode_target(
            transcode_profiles, default_format=default_transcode_format
        )
        if target is None:
            # Server can transcode in general, but not into anything this
            # client will accept → effectively no usable transcode.
            can_transcode = False
        else:
            # Target bitrate (bps): never exceed our hard ceiling, the
            # client's transcoding ceiling, or the source itself (no point
            # upscaling a 128k file to 320k).
            cap_bps = MAX_TRANSCODE_BITRATE * 1000
            client_cap = int(client.get("maxTranscodingAudioBitrate") or 0)
            if client_cap:
                cap_bps = min(cap_bps, client_cap)
            if source_bitrate_bps:
                cap_bps = min(cap_bps, source_bitrate_bps)
            target_kbps = max(1, cap_bps // 1000)

            transcode_stream = StreamDetails(
                protocol="http",
                container=target["container"],
                codec=target["codec"],
                audio_bitrate=target_kbps * 1000,
            )
            # Opaque token the client appends to /rest/stream. Our stream
            # endpoint reads `format` and `maxBitRate` (kbps) query params,
            # so this fragment requests exactly the target we just chose.
            transcode_params = urlencode(
                {"format": target["codec"], "maxBitRate": target_kbps}
            )

    # --- Error reason ------------------------------------------------------
    error_reason = ""
    if not can_direct_play and not can_transcode:
        error_reason = (
            "Source cannot be direct-played by this client and the server "
            "has no acceptable transcode target available."
        )

    return TranscodeDecision(
        can_direct_play=can_direct_play,
        can_transcode=can_transcode,
        source_stream=source_stream,
        transcode_stream=transcode_stream,
        transcode_reason=reasons,
        transcode_params=transcode_params,
        error_reason=error_reason,
    )
