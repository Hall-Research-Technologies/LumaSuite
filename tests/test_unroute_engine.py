"""The Unroute engine against a decoder that behaves as one measured on hardware.

A HW-LUMA-D4111 answered SelectVideoStream.Set {selecttype 0, streamname "none"} with an empty
address and read the same back; SelectAudioStream.Set the same with the follow flag unchanged;
restoring the old names returned it exactly to where it began. The fake below behaves that way
and nothing here can reach a device: the transport is a function over a dict.
"""
import unittest

import _support  # noqa: F401  installs the fence and the import path first
import luma_breakaway as B

VIDEO = ("room-222", "239.70.2.26")
AUDIO = ("room-2211", "239.80.2.26")


class FakeDecoder:
    def __init__(self, follow=False, video=VIDEO, audio=AUDIO):
        self.video = list(video)
        self.audio = list(audio)
        self.follow = follow
        self.calls = []
        self.refuse = set()          # methods that answer with an error
        self.ignore = set()          # methods that answer success and change nothing
        self.silent = False

    def _addr(self, pair):
        return {"ipaddr": pair[1], "portnumber": 1000 if pair[1] else 0}

    def __call__(self, method, params):
        self.calls.append((method, dict(params)))
        if self.silent:
            return None
        if method == "SelectVideoStream.Get":
            return {"result": {"selecttype": 0, "streamname": self.video[0], "multiaddr": self._addr(self.video)}}
        if method == "SelectAudioStream.Get":
            return {"result": {"selecttype": 0, "followvideoenable": self.follow,
                               "streamname": self.audio[0], "multiaddr": self._addr(self.audio)}}
        if method in self.refuse:
            return {"error": {"message": "refused"}}
        if method == "SelectVideoStream.Set" and method not in self.ignore:
            name = params["streamname"]
            self.video = ["none", ""] if name == "none" else [name, "239.70.2.26"]
            if self.follow:
                self.audio = ["none", ""] if name == "none" else list(AUDIO)
        if method == "SelectAudioStream.Set" and method not in self.ignore:
            name = params["streamname"]
            self.follow = params["followvideoenable"]
            self.audio = ["none", ""] if name == "none" else [name, "239.80.2.26"]
        return {"result": {}}

    def writes(self):
        return [m for m, _ in self.calls if m.endswith(".Set")]


def unroute(decoder):
    return B.unroute(decoder, password="x", sleep=lambda s: None)


class UnrouteEngineTests(unittest.TestCase):
    def test_a_decoder_with_audio_pinned_has_video_and_audio_cleared_and_verified(self):
        d = FakeDecoder(follow=False)
        out = unroute(d)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "verified")
        self.assertEqual((d.video[0], d.audio[0]), ("none", "none"))
        self.assertEqual(d.writes(), ["SelectVideoStream.Set", "SelectAudioStream.Set"])

    def test_the_follow_flag_is_never_changed(self):
        d = FakeDecoder(follow=False)
        unroute(d)
        sent = [p for m, p in d.calls if m == "SelectAudioStream.Set"][0]
        self.assertIs(sent["followvideoenable"], False)

    def test_a_display_that_follows_video_is_written_once_and_the_audio_must_read_back_cleared(self):
        d = FakeDecoder(follow=True)
        out = unroute(d)
        self.assertTrue(out["ok"], out)
        self.assertEqual(d.writes(), ["SelectVideoStream.Set"])
        self.assertTrue(d.follow)

    def test_when_a_following_display_does_not_clear_its_audio_it_is_not_verified_and_is_put_back(self):
        d = FakeDecoder(follow=True)
        original = d.__call__

        def sticky(method, params):          # the audio does NOT follow the cleared video
            reply = original(method, params)
            if method == "SelectVideoStream.Set" and params["streamname"] == "none":
                d.audio = list(AUDIO)
            return reply
        out = B.unroute(sticky, password="x", sleep=lambda s: None)
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], "not_verified")
        self.assertEqual(out["rollback"], "verified")
        self.assertEqual(tuple(d.video), VIDEO)

    def test_already_unrouted_is_a_verified_no_op_that_writes_nothing(self):
        d = FakeDecoder(video=("none", ""), audio=("none", ""))
        out = unroute(d)
        self.assertTrue(out["ok"])
        self.assertEqual(d.writes(), [])

    def test_only_the_half_that_differs_is_written(self):
        d = FakeDecoder(follow=False, video=("none", ""))
        out = unroute(d)
        self.assertTrue(out["ok"], out)
        self.assertEqual(d.writes(), ["SelectAudioStream.Set"])

    def test_a_decoder_that_does_not_answer_is_unreachable_and_nothing_is_written(self):
        d = FakeDecoder()
        d.silent = True
        out = unroute(d)
        self.assertEqual((out["ok"], out["status"]), (False, "unreachable"))
        self.assertEqual(d.writes(), [])

    def test_a_refused_video_write_fails_without_a_rollback_of_things_never_written(self):
        d = FakeDecoder()
        d.refuse.add("SelectVideoStream.Set")
        out = unroute(d)
        self.assertEqual((out["ok"], out["status"]), (False, "failed"))
        self.assertEqual(tuple(d.video), VIDEO)

    def test_a_refused_audio_write_after_the_video_was_cleared_rolls_the_video_back(self):
        d = FakeDecoder(follow=False)
        d.refuse.add("SelectAudioStream.Set")
        out = unroute(d)
        self.assertEqual((out["ok"], out["status"]), (False, "failed"))
        self.assertEqual(out["rollback"], "verified")
        self.assertEqual(tuple(d.video), VIDEO, "the half-applied state is put back")
        self.assertEqual(tuple(d.audio), AUDIO)

    def test_a_write_the_device_accepts_and_ignores_is_not_a_success(self):
        d = FakeDecoder()
        d.ignore.add("SelectVideoStream.Set")
        out = unroute(d)
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], "not_verified")

    def test_the_stream_name_sent_is_the_one_the_device_reported_for_nothing(self):
        d = FakeDecoder()
        unroute(d)
        names = {p["streamname"] for m, p in d.calls if m.endswith(".Set")}
        self.assertEqual(names, {"none"})
        self.assertEqual(B.NONE_STREAM, "none")


if __name__ == "__main__":
    unittest.main()
