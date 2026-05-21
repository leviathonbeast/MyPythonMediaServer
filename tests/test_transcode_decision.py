"""
Unit tests for backend/streaming/decision.py — the pure profile-matching
logic behind the OpenSubsonic getTranscodeDecision endpoint.

These exercise the decision function directly (no FastAPI, no DB) so each
branch of the matching algorithm is pinned independently of wiring. Several
tests are deliberate *negative controls*: they assert we do NOT transcode
when a constraint can't be evaluated (missing source metadata) or when a
limit is satisfied — proving the rules actually gate on the right thing
rather than always returning the same answer.
"""

from __future__ import annotations

from backend.streaming import decision as d


# Reusable profile fragments -------------------------------------------------

def _dpp(containers, codecs, protocols=None, max_ch=None):
    p = {"containers": containers, "audioCodecs": codecs}
    if protocols is not None:
        p["protocols"] = protocols
    if max_ch is not None:
        p["maxAudioChannels"] = max_ch
    return p


def _mp3_transcode_profile():
    return {"container": "mp3", "audioCodec": "mp3", "protocol": "http"}


# ---------------------------------------------------------------------------
# codec_for_suffix
# ---------------------------------------------------------------------------


class TestCodecForSuffix:
    def test_known_suffixes(self):
        assert d.codec_for_suffix("mp3") == "mp3"
        assert d.codec_for_suffix("flac") == "flac"
        assert d.codec_for_suffix("m4a") == "aac"
        assert d.codec_for_suffix("ogg") == "vorbis"

    def test_dot_and_case_normalised(self):
        assert d.codec_for_suffix(".FLAC") == "flac"

    def test_unknown_suffix_passes_through(self):
        assert d.codec_for_suffix("xyz") == "xyz"


# ---------------------------------------------------------------------------
# _satisfies (codecProfile comparison operators)
# ---------------------------------------------------------------------------


class TestSatisfies:
    def test_less_than_equal(self):
        assert d._satisfies(320000, "LessThanEqual", ["320000"]) is True
        assert d._satisfies(320001, "LessThanEqual", ["320000"]) is False

    def test_equals_any_of_values(self):
        assert d._satisfies(2, "Equals", ["1", "2"]) is True
        assert d._satisfies(6, "Equals", ["1", "2"]) is False

    def test_greater_than_equal(self):
        assert d._satisfies(48000, "GreaterThanEqual", ["44100"]) is True
        assert d._satisfies(22050, "GreaterThanEqual", ["44100"]) is False

    def test_empty_values_is_no_constraint(self):
        assert d._satisfies(999, "LessThanEqual", []) is True

    def test_unknown_operator_does_not_penalise(self):
        assert d._satisfies(5, "SomethingWeird", ["1"]) is True


# ---------------------------------------------------------------------------
# Direct play
# ---------------------------------------------------------------------------


class TestDirectPlay:
    def test_matching_profile_direct_plays(self):
        client = {"directPlayProfiles": [_dpp(["flac"], ["flac"])]}
        r = d.decide(
            source_container="flac", source_codec="flac",
            source_bitrate_bps=900000, client=client,
            transcoding_enabled=True, default_transcode_format="raw",
        )
        assert r.can_direct_play is True
        assert r.transcode_reason == []
        assert r.transcode_stream is None
        assert r.transcode_params == ""

    def test_container_mismatch_reason(self):
        client = {
            "directPlayProfiles": [_dpp(["mp3"], ["mp3"], ["http"])],
            "transcodingProfiles": [_mp3_transcode_profile()],
        }
        r = d.decide(
            source_container="flac", source_codec="flac",
            source_bitrate_bps=900000, client=client,
            transcoding_enabled=True, default_transcode_format="raw",
        )
        assert r.can_direct_play is False
        assert r.transcode_reason == [d._R_CONTAINER]

    def test_codec_mismatch_reason(self):
        # Container matches (mp4) but the codec (alac) isn't in the profile.
        client = {"directPlayProfiles": [_dpp(["mp4"], ["aac"])]}
        r = d.decide(
            source_container="mp4", source_codec="alac",
            source_bitrate_bps=900000, client=client,
            transcoding_enabled=True, default_transcode_format="mp3",
        )
        assert r.can_direct_play is False
        assert r.transcode_reason == [d._R_CODEC]

    def test_protocol_mismatch_reason(self):
        # Client will only direct-play over hls; we only serve http.
        client = {"directPlayProfiles": [_dpp(["mp3"], ["mp3"], ["hls"])]}
        r = d.decide(
            source_container="mp3", source_codec="mp3",
            source_bitrate_bps=320000, client=client,
            transcoding_enabled=True, default_transcode_format="mp3",
        )
        assert r.can_direct_play is False
        assert r.transcode_reason == [d._R_PROTOCOL]

    def test_empty_protocols_means_any(self):
        client = {"directPlayProfiles": [_dpp(["flac"], ["flac"], [])]}
        r = d.decide(
            source_container="flac", source_codec="flac",
            source_bitrate_bps=900000, client=client,
            transcoding_enabled=True, default_transcode_format="raw",
        )
        assert r.can_direct_play is True

    def test_channel_ceiling_skipped_when_source_channels_unknown(self):
        # Negative control: profile caps at 2 channels, but we don't know the
        # source channel count, so the cap must NOT block direct play.
        client = {"directPlayProfiles": [_dpp(["flac"], ["flac"], [], max_ch=2)]}
        r = d.decide(
            source_container="flac", source_codec="flac",
            source_bitrate_bps=900000, client=client,
            transcoding_enabled=True, default_transcode_format="raw",
        )
        assert r.can_direct_play is True

    def test_channel_ceiling_enforced_when_known(self):
        client = {"directPlayProfiles": [_dpp(["flac"], ["flac"], [], max_ch=2)]}
        r = d.decide(
            source_container="flac", source_codec="flac",
            source_bitrate_bps=900000, source_channels=6, client=client,
            transcoding_enabled=True, default_transcode_format="mp3",
        )
        assert r.can_direct_play is False
        assert r.transcode_reason == [d._R_CHANNELS]

    def test_no_directplay_profiles_forces_transcode(self):
        client = {"transcodingProfiles": [_mp3_transcode_profile()]}
        r = d.decide(
            source_container="flac", source_codec="flac",
            source_bitrate_bps=900000, client=client,
            transcoding_enabled=True, default_transcode_format="raw",
        )
        assert r.can_direct_play is False
        assert r.transcode_reason  # non-empty


