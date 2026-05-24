from __future__ import annotations

import logging
import math
import subprocess

import librosa
import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)

# Target sample rate for analysis. Low (vs 44.1k) on purpose: the timbral
# features below don't need full bandwidth, and downsampling is the cheapest
# lever on per-track DSP cost. Must stay fixed — it's baked into every stored
# vector.
_ANALYSIS_SR = 22050

# Hard ceiling on how long the ffmpeg fallback decode may run before we give
# up on a file. Decoding a ~60s excerpt is normally well under a second; this
# only fires for a pathological/hung input so one bad file can't wedge a
# worker forever.
_FFMPEG_DECODE_TIMEOUT_S = 60


# Fixed feature layout (44 dims total):
#
# 0      : tempo
# 1-13   : mfcc_mean[0:13]
# 14-26  : mfcc_std[0:13]
# 27-38  : chroma_mean[0:12]
# 39     : spectral_centroid_mean
# 40     : spectral_centroid_std
# 41     : rms_mean
# 42     : rms_std
# 43     : zero_crossing_rate_mean
#
# IMPORTANT:
# Never change this order without rebuilding the feature index.


EXPECTED_DIMS = 44
FEATURE_VERSION = 1

# Default analysed-excerpt length (seconds). The caller (the analysis pass)
# passes the configured `sonic_analysis_excerpt_seconds`; this default keeps
# the function usable standalone and pins the historical behaviour.
DEFAULT_EXCERPT_SECONDS = 60.0


