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


def extract_features(path: str) -> list[float] | None:
    """
    Extract a fixed-length timbral feature vector from an audio file.

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
        # Step 1: load a representative 60s excerpt from the middle
        # ------------------------------------------------------------
        duration = librosa.get_duration(path=path)

        # Center a 60-second window on the middle of the track.
        offset = max(0.0, (duration / 2.0) - 30.0)

        y, sr = librosa.load(
            path,
            sr=22050,
            mono=True,
            offset=offset,
            duration=60.0,
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

        # Chroma: (12, frames)
        chroma = librosa.feature.chroma_stft(y=y, sr=sr)
        chroma_mean = np.mean(chroma, axis=1)  # (12,)

        # Spectral centroid: (1, frames)
        centroid = librosa.feature.spectral_centroid(y=y, sr=sr)
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
