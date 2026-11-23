"""A stateful model of LumaStream encoders and decoders, and a loopback WebSocket stand-in.

`DeviceSim.call` has the signature of `lumaserver._ws_call_auth`, so the real route code
(`_route_av`, `_route_breakaway`, the wall operation, the shared refresh) runs against a model that
*holds state*, and a test can read the model afterwards and say what a device would show -- not only
what the HTTP response claimed. The behaviour is the behaviour measured on real decoders and written
down in `luma_breakaway`:

  * a video Set with follow on moves the audio to the audio stream of the encoder that owns the video;
  * an audio Set with follow off never touches the video, and with follow on resyncs the audio to the
    current video's encoder and ignores the name sent;
  * the decoder ACCEPTS an audio name it does not have and reports a subscription to 0.0.0.0;
  * a write can be accepted and not held (`ignore_video`, `ignore_audio`, `wall_ignore`);
  * a reply can be lost after the write happened (`lose_replies`).

Nothing here opens a socket except `WsFixture`, which listens on 127.0.0.1 only. Addresses are the
documentation ranges the other fixtures use.
"""
import base64
import copy
import hashlib
import json
import socket
import socketserver
import struct
import threading
import time

NO_ADDRESS = "0.0.0.0"
VIDEO_ADDRESS = "239.70.0.1"
AUDIO_ADDRESS = "239.80.0.1"


class SimEncoder:
    def __init__(self, ip, video, audio):
        self.ip, self.video, self.audio = ip, video, audio


class SimDecoder:
    def __init__(self, ip, video="", audio="", follow=True):
        self.ip = ip
        self.video, self.audio, self.follow = video, audio, follow
        self.known_audio = None            # None: every encoder's audio stream; else an explicit list
        self.ignore_video = False          # accepts a video Set and does not hold it
        self.ignore_audio = False
        self.silent_audio = False          # lists the audio name and reads back no subscription
        self.wall = {"vmenable": False, "vwmatrix": "1_1_1_1"}
        self.wall_ignore = False
        self.settle = 0.0                  # seconds after a video Set before a Get reports it (a slow display)
        self._settling = None              # (previous stream, monotonic time it starts reporting the new one)