# ---------------------------------------------------------------------------
# Global ceilings + codecProfiles
# ---------------------------------------------------------------------------


class TestGlobalConstraints:
    def test_global_max_bitrate_blocks_direct_play(self):
        client = {
            "directPlayProfiles": [_dpp(["flac"], ["flac"], [])],
            "maxAudioBitrate": 320000,
            "transcodingProfiles": [_mp3_transcode_profile()],
        }
        r = d.decide(
            source_container="flac", source_codec="flac",
            source_bitrate_bps=900000, client=client,
            transcoding_enabled=True, default_transcode_format="raw",
        )
        assert r.can_direct_play is False
        assert r.transcode_reason == [d._R_BITRATE]

    def test_codec_profile_bitrate_limit_blocks(self):
        client = {
            "directPlayProfiles": [_dpp(["mp3"], ["mp3"], [])],
            "codecProfiles": [{
                "type": "AudioCodec", "name": "mp3",
                "limitations": [{
                    "name": "audioBitrate", "comparison": "LessThanEqual",
                    "values": ["192000"], "required": True,
                }],
            }],
            "transcodingProfiles": [_mp3_transcode_profile()],
        }
        r = d.decide(
            source_container="mp3", source_codec="mp3",
            source_bitrate_bps=320000, client=client,
            transcoding_enabled=True, default_transcode_format="mp3",
        )
        assert r.can_direct_play is False
        assert r.transcode_reason == [d._R_BITRATE]

    def test_codec_profile_bitrate_limit_satisfied(self):
        # Negative control: same limit, but the source is under it → direct play.
        client = {
            "directPlayProfiles": [_dpp(["mp3"], ["mp3"], [])],
            "codecProfiles": [{
                "type": "AudioCodec", "name": "mp3",
                "limitations": [{
                    "name": "audioBitrate", "comparison": "LessThanEqual",
                    "values": ["320000"], "required": True,
                }],
            }],
        }
        r = d.decide(
            source_container="mp3", source_codec="mp3",
            source_bitrate_bps=128000, client=client,
            transcoding_enabled=True, default_transcode_format="raw",
        )
        assert r.can_direct_play is True

    def test_codec_profile_unevaluable_limit_is_skipped(self):
        # Negative control: a samplerate limit on a source whose samplerate we
        # don't know must be ignored, not treated as a failure.
        client = {
            "directPlayProfiles": [_dpp(["flac"], ["flac"], [])],
            "codecProfiles": [{
                "type": "AudioCodec", "name": "flac",
                "limitations": [{
                    "name": "audioSamplerate", "comparison": "LessThanEqual",
                    "values": ["48000"], "required": False,
                }],
            }],
        }
        r = d.decide(
            source_container="flac", source_codec="flac",
            source_bitrate_bps=900000, client=client,  # no samplerate given
            transcoding_enabled=True, default_transcode_format="raw",
        )
        assert r.can_direct_play is True


