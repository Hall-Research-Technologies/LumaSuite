"""Audio/video breakaway routing for a LumaStream decoder.

A LumaStream decoder selects its VIDEO stream and its AUDIO stream separately.
By default the audio *follows* the video: the decoder repoints audio at the audio
stream of whichever encoder owns the selected video, even when that encoder names
its audio and video streams differently. Breakaway is the deliberate departure
from that, and every step below was measured on real decoders (see PROJECT.md,
"A/V breakaway"), not inferred:

    SelectVideoStream.Set  {selecttype, streamname, password}       the video
    SelectAudioStream.Set  {followvideoenable, selecttype, streamname}  the audio
                                                                     and the follow flag
    SelectVideoStream.Get / SelectAudioStream.Get                   the device's own answer
    AudioStreamNameList.Get / VideoStreamNameList.Get               names the decoder knows
    VideoMultiIpStream.Get / AudioMultiIpStream.Get   (on an encoder) that encoder's names

What was measured, and what this module therefore does:

  * A Set with follow off never touches the other stream; turning follow on
    resyncs audio to the current video's encoder immediately and ignores the name
    sent. So *video-only* pins audio first, *audio-only* writes audio alone, and
    *return to follow* is one audio write.
  * The decoder ACCEPTS an audio name it does not have (a video name, say) and
    answers success with an address of 0.0.0.0:0 -- a subscription to nothing.
    Transport success is not a route: an audio name is validated against the
    decoder's own list before it is written, and the read-back must show a real
    address.
  * Modes are decided by what the operator asked for; the default, AV, is the
    ordinary route. A restricted User's audio+video route is `user_av`: both planes go
    to the chosen source explicitly (audio is written when the display does not follow
    video, so a breakaway is never left half in place), a display that follows video is
    left following, and a source with no audio stream is refused before any write so
    the caller can route its video the ordinary way.

READ -> PLAN -> WRITE ONLY DIFFERENCES -> VERIFY FROM THE DEVICE -> ROLL BACK ON
FAILURE. A step that fails after something was written puts the decoder back, and
the result says whether that restoration was verified.

The device calls are injected (`rpc`), so the whole transaction is testable
without a device and cannot reach one by accident.
"""
from __future__ import annotations

import time

MODE_AV = "av"
MODE_VIDEO = "video"
MODE_AUDIO = "audio"
MODE_BREAKAWAY = "breakaway"
MODES = (MODE_AV, MODE_VIDEO, MODE_AUDIO, MODE_BREAKAWAY)
#: A restricted User's audio+video route. Not an administrator mode (it is not in MODES, so the
#: administrator's endpoint cannot name it): it is the route that sends BOTH planes to one source
#: explicitly, whatever a breakaway left behind, and leaves a display that follows video as it is.
MODE_USER_AV = "user_av"
_ACCEPTED_MODES = MODES + (MODE_USER_AV,)

#: What the decoder reports for "no audio source".
_NO_ADDRESS = ("", "0.0.0.0", None)

READ_ATTEMPTS = 3
READ_PAUSE_SECONDS = 0.25
VERIFY_ATTEMPTS = 3
VERIFY_PAUSE_SECONDS = 0.5


class Refused(Exception):
    """The request cannot be carried out, decided before anything was written."""

    def __init__(self, reason: str, message: str, status: str = "refused"):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status = status


def _result(resp):
    return resp.get("result") if isinstance(resp, dict) and isinstance(resp.get("result"), (dict, list)) else None


def _name(value) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _address(block) -> str:
    addr = (block or {}).get("multiaddr") if isinstance(block, dict) else None
    return _name((addr or {}).get("ipaddr")) if isinstance(addr, dict) else ""


class Decoder:
    """One decoder, read and written through `rpc(method, params) -> reply|None`."""

    def __init__(self, rpc, password: str, sleep=time.sleep):
        self._rpc = rpc
        self._password = password
        self._sleep = sleep
        self.writes = []                 # method names actually sent, in order

    # ---- reads (safe to repeat) ----------------------------------------
    def _read(self, method, params):
        for attempt in range(READ_ATTEMPTS):
            reply = self._rpc(method, params)
            body = _result(reply)
            if body is not None:
                return body
            if attempt + 1 < READ_ATTEMPTS:
                self._sleep(READ_PAUSE_SECONDS)
        return None

    def state(self):
        """{video, audio, follow, audio_address, video_address} from the device now, or None."""
        video = self._read("SelectVideoStream.Get", {"password": self._password})
        if not isinstance(video, dict):
            return None                  # a decoder that is not answering is asked once, not twice
        audio = self._read("SelectAudioStream.Get", {})
        if not isinstance(audio, dict):
            return None
        follow = audio.get("followvideoenable")
        return {"video": _name(video.get("streamname")),
                "audio": _name(audio.get("streamname")),
                "follow": follow if isinstance(follow, bool) else None,
                "audio_address": _address(audio),
                "video_address": _address(video)}

    def audio_names(self):
        names = self._read("AudioStreamNameList.Get", {})
        return [n for n in names if isinstance(n, str)] if isinstance(names, list) else None

    # ---- writes (never repeated blind) -----------------------------------
    def set_video(self, name):
        self.writes.append("SelectVideoStream.Set")
        reply = self._rpc("SelectVideoStream.Set",
                          {"selecttype": 0, "streamname": name, "password": self._password})
        return _result(reply) is not None

    def set_audio(self, name, follow: bool):
        self.writes.append("SelectAudioStream.Set")
        reply = self._rpc("SelectAudioStream.Set",
                          {"followvideoenable": bool(follow), "selecttype": 0, "streamname": name})
        return _result(reply) is not None