class DeviceSim:
    """Every encoder and decoder of one test, with the faults a test can inject."""

    def __init__(self):
        self.lock = threading.RLock()
        self.encoders = {}
        self.decoders = {}
        self.macs = {}                      # ip -> the hardware address that unit reports
        self.calls = []                     # (ip, method, params copy), in arrival order
        self.unknown = []                   # addresses asked of that the model does not have
        self.violations = []                # protocol rules the caller broke
        # faults
        self.down = set()                   # silent: no reply at all
        self.error = set()                  # replies with a JSON-RPC error
        self.garbage = set()                # replies with a result that is neither an object nor a list
        self.slow = {}                      # ip -> seconds before the reply
        self.lose_replies = set()           # (ip, method): the write happens, the reply is lost
        self.fail_after = {}                # ip -> calls still answered before the unit goes silent
        self.on_call = None                 # hook (ip, method, params), run before the call is applied
        self.write_pause = 0.0              # seconds a write takes (outside the lock), to expose overlap
        # concurrency record
        self._writing = {}
        self.max_concurrent_writes = {}
        self.write_log = []                 # (ip, method, t_start, t_end): when each write began and ended
        self.call_log = []                  # (ip, method, t_start, t_end) for every call, reads included

    # ---- building the lab -------------------------------------------------
    def add_encoder(self, ip, video, audio):
        self.encoders[ip] = SimEncoder(ip, video, audio)
        return self.encoders[ip]

    def add_decoder(self, ip, **kw):
        self.decoders[ip] = SimDecoder(ip, **kw)
        return self.decoders[ip]

    @classmethod
    def lab(cls, sources, displays):
        """Encoders and decoders for fixture units shaped like `_support.SOURCE_A` / `DISPLAY_1`.

        Each source gets an audio stream name of its own, different from its video name, as real
        encoders do.
        """
        sim = cls()
        for unit in sources:
            video = unit["enc"]["video"]["streamname"]
            sim.add_encoder(unit["ip"], video, video.replace("stream-", "audio-"))
            sim.macs[unit["ip"]] = unit.get("mac", "")
        for unit in displays:
            sim.add_decoder(unit["ip"])
            sim.macs[unit["ip"]] = unit.get("mac", "")
        return sim

    # ---- reading the model --------------------------------------------------
    def decoder(self, ip):
        return self.decoders[ip]

    def held(self, ip):
        d = self.decoders[ip]
        return (d.video, d.audio, d.follow)

    def writes(self, ip=None):
        with self.lock:
            return [(i, m) for (i, m, _p) in self.calls
                    if m.endswith(".Set") and (ip is None or i == ip)]

    def methods(self, ip=None):
        with self.lock:
            return [m for (i, m, _p) in self.calls if ip is None or i == ip]

    def audio_of_video(self, video):
        for e in self.encoders.values():
            if e.video == video:
                return e.audio
        return None

    def encoder_audio_names(self):
        return sorted(e.audio for e in self.encoders.values())

    def consistent(self, ip):
        """A decoder that follows holds the audio of the encoder that owns its video."""
        d = self.decoders[ip]
        if d.follow and d.video:
            return d.audio == self.audio_of_video(d.video)
        return True

    # ---- the transport the application calls -----------------------------------
    def call(self, ip, method, params=None, timeout=None):
        params = params if params is not None else {}
        with self.lock:
            self.calls.append((ip, method, copy.deepcopy(params)))
            known = ip in self.encoders or ip in self.decoders
            if not known:
                self.unknown.append((ip, method))
                return None
            if ip in self.fail_after:
                if self.fail_after[ip] <= 0:
                    return None
                self.fail_after[ip] -= 1
            hook = self.on_call
            delay = self.slow.get(ip, 0.0)
        if hook:
            hook(ip, method, params)
        if delay:
            time.sleep(delay)
        if ip in self.down:
            return None
        if ip in self.error:
            return {"jsonrpc": "2.0", "error": {"code": -32000, "message": "device busy"}}
        if ip in self.garbage:
            return {"result": "???"}
        write = method.endswith(".Set")
        began = time.monotonic()
        try:
            return self._serve(ip, method, params, write)
        finally:
            with self.lock:
                self.call_log.append((ip, method, began, time.monotonic()))
                if write:
                    self.write_log.append((ip, method, began, time.monotonic()))

    def _serve(self, ip, method, params, write):
        if write and self.write_pause:
            with self.lock:
                self._writing[ip] = self._writing.get(ip, 0) + 1
                self.max_concurrent_writes[ip] = max(self.max_concurrent_writes.get(ip, 0), self._writing[ip])
            try:
                time.sleep(self.write_pause)
            finally:
                with self.lock:
                    self._writing[ip] -= 1
        with self.lock:
            reply = self._apply(ip, method, params)
            if (ip, method) in self.lose_replies:
                return None
            return reply

    # ---- device behaviour ------------------------------------------------------
    def _apply(self, ip, method, params):
        if method == "Network.Get":                 # who the unit says it is: routes confirm it
            return {"result": {"mac": self.macs.get(ip, "")}}
        enc = self.encoders.get(ip)
        if enc is not None:
            if method == "VideoMultiIpStream.Get":
                return {"result": {"streamname": enc.video, "multiip": {"ipaddr": "239.70.%d.1" % (hash(ip) % 200)}}}
            if method == "AudioMultiIpStream.Get":
                return {"result": {"streamname": enc.audio}}
            return {"result": {}}
        d = self.decoders[ip]
        if method == "SelectVideoStream.Get":
            shown = d.video
            if d._settling and time.monotonic() < d._settling[1]:
                shown = d._settling[0]           # still reporting what it held before the switch
            return {"result": {"streamname": shown, "selecttype": 0,
                               "multiaddr": {"ipaddr": VIDEO_ADDRESS, "portnumber": 1000}}}
        if method == "SelectVideoStream.Set":
            if not d.ignore_video:
                if d.settle:
                    d._settling = (d.video, time.monotonic() + d.settle)
                d.video = params.get("streamname", d.video)
                if d.follow:
                    d.audio = self.audio_of_video(d.video) or d.audio
            return {"result": {"streamname": d.video}}
        if method == "SelectAudioStream.Get":
            return {"result": self._audio(d)}
        if method == "SelectAudioStream.Set":
            flag = params.get("followvideoenable")
            if not isinstance(flag, bool):
                self.violations.append((ip, "the follow flag must be sent explicitly as a boolean"))
            if not d.ignore_audio:
                d.follow = bool(flag)
                if d.follow:
                    d.audio = self.audio_of_video(d.video) or d.audio     # the name sent is ignored
                else:
                    d.audio = params.get("streamname", d.audio)
            return {"result": self._audio(d)}
        if method in ("AudioStreamNameList.Get", "VideoStreamNameList.Get"):
            names = d.known_audio if d.known_audio is not None else self.encoder_audio_names()
            return {"result": list(names)}
        if method == "StandardVideoWallInfo.Get":
            return {"result": dict(d.wall)}
        if method == "StandardVideoWallInfo.Set":
            if not d.wall_ignore:
                d.wall = {"vmenable": bool(params.get("vmenable")), "vwmatrix": params.get("vwmatrix")}
            return {"result": {}}
        return {"result": {}}

    def _audio(self, d):
        known = (d.audio in self.encoder_audio_names() or (d.known_audio and d.audio in d.known_audio))
        known = bool(known) and not d.silent_audio
        return {"followvideoenable": d.follow, "streamname": d.audio, "selecttype": 0,
                "multiaddr": {"ipaddr": AUDIO_ADDRESS if known else NO_ADDRESS, "portnumber": 1000}}