def extract_features(
    path: str, excerpt_seconds: float = DEFAULT_EXCERPT_SECONDS
) -> list[float] | None:
    """
    Extract a fixed-length timbral feature vector from an audio file.

    Args:
        path: audio file to analyse.
        excerpt_seconds: length of the centred excerpt to analyse. Shorter is
            faster but summarises timbre/tempo from less audio. Changing it
            changes the resulting vector, so the library must be re-analysed
            (force=True) to stay internally consistent.

    Returns:
        list[float] of length 44 on success
        None on failure

    Contract:
        - Never raises
        - Always returns the same dimensionality
        - All outputs are finite Python floats
    """
    try:
        # ------------------------------------------------------------
        # Step 1: load a representative excerpt from the middle
        # ------------------------------------------------------------
        y, sr = _load_excerpt(path, excerpt_seconds)

        # ------------------------------------------------------------
        # Step 2: degenerate guard
        # ------------------------------------------------------------
        if y.size == 0:
            return None

        # ------------------------------------------------------------
        # Step 3: feature extraction + aggregation
        # ------------------------------------------------------------

        # Tempo
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        tempo = float(np.asarray(tempo).squeeze())

        # MFCCs: (13, frames)
        mfcc = librosa.feature.mfcc(
            y=y,
            sr=sr,
            n_mfcc=13,
        )

        mfcc_mean = np.mean(mfcc, axis=1)  # (13,)
        mfcc_std = np.std(mfcc, axis=1)  # (13,)

        # Magnitude STFT, computed once and shared by the two features below
        # that would otherwise each recompute it. chroma_stft and
        # spectral_centroid both call librosa's internal _spectrogram with
        # the same defaults (n_fft=2048, hop_length=512); handing them a
        # precomputed `S=` reproduces those values bit-for-bit (verified:
        # zero diff vs computing each from `y`). So this is purely removing a
        # redundant FFT pass — the feature vector is unchanged and no
        # FEATURE_VERSION bump is needed.
        stft_mag = np.abs(librosa.stft(y))

        # Chroma: (12, frames). chroma_stft expects a *power* spectrogram.
        chroma = librosa.feature.chroma_stft(S=stft_mag**2, sr=sr)
        chroma_mean = np.mean(chroma, axis=1)  # (12,)

        # Spectral centroid: (1, frames). Uses the *magnitude* spectrogram.
        centroid = librosa.feature.spectral_centroid(S=stft_mag, sr=sr)
        centroid_mean = float(np.mean(centroid))
        centroid_std = float(np.std(centroid))

        # RMS energy: (1, frames)
        rms = librosa.feature.rms(y=y)
        rms_mean = float(np.mean(rms))
        rms_std = float(np.std(rms))

        # Zero-crossing rate: (1, frames)
        zcr = librosa.feature.zero_crossing_rate(y=y)
        zcr_mean = float(np.mean(zcr))

        # ------------------------------------------------------------
        # Step 4: assemble in FIXED order
        # ------------------------------------------------------------
        features: list[float] = []

        features.append(tempo)

        features.extend(mfcc_mean.tolist())
        features.extend(mfcc_std.tolist())

        features.extend(chroma_mean.tolist())

        features.append(centroid_mean)
        features.append(centroid_std)

        features.append(rms_mean)
        features.append(rms_std)

        features.append(zcr_mean)

        # Structural invariant check:
        # feature dimensionality must NEVER drift.
        if len(features) != EXPECTED_DIMS:
            logger.error(
                "feature dimension mismatch for %s: expected %d, got %d",
                path,
                EXPECTED_DIMS,
                len(features),
            )
            return None

        # ------------------------------------------------------------
        # Step 5: finite-value scrub + Python float conversion
        # ------------------------------------------------------------
        cleaned: list[float] = []

        for v in features:
            v = float(v)

            if not math.isfinite(v):
                v = 0.0

            cleaned.append(v)

        return cleaned

    except Exception as e:
        # WARNING (not DEBUG) with the path + concise reason: when the analysis
        # pass reports "failed=N", this is the only way to learn *which* files
        # and why without re-running at DEBUG. Full traceback stays at DEBUG.
        logger.warning("feature extraction failed for %s: %s", path, e)
        logger.debug("feature extraction traceback for %s", path, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Excerpt loading
#
# Two decode paths, chosen per file:
#
#   * Fast path — libsndfile (via librosa.load). Handles the formats libsndfile
#     supports: FLAC (the bulk of a typical library), WAV, OGG, MP3. We probe
#     with soundfile.info first purely to *decide* the path; the actual decode
#     stays librosa.load with the original parameters, so the resulting vectors
#     are byte-identical to everything already in the index.
#
#   * Fallback — ffmpeg. libsndfile can't read AAC/M4A/WMA (and rejects FLACs
#     with prepended ID3 tags). librosa's own fallback for those is `audioread`,
#     which is slow, deprecated (removed in librosa 1.0), and on Python 3.13
#     drags in the removed `aifc`/`sunau` stdlib modules. We already ship
#     ffmpeg for transcoding, so we decode the excerpt with it directly:
#     faster, supported, and it handles essentially anything.
# ---------------------------------------------------------------------------


def _load_excerpt(path: str, excerpt_seconds: float) -> tuple["np.ndarray", int]:
    """Load a centred, mono, `_ANALYSIS_SR` excerpt as (samples, sr).

    Routes to libsndfile when it can read the file and to ffmpeg otherwise.
    May raise — the caller wraps this in extract_features' try/except.
    """
    try:
        info = sf.info(path)  # cheap header read; raises if libsndfile can't read it
    except Exception:
        return _ffmpeg_load_excerpt(path, excerpt_seconds)

    # Center the window on the middle of the track. For tracks shorter than the
    # window, offset clamps to 0 and librosa just returns the whole file.
    # info.duration equals the old librosa.get_duration value exactly, so the
    # offset — and therefore the feature vector — is unchanged.
    offset = max(0.0, (info.duration / 2.0) - (excerpt_seconds / 2.0))
    return librosa.load(
        path,
        sr=_ANALYSIS_SR,
        mono=True,
        offset=offset,
        duration=excerpt_seconds,
    )


def _ffmpeg_load_excerpt(path: str, excerpt_seconds: float) -> tuple["np.ndarray", int]:
    """Decode a centred mono excerpt at `_ANALYSIS_SR` via ffmpeg.

    Used for formats libsndfile can't handle. ffmpeg does the seek, downmix and
    resample itself and streams raw float32 PCM to stdout, which we read
    straight into a numpy array.
    """
    from backend.config import get_settings

    settings = get_settings()

    # Center the window using a cheap tag-level duration read (mutagen reads
    # m4a/aac/wma lengths without decoding). If unavailable, decode from start.
    offset = 0.0
    try:
        from mutagen import File as _MutagenFile

        mf = _MutagenFile(path)
        dur = float(getattr(getattr(mf, "info", None), "length", 0.0) or 0.0)
        if dur > 0.0:
            offset = max(0.0, (dur / 2.0) - (excerpt_seconds / 2.0))
    except Exception:
        pass  # best-effort centring; offset=0 is a fine fallback

    cmd = [
        settings.ffmpeg_binary,
        "-nostdin",
        "-v", "error",
        "-ss", f"{offset:.3f}",          # seek before -i for a fast input seek
        "-t", f"{excerpt_seconds:.3f}",
        "-i", path,
        "-map", "0:a:0",                  # first audio stream only (ignores cover art)
        "-ac", "1",                       # downmix to mono
        "-ar", str(_ANALYSIS_SR),         # resample to analysis rate
        "-f", "f32le",                    # raw little-endian float32 PCM
        "-",
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=_FFMPEG_DECODE_TIMEOUT_S,
    )
    if proc.returncode != 0:
        # Surface ffmpeg's own diagnostic so a decode failure is actionable.
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"ffmpeg decode failed: {msg[-1] if msg else 'unknown error'}")

    # frombuffer aliases ffmpeg's read-only output buffer; copy so downstream
    # librosa ops that expect a writable array are safe.
    y = np.frombuffer(proc.stdout, dtype=np.float32).copy()
    return y, _ANALYSIS_SR