def _encoder_stream(rpc, method):
    """The stream name an encoder transmits, read now. '' when it will not say."""
    for attempt in range(READ_ATTEMPTS):
        body = _result(rpc(method, {}))
        if isinstance(body, dict) and _name(body.get("streamname")):
            return _name(body["streamname"])
        if attempt + 1 < READ_ATTEMPTS:
            time.sleep(READ_PAUSE_SECONDS)
    return ""


def route(decoder_rpc, encoder_rpc, mode, *, password, video_encoder=None,
          audio_encoder=None, sleep=time.sleep) -> dict:
    """Carry out one routing mode on one decoder and verify it from the device.

    `decoder_rpc(method, params)` talks to the decoder; `encoder_rpc(ip, method,
    params)` to an encoder. `video_encoder` / `audio_encoder` are the addresses
    the video and audio come from; AV and video-only use `video_encoder`,
    audio-only uses `audio_encoder`, breakaway needs both.

    Returns a dict: ok, status (`verified` | `not_verified` | `unreachable` |
    `failed` | `refused`), mode, reason, error, before, after, writes,
    rollback (None | `verified` | `incomplete`).
    """
    out = {"ok": False, "status": "failed", "mode": mode, "reason": "", "error": "",
           "before": None, "after": None, "writes": [], "rollback": None,
           "video_stream": "", "audio_stream": ""}
    try:
        if mode not in _ACCEPTED_MODES:
            raise Refused("unknown_mode", "That routing mode does not exist.")
        if mode in (MODE_AV, MODE_VIDEO, MODE_BREAKAWAY, MODE_USER_AV) and not video_encoder:
            raise Refused("missing_source", "Choose the source for the video.")
        if mode in (MODE_AUDIO, MODE_BREAKAWAY, MODE_USER_AV) and not audio_encoder:
            raise Refused("missing_source", "Choose the source for the audio.")
        return _run(out, decoder_rpc, encoder_rpc, mode, password, video_encoder,
                    audio_encoder, sleep)
    except Refused as refusal:
        out.update(status=refusal.status, reason=refusal.reason, error=refusal.message)
        return out