# ============================================================================== a real socket
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError
        buf += chunk
    return buf


def _read_frame(sock):
    head = _recv_exact(sock, 2)
    opcode = head[0] & 0x0F
    masked = head[1] & 0x80
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if masked else b""
    data = _recv_exact(sock, length) if length else b""
    if masked:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return opcode, data


def _send_frame(sock, opcode, data=b""):
    n = len(data)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    sock.sendall(head + data)


class WsFixture:
    """One loopback WebSocket listener standing for one unit, answering from a `DeviceSim`.

    `mode` decides how it answers, so the real `_ws_call` meets every way a unit can misbehave:

      ok        a normal JSON-RPC reply from the model
      hang      accepts the connection and never answers the upgrade
      silent    completes the upgrade and never answers a request
      slow      answers after `delay` seconds
      garbage   a reply that is not JSON
      list      a reply that is JSON but not an object
      wrong_id  replies, over and over, to a request id nobody sent
      error     a JSON-RPC error reply
      close     closes the connection straight after the upgrade
    """

    def __init__(self, sim, ip, mode="ok", delay=0.0):
        self.sim, self.ip, self.mode, self.delay = sim, ip, mode, delay
        self.connections = 0
        self.requests = 0
        self._held = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(64)
        self.port = self._sock.getsockname()[1]
        self._alive = True
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self._alive = False
        try:
            self._sock.close()
        except OSError:
            pass
        for c in self._held:
            try:
                c.close()
            except OSError:
                pass

    def _accept(self):
        while self._alive:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, sock):
        self._held.append(sock)
        sock.settimeout(30)
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    return
                data += chunk
            if self.mode == "hang":
                time.sleep(30)
                return
            key = None
            for line in data.split(b"\r\n")[1:]:
                if b":" in line:
                    k, v = line.split(b":", 1)
                    if k.strip().lower() == b"sec-websocket-key":
                        key = v.strip()
            if key is None:
                return
            accept = base64.b64encode(hashlib.sha1(key + GUID).digest())
            sock.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                         b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            if self.mode == "close":
                return
            while True:
                opcode, payload = _read_frame(sock)
                if opcode == 8:
                    _send_frame(sock, 8)
                    return
                if opcode == 9:
                    _send_frame(sock, 10, payload)
                    continue
                if opcode not in (1, 2):
                    continue
                self.requests += 1
                if self.mode == "silent":
                    continue
                try:
                    req = json.loads(payload.decode("utf-8"))
                except ValueError:
                    _send_frame(sock, 1, b"{}")
                    continue
                if self.delay:
                    time.sleep(self.delay)
                if self.mode == "garbage":
                    _send_frame(sock, 1, b"this is not json")
                    continue
                if self.mode == "list":
                    _send_frame(sock, 1, b"[1, 2, 3]")
                    continue
                if self.mode == "wrong_id":
                    for _ in range(8):
                        _send_frame(sock, 1, json.dumps({"jsonrpc": "2.0", "id": "someone-else",
                                                         "result": {}}).encode())
                    continue
                if self.mode == "error":
                    _send_frame(sock, 1, json.dumps({"jsonrpc": "2.0", "id": req.get("id"),
                                                     "error": {"code": -32000, "message": "device busy"}}).encode())
                    continue
                reply = self.sim.call(self.ip, req.get("method", ""), req.get("params") or {})
                if reply is None:
                    continue
                reply = dict(reply, id=req.get("id"))
                reply.setdefault("jsonrpc", "2.0")
                _send_frame(sock, 1, json.dumps(reply).encode())
        except (ConnectionError, OSError, socket.timeout):
            return