# ---------------------------------------------------------------------------
# Similarity math (pure — operates on already-loaded vectors, no DB/librosa).
#
# Callers load every track's vector via queries.get_all_track_features() and
# pass the resulting list[(track_id, vector)] in. Keeping these functions pure
# makes them unit-testable against synthetic matrices with no audio files.
#
# Standardisation is the load-bearing step: raw feature dimensions span wildly
# different scales (tempo ~120, MFCC[0] ~±400, rms ~0.05). Without per-dimension
# z-scoring, a couple of large-magnitude dims dominate every distance and the
# rest become noise. We standardise the whole matrix per request — trivial cost
# for a home library, and it means a changing library never needs a stored
# scaler.
# ---------------------------------------------------------------------------


def _build_matrix(
    all_features: list[tuple[int, list[float]]],
) -> tuple[list[int], "np.ndarray"]:
    ids = [tid for tid, _ in all_features]
    mat = np.asarray([vec for _, vec in all_features], dtype=float)
    return ids, mat


def _standardize(mat: "np.ndarray") -> "np.ndarray":
    """Per-dimension z-score. Dimensions with zero variance (constant across
    the library) are left centred at 0 rather than dividing by zero."""
    mu = mat.mean(axis=0)
    sigma = mat.std(axis=0)
    sigma = np.where(sigma == 0.0, 1.0, sigma)
    return (mat - mu) / sigma


def _cosine(a: "np.ndarray", b: "np.ndarray") -> float:
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _score(cos: float) -> float:
    """Map cosine [-1, 1] → similarity [0, 1] (spec: 1.0 identical, 0.0 max
    dissimilar)."""
    return (cos + 1.0) / 2.0


def find_similar(
    all_features: list[tuple[int, list[float]]],
    query_id: int,
    count: int,
) -> list[tuple[int, float]]:
    """Top-`count` tracks most sonically similar to `query_id`, most-similar
    first, excluding the query track itself. Returns [(track_id, score)].
    Empty list if the query track has no feature vector."""
    if count <= 0 or not all_features:
        return []
    ids, mat = _build_matrix(all_features)
    if query_id not in ids:
        return []
    std = _standardize(mat)
    qi = ids.index(query_id)
    qn = np.linalg.norm(std, axis=1)
    qn = np.where(qn == 0.0, 1.0, qn)
    query = std[qi]
    query_norm = np.linalg.norm(query) or 1.0
    cos = (std @ query) / (qn * query_norm)
    order = np.argsort(cos)[::-1]
    out: list[tuple[int, float]] = []
    for idx in order:
        if ids[idx] == query_id:
            continue
        out.append((ids[idx], _score(float(cos[idx]))))
        if len(out) >= count:
            break
    return out


def find_path(
    all_features: list[tuple[int, list[float]]],
    start_id: int,
    end_id: int,
    count: int,
) -> list[tuple[int, float]]:
    """An ordered path of up to `count` tracks from `start_id` to `end_id`
    through feature space. We walk evenly-spaced points on the straight line
    between the two standardised vectors and snap each to the nearest not-yet-
    used track; endpoints are pinned. Each entry's score is its similarity to
    the start track (so the path begins at 1.0). Returns [(track_id, score)].

    Shorter than `count` if the library runs out of distinct tracks; empty if
    either endpoint lacks a feature vector."""
    count = max(count, 2)
    ids, mat = _build_matrix(all_features)
    if start_id not in ids or end_id not in ids:
        return []
    std = _standardize(mat)
    si = ids.index(start_id)
    ei = ids.index(end_id)
    start_vec = std[si]

    def scored(idx: int) -> tuple[int, float]:
        return ids[idx], _score(_cosine(std[idx], start_vec))

    if start_id == end_id:
        return [scored(si)]

    end_vec = std[ei]
    used = {si, ei}
    middle: list[int] = []
    for step in range(1, count - 1):
        t = step / (count - 1)
        target = (1.0 - t) * start_vec + t * end_vec
        dists = np.linalg.norm(std - target, axis=1)
        for u in used:
            dists[u] = np.inf
        if not np.isfinite(dists.min()):
            break  # ran out of unused tracks
        chosen = int(np.argmin(dists))
        used.add(chosen)
        middle.append(chosen)

    return [scored(i) for i in [si, *middle, ei]]