def _run(out, decoder_rpc, encoder_rpc, mode, password, video_encoder, audio_encoder, sleep):
    dec = Decoder(decoder_rpc, password, sleep=sleep)

    # READ: the decoder as it is, and each source's stream names, now.
    before = dec.state()
    if before is None:
        raise Refused("decoder_unreachable", "The display did not answer.", "unreachable")
    out["before"] = before

    want_video = want_audio = ""
    if mode in (MODE_AV, MODE_VIDEO, MODE_BREAKAWAY, MODE_USER_AV):
        want_video = _encoder_stream(lambda m, p: encoder_rpc(video_encoder, m, p), "VideoMultiIpStream.Get")
        if not want_video:
            raise Refused("source_unreachable", "The video source did not return its stream.", "unreachable")
        out["video_stream"] = want_video
    # A display that follows video resolves the audio itself, so for it the source's audio name
    # and the display's list are not consulted; every other case writes the name and must check it.
    explicit_audio = mode in (MODE_AUDIO, MODE_BREAKAWAY) or (
        mode == MODE_USER_AV and before["follow"] is not True)
    if mode == MODE_USER_AV:
        want_audio = _encoder_stream(lambda m, p: encoder_rpc(audio_encoder, m, p), "AudioMultiIpStream.Get")
        if not want_audio:
            # Decided before anything is written: the caller may route the video the ordinary way.
            raise Refused("no_source_audio", "That source has no audio stream.")
        out["audio_stream"] = want_audio
    if explicit_audio:
        if mode != MODE_USER_AV:
            want_audio = _encoder_stream(lambda m, p: encoder_rpc(audio_encoder, m, p), "AudioMultiIpStream.Get")
            if not want_audio:
                raise Refused("source_unreachable", "The audio source did not return its stream.", "unreachable")
        known = dec.audio_names()
        if known is None:
            raise Refused("decoder_unreachable",
                          "The display did not say which audio streams it knows.", "unreachable")
        # The device accepts a name it does not have and subscribes to nothing.
        if want_audio not in known:
            raise Refused("audio_not_available",
                          "That source's audio is not available to this display.")
        out["audio_stream"] = want_audio

    # PLAN: what must be true afterwards, and only the writes that get there.
    plan_audio = None        # (name, follow) or None for no audio write
    plan_video = None
    if mode == MODE_AV:
        if before["follow"] is False:
            plan_audio = (before["audio"], True)
        if before["video"] != want_video:
            plan_video = want_video
    elif mode == MODE_VIDEO:
        if before["follow"] is not False:
            if not before["audio"] or before["audio_address"] in _NO_ADDRESS:
                raise Refused("no_current_audio",
                              "This display has no audio selected, so there is none to keep "
                              "while the video changes.")
            plan_audio = (before["audio"], False)
        if before["video"] != want_video:
            plan_video = want_video
    elif mode == MODE_AUDIO:
        if before["follow"] is not False or before["audio"] != want_audio:
            plan_audio = (want_audio, False)
    elif mode == MODE_USER_AV:
        if explicit_audio:
            # Audio first, as in a breakaway, so a failure leaves the video where it was.
            if before["follow"] is not False or before["audio"] != want_audio:
                plan_audio = (want_audio, False)
        if before["video"] != want_video:
            plan_video = want_video
    else:  # breakaway: audio first, so a failure leaves the video alone
        if before["follow"] is not False or before["audio"] != want_audio:
            plan_audio = (want_audio, False)
        if before["video"] != want_video:
            plan_video = want_video

    # WRITE, then VERIFY from the device.
    failed = ""
    if plan_audio is not None and not dec.set_audio(*plan_audio):
        failed = "audio_set_failed"
    if not failed and plan_video is not None and not dec.set_video(plan_video):
        failed = "video_set_failed"

    after = None
    verified, why = False, failed
    if not failed:
        for attempt in range(VERIFY_ATTEMPTS):
            after = dec.state()
            verified, why = _check(mode, after, before, want_video, want_audio, explicit_audio)
            if verified:
                break
            if attempt + 1 < VERIFY_ATTEMPTS:
                sleep(VERIFY_PAUSE_SECONDS)
    out["after"] = after
    out["writes"] = list(dec.writes)

    if verified:
        out.update(ok=True, status="verified")
        if mode == MODE_USER_AV:
            out["audio"] = "routed" if explicit_audio else "follows"
        return out

    # ROLL BACK whatever was written, then say what happened to the restoration.
    if dec.writes:
        out["rollback"] = _roll_back(dec, before, sleep)
        out["writes"] = list(dec.writes)
        out["after"] = dec.state() or after
    out["reason"] = why or "readback_mismatch"
    if failed:
        out.update(status="failed", error="The display did not accept the change.")
    elif after is None:
        out.update(status="unreachable", error="The display did not answer the confirmation read.")
    elif why == "audio_not_available":
        out.update(status="not_verified",
                   error="The display accepted the audio but is not receiving it.")
    else:
        out.update(status="not_verified", error="The display did not report the requested routing.")
    return out


def _check(mode, after, before, want_video, want_audio, explicit_audio=False):
    """(verified, reason) from what the decoder reports now."""
    if after is None:
        return False, "readback_failed"
    if mode == MODE_USER_AV:
        if after["video"] != want_video:
            return False, "readback_mismatch"
        if explicit_audio:
            if after["audio"] != want_audio or after["follow"] is not False:
                return False, "readback_mismatch"
        elif after["follow"] is not True:
            return False, "audio_not_following"
        if after["audio_address"] in _NO_ADDRESS:
            return False, "audio_not_available"
        return True, ""
    if mode == MODE_AV:
        if after["video"] != want_video:
            return False, "readback_mismatch"
        # Audio follows: its name may differ from the video's (the encoder names
        # them separately), so it is its subscription that must be real.
        if after["follow"] is False:
            return False, "audio_not_following"
        if after["follow"] is True and after["audio_address"] in _NO_ADDRESS:
            return False, "audio_not_available"
        return True, ""
    if mode == MODE_VIDEO:
        if after["video"] != want_video:
            return False, "readback_mismatch"
        if after["follow"] is not False or after["audio"] != before["audio"]:
            return False, "audio_changed"
        return True, ""
    if mode == MODE_AUDIO:
        if after["audio"] != want_audio or after["follow"] is not False:
            return False, "readback_mismatch"
        if after["audio_address"] in _NO_ADDRESS:
            return False, "audio_not_available"
        if after["video"] != before["video"]:
            return False, "video_changed"
        return True, ""
    # breakaway
    if after["video"] != want_video:
        return False, "readback_mismatch"
    if after["audio"] != want_audio or after["follow"] is not False:
        return False, "readback_mismatch"
    if after["audio_address"] in _NO_ADDRESS:
        return False, "audio_not_available"
    return True, ""


