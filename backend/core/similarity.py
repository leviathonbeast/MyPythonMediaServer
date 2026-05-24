from __future__ import annotations

import logging
import math

import librosa
import numpy as np

logger = logging.getLogger(__name__)


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
        duration = librosa.get_duration(path=path)

        # Center the window on the middle of the track. For tracks shorter
        # than the window, offset clamps to 0 and librosa just returns the
        # whole file.
        offset = max(0.0, (duration / 2.0) - (excerpt_seconds / 2.0))

        y, sr = librosa.load(
            path,
            sr=22050,
            mono=True,
            offset=offset,
            duration=excerpt_seconds,
        )

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

    except Exception:
        logger.debug(
            "feature extraction failed for %s",
            path,
            exc_info=True,
        )
        return None


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
