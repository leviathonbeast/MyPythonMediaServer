"""
Tests for the Last.fm API signing helper.

Every signed Last.fm request must include an `api_sig` computed as
md5(sorted_param_concat + secret). Get this wrong and every scrobble
silently fails with "invalid signature" — the auth-flow handshake
breaks before a user can even link their account.

These tests pin the signing behaviour against the worked example from
https://www.last.fm/api/authspec, plus a handful of edge cases. Each
test catches a distinct failure mode of the implementation.
"""

from __future__ import annotations

import hashlib

from backend.core.lastfm import _sign


# Last.fm's auth-spec worked example. The spec says: "for an account with
# a secret equal to 'mysecret', you would have:
#   api_signature = md5('api_keyxxxxxxxxmethodauth.getSessiontokenxxxxxxxmysecret')"
_SPEC_PARAMS = {
    "api_key": "xxxxxxxx",
    "method":  "auth.getSession",
    "token":   "xxxxxxx",
}
_SPEC_SECRET = "mysecret"
_SPEC_INPUT_STR = "api_keyxxxxxxxxmethodauth.getSessiontokenxxxxxxxmysecret"
_SPEC_EXPECTED = hashlib.md5(_SPEC_INPUT_STR.encode("utf-8")).hexdigest()


class TestSign:
    """Each test pins one rule from the Last.fm signature spec."""

    def test_matches_canonical_spec_example(self):
        """The documented example must produce the documented hash.

        Catches: wrong concat scheme (e.g. inserting separators), failing
        to append the secret, hashing without encoding.
        """
        assert _sign(_SPEC_PARAMS, _SPEC_SECRET) == _SPEC_EXPECTED

    def test_format_param_excluded_from_signature(self):
        """`format` is part of the request URL but not part of the signature.

        Catches: signing every param indiscriminately, which would cause
        every signed call where the client also sent ?f=json to fail.
        """
        with_format = {**_SPEC_PARAMS, "format": "json"}
        assert _sign(with_format, _SPEC_SECRET) == _SPEC_EXPECTED

    def test_callback_param_excluded_from_signature(self):
        """`callback` (JSONP) is also excluded from the signature."""
        with_cb = {**_SPEC_PARAMS, "callback": "myCb"}
        assert _sign(with_cb, _SPEC_SECRET) == _SPEC_EXPECTED

    def test_param_input_order_does_not_matter(self):
        """The signer sorts internally, so caller order is irrelevant.

        Catches: dropping the sort step. Without sorting, two callers
        passing the same params in different orders would compute
        different signatures — only one of which Last.fm accepts.
        """
        a = _sign({"method": "x", "api_key": "y", "token": "z"}, "s")
        b = _sign({"token": "z", "api_key": "y", "method": "x"}, "s")
        c = _sign({"api_key": "y", "token": "z", "method": "x"}, "s")
        assert a == b == c

    def test_utf8_values_encoded_correctly(self):
        """Non-ASCII values must hash as utf-8 bytes.

        Catches: encoding as ascii/latin-1, which would silently mangle
        any artist or album with non-English characters — i.e. half a
        typical music library. The expected hash is computed locally
        from the spec's documented input format, not from the impl.
        """
        params = {"artist": "Sigur Rós", "method": "track.scrobble"}
        secret = "s"
        expected_input = "artistSigur Rósmethodtrack.scrobble" + secret
        expected = hashlib.md5(expected_input.encode("utf-8")).hexdigest()
        assert _sign(params, secret) == expected

    def test_empty_string_value_included(self):
        """A param with an empty value must still be signed as key+"".

        Catches: filtering out empty values, which would mean optional
        scrobble fields like `mbid=""` accidentally change the signature
        space and produce a different hash from what Last.fm computes.
        """
        # Spec is silent on this edge but consistent practice: include
        # every param the request sends (except format/callback).
        params = {"method": "track.scrobble", "mbid": ""}
        secret = "s"
        expected_input = "mbidmethodtrack.scrobble" + secret
        expected = hashlib.md5(expected_input.encode("utf-8")).hexdigest()
        assert _sign(params, secret) == expected