def _roll_back(dec, before, sleep):
    """Put the decoder back as it was read, then confirm it. 'verified' | 'incomplete'."""
    current = dec.state()
    ok = True
    if current is None or current["video"] != before["video"]:
        if before["video"]:
            ok = dec.set_video(before["video"]) and ok
    # A display that followed video and still does has its audio restored by the video; writing
    # the audio again would be a device write that changes nothing.
    audio_differs = (current is None or current["follow"] != before["follow"]
                     or (before["follow"] is not True and current["audio"] != before["audio"]))
    if audio_differs and before["follow"] is not None:
        ok = dec.set_audio(before["audio"], before["follow"]) and ok
    for attempt in range(VERIFY_ATTEMPTS):
        now = dec.state()
        if (now is not None and now["video"] == before["video"]
                and now["follow"] == before["follow"]
                and (before["follow"] or now["audio"] == before["audio"])):
            return "verified"
        if attempt + 1 < VERIFY_ATTEMPTS:
            sleep(VERIFY_PAUSE_SECONDS)
    return "incomplete"


#: What a LumaStream decoder reports, and accepts, for "subscribed to nothing". Measured on a
#: HW-LUMA-D4111: SelectVideoStream.Set {selecttype 0, streamname "none"} is answered with an empty
#: address and reads back the same; SelectAudioStream.Set the same with the follow flag unchanged.
#: The device's own web page also treats a reported "none" as a state. Restored and re-read to the
#: starting state afterwards.
NONE_STREAM = "none"


def _is_none(name) -> bool:
    return isinstance(name, str) and name.strip().lower() == NONE_STREAM


def unroute(decoder_rpc, *, password, sleep=time.sleep) -> dict:
    """Put one decoder into the verified unrouted state: video and audio subscribed to nothing.

    READ -> WRITE ONLY WHAT DIFFERS -> VERIFY FROM THE DEVICE -> ROLL BACK ON FAILURE.
    Needs no source. A display that follows video has its audio cleared by the video write, so
    only the video is written and the audio is then required to read back cleared (which is
    hardware-dependent and therefore verified, never assumed). A decoder already unrouted is a
    verified no-op that writes nothing. Returns the same shape as `route`.
    """
    out = {"ok": False, "status": "failed", "mode": "unroute", "reason": "", "error": "",
           "before": None, "after": None, "writes": [], "rollback": None,
           "video_stream": "", "audio_stream": ""}
    dec = Decoder(decoder_rpc, password, sleep)
    before = dec.state()
    if before is None:
        out.update(status="unreachable", reason="decoder_unreachable",
                   error="The display did not answer.")
        return out
    out["before"] = before
    video_set = not _is_none(before["video"])
    audio_set = before["follow"] is not True and not _is_none(before["audio"])         and before["audio_address"] not in _NO_ADDRESS
    if before["follow"] is None and not _is_none(before["audio"]):
        # The follow flag was not reported: writing the audio would have to guess it.
        audio_set = False
    ok = True
    if video_set:
        ok = dec.set_video(NONE_STREAM)
    if ok and audio_set:
        ok = dec.set_audio(NONE_STREAM, before["follow"] is True)
    out["writes"] = list(dec.writes)
    after = None
    if ok:
        for attempt in range(VERIFY_ATTEMPTS):
            after = dec.state()
            if (after is not None and _is_none(after["video"])
                    and after["video_address"] in _NO_ADDRESS
                    and (_is_none(after["audio"]) or after["audio_address"] in _NO_ADDRESS)):
                out.update(ok=True, status="verified", after=after, video_stream=NONE_STREAM,
                           audio_stream=after["audio"])
                return out
            if attempt + 1 < VERIFY_ATTEMPTS:
                sleep(VERIFY_PAUSE_SECONDS)
    out["after"] = after
    if dec.writes:
        # Something was written and the display does not hold the unrouted state: put it back.
        out["rollback"] = _roll_back(dec, before, sleep)
        out["writes"] = list(dec.writes)
    out.update(status="not_verified" if ok else "failed",
               reason="readback_mismatch" if ok else "set_failed",
               error="The display did not report the unrouted state." if ok
               else "The display refused the change.")
    return out