# ---------------------------------------------------------------------------
# Transcode target selection
# ---------------------------------------------------------------------------


class TestTranscodeTarget:
    def test_picks_first_supported_client_profile(self):
        client = {
            "directPlayProfiles": [_dpp(["mp3"], ["mp3"], ["http"])],
            "transcodingProfiles": [_mp3_transcode_profile()],
        }
        r = d.decide(
            source_container="flac", source_codec="flac",
            source_bitrate_bps=900000, client=client,
            transcoding_enabled=True, default_transcode_format="raw",
        )
        assert r.can_transcode is True
        assert r.transcode_stream is not None
        assert r.transcode_stream.codec == "mp3"
        assert r.transcode_params == "format=mp3&maxBitRate=320"

    def test_client_bitrate_cap_applied(self):
        client = {
            "directPlayProfiles": [_dpp(["mp3"], ["mp3"], ["http"])],
            "transcodingProfiles": [_mp3_transcode_profile()],
            "maxTranscodingAudioBitrate": 192000,
        }
        r = d.decide(
            source_container="flac", source_codec="flac",
            source_bitrate_bps=900000, client=client,
            transcoding_enabled=True, default_transcode_format="raw",
        )
        assert r.transcode_stream.audio_bitrate == 192000
        assert r.transcode_params == "format=mp3&maxBitRate=192"

    def test_target_bitrate_never_upscales_source(self):
        # 128k source → target capped at 128k even though client allows 320k.
        client = {
            "directPlayProfiles": [_dpp(["opus"], ["opus"], ["http"])],
            "transcodingProfiles": [_mp3_transcode_profile()],
        }
        r = d.decide(
            source_container="mp3", source_codec="mp3",
            source_bitrate_bps=128000, client=client,
            transcoding_enabled=True, default_transcode_format="raw",
        )
        assert r.transcode_stream.audio_bitrate == 128000

    def test_hls_only_transcode_profile_has_no_usable_target(self):
        # We can't serve hls; an hls-only client transcode profile means no
        # acceptable target, so canTranscode collapses to False.
        client = {
            "directPlayProfiles": [_dpp(["mp3"], ["mp3"], ["http"])],
            "transcodingProfiles": [
                {"container": "mp4", "audioCodec": "aac", "protocol": "hls"},
            ],
        }
        r = d.decide(
            source_container="flac", source_codec="flac",
            source_bitrate_bps=900000, client=client,
            transcoding_enabled=True, default_transcode_format="raw",
        )
        assert r.can_transcode is False
        assert r.transcode_stream is None
        assert r.error_reason  # non-empty

    def test_server_default_used_when_no_client_profiles(self):
        client = {"directPlayProfiles": [_dpp(["flac"], ["flac"], [])]}
        r = d.decide(
            source_container="mp3", source_codec="mp3",
            source_bitrate_bps=320000, client=client,
            transcoding_enabled=True, default_transcode_format="opus",
        )
        assert r.can_transcode is True
        assert r.transcode_stream.codec == "opus"

    def test_transcoding_disabled_sets_error(self):
        client = {
            "directPlayProfiles": [_dpp(["mp3"], ["mp3"], ["http"])],
            "transcodingProfiles": [_mp3_transcode_profile()],
        }
        r = d.decide(
            source_container="flac", source_codec="flac",
            source_bitrate_bps=900000, client=client,
            transcoding_enabled=False, default_transcode_format="mp3",
        )
        assert r.can_transcode is False
        assert r.transcode_stream is None
        assert r.error_reason


# ---------------------------------------------------------------------------
# Serialisation shape
# ---------------------------------------------------------------------------


class TestSerialisation:
    def test_source_stream_omits_unknown_numeric_fields(self):
        client = {"directPlayProfiles": [_dpp(["flac"], ["flac"], [])]}
        r = d.decide(
            source_container="flac", source_codec="flac",
            source_bitrate_bps=900000, client=client,
            transcoding_enabled=True, default_transcode_format="raw",
        )
        src = r.to_dict()["sourceStream"]
        assert src["audioBitrate"] == 900000
        # We don't know these, so they must be absent (not null / not 0).
        assert "audioChannels" not in src
        assert "audioSamplerate" not in src
        assert "audioBitdepth" not in src

    def test_decision_dict_has_required_keys(self):
        client = {"directPlayProfiles": [_dpp(["flac"], ["flac"], [])]}
        out = d.decide(
            source_container="flac", source_codec="flac",
            source_bitrate_bps=900000, client=client,
            transcoding_enabled=True, default_transcode_format="raw",
        ).to_dict()
        for key in ("canDirectPlay", "canTranscode", "transcodeReason",
                    "errorReason", "transcodeParams", "sourceStream"):
            assert key in out
