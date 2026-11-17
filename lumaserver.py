
from __future__ import annotations
# --- CSV Export Endpoint for Device Manager ---
from io import StringIO
import csv
from datetime import datetime as dt
import os, sys, json, time, re, threading, socket, ipaddress, platform, subprocess, logging, glob, io, uuid, shutil, copy, itertools
import urllib.parse
def _upload_fw_cs31_socket(ip: str, firmware_path: str, attempts: list, debug: bool=False) -> Tuple[bool,str,Dict[str,Any]]:
    """Raw socket fallback for switcher devices that speak a custom protocol on port 80.
    Observed banner 'Upload data:' indicates non-HTTP; we treat it as prompt then stream bytes.
    Returns (ok, detail, meta)."""
    total_bytes = 0
    data_bytes = None
    try:
        with open(firmware_path, 'rb') as f:
            data_bytes = f.read()
        total_bytes = len(data_bytes)
    except FileNotFoundError:
        return False, 'file not found', {}
    except Exception as e:
        return False, f'read_err:{e.__class__.__name__}', {}
    chunk_size = CONFIG.get('UPLOAD_CHUNK_SIZE', 256*1024)  # larger default for raw
    log_interval = CONFIG.get('UPLOAD_LOG_INTERVAL', 1024*1024)
    path = CONFIG.get('SWITCHER_SOCKET_PATH', '/upload')  # semantic only
    banner = ''
    sent = 0
    start = time.time()
    meta = {
        'mode':'socket', 'path': path, 'chunks': [], 'total_bytes': total_bytes,
    }
    try:
        s = socket.create_connection((ip, CONFIG.get('http_port', 80)), timeout=CONFIG.get('TIMEOUT', 10))
        s.settimeout(CONFIG.get('TIMEOUT', 10))
        # Try to read initial banner/prompt (non-blocking small window)
        try:
            banner = s.recv(256).decode(errors='ignore')
        except socket.timeout:
            banner = ''
        meta['banner'] = banner
        logging.info(f"[switcher-socket] ip={ip} banner={banner.strip()[:60]}")
        # Stream firmware bytes raw (no HTTP headers)
        for i in range(0, total_bytes, chunk_size):
            chunk = data_bytes[i:i+chunk_size]
            try:
                s.sendall(chunk)
            except Exception as e:
                logging.warning(f"[switcher-socket] ip={ip} send_err chunk_index={i//chunk_size} err={e.__class__.__name__}:{str(e)[:60]}")
                meta['error'] = f'send_err:{e.__class__.__name__}'
                s.close()
                attempts.append({'mode':'socket','path':path,'banner':banner,'sent_bytes':sent,'total_bytes':total_bytes,'ok':False,'error':meta['error']})
                return False, 'socket_send_fail', meta
            sent += len(chunk)
            if sent % log_interval < chunk_size or sent == total_bytes:
                logging.info(f"[switcher-progress] ip={ip} mode=socket sent={sent}/{total_bytes} ({(sent/total_bytes)*100:.1f}%)")
            meta['chunks'].append({'idx': i//chunk_size, 'size': len(chunk)})
        # Attempt to read acknowledgement (optional)
        ack = ''
        try:
            ack = s.recv(256).decode(errors='ignore')
        except Exception:
            ack = ''
        meta['ack'] = ack
        s.close()
        dt_ms = int((time.time()-start)*1000)
        meta['duration_ms'] = dt_ms
        meta['sent_bytes'] = sent
        ok = sent == total_bytes
        attempts.append({'mode':'socket','path':path,'banner':banner,'ack':ack,'sent_bytes':sent,'total_bytes':total_bytes,'ok':ok,'duration_ms':dt_ms})
        logging.info(f"[switcher-socket] ip={ip} complete sent={sent}/{total_bytes} ms={dt_ms} ack={ack.strip()[:40]}")
        if ok:
            return True, 'socket_ok', meta
        return False, 'socket_incomplete', meta
    except Exception as e:
        meta['error'] = f'conn_err:{e.__class__.__name__}'
        attempts.append({'mode':'socket','path':path,'banner':banner,'sent_bytes':sent,'total_bytes':total_bytes,'ok':False,'error':meta['error']})
        logging.warning(f"[switcher-socket] ip={ip} conn_err={e.__class__.__name__}:{str(e)[:80]}")
        return False, 'socket_connect_fail', meta

from html.parser import HTMLParser
from typing import Dict, Any, List, Optional, Tuple
from flask import Flask, jsonify, request, send_from_directory, abort, Response, g
import hashlib
import requests
from websocket import create_connection
from concurrent.futures import ThreadPoolExecutor, as_completed
import luma_store
import luma_statecache
import luma_projection
import luma_remote_api
import luma_remote
import luma_request
import luma_mac
import luma_gate
import luma_destinations
import luma_breakaway
import luma_scanplan
import luma_netstatus
import luma_thumbnail
import luma_previewstats
import luma_portcontrol
import luma_liveness
import luma_walls
import luma_inventory
import luma_adapters
import luma_wiring
import luma_backup

# Passwords are restricted to alphanumeric plus a fixed special-character set.
ALLOWED_PASSWORD_RE = re.compile(r"^[A-Za-z0-9!@#$%^&*]+$")


def _is_allowed_password(value: str) -> bool:
    return isinstance(value, str) and len(value) >= 6 and bool(ALLOWED_PASSWORD_RE.fullmatch(value))

# --- Application Version ---
__version__ = "1.1.1"

# --- CSV Export Endpoint for Device Manager ---

# --------- MATRIX ROUTING ENDPOINT ---------
APP = Flask(__name__)
app = APP  # lowercase alias for external importers

# --- CSV Export Endpoint for Device Manager ---
# (Moved after blueprint registration to avoid route override)


# --------- MATRIX ROUTING ENDPOINT ---------
APP = Flask(__name__)
app = APP  # lowercase alias for external importers
luma_request.install(APP, luma_gate.REMOTE_MAX_BODY_BYTES)

# --- Matrix state refresh endpoint ---
def _refresh_matrix_state_core() -> int:
    """Poll every known encoder and decoder and update the cache. Returns how many changed."""
    with cache_lock:
        units = list(cache_units.values())
    updated = 0
    results = []
    # Use a short timeout for polling (e.g., 0.7s)
    poll_timeout = 0.7
    def poll_unit(unit):
        ip = unit.get("ip")
        if not ip:
            return None
        role = (unit.get("type") or unit.get("role") or '').lower()
        device_type = (unit.get("type") or "").lower()
        changed = False
        answered = False
        try:
            if device_type == 'switcher' or 'decoder' in role:
                streamname = None
                # A switcher's type is "switcher", so it has to be recognised by type: the
                # role test below never matches it. Whether it ANSWERED is a fact about the
                # reply, not about the stream it names -- an idle switcher with nothing
                # selected answers with an empty name and is perfectly healthy.
                if device_type == 'switcher':
                    resp = _ws_call_switcher_no_password(ip, "SelectVideoStream.Get", {}, poll_timeout)
                    if resp and "result" in resp and isinstance(resp["result"], dict):
                        answered = True
                        name = resp["result"].get("streamname")
                        if isinstance(name, str) and name.strip():
                            streamname = name.strip()
                else:
                    # Standard decoder API
                    resp = _ws_call_auth(ip, "SelectVideoStream.Get", {"password": CONFIG.get("password", "password")}, poll_timeout)
                    if resp and "result" in resp and isinstance(resp["result"], dict):
                        streamname = resp["result"].get("streamname")
                        answered = True

                if streamname:
                    if "dec" not in unit or not isinstance(unit["dec"], dict):
                        unit["dec"] = {}
                    if "video" not in unit["dec"] or not isinstance(unit["dec"]["video"], dict):
                        unit["dec"]["video"] = {}
                    unit["dec"]["video"]["streamname"] = streamname
                    changed = True
            if 'encoder' in role:
                resp = _ws_call_auth(ip, "VideoMultiIpStream.Get", {}, poll_timeout)
                if resp and "result" in resp and isinstance(resp["result"], dict):
                    answered = True
                    streamname = resp["result"].get("streamname")
                    if streamname:
                        if "enc" not in unit or not isinstance(unit["enc"], dict):
                            unit["enc"] = {}
                        if "video" not in unit["enc"] or not isinstance(unit["enc"]["video"], dict):
                            unit["enc"]["video"] = {}
                        unit["enc"]["video"]["streamname"] = streamname
                        unit["enc"]["video"]["ip"] = _safe_get(resp["result"], "multiip", "ipaddr", default="")
                        unit["enc"]["video"]["port"] = _safe_get(resp["result"], "multiport", "portnum", default=None)
                        unit["enc"]["video"]["userdefineip"] = _safe_get(resp["result"], "multiip", "userdefineip", default="")
                        unit["enc"]["video"]["userdefineport"] = _safe_get(resp["result"], "multiport", "userdefineportnum", default=None)
                        changed = True
                # A unit that gave no reply at all to the first read will give none to the
                # second; asking again only doubles what an unreachable unit costs a refresh.
                aresp = (_ws_call_auth(ip, "AudioMultiIpStream.Get", {}, poll_timeout)
                         if resp is not None else None)
                if aresp and "result" in aresp and isinstance(aresp["result"], dict):
                    answered = True
                    if "enc" not in unit or not isinstance(unit["enc"], dict):
                        unit["enc"] = {}
                    if "audio" not in unit["enc"] or not isinstance(unit["enc"]["audio"], dict):
                        unit["enc"]["audio"] = {}
                    unit["enc"]["audio"]["streamname"] = aresp["result"].get("streamname", "")
                    unit["enc"]["audio"]["ip"] = _safe_get(aresp["result"], "multiip", "ipaddr", default="")
                    unit["enc"]["audio"]["port"] = _safe_get(aresp["result"], "multiport", "portnum", default=None)
                    unit["enc"]["audio"]["userdefineip"] = _safe_get(aresp["result"], "multiip", "userdefineip", default="")
                    unit["enc"]["audio"]["userdefineport"] = _safe_get(aresp["result"], "multiport", "userdefineportnum", default=None)
                    changed = True
        except Exception as e:
            logging.warning(f"[refresh_matrix_state] Failed to poll {ip}: {e}")
        # Whether the unit answered is learned from the read just made, never from
        # an extra one. A unit that missed enough refreshes in a row reads as
        # offline everywhere (luma_liveness); any answer clears it at once.
        with cache_lock:
            luma_liveness.record(unit, answered)
        return changed

    # Use ThreadPoolExecutor for parallel polling
    with ThreadPoolExecutor(max_workers=32) as executor:
        future_to_unit = {executor.submit(poll_unit, unit): unit for unit in units}
        for future in as_completed(future_to_unit):
            try:
                if future.result():
                    updated += 1
            except Exception as e:
                logging.warning(f"[refresh_matrix_state] Polling error: {e}")
    # Persist updated cache
    _persist_units_cache()
    return updated


@APP.post("/api/refresh_matrix_state")
def api_refresh_matrix_state():
    """Poll all known encoders and decoders for their real state and update the cache.

    One sweep serves every page that asks. This used to run a whole sweep for each request, so
    three open Matrix pages cost three sweeps every ten seconds and a page nobody was looking at
    kept asking; now concurrent callers share the sweep in flight, and a sweep a few seconds old
    is shared too (the same read the User pages observe through). `force` -- the Refresh button,
    or a route that has just been made -- drops what was remembered and reads again.
    """
    body = luma_request.json_object()
    if isinstance(body, dict) and body.get("force") is True:
        _observe_cache.invalidate("matrix")
    try:
        updated = _observe_cache.get("matrix", _refresh_matrix_state_core)
        with cache_lock:
            served = [dict(u, online=not luma_projection.is_offline(u)) for u in cache_units.values()]
        return jsonify({"ok": True, "updated": updated, "units": served})
    except Exception as e:
        logging.error(f"[refresh_matrix_state] Exception: {e}")
        return luma_request.internal_error(e)

#: Reads of the decoder after a route, and the pause between them. A device can
#: take a moment to report the stream it has just been given; a read is safe to
#: repeat, so the verification asks a few times before it says no.
ROUTE_READBACK_ATTEMPTS = 3
ROUTE_READBACK_PAUSE_SECONDS = 0.25


def _route_av(decoder_ip, encoder_ip, allow_cached_stream=False, check_audio=False):
    """Route one encoder's stream to one decoder, and verify it from the device.

    One logical audio+video route. LumaStream routes by selecting the encoder's
    stream on the decoder; the decoder's audio follows that selection when its
    `followvideoenable` setting is on. No separate audio command is sent -- none
    is established for this hardware -- and when `check_audio` is set the result
    says whether audio is following, so a display that is not set to follow is
    reported rather than assumed.

    Success is decided by what the decoder reports after the write, never by the
    write's acknowledgement and never by what the cache already believed. The
    cache is updated from the device's answer. When the decoder does not report
    the requested stream, the route is `not_verified`: the command was accepted
    but the display does not hold it.

    Returns a dict: ok, status (`verified` | `not_verified` | `unreachable` |
    `failed`), streamname, audio (`follows` | `not_following` | `unknown`),
    error, reason, set_result, get_result.
    """
    timeout = float(CONFIG.get("TIMEOUT", 3.0))
    result = {"ok": False, "status": "failed", "streamname": "", "audio": "unknown",
              "error": "", "reason": "", "set_result": None, "get_result": None}

    # 1. The encoder's stream, read now.
    streamname = None
    try:
        enc_video = _ws_call_auth(encoder_ip, "VideoMultiIpStream.Get", {}, timeout)
        if enc_video and "result" in enc_video and isinstance(enc_video["result"], dict):
            streamname = enc_video["result"].get("streamname")
    except Exception as e:
        logging.warning(f"[route_av] VideoMultiIpStream.Get failed for {encoder_ip}: {e}")
    streamname = str(streamname or "").strip()
    if not streamname and allow_cached_stream:
        # Preserved for the administrator's matrix, which has always done this.
        # A restricted user's route never does: it consumes state read now.
        with cache_lock:
            enc_unit = cache_units.get(encoder_ip, {})
            streamname = str((((enc_unit.get("enc") or {}).get("video") or {}).get("streamname")) or "").strip()
    if not streamname:
        result.update(status="unreachable", error="Encoder did not return streamname",
                      reason="encoder_unreachable")
        return result
    result["streamname"] = streamname

    with cache_lock:
        is_switcher = (cache_units.get(decoder_ip, {}).get("type") or "").lower() == "switcher"

    def read_back():
        if is_switcher:
            return _ws_get_selected_stream_switcher(decoder_ip, timeout), None
        resp = _ws_call_auth(decoder_ip, "SelectVideoStream.Get",
                             {"password": CONFIG.get("password", "password")}, timeout)
        name = None
        if isinstance(resp, dict) and isinstance(resp.get("result"), dict):
            name = resp["result"].get("streamname")
        return (str(name).strip() if isinstance(name, str) else None), resp

    # 2. The write.
    if is_switcher:
        set_resp = _ws_set_selected_stream_switcher(decoder_ip, streamname, timeout)
        if not set_resp:
            result.update(status="failed", error="Failed to set Switcher stream",
                          reason="set_failed")
            return result
    else:
        params = {"selecttype": 0, "streamname": streamname,
                  "password": CONFIG.get("password", "password")}
        set_resp = _ws_call_auth(decoder_ip, "SelectVideoStream.Set", params, timeout)
        if not set_resp or "result" not in set_resp:
            result.update(status="failed", error="Failed to set decoder stream",
                          reason="set_failed")
            return result
    result["set_result"] = {"streamname": set_resp} if isinstance(set_resp, str) else set_resp

    # 3. Verification: ask the decoder what it holds now.
    observed, raw = None, None
    for attempt in range(ROUTE_READBACK_ATTEMPTS):
        observed, raw = read_back()
        if observed == streamname:
            break
        if attempt + 1 < ROUTE_READBACK_ATTEMPTS:
            time.sleep(ROUTE_READBACK_PAUSE_SECONDS)
    result["get_result"] = raw if isinstance(raw, dict) else {"streamname": observed}
    verified = observed == streamname

    # 4. The cache follows the device. What the decoder reported is recorded; what
    #    we asked for is recorded as the route only once the decoder confirmed it.
    with cache_lock:
        dec_unit = cache_units.get(decoder_ip, {})
        if observed:
            if not isinstance(dec_unit.get("dec"), dict):
                dec_unit["dec"] = {}
            if not isinstance(dec_unit["dec"].get("video"), dict):
                dec_unit["dec"]["video"] = {}
            dec_unit["dec"]["video"]["streamname"] = observed
        if verified:
            dec_unit["routed_encoder_ip"] = encoder_ip
            dec_unit["routed_streamname"] = streamname
        if dec_unit:
            cache_units[decoder_ip] = dec_unit
    _persist_units_cache()

    if not verified:
        result.update(status="not_verified" if observed is not None else "unreachable",
                      reason="readback_mismatch" if observed is not None else "readback_failed",
                      error=("The display did not report the requested stream."
                             if observed is not None else
                             "The display did not answer the confirmation read."))
        return result

    result.update(ok=True, status="verified")
    if check_audio and not is_switcher:
        try:
            audio_resp = _ws_call_auth(decoder_ip, "SelectAudioStream.Get", {}, timeout)
            if isinstance(audio_resp, dict) and isinstance(audio_resp.get("result"), dict):
                follows = audio_resp["result"].get("followvideoenable")
                if isinstance(follows, bool):
                    result["audio"] = "follows" if follows else "not_following"
                _remember_decoder_audio(decoder_ip, audio_resp["result"])
        except Exception as e:
            logging.info(f"[route_av] audio follow read failed for {decoder_ip}: {e}")
    return result


def _remember_decoder_audio(decoder_ip, audio):
    """Record the audio a decoder just reported, so the cache agrees with the device.

    With audio following the video, a route changes the decoder's audio as well, and the
    routed answer is the only moment the host holds that fact: the shared refresh reads a
    decoder's video and not its audio. Without this the cache kept the audio stream the last
    scan saw, and the User matrix drew the audio dot on a source the display no longer used.
    Nothing here asks the device anything: it is the read the caller had already made.
    """
    name = audio.get("streamname")
    if not isinstance(name, str):
        return
    address = audio.get("multiaddr")
    address = address.get("ipaddr") if isinstance(address, dict) else ""
    with cache_lock:
        unit = cache_units.get(decoder_ip)
        if not isinstance(unit, dict):
            return
        dec = unit["dec"] if isinstance(unit.get("dec"), dict) else {}
        unit["dec"] = dec
        held = dec["audio"] if isinstance(dec.get("audio"), dict) else {}
        dec["audio"] = held
        changed = held.get("streamname") != name.strip()
        held["streamname"] = name.strip()
        follows = audio.get("followvideoenable")
        if isinstance(follows, bool):
            held["followvideoenable"] = follows
        if isinstance(address, str):
            held["ip"] = address
    if changed:
        _persist_units_cache()


def _route_breakaway(decoder_ip, mode, video_encoder_ip=None, audio_encoder_ip=None):
    """Administrator routing with the video and the audio chosen separately.

    The decision logic lives in `luma_breakaway`, which is given the device calls
    rather than owning them. Here those calls are the proven `_ws_call_auth`, and
    the cache follows what the decoder reported (never what was asked for).
    A switcher has no audio method observed and is not offered any mode but AV.
    """
    timeout = float(CONFIG.get("TIMEOUT", 3.0))
    password = CONFIG.get("password", "password")
    outcome = luma_breakaway.route(
        lambda method, params: _ws_call_auth(decoder_ip, method, params, timeout),
        lambda ip, method, params: _ws_call_auth(ip, method, params, timeout),
        mode, password=password,
        video_encoder=video_encoder_ip, audio_encoder=audio_encoder_ip)

    if outcome.get("ok"):
        logging.info("[route_breakaway] %s on %s verified (%d write(s))", mode, decoder_ip, len(outcome.get("writes") or []))
    else:
        # Safe context only: the mode, the reason and what became of the restoration.
        logging.warning("[route_breakaway] %s on %s: %s (%s, rollback %s)", mode, decoder_ip,
                        outcome.get("reason"), outcome.get("status"), outcome.get("rollback"))
    after = outcome.get("after")
    if isinstance(after, dict):
        with cache_lock:
            unit = cache_units.get(decoder_ip)
            if isinstance(unit, dict):
                dec = unit["dec"] if isinstance(unit.get("dec"), dict) else {}
                unit["dec"] = dec
                if not isinstance(dec.get("video"), dict):
                    dec["video"] = {}
                dec["video"]["streamname"] = after.get("video", "")
                if not isinstance(dec.get("audio"), dict):
                    dec["audio"] = {}
                audio = dec["audio"]
                audio["streamname"] = after.get("audio", "")
                if isinstance(after.get("follow"), bool):
                    audio["followvideoenable"] = after["follow"]
                audio["ip"] = after.get("audio_address", "")
                if outcome.get("ok") and video_encoder_ip and mode != luma_breakaway.MODE_AUDIO:
                    unit["routed_encoder_ip"] = video_encoder_ip
                    unit["routed_streamname"] = outcome.get("video_stream", "")
        _persist_units_cache()
    return outcome


_BREAKAWAY_STATUS = {"verified": 200, "refused": 400, "unreachable": 503, "not_verified": 502, "failed": 502}


def _offline_among(*ips):
    """The addresses, among those named, of units the host has found not answering.

    A unit the host does not know is not called offline: only a determined-offline
    answer from the shared liveness record refuses an operation.
    """
    with cache_lock:
        return [ip for ip in ips
                if isinstance(ip, str) and ip in cache_units
                and luma_projection.is_offline(cache_units[ip])]


@APP.post("/api/route_matrix")
def api_route_matrix():
    data = luma_request.json_object()
    decoder_ip = data.get("decoder_ip")
    encoder_ip = data.get("encoder_ip")

    # Administrator breakaway: only when a mode is named. With no mode the route is
    # the ordinary one below, exactly as it has always been.
    mode = data.get("mode")
    if mode is not None:
        mode = str(mode).strip().lower()
        video_ip = data.get("video_encoder_ip") or encoder_ip
        audio_ip = data.get("audio_encoder_ip") or (encoder_ip if mode == luma_breakaway.MODE_AUDIO else None)
        needed = [decoder_ip]
        if mode in (luma_breakaway.MODE_AV, luma_breakaway.MODE_VIDEO, luma_breakaway.MODE_BREAKAWAY):
            needed.append(video_ip)
        if mode in (luma_breakaway.MODE_AUDIO, luma_breakaway.MODE_BREAKAWAY):
            needed.append(audio_ip)
        try:
            for address in needed:
                ipaddress.ip_address(address)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "reason": "invalid_address", "error": "Invalid IP address format"}), 400
        if mode not in luma_breakaway.MODES:
            return jsonify({"ok": False, "reason": "unknown_mode", "error": "That routing mode does not exist."}), 400
        if _offline_among(*needed):
            return jsonify({"ok": False, "reason": "device_offline", "status": "offline",
                            "error": "That device is offline."}), 409
        with cache_lock:
            is_switcher = (cache_units.get(decoder_ip, {}).get("type") or "").lower() == "switcher"
        if is_switcher and mode != luma_breakaway.MODE_AV:
            return jsonify({"ok": False, "reason": "not_supported",
                            "error": "A switcher routes audio and video together only."}), 400
        if is_switcher:
            mode = None            # AV on a switcher is the ordinary route
    if mode is not None:
        try:
            outcome = _route_breakaway(decoder_ip, mode, video_ip, audio_ip)
        except Exception as e:
            logging.error(f"[route_matrix] breakaway exception: {type(e).__name__}")
            return jsonify({"ok": False, "reason": "failed", "error": "The route could not be carried out."}), 500
        finally:
            _observe_cache.invalidate("matrix")      # the next read sees what this just did
        body = {k: outcome.get(k) for k in ("ok", "status", "mode", "reason", "error",
                                              "before", "after", "writes", "rollback")}
        body["verified"] = bool(outcome.get("ok"))
        return jsonify(body), _BREAKAWAY_STATUS.get(outcome.get("status"), 500)

    # Validate IP addresses
    try:
        ipaddress.ip_address(decoder_ip)
        ipaddress.ip_address(encoder_ip)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Invalid IP address format"}), 400

    if not decoder_ip or not encoder_ip:
        return jsonify({"ok": False, "error": "Missing decoder_ip or encoder_ip"}), 400
    if _offline_among(decoder_ip, encoder_ip):
        return jsonify({"ok": False, "reason": "device_offline", "status": "offline",
                        "error": "That device is offline."}), 409
    try:
        outcome = _route_av(decoder_ip, encoder_ip, allow_cached_stream=True)
        if not outcome["ok"]:
            # An accepted command the display does not hold is a failure, and is
            # said as one; the cache was not told it succeeded.
            status = _BREAKAWAY_STATUS.get(outcome["status"], 500)    # a unit that does not answer is 502/503, not a server fault
            return jsonify({"ok": False, "verified": False, "status": outcome["status"],
                            "error": outcome["error"],
                            "get_result": outcome["get_result"]}), status
        return jsonify({"ok": True, "verified": True,
                        "set_result": outcome["set_result"],
                        "get_result": outcome["get_result"]})
    except Exception as e:
        import traceback
        logging.error(f"[route_matrix] Exception: {e}\n{traceback.format_exc()}")
        return luma_request.internal_error(e)
    finally:
        _observe_cache.invalidate("matrix")          # the next read sees what this just did

# ---------------- Video Wall persistence and device API ----------------

def _load_video_wall_state() -> Dict[str, Any]:
    # `ignored_candidates` is new, additive, local-only metadata (site-
    # discovery dismissal lifecycle -- see _vw_classify_candidates). Any
    # video_walls.json written before this existed simply has no key here;
    # `.get(..., [])` below (and the `default` used when the file is
    # missing/unreadable) makes that the same as "nothing ignored yet" --
    # no migration step, no rewrite forced on an untouched file.
    default = {"walls": [], "ignored_candidates": []}
    # A malformed file is quarantined and read as empty, never propagated.
    data = luma_store.read_json(VIDEO_WALL_STATE_FILE, default=None)
    if isinstance(data, dict) and isinstance(data.get("walls"), list):
        if not isinstance(data.get("ignored_candidates"), list):
            data["ignored_candidates"] = []
        return data
    return default

def _save_video_wall_state(data: Dict[str, Any]):
    try:
        payload = {
            "kind": "lumasuite.video_walls",
            "version": 1,
            "walls": data.get("walls", []) if isinstance(data, dict) else [],
            "ignored_candidates": data.get("ignored_candidates", []) if isinstance(data, dict) else [],
        }
        if not luma_store.atomic_write_json(VIDEO_WALL_STATE_FILE, payload):
            raise OSError("video wall state could not be written")
        _vw_invalidate_detect_cache()
    except Exception as e:
        logging.warning(f"[video_wall] save failed: {e}")
        raise

def _vw_device_id(unit: Dict[str, Any]) -> str:
    for key in ("mac", "serialnumber", "uuid", "id"):
        val = unit.get(key)
        if isinstance(val, str) and val.strip():
            return f"{key}:{val.strip().lower()}"
    return f"ip:{unit.get('ip', '').strip()}"

def _vw_unit_label(unit: Dict[str, Any]) -> str:
    return unit.get("hostname") or unit.get("serialnumber") or unit.get("mac") or unit.get("ip") or "Unknown"

def _vw_is_cs31(unit: Dict[str, Any]) -> bool:
    """Atlona AT-OME-CS31 is a switcher, not a LumaStream encoder or
    decoder, and has no Video Wall capability on the device itself -- see
    PROJECT.md, 'Video Wall: CS31 is not an eligible participant'. It must
    never be offered as a Video Wall source, decoder, or drop target,
    regardless of what type/role text a cache entry happens to carry, and
    regardless of what OmniStream/OmniSuite would do with an analogous
    device -- this is a LumaStream hardware fact, not a UI policy borrowed
    from anywhere else."""
    text = f"{unit.get('type','')} {unit.get('role','')} {unit.get('model','')}".lower()
    return "cs31" in text

def _vw_cs31_reason(device_id: str) -> Optional[str]:
    """If `device_id` names a real, cached CS31, say so plainly instead of
    the generic 'not discovered' -- it is excluded by design, not missing."""
    if not device_id:
        return None
    with cache_lock:
        rows = [dict(u) for u in cache_units.values()]
    for u in rows:
        if _vw_device_id(u) == device_id and _vw_is_cs31(u):
            return "CS31 switchers cannot participate in a Video Wall"
    return None

def _vw_units_snapshot() -> Dict[str, Any]:
    with cache_lock:
        rows = [dict(u) for u in cache_units.values()]
    encoders, decoders = [], []
    for u in rows:
        if _vw_is_cs31(u):
            continue
        type_text = f"{u.get('type','')} {u.get('role','')} {u.get('model','')}".lower()
        item = {
            "id": _vw_device_id(u),
            "ip": u.get("ip", ""),
            "mac": u.get("mac", ""),
            "serialnumber": u.get("serialnumber", ""),
            "hostname": u.get("hostname", ""),
            "model": u.get("model", ""),
            "type": u.get("type", ""),
            "role": u.get("role", ""),
            "label": _vw_unit_label(u),
            "online": not luma_projection.is_offline(u),
            "streamname": _safe_get(u, "enc", "video", "streamname", default="") or _safe_get(u, "dec", "video", "streamname", default="")
        }
        if "encoder" in type_text:
            encoders.append(item)
        if "decoder" in type_text or "switcher" in type_text:
            decoders.append(item)
    return {"encoders": encoders, "decoders": decoders}

def _vw_compact_snapshot(unit: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(unit, dict):
        return None
    return {
        "id": unit.get("id") or _vw_device_id(unit),
        "ip": unit.get("ip", ""),
        "mac": unit.get("mac", ""),
        "serialnumber": unit.get("serialnumber", ""),
        "hostname": unit.get("hostname", ""),
        "model": unit.get("model", ""),
        "type": unit.get("type", ""),
        "role": unit.get("role", ""),
        "label": unit.get("label") or _vw_unit_label(unit),
        "streamname": unit.get("streamname", "")
    }

def _vw_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default

def _vw_clean_snapshot(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    return {
        "id": str(value.get("id") or ""),
        "ip": str(value.get("ip") or ""),
        "mac": str(value.get("mac") or ""),
        "serialnumber": str(value.get("serialnumber") or ""),
        "hostname": str(value.get("hostname") or ""),
        "model": str(value.get("model") or ""),
        "type": str(value.get("type") or ""),
        "role": str(value.get("role") or ""),
        "label": str(value.get("label") or value.get("hostname") or value.get("ip") or ""),
        "streamname": str(value.get("streamname") or "")
    }

def _vw_clean_display(value: Any) -> Dict[str, int]:
    display = value if isinstance(value, dict) else {}
    return {
        "totalwidth": _vw_int(display.get("totalwidth"), 0),
        "totalheight": _vw_int(display.get("totalheight"), 0),
        "topborder": _vw_int(display.get("topborder"), 0),
        "bottomborder": _vw_int(display.get("bottomborder"), 0),
        "leftborder": _vw_int(display.get("leftborder"), 0),
        "rightborder": _vw_int(display.get("rightborder"), 0),
    }

def _vw_is_contiguous_run(values: List[int]) -> bool:
    """True if a sorted list of distinct ints has no gap -- e.g. [1,2,3] is
    contiguous, [1,3] is not. Used to reject a region whose distinct rows
    (or columns) merely have a count matching a rectangle's, without those
    rows actually being adjacent -- see _vw_clean_regions()."""
    return bool(values) and values[-1] - values[0] + 1 == len(values)

def _vw_clean_regions(value: Any, rows: int, columns: int) -> List[Dict[str, Any]]:
    """A region is an additional source assignment spanning more than one
    physical cell -- the spatial Video Wall's boundary/intersection drop
    targets. It sits alongside each cell's own source_mode rather than
    replacing it, so a wall with no regions behaves exactly as it did before
    regions existed, and _vw_effective_matrix_and_source() below is the only
    place that has to know regions exist at all.

    A region's cells must form a complete rectangle (no holes, nothing
    outside the wall) or it is dropped rather than guessed at, and no cell
    may belong to two regions -- the later one loses the overlapping cell.
    """
    out = []
    if not isinstance(value, list):
        return out
    claimed = set()
    for region in value:
        if not isinstance(region, dict):
            continue
        cells = []
        seen = set()
        for rc in region.get("cells") or []:
            if not isinstance(rc, dict):
                continue
            r = _vw_int(rc.get("row"), 0)
            c = _vw_int(rc.get("column"), 0)
            if r < 1 or r > rows or c < 1 or c > columns or (r, c) in seen:
                continue
            seen.add((r, c))
            cells.append({"row": r, "column": c})
        if len(cells) < 2:
            continue  # a single cell is source_mode="override", not a region
        rows_present = sorted(set(c["row"] for c in cells))
        cols_present = sorted(set(c["column"] for c in cells))
        if len(cells) != len(rows_present) * len(cols_present):
            continue  # not a full rectangle
        if not _vw_is_contiguous_run(rows_present) or not _vw_is_contiguous_run(cols_present):
            continue  # e.g. rows 1 and 3 with row 2 absent -- count matches a
            # rectangle's, but the rows themselves aren't physically adjacent
        if seen & claimed:
            continue  # already part of an earlier region
        claimed |= seen
        out.append({
            "id": str(region.get("id") or f"region-{uuid.uuid4().hex[:8]}"),
            "cells": sorted(cells, key=lambda c: (c["row"], c["column"])),
            "source_id": str(region.get("source_id") or ""),
            "source_snapshot": _vw_clean_snapshot(region.get("source_snapshot")),
        })
    return out

def _vw_zone_cells(zone: Dict[str, Any]) -> List[Dict[str, int]]:
    """Member cells derived from a zone's canonical rectangle (row1/column1
    top-left, row2/column2 bottom-right) -- never stored separately, so
    there is exactly one source of truth for a zone's shape."""
    return [
        {"row": r, "column": c}
        for r in range(zone["row1"], zone["row2"] + 1)
        for c in range(zone["column1"], zone["column2"] + 1)
    ]

def _vw_zone_label(zone: Dict[str, Any]) -> str:
    name = str(zone.get("name") or "").strip()
    if name:
        return name
    rows = zone["row2"] - zone["row1"] + 1
    cols = zone["column2"] - zone["column1"] + 1
    return f"{rows}x{cols} · R{zone['row1']}C{zone['column1']}–R{zone['row2']}C{zone['column2']}"

def _vw_clean_zones(value: Any, rows: int, columns: int) -> List[Dict[str, Any]]:
    """A zone is a SAVED, REUSABLE rectangular routing-target definition --
    a different lifecycle from wall.regions (see _vw_clean_regions), which
    is live/active routing state that a single source-routing action
    creates and "Apply to Entire Wall" clears. A zone carries no source_id
    and triggers no device call by existing; it is purely metadata that the
    UI offers as a drop/click target, which then creates an ordinary region
    (via the existing /api/video_wall/source scope="region" path) when the
    operator actually routes a source to it.

    Unlike regions, zones MAY overlap each other -- "Top Row", "Left 2x2"
    and "Entire Wall" are all legitimate, simultaneously useful saved
    shortcuts on the same wall (see PROJECT.md). Canonical shape is a
    rectangle's two corners (row1/column1, row2/column2); member cells are
    always derived from that via _vw_zone_cells(), never stored twice.
    """
    out = []
    if not isinstance(value, list):
        return out
    seen_ids = set()
    for zone in value:
        if not isinstance(zone, dict):
            continue
        r1 = _vw_int(zone.get("row1"), 0)
        c1 = _vw_int(zone.get("column1"), 0)
        r2 = _vw_int(zone.get("row2"), 0)
        c2 = _vw_int(zone.get("column2"), 0)
        top, bottom = sorted((r1, r2))
        left, right = sorted((c1, c2))
        if top < 1 or left < 1 or bottom > rows or right > columns:
            continue  # would no longer fit the wall's current bounds
        if (bottom - top + 1) * (right - left + 1) < 2:
            continue  # a single cell is already independently routable -- not a zone
        zid = str(zone.get("id") or "")
        if not zid or zid in seen_ids:
            zid = f"zone-{uuid.uuid4().hex[:8]}"
        seen_ids.add(zid)
        out.append({
            "id": zid,
            "name": str(zone.get("name") or "").strip(),
            "row1": top, "column1": left, "row2": bottom, "column2": right,
        })
    return out

def _vw_clean_wall(wall: Dict[str, Any]) -> Dict[str, Any]:
    rows = max(1, min(16, _vw_int(wall.get("rows"), 2)))
    columns = max(1, min(16, _vw_int(wall.get("columns"), 2)))
    cleaned = {
        "id": str(wall.get("id") or f"wall-{uuid.uuid4().hex[:12]}"),
        "name": str(wall.get("name") or "Video Wall"),
        "rows": rows,
        "columns": columns,
        "source_id": str(wall.get("source_id") or ""),
        "source_snapshot": _vw_clean_snapshot(wall.get("source_snapshot")),
        "display": _vw_clean_display(wall.get("display")),
        "updated_at": _vw_int(wall.get("updated_at"), int(time.time())),
        "regions": _vw_clean_regions(wall.get("regions"), rows, columns),
        "zones": _vw_clean_zones(wall.get("zones"), rows, columns),
        # Local-only site-discovery metadata (see _vw_classify_candidates /
        # "Confirm & Save Detected Wall"). All three are optional and
        # additive: a wall saved before this existed has none of them, and
        # they default to exactly the pre-existing implicit state ("this
        # was created/edited by hand, never matched against a detected
        # candidate") -- no migration, no behavior change for old data.
        "origin": "detected" if wall.get("origin") == "detected" else "manual",
        "confirmed_at": _vw_int(wall.get("confirmed_at"), 0) or None,
        "confirmed_fingerprint": str(wall.get("confirmed_fingerprint") or ""),
        "cells": []
    }
    for cell in wall.get("cells", []) if isinstance(wall.get("cells"), list) else []:
        if not isinstance(cell, dict):
            continue
        row = max(1, min(rows, _vw_int(cell.get("row"), 1)))
        column = max(1, min(columns, _vw_int(cell.get("column"), 1)))
        cleaned["cells"].append({
            "row": row,
            "column": column,
            "decoder_id": str(cell.get("decoder_id") or ""),
            "source_mode": "override" if cell.get("source_mode") == "override" else "wall",
            "override_source_id": str(cell.get("override_source_id") or ""),
            "rotation": _vw_int(cell.get("rotation"), 0),
            "display": dict(cell.get("display") or {}) if isinstance(cell.get("display"), dict) else {},
            "decoder_snapshot": _vw_clean_snapshot(cell.get("decoder_snapshot")),
            "override_source_snapshot": _vw_clean_snapshot(cell.get("override_source_snapshot")),
        })
    return cleaned

def _vw_enrich_wall(wall: Dict[str, Any]) -> Dict[str, Any]:
    snap = _vw_units_snapshot()
    devices = {u.get("id"): u for u in snap.get("encoders", []) + snap.get("decoders", []) if u.get("id")}
    out = _vw_clean_wall(wall)
    source_id = out.get("source_id")
    out["source_snapshot"] = _vw_compact_snapshot(devices.get(source_id)) or out.get("source_snapshot")
    cells = []
    for cell in out.get("cells", []) if isinstance(out.get("cells"), list) else []:
        next_cell = dict(cell)
        next_cell.pop("live_source_pending", None)
        decoder_id = next_cell.get("decoder_id")
        override_id = next_cell.get("override_source_id")
        next_cell["decoder_snapshot"] = _vw_compact_snapshot(devices.get(decoder_id)) or next_cell.get("decoder_snapshot")
        next_cell["override_source_snapshot"] = _vw_compact_snapshot(devices.get(override_id)) or next_cell.get("override_source_snapshot")
        cells.append(next_cell)
    out["cells"] = cells
    regions = []
    for region in out.get("regions", []) if isinstance(out.get("regions"), list) else []:
        next_region = dict(region)
        next_region["source_snapshot"] = _vw_compact_snapshot(devices.get(region.get("source_id"))) or region.get("source_snapshot")
        regions.append(next_region)
    out["regions"] = regions
    return out

def _vw_find_unit(device_id: str) -> Optional[Dict[str, Any]]:
    snap = _vw_units_snapshot()
    for u in snap["encoders"] + snap["decoders"]:
        if u.get("id") == device_id:
            return u
    return None

def _vw_parse_matrix(value: Any) -> Dict[str, Optional[int]]:
    out = {"rows": None, "columns": None, "row": None, "column": None}
    if not isinstance(value, str):
        return out
    parts = value.split("_")
    if len(parts) != 4:
        return out
    try:
        out.update({"rows": int(parts[0]), "columns": int(parts[1]), "row": int(parts[2]), "column": int(parts[3])})
    except Exception:
        pass
    return out

def _vw_call(ip: str, method: str, params: Optional[Any] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
    try:
        resp = _ws_call_auth(ip, method, params or {}, timeout or CONFIG.get("TIMEOUT", 3.0))
        if not resp:
            return {"ok": False, "error": "no response"}
        if resp.get("error"):
            return {"ok": False, "error": resp["error"]}
        return {"ok": True, "result": resp.get("result")}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _vw_read_decoder(unit: Dict[str, Any]) -> Dict[str, Any]:
    ip = unit.get("ip", "")
    reported = {"device_id": unit.get("id") or _vw_device_id(unit), "ip": ip, "label": unit.get("label") or _vw_unit_label(unit), "online": bool(ip)}
    calls = {
        "wall": _vw_call(ip, "StandardVideoWallInfo.Get"),
        "bezel": _vw_call(ip, "VideoWallBezel.Get"),
        "display": _vw_call(ip, "VideoWallDisplayPref.Get"),
        "source": _vw_call(ip, "SelectVideoStream.Get", {"password": CONFIG.get("password", "password")}),
    }
    reported["calls"] = calls
    wall = calls["wall"].get("result") if calls["wall"].get("ok") else None
    if isinstance(wall, dict):
        reported["video_wall"] = {**_vw_parse_matrix(wall.get("vwmatrix")), "vmenable": wall.get("vmenable")}
    bezel = calls["bezel"].get("result") if calls["bezel"].get("ok") else None
    if isinstance(bezel, dict):
        reported["bezel"] = bezel
    display = calls["display"].get("result") if calls["display"].get("ok") else None
    if isinstance(display, dict):
        reported["rotation"] = display.get("rotate")
    source = calls["source"].get("result") if calls["source"].get("ok") else None
    if isinstance(source, dict):
        reported["source"] = source.get("streamname") or _safe_get(source, "multiaddr", "ipaddr", default="")
    reported["online"] = any(c.get("ok") for c in calls.values())
    return reported

def _vw_encoder_stream(encoder_id: str) -> Dict[str, Any]:
    unit = _vw_find_unit(encoder_id)
    if not unit:
        return {"ok": False, "error": "source encoder not found"}
    ip = unit.get("ip")
    streamname = unit.get("streamname")
    if ip:
        resp = _vw_call(ip, "VideoMultiIpStream.Get")
        if resp.get("ok") and isinstance(resp.get("result"), dict):
            streamname = resp["result"].get("streamname") or streamname
    if not streamname:
        return {"ok": False, "error": "source encoder has no streamname"}
    return {"ok": True, "streamname": streamname, "ip": ip}

def _vw_set_decoder_source(decoder_ip: str, streamname: str) -> Dict[str, Any]:
    return _vw_call(decoder_ip, "SelectVideoStream.Set", {"selecttype": 0, "streamname": streamname, "password": CONFIG.get("password", "password")})

def _vw_decoder_selected_stream(decoder_ip: str) -> Dict[str, Any]:
    r = _vw_call(decoder_ip, "SelectVideoStream.Get", {"password": CONFIG.get("password", "password")})
    if not r.get("ok"):
        return r
    result = r.get("result")
    streamname = result.get("streamname") if isinstance(result, dict) else ""
    return {"ok": True, "streamname": str(streamname or "").strip(), "result": result}

def _vw_set_decoder_source_if_needed(decoder_ip: str, streamname: str) -> Dict[str, Any]:
    streamname = str(streamname or "").strip()
    current = _vw_decoder_selected_stream(decoder_ip)
    if current.get("ok") and current.get("streamname") == streamname:
        return {"ok": True, "skipped": True, "streamname": streamname, "current": current.get("streamname")}
    r = _vw_set_decoder_source(decoder_ip, streamname)
    if current.get("ok"):
        r["previous_streamname"] = current.get("streamname")
    else:
        r["current_check_error"] = current.get("error")
    return r

def _vw_decoder_wall_info(decoder_ip: str) -> Dict[str, Any]:
    r = _vw_call(decoder_ip, "StandardVideoWallInfo.Get")
    if not r.get("ok"):
        return r
    result = r.get("result")
    if not isinstance(result, dict):
        return {"ok": False, "error": "invalid wall info response"}
    return {
        "ok": True,
        "vmenable": result.get("vmenable"),
        "vwmatrix": result.get("vwmatrix"),
        "result": result,
    }

def _vw_set_wall_info_if_needed(decoder_ip: str, enabled: bool, matrix: str) -> Dict[str, Any]:
    current = _vw_decoder_wall_info(decoder_ip)
    if current.get("ok") and bool(current.get("vmenable")) == bool(enabled) and current.get("vwmatrix") == matrix:
        return {"ok": True, "skipped": True, "vmenable": enabled, "vwmatrix": matrix}
    # A vwmatrix write made in the SAME call as vmenable False->True is not
    # reliably applied by real hardware (verified live -- PROJECT.md, section
    # "StandardVideoWallInfo.Set write behavior"): the Set() response reports
    # ok, but an immediate readback shows the coordinate unchanged. The
    # reliable pattern is two separate calls: prime vmenable=true at the
    # decoder's CURRENT vwmatrix first, then set the new vwmatrix in a second
    # call. Only needed for this specific transition -- already-enabled
    # decoders and disabling both apply correctly in one call.
    priming = None
    if (
        enabled
        and current.get("ok")
        and not current.get("vmenable")
        and current.get("vwmatrix")
        and current.get("vwmatrix") != matrix
    ):
        priming = _vw_call(decoder_ip, "StandardVideoWallInfo.Set", {"vmenable": True, "vwmatrix": current.get("vwmatrix")})
    r = _vw_call(decoder_ip, "StandardVideoWallInfo.Set", {"vmenable": enabled, "vwmatrix": matrix})
    if priming is not None:
        r["primed"] = priming
    if current.get("ok"):
        r["previous_vmenable"] = current.get("vmenable")
        r["previous_vwmatrix"] = current.get("vwmatrix")
    else:
        r["current_check_error"] = current.get("error")
    return r

def _vw_decoder_bezel(decoder_ip: str) -> Dict[str, Any]:
    r = _vw_call(decoder_ip, "VideoWallBezel.Get")
    if not r.get("ok"):
        return r
    result = r.get("result")
    if not isinstance(result, dict):
        return {"ok": False, "error": "invalid bezel response"}
    return {"ok": True, "bezel": _vw_clean_display(result), "result": result}

def _vw_set_bezel_if_needed(decoder_ip: str, params: Dict[str, int]) -> Dict[str, Any]:
    desired = _vw_clean_display(params)
    current = _vw_decoder_bezel(decoder_ip)
    if current.get("ok") and current.get("bezel") == desired:
        return {"ok": True, "skipped": True, "bezel": desired}
    r = _vw_call(decoder_ip, "VideoWallBezel.Set", desired)
    if current.get("ok"):
        r["previous_bezel"] = current.get("bezel")
    else:
        r["current_check_error"] = current.get("error")
    return r

def _vw_decoder_rotation(decoder_ip: str) -> Dict[str, Any]:
    r = _vw_call(decoder_ip, "VideoWallDisplayPref.Get")
    if not r.get("ok"):
        return r
    result = r.get("result")
    rotation = result.get("rotate") if isinstance(result, dict) else None
    return {"ok": True, "rotation": _vw_int(rotation, 0), "result": result}

def _vw_set_rotation_if_needed(decoder_ip: str, rotation: int) -> Dict[str, Any]:
    desired = _vw_int(rotation, 0)
    current = _vw_decoder_rotation(decoder_ip)
    if current.get("ok") and current.get("rotation") == desired:
        return {"ok": True, "skipped": True, "rotation": desired}
    r = _vw_call(decoder_ip, "VideoWallDisplayPref.Set", {"rotate": desired})
    if current.get("ok"):
        r["previous_rotation"] = current.get("rotation")
    else:
        r["current_check_error"] = current.get("error")
    return r

def _vw_read_wall_mode(decoder_ip: str) -> Dict[str, Any]:
    """The decoder's own Video Wall mode, read from the device: the
    StandardVideoWallInfo.Get fields vmenable (boolean) and vwmatrix
    ("rows_columns_row_column"). Never inferred from saved wall membership.
    An unreadable or malformed answer is reported as unreadable, not as off."""
    info = _vw_decoder_wall_info(decoder_ip)
    if not info.get("ok"):
        return {"ok": False, "error": info.get("error") or "wall mode could not be read"}
    enabled = info.get("vmenable")
    if not isinstance(enabled, bool):
        return {"ok": False, "error": "decoder returned an unrecognised wall mode value",
                "raw_vmenable": enabled, "vwmatrix": info.get("vwmatrix")}
    return {"ok": True, "enabled": enabled, "vwmatrix": info.get("vwmatrix")}

def _vw_verify_wall_mode(decoder_ip: str, enabled: bool, matrix: Optional[str] = None) -> Dict[str, Any]:
    """Read the decoder again after a write and compare with what was asked.
    A Set that the decoder acknowledged is not a held setting (PROJECT.md,
    'StandardVideoWallInfo.Set write behavior'), so this is the only proof."""
    observed = _vw_read_wall_mode(decoder_ip)
    if not observed.get("ok"):
        return {"ok": False, "error": f"read-back failed: {observed.get('error')}", "observed": None}
    problems = []
    if observed["enabled"] != bool(enabled):
        problems.append(f"decoder reports wall mode {'enabled' if observed['enabled'] else 'disabled'}, expected {'enabled' if enabled else 'disabled'}")
    if matrix is not None and observed.get("vwmatrix") != matrix:
        problems.append(f"decoder reports position {observed.get('vwmatrix')}, expected {matrix}")
    out = {"ok": not problems, "observed": {"enabled": observed["enabled"], "vwmatrix": observed.get("vwmatrix")}}
    if problems:
        out["error"] = "; ".join(problems)
    return out

def _vw_member_failure_reason(result: Dict[str, Any]) -> str:
    step = next((st for st in (result.get("steps") or []) if not st.get("ok")), {})
    detail = step.get("error") or result.get("error") or "failed"
    return f"{step['step']}: {detail}" if step.get("step") else str(detail)

def _vw_apply_summary(results: Dict[str, Any]) -> Dict[str, Any]:
    """Aggregate an apply honestly: complete only when every member verified."""
    total = len(results)
    good = [d for d, r in results.items() if r.get("ok")]
    failed = [{"decoder_id": d, "label": r.get("label") or d, "reason": _vw_member_failure_reason(r)}
              for d, r in results.items() if not r.get("ok")]
    if not total:
        status = "empty"
    elif not failed:
        status = "complete"
    elif good:
        status = "partial"
    else:
        status = "failed"
    return {"status": status, "members": total, "succeeded": len(good), "failed_members": failed}

def _vw_cell_bezel(wall: Dict[str, Any], cell: Dict[str, Any]) -> Dict[str, Any]:
    global_bezel = wall.get("display") or {}
    return {**global_bezel, **(cell.get("display") or {})}

def _vw_compare_cell(wall: Dict[str, Any], cell: Dict[str, Any], reported: Dict[str, Any]) -> List[str]:
    diffs = []
    vw = reported.get("video_wall") or {}
    if not reported.get("online"):
        return ["offline"]
    rows, cols, row, col, enable, _source_id = _vw_effective_matrix_and_source(wall, cell)
    for key, expected in (("rows", rows), ("columns", cols), ("row", row), ("column", col)):
        if vw.get(key) is not None and int(vw.get(key)) != int(expected):
            diffs.append(f"{key}: saved {expected}, reported {vw.get(key)}")
    if vw.get("vmenable") is not None and bool(vw.get("vmenable")) != enable:
        diffs.append(f"video wall enabled: saved {enable}, reported {vw.get('vmenable')}")
    if reported.get("rotation") is not None and int(reported.get("rotation") or 0) != int(cell.get("rotation") or 0):
        diffs.append(f"rotation: saved {cell.get('rotation') or 0}, reported {reported.get('rotation')}")
    return diffs

def _vw_detect_walls(decoder_states: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure grouping/classification -- no device I/O, takes already-read
    per-decoder state (see _vw_read_decoder / api_video_wall_detect).

    Verified from real hardware (four decoders already forming a working
    2x2): `StandardVideoWallInfo.Get`'s vwmatrix ("rows_columns_row_column")
    is the ONLY field that reliably establishes wall membership and
    topology. `vmenable` does NOT -- it reflects whether THIS decoder is
    CURRENTLY showing the wall-cropped source right now (true) or an
    independently-selected one (false, LumaSuite's own "override" mode),
    which can legitimately differ across members of one otherwise-intact
    wall (observed live: two of four members read vmenable=false while all
    four carried complete, non-conflicting 2x2 coordinates). So membership
    is decided from vwmatrix's rows/columns/row/column alone; vmenable and
    the reported streamname are carried through per-member for the operator
    to see, never used to decide who belongs.

    There is no explicit wall identifier anywhere in the protocol. Decoders
    are grouped by their own reported (rows, columns) -- the only shared
    characteristic the hardware actually exposes. Two same-sized but
    physically distinct walls are indistinguishable from this data alone;
    rather than guess a split, a duplicate coordinate inside one (rows,
    columns) group is reported as a conflict naming every claimant, never
    silently resolved by picking one.

    A decoder whose own vwmatrix cannot be parsed into four positive ints,
    or whose row/column falls outside its own claimed rows/columns, is
    never allowed to join a group: the first case is unassigned/standalone,
    the second is reported as its own invalid-coordinate entry. A group
    that reduces to a single member (rows*columns == 1) is not a wall --
    the decoder is reported as standalone instead, matching the fact that a
    single-cell target in the spatial UI is the existing override
    mechanism, not a one-cell "wall".
    """
    groups: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
    unassigned: List[Dict[str, Any]] = []
    invalid: List[Dict[str, Any]] = []

    for d in decoder_states:
        vw = d.get("video_wall") or {}
        rows, columns, row, column = vw.get("rows"), vw.get("columns"), vw.get("row"), vw.get("column")
        values = (rows, columns, row, column)
        if any(not isinstance(x, int) or isinstance(x, bool) or x <= 0 for x in values):
            unassigned.append(d)
            continue
        if row > rows or column > columns:
            invalid.append(d)
            continue
        groups.setdefault((rows, columns), []).append(d)

    walls = []
    for (rows, columns), members in groups.items():
        if rows * columns <= 1:
            unassigned.extend(members)
            continue
        by_coord: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        for d in members:
            vw = d["video_wall"]
            by_coord.setdefault((vw["row"], vw["column"]), []).append(d)
        conflicts = [
            {"row": r, "column": c, "claimants": [dd.get("device_id") for dd in claimants]}
            for (r, c), claimants in by_coord.items() if len(claimants) > 1
        ]
        clean_coords = {k: v[0] for k, v in by_coord.items() if len(v) == 1}
        expected = {(r, c) for r in range(1, rows + 1) for c in range(1, columns + 1)}
        missing = sorted(set(expected) - set(by_coord.keys()))
        status = "conflict" if conflicts else ("complete" if not missing else "partial")
        walls.append({
            "rows": rows,
            "columns": columns,
            "status": status,
            "member_count": len(clean_coords),
            "expected_count": rows * columns,
            "members": {f"{r}:{c}": d for (r, c), d in clean_coords.items()},
            "missing": [{"row": r, "column": c} for (r, c) in missing],
            "conflicts": conflicts,
        })
    return {"walls": walls, "unassigned": unassigned, "invalid": invalid}


# ---- Site-discovery candidate classification ------------------------------
#
# Everything below is a pure, read-only wrapper AROUND _vw_detect_walls() --
# it adds a stable identity, a qualitative status, saved-wall matching, and
# a dismissal lifecycle on top of that function's existing grouping, but it
# is not a second scanner: the only device I/O anywhere in this feature is
# the same per-decoder .Get calls _vw_read_decoder() already made for the
# single-wall detection banner. See PROJECT.md, "Video Wall: site discovery
# / existing-configuration candidates" for the full evidence hierarchy this
# implements.
#
# EVIDENCE HIERARCHY (verified against real hardware -- see PROJECT.md):
#   STRONG / AUTHORITATIVE:
#     - StandardVideoWallInfo.Get's vwmatrix (rows/columns/row/column) --
#       the only field that establishes membership and position at all.
#     - An existing saved wall's exact decoder-ID SET (regardless of which
#       coordinate each member currently claims) -- "this is a wall the
#       operator already confirmed", not a fresh guess.
#   SUPPORTING ONLY (may be shown to the operator, may never split or merge
#   a group, may never establish membership on its own):
#     - streamname / currently-selected source -- proven highly volatile
#       across an intact wall's own members (PROJECT.md sec. 26/27); shown
#       per-cell for context only.
#     - rotation -- stable per decoder, but reflects physical mounting, not
#       wall topology; informational only.
#   PROHIBITED (never used to establish, split, or merge membership, under
#   any circumstance):
#     - discovery order, IP ordering, hostname similarity, streamname alone,
#       vmenable alone.
#   UNUSABLE, per real-hardware Phase 1 investigation (see PROJECT.md):
#     - VideoWallBezel.Get ("crop"/geometry). Live-verified against the real
#       four-decoder 2x2: all four decoders reported the IDENTICAL bezel
#       (totalwidth/totalheight/all four borders) regardless of quadrant
#       (R1C1..R2C2), and it did not change across whole-wall routing,
#       region routing, or an individual 1x1 override. It is a per-display
#       physical-screen-size setting, not a per-cell crop rectangle -- it
#       carries no topology signal at all and is not part of this hierarchy.

def _vw_candidate_device_set_fingerprint(rows: int, columns: int, member_ids: List[str]) -> str:
    """Identity for 'this SET of decoders, at this wall size', independent
    of which coordinate each one currently claims. Used to match a fresh
    candidate against an existing saved wall (the same rule the pre-existing
    matched_saved_wall lookup in api_video_wall_detect already used) and
    stored on a saved wall as confirmed_fingerprint at Confirm & Save time."""
    ids = sorted(set(i for i in member_ids if i))
    basis = f"{rows}x{columns}|" + ",".join(ids)
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

def _vw_candidate_fingerprint(candidate: Dict[str, Any]) -> str:
    """Stable identity for a detected candidate AS CURRENTLY OBSERVED --
    independent of discovery order (the member map is sorted before
    hashing), but sensitive to exactly which coordinate each decoder claims
    and to the candidate's own status. Two rescans that find the identical
    decoders at the identical positions with the identical complete/
    partial/conflict outcome produce the identical fingerprint (so an
    Ignore doesn't re-nag); a member being added/removed, moving to a
    different coordinate, or the status changing produces a different one
    (so a materially-changed candidate becomes reviewable again even if an
    earlier version of it was ignored). Never derived from discovery order,
    IP ordering, hostname, streamname alone, or vmenable alone."""
    members = candidate.get("members") or {}
    parts = sorted(f"{pos}={m.get('device_id','')}" for pos, m in members.items())
    conflict_parts = sorted(
        f"{c.get('row')}:{c.get('column')}=" + ",".join(sorted(c.get("claimants") or []))
        for c in candidate.get("conflicts") or []
    )
    basis = (
        f"{candidate.get('rows')}x{candidate.get('columns')}|{candidate.get('status')}|"
        + ",".join(parts) + "|" + ",".join(conflict_parts)
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

def _vw_ambiguous_reason(rows: int, columns: int, wall: Dict[str, Any]) -> Optional[str]:
    """A conflict where members()+conflicting claimants together add up to
    roughly a multiple of the wall's own cell count, and most/all
    coordinates are contested, looks like TWO OR MORE physically distinct
    walls of the same size rather than one misconfigured wall -- the
    protocol has no wall ID to tell them apart (PROJECT.md). This is a
    heuristic, not a certainty: it is presented to the operator as an
    honest qualitative statement, never a numeric confidence, and it never
    causes this code to guess a split -- the manual decoder-assignment path
    (Create/Edit Wall wizard) remains the only way to actually separate
    them. Returns None when the conflict looks like an ordinary single-wall
    misconfiguration instead (e.g. one duplicated coordinate)."""
    expected = rows * columns
    conflicts = wall.get("conflicts") or []
    if not conflicts or expected <= 0:
        return None
    total_claimants = len(wall.get("members") or {})
    for c in conflicts:
        total_claimants += len(c.get("claimants") or [])
    contested_coords = len(conflicts)
    total_coords = expected
    if total_claimants >= 2 * expected and contested_coords >= max(1, total_coords // 2):
        wall_count_guess = round(total_claimants / expected) if expected else 0
        return (
            f"{total_claimants} decoders contain {rows}x{columns} Video Wall configuration "
            f"(roughly {wall_count_guess} walls' worth); LumaSuite cannot reliably determine "
            f"which decoders belong to each physical wall of this size from the data the "
            f"protocol exposes. Use Create/Edit Wall to assign each wall's decoders manually."
        )
    return None

def _vw_saved_wall_diff(saved_wall: Dict[str, Any], candidate: Dict[str, Any]) -> List[str]:
    """Plain-language diff between a saved wall's own cell assignments and
    what a freshly detected candidate (matched to it by device-ID set) now
    reports -- used for the 'known wall changed' review, never to silently
    rewrite the saved definition. Returns [] when nothing material differs."""
    diffs: List[str] = []
    saved_pos_by_decoder = {
        c.get("decoder_id"): (c.get("row"), c.get("column"))
        for c in saved_wall.get("cells", []) if c.get("decoder_id")
    }
    live_pos_by_decoder = {
        m.get("device_id"): (int(pos.split(":")[0]), int(pos.split(":")[1]))
        for pos, m in (candidate.get("members") or {}).items()
    }
    if (int(saved_wall.get("rows") or 0), int(saved_wall.get("columns") or 0)) != (candidate.get("rows"), candidate.get("columns")):
        diffs.append(
            f"Dimensions changed: saved {saved_wall.get('rows')}x{saved_wall.get('columns')}, "
            f"now {candidate.get('rows')}x{candidate.get('columns')}"
        )
    for decoder_id, saved_pos in saved_pos_by_decoder.items():
        if decoder_id not in live_pos_by_decoder:
            diffs.append(f"{decoder_id}: no longer detected at R{saved_pos[0]} C{saved_pos[1]}")
        elif live_pos_by_decoder[decoder_id] != saved_pos:
            new_pos = live_pos_by_decoder[decoder_id]
            diffs.append(f"{decoder_id}: moved from R{saved_pos[0]} C{saved_pos[1]} to R{new_pos[0]} C{new_pos[1]}")
    for decoder_id, live_pos in live_pos_by_decoder.items():
        if decoder_id not in saved_pos_by_decoder:
            diffs.append(f"{decoder_id}: newly detected at R{live_pos[0]} C{live_pos[1]} (not in saved wall)")
    if candidate.get("status") == "partial" and not diffs:
        missing = candidate.get("missing") or []
        if missing:
            diffs.append("Now incomplete: missing " + ", ".join(f"R{m['row']} C{m['column']}" for m in missing))
    return diffs

def _vw_classify_candidates(detected: Dict[str, Any], saved_walls: List[Dict[str, Any]],
                             ignored_candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Wraps each raw _vw_detect_walls() group with: a stable fingerprint,
    one of the five qualitative statuses (DETECTED / POSSIBLE / PARTIAL /
    AMBIGUOUS / CONFLICT -- never a numeric confidence score), the matching
    saved wall (if any) plus a plain-language diff when that saved wall's
    live state has materially changed, and whether the operator already
    dismissed this exact candidate. Pure function -- no device I/O, no
    persistence write."""
    ignored_fingerprints = {str(i.get("fingerprint") or "") for i in (ignored_candidates or [])}
    out = []
    for wall in detected.get("walls", []):
        member_ids = [d.get("device_id") for d in wall["members"].values()]
        device_set_fp = _vw_candidate_device_set_fingerprint(wall["rows"], wall["columns"], member_ids)
        fingerprint = _vw_candidate_fingerprint(wall)
        matched = next((
            sw for sw in saved_walls
            if int(sw.get("rows") or 0) == wall["rows"] and int(sw.get("columns") or 0) == wall["columns"]
            and {c.get("decoder_id") for c in sw.get("cells", []) if c.get("decoder_id")} == set(member_ids)
        ), None)
        known_wall_changed = None
        if matched:
            diff = _vw_saved_wall_diff(matched, wall)
            if diff:
                known_wall_changed = {"wall_id": matched["id"], "wall_name": matched["name"], "diff": diff}
        ambiguous_reason = _vw_ambiguous_reason(wall["rows"], wall["columns"], wall) if wall["status"] == "conflict" else None
        if ambiguous_reason:
            candidate_status = "AMBIGUOUS"
        elif wall["status"] == "conflict":
            candidate_status = "CONFLICT"
        elif wall["status"] == "partial":
            candidate_status = "PARTIAL"
        elif matched and not known_wall_changed:
            candidate_status = "DETECTED"
        else:
            candidate_status = "POSSIBLE"
        needs_review = bool(known_wall_changed) or candidate_status in ("POSSIBLE", "PARTIAL", "AMBIGUOUS", "CONFLICT")
        ignored = fingerprint in ignored_fingerprints and not known_wall_changed
        out.append({
            **wall,
            "fingerprint": fingerprint,
            "device_set_fingerprint": device_set_fp,
            "candidate_status": candidate_status,
            "ambiguous_reason": ambiguous_reason,
            "matched_saved_wall": {"id": matched["id"], "name": matched["name"]} if matched else None,
            "known_wall_changed": known_wall_changed,
            "ignored": ignored,
            "needs_review": needs_review and not ignored,
        })
    return out

# Short-TTL cache for the read-only detect pipeline. Video Wall page load
# already runs BOTH /api/video_wall/refresh and /api/video_wall/detect
# against every decoder (a known, already-documented double-read -- see
# PROJECT.md); adding a Device Manager badge that also wants candidate data
# would make that worse by adding a third independent poll site. Instead,
# both /api/video_wall/detect and the new lightweight
# /api/video_wall/candidates_summary share one cached result for
# _VW_DETECT_CACHE_TTL seconds, invalidated immediately by any call that
# actually changes device wall state (save/delete/import/apply/source) so
# staleness never survives an operator action. Never used to skip an
# operator-triggered read that must be current -- see _vw_cached_detect().
_VW_DETECT_CACHE: Dict[str, Any] = {"ts": 0.0, "payload": None}
_VW_DETECT_CACHE_TTL = 15.0
_vw_detect_cache_lock = threading.Lock()

def _vw_invalidate_detect_cache() -> None:
    with _vw_detect_cache_lock:
        _VW_DETECT_CACHE["ts"] = 0.0
        _VW_DETECT_CACHE["payload"] = None

def _vw_run_detect() -> Dict[str, Any]:
    """The actual device I/O: one parallel round of .Get calls per eligible
    decoder (identical transport pattern to /api/video_wall/refresh),
    classified into candidates. This is the ONLY function in the site-
    discovery feature that talks to a device."""
    snap = _vw_units_snapshot()
    decoders = snap["decoders"]
    states = []
    if decoders:
        with ThreadPoolExecutor(max_workers=min(16, len(decoders))) as ex:
            future_map = {ex.submit(_vw_read_decoder, unit): unit for unit in decoders}
            for fut in as_completed(future_map):
                unit = future_map[fut]
                try:
                    states.append(fut.result())
                except Exception as e:
                    states.append({"device_id": unit.get("id"), "online": False, "error": type(e).__name__})
    detected = _vw_detect_walls(states)
    data = _load_video_wall_state()
    saved_walls = data.get("walls", [])
    ignored = data.get("ignored_candidates", [])
    candidates = _vw_classify_candidates(detected, saved_walls, ignored)
    # Backward-compatible: existing callers/tests read matched_saved_wall
    # straight off detected["walls"] entries.
    for raw, classified in zip(detected["walls"], candidates):
        raw["matched_saved_wall"] = classified["matched_saved_wall"]
    return {"ok": True, **detected, "candidates": candidates,
            "review_count": sum(1 for c in candidates if c["needs_review"]), **snap}

def _vw_cached_detect() -> Dict[str, Any]:
    now = time.time()
    with _vw_detect_cache_lock:
        if _VW_DETECT_CACHE["payload"] is not None and (now - _VW_DETECT_CACHE["ts"]) < _VW_DETECT_CACHE_TTL:
            return copy.deepcopy(_VW_DETECT_CACHE["payload"])
    payload = _vw_run_detect()
    with _vw_detect_cache_lock:
        _VW_DETECT_CACHE["ts"] = time.time()
        _VW_DETECT_CACHE["payload"] = payload
    return copy.deepcopy(payload)


def _vw_wall_status(wall: Dict[str, Any], reports: Dict[str, Any]) -> str:
    cells = wall.get("cells") or []
    if any((c.get("source_mode") or "wall") != "wall" or _vw_cell_region(wall, c) for c in cells):
        return "Override"
    missing = [c for c in cells if c.get("decoder_id") and not reports.get(c.get("decoder_id"), {}).get("online")]
    if missing:
        return "Partial"
    diffs = []
    seen = set()
    conflict = False
    for c in cells:
        pos = (c.get("row"), c.get("column"))
        if pos in seen:
            conflict = True
        seen.add(pos)
        if c.get("decoder_id"):
            diffs.extend(_vw_compare_cell(wall, c, reports.get(c.get("decoder_id"), {})))
    if conflict:
        return "Conflict"
    if diffs:
        return "Modified"
    return "Healthy"

def _vw_cell_region(wall: Dict[str, Any], cell: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    row, col = int(cell.get("row") or 0), int(cell.get("column") or 0)
    for region in wall.get("regions") or []:
        for rc in region.get("cells") or []:
            if int(rc.get("row") or 0) == row and int(rc.get("column") or 0) == col:
                return region
    return None

def _vw_region_geometry(region: Dict[str, Any], cell: Dict[str, Any]) -> Tuple[int, int, int, int]:
    """Local rows/columns/row/column for `cell` inside `region`, computed
    from the region's own cell set -- not the whole wall's -- so a region
    is its own independent sub-wall as far as the decoder is concerned."""
    cells = region.get("cells") or []
    rows_present = sorted(set(int(c.get("row") or 0) for c in cells))
    cols_present = sorted(set(int(c.get("column") or 0) for c in cells))
    row = int(cell.get("row") or 0)
    col = int(cell.get("column") or 0)
    local_row = rows_present.index(row) + 1 if row in rows_present else 1
    local_col = cols_present.index(col) + 1 if col in cols_present else 1
    return len(rows_present) or 1, len(cols_present) or 1, local_row, local_col

def _vw_effective_matrix_and_source(wall: Dict[str, Any], cell: Dict[str, Any]) -> Tuple[int, int, int, int, bool, str]:
    """(rows, columns, row, column, video-wall-mode-enabled, source_id) for
    this cell, right now. A region -- if this cell belongs to one -- wins
    over the cell's own source_mode; that is the only precedence rule
    regions introduce. Everything else (decoder assignment, rotation,
    bezel) stays tied to the cell regardless of region membership."""
    region = _vw_cell_region(wall, cell)
    if region:
        rows, cols, row, col = _vw_region_geometry(region, cell)
        return rows, cols, row, col, True, str(region.get("source_id") or "")
    mode = cell.get("source_mode") or "wall"
    row = int(cell.get("row") or 1)
    col = int(cell.get("column") or 1)
    rows = int(wall.get("rows") or 1)
    cols = int(wall.get("columns") or 1)
    if mode == "wall":
        return rows, cols, row, col, True, str(wall.get("source_id") or "")
    return rows, cols, row, col, False, str(cell.get("override_source_id") or "")

def _vw_identity_of(device_id):
    """The canonical hardware identity of a wall cell's decoder, or '' (never an address)."""
    unit = _vw_find_unit(device_id) if device_id else None
    return luma_projection.unit_identity(unit or {})


_WALL_IDENTITY_SENTENCES = {
    "identity_mismatch": "another unit answers at this display's address",
    "identity_unconfirmed": "the display did not confirm which unit it is",
    "ambiguous_identity": "more than one address is recorded for this display",
}


def _wall_identity_refusal(unit, units=None):
    """None when the unit a wall cell names is shown to be that hardware, else the reason.

    A wall cell names a decoder by hardware identity, and the inventory turns that into an
    address. An address can be re-leased between scans, so before a wall writes to a member
    it (1) refuses an identity the inventory holds under more than one address -- which of
    them is the display is not something the cache can say, so neither first nor last is
    chosen -- and (2) asks the unit at the address for its hardware address and refuses on
    anything but an exact match. A unit with no hardware identity cannot be proven and is
    refused. The same check, the same reasons and the same helper as the restricted routes.
    """
    identity = luma_projection.unit_identity(unit or {})
    if not identity or not (unit or {}).get("ip"):
        return "identity_unconfirmed"
    held = units if units is not None else _load_units_snapshot()
    addresses = {u.get("ip") for u in held
                 if luma_projection.unit_identity(u) == identity and u.get("ip")}
    if len(addresses) > 1:
        return "ambiguous_identity"
    return luma_remote_api.confirm_identity(_identify_for_route, unit["ip"], identity)


def _vw_apply_source_cell(wall: Dict[str, Any], cell: Dict[str, Any], confirmed: bool = False) -> Dict[str, Any]:
    """Route a wall cell's source. `confirmed` means the caller has already proven who is at
    the address (the saved-wall member route does, before its first write)."""
    did = cell.get("decoder_id")
    if not did:
        return {"ok": False, "error": "display has no decoder"}
    unit = _vw_find_unit(did)
    if not unit:
        return {"ok": False, "error": _vw_cs31_reason(did) or "decoder not discovered"}
    ip = unit.get("ip")
    if unit.get("online") is False:
        # A display that is not answering is never written to, whoever asks.
        return {"ok": False, "offline": True, "ip": ip, "label": unit.get("label"),
                "error": "display is offline", "steps": []}
    if not confirmed:
        reason = _wall_identity_refusal(unit)
        if reason:
            logging.warning("[video_wall] a cell's address was not confirmed (%s); nothing was sent", reason)
            return {"ok": False, "ip": ip, "label": unit.get("label"), "reason": reason,
                    "error": _WALL_IDENTITY_SENTENCES.get(reason, reason), "steps": []}
    rows, cols, row, col, enable, source_id = _vw_effective_matrix_and_source(wall, cell)
    matrix = f"{rows}_{cols}_{row}_{col}"
    source = _vw_encoder_stream(source_id) if source_id else {"ok": False, "error": "no source selected"}
    # A plain "wall" cell (not a region, not an override) whose WALL has no
    # source_id yet is a normal, valid state -- a freshly created wall with
    # no Wall Source chosen. This must be a benign no-op, not a failure, the
    # same way it was before regions existed (a region or an override with
    # no source configured is still a hard failure, same as always -- both
    # of those only ever get a source_id from an explicit operator action,
    # so an empty one there means something is actually wrong).
    plain_wall_mode_unset = (
        enable and not source_id and not _vw_cell_region(wall, cell)
        and (cell.get("source_mode") or "wall") == "wall"
    )
    steps = []
    ok = True

    if enable:
        if source.get("ok"):
            r = _vw_set_decoder_source_if_needed(ip, source["streamname"])
            steps.append({"step": "sync wall source" if r.get("skipped") else "route wall source", **r})
            ok = ok and r.get("ok")
        elif plain_wall_mode_unset:
            steps.append({"step": "route wall source", "ok": True, "skipped": True, "detail": source.get("error")})
        else:
            steps.append({"step": "sync wall source", "ok": False, "error": source.get("error")})
            ok = False
        r = _vw_set_wall_info_if_needed(ip, True, matrix)
        steps.append({"step": "sync video wall" if r.get("skipped") else "video wall", **r})
        ok = ok and r.get("ok")
    else:
        r = _vw_set_wall_info_if_needed(ip, False, matrix)
        steps.append({"step": "sync wall override" if r.get("skipped") else "disable wall override", **r})
        ok = ok and r.get("ok")
        if source.get("ok"):
            r = _vw_set_decoder_source_if_needed(ip, source["streamname"])
            steps.append({"step": "sync override source" if r.get("skipped") else "route override source", **r})
            ok = ok and r.get("ok")
        else:
            steps.append({"step": "sync override source", "ok": False, "error": source.get("error")})
            ok = False

    return {"ok": ok, "ip": ip, "label": unit.get("label"), "steps": steps}

def _vw_apply_full_cell(wall: Dict[str, Any], cell: Dict[str, Any]) -> Dict[str, Any]:
    source_result = _vw_apply_source_cell(wall, cell)
    did = cell.get("decoder_id")
    unit = _vw_find_unit(did) if did else None
    if not unit or source_result.get("offline"):
        return source_result
    ip = unit.get("ip")
    steps = list(source_result.get("steps") or [])
    ok = bool(source_result.get("ok"))
    try:
        bezel = _vw_cell_bezel(wall, cell)
        if bezel:
            params = {
                "totalwidth": int(bezel.get("totalwidth") or 0),
                "totalheight": int(bezel.get("totalheight") or 0),
                "topborder": int(bezel.get("topborder") or 0),
                "bottomborder": int(bezel.get("bottomborder") or 0),
                "leftborder": int(bezel.get("leftborder") or 0),
                "rightborder": int(bezel.get("rightborder") or 0),
            }
            r = _vw_set_bezel_if_needed(ip, params)
            steps.append({"step": "sync bezel" if r.get("skipped") else "bezel", **r})
            ok = ok and r.get("ok")
        r = _vw_set_rotation_if_needed(ip, int(cell.get("rotation") or 0))
        steps.append({"step": "sync rotation" if r.get("skipped") else "rotation", **r})
        ok = ok and r.get("ok")
    except Exception as e:
        ok = False
        steps.append({"step": "exception", "ok": False, "error": type(e).__name__})
    # Read the wall mode back from the decoder itself: an accepted Set is not
    # a held setting, and the saved wall is not evidence of the device state.
    wall_mode = {"before": None, "after": None, "enabled_by_apply": False}
    try:
        rows, cols, row, col, enable, _src = _vw_effective_matrix_and_source(wall, cell)
        matrix = f"{rows}_{cols}_{row}_{col}"
        verify = _vw_verify_wall_mode(ip, enable, matrix)
        steps.append({"step": "verify wall mode", **verify})
        ok = ok and bool(verify.get("ok"))
        for st in steps:
            if st.get("step") in ("video wall", "sync video wall", "disable wall override", "sync wall override") and "previous_vmenable" in st:
                wall_mode["before"] = st.get("previous_vmenable")
        wall_mode["after"] = (verify.get("observed") or {}).get("enabled")
        wall_mode["enabled_by_apply"] = bool(enable and wall_mode["before"] is False and wall_mode["after"] is True)
    except Exception as e:
        ok = False
        steps.append({"step": "verify wall mode", "ok": False, "error": type(e).__name__})
    return {"ok": ok, "ip": ip, "label": unit.get("label"), "steps": steps, "wall_mode": wall_mode}

def _vw_apply_bezel_cell(wall: Dict[str, Any], cell: Dict[str, Any], use_cell_override: bool = True) -> Dict[str, Any]:
    did = cell.get("decoder_id")
    if not did:
        return {"ok": False, "error": "display has no decoder"}
    unit = _vw_find_unit(did)
    if not unit:
        return {"ok": False, "error": _vw_cs31_reason(did) or "decoder not discovered"}
    ip = unit.get("ip")
    bezel = _vw_cell_bezel(wall, cell) if use_cell_override else (wall.get("display") or {})
    params = {
        "totalwidth": int(bezel.get("totalwidth") or 0),
        "totalheight": int(bezel.get("totalheight") or 0),
        "topborder": int(bezel.get("topborder") or 0),
        "bottomborder": int(bezel.get("bottomborder") or 0),
        "leftborder": int(bezel.get("leftborder") or 0),
        "rightborder": int(bezel.get("rightborder") or 0),
    }
    r = _vw_set_bezel_if_needed(ip, params)
    return {"ok": bool(r.get("ok")), "ip": ip, "label": unit.get("label"), "steps": [{"step": "sync bezel" if r.get("skipped") else "bezel", **r}]}

def _vw_apply_rotation_cell(cell: Dict[str, Any]) -> Dict[str, Any]:
    did = cell.get("decoder_id")
    if not did:
        return {"ok": False, "error": "display has no decoder"}
    unit = _vw_find_unit(did)
    if not unit:
        return {"ok": False, "error": _vw_cs31_reason(did) or "decoder not discovered"}
    ip = unit.get("ip")
    r = _vw_set_rotation_if_needed(ip, int(cell.get("rotation") or 0))
    return {"ok": bool(r.get("ok")), "ip": ip, "label": unit.get("label"), "steps": [{"step": "sync rotation" if r.get("skipped") else "rotation", **r}]}

@APP.get("/api/video_wall/state")
def api_video_wall_state():
    data = _load_video_wall_state()
    original = json.dumps(data.get("walls", []), sort_keys=True, default=str)
    needs_header = data.get("kind") != "lumasuite.video_walls" or data.get("version") != 1
    data["walls"] = [_vw_enrich_wall(w) for w in data.get("walls", []) if isinstance(w, dict)]
    if needs_header or json.dumps(data.get("walls", []), sort_keys=True, default=str) != original:
        _save_video_wall_state(data)
    return jsonify({"ok": True, **data, **_vw_units_snapshot(), "state_file": VIDEO_WALL_STATE_FILE})

@APP.post("/api/video_wall/save")
def api_video_wall_save():
    body = luma_request.json_object()
    wall = body.get("wall") if isinstance(body.get("wall"), dict) else body
    if not isinstance(wall, dict):
        return jsonify({"ok": False, "error": "missing wall"}), 400
    # A wall that describes nothing is not a wall to save. Every real save carries a name and a
    # size; a body without them (an empty object, a stray field) used to be stored as an empty
    # "Video Wall" every time it was sent.
    def _positive_int(value):
        return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 64
    if not (isinstance(wall.get("name"), str) and wall["name"].strip()
            and _positive_int(wall.get("rows")) and _positive_int(wall.get("columns"))):
        return jsonify({"ok": False, "reason": "bad_body",
                        "error": "A video wall needs a name and its number of rows and columns."}), 400
    wall_id = wall.get("id") or f"wall-{uuid.uuid4().hex[:12]}"
    wall["id"] = wall_id
    wall["updated_at"] = int(time.time())
    wall = _vw_enrich_wall(wall)
    data = _load_video_wall_state()
    walls = [w for w in data.get("walls", []) if w.get("id") != wall_id]
    walls.append(wall)
    data["walls"] = walls
    _save_video_wall_state(data)
    return jsonify({"ok": True, "wall": wall, **_vw_units_snapshot()})

@APP.post("/api/video_wall/delete")
def api_video_wall_delete():
    body = luma_request.json_object()
    wall_id = body.get("id")
    if not wall_id:
        return jsonify({"ok": False, "error": "missing id"}), 400
    data = _load_video_wall_state()
    data["walls"] = [w for w in data.get("walls", []) if w.get("id") != wall_id]
    _save_video_wall_state(data)
    # A grant to a wall that is gone must not remain: the User loses it at once.
    _forget_missing_wall_grants()
    return jsonify({"ok": True, **data})

@APP.post("/api/video_wall/import")
def api_video_wall_import():
    body = luma_request.json_object()
    if not isinstance(body, dict) or body.get("kind") != "lumasuite.video_walls":
        return jsonify({"ok": False, "error": "import file is not a LumaSuite video wall config"}), 400
    walls = body.get("walls")
    if not isinstance(walls, list):
        return jsonify({"ok": False, "error": "video wall config must contain a walls array"}), 400
    cleaned = []
    for wall in walls:
        if not isinstance(wall, dict):
            return jsonify({"ok": False, "error": "video wall config contains an invalid wall"}), 400
        cells = wall.get("cells")
        if not isinstance(cells, list):
            return jsonify({"ok": False, "error": "video wall config contains a wall without cells"}), 400
        if not all(isinstance(cell, dict) and "row" in cell and "column" in cell for cell in cells):
            return jsonify({"ok": False, "error": "video wall config contains invalid display cells"}), 400
        wall = _vw_enrich_wall(wall)
        wall["id"] = wall.get("id") or f"wall-{uuid.uuid4().hex[:12]}"
        wall["updated_at"] = int(time.time())
        cleaned.append(wall)
    # Import replaces the walls list wholesale (pre-existing behavior) but
    # preserves the separate local ignored-candidates dismissal list --
    # that's operator bookkeeping about THIS installation's live decoders,
    # not part of the portable wall config being imported.
    data = {"walls": cleaned, "ignored_candidates": _load_video_wall_state().get("ignored_candidates", [])}
    _save_video_wall_state(data)
    _forget_missing_wall_grants()
    return jsonify({"ok": True, **data, **_vw_units_snapshot(), "state_file": VIDEO_WALL_STATE_FILE})

@APP.post("/api/video_wall/refresh")
def api_video_wall_refresh():
    body = luma_request.json_object()
    wall_id = body.get("id")
    data = _load_video_wall_state()
    walls = data.get("walls", [])
    wall = next((w for w in walls if w.get("id") == wall_id), None) if wall_id else None
    snap = _vw_units_snapshot()
    target_ids = set()
    if wall:
        target_ids = {c.get("decoder_id") for c in wall.get("cells", []) if c.get("decoder_id")}
    else:
        target_ids = {d.get("id") for d in snap["decoders"]}
    reports = {}
    target_ids = [did for did in target_ids if did]
    if target_ids:
        with ThreadPoolExecutor(max_workers=min(16, len(target_ids))) as ex:
            future_map = {}
            for did in target_ids:
                unit = _vw_find_unit(did)
                if unit:
                    future_map[ex.submit(_vw_read_decoder, unit)] = did
                else:
                    reports[did] = {"device_id": did, "online": False, "error": _vw_cs31_reason(did) or "not discovered"}
            for fut in as_completed(future_map):
                did = future_map[fut]
                try:
                    reports[did] = fut.result()
                except Exception as e:
                    reports[did] = {"device_id": did, "online": False, "error": str(e)}
    status = _vw_wall_status(wall, reports) if wall else None
    return jsonify({"ok": True, "wall": wall, "reports": reports, "status": status, **snap})

@APP.get("/api/video_wall/detect")
def api_video_wall_detect():
    """Read-only reconstruction of whatever Video Wall configuration
    already exists on the discovered decoders, extended into a
    site-discovery feature (see PROJECT.md, "Video Wall: site discovery /
    existing-configuration candidates"). Every call this makes is a .Get --
    see _vw_read_decoder -- so opening or refreshing the Video Wall page,
    or Device Manager's badge, can call this freely without writing
    anything to a device or to video_walls.json. The operator decides,
    separately, whether a detected wall becomes a saved LumaSuite
    configuration (see api_video_wall_save / "Confirm & Save").

    Backed by a short-TTL cache (_vw_cached_detect) shared with
    /api/video_wall/candidates_summary so opening Device Manager and the
    Video Wall page close together doesn't multiply device reads; pass
    ?fresh=1 to force a live read (used by an operator-triggered refresh).
    """
    if request.args.get("fresh"):
        _vw_invalidate_detect_cache()
    payload = _vw_cached_detect()
    return jsonify(payload)

@APP.get("/api/video_wall/candidates_summary")
def api_video_wall_candidates_summary():
    """Lightweight read for Device Manager's notification badge -- reuses
    the exact same cached, read-only detection pipeline as
    /api/video_wall/detect (never a second scanner), but returns only
    counts so a page that isn't showing the Video Wall canvas doesn't need
    the full candidate/member payload."""
    payload = _vw_cached_detect()
    by_status: Dict[str, int] = {}
    for c in payload.get("candidates", []):
        if c["needs_review"]:
            by_status[c["candidate_status"]] = by_status.get(c["candidate_status"], 0) + 1
    return jsonify({"ok": True, "review_count": payload.get("review_count", 0), "by_status": by_status})

@APP.post("/api/video_wall/candidates/ignore")
def api_video_wall_candidates_ignore():
    """Persist that the operator dismissed a specific detected candidate,
    identified by its content-derived fingerprint (see
    _vw_candidate_fingerprint) -- purely local bookkeeping in
    video_walls.json's ignored_candidates list, NEVER a device write. A
    later rescan that finds the identical candidate (same fingerprint)
    stays quiet; any material change produces a different fingerprint and
    the candidate becomes reviewable again automatically."""
    body = luma_request.json_object()
    fingerprint = str(body.get("fingerprint") or "")
    if not fingerprint:
        return jsonify({"ok": False, "error": "missing fingerprint"}), 400
    data = _load_video_wall_state()
    ignored = [i for i in data.get("ignored_candidates", []) if i.get("fingerprint") != fingerprint]
    ignored.append({
        "fingerprint": fingerprint,
        "rows": _vw_int(body.get("rows"), 0),
        "columns": _vw_int(body.get("columns"), 0),
        "member_count": _vw_int(body.get("member_count"), 0),
        "status": str(body.get("status") or ""),
        "ignored_at": int(time.time()),
    })
    data["ignored_candidates"] = ignored
    _save_video_wall_state(data)
    return jsonify({"ok": True, "ignored_candidates": ignored})

@APP.post("/api/video_wall/source")
def api_video_wall_source():
    body = luma_request.json_object()
    wall = body.get("wall")
    if not isinstance(wall, dict):
        return jsonify({"ok": False, "error": "missing wall"}), 400
    scope = body.get("scope") or "cell"
    cells = wall.get("cells") or []
    if scope == "wall":
        # A cell that belongs to a region is no longer a "whole wall" cell in
        # any functional sense -- _vw_effective_matrix_and_source() would
        # apply the region's source to it regardless, so excluding it here
        # keeps the toolbar's wall-wide sync from looking like it is
        # targeting cells it cannot actually change.
        targets = [
            c for c in cells
            if c.get("decoder_id") and (c.get("source_mode") or "wall") == "wall"
            and not _vw_cell_region(wall, c)
        ]
    elif scope == "region":
        region_id = str(body.get("region_id") or "")
        region = next((r for r in wall.get("regions") or [] if r.get("id") == region_id), None)
        if not region:
            return jsonify({"ok": False, "error": "region not found"}), 400
        wanted = {(rc.get("row"), rc.get("column")) for rc in region.get("cells") or []}
        targets = [c for c in cells if c.get("decoder_id") and (c.get("row"), c.get("column")) in wanted]
    else:
        row = int(body.get("row") or 0)
        column = int(body.get("column") or 0)
        targets = [c for c in cells if c.get("decoder_id") and int(c.get("row") or 0) == row and int(c.get("column") or 0) == column]
    if not targets:
        return jsonify({"ok": False, "error": "no assigned displays to route"}), 400

    # What is about to be written to, by hardware identity, before anything is. No address,
    # no credential.
    intended = {c.get("decoder_id"): _vw_identity_of(c.get("decoder_id")) for c in targets}
    logging.info("[ROUTE-INTENT] rule=/api/video_wall/source scope=%s wall=%s source=%s members=%s",
                 scope, wall.get("id") or "-", wall.get("source_id") or "-",
                 ",".join(sorted(v or "unknown" for v in intended.values())))
    results = {}
    with ThreadPoolExecutor(max_workers=min(16, len(targets))) as ex:
        future_map = {ex.submit(_vw_apply_source_cell, wall, cell): cell for cell in targets}
        for fut in as_completed(future_map):
            cell = future_map[fut]
            did = cell.get("decoder_id")
            try:
                results[did] = fut.result()
            except Exception as e:
                results[did] = {"ok": False, "error": str(e)}
    success_count = sum(1 for r in results.values() if r.get("ok"))
    overall = bool(results) and success_count == len(results)
    # Source routing changes vwmatrix/vmenable/streamname on the device --
    # exactly the fields candidate detection reads -- so the shared detect
    # cache must not serve a stale pre-route snapshot after this.
    _vw_invalidate_detect_cache()
    return jsonify({"ok": overall, "results": results, "target_count": len(targets), "success_count": success_count})

@APP.post("/api/video_wall/bezel")
def api_video_wall_bezel():
    body = luma_request.json_object()
    wall = body.get("wall")
    if not isinstance(wall, dict):
        return jsonify({"ok": False, "error": "missing wall"}), 400
    scope = body.get("scope") or "wall"
    cells = wall.get("cells") or []
    if scope == "cell":
        row = int(body.get("row") or 0)
        column = int(body.get("column") or 0)
        targets = [c for c in cells if c.get("decoder_id") and int(c.get("row") or 0) == row and int(c.get("column") or 0) == column]
    else:
        targets = [c for c in cells if c.get("decoder_id")]
    if not targets:
        return jsonify({"ok": False, "error": "no assigned displays to update"}), 400

    results = {}
    with ThreadPoolExecutor(max_workers=min(16, len(targets))) as ex:
        use_cell_override = scope == "cell"
        future_map = {ex.submit(_vw_apply_bezel_cell, wall, cell, use_cell_override): cell for cell in targets}
        for fut in as_completed(future_map):
            cell = future_map[fut]
            did = cell.get("decoder_id")
            try:
                results[did] = fut.result()
            except Exception as e:
                results[did] = {"ok": False, "error": str(e)}
    success_count = sum(1 for r in results.values() if r.get("ok"))
    overall = bool(results) and success_count == len(results)
    return jsonify({"ok": overall, "results": results, "target_count": len(targets), "success_count": success_count})

@APP.post("/api/video_wall/rotation")
def api_video_wall_rotation():
    body = luma_request.json_object()
    wall = body.get("wall")
    if not isinstance(wall, dict):
        return jsonify({"ok": False, "error": "missing wall"}), 400
    row = int(body.get("row") or 0)
    column = int(body.get("column") or 0)
    targets = [
        c for c in (wall.get("cells") or [])
        if c.get("decoder_id") and int(c.get("row") or 0) == row and int(c.get("column") or 0) == column
    ]
    if not targets:
        return jsonify({"ok": False, "error": "no assigned display to update"}), 400

    results = {}
    for cell in targets:
        did = cell.get("decoder_id")
        try:
            results[did] = _vw_apply_rotation_cell(cell)
        except Exception as e:
            results[did] = {"ok": False, "error": str(e)}
    success_count = sum(1 for r in results.values() if r.get("ok"))
    overall = bool(results) and success_count == len(results)
    return jsonify({"ok": overall, "results": results, "target_count": len(targets), "success_count": success_count})

@APP.post("/api/video_wall/decoder_mode")
def api_video_wall_decoder_mode():
    """Read, or set, one decoder's Video Wall mode (vmenable) on the device.

    Body: {"decoder_id": "...", "enabled": true|false (optional), "wall_id": "..." (optional)}.
    Without `enabled` this only reads. With it, the write is made through the
    existing StandardVideoWallInfo path and read back; success is claimed only
    when the decoder itself reports the requested state. Wall mode is a device
    state and is never inferred from saved wall membership."""
    body = luma_request.json_object()
    decoder_id = str(body.get("decoder_id") or "").strip()
    if not decoder_id:
        return jsonify({"ok": False, "reason": "bad_request", "error": "decoder_id is required"}), 400
    if "enabled" in body and not isinstance(body.get("enabled"), bool):
        return jsonify({"ok": False, "reason": "bad_request", "error": "enabled must be true or false"}), 400
    reason = _vw_cs31_reason(decoder_id)
    if reason:
        return jsonify({"ok": False, "reason": "not_eligible", "error": reason}), 409
    unit = _vw_find_unit(decoder_id)
    if not unit or not unit.get("ip"):
        return jsonify({"ok": False, "reason": "unknown_decoder", "error": "decoder not discovered"}), 404
    ip = unit["ip"]
    label = unit.get("label")
    current = _vw_read_wall_mode(ip)
    cur_view = ({"enabled": current["enabled"], "vwmatrix": current.get("vwmatrix")} if current.get("ok") else None)
    if "enabled" not in body:
        status = 200 if current.get("ok") else 502
        return jsonify({"ok": bool(current.get("ok")), "label": label, "wall_mode": cur_view,
                        "error": None if current.get("ok") else current.get("error")}), status
    if unit.get("online") is False:
        return jsonify({"ok": False, "reason": "unit_offline", "label": label, "wall_mode": None,
                        "error": "display is offline"}), 409
    if not current.get("ok"):
        # Cannot tell what the decoder holds: refuse rather than write blind.
        return jsonify({"ok": False, "reason": "unreadable", "label": label, "wall_mode": None,
                        "error": f"wall mode could not be read first: {current.get('error')}"}), 502
    enabled = bool(body["enabled"])
    matrix = current.get("vwmatrix")
    wall_id = str(body.get("wall_id") or "").strip()
    if enabled and wall_id:
        wall = next((w for w in _load_video_wall_state().get("walls", []) if w.get("id") == wall_id), None)
        cell = next((c for c in (wall or {}).get("cells", []) if c.get("decoder_id") == decoder_id), None)
        if wall and cell:
            rows, cols, row, col, _en, _src = _vw_effective_matrix_and_source(wall, cell)
            matrix = f"{rows}_{cols}_{row}_{col}"
    if enabled and _vw_parse_matrix(matrix).get("rows") is None:
        return jsonify({"ok": False, "reason": "no_position", "label": label, "wall_mode": cur_view,
                        "error": "decoder has no wall position to enable; assign it in a wall and Apply"}), 409
    write = _vw_set_wall_info_if_needed(ip, enabled, matrix)
    verify = _vw_verify_wall_mode(ip, enabled, matrix if enabled else None)
    _vw_invalidate_detect_cache()
    logging.info("[video_wall] decoder wall mode set requested=%s write_ok=%s verified=%s", enabled, bool(write.get("ok")), bool(verify.get("ok")))
    if verify.get("ok"):
        return jsonify({"ok": True, "label": label, "wall_mode": verify["observed"], "previous": cur_view})
    return jsonify({"ok": False, "reason": "not_verified", "label": label, "previous": cur_view,
                    "wall_mode": verify.get("observed"),
                    "error": verify.get("error") or write.get("error") or "decoder did not hold the requested wall mode"}), 502

@APP.post("/api/video_wall/apply")
def api_video_wall_apply():
    body = luma_request.json_object()
    wall = body.get("wall")
    wall_id = body.get("id")
    if not wall and wall_id:
        wall = next((w for w in _load_video_wall_state().get("walls", []) if w.get("id") == wall_id), None)
    if not isinstance(wall, dict):
        return jsonify({"ok": False, "error": "missing wall"}), 400

    targets = [cell for cell in wall.get("cells", []) if cell.get("decoder_id")]
    results = {}
    if targets:
        with ThreadPoolExecutor(max_workers=min(16, len(targets))) as ex:
            future_map = {ex.submit(_vw_apply_full_cell, wall, cell): cell for cell in targets}
            for fut in as_completed(future_map):
                cell = future_map[fut]
                did = cell.get("decoder_id")
                try:
                    results[did] = fut.result()
                except Exception as e:
                    results[did] = {"ok": False, "error": str(e)}

    overall = bool(results) and all(r.get("ok") for r in results.values())
    _vw_invalidate_detect_cache()
    return jsonify({"ok": overall, "results": results, **_vw_apply_summary(results)})
# ---- HARDEN: skip duplicate endpoint registrations (prevents AssertionError) ----
import types as _types
_orig_add_url_rule = APP.add_url_rule
def _safe_add_url_rule(self, rule, endpoint=None, view_func=None, **options):
    # If endpoint already registered, quietly skip to avoid collisions
    if endpoint and endpoint in self.view_functions:
        self.logger.info(f"[guard] skip duplicate endpoint: {endpoint} ({rule})")
        return
    return _orig_add_url_rule(rule, endpoint=endpoint, view_func=view_func, **options)
APP.add_url_rule = _types.MethodType(_safe_add_url_rule, APP)
# ---------
import producer2_routes
from producer2_routes import producer2_bp
APP.register_blueprint(producer2_bp)

# --- CSV Export Endpoint for Device Manager ---
@APP.post("/api/set_dante_power")
def api_set_dante_power():
    try:
        data = luma_request.json_object()
        ip = data.get("ip", "").strip()
        enabled = bool(data.get("enabled", False))
        if not ip:
            return jsonify({"ok": False, "error": "missing ip"}), 400
        
        params = {"dante": enabled, "password": CONFIG.get("password", "password")}
        r = _ws_call_auth(ip, "SystemFuncDantePowerStatus.Set", params, CONFIG.get("TIMEOUT", 3.0))
        if not r or "result" not in r:
            return jsonify({"ok": False, "error": "command failed"}), 500
        
        result_enabled = bool(r["result"].get("dante", False))
        
        # Update cache
        with cache_lock:
            if ip in cache_units:
                cache_units[ip]["dante_enabled"] = result_enabled
        _persist_units_cache()
        
        return jsonify({"ok": True, "dante_enabled": result_enabled})
    except Exception as e:
        logging.error(f"[set_dante_power] Exception: {e}")
        return luma_request.internal_error(e)

@APP.post("/api/set_hdcp")
def api_set_hdcp():
    try:
        data = luma_request.json_object()
        ip = data.get("ip", "").strip()
        hdcp_version = data.get("hdcp", "").strip()
        role = data.get("role", "").strip().lower()
        
        logging.info(f"[set_hdcp] Request: ip={ip}, hdcp={hdcp_version}, role={role}")
        
        if not ip or not hdcp_version:
            return jsonify({"ok": False, "error": "missing ip or hdcp"}), 400
        
        result_hdcp = hdcp_version
        if role == "encoder" or "encoder" in role:
            params = {"source": "in1", "hdcpversion": hdcp_version, "password": CONFIG.get("password", "password")}
            r = _ws_call_auth(ip, "HdcpAnnouncement.Set", params, CONFIG.get("TIMEOUT", 3.0))
            if r and "result" in r:
                result_hdcp = r["result"].get("hdcpversion", hdcp_version)
        elif role == "decoder" or "decoder" in role:
            params = {"hdcpversion": hdcp_version, "password": CONFIG.get("password", "password")}
            r = _ws_call_auth(ip, "OutHdcpAnnouncement.Set", params, CONFIG.get("TIMEOUT", 3.0))
            if r and "result" in r:
                result_hdcp = r["result"].get("hdcpversion", hdcp_version)
        else:
            logging.warning(f"[set_hdcp] Unknown role: {role}, proceeding anyway")
        
        # Update cache
        with cache_lock:
            if ip in cache_units:
                cache_units[ip]["hdcp"] = result_hdcp
        _persist_units_cache()
        
        return jsonify({"ok": True, "hdcp": result_hdcp})
    except Exception as e:
        import traceback
        logging.error(f"[set_hdcp] Exception: {e}\n{traceback.format_exc()}")
        return luma_request.internal_error(e)

@APP.post("/api/export_csv")
def api_export_csv():
    try:
        with cache_lock:
            units = list(cache_units.values())
        if not units:
            return Response("No units to export", status=400)
        output = StringIO()
        fieldnames = [
            "ip", "serialnumber", "mac", "hostname", "dante_name", "dante_ip", "dante_mac", "type", "model", "version", "status"
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore', quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for u in units:
            # Prepend tab to serialnumber to force Excel to treat as text
            serial = u.get("serialnumber", "")
            if serial and serial.strip():
                serial = "\t" + serial
            row = {
                "ip": u.get("ip", ""),
                "serialnumber": serial,
                "mac": u.get("mac", ""),
                "hostname": u.get("hostname", ""),
                "dante_name": u.get("dante_name", ""),
                "dante_ip": u.get("dante_ip", ""),
                "dante_mac": u.get("dante_mac", ""),
                "type": u.get("type", u.get("role", "")),
                "model": u.get("model", ""),
                "version": u.get("version", ""),
                "status": u.get("status", "")
            }
            writer.writerow(row)
        csv_data = output.getvalue()
        output.close()
        filename = f"units_{dt.now().strftime('%Y%m%d_%H%M%S')}.csv"
        resp = Response(csv_data, mimetype="text/csv")
        resp.headers["Content-Disposition"] = f"attachment; filename=\"{filename}\""
        return resp
    except Exception as e:
        import traceback
        logging.error(f"[export_csv] Exception: {e}\n{traceback.format_exc()}")
        return luma_request.internal_error(e)
PORT = int(os.environ.get("LUMA_PORT","8089"))

IS_FROZEN = getattr(sys, "frozen", False)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _default_data_dir() -> str:
    override = (os.environ.get("LUMA_DATA_DIR") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    system = platform.system().lower()
    if system == "windows":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "LumaSuite")
    if system == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "LumaSuite")
    return os.path.join(os.path.expanduser("~"), ".lumasuite")

def _legacy_data_dirs() -> List[str]:
    candidates = []
    if IS_FROZEN:
        candidates.append(os.path.dirname(sys.executable))
    candidates.append(BASE_DIR)
    out = []
    seen = set()
    for path in candidates:
        abs_path = os.path.abspath(path)
        key = abs_path.lower() if os.name == "nt" else abs_path
        if key in seen:
            continue
        seen.add(key)
        out.append(abs_path)
    return out

DATA_DIR = _default_data_dir()
os.makedirs(DATA_DIR, exist_ok=True)

UI_DIR = os.path.join(BASE_DIR, "ui")
FIRMWARE_DIR = os.path.join(BASE_DIR, "firmware")
CACHE_FILE = os.path.join(DATA_DIR, "units_cache.json")
VIDEO_WALL_STATE_FILE = os.path.join(DATA_DIR, "video_walls.json")
CONFIG_FILE = os.path.join(DATA_DIR, "producer_config.json")
PRODUCER_STATE_FILE = os.path.join(DATA_DIR, "producer_state.json")
PRODUCER2_STATE_FILE = os.path.join(DATA_DIR, "producer2_state.json")
# in-memory cache structures (needed before autoload)
cache_units: Dict[str, Dict[str, Any]] = {}
cache_lock = threading.Lock()
cache_generation = 0

_RUNTIME_JSON_DEFAULTS = {
    "units_cache.json": [],
    "video_walls.json": {
        "kind": "lumasuite.video_walls",
        "version": 1,
        "walls": []
    },
    "producer_config.json": None,
    "producer_state.json": {"ticker": {"dir": "rtl", "rows": []}, "streams": []},
    "producer2_state.json": {"streams": [], "ticker": {}},
}

def _migrate_runtime_json_files():
    """Copy legacy runtime JSON into the user data folder without discarding existing state."""
    for filename in _RUNTIME_JSON_DEFAULTS:
        dest = os.path.join(DATA_DIR, filename)
        for legacy_dir in _legacy_data_dirs():
            src = os.path.join(legacy_dir, filename)
            if os.path.abspath(src) == os.path.abspath(dest) or not os.path.exists(src):
                continue
            if os.path.exists(dest):
                try:
                    if os.path.getmtime(src) <= os.path.getmtime(dest):
                        continue
                    backup = f"{dest}.bak-{time.strftime('%Y%m%d%H%M%S')}"
                    shutil.copy2(dest, backup)
                    logging.info(f"[startup] backed up existing {filename} to {backup}")
                except Exception as e:
                    logging.warning(f"[startup] skipping migration for {filename}; could not compare or back up: {e}")
                    continue
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
                logging.info(f"[startup] migrated {filename} from {src} to {dest}")
                break
            except Exception as e:
                logging.warning(f"[startup] failed to migrate {src} to {dest}: {e}")

def _ensure_json_file(path: str, default: Any):
    """Create a state file if it is missing; leave existing files untouched."""
    if os.path.exists(path):
        return
    try:
        if not luma_store.atomic_write_json(path, default):
            raise OSError("could not write")
        logging.info(f"[startup] created missing state file -> {path}")
    except Exception as e:
        logging.warning(f"[startup] failed to create {path}: {e}")

_migrate_runtime_json_files()
_ensure_json_file(CACHE_FILE, [])
_ensure_json_file(VIDEO_WALL_STATE_FILE, {
    "kind": "lumasuite.video_walls",
    "version": 1,
    "walls": []
})
_ensure_json_file(PRODUCER_STATE_FILE, {"ticker": {"dir": "rtl", "rows": []}, "streams": []})
_ensure_json_file(PRODUCER2_STATE_FILE, {"streams": [], "ticker": {}})

def _units_by_address(rows):
    """Persisted rows as {address: row}, one row per hardware identity.

    The file is written in the cache's own order, and a unit that moved is appended at its new
    address, so a later row is the newer observation. Rows are therefore taken newest first and
    an identity already taken is not taken again: a stale persisted row for a unit that has
    since moved cannot survive a restart and give one identity two addresses. A row with no
    hardware identity is kept as it always was (by address, the later one winning), and the
    file's order is preserved.
    """
    taken_addresses, taken_identities, kept = set(), set(), []
    for row in reversed(list(rows or [])):
        if not isinstance(row, dict) or "ip" not in row:
            continue
        address = row["ip"]
        if address in taken_addresses:
            continue
        identity = luma_projection.unit_identity(row)
        if identity and identity in taken_identities:
            continue
        taken_addresses.add(address)
        if identity:
            taken_identities.add(identity)
        kept.append(row)
    return {row["ip"]: row for row in reversed(kept)}


# --- baseline cache autoload (dual JSON support) ---
if os.path.exists(CACHE_FILE):
    try:
        data = luma_store.read_json(CACHE_FILE, default=None)
        # Handle both array format and {"units": [...]} format
        if isinstance(data, list):
            cache_units.update(_units_by_address(data))
        elif isinstance(data, dict) and "units" in data:
            cache_units.update(_units_by_address(data["units"]))
        logging.info(f"[startup] loaded {len(cache_units)} units from {CACHE_FILE}")
    except Exception as e:
        logging.warning(f"[startup] failed to load cache: {e}")
# --- end baseline cache autoload ---


CONFIG: Dict[str, Any] = {
    "username": "admin",
    "password": "password",
    "fallback_password": "",  # Primary/Fallback device passwords -- see Part 4
    "http_scheme": "http",
    "http_port": 80,
    "http_verify_tls": False,
    "login_path": "/api/v1/auth/login",
    "upload_path": "/api/v1/fw_upload",
    "upload_field": "FIREWARE_FILE",
    "extra_headers": {},
    "ws_paths": ["/wsapp","/wsapp/","/ws","/ws/","/wsapi"],
    "TIMEOUT": 3.0,
    "ws_port": 80,
    "ws_include_password": True,  # include password param on WS calls when True
    # Switcher upload tuning keys
    "UPLOAD_CHUNK_SIZE": 256*1024,          # bytes per raw socket chunk
    "UPLOAD_LOG_INTERVAL": 1024*1024,       # progress log interval
    "SWITCHER_FORCE_CHUNKED": False,        # if True use HTTP chunked framing for raw attempts
    "SWITCHER_SOCKET_PATH": "/upload",    # semantic path label for socket mode
    # Asset paths (can be changed from producer config panel)
    # Defaults point to C:\ until user sets them
    "logos_path": "c:\\",
    "imagestream_path": "c:\\",
    "firmware_path": "c:\\",
}

SCAN_WORKERS = int(os.environ.get("LUMA_SCAN_WORKERS", "64"))
SCAN_TIMEOUT = float(os.environ.get("LUMA_SCAN_TIMEOUT", "1.2"))
TCP_TIMEOUT_MS = int(os.environ.get("LUMA_TCP_TIMEOUT_MS", "400"))

# Set to DEBUG to see detailed WebSocket connection attempts
logging.basicConfig(level=logging.DEBUG, format="[%(asctime)s] %(levelname)s - %(message)s", force=True)
# Force unbuffered logging
import sys
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None
sys.stderr.reconfigure(line_buffering=True) if hasattr(sys.stderr, 'reconfigure') else None

_NO_WINDOW_SUBPROCESS_KWARGS: Dict[str, Any] = {}
if os.name == "nt":
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        _NO_WINDOW_SUBPROCESS_KWARGS = {
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
            "startupinfo": startupinfo,
        }
    except Exception:
        _NO_WINDOW_SUBPROCESS_KWARGS = {}


def _subprocess_run_no_window(cmd, **kwargs):
    return subprocess.run(cmd, **kwargs, **_NO_WINDOW_SUBPROCESS_KWARGS)


def _subprocess_check_output_no_window(cmd, **kwargs):
    return subprocess.check_output(cmd, **kwargs, **_NO_WINDOW_SUBPROCESS_KWARGS)

def _load_persisted_config():
    try:
        if os.path.exists(CONFIG_FILE):
            data = luma_store.read_json(CONFIG_FILE, default=None)
            if isinstance(data, dict):
                for k in ("logos_path","imagestream_path","firmware_path"):
                    v = data.get(k)
                    if isinstance(v, str) and v.strip():
                        # store absolute path for consistency
                        CONFIG[k] = os.path.abspath(v)
                # Optional persisted creds: use if present; else keep defaults
                u = data.get("username")
                p = data.get("password")
                fp = data.get("fallback_password")
                if isinstance(u, str) and u.strip():
                    CONFIG["username"] = u.strip()
                if isinstance(p, str) and p.strip():
                    CONFIG["password"] = p.strip()
                if isinstance(fp, str):
                    CONFIG["fallback_password"] = fp.strip()
                # Additional (routed) networks the operator keeps; a malformed
                # saved value is dropped rather than scanned.
                try:
                    CONFIG["scan_ranges"] = luma_scanplan.normalise(data.get("scan_ranges"))
                except luma_scanplan.ScanRangeError:
                    CONFIG["scan_ranges"] = []
                logging.info(f"[config] loaded persisted config from {CONFIG_FILE}")
    except Exception as e:
        logging.warning(f"[config] failed to load persisted config: {e}")

def _save_persisted_config():
    try:
        to_write = {k: CONFIG.get(k) for k in ("logos_path","imagestream_path","firmware_path")}
        # Also persist username/password if user customized them (non-empty strings)
        if isinstance(CONFIG.get("username"), str) and CONFIG.get("username").strip():
            to_write["username"] = CONFIG.get("username").strip()
        if isinstance(CONFIG.get("password"), str) and CONFIG.get("password").strip():
            to_write["password"] = CONFIG.get("password").strip()
        # Fallback password is allowed to be persisted as empty (means "not
        # configured" / disabled), unlike the primary password above.
        if isinstance(CONFIG.get("fallback_password"), str):
            to_write["fallback_password"] = CONFIG.get("fallback_password").strip()
        if CONFIG.get("scan_ranges"):
            to_write["scan_ranges"] = list(CONFIG["scan_ranges"])
        # Holds the device credentials: written atomically and narrowed to its owner.
        if not luma_store.atomic_write_json(CONFIG_FILE, to_write, private=True):
            raise OSError("config could not be written")
        logging.info(f"[config] persisted config -> {CONFIG_FILE}")
    except Exception as e:
        logging.warning(f"[config] failed to persist config: {e}")

# Attempt to load any persisted config on startup
_ensure_json_file(CONFIG_FILE, {k: CONFIG.get(k) for k in ("logos_path","imagestream_path","firmware_path")})
_load_persisted_config()
# Export CONFIG into Flask app config so blueprints can read it
APP.config['LUMA_CONFIG'] = CONFIG





def _is_default_password_in_use() -> bool:
    return str(CONFIG.get("password") or "") == "password"


def _is_same_origin_request() -> bool:
    """Allow requests without Origin (CLI/tools), but block browser cross-origin writes."""
    origin = (request.headers.get("Origin") or "").strip()
    if not origin:
        return True
    expected = f"{request.scheme}://{request.host}"
    return origin.lower() == expected.lower()



# Only enforce password rules when setting/syncing passwords, not for upgrades or other actions
@APP.before_request
def enforce_request_security_guards():
    if request.method == "OPTIONS":
        return None

    if request.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if not _is_same_origin_request():
            return jsonify({"ok": False, "error": "cross_origin_blocked"}), 403

    # No default password enforcement for any endpoint except password sync/set
    return None


def _firmware_search_roots() -> List[str]:
    roots: List[str] = []
    cfg_root = CONFIG.get("firmware_path")
    if isinstance(cfg_root, str) and cfg_root.strip():
        roots.append(os.path.abspath(cfg_root.strip()))
    roots.append(os.path.abspath(FIRMWARE_DIR))
    roots.append(os.path.abspath(os.path.join(DATA_DIR, "firmware")))

    deduped: List[str] = []
    seen = set()
    for r in roots:
        key = r.lower() if os.name == "nt" else r
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


def _resolve_firmware_path(firmware: str) -> Tuple[Optional[str], List[str]]:
    checked: List[str] = []
    if not firmware:
        return None, checked

    if os.path.isabs(firmware):
        abs_path = os.path.abspath(firmware)
        checked.append(abs_path)
        return (abs_path if os.path.isfile(abs_path) else None), checked

    for root in _firmware_search_roots():
        candidate = os.path.abspath(os.path.join(root, firmware))
        checked.append(candidate)
        if os.path.isfile(candidate):
            return candidate, checked

    return None, checked

# --- Tuning config endpoint for switcher uploads ---
@APP.get("/api/switcher_tune", endpoint="switcher_tune_get")
def api_switcher_tune_get():
    keys = ["UPLOAD_CHUNK_SIZE","UPLOAD_LOG_INTERVAL","SWITCHER_FORCE_CHUNKED","SWITCHER_SOCKET_PATH"]
    return {k: CONFIG.get(k) for k in keys}

@APP.post("/api/switcher_tune", endpoint="switcher_tune_post")
def api_switcher_tune_post():
    data = luma_request.json_object()
    changed = {}
    for k in ["UPLOAD_CHUNK_SIZE","UPLOAD_LOG_INTERVAL"]:
        if k in data:
            try:
                v = int(data[k])
                if v > 0:
                    CONFIG[k] = v
                    changed[k] = v
            except Exception:
                pass
    for k in ["SWITCHER_FORCE_CHUNKED"]:
        if k in data:
            CONFIG[k] = bool(data[k])
            changed[k] = CONFIG[k]
    if "SWITCHER_SOCKET_PATH" in data:
        v = str(data["SWITCHER_SOCKET_PATH"]).strip() or CONFIG["SWITCHER_SOCKET_PATH"]
        CONFIG["SWITCHER_SOCKET_PATH"] = v
        changed["SWITCHER_SOCKET_PATH"] = v
    return {"ok": True, "changed": changed, "current": {k: CONFIG.get(k) for k in ["UPLOAD_CHUNK_SIZE","UPLOAD_LOG_INTERVAL","SWITCHER_FORCE_CHUNKED","SWITCHER_SOCKET_PATH"]}}

# ---------------- TCP / WS helpers ----------------

def _tcp_probe(ip, port, timeout_ms=TCP_TIMEOUT_MS):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout_ms/1000.0)
    try:
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass

def _ws_url(ip: str, path: str) -> str:
    port = int(CONFIG.get("ws_port") or 80)
    scheme = "wss" if port == 443 else "ws"
    host = f"{ip}:{port}"
    if not path.startswith('/'):
        path = "/" + path
    return f"{scheme}://{host}{path}"

def _ws_call(ip: str, payload: Dict[str, Any], timeout: float) -> Optional[Dict[str, Any]]:
    """
    Call a device via WebSocket. Opens new connection, sends payload, reads response.
    If device echoes back wrong ID, retry reading until we get the right one (max 5 attempts).
    """
    # A unit that does not answer at all -- no connection, a refused one, or an upgrade that is never
    # replied to -- will not answer on the next path either. Only a unit that *replies* with a
    # rejection is worth asking on another path, so silence ends the call, and the whole call is
    # bounded: five paths at the full timeout turned one unreachable unit into 5x the timeout
    # (3.5 s per read when polling, 15 s per read when scanning).
    silent = False
    deadline = time.monotonic() + max(timeout * 2.0, timeout + 0.5)
    for p in CONFIG["ws_paths"]:
        if silent or time.monotonic() >= deadline:
            break
        url = _ws_url(ip, p)
        ws = None
        try:
            ws = create_connection(url, timeout=timeout, redirect_limit=0)
            ws.settimeout(timeout)

            request_id = payload.get("id")
            payload_str = json.dumps(payload)
            logging.debug(f"[ws_call] Sending to {url}: method={payload.get('method')}, id={request_id}")
            
            ws.send(payload_str)
            
            # Try to read responses until we get one that matches our request ID
            # or we get a valid response without an ID
            max_read_attempts = 5
            for attempt in range(max_read_attempts):
                try:
                    raw = ws.recv()
                    logging.debug("[ws_call] response received (attempt %d, %d characters)", attempt + 1, len(raw or ""))
                    
                    # Parse response
                    try:
                        data = json.loads(raw)
                    except Exception as e:
                        logging.warning("[ws_call] Invalid JSON from %s (%d characters)", ip, len(raw or ""))
                        break  # Bad JSON, try next path
                    
                    # Validate it's a dict
                    if not isinstance(data, dict):
                        logging.warning(f"[ws_call] Response not a dict: {type(data).__name__}")
                        break  # Wrong type, try next path
                    
                    # Check for authentication or other errors in response
                    if "error" in data and isinstance(data.get("error"), dict):
                        error_msg = data["error"].get("message", str(data["error"]))
                        error_code = data["error"].get("code", "unknown")
                        logging.warning(f"[ws_call] Device {ip} returned error (code {error_code}): {error_msg}")
                        # If it's an authentication error, log it prominently
                        if "auth" in error_msg.lower() or "password" in error_msg.lower() or error_code in (-32001, -32002):
                            logging.error(f"⚠️ AUTHENTICATION FAILED for device {ip} - Check password in config")
                        ws.close()
                        return data  # Return error response so caller can handle it
                    
                    response_id = data.get("id")
                    
                    # Check if response matches our request
                    if request_id and response_id:
                        # Both have IDs - they must match
                        if response_id == request_id:
                            logging.info(f"[ws_call] ✓ Got matching response (id={response_id})")
                            ws.close()
                            return data
                        else:
                            # ID mismatch - device sent wrong response,  likely cached/stale
                            logging.warning(f"[ws_call] ID mismatch attempt {attempt+1}: expected '{request_id}', got '{response_id}'. Reading next response...")
                            continue  # Try reading again
                    else:
                        # No ID validation possible - accept response
                        logging.info(f"[ws_call] ✓ Got response without ID validation")
                        ws.close()
                        return data
                        
                except socket.timeout:
                    logging.debug(f"[ws_call] Timeout on recv attempt {attempt+1}")
                    silent = True   # connected, asked, and nothing came back
                    break

            # Exhausted all read attempts on this path
            logging.warning(f"[ws_call] Exhausted {max_read_attempts} read attempts on {p}")

        except socket.timeout:
            logging.debug(f"[ws_call] Connection timeout on {p}")
            silent = True
            continue
        except ConnectionRefusedError:
            logging.debug(f"[ws_call] Connection refused on {p}")
            silent = True
            continue
        except Exception as e:
            logging.debug(f"[ws_call] Error on {p}: {type(e).__name__}: {e}")
            # Silence (a timeout, a reset, no route) ends the call; a reply that rejects this path
            # (an HTTP status, a bad handshake) still moves on to the next one.
            if isinstance(e, (TimeoutError, OSError)) or type(e).__name__ == "WebSocketTimeoutException":
                silent = True
            continue
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass
    
    logging.warning(f"[ws_call] Failed on all paths for {ip}: {CONFIG['ws_paths']}")
    return None
# -------- PRIMARY / FALLBACK DEVICE PASSWORDS (family spec Part 4) --------
#
# Semantics (Part 4.3, corrected 2026-09-19): the credential cache is an
# optimization for which credential to try FIRST -- never a permanent
# one-way latch. Try the cached known-good credential for this device (or
# Primary, if none is cached); on a genuine credential rejection, make
# exactly one attempt with the OTHER configured credential, in EITHER
# direction -- a cached Fallback that starts getting rejected must be able
# to recover back to Primary, exactly as a cached/default Primary recovers
# to Fallback. Never on timeout/unreachable/malformed/refused/other
# transport or protocol failure -- those never rotate the cached source.
# If both are genuinely rejected: report Auth Failed for this call, but do
# not latch a dead state -- the very next call retries from scratch, so a
# later credential/settings change can still recover. Primary and Fallback
# resolving to the identical value count as one unique credential and are
# never sent twice.
#
# _is_credential_rejection() below is the narrowest reliable signal this
# codebase has for "the device rejected the credential specifically": it is
# the exact heuristic _ws_call already used (before this feature existed)
# to decide whether to log "AUTHENTICATION FAILED" -- i.e. it was already
# derived from real LumaStream device behavior, not invented for this
# feature. A transport failure never reaches this check at all, because
# _ws_call returns None (not a dict) for a timeout, refused connection,
# malformed response, or any other communication failure -- only a
# well-formed JSON-RPC error frame from the device itself can match.
_device_credential_lock = threading.Lock()
_device_credential_cache: Dict[str, str] = {}  # ip -> "primary" | "fallback"

_AUDIT_SECRET_FIELDS = {
    "password", "passwd", "pwd", "new_password", "newpassword", "oldpassword",
    "username", "user", "token", "secret", "fallback_password",
}

def _redact_for_audit(value):
    """Deep-redact any dict key in _AUDIT_SECRET_FIELDS (case-insensitive).
    Used for audit-log lines only -- never for what's sent to a device."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if str(k).lower() in _AUDIT_SECRET_FIELDS:
                out[k] = "***"
            else:
                out[k] = _redact_for_audit(v)
        return out
    if isinstance(value, list):
        return [_redact_for_audit(v) for v in value]
    return value

def _audit_device_request(ip, method, params, outcome):
    try:
        logging.info("[audit] device=%s method=%s params=%s outcome=%s", ip, method, json.dumps(_redact_for_audit(params), default=str), outcome)
    except Exception:
        logging.info("[audit] device=%s method=%s outcome=%s (params unlogged: serialization error)", ip, method, outcome)

def _is_credential_rejection(response) -> bool:
    """True only when the device returned a well-formed JSON-RPC error
    whose own text/code identifies it as an authentication/password
    problem. None (no response at all) is never a rejection."""
    if not isinstance(response, dict):
        return False
    error = response.get("error")
    if not isinstance(error, dict):
        return False
    message = str(error.get("message", "")).lower()
    code = error.get("code")
    return ("auth" in message) or ("password" in message) or (code in (-32001, -32002))

_CRED_MAC_RE = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")

def _credential_key(ip: str) -> str:
    """What the working credential belongs to: the unit's MAC when the inventory knows it,
    and the address only when it does not. An address says where a unit answered today; a
    different unit that takes a reused address must not inherit the previous unit's
    credential, and a unit that moved must not lose its own. Never takes the credential lock
    (the cache lock is a different lock and is never held together with it)."""
    mac = ""
    try:
        with cache_lock:
            unit = cache_units.get(ip)
            if isinstance(unit, dict):
                mac = _normalize_mac(str(unit.get("mac") or ""))
    except Exception:
        mac = ""
    return ("mac:" + mac) if _CRED_MAC_RE.fullmatch(mac or "") else ("ip:" + str(ip))

def _persisted_credential_source(ip: str) -> Optional[str]:
    """The slot recorded on the unit's own inventory record by an earlier run, so a restart
    does not forget which credential a unit accepts. A slot name only, never a value."""
    try:
        with cache_lock:
            unit = cache_units.get(ip)
            source = unit.get("password_source") if isinstance(unit, dict) else None
    except Exception:
        return None
    return source if source in ("primary", "fallback") else None

def _known_credential_source(ip: str) -> Optional[str]:
    key = _credential_key(ip)
    with _device_credential_lock:
        known = _device_credential_cache.get(key)
    if known is None:
        known = _persisted_credential_source(ip)
        if known is not None:
            with _device_credential_lock:
                _device_credential_cache.setdefault(key, known)
    return known

def _remember_credential_source(ip: str, source: str) -> None:
    key = _credential_key(ip)
    with _device_credential_lock:
        _device_credential_cache[key] = source
    # Best-effort: surface it on the cached unit record too, so /api/cache
    # and /api/scan responses carry it for the UI (Part 4.6) without a
    # separate endpoint. Never fails the caller if the unit isn't cached.
    try:
        with cache_lock:
            if ip in cache_units:
                cache_units[ip]["password_source"] = source
    except Exception:
        pass

def _forget_credential_source_as_failed(ip: str) -> None:
    # Drop the cached slot too -- not just the display status. Leaving a
    # disproven "primary"/"fallback" label in _device_credential_cache
    # would make the NEXT call keep preferring a credential that was just
    # proven wrong; popping it means the next call restarts at Primary
    # (the authoritative default), which is what lets recovery happen
    # after a device's credentials/settings change back. This is never a
    # permanent lockout: nothing anywhere checks "failed" to skip an
    # attempt, it is a display value only.
    key = _credential_key(ip)
    with _device_credential_lock:
        _device_credential_cache.pop(key, None)
    try:
        with cache_lock:
            if ip in cache_units:
                cache_units[ip]["password_source"] = "failed"
    except Exception:
        pass

def _invalidate_credential_cache(reason: str) -> None:
    """Settings changed Primary and/or Fallback. The credential cache
    stores which SLOT ("primary"/"fallback") last worked for an IP --
    never the secret value itself -- but a slot label is only meaningful
    for the value it was resolved against. Smallest safe invalidation:
    drop every cached slot so the next call for each IP starts fresh at
    Primary rather than keep trusting a label whose underlying value just
    changed. Never logs or stores password material."""
    with _device_credential_lock:
        _device_credential_cache.clear()
    try:
        with cache_lock:
            for unit in cache_units.values():
                if isinstance(unit, dict):
                    unit.pop("password_source", None)
    except Exception:
        pass
    logging.info("[credential] cache invalidated (%s)", reason)

def _credential_value(source: str) -> str:
    if source == "fallback":
        return str(CONFIG.get("fallback_password", ""))
    return str(CONFIG.get("password", "password"))

def _fallback_usable() -> bool:
    """False when Fallback is unconfigured (empty) OR resolves to the
    exact same value as Primary -- in both cases there is only one unique
    credential to try, and it must never be sent twice under two labels."""
    fallback_val = str(CONFIG.get("fallback_password", ""))
    if not fallback_val.strip():
        return False
    return fallback_val != str(CONFIG.get("password", "password"))

def _http_credential_candidates(ip: str) -> List[Tuple[str, str]]:
    """Ordered (slot, value) credentials to present over HTTP to this unit: the one that last
    worked first, then the other configured one. The WebSocket paths already resolve this way;
    the HTTP paths (slate and logo uploads, the firmware login) minted their bearer from the
    primary alone, so a unit that only accepts the fallback could be discovered and controlled
    but never given a logo. Values are returned to the caller and never logged."""
    known = _known_credential_source(ip)
    first = known if known in ("primary", "fallback") else "primary"
    if first == "fallback" and not _fallback_usable():
        first = "primary"
    order = [first]
    other = "fallback" if first == "primary" else "primary"
    if other == "primary" or _fallback_usable():
        order.append(other)
    seen, out = set(), []
    for slot in order:
        value = _credential_value(slot)
        if value in seen:
            continue
        seen.add(value)
        out.append((slot, value))
    return out

def _note_http_credential(ip: str, slot: str) -> None:
    """An HTTP call proved this slot for this unit (an HTTP success is as good as a WebSocket one)."""
    if slot in ("primary", "fallback"):
        _remember_credential_source(ip, slot)

# The producer routes live in their own module and cannot import this one; they find the
# resolver here, so there is one definition of which credential a unit is given.
APP.config['LUMA_HTTP_CREDENTIALS'] = (_http_credential_candidates, _note_http_credential)

def _resolve_ws_auth_response(ip: str, method: str, params: Optional[Any], timeout: float,
                               forced_id: Optional[str] = None,
                               honor_caller_password_override: bool = False) -> Optional[Dict[str, Any]]:
    """Shared Primary/Fallback credential resolution, used by both
    _ws_call_auth (device control/discovery) and the Producer JSON-RPC
    proxy so the two authenticated call paths behave identically.

    honor_caller_password_override: only api_producer_jsonrpc passes True.
    When set, and the caller's own params already carry a "password" key,
    that value is an explicit, operation-scoped authentication override
    (e.g. the Reboot button sends the UI's own idea of the current
    password) -- sent verbatim, in a single attempt, never rotated to the
    other credential and never remembered as the device's cached
    Primary/Fallback source. _ws_call_auth's internal call sites never set
    this flag, so their params (some of which redundantly pre-populate a
    "password" key with the current Primary value) continue to be resolved
    by Primary/Fallback exactly as before -- unchanged for every existing
    internal caller.
    """
    base_params = dict(params) if isinstance(params, dict) else params

    def attempt(pw_value: Optional[str]) -> Optional[Dict[str, Any]]:
        p = dict(base_params) if isinstance(base_params, dict) else base_params
        if pw_value is not None and isinstance(p, dict):
            p["password"] = pw_value
        unique_id = forced_id or f"{method.replace('.', '_')}_{uuid.uuid4().hex[:8]}"
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": unique_id, "method": method}
        if isinstance(p, dict):
            if p:
                payload["params"] = p
        elif p is not None:
            payload["params"] = p
        return _ws_call(ip, payload, timeout)

    if not isinstance(base_params, dict):
        # No credential concept applies to a non-dict params shape (e.g. a
        # bare boolean for SystemBlinkLed.Set).
        result = attempt(None)
        _audit_device_request(ip, method, params, "no_credential_shape")
        return result

    if (honor_caller_password_override and "password" in base_params
            and base_params["password"] in (_credential_value("primary"), _credential_value("fallback"))):
        # The page's own idea of "the current password" is one of the two configured
        # credentials, which overrides nothing: it is the page not knowing which one THIS unit
        # accepts. Sent verbatim it was refused by every unit that answers to the other one
        # (the Reboot button on a fallback unit). Resolve it like any other call.
        base_params.pop("password")
    elif honor_caller_password_override and "password" in base_params:
        override_value = base_params.pop("password")
        response = attempt(override_value)
        if response is not None and not _is_credential_rejection(response):
            _audit_device_request(ip, method, params, "ok_via_override")
        elif _is_credential_rejection(response):
            _audit_device_request(ip, method, params, "rejected_override")
        else:
            _audit_device_request(ip, method, params, "no_response")
        return response

    fallback_usable = _fallback_usable()
    known = _known_credential_source(ip)
    first_source = known if known in ("primary", "fallback") else "primary"
    if first_source == "fallback" and not fallback_usable:
        # Cached Fallback is stale (unconfigured, cleared, or now
        # identical to Primary) -- Primary is always the safe restart.
        first_source = "primary"

    response = attempt(_credential_value(first_source))

    if response is not None and not _is_credential_rejection(response):
        _remember_credential_source(ip, first_source)
        _audit_device_request(ip, method, params, "ok_via_" + first_source)
        return response

    if not _is_credential_rejection(response):
        # Transport or unrelated protocol failure -- never rotates the
        # credential source and never touches the cache.
        _audit_device_request(ip, method, params, "no_response" if response is None else "protocol_error")
        return response

    # Genuine rejection of first_source. Try the OTHER configured
    # credential exactly once, symmetrically regardless of which one was
    # tried first -- this is the fix for the one-way-fallback defect: a
    # cached Fallback that starts being rejected must be able to recover
    # back to Primary, not fail forever just because Fallback was cached.
    second_source = "fallback" if first_source == "primary" else "primary"
    if second_source == "fallback" and not fallback_usable:
        _forget_credential_source_as_failed(ip)
        _audit_device_request(ip, method, params, "rejected_" + first_source + "_no_fallback_configured")
        return response

    second_response = attempt(_credential_value(second_source))
    if second_response is not None and not _is_credential_rejection(second_response):
        _remember_credential_source(ip, second_source)
        _audit_device_request(ip, method, params, "ok_via_" + second_source)
        return second_response
    if _is_credential_rejection(second_response):
        _forget_credential_source_as_failed(ip)
        _audit_device_request(ip, method, params, "rejected_primary_and_fallback")
        return second_response

    # second_source hit a transport/protocol failure, not a rejection --
    # stop here (no third attempt). first_source is proven bad and
    # second_source is unconfirmed, so clear the cache rather than claim
    # either as known-good.
    _forget_credential_source_as_failed(ip)
    _audit_device_request(ip, method, params, "rejected_" + first_source + "_then_no_response")
    return second_response

def _ws_call_auth(ip: str, method: str, params: Optional[Any] = None, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """
    Wrapper around _ws_call that injects a device password, trying the
    configured Fallback Password only after the Primary password is
    genuinely rejected by the device (see _resolve_ws_auth_response).
    If CONFIG["ws_include_password"] is False, sends params as-is with no
    credential concept at all (legacy escape hatch, unchanged).
    Params can be dict, bool, or any JSON-serializable value.
    """
    effective_timeout = timeout or CONFIG.get("TIMEOUT", 3.0)

    if not CONFIG.get("ws_include_password", True):
        unique_id = f"{method.replace('.', '_')}_{uuid.uuid4().hex[:8]}"
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": unique_id, "method": method}
        if isinstance(params, dict):
            if params:
                payload["params"] = dict(params)
        elif params is not None:
            payload["params"] = params
        return _ws_call(ip, payload, effective_timeout)

    return _resolve_ws_auth_response(ip, method, params, effective_timeout)


def _normalize_mac(s: str) -> str:
    if not isinstance(s, str): return ""
    mac = luma_mac.normalise_mac(s)
    return mac if mac else s.strip().lower()

def _validate_hostname(hostname: str) -> tuple[bool, str]:
    """Validate hostname according to RFC 1123.
    Returns (is_valid, error_message)"""
    if not hostname:
        return False, "hostname cannot be empty"
    if len(hostname) > 255:
        return False, "hostname too long (max 255 characters)"
    # Each label must be 1-63 characters, alphanumeric or hyphen, not starting/ending with hyphen
    labels = hostname.split('.')
    for label in labels:
        if not label or len(label) > 63:
            return False, "invalid hostname format"
        if not re.match(r'^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$', label):
            return False, "hostname contains invalid characters"
    return True, ""

def _validate_streamname(streamname: str) -> tuple[bool, str]:
    """Validate streamname for RTMP/streaming.
    Returns (is_valid, error_message)"""
    if not streamname:
        return False, "streamname cannot be empty"
    if len(streamname) > 128:
        return False, "streamname too long (max 128 characters)"
    # Allow alphanumeric, underscore, hyphen, and period
    if not re.match(r'^[a-zA-Z0-9_.-]+$', streamname):
        return False, "streamname contains invalid characters (use only letters, numbers, underscore, hyphen, period)"
    return True, ""

def _ws_get_hostname(ip: str, timeout: float) -> str:
    candidates = [
        ("Network.Get", ["hostname"]),
        ("NetworkHostname.Get", ["hostname","HostName","name"]),
        ("Network.Hostname.Get", ["hostname","HostName","name"]),
        ("System.Get", ["Hostname","HostName","hostname"]),
    ]
    for method, keys in candidates:
        r = _ws_call_auth(ip, method, {}, timeout)
        if not r or "result" not in r: 
            continue
        res = r["result"]
        if isinstance(res, dict):
            for k in keys:
                v = res.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        if isinstance(res, str) and res.strip():
            return res.strip()
    return ""

def _is_switcher_device(ip: str) -> bool:
    """Check if device is a Switcher (OME CS31) based on cached model."""
    try:
        with cache_lock:
            unit = cache_units.get(ip)
            if unit:
                model = (unit.get("model") or "").lower()
                return "at-ome-cs31" in model
    except Exception:
        pass
    return False

def _ws_call_switcher(ip: str, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """
    Call a Switcher device with custom JSON-RPC format where parameters are at top-level, not in 'params' field.
    Switcher devices (OME CS31) expect format: {"jsonrpc":"2.0", "id":"...", "method":"...", "hostname":"..."}
    Used for: NetworkHostname.Set, etc.
    """
    unique_id = f"{method.replace('.', '_')}_{uuid.uuid4().hex[:8]}"
    payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": unique_id, "method": method}
    
    # For Switcher, add params at top level instead of in a "params" field
    if isinstance(params, dict):
        payload.update(params)
    elif params is not None:
        # If params is non-dict, this shouldn't happen for Switcher but handle it
        payload["params"] = params
    
    return _ws_call(ip, payload, timeout or CONFIG.get("TIMEOUT", 3.0))

def _ws_call_switcher_no_password(ip: str, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """
    Call a Switcher device with standard JSON-RPC format (with 'params' field) but NO password.
    Switcher devices support SelectVideoStream methods via standard JSON-RPC format.
    """
    unique_id = f"{method.replace('.', '_')}_{uuid.uuid4().hex[:8]}"
    payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": unique_id, "method": method}
    
    # Use standard JSON-RPC format with params field, but don't add password
    if isinstance(params, dict):
        payload["params"] = dict(params)
    elif params is not None:
        payload["params"] = params
    
    return _ws_call(ip, payload, timeout or CONFIG.get("TIMEOUT", 3.0))

def _ws_set_hostname(ip: str, hostname: str, timeout: float) -> Optional[str]:
    """Set hostname on device. Returns the new hostname if successful, None otherwise."""
    hostname = str(hostname or "").strip()
    if not hostname:
        return None
    
    # Validate hostname format
    if not _validate_hostname(hostname):
        return None
    
    # Check if this is a Switcher device (OME CS31)
    is_switcher = _is_switcher_device(ip)
    
    # Try NetworkHostname.Set with appropriate format for device type
    try:
        if is_switcher:
            # Switcher devices use top-level parameters, no password
            params = {"hostname": hostname}
            r = _ws_call_switcher(ip, "NetworkHostname.Set", params, timeout)
        else:
            # Standard format with password in params
            params = {"hostname": hostname, "password": CONFIG.get("password", "password")}
            r = _ws_call_auth(ip, "NetworkHostname.Set", params, timeout)
        
        if r and "result" in r:
            res = r["result"]
            if isinstance(res, dict):
                for k in ["hostname", "HostName", "name"]:
                    v = res.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
            if isinstance(res, str) and res.strip():
                return res.strip()
    except Exception as e:
        logging.debug(f"[_ws_set_hostname] error: {e}")
        pass
    
    return None

def _ws_get_streamname(ip: str, timeout: float) -> str:
    """Get video streamname from encoder using VideoMultiIpStream.Get."""
    try:
        r = _ws_call_auth(ip, "VideoMultiIpStream.Get", {}, timeout)
        if r and "result" in r and isinstance(r["result"], dict):
            streamname = r["result"].get("streamname")
            if isinstance(streamname, str) and streamname.strip():
                return streamname.strip()
    except Exception:
        pass
    return ""

def _ws_get_selected_stream_switcher(ip: str, timeout: float) -> Optional[str]:
    """Get currently selected stream from Switcher decoder using SelectVideoStream.Get (standard JSON-RPC, no password)."""
    try:
        r = _ws_call_switcher_no_password(ip, "SelectVideoStream.Get", {}, timeout)
        if r and "result" in r and isinstance(r["result"], dict):
            streamname = r["result"].get("streamname")
            if isinstance(streamname, str) and streamname.strip():
                return streamname.strip()
    except Exception as e:
        logging.debug(f"[_ws_get_selected_stream_switcher] error for {ip}: {e}")
    return None

def _ws_set_selected_stream_switcher(ip: str, streamname: str, timeout: float) -> Optional[str]:
    """Set selected stream on Switcher decoder using SelectVideoStream.Set (standard JSON-RPC, no password)."""
    streamname = str(streamname or "").strip()
    if not streamname:
        return None
    
    # Validate streamname before sending to device
    is_valid, err_msg = _validate_streamname(streamname)
    if not is_valid:
        logging.warning(f"[_ws_set_selected_stream_switcher] {ip}: invalid streamname - {err_msg}")
        return None
    
    try:
        # Switcher format: standard JSON-RPC with params field, no password, selecttype=0
        params = {"selecttype": 0, "streamname": streamname}
        r = _ws_call_switcher_no_password(ip, "SelectVideoStream.Set", params, timeout)
        # Accept any valid JSON-RPC result (with or without streamname echo).
        # Some CS31 firmware versions return {} or {"selecttype":0} without echoing streamname.
        if r and "result" in r and "error" not in r:
            # Prefer echoed streamname from device; fall back to the value we sent
            result_stream = r["result"].get("streamname") if isinstance(r["result"], dict) else None
            return (result_stream.strip() if isinstance(result_stream, str) and result_stream.strip()
                    else streamname)
    except Exception as e:
        logging.debug(f"[_ws_set_selected_stream_switcher] error for {ip}: {e}")
    return None

def _ws_set_streamname(ip: str, streamname: str, timeout: float) -> Optional[str]:
    """Set video streamname on encoder using VideoMultiIpStream.Set.
    Preserves existing multiip and port settings."""
    streamname = str(streamname or "").strip()
    if not streamname:
        return None
    
    try:
        # First get current settings to preserve multiip and port
        get_r = _ws_call_auth(ip, "VideoMultiIpStream.Get", {}, timeout)
        if not get_r or "result" not in get_r:
            return None
        
        current = get_r["result"]
        if not isinstance(current, dict):
            return None
        
        # Extract IP address and port from current settings
        ipaddr = ""
        portnum = 1000
        
        if "multiip" in current and isinstance(current["multiip"], dict):
            ipaddr = current["multiip"].get("ipaddr", "")
        
        if "multiport" in current and isinstance(current["multiport"], dict):
            portnum = current["multiport"].get("portnum", 1000)
        
        # Build Set parameters - note: port goes inside multiip
        params = {
            "streamname": streamname,
            "transenable": current.get("transenable", True),
            "usetype": current.get("usetype", "default"),
            "multiip": {
                "ipaddr": ipaddr,
                "portnum": portnum
            },
            "password": CONFIG.get("password", "password")
        }
        
        # Set the new streamname
        set_r = _ws_call_auth(ip, "VideoMultiIpStream.Set", params, timeout)
        if set_r and "result" in set_r:
            res = set_r["result"]
            if isinstance(res, dict):
                new_streamname = res.get("streamname")
                if isinstance(new_streamname, str) and new_streamname.strip():
                    return new_streamname.strip()
    except Exception as e:
        logging.warning(f"[ws_set_streamname] Error: {e}")
    
    return None

def _ws_get_mac(ip: str, timeout: float) -> str:
    r = _ws_call_auth(ip, "Network.Get", {}, timeout)
    if r and "result" in r and isinstance(r["result"], dict):
        mac = r["result"].get("mac") or r["result"].get("MAC") or r["result"].get("Mac")
        if isinstance(mac, str) and mac.strip():
            return _normalize_mac(mac)
    candidates = [
        ("NetworkMAC.Get", ["mac","MAC","Mac","macAddress","MacAddress"]),
        ("Network.MAC.Get", ["mac","MAC","Mac","macAddress","MacAddress"]),
        ("NetworkMac.Get", ["mac","MAC","Mac","macAddress","MacAddress"]),
        ("Network.Mac.Get", ["mac","MAC","Mac","macAddress","MacAddress"]),
        ("System.Get", ["MAC","Mac","MacAddr","MACAddr"]),
    ]
    for method, keys in candidates:
        rr = _ws_call_auth(ip, method, {}, timeout)
        if not rr or "result" not in rr:
            continue
        res = rr["result"]
        if isinstance(res, dict):
            for k in keys:
                v = res.get(k)
                if isinstance(v, str) and v.strip():
                    return _normalize_mac(v)
        if isinstance(res, str) and res.strip():
            return _normalize_mac(res)
    # ARP fallback (best effort)
    try:
        if platform.system().lower().startswith("win"):
            out = _subprocess_check_output_no_window(["arp","-a", ip], text=True, encoding="utf-8", errors="ignore")
            m = re.search(r"(?i)\b([0-9a-f]{2}(?:-[0-9a-f]{2}){5})\b", out)
            if m: return _normalize_mac(m.group(1))
        else:
            out = _subprocess_check_output_no_window(["arp","-n", ip], text=True, encoding="utf-8", errors="ignore")
            m = re.search(r"(?i)\b([0-9a-f]{2}(?::[0-9a-f]{2}){5})\b", out)
            if m: return _normalize_mac(m.group(1))
    except Exception:
        pass
    return ""

def _ws_get_dante_info(ip: str, timeout: float) -> Dict[str, str]:
    try:
        r = _ws_call_auth(ip, "DanteInfo.Get", {"password": CONFIG.get("password", "password")}, timeout)
        if r and "result" in r and isinstance(r["result"], dict):
            res = r["result"]
            name = res.get("name") or ""
            dip = res.get("ip") or ""
            dmac = res.get("mac") or res.get("MAC") or res.get("Mac") or res.get("macaddr") or res.get("macAddr") or res.get("macAddress") or res.get("MacAddress") or ""
            dmac = _normalize_mac(dmac) if isinstance(dmac, str) else ""
            return {"dante_name": name, "dante_ip": dip, "dante_mac": dmac}
    except Exception:
        pass
    return {"dante_name": "", "dante_ip": "", "dante_mac": ""}

def _ws_get_dante_power_status(ip: str, timeout: float) -> bool:
    try:
        r = _ws_call_auth(ip, "SystemFuncDantePowerStatus.Get", {"password": CONFIG.get("password", "password")}, timeout)
        if r and "result" in r and isinstance(r["result"], dict):
            return bool(r["result"].get("dante", False))
    except Exception:
        pass
    return False

def _ws_get_hdcp_encoder(ip: str, timeout: float) -> str:
    try:
        r = _ws_call_auth(ip, "HdcpAnnouncement.Get", {"password": CONFIG.get("password", "password")}, timeout)
        if r and "result" in r and isinstance(r["result"], dict):
            announcements = r["result"].get("hdcpannouncements", [])
            if isinstance(announcements, list) and len(announcements) > 0:
                return announcements[0].get("hdcpversion", "")
    except Exception:
        pass
    return ""

def _ws_get_hdcp_decoder(ip: str, timeout: float) -> str:
    try:
        r = _ws_call_auth(ip, "OutHdcpAnnouncement.Get", {"password": CONFIG.get("password", "password")}, timeout)
        if r and "result" in r and isinstance(r["result"], dict):
            return r["result"].get("hdcpversion", "")
    except Exception:
        pass
    return ""

def _edid_mode_to_label(mode: Optional[int], edid_name: str = "") -> str:
    mode_map = {
        1: "4K60",
        2: "1080P",
        3: "720P",
        4: "User",
    }
    label = mode_map.get(mode)
    if not label:
        return ""
    if mode == 4 and isinstance(edid_name, str) and edid_name.strip():
        return f"{label} ({edid_name.strip()})"
    return label

def _ws_get_encoder_edid(ip: str, timeout: float) -> Dict[str, Any]:
    info: Dict[str, Any] = {"edidmode": None, "source": "", "edidname": "", "label": ""}
    try:
        r = _ws_call_auth(ip, "EdidInput.Get", {}, timeout)
        if r and "result" in r and isinstance(r["result"], dict):
            res = r["result"]
            mode = res.get("edidmode")
            if isinstance(mode, int):
                info["edidmode"] = mode
            src = res.get("source")
            if isinstance(src, str):
                info["source"] = src
    except Exception:
        pass

    if info.get("edidmode") == 4:
        try:
            r2 = _ws_call_auth(ip, "EdidMem1File.Get", {}, timeout)
            if r2 and "result" in r2 and isinstance(r2["result"], dict):
                edidname = r2["result"].get("edidname")
                if isinstance(edidname, str):
                    info["edidname"] = edidname
        except Exception:
            pass

    info["label"] = _edid_mode_to_label(info.get("edidmode"), info.get("edidname", ""))
    return info

def _ws_get_decoder_output_timing(ip: str, timeout: float) -> str:
    try:
        r = _ws_call_auth(ip, "VideoOutTiming.Get", {}, timeout)
        if r and "result" in r and isinstance(r["result"], dict):
            timing = r["result"].get("timing")
            if isinstance(timing, str):
                return timing
    except Exception:
        pass
    return ""

def _ws_get_decoder_output_timing_list(ip: str, timeout: float) -> List[str]:
    try:
        r = _ws_call_auth(ip, "VideoOutSupportedTimingList.Get", {}, timeout)
        if r and "result" in r and isinstance(r["result"], list):
            return [str(x) for x in r["result"] if isinstance(x, str)]
    except Exception:
        pass
    return []

def _ws_get_minimal(ip: str) -> Optional[Dict[str, Any]]:
    out = {"ip": ip}
    sysr = _ws_call_auth(ip, "System.Get", {}, SCAN_TIMEOUT)
    
    # Log the raw response for debugging
    logging.info(f"[WS_GET_MINIMAL] {ip} response type: {'SUCCESS' if sysr and 'result' in sysr else 'ERROR'}")
    logging.debug(f"[ws_get_minimal] {ip} raw response: {sysr}")
    
    if not sysr or "result" not in sysr:
        # Check if it was an authentication error
        if sysr and "error" in sysr:
            error_msg = sysr.get("error", {}).get("message", "") if isinstance(sysr.get("error"), dict) else str(sysr.get("error"))
            error_code = sysr.get("error", {}).get("code", "unknown") if isinstance(sysr.get("error"), dict) else "unknown"
            if "auth" in error_msg.lower() or "password" in error_msg.lower():
                logging.error(f"⚠️ Authentication failed for device {ip} - Incorrect password")
                logging.error(f"   Full error response: {sysr}")
                # Return error dict instead of None so we can track auth failures
                return {"ip": ip, "_error": "Authentication failed", "_error_type": "auth", "_error_details": {"message": error_msg, "code": error_code}}
            else:
                logging.warning(f"[ws_get_minimal] Device {ip} returned error: {error_msg}")
                logging.warning(f"   Full error response: {sysr}")
        return None
    r = sysr["result"]
    # Flexible key extraction (case-insensitive + aliases)
    def _first_key(d: Dict[str, Any], candidates):
        if not isinstance(d, dict):
            return ""
        # Direct exact match first (fast path)
        for k in candidates:
            if k in d and isinstance(d[k], str) and d[k].strip():
                return d[k].strip()
        # Case-insensitive / alias match
        lower_map = {k.lower(): k for k in d.keys()}
        for k in candidates:
            lk = k.lower()
            if lk in lower_map:
                v = d.get(lower_map[lk])
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return ""

    model_val = _first_key(r, ["Model", "model", "HWModel", "DeviceModel"]) or ""
    # Fallback: any key containing 'model'
    if not model_val:
        for k, v in r.items():
            if isinstance(v, str) and v.strip() and "model" in k.lower():
                model_val = v.strip(); break

    serial_val = _first_key(r, ["SN", "sn", "SerialNumber", "serialnumber", "Serial", "serial"]) or ""
    # Fallback: any key containing 'serial'
    if not serial_val:
        for k, v in r.items():
            if isinstance(v, str) and v.strip() and "serial" in k.lower():
                serial_val = v.strip(); break

    fw_val = _first_key(r, ["FwVer", "FWVersion", "fwver", "Firmware", "firmware", "Version", "version"]) or ""

    out["model"] = model_val
    out["firmware"] = fw_val
    out["version"] = fw_val  # Preserve existing duplication behavior
    out["serialnumber"] = serial_val
    out["hostname"] = _ws_get_hostname(ip, SCAN_TIMEOUT) or ""
    out["mac"] = _ws_get_mac(ip, SCAN_TIMEOUT) or ""
    ntp_settings = _read_ntp_settings(ip, SCAN_TIMEOUT)
    if ntp_settings:
        out["timezone"] = ntp_settings.get("timezone", "")
        out["active_timezone"] = ntp_settings.get("active_timezone", "")
        out["ntp_server"] = ntp_settings.get("ntp_server", "")
        out["ntpserver"] = ntp_settings.get("ntpserver", "")
        out["datetime"] = ntp_settings.get("datetime", "")
        out["device_datetime"] = ntp_settings.get("device_datetime", "")

    type_hint = _first_key(r, ["Role", "role", "Type", "type", "DeviceType", "deviceType", "devicetype", "DevType", "devtype"]).lower()
    m = (out.get("model","") or "").lower()

    inferred_type = ""
    inferred_role = ""

    # Highest-priority: explicit switcher model
    if "at-ome-cs31" in m:
        inferred_type = "switcher"
        inferred_role = "Decoder/Switcher"
    # Next: explicit type/role hints returned by System.Get
    elif any(tok in type_hint for tok in ["switcher", "cs31"]):
        inferred_type = "switcher"
        inferred_role = "Decoder/Switcher"
    elif any(tok in type_hint for tok in ["encoder", "encode", "transmitter", "tx"]):
        inferred_type = "encoder"
        inferred_role = "Encoder"
    elif any(tok in type_hint for tok in ["decoder", "decode", "receiver", "rx"]):
        inferred_type = "decoder"
        inferred_role = "Decoder"
    else:
        # Fallback: model family heuristics
        if m.startswith(("hw-luma-e", "at-luma-e", "at-omni-e", "at-ome-e")) or re.search(r"(?:^|[-_\s])e\d{3,4}\b", m):
            inferred_type = "encoder"
            inferred_role = "Encoder"
        elif m.startswith(("hw-luma-d", "at-luma-d", "at-omni-d", "at-ome-d")) or re.search(r"(?:^|[-_\s])d\d{3,4}\b", m):
            inferred_type = "decoder"
            inferred_role = "Decoder"

    out["type"] = inferred_type
    out["role"] = inferred_role
    out["status"] = "idle"
    out["status_ts"] = int(time.time())
    if out.get("role") == "Encoder":
        dante = _ws_get_dante_info(ip, SCAN_TIMEOUT)
        out["dante_name"] = dante.get("dante_name", "")
        out["dante_ip"] = dante.get("dante_ip", "")
        out["dante_mac"] = dante.get("dante_mac", "")
        out["dante_enabled"] = _ws_get_dante_power_status(ip, SCAN_TIMEOUT)
        out["hdcp"] = _ws_get_hdcp_encoder(ip, SCAN_TIMEOUT)
        edid = _ws_get_encoder_edid(ip, SCAN_TIMEOUT)
        out["edid_mode"] = edid.get("edidmode")
        out["edid_source"] = edid.get("source", "")
        out["edid_name"] = edid.get("edidname", "")
        out["edid_resolution"] = edid.get("label", "")
    elif out.get("role") == "Decoder/Switcher" or out.get("type") == "switcher":
        # For Switchers, get current selected stream using SelectVideoStream.Get
        streamname = _ws_get_selected_stream_switcher(ip, SCAN_TIMEOUT)
        if streamname:
            if "dec" not in out:
                out["dec"] = {}
            if "video" not in out["dec"]:
                out["dec"]["video"] = {}
            out["dec"]["video"]["streamname"] = streamname
    elif out.get("role") == "Decoder":
        # For standard decoders, get current selected stream
        try:
            resp = _ws_call_auth(ip, "SelectVideoStream.Get", {"password": CONFIG.get("password", "password")}, SCAN_TIMEOUT)
            if resp and "result" in resp and isinstance(resp["result"], dict):
                streamname = resp["result"].get("streamname")
                if streamname:
                    if "dec" not in out:
                        out["dec"] = {}
                    if "video" not in out["dec"]:
                        out["dec"]["video"] = {}
                    out["dec"]["video"]["streamname"] = streamname
        except Exception as e:
            logging.debug(f"[_ws_get_minimal] Failed to get decoder stream for {ip}: {e}")
        
        out["hdcp"] = _ws_get_hdcp_decoder(ip, SCAN_TIMEOUT)
        out["video_out_timing"] = _ws_get_decoder_output_timing(ip, SCAN_TIMEOUT)
        out["video_out_supported_timing_list"] = _ws_get_decoder_output_timing_list(ip, SCAN_TIMEOUT)
        out["edid_resolution"] = out.get("video_out_timing", "")
    return out


# -------- Encoder / Decoder detail queries (JSON-RPC) --------
def _safe_get(d, *path, default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default

def _query_encoder_details(ip: str, timeout: float):
    enc: Dict[str, Any] = {}

    # RTMP settings
    r1 = _ws_call_auth(ip, "Rtmp.Get", {}, timeout)
    if isinstance(r1, dict) and "result" in r1:
        res = r1["result"]
        enc["rtmp"] = {
            "enable": bool(res.get("enable", False)),
            "url": res.get("url", ""),
            "port": res.get("port", 1935),
            "streamname": res.get("streamname", ""),
            "username": res.get("username", ""),
            "authpassword": res.get("authpassword", ""),
            "advanced": bool(res.get("advanced", False)),
        }

    # Audio multicast (AudioMultiIpStream.Get)
    r2 = _ws_call_auth(ip, "AudioMultiIpStream.Get", {}, timeout)
    if isinstance(r2, dict) and "result" in r2:
        res = r2["result"]
        enc["audio"] = {
            "followvideoenable": bool(res.get("followvideoenable", False)),
            "streamname": res.get("streamname", ""),
            "transenable": bool(res.get("transenable", False)),
            "usetype": res.get("usetype", ""),
            "ip": _safe_get(res, "multiip", "ipaddr", default=""),
            "port": _safe_get(res, "multiport", "portnum", default=None),
            "userdefineip": _safe_get(res, "multiip", "userdefineip", default=""),
            "userdefineport": _safe_get(res, "multiport", "userdefineportnum", default=None),
        }

    # Video multicast (VideoMultiIpStream.Get)
    r3 = _ws_call_auth(ip, "VideoMultiIpStream.Get", {}, timeout)
    if isinstance(r3, dict) and "result" in r3:
        res = r3["result"]
        enc["video"] = {
            "streamname": res.get("streamname", ""),
            "transenable": bool(res.get("transenable", False)),
            "usetype": res.get("usetype", ""),
            "ip": _safe_get(res, "multiip", "ipaddr", default=""),
            "port": _safe_get(res, "multiport", "portnum", default=None),
            "userdefineip": _safe_get(res, "multiip", "userdefineip", default=""),
            "userdefineport": _safe_get(res, "multiport", "userdefineportnum", default=None),
        }

    # Current encoder output (MainStreamVideoEncode.Get)
    r4 = _ws_call_auth(ip, "MainStreamVideoEncode.Get", {}, timeout)
    if isinstance(r4, dict) and "result" in r4:
        res = r4["result"]
        enc["encode"] = {
            "encodetype": res.get("encodetype", ""),
            "resolution": res.get("resolution", ""),
            "framerate": res.get("framerate", None),
            "bitrate": res.get("bitrate", None),
            "gop": res.get("gop", None),
            "rctype": res.get("rctype", ""),
            "chn": res.get("chn", None),
        }

    # Video mute status (VideoInputMute.Get)
    r5 = _ws_call_auth(ip, "VideoInputMute.Get", {}, timeout)
    if isinstance(r5, dict) and "result" in r5:
        enc["video_mute"] = bool(_safe_get(r5, "result", "videomute", default=False))

    # ------- NEW: OSD Text Overlay (OsdText.Get) -------
    r6 = _ws_call_auth(ip, "OsdText.Get", {"osdindex": 0}, timeout)
    if isinstance(r6, dict) and "result" in r6:
        res = r6["result"] if isinstance(r6["result"], dict) else {}
        disp = res.get("displaytext") or {}
        enc["osd_text"] = {
            "osdindex": res.get("osdindex", 0),
            "osdtextenabled": bool(res.get("osdtextenabled", False)),
            "displaytext": {
                "content": disp.get("content", ""),
                "fontcolor": disp.get("fontcolor", ""),
                "backcolor": disp.get("backcolor", ""),
                "fonttransparency": disp.get("fonttransparency", None),
                "backtransparency": disp.get("backtransparency", None),
                "startpostion": {
                    "startx": _safe_get(disp, "startpostion", "startx", default=0),
                    "starty": _safe_get(disp, "startpostion", "starty", default=0),
                },
                "fontsize": { "fonth": _safe_get(disp, "fontsize", "fonth", default=None) },
                "displayscrolleffects": {
                    "enable": _safe_get(disp, "displayscrolleffects", "enable", default=False),
                    "direction": _safe_get(disp, "displayscrolleffects", "direction", default=0),
                    "iterations": _safe_get(disp, "displayscrolleffects", "iterations", default=0),
                    "speed": _safe_get(disp, "displayscrolleffects", "speed", default=0),
                },
            },
        }

    # ------- NEW: Image Display (ImageDisplay.Get) -------
    r7 = _ws_call_auth(ip, "ImageDisplay.Get", {}, timeout)
    if isinstance(r7, dict) and "result" in r7:
        enc["image_display"] = {
            "display": (r7["result"].get("display") if isinstance(r7["result"], dict) else "")
        }

    # ------- NEW: Logo Overlay (OsdLogo.Get) -------
    r8 = _ws_call_auth(ip, "OsdLogo.Get", {}, timeout)
    if isinstance(r8, dict) and "result" in r8:
        res = r8["result"] if isinstance(r8["result"], dict) else {}
        dl = res.get("displaylogo") or {}
        enc["logo"] = {
            "osdlogoenabled": bool(res.get("osdlogoenabled", False)),
            "displaylogo": {
                "backtransparency": dl.get("backtransparency", None),
                "logohight": dl.get("logohight", None),
                "startpostion": {
                    "startx": _safe_get(dl, "startpostion", "startx", default=0),
                    "starty": _safe_get(dl, "startpostion", "starty", default=0),
                },
            },
        }

    # ------- NEW: Audio mute (AudioInputMute.Get) -------
    r9 = _ws_call_auth(ip, "AudioInputMute.Get", {}, timeout)
    if isinstance(r9, dict) and "result" in r9:
        enc["audio_mute"] = bool(_safe_get(r9, "result", "mute", default=False))

    # ------- NEW: Audio source (AudioInSelection.Get) -------
    r10 = _ws_call_auth(ip, "AudioInSelection.Get", {}, timeout)
    if isinstance(r10, dict) and "result" in r10:
        enc["audio_source"] = (r10["result"].get("audiosource") if isinstance(r10["result"], dict) else "")

    # ------- NEW: Active profile (ProfileSelection.Get) -------
    # Some firmware returns JSON-as-string in "result"; try to parse but keep raw if it fails.
    r11 = _ws_call_auth(ip, "ProfileSelection.Get", {}, timeout)
    profile_dict = None
    profile_raw = None
    if isinstance(r11, dict) and "result" in r11:
        if isinstance(r11["result"], str):
            profile_raw = r11["result"]
            try:
                profile_dict = json.loads(r11["result"])
            except Exception:
                profile_dict = None
        elif isinstance(r11["result"], dict):
            profile_dict = r11["result"]
            try:
                profile_raw = json.dumps(r11["result"])
            except Exception:
                profile_raw = None
    if profile_dict or profile_raw:
        enc["profile"] = {"parsed": profile_dict, "raw": profile_raw}

    return enc


def _query_decoder_details(ip: str, timeout: float):
    dec = {}
    # Selected audio (SelectAudioStream.Get)
    r1 = _ws_call_auth(ip, "SelectAudioStream.Get", {}, timeout)
    if isinstance(r1, dict) and "result" in r1:
        res = r1["result"]
        dec["audio"] = {
            "followvideoenable": bool(res.get("followvideoenable", False)),
            "streamname": res.get("streamname", ""),
            "ip": _safe_get(res, "multiaddr", "ipaddr", default=""),
            "port": _safe_get(res, "multiaddr", "portnumber", default=None),
            "selecttype": res.get("selecttype", None),
        }

    # Available video stream names (VideoStreamNameList.Get)
    r2 = _ws_call_auth(ip, "VideoStreamNameList.Get", {}, timeout)
    if isinstance(r2, dict) and "result" in r2 and isinstance(r2["result"], list):
        dec["video_stream_list"] = list(r2["result"])

    # Selected video (SelectVideoStream.Get)
    r3 = _ws_call_auth(ip, "SelectVideoStream.Get", {}, timeout)
    if isinstance(r3, dict) and "result" in r3:
        res = r3["result"]
        dec["video"] = {
            "streamname": res.get("streamname", ""),
            "ip": _safe_get(res, "multiaddr", "ipaddr", default=""),
            "port": _safe_get(res, "multiaddr", "portnumber", default=None),
            "selecttype": res.get("selecttype", None),
        }

    return dec


def _ws_trigger_upgrade(ip: str) -> bool:
    r = _ws_call_auth(ip, "Platform.Upgrade", {})
    ok = bool(r)
    logging.info(f"[ws_trigger] {'ok' if ok else 'failed'} {ip}")
    return ok


def _ws_factory_reset(ip: str) -> bool:
    r = _ws_call_auth(ip, "Platform.FactoryReset", {})
    ok = bool(r)
    logging.info(f"[ws_reset] {'sent' if ok else 'failed to send'} to {ip}")
    return ok


# ---------------- expand / persist ----------------

def _expand_targets(targets: str) -> List[str]:
    ips: List[str] = []
    if not targets: return ips
    for part in re.split(r"[,\s;]+", str(targets).strip()):
        if not part: continue
        if "/" in part:
            try:
                net = ipaddress.ip_network(part, strict=False)
                # Taken lazily: list(net.hosts()) of a /8 built sixteen million
                # addresses just to keep the first 4096 of them.
                ips.extend(str(h) for h in itertools.islice(net.hosts(), 4096))
            except Exception:
                pass
            continue
        full = re.match(r"^(\d{1,3}(?:\.\d{1,3}){3})-(\d{1,3}(?:\.\d{1,3}){3})$", part)
        if full:
            try:
                a, b = sorted((int(ipaddress.IPv4Address(full.group(1))), int(ipaddress.IPv4Address(full.group(2)))))
            except ValueError:
                continue
            # Counted, not built: the same 4096 ceiling a network gets.
            ips.extend(str(ipaddress.IPv4Address(n)) for n in range(a, min(b, a + 4095) + 1))
            continue
        m = re.match(r"^(\d{1,3}(?:\.\d{1,3}){3})-(\d{1,3})$", part)
        if m:
            base = m.group(1); end = int(m.group(2))
            octs = base.split("."); start = int(octs[-1]); prefix = ".".join(octs[:-1])
            lo,hi = sorted([start,end])
            if hi > 255:
                continue     # not an address range; it used to build hi-lo strings anyway
            for n in range(lo,hi+1):
                ips.append(f"{prefix}.{n}")
            continue
        if re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", part):
            ips.append(part)
    seen=set(); out=[]
    for ip in ips:
        if ip not in seen:
            out.append(ip); seen.add(ip)
    return out

def _cache_file_rows(data):
    """The list of rows in a parsed inventory file, or None if it is not an inventory.

    Two shapes are accepted: a bare list, and {"units": [...]}. Anything else, including a
    "units" value that is not a list, is not an inventory.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("units"), list):
        return data["units"]
    return None


def _persist_units_cache():
    """Save cache to disk, preserving existing fields not currently in memory.

    The disk read, the merge and the write are one operation under the file's own
    lock, and the write is atomic. The in-memory snapshot is taken first (under
    `cache_lock`, released before the file lock is taken, so the two locks are
    never held in the opposite order from `api_clear_units`), and a write whose
    snapshot predates a clear or a removal is dropped rather than resurrecting
    what was cleared.

    Returns True when the file holds this snapshot, or when the write was
    deliberately dropped because the inventory was cleared meanwhile (there is
    nothing left to save), and False when the write failed. Existing callers
    that ignore the result are unaffected; a caller that must not claim a save
    that did not happen (a restore) reads it.
    """
    try:
        with cache_lock:
            generation = cache_generation
            snapshot = {ip: dict(unit) for ip, unit in cache_units.items()}
        with luma_store.lock_for(CACHE_FILE):
            existing = {}
            data = luma_store.read_json(CACHE_FILE, default=None)
            # Handle both array format and {"units": [...]} format
            disk_rows = _cache_file_rows(data)
            if disk_rows is None and data is not None:
                # Valid JSON that is not an inventory (a number, a string, "units" that is not a
                # list). It can never be merged, and leaving it would make every later save fail
                # the same way: keep it for inspection and write the inventory fresh.
                logging.error("[persist] %s is not an inventory; quarantining it", os.path.basename(CACHE_FILE))
                luma_store.quarantine(CACHE_FILE)
            for u in (disk_rows or []):
                if isinstance(u, dict) and isinstance(u.get("ip"), str):
                    existing[u["ip"]] = u
            if generation != cache_generation:
                logging.info("[persist] skipped: the cache was cleared while this write was pending")
                return True
            merged = {}
            # Start with the snapshot as the source of truth
            for ip, unit in snapshot.items():
                merged[ip] = unit
                # Preserve additional fields from disk for existing units
                if ip in existing:
                    for key, value in existing[ip].items():
                        if key not in merged[ip]:
                            merged[ip][key] = value
            rows = list(merged.values())
            if not luma_store.atomic_write_json(CACHE_FILE, rows):
                raise OSError("cache could not be written")
        logging.debug(f"[persist] wrote {len(rows)} units -> {CACHE_FILE}")
        return True
    except Exception as e:
        logging.warning(f"[persist] failed {e}")
        return False

def _merge_disk_into_memory():
    """Add units that are on disk but not in memory, without disturbing memory.

    Memory is the live state: it carries route read-backs and scan results that
    may not have been written yet, so a disk row never replaces one. A malformed
    file is quarantined and ignored.
    """
    if not os.path.exists(CACHE_FILE):
        logging.debug("[merge] No cache file exists yet")
        return 0
    try:
        # The file is read outside the lock. A Clear Units or removal that lands before the rows
        # are applied makes them describe an inventory that no longer exists, so they are dropped.
        with cache_lock:
            generation = cache_generation
        data = luma_store.read_json(CACHE_FILE, default=None)
        # Handle both array format and {"units": [...]} format
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict) and "units" in data:
            rows = data["units"]
        else:
            return 0
        added = 0
        with cache_lock:
            if generation != cache_generation:
                return 0
            known = {luma_projection.unit_identity(u) for u in cache_units.values()}
            known.discard("")
            for r in rows:
                if not isinstance(r, dict):
                    continue
                ip = r.get("ip")
                if not ip or ip in cache_units:
                    continue
                # A unit already held under another address is the same unit: the disk row
                # is its old address, and adding it would give one identity two addresses.
                disk_mac = luma_projection.unit_identity(r)
                if disk_mac and disk_mac in known:
                    continue
                if disk_mac:
                    known.add(disk_mac)
                cache_units[ip] = r
                added += 1
        if added:
            logging.info(f"[merge] added {added} units from disk into memory")
        return added
    except Exception as e:
        logging.warning(f"[merge] failed {e} - preserving existing in-memory cache")
        return 0

# ---------------- static/UI routes ----------------

def _send_ui_file(filename: str):
    resp = send_from_directory(UI_DIR, filename)
    try:
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    except Exception:
        pass
    return resp

@APP.get("/")
def root():
    if os.path.exists(os.path.join(UI_DIR, "index.html")):
        logging.info("[root] serving /ui/index.html")
        return _send_ui_file("index.html")
    return "UI not found. Put index.html in ./ui/"

@APP.get("/favicon.ico")
def favicon():
    icon_path = os.path.join(UI_DIR, "favicon.ico")
    if os.path.isfile(icon_path):
        return _send_ui_file("favicon.ico")
    abort(404)

@APP.get("/ui/<path:filename>")
def ui_files(filename):
    path = os.path.join(UI_DIR, filename)
    if os.path.isfile(path):
        return _send_ui_file(filename)
    abort(404)

# ---------------- API routes ----------------

def api_adapters_legacy():
    rows: List[Dict[str, Any]] = []
    try:
        import netifaces
        for iface in netifaces.interfaces():
            addrs = netifaces.ifaddresses(iface).get(netifaces.AF_INET, [])
            for a in addrs:
                ip = a.get("addr"); nm = a.get("netmask")
                if not ip or not nm: continue
                try:
                    net = ipaddress.IPv4Network((ip, nm), strict=False)
                    name = f"{iface} — {net.with_netmask}"
                    cidr = str(net)
                    rows.append({"name": name, "cidr": cidr, "scan": cidr})
                except Exception:
                    pass
    except Exception:
        # ipconfig fallback on Windows
        if platform.system().lower().startswith("win"):
            try:
                out = _subprocess_check_output_no_window(["ipconfig"], text=True, encoding="utf-8", errors="ignore")
                cur=None; ip=None; mask=None
                for line in out.splitlines():
                    t=line.strip()
                    if not t: continue
                    if t.endswith(":") and ("adapter" in t.lower() or "wi-fi" in t.lower() or "ethernet" in t.lower()):
                        cur=t.rstrip(":"); ip=None; mask=None; continue
                    if "IPv4 Address" in t or "IPv4 Address." in t:
                        m=re.search(r"(\d+\.\d+\.\d+\.\d+)", t); 
                        if m: ip=m.group(1)
                    if "Subnet Mask" in t:
                        m=re.search(r"(\d+\.\d+\.\d+\.\d+)", t); 
                        if m: mask=m.group(1)
                        if cur and ip and mask:
                            try:
                                net = ipaddress.IPv4Network((ip, mask), strict=False)
                                rows.append({"name": cur, "cidr": str(net), "scan": str(net)})
                            except Exception:
                                pass
            except Exception:
                pass
    if not rows:
        rows=[{"name":"Default — 192.168.0.0/24","cidr":"192.168.0.0/24","scan":"192.168.0.0/24"}]
    return jsonify({"adapters": rows})

def _is_private_ipv4(ip: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip)
        return addr.is_private and not addr.is_loopback
    except Exception:
        return False

def _adapter_scan_entry(name: str, ip: str, mask: str) -> Optional[Dict[str, Any]]:
    if not ip or not mask:
        return None
    if ip.startswith("169.254.") or not _is_private_ipv4(ip):
        return None
    try:
        net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
        cidr = f"{net.network_address}/{net.prefixlen}"
        scan = f"{str(net.network_address).rsplit('.', 1)[0]}.1-254" if net.prefixlen <= 24 else cidr
        return {"name": name or f"iface {ip}", "ip": ip, "netmask": str(net.netmask), "cidr": cidr, "scan": scan}
    except Exception:
        return None

def _dedupe_adapters(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        key = (row.get("ip"), row.get("cidr"))
        if not row.get("ip") or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out

def _sort_adapters(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # One ranking for every list of adapters (see luma_adapters.rank_key): a
    # connected physical Ethernet port first, then Wi-Fi, other physical,
    # and virtual/VPN adapters last. It only orders the list; a selection the
    # operator saved is never re-ranked.
    return luma_adapters.rank_adapters(rows)

def _adapters_windows(active_only: bool = True) -> List[Dict[str, Any]]:
    if not platform.system().lower().startswith("win"):
        return []
    rows: List[Dict[str, Any]] = []
    try:
        txt = _subprocess_check_output_no_window(["ipconfig", "/all"], text=True, encoding="utf-8", errors="ignore")
    except Exception:
        return rows
    for block in re.split(r"\r?\n\r?\n", txt):
        if active_only and re.search(r"(?mi)^\s*Media\s*State\s*.*:\s*Media\s*disconnected\s*$", block):
            continue
        name_m = re.search(r"(?mi)^(?:.*adapter)\s+(.+?):\s*$", block)
        ipv4_m = re.search(r"(?mi)^\s*IPv4[^:]*:\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", block)
        mask_m = re.search(r"(?mi)^\s*Subnet\s*Mask[^:]*:\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", block)
        if not (name_m and ipv4_m and mask_m):
            continue
        entry = _adapter_scan_entry(name_m.group(1).strip(), ipv4_m.group(1), mask_m.group(1))
        if entry:
            rows.append(entry)
    return _dedupe_adapters(rows)

def _adapters_netifaces(active_only: bool = True) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        import netifaces
    except Exception:
        return rows
    try:
        for iface in netifaces.interfaces():
            addrs = netifaces.ifaddresses(iface).get(netifaces.AF_INET, [])
            for addr in addrs:
                entry = _adapter_scan_entry(iface, addr.get("addr"), addr.get("netmask"))
                if entry:
                    rows.append(entry)
    except Exception:
        return []
    return _dedupe_adapters(rows)

def _adapters_psutil(active_only: bool = True) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        import psutil
    except Exception:
        return rows
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except Exception:
        return rows
    for name, addr_list in addrs.items():
        if active_only and name in stats and not stats[name].isup:
            continue
        for addr in addr_list:
            if getattr(addr, "family", None) != socket.AF_INET:
                continue
            entry = _adapter_scan_entry(name, addr.address, addr.netmask)
            if entry:
                rows.append(entry)
    return _dedupe_adapters(rows)

def _adapters_ip_addr(active_only: bool = True) -> List[Dict[str, Any]]:
    if platform.system().lower() != "linux":
        return []
    cmd = ["ip", "-o", "-4", "addr", "show"]
    if active_only:
        cmd.append("up")
    try:
        txt = _subprocess_check_output_no_window(cmd, text=True, encoding="utf-8", errors="ignore")
    except Exception:
        return []
    rows: List[Dict[str, Any]] = []
    for line in txt.splitlines():
        m = re.match(r"\d+:\s+([^:\s]+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
        if not m:
            continue
        name, ip, prefix = m.groups()
        try:
            mask = str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)
        except Exception:
            continue
        entry = _adapter_scan_entry(name, ip, mask)
        if entry:
            rows.append(entry)
    return _dedupe_adapters(rows)

def _adapters_ifconfig(active_only: bool = True) -> List[Dict[str, Any]]:
    if platform.system().lower() not in ("darwin", "linux"):
        return []
    try:
        txt = _subprocess_check_output_no_window(["ifconfig"], text=True, encoding="utf-8", errors="ignore")
    except Exception:
        return []
    rows: List[Dict[str, Any]] = []
    for block in re.split(r"\n(?=\S)", txt):
        first = block.splitlines()[0] if block.splitlines() else ""
        name = first.split(":", 1)[0].strip()
        if not name:
            continue
        if active_only and "status: inactive" in block.lower():
            continue
        m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)\s+(?:netmask\s+)?(0x[0-9a-fA-F]+|\d+\.\d+\.\d+\.\d+)", block)
        if not m:
            continue
        ip, raw_mask = m.groups()
        if raw_mask.lower().startswith("0x"):
            try:
                mask_int = int(raw_mask, 16)
                mask = ".".join(str((mask_int >> shift) & 0xff) for shift in (24, 16, 8, 0))
            except Exception:
                continue
        else:
            mask = raw_mask
        entry = _adapter_scan_entry(name, ip, mask)
        if entry:
            rows.append(entry)
    return _dedupe_adapters(rows)

def _adapters_route_print() -> List[Dict[str, Any]]:
    if not platform.system().lower().startswith("win"):
        return []
    try:
        txt = _subprocess_check_output_no_window(["route", "print", "-4"], text=True, encoding="utf-8", errors="ignore")
    except Exception:
        return []
    rows: List[Dict[str, Any]] = []
    for line in txt.splitlines():
        m = re.match(r"\s*(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)\s+\S+\s+(\d+\.\d+\.\d+\.\d+)", line)
        if not m:
            continue
        network, mask, iface_ip = m.groups()
        try:
            net = ipaddress.IPv4Network(f"{network}/{mask}", strict=False)
            if net.prefixlen < 8 or net.prefixlen > 30:
                continue
        except Exception:
            continue
        entry = _adapter_scan_entry(f"iface {iface_ip}", iface_ip, str(net.netmask))
        if entry:
            rows.append(entry)
    return _dedupe_adapters(rows)

@APP.get("/api/adapters")
def api_adapters():
    include_all = str(request.args.get("all") or "").lower() in ("1", "true", "yes")
    try:
        rows: List[Dict[str, Any]] = []
        for loader in (_adapters_windows, _adapters_psutil, _adapters_netifaces, _adapters_ip_addr, _adapters_ifconfig):
            rows = loader(active_only=not include_all)
            if rows:
                break
        if not rows:
            rows = _adapters_route_print()
        return jsonify({"ok": True, "adapters": _sort_adapters(rows)})
    except Exception as e:
        return jsonify({"ok": True, "adapters": [], "note": "error: " + type(e).__name__}), 200

def _device_config_call(ip: str, config_get: str, timeout: float = 4.0):
    method = "TimeSync.Get" if config_get == "timezone" else config_get
    return _ws_call_auth(ip, method, {}, timeout)

def _read_ntp_settings(ip: str, timeout: float = 4.0) -> Dict[str, Any]:
    tz_resp = _device_config_call(ip, "timezone", timeout) or {}
    cfg = tz_resp.get("result") or {}
    if tz_resp.get("error") or not isinstance(cfg, dict):
        return {}
    ntp = cfg.get("ntp") if isinstance(cfg.get("ntp"), dict) else cfg
    server = ntp.get("ntpserver") or ntp.get("ntp_server") or ntp.get("server") or ""
    datetime_val = ""
    try:
        clock_resp = _ws_call_auth(ip, "SystemCurrentTime.Get", {}, timeout) or {}
        clock = clock_resp.get("result") or {}
        if isinstance(clock, dict):
            datetime_val = clock.get("datetime") or clock.get("date_time") or clock.get("time") or ""
    except Exception:
        pass
    return {
        "timezone": ntp.get("timezone") or cfg.get("timezone") or "UTC",
        "active_timezone": ntp.get("timezone") or cfg.get("timezone") or "UTC",
        "ntp_server": server,
        "ntpserver": server,
        "datetime": datetime_val,
        "device_datetime": datetime_val,
        "timezonelist": ntp.get("timezonelist") or [],
    }

@APP.post("/api/ntp_profile")
def api_ntp_profile():
    data = luma_request.json_object()
    requested_ips = data.get("ips") or []
    target_ips = [ip for ip in requested_ips if ip in cache_units] or list(cache_units)
    if not target_ips:
        return jsonify({"ok": True, "zones": [{"name": "UTC"}], "timezone": "UTC", "ntp_server": ""})

    for ip in target_ips:
        try:
            ntp_settings = _read_ntp_settings(ip)
            if ntp_settings:
                raw_zones = ntp_settings.get("timezonelist") or []
                zones = [zone if isinstance(zone, dict) else {"name": str(zone)} for zone in raw_zones]
                return jsonify({
                    "ok": True,
                    "source_ip": ip,
                    "zones": zones or [{"name": ntp_settings.get("timezone") or "UTC"}],
                    "timezone": ntp_settings.get("timezone") or "UTC",
                    "ntp_server": ntp_settings.get("ntp_server") or "",
                    "datetime": ntp_settings.get("datetime") or ntp_settings.get("device_datetime") or "",
                })
        except Exception as e:
            logging.debug(f"[ntp_profile] {ip}: {e}")
    return jsonify({"ok": False, "error": "Unable to read time settings from selected units", "devices": target_ips}), 502

@APP.post("/api/set_ntp")
def api_set_ntp():
    data = luma_request.json_object()
    scope = str(data.get("scope") or "selected").lower()
    timezone = str(data.get("timezone") or "").strip()
    server = str(data.get("server") or "").strip()
    requested_ips = data.get("ips") or []
    target_ips = list(cache_units) if scope == "all" else [ip for ip in requested_ips if ip in cache_units]
    if scope not in ("selected", "all") or not timezone or not target_ips:
        return jsonify({"ok": False, "error": "timezone and at least one target unit are required"}), 400

    results = {}
    for ip in target_ips:
        try:
            tz_resp = _device_config_call(ip, "timezone") or {}
            tz_cfg = tz_resp.get("result") or {}
            if tz_resp.get("error") or not isinstance(tz_cfg, dict):
                raise RuntimeError(tz_resp.get("error") or "timezone read failed")

            ntp_cfg = dict(tz_cfg.get("ntp") or {})
            ntp_cfg["ntpserver"] = server
            ntp_cfg["timezone"] = timezone
            tz_set = _ws_call_auth(ip, "TimeSync.Set", {
                "synctype": "ntp" if server else "manual",
                "ntp": ntp_cfg,
                "timezone": timezone,
            }, 6.0) or {}
            if tz_set.get("error"):
                raise RuntimeError(tz_set.get("error"))

            refreshed_ntp = _read_ntp_settings(ip, 4.0) or {
                "timezone": timezone,
                "active_timezone": timezone,
                "ntp_server": server,
                "ntpserver": server,
                "datetime": "",
                "device_datetime": "",
            }
            cache_units[ip]["timezone"] = refreshed_ntp.get("timezone", timezone)
            cache_units[ip]["active_timezone"] = refreshed_ntp.get("active_timezone", cache_units[ip]["timezone"])
            cache_units[ip]["ntp_server"] = refreshed_ntp.get("ntp_server", server)
            cache_units[ip]["ntpserver"] = refreshed_ntp.get("ntpserver", cache_units[ip]["ntp_server"])
            cache_units[ip]["datetime"] = refreshed_ntp.get("datetime", "")
            cache_units[ip]["device_datetime"] = refreshed_ntp.get("device_datetime", "")
            results[ip] = {"ip": ip, "ok": True}
        except Exception as e:
            results[ip] = {"ip": ip, "ok": False, "error": str(e)}

    _persist_units_cache()
    return jsonify({"ok": True, "scope": scope, "timezone": timezone, "ntp_server": server, "results": results})

def _collect_firmware_files() -> Tuple[List[Tuple[str,int]], List[str]]:
    checked: List[str] = []
    names: List[Tuple[str,int]] = []
    for folder in _firmware_search_roots():
        checked.append(folder)
        try:
            if os.path.isdir(folder):
                for fn in os.listdir(folder):
                    if fn.lower().endswith(".tar.gz"):
                        p = os.path.join(folder, fn)
                        try:
                            names.append((fn, int(os.path.getmtime(p))))
                        except Exception:
                            pass
        except Exception:
            pass
    return names, checked

@APP.get("/api/files")
def api_files():
    names, checked = _collect_firmware_files()
    # dedupe by filename (prefer latest mtime)
    best = {}
    for n, mt in names:
        if (n not in best) or (mt > best[n]): best[n] = mt
    rows = [{"name": n, "mtime": best[n]} for n in best]
    rows.sort(key=lambda x: -x["mtime"])
    if not luma_remote.is_local_request(request.environ):
        # File names only: a remote administrator is not told where on the host
        # computer the firmware folder is.
        return jsonify({"files": rows})
    return jsonify({
        "files": rows,
        "searched": checked,
        "base_dir": _firmware_search_roots()[0] if _firmware_search_roots() else os.path.abspath(FIRMWARE_DIR)
    })

def _selected_firmware_folder() -> Optional[str]:
    """The folder the operator chose in Settings, or None.

    A blank value, or a drive root (the stock default is a bare drive letter), is not a
    firmware folder: opening it would put an arbitrary part of the host's disk in front of
    the operator and call it theirs. Nothing is guessed from the application's own layout.
    """
    raw = CONFIG.get("firmware_path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = os.path.abspath(raw.strip())
    if os.path.dirname(path) == path:
        return None
    return path if os.path.isdir(path) else ""


def _open_folder_on_host(path: str) -> None:
    """Show a folder in the host's file manager. Argument lists only: the path is operator text."""
    system = platform.system()
    if system == "Windows":
        os.startfile(path)  # type: ignore[attr-defined]
    elif system == "Darwin":
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)


@APP.post("/api/open_firmware_folder")
def api_open_firmware_folder():
    """Open the selected firmware folder on the computer running LumaSuite (local requests only)."""
    chosen = _selected_firmware_folder()
    if chosen is None:
        return jsonify({"ok": False, "error": "No firmware folder is selected. Choose one in Settings > Firmware."}), 400
    if chosen == "":
        return jsonify({"ok": False, "error": "The selected firmware folder does not exist."}), 400
    try:
        _open_folder_on_host(chosen)
    except Exception as exc:
        logging.warning("[firmware-folder] could not open the selected folder: %s", exc)
        return jsonify({"ok": False, "error": "The folder could not be opened on this computer."}), 500
    logging.info("[firmware-folder] opened the selected firmware folder")
    return jsonify({"ok": True, "path": chosen})


# -------- FIRMWARE CHECK (non-destructive, Part 3.4) --------
# Reports firmware currency per device WITHOUT starting an upgrade. This
# only compares two things LumaSuite can actually establish: the device's
# own last-reported version (from the unit cache) and the version implied
# by filenames in the operator's configured local firmware folder -- the
# same folder/convention the upgrade flow already uses. It never contacts
# any vendor update service and never invents a state it cannot support.

def _parse_full_version_tuple(text):
    """'V1.0.2.6' -> (1, 0, 2, 6); arbitrary number of dot components."""
    if not text:
        return None
    m = re.search(r"v?(\d+(?:\.\d+){0,4})", str(text), re.IGNORECASE)
    if not m:
        return None
    try:
        return tuple(int(p) for p in m.group(1).split("."))
    except Exception:
        return None

def _extract_version_from_firmware_filename(fn):
    """Firmware filenames follow '<MODEL>-WHOLE-V<version>.tar.gz' (see the
    firmware/ directory). Require the version to be immediately preceded
    by a literal 'V'/'v' -- unlike a bare digit search, this cannot match
    digits that are part of the model name itself (e.g. the "4211" in
    "HW-LUMA-E4211-WHOLE-V1.0.2.3.tar.gz")."""
    m = re.search(r"[Vv](\d+(?:\.\d+){1,4})\b", fn)
    if not m:
        return None, None
    version_str = m.group(1)
    try:
        return version_str, tuple(int(p) for p in version_str.split("."))
    except Exception:
        return None, None

def _highest_local_firmware_version(model):
    """Among locally available firmware files whose name matches this
    device's model prefix, return (version_string, version_tuple) for the
    highest parseable version, or (None, None) if there are none / none
    parseable."""
    if not model:
        return None, None
    model_norm = str(model).strip().lower()
    names, _checked = _collect_firmware_files()
    best_str, best_tuple = None, None
    for fn, _mt in names:
        if not fn.lower().startswith(model_norm):
            continue
        version_str, parsed = _extract_version_from_firmware_filename(fn)
        if parsed is None:
            continue
        if best_tuple is None or parsed > best_tuple:
            best_tuple = parsed
            best_str = version_str
    return best_str, best_tuple

@APP.post("/api/firmware_check", endpoint="luma_api_firmware_check")
def api_firmware_check():
    """Body: {"ips": [...]}. For each IP, report firmware currency using
    only the cached unit record and the local firmware folder -- no device
    I/O, so this is safe to call freely and cannot slow discovery."""
    data = luma_request.json_object()
    ips = data.get("ips") or []
    if not isinstance(ips, list):
        return jsonify({"ok": False, "error": "invalid_ips"}), 400

    with cache_lock:
        by_ip = dict(cache_units)

    results = {}
    for ip in ips:
        unit = by_ip.get(ip)
        if not unit:
            results[ip] = {"state": "unknown", "detail": "device_not_in_cache"}
            continue
        current_raw = unit.get("version") or ""
        model = unit.get("model") or ""
        current_tuple = _parse_full_version_tuple(current_raw)
        if current_tuple is None:
            results[ip] = {"state": "unable_to_check", "current_version": current_raw or None, "detail": "current_version_unknown"}
            continue
        latest_str, latest_tuple = _highest_local_firmware_version(model)
        if latest_tuple is None:
            results[ip] = {"state": "unknown", "current_version": current_raw, "detail": "no_local_firmware_for_model"}
            continue
        if latest_tuple > current_tuple:
            results[ip] = {"state": "update_available", "current_version": current_raw, "latest_local_version": latest_str}
        else:
            results[ip] = {"state": "up_to_date", "current_version": current_raw, "latest_local_version": latest_str}

    return jsonify({"ok": True, "results": results})

# -------- HALL RESEARCH PUBLISHED-FIRMWARE STATUS (optional enrichment) -----
# CHECK + COMPARE + DISPLAY only -- never installs firmware, never affects
# POST /api/upgrade, and is a completely separate question from the
# non-destructive check above (api_firmware_check compares a device against
# firmware files the operator already downloaded locally; this compares a
# device against the version Hall Research has publicly published for that
# product family, over the internet). Keeping both: they answer different
# operator questions and neither makes the other redundant.
#
# Source: Hall Research's own first-party product API,
# https://hallresearch.com/api/products/<slug> -- same-origin HTTPS JSON,
# unauthenticated. Confirmed (2026-09-19, real network inspection of a
# rendered hallresearch.com product page) that the visible product page is a
# client-rendered shell ("Loading product...") that itself fetches this exact
# endpoint; there is no separate/more-static public source. Response shape is
# Prismic CMS content resolved to JSON; the field used here is
# data.firmware[]              (list, may be empty or absent)
#   .version[].text             concatenated -> plain version string, e.g. "1.0.2"
#   .release_date                "YYYY-MM-DD" or null
#   .firmware_link.url           NEVER read here -- this feature never treats
#                                 a Hall-supplied URL as an install source.
# Verified real content at audit time: hw-luma-e4211 -> one entry, "1.0.2"
# (2026-04-16); hw-luma-d4111 -> one entry, "1.0.2" (2026-04-16);
# at-ome-cs31-sa -> two entries, "1.0.2" (2026-04-16) and "1.0.1" (no date) --
# the highest parses as current. These are validation-time facts, not
# hard-coded anywhere below; the actual lookup always re-fetches from Hall.
HALL_FIRMWARE_API_BASE = "https://hallresearch.com/api/products/"
# (model prefix as reported by the device -- see cache_units[ip]["model"] --
# matched case-insensitively as a PREFIX so a variant suffix like the real
# "AT-OME-CS31-SA-HDBT" still maps to the base "at-ome-cs31-sa" product page,
# Hall product slug)
HALL_FIRMWARE_PRODUCT_MAP = (
    ("HW-LUMA-E4211", "hw-luma-e4211"),
    ("HW-LUMA-D4111", "hw-luma-d4111"),
    ("AT-OME-CS31-SA", "at-ome-cs31-sa"),
)
HALL_FIRMWARE_FETCH_TIMEOUT = (3, 5)  # (connect, read) seconds -- bounded, per product
HALL_FIRMWARE_MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # guard against a runaway/hostile response

_hall_firmware_lock = threading.Lock()
_hall_firmware_cache: Dict[str, Dict[str, Any]] = {}   # slug -> {"version": str, "release_date": str|None}
_hall_firmware_fetch_state = "pending"   # "pending" | "ready" | "unavailable"
_hall_firmware_fetched_at: Optional[float] = None
_hall_firmware_fetch_started = False
_hall_firmware_last_attempt: Optional[float] = None   # when a check last finished, successful or not
_hall_firmware_last_error: str = ""                   # short reason the last attempt found nothing

# Schedule. One check when the application launches, then one every seven days
# while it keeps running. A check that found nothing at all (offline, Hall down)
# is tried again after HALL_FIRMWARE_RETRY_INTERVAL rather than leaving a week
# with no answer; that is six requests-per-product a day at most, and never a
# loop. The last good answer is kept and shown, marked stale, meanwhile.
HALL_FIRMWARE_CHECK_INTERVAL = 7 * 24 * 3600
HALL_FIRMWARE_RETRY_INTERVAL = 6 * 3600
HALL_FIRMWARE_STATE_FILE = os.path.join(DATA_DIR, "firmware_status.json")
_hall_firmware_stop = threading.Event()


def _hall_slug_for_model(model: Optional[str]) -> Optional[str]:
    m = (model or "").strip().upper()
    if not m:
        return None
    for prefix, slug in HALL_FIRMWARE_PRODUCT_MAP:
        if m.startswith(prefix):
            return slug
    return None


def _fetch_hall_firmware_catalog():
    """Runs in a background daemon thread only (see
    _ensure_hall_firmware_lookup_started) -- never on the request path of any
    route, never at module import, so it can never fire during a
    hardware-fenced test import. Populates _hall_firmware_cache. This is
    optional internet enrichment: any failure (DNS, timeout, non-200,
    malformed JSON, missing firmware section) just leaves that one product's
    entry absent -- logged once at DEBUG, never raised, never surfaced to the
    operator as an error/warning/prompt."""
    global _hall_firmware_fetch_state, _hall_firmware_fetched_at
    results: Dict[str, Dict[str, Any]] = {}
    any_ok = False
    for _prefix, slug in HALL_FIRMWARE_PRODUCT_MAP:
        try:
            resp = requests.get(HALL_FIRMWARE_API_BASE + slug, timeout=HALL_FIRMWARE_FETCH_TIMEOUT)
            if resp.status_code != 200:
                continue
            if len(resp.content or b"") > HALL_FIRMWARE_MAX_RESPONSE_BYTES:
                continue
            payload = resp.json()
            entries = ((payload or {}).get("data") or {}).get("firmware") or []
            best_str, best_tuple, best_date = None, None, None
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                try:
                    version_blocks = entry.get("version") or []
                    text = "".join(
                        b.get("text", "") for b in version_blocks if isinstance(b, dict)
                    ).strip()
                except Exception:
                    text = ""
                parsed = _parse_full_version_tuple(text)
                if parsed is None:
                    continue
                if best_tuple is None or parsed > best_tuple:
                    best_tuple, best_str = parsed, text
                    best_date = entry.get("release_date")
            if best_str:
                results[slug] = {"version": best_str, "release_date": best_date, "checked_at": time.time()}
                any_ok = True
        except Exception as e:
            logging.debug(f"[hall-firmware] lookup failed for {slug}: {type(e).__name__}")
            continue
    global _hall_firmware_last_attempt, _hall_firmware_last_error
    now = time.time()
    with _hall_firmware_lock:
        # A product that did not answer this time keeps the last answer it gave:
        # an offline machine must not forget what it already knew.
        merged = dict(_hall_firmware_cache)
        merged.update(results)
        _hall_firmware_cache.clear()
        _hall_firmware_cache.update(merged)
        _hall_firmware_fetch_state = "ready" if any_ok else "unavailable"
        _hall_firmware_last_attempt = now
        if any_ok:
            _hall_firmware_fetched_at = now
            _hall_firmware_last_error = ""
        else:
            _hall_firmware_last_error = "Hall Research could not be reached"
    if any_ok:
        _save_hall_firmware_state()


def _save_hall_firmware_state():
    """Remember the last good answer, so a restart starts from it. Never raises."""
    try:
        with _hall_firmware_lock:
            body = {"version": 1, "fetched_at": _hall_firmware_fetched_at,
                    "catalog": {slug: dict(entry) for slug, entry in _hall_firmware_cache.items()}}
        luma_store.atomic_write_json(HALL_FIRMWARE_STATE_FILE, body)
    except Exception as exc:
        logging.debug(f"[hall-firmware] could not persist status: {type(exc).__name__}")


def _load_hall_firmware_state():
    """Load the remembered answer, validating every field. Anything that does not
    parse is ignored, never trusted: a damaged file means 'no memory', not a crash
    and not a made-up version."""
    global _hall_firmware_fetched_at, _hall_firmware_fetch_state
    try:
        data = luma_store.read_json(HALL_FIRMWARE_STATE_FILE, default=None)
    except Exception:
        return
    if not isinstance(data, dict) or not isinstance(data.get("catalog"), dict):
        return
    known = {slug for _prefix, slug in HALL_FIRMWARE_PRODUCT_MAP}
    loaded: Dict[str, Dict[str, Any]] = {}
    for slug, entry in data["catalog"].items():
        if slug not in known or not isinstance(entry, dict):
            continue
        version = entry.get("version")
        if not isinstance(version, str) or _parse_full_version_tuple(version) is None:
            continue
        date = entry.get("release_date")
        checked = entry.get("checked_at")
        loaded[slug] = {"version": version,
                        "release_date": date if isinstance(date, str) else None,
                        "checked_at": checked if isinstance(checked, (int, float)) else None}
    if not loaded:
        return
    fetched = data.get("fetched_at")
    with _hall_firmware_lock:
        if not _hall_firmware_cache:
            _hall_firmware_cache.update(loaded)
            _hall_firmware_fetched_at = fetched if isinstance(fetched, (int, float)) else None
            # Shown at once, marked as remembered until this launch's check answers.
            _hall_firmware_fetch_state = "cached"


#: Held for the whole of one check, scheduled or explicit, so two checks never overlap. Whoever
#: finds it held does not start another: the one running is the answer they were about to ask for.
_hall_check_gate = threading.Lock()
_hall_check_thread: Optional[threading.Thread] = None


def _run_hall_check_if_idle() -> bool:
    """One check under the gate. False (and nothing fetched) when a check is already running."""
    if not _hall_check_gate.acquire(blocking=False):
        return False
    try:
        _fetch_hall_firmware_catalog()
    finally:
        _hall_check_gate.release()
    return True


def start_explicit_hall_check() -> bool:
    """The operator pressed Check Now: one check, in the background, through the same fetch the
    schedule uses. False when one is already running. The request never waits on Hall Research."""
    global _hall_check_thread
    if _hall_check_gate.locked():
        return False
    worker = threading.Thread(target=_run_hall_check_if_idle, daemon=True, name="hall-firmware-now")
    _hall_check_thread = worker
    worker.start()
    return True


def _hall_firmware_loop():
    """Check at launch, then on the schedule above, until told to stop."""
    while not _hall_firmware_stop.is_set():
        if not _run_hall_check_if_idle():
            # An explicit check is already asking Hall Research; wait for it rather than asking twice,
            # then schedule from what it found.
            with _hall_check_gate:
                pass
        with _hall_firmware_lock:
            failed = _hall_firmware_fetch_state == "unavailable"
        wait = HALL_FIRMWARE_RETRY_INTERVAL if failed else HALL_FIRMWARE_CHECK_INTERVAL
        if _hall_firmware_stop.wait(wait):
            break


def _ensure_hall_firmware_lookup_started():
    """Idempotent lazy-start: the first call to GET /api/firmware_status (by
    any client) kicks off exactly one background fetch of all three product
    pages for the life of the process; every call after that is a no-op. This
    keeps the internet lookup off the module-import path entirely (so
    hardware-fenced tests that only `import lumaserver` never trigger it) and
    off the normal Flask startup path, while still satisfying "look this up
    once, early, not once per device/scan" without needing a second
    entrypoint-specific startup hook in app_launcher.py/__main__."""
    global _hall_firmware_fetch_started
    with _hall_firmware_lock:
        if _hall_firmware_fetch_started:
            return
        _hall_firmware_fetch_started = True
    _load_hall_firmware_state()
    threading.Thread(target=_hall_firmware_loop, daemon=True, name="hall-firmware").start()


def start_firmware_checks():
    """Begin the launch check and the seven-day schedule. Idempotent, never raises,
    never blocks: the work is one daemon thread, and nothing waits for it."""
    try:
        _ensure_hall_firmware_lookup_started()
    except Exception as exc:
        logging.warning("[hall-firmware] could not start: %s", type(exc).__name__)


@APP.get("/api/firmware_status")
def api_firmware_status():
    """Optional-enrichment endpoint: compares each currently cached device's
    own reported firmware version (device I/O already happened during normal
    discovery/scan -- none happens here) against Hall Research's publicly
    published version for its product family. Zero device I/O and zero
    BLOCKING network calls on this request: if the Hall lookup hasn't run yet
    this call starts it in the background and returns immediately with
    whatever is already known (possibly nothing yet)."""
    _ensure_hall_firmware_lookup_started()
    with cache_lock:
        by_ip = dict(cache_units)
    with _hall_firmware_lock:
        state = _hall_firmware_fetch_state
        fetched_at = _hall_firmware_fetched_at
        last_attempt = _hall_firmware_last_attempt
        last_error = _hall_firmware_last_error
        catalog = dict(_hall_firmware_cache)

    # The answer on screen is stale when the last check found nothing, or when
    # what we remember is older than a full schedule period.
    now = time.time()
    stale = bool(catalog) and (state in ("unavailable", "cached") or
                               (fetched_at is not None and now - fetched_at > HALL_FIRMWARE_CHECK_INTERVAL))
    attempted = last_attempt is not None

    results = {}
    per_model: Dict[str, Dict[str, Any]] = {}

    def _note_model(unit, entry):
        # One row per model for Settings. The most pressing state wins, so one unit that can be
        # updated is never hidden behind its neighbours.
        label = str(unit.get("model") or "").strip().upper()
        if not label:
            return
        row = per_model.setdefault(label, {"model": label, "units": 0, "installed": set(),
                                           "latest": "", "state": "up_to_date", "reason": ""})
        row["units"] += 1
        if entry.get("installed_version"):
            row["installed"].add(entry["installed_version"])
        if entry.get("published_version"):
            row["latest"] = entry["published_version"]
        rank = {"up_to_date": 0, "unknown": 1, "update_available": 2}
        if rank.get(entry.get("state"), 1) > rank.get(row["state"], 0):
            row["state"] = entry.get("state") or "unknown"
            row["reason"] = entry.get("reason") or ""

    for ip, unit in by_ip.items():
        if not isinstance(unit, dict):
            continue
        slug = _hall_slug_for_model(unit.get("model"))
        if not slug:
            continue          # a model Hall's catalogue here does not cover: nothing to say
        installed_raw = unit.get("version") or ""
        installed_tuple = _parse_full_version_tuple(installed_raw)
        published = catalog.get(slug)
        published_tuple = _parse_full_version_tuple((published or {}).get("version"))
        if published_tuple is None:
            # A mapped model with no published version yet. While the first check
            # is still running there is nothing to say; once a check has finished
            # and found nothing, say so truthfully rather than showing N/A.
            if attempted:
                results[ip] = {"installed_version": installed_raw, "state": "unknown",
                               "reason": "published_version_unavailable"}
            continue
        if installed_tuple is None:
            results[ip] = {"installed_version": installed_raw, "state": "unknown",
                           "reason": "installed_version_unreadable",
                           "published_version": published.get("version"),
                           "published_release_date": published.get("release_date")}
            continue
        results[ip] = {
            "installed_version": installed_raw,
            "published_version": published.get("version"),
            "published_release_date": published.get("release_date"),
            "checked_at": published.get("checked_at"),
            "stale": stale,
            # Intentional: installed >= published is Up to Date, not just
            # installed == published -- a dev/engineering unit may legitimately
            # carry firmware newer than Hall's current public release, and
            # that must never be reported as needing an update/downgrade.
            "state": "up_to_date" if installed_tuple >= published_tuple else "update_available",
        }

    for ip, entry in results.items():
        _note_model(by_ip.get(ip) or {}, entry)
    model_rows = []
    for label in sorted(per_model):
        row = dict(per_model[label])
        row["installed"] = sorted(row["installed"])
        model_rows.append(row)

    # Per product, so a page can say what the newest published release is even
    # for a product with no device on the network.
    models = {slug: {"version": entry.get("version"), "release_date": entry.get("release_date"),
                     "checked_at": entry.get("checked_at")} for slug, entry in catalog.items()}
    return jsonify({
        "ok": True,
        "hall_lookup": {"state": state, "fetched_at": fetched_at, "last_attempt": last_attempt,
                        "error": last_error, "stale": stale, "running": _hall_check_gate.locked(),
                        "interval_days": HALL_FIRMWARE_CHECK_INTERVAL // 86400},
        "results": results,
        "models": models,
        "model_rows": model_rows,
    })

@APP.post("/api/firmware_status/check")
def api_firmware_status_check():
    """Check Now: ask Hall Research once, in the background, and answer at once. 202 either way -- a
    check that was already running is the check being asked for. The page then reads
    /api/firmware_status, which is local, until `hall_lookup.running` clears. Nothing is installed."""
    _ensure_hall_firmware_lookup_started()
    started = start_explicit_hall_check()
    return jsonify({"ok": True, "started": started, "running": True}), 202


#: At most one background verification at a time, and at most this often.
CACHE_VERIFY_MIN_INTERVAL = float(os.environ.get("LUMA_CACHE_VERIFY_INTERVAL", "15"))
_cache_verify_lock = threading.Lock()
_cache_verify_state = {"running": False, "finished": -1e9}

@APP.get("/api/cache")
def api_cache():
    _merge_disk_into_memory()
    
    with cache_lock:
        rows = list(cache_units.values())
        # Whether each unit is answering, derived once here so every page agrees.
        served = [dict(u, online=not luma_projection.is_offline(u)) for u in rows]

    # Return cache immediately for fast page load
    resp = jsonify({"units": served})
    try:
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
    except Exception:
        pass
    
    # Verify hostnames asynchronously in background (don't block response)
    def verify_hostnames_async():
        try:
            for unit in rows:
                ip = unit.get("ip")
                if not ip:
                    continue
                try:
                    hostname = _ws_get_hostname(ip, min(1.0, CONFIG["TIMEOUT"]))
                    if hostname and hostname != unit.get("hostname"):
                        logging.info(f"[cache-verify] {ip}: hostname updated to '{hostname}'")
                        with cache_lock:
                            if ip in cache_units:
                                cache_units[ip]["hostname"] = hostname
                    role = (unit.get("role") or unit.get("type") or "").lower()
                    model = (unit.get("model") or "").lower()
                    is_encoder = role == "encoder" or model.startswith("hw-luma-e") or model.startswith("at-luma-e")
                    if is_encoder:
                        dante = _ws_get_dante_info(ip, min(1.0, CONFIG["TIMEOUT"]))
                        dante_power = _ws_get_dante_power_status(ip, min(1.0, CONFIG["TIMEOUT"]))
                        hdcp_val = _ws_get_hdcp_encoder(ip, min(1.0, CONFIG["TIMEOUT"]))
                        if dante.get("dante_name") or dante.get("dante_ip") or dante.get("dante_mac"):
                            with cache_lock:
                                if ip in cache_units:
                                    cache_units[ip]["dante_name"] = dante.get("dante_name", "")
                                    cache_units[ip]["dante_ip"] = dante.get("dante_ip", "")
                                    cache_units[ip]["dante_mac"] = dante.get("dante_mac", "")
                                    cache_units[ip]["dante_enabled"] = dante_power
                                    if hdcp_val:
                                        cache_units[ip]["hdcp"] = hdcp_val
                    is_decoder = role == "decoder" or model.startswith("hw-luma-d") or model.startswith("at-luma-d")
                    if is_decoder:
                        hdcp_val = _ws_get_hdcp_decoder(ip, min(1.0, CONFIG["TIMEOUT"]))
                        if hdcp_val:
                            with cache_lock:
                                if ip in cache_units:
                                    cache_units[ip]["hdcp"] = hdcp_val
                except Exception:
                    pass
            # Save if any hostname was updated
            try:
                _persist_units_cache()
            except Exception:
                pass
        except Exception as e:
            logging.warning(f"[cache-verify-bg] error: {e}")
    
    # One verification at a time, and not more often than the minimum interval:
    # several pages (or several people) asking for the cache must not each start
    # a sweep of every device. The flag is released on every path out.
    with _cache_verify_lock:
        start = (not _cache_verify_state["running"]
                 and time.monotonic() - _cache_verify_state["finished"] >= CACHE_VERIFY_MIN_INTERVAL)
        if start:
            _cache_verify_state["running"] = True

    if start:
        def _verify_worker():
            try:
                verify_hostnames_async()
            finally:
                with _cache_verify_lock:
                    _cache_verify_state["running"] = False
                    _cache_verify_state["finished"] = time.monotonic()
        threading.Thread(target=_verify_worker, daemon=True).start()

    return resp

@APP.get("/api/heartbeat")
def api_heartbeat():
    """Simple heartbeat endpoint for connection monitoring.
    Returns immediately with minimal overhead.
    Used by browser to detect if server is reachable."""
    return jsonify({"ok": True, "timestamp": time.time()})

def _unit_label(unit) -> str:
    identity = luma_projection.unit_identity(unit)
    base = unit.get("hostname") or unit.get("model") or "Unnamed device"
    return REMOTE.aliases.display_name(identity, base) if identity else base


def _forget_devices(identities):
    """Remove devices from every store that refers to them, all-or-nothing.

    Accounts' source/display grants, display-group members and friendly names are
    three files. They are updated one after another, each atomically, and if any
    step fails the ones already done are put back -- so a failure leaves the
    application as it was, never a device that is gone from one place and still
    assigned in another. Returns (result, undo); result["ok"] is False on failure.
    """
    ids = {i for i in identities if i}
    users = REMOTE.store.forget_devices(ids)
    if not users["ok"]:
        return {"ok": False}, (lambda: None)
    members = REMOTE.groups.forget_members(ids)

    def undo_users():
        REMOTE.store.restore_permissions(users["before"])

    if not members["ok"]:
        undo_users()
        return {"ok": False}, (lambda: None)
    names = REMOTE.aliases.forget_many(ids)

    def undo_groups():
        REMOTE.groups.restore(members["before"])

    if not names["ok"]:
        undo_groups()
        undo_users()
        return {"ok": False}, (lambda: None)

    def undo():
        REMOTE.aliases.restore(names["before"])
        undo_groups()
        undo_users()

    return ({"ok": True, "accounts": users["changed"], "groups": members["changed"],
             "names": names["removed"]}, undo)


def _walls_directory():
    """The saved walls as the remote layer reads them (stateless, read as they are now)."""
    return luma_walls.WallDirectory(_wall_directory, _vw_device_id)


def _inventory_assignments():
    """identity -> who depends on it, from the stores as they are now.

    The single derivation behind Clear Units, View Assignments and the Device names page.
    """
    with cache_lock:
        units = list(cache_units.values())
    return luma_inventory.build(units, REMOTE.store, REMOTE.groups, _walls_directory())


def _clear_full():
    """Remote Access is off: clear everything and everything that pointed into it."""
    global cache_generation
    with cache_lock:
        known = {luma_projection.unit_identity(u) for u in cache_units.values()}
    # Grants that already point at nothing are ghosts of an earlier clear or an
    # address that never came back; an emptied inventory leaves none of them.
    known.update(REMOTE.store.assigned_devices())
    known.update(REMOTE.groups.referenced_identities())
    known.update(REMOTE.aliases.all())
    result, undo = _forget_devices(known)
    if not result["ok"]:
        logging.warning("[clear_units] could not update accounts and groups; nothing cleared")
        return jsonify({"ok": False, "error": "Accounts and display groups could not be "
                                              "updated, so nothing was cleared."}), 500
    with cache_lock:
        before_units = dict(cache_units)
        cache_generation += 1
        cache_units.clear()
        if not luma_store.atomic_write_json(CACHE_FILE, []):
            cache_units.update(before_units)
            undo()
            logging.warning("[clear_units] failed to reset file; assignments restored")
            return jsonify({"ok": False, "error": "failed to persist"}), 500
        logging.info("[clear_units] cleared %d", len(before_units))
    try:
        REMOTE.store.audit("units.cleared", actor=getattr(g, "principal", None),
                           detail={"cleared": len(before_units),
                                   "accounts_updated": result["accounts"],
                                   "groups_updated": result["groups"],
                                   "names_removed": result["names"]})
    except Exception:
        logging.warning("[clear_units] audit entry could not be written")
    return jsonify({"ok": True, "scope": "all", "cleared": len(before_units), "kept": 0,
                    "accounts_updated": result["accounts"],
                    "groups_updated": result["groups"],
                    "names_removed": result["names"]})


def _clear_unassigned():
    """Remote Access is on: remove only the units nothing refers to.

    The classification and the removal happen under the account, group and wall locks
    together, so a grant, a group member or a wall cell added while this runs either
    lands first (and its device is kept) or waits until the devices are gone. Nothing
    Remote Access holds is edited: no grant, member, wall or account changes, and a
    kept unit stays exactly as it was. If the inventory file cannot be written the
    removal is undone.
    """
    global cache_generation
    with REMOTE.store.hold(), REMOTE.groups.hold(), luma_store.lock_for(VIDEO_WALL_STATE_FILE):
        with cache_lock:
            units_now = list(cache_units.values())
        assigned = luma_inventory.build(units_now, REMOTE.store, REMOTE.groups,
                                        _walls_directory())
        with cache_lock:
            kept, doomed = luma_inventory.split(cache_units, assigned)
            doomed_identities = {luma_projection.unit_identity(cache_units[ip]) for ip in doomed}
            doomed_identities.discard("")
            names = {"ok": True, "removed": 0, "before": {}}
            # Nothing to remove is not a change: it must not move the generation, because
            # that discards a scan that is still running for no reason, and must not rewrite
            # the inventory file.
            if doomed:
                names = REMOTE.aliases.forget_many(doomed_identities)
                if not names["ok"]:
                    logging.warning("[clear_units] device names could not be updated; nothing cleared")
                    return jsonify({"ok": False, "error": "Device names could not be updated, "
                                                          "so nothing was cleared."}), 500
                before_units = dict(cache_units)
                cache_generation += 1
                for ip in doomed:
                    cache_units.pop(ip, None)
                if not luma_store.atomic_write_json(CACHE_FILE, list(cache_units.values())):
                    cache_units.clear()
                    cache_units.update(before_units)
                    REMOTE.aliases.restore(names["before"])
                    logging.warning("[clear_units] failed to write the inventory; nothing cleared")
                    return jsonify({"ok": False, "error": "failed to persist"}), 500
            logging.info("[clear_units] cleared %d unassigned, kept %d assigned",
                         len(doomed), len(kept))
    try:
        REMOTE.store.audit("units.cleared_unassigned", actor=getattr(g, "principal", None),
                           detail={"cleared": len(doomed), "kept": len(kept),
                                   "names_removed": names["removed"]})
    except Exception:
        logging.warning("[clear_units] audit entry could not be written")
    return jsonify({"ok": True, "scope": "unassigned", "cleared": len(doomed),
                    "kept": len(kept), "accounts_updated": 0, "groups_updated": 0,
                    "names_removed": names["removed"]})


def _assignment_row(unit, identity, record):
    """One protected unit as an administrator reads it: names, type and what holds it.

    Shared by View Assignments and by Remove Selected, so both say the same thing about
    the same unit. Carries names an administrator can already see; no password or token.
    """
    device_name = unit.get("hostname") or unit.get("name") or identity
    return {"id": identity, "name": REMOTE.aliases.display_name(identity, device_name),
            "friendly_name": REMOTE.aliases.alias_for(identity),
            "device_name": device_name,
            "type": {"source": "Encoder", "display": "Decoder"}.get(
                luma_projection.unit_kind(unit), "Other"),
            **record}


@APP.get("/api/clear_units/assignments")
def api_clear_units_assignments():
    """What Clear Units would keep, and why, for the confirmation and View Assignments.

    Read-only and derived from the same function the clear itself uses. Carries names
    an administrator can already see; no password, token or address.
    """
    remote_on = bool(REMOTE.enabled())
    assigned = _inventory_assignments()
    with cache_lock:
        rows = list(cache_units.values())
    kept, doomed = [], 0
    for unit in rows:
        identity = luma_projection.unit_identity(unit)
        record = assigned.get(identity) if identity else None
        if record is None:
            doomed += 1
            continue
        kept.append(_assignment_row(unit, identity, record))
    kept.sort(key=lambda r: (r["name"].lower(), r["id"]))
    return jsonify({"ok": True, "remote_access": remote_on,
                    "protected": len(kept) if remote_on else 0,
                    "clearable": doomed if remote_on else len(rows),
                    "units": kept if remote_on else []})


@APP.post("/api/clear_units")
def api_clear_units():
    """Clear the discovered inventory.

    Remote Access off: everything goes, with every grant, group member and name that
    pointed into it. Remote Access on: only units nothing refers to go; assigned units
    stay, and nothing Remote Access holds changes. The server decides from its own state
    and the request body is not consulted, so a manipulated request cannot remove an
    assigned unit.
    """
    if REMOTE.enabled():
        return _clear_unassigned()
    return _clear_full()


def _remove_selected_unassigned(ips_to_remove):
    """Remote Access is on: remove the selected units nothing refers to, keep the rest.

    The same decision as Clear Units, made the same way: classified and removed under the
    account, group and wall locks together, so an assignment added while this runs either
    lands first (and its unit stays) or waits until the units are gone. Nothing Remote
    Access holds is edited; a protected unit is reported with what holds it.
    """
    global cache_generation
    wanted = [ip for ip in ips_to_remove if isinstance(ip, str)]
    with REMOTE.store.hold(), REMOTE.groups.hold(), luma_store.lock_for(VIDEO_WALL_STATE_FILE):
        with cache_lock:
            units_now = list(cache_units.values())
        assigned = luma_inventory.build(units_now, REMOTE.store, REMOTE.groups,
                                        _walls_directory())
        with cache_lock:
            selected = {ip: cache_units[ip] for ip in wanted if ip in cache_units}
            doomed, protected = [], []
            for ip in sorted(selected):
                identity = luma_projection.unit_identity(selected[ip])
                record = assigned.get(identity) if identity else None
                if record is None:
                    doomed.append(ip)
                else:
                    protected.append(_assignment_row(selected[ip], identity, record))
            protected.sort(key=lambda r: (r["name"].lower(), r["id"]))
            # A unit that stays under another address keeps its name: identity is the
            # hardware, not the address that was removed.
            staying = {luma_projection.unit_identity(u) for ip, u in cache_units.items()
                       if ip not in doomed}
            doomed_identities = ({luma_projection.unit_identity(selected[ip]) for ip in doomed}
                                 - staying)
            doomed_identities.discard("")
            removed_names = 0
            if doomed:
                names = REMOTE.aliases.forget_many(doomed_identities)
                if not names["ok"]:
                    logging.warning("[remove_units] device names could not be updated; "
                                    "nothing removed")
                    return jsonify({"ok": False, "error": "Device names could not be "
                                                          "updated, so nothing was removed."}), 500
                before_units = dict(cache_units)
                cache_generation += 1
                for ip in doomed:
                    cache_units.pop(ip, None)
                if not luma_store.atomic_write_json(CACHE_FILE, list(cache_units.values())):
                    cache_units.clear()
                    cache_units.update(before_units)
                    REMOTE.aliases.restore(names["before"])
                    logging.warning("[remove_units] failed to write the inventory; "
                                    "nothing removed")
                    return jsonify({"ok": False, "error": "failed to persist"}), 500
                removed_names = names["removed"]
    if doomed or protected:
        try:
            REMOTE.store.audit("units.removed_unassigned", actor=getattr(g, "principal", None),
                               detail={"removed": len(doomed), "protected": len(protected),
                                       "names_removed": removed_names})
        except Exception:
            logging.warning("[remove_units] audit entry could not be written")
    logging.info("[remove_units] removed %d unassigned, protected %d", len(doomed), len(protected))
    body = {"ok": True, "scope": "selected", "removed": len(doomed),
            "protected": len(protected), "kept": protected, "names_removed": removed_names}
    if protected and not doomed:
        body.update({"ok": False, "reason": "inventory_protected",
                     "error": "Remote Access is using every selected unit, so none was removed. "
                              "Remove the assignment, or turn Remote Access off in Settings."})
        return jsonify(body), 409
    return jsonify(body)


@APP.post("/api/remove_units")
def api_remove_units():
    """Remove selected units from the inventory and from everything that names them.

    Remote Access off: as it always was. Remote Access on: the same protection as Clear
    Units. Units an account, group or saved wall refers to stay and are reported with why;
    the rest are removed, and nothing Remote Access holds is edited.
    """
    global cache_generation
    data = luma_request.json_object()
    data = data if isinstance(data, dict) else {}
    ips_to_remove = data.get("ips") or []
    if not ips_to_remove or not isinstance(ips_to_remove, list):
        return jsonify({"ok": False, "error": "ips required"}), 400
    if REMOTE.enabled():
        return _remove_selected_unassigned(ips_to_remove)
    try:
        with cache_lock:
            doomed = {ip: cache_units[ip] for ip in ips_to_remove
                      if isinstance(ip, str) and ip in cache_units}
        # A device that stays in the inventory under another address keeps its
        # grants: identity is the hardware, not the address that was removed.
        with cache_lock:
            staying = {luma_projection.unit_identity(u) for ip, u in cache_units.items()
                       if ip not in doomed}
        identities = {luma_projection.unit_identity(u) for u in doomed.values()} - staying
        result, undo = _forget_devices(identities)
        if not result["ok"]:
            return jsonify({"ok": False, "error": "Accounts and display groups could not "
                                                  "be updated, so nothing was removed."}), 500
        with cache_lock:
            cache_generation += 1
            for ip in doomed:
                cache_units.pop(ip, None)
        # Persist updated cache
        _persist_units_cache()
        try:
            REMOTE.store.audit("units.cleared", actor=getattr(g, "principal", None),
                               detail={"cleared": len(doomed),
                                       "accounts_updated": result["accounts"],
                                       "groups_updated": result["groups"],
                                       "names_removed": result["names"]})
        except Exception:
            logging.warning("[remove_units] audit entry could not be written")
        logging.info(f"[remove_units] removed {len(doomed)}/{len(ips_to_remove)} units")
        return jsonify({"ok": True, "removed": len(doomed), "kept": []})
    except Exception as e:
        logging.error(f"[remove_units] error: {e}")
        return luma_request.internal_error(e)

@APP.route("/api/config", methods=["GET","POST"])
def api_config():
    # GET: return a minimal view of current config
    if request.method == "GET":
        pw = str(CONFIG.get("password",""))
        pw_hash = hashlib.sha256(pw.encode("utf-8")).hexdigest() if pw else ""
        include_pw = request.args.get("include_password") in ("1","true","True")
        if include_pw and not luma_remote.is_local_request(request.environ):
            # Plaintext device passwords never leave the computer LumaSuite runs
            # on, whatever the role asking. This is the narrow local-only door for
            # the one operation that needs them (the Settings eye toggle).
            return jsonify({"ok": False, "reason": "local_only",
                            "error": "Device passwords can only be shown on the "
                                     "computer running LumaSuite."}), 403
        resp = {
            "username": CONFIG.get("username"),
            # do not return sensitive headers or extra headers here
            "logos_path": CONFIG.get("logos_path"),
            "imagestream_path": CONFIG.get("imagestream_path"),
            "firmware_path": CONFIG.get("firmware_path"),
            "ws_port": CONFIG.get("ws_port", 80),
            # An unsalted hash of a password from a small alphabet is a brute-force
            # target; nothing in the product reads this field, and a remote caller has
            # no use for it. Blank, not removed: the shape of the answer is the API.
            "password_hash": pw_hash if luma_remote.is_local_request(request.environ) else "",
            "is_default_password": pw == "password",
            "has_fallback_password": bool(str(CONFIG.get("fallback_password", "")).strip()),
            # Additional networks scanned along with the selected adapter's.
            "scan_ranges": list(CONFIG.get("scan_ranges") or []),
        }
        if include_pw:
            resp["password"] = pw
            resp["fallback_password"] = str(CONFIG.get("fallback_password", ""))
        return jsonify(resp)

    # POST: accept updates for ports, creds and asset paths
    data = luma_request.json_object()
    if not isinstance(data, dict):
        data = {}
    if not luma_remote.is_local_request(request.environ):
        # A folder on the host's own disk is chosen on the host. A signed-in remote
        # session that could name one would turn the asset route into a way to read
        # any folder -- this application's own configuration and account files
        # included. The page round-trips the folders it was shown, so an unchanged
        # value is an ordinary save; a different one is refused before anything in
        # this request is applied.
        for key in ("logos_path", "imagestream_path", "firmware_path"):
            wanted = str(data.get(key) or "").strip()
            if wanted and os.path.abspath(wanted) != os.path.abspath(str(CONFIG.get(key) or "")):
                return jsonify({"ok": False, "reason": "local_only",
                                "error": "Folders can only be changed on the computer "
                                         "running LumaSuite."}), 403
    if "scan_ranges" in data:
        try:
            CONFIG["scan_ranges"] = luma_scanplan.normalise(data["scan_ranges"])
        except luma_scanplan.ScanRangeError as refusal:
            return jsonify({"ok": False, "reason": refusal.reason, "error": refusal.message}), 400
    if "ws_port" in data:
        try:
            CONFIG["ws_port"] = int(data["ws_port"])
        except Exception:
            pass
    if "username" in data:
        try:
            CONFIG["username"] = str(data["username"]).strip()
        except Exception:
            pass
    if "password" in data:
        try:
            candidate_password = str(data["password"]).strip()
            if candidate_password and not _is_allowed_password(candidate_password):
                return jsonify({
                    "ok": False,
                    "error": "invalid_password",
                    "details": "Password must be at least 6 characters and may only include A-Z, a-z, 0-9, and !@#$%^&*",
                }), 400
            if candidate_password != CONFIG.get("password"):
                CONFIG["password"] = candidate_password
                # Every cached "primary"/"fallback" slot label was resolved
                # against the OLD Primary value -- it may no longer be
                # accurate now that Primary changed, so drop it rather
                # than let calls keep trusting a stale label.
                _invalidate_credential_cache("primary_password_changed")
        except Exception:
            pass
    if "fallback_password" in data:
        try:
            candidate_fallback = str(data["fallback_password"]).strip()
            # Empty is allowed here -- it means "no Fallback Password
            # configured", not "clear it to something invalid".
            if candidate_fallback and not _is_allowed_password(candidate_fallback):
                return jsonify({
                    "ok": False,
                    "error": "invalid_fallback_password",
                    "details": "Fallback Password must be at least 6 characters and may only include A-Z, a-z, 0-9, and !@#$%^&*",
                }), 400
            if candidate_fallback != CONFIG.get("fallback_password"):
                CONFIG["fallback_password"] = candidate_fallback
                _invalidate_credential_cache("fallback_password_changed")
        except Exception:
            pass

    # Validate and apply asset path updates: only persist if they exist and are directories
    invalid = []
    for k in ("logos_path", "imagestream_path", "firmware_path"):
        if k in data:
            v = data.get(k) or ""
            v = v.strip()
            if not v:
                # empty -> ignore (do not clobber existing)
                continue
            abs_v = os.path.abspath(v)
            if not os.path.isdir(abs_v):
                invalid.append({"key": k, "path": v})
            else:
                CONFIG[k] = abs_v

    if invalid:
        return jsonify({"ok": False, "error": "invalid_paths", "details": invalid}), 400

    # Save persisted config after successful validation (if any keys set)
    _save_persisted_config()

    return jsonify({"ok": True, "config": {
        "logos_path": CONFIG.get("logos_path"),
        "imagestream_path": CONFIG.get("imagestream_path"),
        "firmware_path": CONFIG.get("firmware_path"),
        # Do not echo password back here for safety
    }})

# -------- FAST PARALLEL SCAN --------
def _is_empty_value(v):
    """Check if a value should be considered empty/null for retry purposes."""
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    if v == "":
        return True
    return False

CLEARABLE_SCAN_FIELDS = {"ntp_server", "ntpserver"}

def _has_empty_values(obj, max_depth=3, current_depth=0):
    """Recursively check if a nested structure contains empty/null values (used for retry decision)."""
    if current_depth >= max_depth:
        return False
    if _is_empty_value(obj):
        return True
    if isinstance(obj, dict):
        for v in obj.values():
            if _has_empty_values(v, max_depth, current_depth + 1):
                return True
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            if _has_empty_values(item, max_depth, current_depth + 1):
                return True
    return False

def _deep_merge_unit(existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deep merge new unit data into existing unit data.
    - Top-level fields are updated if present in 'new'
    - For nested dicts (enc, dec, etc.), recursively merge
    - Only update a field if the new value is not empty
    - Preserve existing values if new value is empty/null
    Returns the merged dict.
    """
    merged = dict(existing)  # Start with existing
    
    for key, new_val in new.items():
        if key not in existing:
            # New key, add it
            merged[key] = new_val
        else:
            # Key exists in both
            existing_val = existing[key]
            
            # If both are dicts, recursively merge
            if isinstance(existing_val, dict) and isinstance(new_val, dict):
                merged[key] = _deep_merge_unit(existing_val, new_val)
            # If new value is not empty, use it; otherwise keep existing.
            # Some device fields are legitimately blank and must clear stale cache.
            elif key in CLEARABLE_SCAN_FIELDS or not _is_empty_value(new_val):
                merged[key] = new_val
            # else: new_val is empty, keep existing_val

    return merged


def _mac_index_of_cache():
    """canonical MAC -> the address the cache holds it under. The caller holds `cache_lock`."""
    index = {}
    for cached_ip, cached_unit in cache_units.items():
        cached_mac = luma_projection.unit_identity(cached_unit)
        if cached_mac:
            index[cached_mac] = cached_ip
    return index


def _ingest_scanned_unit(ip, info, mac_to_ip_index):
    """Fold one scanned unit into `cache_units`. The caller holds `cache_lock`.

    The cache is keyed by address but a unit is *identified* by its MAC, and a Display
    Group, a User's grant and a friendly name all follow the MAC. Merging a scan into the
    wrong row therefore attaches one display's identity to another display's hardware.
    Two ways that used to happen, both fixed here:

    * A different device now answers at an address the cache held for another MAC. The
      scan's empty MAC (a read that failed) is "no new information" to the deep merge, so
      the old MAC survived onto the new device and every route for that identity went to
      the wrong display. A row is merged only when the scan can be shown to describe the
      same device: same MAC, or no MAC but the same serial number. A different MAC or serial
      starts a fresh row; an unprovable one leaves the row exactly as it was.
    * `mac_to_ip_index` was built once before the scan and never corrected when a row it
      pointed at was replaced, so a later sighting of the first device deleted the row of
      the device that had taken its address. The index is only believed when the row it
      names still carries that MAC.

    Returns {"action": "updated"|"added"|"skipped", "moved_from": ip|None, "mac": str}.
    """
    mac_new = luma_projection.unit_identity(info)
    held = cache_units.get(ip)
    held_mac = luma_projection.unit_identity(held) if isinstance(held, dict) else ""
    existing_unit, moved_from = ({} if held is None else held), None
    existed = held is not None

    if mac_new:
        old_ip = mac_to_ip_index.get(mac_new)
        old_row = cache_units.get(old_ip) if old_ip and old_ip != ip else None
        if old_row is not None and luma_projection.unit_identity(old_row) == mac_new:
            existing_unit, existed, moved_from = old_row, True, old_ip
            del cache_units[old_ip]
        elif held_mac and held_mac != mac_new:
            existing_unit, existed = {}, False        # another device's row: not this one's
        # A stale index entry for the device that used to be here no longer points here.
        if held_mac and held_mac != mac_new and mac_to_ip_index.get(held_mac) == ip:
            mac_to_ip_index.pop(held_mac, None)
    elif held_mac:
        serial_new = str(info.get("serialnumber") or "").strip().lower()
        serial_held = str(held.get("serialnumber") or "").strip().lower()
        if serial_new and serial_held and serial_new != serial_held:
            existing_unit, existed = {}, False
            if mac_to_ip_index.get(held_mac) == ip:
                mac_to_ip_index.pop(held_mac, None)
            logging.warning("[scan-fast] %s answers with a different serial number; "
                            "its cached identity is not carried over", ip)
        elif not (serial_new and serial_held):
            logging.warning("[scan-fast] %s answered without a hardware address and cannot be "
                            "shown to be the unit cached there; the row is left as it was", ip)
            return {"action": "skipped", "moved_from": None, "mac": ""}

    if existed:
        # The scan has just read this device and read its own hardware address off it, and
        # that address is the unit already on record: a fresh answer from that unit, newer
        # than the refreshes that missed it. It clears the miss count at once rather than
        # leaving a reachable display refused as offline until the next matrix refresh.
        # Proof is the exact MAC only: a scan that read none, or a different one, clears
        # nothing, and a unit that really is down is not scanned so is still refused.
        answered_as_itself = bool(mac_new) and luma_projection.unit_identity(existing_unit) == mac_new
        cache_units[ip] = _deep_merge_unit(existing_unit, info)
        if answered_as_itself:
            luma_liveness.record(cache_units[ip], True)
        _replace_net_status(cache_units[ip], info)
        if isinstance(info.get("thumbnail_enabled"), bool):
            cache_units[ip]["thumbnail_enabled"] = info["thumbnail_enabled"]
        action = "updated"
    else:
        cache_units[ip] = info
        action = "added"
    if mac_new:
        mac_to_ip_index[mac_new] = ip
    return {"action": action, "moved_from": moved_from, "mac": mac_new}

def _scan_one(ip: str) -> Optional[Dict[str, Any]]:
    """
    Scan a single IP for unit details. Retries up to 2 additional times if critical values are empty/null.
    Returns unit dict on success, error dict with _error key on auth failure, or None on other failures.
    """
    # quick reachability check
    try:
        if not (_tcp_probe(ip,80) or _tcp_probe(ip,443)):
        # Note: Authentication errors are already logged in _ws_get_minimal
            return None
    except Exception:
        # If probes not available for any reason, continue and let ws call decide
        pass

    base = _ws_get_minimal(ip)
    if not base:
        return None
    
    # If this is an error response, log what fields we DO have and return it
    if "_error" in base:
        logging.error(f"[scan_one] {ip} auth failure - Available fields: {list(base.keys())}")
        logging.error("[scan_one] %s error: %s", ip, base.get("_error"))
        return base

    role = (base.get("type") or base.get("role") or "").strip().lower()
    timeout = globals().get("SCAN_TIMEOUT", 2.0)

    is_encoder = "encoder" in role
    is_decoder_like = ("decoder" in role) or ("switcher" in role)

    # Fallback role probe for devices with non-standard model strings or missing role hints.
    # Prefer protocol capabilities over naming heuristics.
    if not is_encoder and not is_decoder_like:
        try:
            enc_probe = _ws_call_auth(ip, "VideoMultiIpStream.Get", {}, timeout)
            if isinstance(enc_probe, dict) and "result" in enc_probe and isinstance(enc_probe.get("result"), dict):
                base["type"] = "encoder"
                base["role"] = "Encoder"
                role = "encoder"
                is_encoder = True
                logging.info(f"[scan_one] {ip} role inferred by encoder probe")
        except Exception as e:
            logging.debug(f"[scan_one] {ip} encoder probe failed: {e}")

    if not is_encoder and not is_decoder_like:
        try:
            dec_probe = _ws_call_auth(ip, "SelectVideoStream.Get", {"password": CONFIG.get("password", "password")}, timeout)
            if isinstance(dec_probe, dict) and "result" in dec_probe:
                base["type"] = "decoder"
                base["role"] = "Decoder"
                role = "decoder"
                is_decoder_like = True
                logging.info(f"[scan_one] {ip} role inferred by decoder probe")
        except Exception as e:
            logging.debug(f"[scan_one] {ip} decoder probe failed: {e}")

    # Fetch encoder/decoder details with retry logic
    max_retries = 2  # Allow up to 2 additional attempts
    attempt = 0
    while attempt <= max_retries:
        try:
            if is_encoder:
                base["enc"] = _query_encoder_details(ip, timeout)
            elif is_decoder_like:
                base["dec"] = _query_decoder_details(ip, timeout)
        except Exception as e:
            logging.warning(f"[scan-one] details error {ip} (attempt {attempt+1}) {e}")

        # Check if we got critical empty values
        has_empty = False
        if is_encoder and "enc" in base:
            has_empty = _has_empty_values(base.get("enc", {}))
        elif is_decoder_like and "dec" in base:
            has_empty = _has_empty_values(base.get("dec", {}))
        
        # If no empty values or we've exhausted retries, break
        if not has_empty or attempt >= max_retries:
            break
        
        attempt += 1
        logging.debug(f"[scan-one] {ip} retry {attempt}/{max_retries} due to empty values")
        # Small delay before retry to avoid hammering
        time.sleep(0.1)

    # AV LAN link speed and LLDP neighbour: one read, taken while the unit is already being read.
    # A unit that does not answer, or answers oddly, leaves what the cache already knows alone.
    try:
        net = luma_netstatus.read(_ws_call_auth, ip, role, timeout)
    except Exception as exc:
        logging.debug("[scan-one] %s net status unavailable: %s", ip, type(exc).__name__)
        net = None
    if net:
        base["net"] = net

    # The unit's own thumbnail switch, read while the unit is already being read. Left out when the
    # unit does not answer, so a failed read never erases a verified value.
    try:
        if luma_thumbnail.supported(role, base.get("model")):
            thumb = luma_thumbnail.read(_ws_call_auth, ip, timeout)
            if thumb is not None:
                base["thumbnail_enabled"] = thumb
    except Exception as exc:
        logging.debug("[scan-one] %s thumbnail switch unavailable: %s", ip, type(exc).__name__)

    return base


def _replace_net_status(cached: Dict[str, Any], info: Dict[str, Any]) -> None:
    """A successful link read replaces the cached one outright.

    The ordinary merge keeps an old value whenever the new one is empty, which would keep a
    neighbour that is no longer there and a speed the link no longer has.
    """
    if isinstance(info.get("net"), dict):
        cached["net"] = copy.deepcopy(info["net"])


@APP.post("/api/thumbnail")
def api_thumbnail():
    """Turn a unit's own thumbnail on or off, and say what the unit actually holds afterwards.

    The unit is addressed by IP but identified by MAC: the recorded MAC must be the one sent, so a
    unit that moved to another address is never written by mistake. Success is the read-back.
    """
    data = luma_request.json_object()
    ip = str(data.get("ip") or "").strip()
    enable = data.get("enable")
    mac = _normalize_mac(str(data.get("mac") or ""))
    if not isinstance(enable, bool) or not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", ip) or not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
        return jsonify({"ok": False, "reason": "invalid", "error": "A unit address, its MAC and true/false are required."}), 400
    with cache_lock:
        unit = copy.deepcopy(cache_units.get(ip))
    if not unit:
        return jsonify({"ok": False, "reason": "unknown_unit", "error": "That unit is not in the discovered inventory."}), 404
    if _normalize_mac(unit.get("mac") or "") != mac:
        return jsonify({"ok": False, "reason": "identity_mismatch", "error": "That address belongs to a different unit now. Scan again."}), 409
    if not luma_thumbnail.supported(unit.get("type") or unit.get("role"), unit.get("model")):
        return jsonify({"ok": False, "reason": "unsupported", "error": "This unit has no thumbnail setting that is known."}), 409
    result = luma_thumbnail.write_verified(_ws_call_auth, ip, enable, float(CONFIG.get("TIMEOUT", 3.0)))
    if result.get("enabled") is not None:
        with cache_lock:
            if ip in cache_units and _normalize_mac(cache_units[ip].get("mac") or "") == mac:
                cache_units[ip]["thumbnail_enabled"] = result["enabled"]
        _persist_units_cache()
    if result.get("ok"):
        logging.info("[thumbnail] %s set to %s and verified", ip, "on" if enable else "off")
        return jsonify({"ok": True, "enabled": result["enabled"], "verified": True})
    logging.warning("[thumbnail] %s write not confirmed: %s", ip, result.get("reason"))
    status = 502 if result.get("reason") in ("unreadable", "no_answer", "unverified") else 409
    return jsonify({"ok": False, "reason": result.get("reason"), "enabled": result.get("enabled"),
                    "error": "The unit did not confirm the change."}), status


@APP.post("/api/scan")
def api_scan():
    data = luma_request.json_object()
    global cache_generation
    logging.info(f"===== [API_SCAN] STARTING - received targets: {data.get('targets')} =====")
    logging.debug("[api_scan] fields received: %s", sorted(str(k) for k in data))
    targets = data.get("targets") or ""
    # Counted from the expression before anything is expanded; refused, not truncated.
    try:
        luma_scanplan.check(targets)
    except luma_scanplan.ScanRangeError as refusal:
        logging.warning("[api_scan] refused: %s", refusal.reason)
        return jsonify({"ok": False, "reason": refusal.reason, "error": refusal.message}), 400
    ips = _expand_targets(targets)
    
    # Validate all IPs are properly formatted
    invalid_ips = []
    for ip in ips:
        try:
            ipaddress.ip_address(ip)
        except (ValueError, TypeError):
            invalid_ips.append(ip)
    if invalid_ips:
        logging.warning(f"[api_scan] Skipping {len(invalid_ips)} invalid IPs: {invalid_ips}")
        ips = [ip for ip in ips if ip not in invalid_ips]
    
    logging.info(f"[API_SCAN] Expanded to {len(ips)} IPs")
    logging.debug(f"[api_scan] expanded targets: {ips}")
    if not ips:
        logging.warning("[api_scan] No IPs to scan.")
        return jsonify({"ok": True, "duration": 0, "scanned": 0, "units": []})

    start = time.time()
    counts = {"scanned":0,"added":0,"updated":0}
    errors = []  # Track authentication and other errors
    with cache_lock:
        scan_generation = cache_generation

    # Build MAC-to-IP index once for O(1) lookups (prevents O(n²) complexity)
    with cache_lock:
        if cache_generation != scan_generation:
            return jsonify({"ok": True, "duration": 0, "scanned": 0, "units": list(cache_units.values()), "errors": errors, "stale": True})
        mac_to_ip_index = _mac_index_of_cache()

    with ThreadPoolExecutor(max_workers=min(max(1, SCAN_WORKERS), len(ips))) as ex:
        futs = {ex.submit(_scan_one, ip): ip for ip in ips}
        for fut in as_completed(futs):
            ip = futs[fut]
            counts["scanned"] += 1
            try:
                info = fut.result()
            except Exception as e:
                logging.error(f"[api_scan] Exception scanning {ip}: {e}")
                info = None
            if not info:
                logging.debug(f"[api_scan] No info returned for {ip}")
                continue
            
            # Check if this is an error response
            if "_error" in info:
                error_details = info.get("_error_details", {})
                errors.append({
                    "ip": ip, 
                    "error": info.get("_error", "Unknown error"),
                    "details": error_details
                })
                logging.error(f"[API_SCAN] ******** AUTH ERROR for {ip}: {info.get('_error')} ********")
                logging.warning(f"[api_scan] Error for {ip}: {info.get('_error')}")
                logging.warning(f"[api_scan] Error details: {error_details}")
                logging.warning(f"[api_scan] Full info returned: {info}")
                continue
            
            # Only add devices that are encoders, decoders, or switchers
            role = (info.get("role") or info.get("type") or "").strip().lower()
            if not role:
                model_l = (info.get("model") or "").strip().lower()
                if "at-ome-cs31" in model_l:
                    role = "switcher"
                    info["type"] = info.get("type") or "switcher"
                    info["role"] = info.get("role") or "Decoder/Switcher"
                elif model_l.startswith(("hw-luma-e", "at-luma-e", "at-omni-e")):
                    role = "encoder"
                    info["type"] = info.get("type") or "encoder"
                    info["role"] = info.get("role") or "Encoder"
                elif model_l.startswith(("hw-luma-d", "at-luma-d", "at-omni-d")):
                    role = "decoder"
                    info["type"] = info.get("type") or "decoder"
                    info["role"] = info.get("role") or "Decoder"
            if not any(r in role for r in ["encoder", "decoder", "switcher"]):
                logging.info(f"[api_scan] Skipping {ip} - not an encoder/decoder/switcher (role: {role}, model: {info.get('model', 'unknown')})")
                continue
            
            with cache_lock:
                if cache_generation != scan_generation:
                    logging.info("[api_scan] cache was cleared during scan; ignoring stale scan result")
                    continue
                outcome = _ingest_scanned_unit(ip, info, mac_to_ip_index)
                if outcome["action"] in ("updated", "added"):
                    counts[outcome["action"]] += 1
                if outcome["moved_from"]:
                    logging.info(f"[scan-fast] mac {outcome['mac']} moved {outcome['moved_from']} -> {ip}")

    dur = round(time.time()-start, 2)
    with cache_lock:
        stale = cache_generation != scan_generation
        all_units = list(cache_units.values())
    if stale:
        logging.info("[api_scan] finished stale scan after clear; skipping cache persist")
    else:
        _persist_units_cache()
    logging.info(f"[scan-fast] done {counts} errors={len(errors)} duration={dur}s total_mem={len(all_units)}")
    
    # Return errors along with units
    return jsonify({"ok": True, "duration": dur, "scanned": counts["scanned"], "units": all_units, "errors": errors, "stale": stale})

# -------- misc endpoints --------
@APP.post("/api/tcp_probe")
def api_tcp_probe():
    data = luma_request.json_object()
    ip = (data.get("ip") or "").strip()
    timeout_ms = int(data.get("timeout_ms") or TCP_TIMEOUT_MS)
    if not ip:
        return jsonify({"ok": False, "error": "missing ip"}), 400
    alive = _tcp_probe(ip, 80, timeout_ms) or _tcp_probe(ip, 443, timeout_ms)
    return jsonify({"ok": True, "reachable": bool(alive)})

@APP.post("/api/version", endpoint="luma_api_version")
def api_version():
    data = luma_request.json_object()
    ip = (data.get("ip") or "").strip()
    if not ip:
        return jsonify({"ok": False, "error":"missing ip"}), 400
    if not (_tcp_probe(ip,80) or _tcp_probe(ip,443)):
        return jsonify({"ok": True, "alive": False})
    sysr = _ws_call_auth(ip, "System.Get", {}, CONFIG["TIMEOUT"])
    if not sysr or "result" not in sysr:
        return jsonify({"ok": True, "alive": True, "version": ""})
    fw = sysr["result"].get("FwVer","") or ""
    return jsonify({"ok": True, "alive": True, "version": fw, "unit": {"ip":ip,"version":fw}})

@APP.post("/api/hostname", endpoint="luma_api_hostname")
def api_hostname():
    data = luma_request.json_object()
    ip = (data.get("ip") or "").strip()
    if not ip:
        return jsonify({"ok": False, "error":"missing ip"}), 400
    if not (_tcp_probe(ip,80) or _tcp_probe(ip,443)):
        return jsonify({"ok": True, "alive": False, "hostname": ""})
    name = _ws_get_hostname(ip, CONFIG["TIMEOUT"])
    return jsonify({"ok": True, "alive": bool(name), "hostname": name})

@APP.post("/api/set_hostname", endpoint="luma_api_set_hostname")
def api_set_hostname():
    data = luma_request.json_object()
    ip = (data.get("ip") or "").strip()
    hostname = (data.get("hostname") or "").strip()
    
    if not ip:
        return jsonify({"ok": False, "error":"missing ip"}), 400
    if not hostname:
        return jsonify({"ok": False, "error":"missing hostname"}), 400
    
    # Validate hostname format
    is_valid, error_msg = _validate_hostname(hostname)
    if not is_valid:
        return jsonify({"ok": False, "error": error_msg}), 400
    
    if not (_tcp_probe(ip,80) or _tcp_probe(ip,443)):
        return jsonify({"ok": False, "error":"device unreachable"}), 500
    
    try:
        result_hostname = _ws_set_hostname(ip, hostname, CONFIG["TIMEOUT"])
        if result_hostname:
            # Update cache and persist atomically
            with cache_lock:
                if ip in cache_units:
                    cache_units[ip]["hostname"] = result_hostname
            # Persist after releasing lock to avoid deadlock
            _persist_units_cache()
            return jsonify({"ok": True, "hostname": result_hostname})
        else:
            return jsonify({"ok": False, "error":"failed to set hostname"}), 500
    except Exception as e:
        logging.error(f"[set_hostname] Exception: {e}")
        return luma_request.internal_error(e)

@APP.post("/api/get_streamname", endpoint="luma_api_get_streamname")
def api_get_streamname():
    data = luma_request.json_object()
    ip = (data.get("ip") or "").strip()
    
    if not ip:
        return jsonify({"ok": False, "error":"missing ip"}), 400
    if not (_tcp_probe(ip,80) or _tcp_probe(ip,443)):
        return jsonify({"ok": False, "error":"device unreachable"}), 500
    
    try:
        streamname = _ws_get_streamname(ip, CONFIG["TIMEOUT"])
        return jsonify({"ok": True, "streamname": streamname})
    except Exception as e:
        logging.error(f"[get_streamname] Exception: {e}")
        return luma_request.internal_error(e)

@APP.post("/api/set_streamname", endpoint="luma_api_set_streamname")
def api_set_streamname():
    data = luma_request.json_object()
    ip = (data.get("ip") or "").strip()
    streamname = (data.get("streamname") or "").strip()
    
    if not ip:
        return jsonify({"ok": False, "error":"missing ip"}), 400
    if not streamname:
        return jsonify({"ok": False, "error":"missing streamname"}), 400
    
    # Validate streamname format
    is_valid, error_msg = _validate_streamname(streamname)
    if not is_valid:
        return jsonify({"ok": False, "error": error_msg}), 400
    
    if not (_tcp_probe(ip,80) or _tcp_probe(ip,443)):
        return jsonify({"ok": False, "error":"device unreachable"}), 500
    
    try:
        result_streamname = _ws_set_streamname(ip, streamname, CONFIG["TIMEOUT"])
        if result_streamname:
            # Update cache and persist atomically
            with cache_lock:
                if ip in cache_units:
                    if "enc" not in cache_units[ip]:
                        cache_units[ip]["enc"] = {}
                    if "video" not in cache_units[ip]["enc"]:
                        cache_units[ip]["enc"]["video"] = {}
                    if "audio" not in cache_units[ip]["enc"]:
                        cache_units[ip]["enc"]["audio"] = {}
                    cache_units[ip]["enc"]["video"]["streamname"] = result_streamname
                    cache_units[ip]["enc"]["audio"]["streamname"] = result_streamname
            # Persist after releasing lock to avoid deadlock
            _persist_units_cache()
            return jsonify({"ok": True, "streamname": result_streamname})
        else:
            return jsonify({"ok": False, "error":"failed to set streamname"}), 500
    except Exception as e:
        logging.error(f"[set_streamname] Exception: {e}")
        return luma_request.internal_error(e)

@APP.post("/api/reset", endpoint="luma_api_reset")
def api_reset():
    data = luma_request.json_object()
    ip = (data.get("ip") or "").strip()
    if not ip:
        return jsonify({"ok": False, "error":"missing ip"}), 400
    ok = _ws_factory_reset(ip)
    return jsonify({"ok": bool(ok)})

def _http_base(ip: str) -> str:
    scheme = CONFIG["http_scheme"]
    port = CONFIG["http_port"]
    default = 80 if scheme=="http" else 443
    host = f"{ip}:{port}" if port and port!=default else ip
    return f"{scheme}://{host}"

class _FormUploadParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self._cur = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'form':
            attr = dict((k.lower(), v) for k, v in attrs)
            self._cur = {
                'action': attr.get('action') or '',
                'method': (attr.get('method') or 'POST').upper(),
                'enctype': (attr.get('enctype') or '').lower(),
                'file_fields': []
            }
        elif tag.lower() == 'input' and self._cur is not None:
            attr = dict((k.lower(), v) for k, v in attrs)
            typ = (attr.get('type') or '').lower()
            if typ == 'file':
                name = attr.get('name') or attr.get('id') or 'file'
                if name not in self._cur['file_fields']:
                    self._cur['file_fields'].append(name)

    def handle_endtag(self, tag):
        if tag.lower() == 'form' and self._cur is not None:
            # Keep forms that include at least one file input
            if self._cur['file_fields'] and self._cur['action']:
                self.forms.append(self._cur)
            self._cur = None

def _normalize_action_path(action: str) -> str:
    if not action:
        return ''
    a = action.strip()
    if a.startswith('http://') or a.startswith('https://'):
        # strip scheme and host
        try:
            p = a.split('://', 1)[1]
            p = p[p.find('/'):] if '/' in p else '/'
        except Exception:
            p = '/'
    else:
        p = a if a.startswith('/') else '/' + a
    # collapse multiple slashes
    p = re.sub(r'/+', '/', p)
    return p

def _discover_switcher_upload_forms(ip: str, session: requests.Session, attempts: list, headers_base: dict) -> list:
    """Fetch common pages and discover firmware upload forms.
    Returns a list of dicts: {path, method, enctype, fields, source}.
    """
    pages = [
        '/', '/index.html', '/upgrade', '/firmware', '/upload', '/admin/firmware', '/system/upgrade'
    ]
    discovered = []
    seen_sources = set()
    for p in pages:
        url = _http_base(ip) + (p if p.startswith('/') else '/' + p)
        if url in seen_sources:
            continue
        t0 = time.time()
        try:
            r = session.get(url, timeout=CONFIG["TIMEOUT"], verify=CONFIG["http_verify_tls"], headers={**headers_base, 'Accept': 'text/html,*/*'})
            dt_ms = int((time.time()-t0)*1000)
            ctype = (r.headers.get('Content-Type') or '').lower()
            ok = r.status_code == 200 and ('text/html' in ctype or ctype == '' or 'text/' in ctype)
            attempts.append({
                'mode':'discover', 'url': url, 'status': r.status_code, 'ok': bool(ok), 'ms': dt_ms,
                'headers': {k: r.headers.get(k) for k in ('Server','Allow','Content-Type')}
            })
            if ok:
                parser = _FormUploadParser()
                # Use response.text to respect encoding
                try:
                    parser.feed(r.text)
                except Exception:
                    # Fallback to bytes decoding
                    parser.feed(r.content.decode(errors='ignore'))
                for f in parser.forms:
                    norm_path = _normalize_action_path(f['action'])
                    if not norm_path:
                        continue
                    entry = {
                        'path': norm_path,
                        'method': f['method'],
                        'enctype': f['enctype'],
                        'fields': list(f['file_fields']),
                        'source': url
                    }
                    discovered.append(entry)
                    attempts.append({
                        'mode':'discover-form', 'source': url, 'path': norm_path, 'method': f['method'],
                        'enctype': f['enctype'], 'fields': list(f['file_fields'])
                    })
                    logging.info(f"[switcher-discover] ip={ip} source={url} action={norm_path} method={f['method']} enctype={f['enctype']} fields={f['file_fields']}")
            seen_sources.add(url)
        except requests.RequestException as e:
            dt_ms = int((time.time()-t0)*1000)
            attempts.append({'mode':'discover', 'url': url, 'status': -1, 'ok': False, 'ms': dt_ms, 'error': f"{e.__class__.__name__}:{str(e)[:120]}"})
            logging.warning(f"[switcher-discover] ip={ip} url={url} err={e.__class__.__name__}:{str(e)[:80]}")
            continue
    return discovered

def _login_and_session(ip: str, session: requests.Session) -> bool:
    url = _http_base(ip) + CONFIG["login_path"]
    headers = {"Accept":"application/json"}; headers.update(CONFIG.get("extra_headers",{}))
    try:
        for slot, value in _http_credential_candidates(ip):
            payload = {"username": CONFIG["username"], "password": value}
            r = session.post(url, json=payload, timeout=CONFIG["TIMEOUT"], verify=CONFIG["http_verify_tls"], headers=headers)
            logging.info(f"[login] {ip} -> {r.status_code}")
            if r.status_code in (200, 204):
                _note_http_credential(ip, slot)
                return True
            if r.status_code not in (401, 403):
                return False        # not a credential refusal: another password would not help
        return False
    except requests.RequestException as e:
        logging.warning(f"[login] {ip} exc {e.__class__.__name__}")
        return False

def _upload_fw(ip: str, firmware_path: str, session: requests.Session) -> Tuple[bool,str]:
    url = _http_base(ip) + CONFIG["upload_path"]
    headers = CONFIG.get("extra_headers",{}).copy()
    field = CONFIG["upload_field"]
    try:
        with open(firmware_path, "rb") as f:
            files = {field: (os.path.basename(firmware_path), f, "application/gzip")}
            r = session.post(url, files=files, timeout=CONFIG["TIMEOUT"], verify=CONFIG["http_verify_tls"], headers=headers)
        logging.info(f"[upload] {ip} -> {r.status_code}")
        if r.status_code in (200,204): return True,"ok"
        return False, f"http {r.status_code}"
    except FileNotFoundError:
        return False, "file not found"
    except requests.RequestException as e:
        return False, f"req_err:{e.__class__.__name__}"

# CS31 (Switcher) variant: often processes firmware automatically after HTTP upload
# Placeholder path & field reuse existing CONFIG; adjust if capture indicates different.
def _upload_fw_cs31_multi(ip: str, firmware_path: str, session: requests.Session, debug: bool=False) -> Tuple[bool,str,List[Dict[str,Any]]]:
    """Try multiple endpoint/field/content-type combinations for CS31 switcher firmware.
    Returns (ok, final_detail, attempts)."""
    attempts: List[Dict[str, Any]] = []
    
    # Try the known working combination first
    primary_attempts = [
        ("/upload", "FIREWARE_FILE", "application/x-gzip"),
        ("/upload", "FIRMWARE_FILE", "application/x-gzip"),
        ("/upload", "file", "application/gzip"),
        ("/firmware", "FIREWARE_FILE", "application/x-gzip"),
        ("/api/v1/fw_upload", "FIREWARE_FILE", "application/x-gzip"),
    ]
    
    try:
        with open(firmware_path, "rb") as f:
            data_bytes = f.read()
        fname = os.path.basename(firmware_path)
    except FileNotFoundError:
        return False, "file not found", attempts
    except Exception as e:
        return False, f"read_err:{e.__class__.__name__}:{str(e)}", attempts
    
    # Try each combination
    for path, field, ctype in primary_attempts:
        url = f"http://{ip}{path}"
        logging.info(f"[CS31] Attempt: {url} field={field} type={ctype}")
        
        try:
            # Re-create file object for each attempt
            with io.BytesIO(data_bytes) as file_obj:
                files = {field: (fname, file_obj, ctype)}

                # Use longer timeout for firmware upload
                resp = session.post(url, files=files, timeout=600)
            
            status = resp.status_code
            snippet = (resp.text or "")[:200]
            logging.info(f"[CS31] HTTP Status: {status}")
            if snippet:
                logging.info(f"[CS31] Response: {snippet}")
            
            attempt = {
                "path": path, "field": field, "ctype": ctype, 
                "status": status, "ok": status in (200, 204), 
                "resp_snippet": snippet, "mode": "multipart", "http_method": "POST"
            }
            attempts.append(attempt)
            
            if status in (200, 204):
                logging.info(f"[CS31] ✓ Upload successful!")
                return True, "ok", attempts
            
            logging.warning(f"[CS31] Got status {status}, trying next combination...")
            
        except requests.exceptions.ConnectionError as e:
            # Device closed connection during flash (expected behavior)
            logging.info("[CS31] Device closed connection during write (this is normal - flash in progress)")
            attempt = {
                "path": path, "field": field, "ctype": ctype,
                "status": -1, "ok": True, 
                "error": "Device closed connection (expected)", 
                "mode": "multipart", "http_method": "POST"
            }
            attempts.append(attempt)
            return True, "Device closed connection during flash (expected)", attempts
            
        except requests.exceptions.ReadTimeout:
            # Timeout means upload was accepted (device is busy flashing)
            logging.info("[CS31] Request timeout (upload was accepted, device is flashing)")
            attempt = {
                "path": path, "field": field, "ctype": ctype,
                "status": -1, "ok": True,
                "error": "Timeout (upload accepted)",
                "mode": "multipart", "http_method": "POST"
            }
            attempts.append(attempt)
            return True, "Timeout - upload accepted, device flashing", attempts
            
        except requests.RequestException as e:
            error_msg = f"{e.__class__.__name__}:{str(e)[:100]}"
            logging.warning(f"[CS31] Request error: {error_msg}")
            attempt = {
                "path": path, "field": field, "ctype": ctype,
                "status": -1, "ok": False,
                "error": error_msg,
                "mode": "multipart", "http_method": "POST"
            }
            attempts.append(attempt)
            # Continue to next combination
            continue
    
    # If we get here, all attempts failed
    logging.error(f"[CS31] All {len(primary_attempts)} upload attempts failed")
    return False, "all_attempts_failed", attempts

def _monitor_cs31_upgrade(
    ip: str,
    firmware_path: str,
    session: requests.Session,
    timeout_sec: int = 600,
    debug: bool = False,
    baseline_version: Optional[str] = None,
) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """Monitor CS31 firmware upgrade progress after upload.
    
    Status progression: sending file -> updating -> rebooting -> confirm version -> success
    When device is unreachable, we're in 'rebooting' state.
    Validates that the new firmware version matches the uploaded version.
    Returns (ok, status_detail, telemetry)
    """
    telemetry: List[Dict[str, Any]] = []
    start_time = time.time()
    poll_interval = 2.0  # seconds between checks
    last_reachable = True
    first_unreachable_time = None
    
    # Extract version from firmware filename
    # Expected formats: at-ome-cs31_v1.2.5.bin, at-ome-cs31_1.2.5.bin, firmware_v1.0.0.bin, etc.
    fname = os.path.basename(firmware_path).lower()
    expected_version = None
    
    # Try to extract version from filename (patterns like v1.2.3, _1.2.3)
    import re
    version_match = re.search(r'v?(\d+\.\d+\.\d+|\d+\.\d+)', fname)
    if version_match:
        expected_version = version_match.group(1)
    
    logging.info(f"[CS31-Monitor] Starting upgrade monitoring for {ip}")
    logging.info(f"[CS31-Monitor] Firmware: {fname}")
    logging.info(f"[CS31-Monitor] Expected version: {expected_version or 'unknown'}")
    
    version_before_upgrade = (baseline_version or "").strip() or None
    consecutive_changed_while_reachable = 0
    version_validated_after_reboot = False
    
    while time.time() - start_time < timeout_sec:
        elapsed = int(time.time() - start_time)
        
        # Check if device is reachable via ping
        try:
            import subprocess as sp
            system = platform.system().lower()
            if "windows" in system:
                cmd = ["ping", "-n", "1", "-w", "1000", ip]
            else:
                cmd = ["ping", "-c", "1", "-W", "1", ip]
            rc = _subprocess_run_no_window(cmd, capture_output=True, text=True, timeout=3)
            is_reachable = rc.returncode == 0
        except Exception:
            is_reachable = False
        
        ping_status = "reachable" if is_reachable else "unreachable"
        
        # Track reboot state
        if not is_reachable:
            if last_reachable:
                first_unreachable_time = time.time()
                logging.info(f"[CS31-Monitor] Device became unreachable at {elapsed}s - entering rebooting state")
            status = "rebooting"
        else:
            status = "updating"
        
        # Try to get system info
        current_version = None
        try:
            sysinfo = _ws_call_auth(ip, "System.Get", {}, timeout=2.0)
            if sysinfo and "result" in sysinfo:
                result = sysinfo["result"]
                # Extract version from multiple possible fields
                current_version = (
                    result.get("FwVer") or 
                    result.get("FWVersion") or 
                    result.get("fwver") or 
                    result.get("Firmware") or 
                    result.get("firmware") or 
                    result.get("Version") or 
                    result.get("version")
                )
                # Capture version before upgrade (on first successful check while still reachable)
                if version_before_upgrade is None and is_reachable and first_unreachable_time is None:
                    version_before_upgrade = current_version
                
                if debug:
                    logging.debug(f"[CS31-Monitor] System info response: {result}")
        except Exception as e:
            if debug:
                logging.debug(f"[CS31-Monitor] System.Get failed: {e.__class__.__name__}")
        
        # Build telemetry entry
        telem = {
            "elapsed_sec": elapsed,
            "ping": ping_status,
            "status": status,
            "version": current_version
        }
        telemetry.append(telem)
        
        # Log progress
        version_str = f" version={current_version}" if current_version else ""
        logging.info(f"[CS31-Monitor] {elapsed}s: ping={ping_status} status={status}{version_str}")
        
        # If reboot is not observable via ping (or is too brief to catch),
        # allow completion based on stable, changed version while reachable.
        if is_reachable and first_unreachable_time is None and version_before_upgrade and current_version:
            if current_version != version_before_upgrade:
                consecutive_changed_while_reachable += 1
            else:
                consecutive_changed_while_reachable = 0

            if consecutive_changed_while_reachable >= 2:
                logging.info(
                    f"[CS31-Monitor] Upgrade complete without observed reboot: {version_before_upgrade} -> {current_version}"
                )
                telemetry.append({
                    "elapsed_sec": elapsed,
                    "ping": ping_status,
                    "status": "confirm_version",
                    "version": current_version,
                    "version_before": version_before_upgrade,
                    "version_changed": True,
                    "version_match_expected": (expected_version in current_version) if expected_version else True,
                    "reboot_observed": False,
                })

                if expected_version and (expected_version not in current_version and current_version not in expected_version):
                    return False, f"upgrade_failed: version_mismatch (expected: {expected_version}, got: {current_version})", telemetry

                return True, f"success (version changed without observed reboot: {version_before_upgrade} -> {current_version})", telemetry

        # Check for upgrade completion
        if is_reachable and first_unreachable_time is not None and not version_validated_after_reboot:
            # Device came back online after rebooting
            version_validated_after_reboot = True
            reboot_duration = int(time.time() - first_unreachable_time)
            logging.info(f"[CS31-Monitor] Device rebooted in {reboot_duration}s, now confirming version")
            status = "confirm_version"
            
            # Wait longer for device to fully stabilize after reboot before checking version
            logging.info(f"[CS31-Monitor] Waiting 3s for device to stabilize post-reboot...")
            time.sleep(3)
            
            # Check version several times to ensure stability
            version_checks = 0
            version_stable = True
            last_observed_version = None
            all_versions = []
            
            for check in range(4):
                time.sleep(2)  # Increased sleep between checks to avoid hammering device
                try:
                    sysinfo = _ws_call_auth(ip, "System.Get", {}, timeout=2.0)
                    if sysinfo and "result" in sysinfo:
                        observed = (
                            sysinfo["result"].get("FwVer") or 
                            sysinfo["result"].get("FWVersion") or 
                            sysinfo["result"].get("fwver") or 
                            sysinfo["result"].get("Firmware") or 
                            sysinfo["result"].get("firmware") or 
                            sysinfo["result"].get("Version") or 
                            sysinfo["result"].get("version")
                        )
                        all_versions.append(observed)
                        if last_observed_version is not None and observed != last_observed_version:
                            version_stable = False
                        last_observed_version = observed
                        version_checks += 1
                        logging.info(f"[CS31-Monitor] Version check {check+1}/4: {observed}")
                except Exception as e:
                    logging.debug(f"[CS31-Monitor] Version check {check+1}/4 failed: {e}")
            
            final_version = last_observed_version
            
            # Validate version changed and matches expected
            version_match_ok = True
            version_changed = True
            version_change_msg = ""
            
            if version_before_upgrade and final_version:
                if version_before_upgrade == final_version:
                    version_changed = False
                    version_change_msg = f"WARNING: Version did not change! Before: {version_before_upgrade}, After: {final_version}"
                    logging.warning(f"[CS31-Monitor] {version_change_msg}")
                else:
                    version_change_msg = f"Version changed: {version_before_upgrade} → {final_version}"
                    logging.info(f"[CS31-Monitor] {version_change_msg}")
            
            if expected_version and final_version:
                if expected_version not in final_version and final_version not in expected_version:
                    version_match_ok = False
                    logging.warning(f"[CS31-Monitor] Version mismatch! Expected: {expected_version}, Got: {final_version}")
            
            # Build final telemetry
            final_telem = {
                "elapsed_sec": int(time.time() - start_time),
                "ping": "reachable",
                "status": "confirm_version",
                "version": final_version,
                "version_before": version_before_upgrade,
                "version_changed": version_changed,
                "version_match_expected": version_match_ok,
                "version_checks": version_checks,
                "version_stable": version_stable,
                "reboot_duration_sec": reboot_duration
            }
            telemetry.append(final_telem)
            
            # Determine success based on version validation
            if not version_changed:
                logging.error(f"[CS31-Monitor] Upgrade FAILED - version did not change")
                return False, f"upgrade_failed: version_unchanged (before: {version_before_upgrade}, after: {final_version})", telemetry
            
            if expected_version and not version_match_ok:
                logging.error(f"[CS31-Monitor] Upgrade FAILED - version mismatch")
                return False, f"upgrade_failed: version_mismatch (expected: {expected_version}, got: {final_version})", telemetry
            
            logging.info(f"[CS31-Monitor] Upgrade complete! {version_change_msg}")
            return True, f"success (reboot={reboot_duration}s, {version_change_msg})", telemetry
        
        # Wait before next poll
        time.sleep(poll_interval)
        last_reachable = is_reachable
    
    # Timeout
    final_status = "rebooting" if not is_reachable else "updating"
    logging.error(f"[CS31-Monitor] Timeout after {timeout_sec}s in state '{final_status}'")
    telemetry.append({
        "elapsed_sec": int(time.time() - start_time),
        "ping": ping_status,
        "status": f"timeout_{final_status}",
        "version": current_version
    })
    return False, f"timeout after {timeout_sec}s ({final_status})", telemetry

@APP.post("/api/upgrade", endpoint="luma_api_upgrade")
def api_upgrade():
    data = luma_request.json_object()
    logging.debug("[api_upgrade] fields received: %s", sorted(str(k) for k in data))
    targets = data.get("targets") or []
    firmware = data.get("file") or data.get("firmware")

    # Only the computer LumaSuite runs on may change the stored device credentials
    # through an upgrade request, and only it may name a file by absolute path.
    local_request = luma_remote.is_local_request(request.environ)
    if local_request:
        if data.get("username"): CONFIG["username"] = data["username"]
        if data.get("password"):
            if data["password"] != CONFIG.get("password"):
                CONFIG["password"] = data["password"]
                _invalidate_credential_cache("firmware_upgrade_password_override")
    elif firmware and (os.path.isabs(str(firmware)) or os.path.basename(str(firmware)) != str(firmware)
                       or str(firmware) in (".", "..")):
        return jsonify({"ok": False, "error": "firmware_name_only",
                        "details": "Choose a firmware file by name from the firmware folder."}), 400

    if not targets or not firmware:
        logging.error(f"[api_upgrade] missing targets or firmware: targets={targets}, firmware={firmware}")
        return jsonify({"ok": False, "error":"missing targets or firmware"}), 400

    firmware_path, checked_paths = _resolve_firmware_path(firmware)
    if not firmware_path:
        logging.error(f"[api_upgrade] firmware not found: firmware={firmware} checked={checked_paths}")
        return jsonify({"ok": False, "error": "firmware_not_found", "firmware": firmware, "checked": checked_paths}), 400

    results = {}
    for ip in targets:
        steps = []
        logging.info(f"[upgrade] start ip={ip} firmware={os.path.basename(firmware_path)} targets={len(targets)}")
        if not (_tcp_probe(ip,80) or _tcp_probe(ip,443)):
            logging.warning(f"[api_upgrade] {ip} not reachable on 80/443")
            steps.append({"step":"precheck","result":"unreachable"})
            results[ip] = {"ok": False, "steps": steps}
            continue
        sess = requests.Session()
        if _login_and_session(ip, sess):
            steps.append({"step":"login","result":"ok"})
        else:
            logging.warning(f"[api_upgrade] login failed for {ip}")
            steps.append({"step":"login","result":"fail"})
        # Detect switcher (CS31) device by cached model or firmware filename prefix
        cached = cache_units.get(ip, {}) if isinstance(cache_units, dict) else {}
        baseline_version = (
            cached.get("version")
            or cached.get("firmware")
            or cached.get("fwver")
            or cached.get("FWVersion")
            or ""
        )
        model_l = (cached.get("model") or "").lower()
        is_switcher = ("at-ome-cs31" in model_l) or os.path.basename(firmware_path).lower().startswith("at-ome-cs31")
        try:
            logging.info(f"[upgrade] detect ip={ip} model='{model_l}' fname='{os.path.basename(firmware_path).lower()}' switcher={int(is_switcher)}")
        except Exception as e:
            logging.error(f"[api_upgrade] error in switcher detect logging: {e}")
        if is_switcher:
            steps.append({"step":"switcher_detect","result":"yes"})
            ok_up, detail, attempts = _upload_fw_cs31_multi(ip, firmware_path, sess, debug=bool(data.get("debug")))
            steps.append({"step":"upload_switcher","result":"ok" if ok_up else "fail","detail":detail, "attempts": attempts})
            if not ok_up:
                # Fallback to raw socket protocol if HTTP attempts failed
                logging.warning(f"[api_upgrade] HTTP upload failed for {ip}, trying socket fallback.")
                ok_sock, detail_sock, meta_sock = _upload_fw_cs31_socket(ip, firmware_path, attempts, debug=bool(data.get("debug")))
                steps.append({"step":"upload_switcher_socket","result":"ok" if ok_sock else "fail","detail":detail_sock, "meta": meta_sock})
                if ok_sock:
                    ok_up = True
            
            # After successful upload, monitor the upgrade progress
            if ok_up:
                logging.info(f"[api_upgrade] Upload successful for {ip}, starting upgrade monitor...")
                mon_ok, mon_detail, mon_telem = _monitor_cs31_upgrade(
                    ip,
                    firmware_path,
                    sess,
                    timeout_sec=600,
                    debug=bool(data.get("debug")),
                    baseline_version=baseline_version,
                )
                steps.append({"step":"monitor_upgrade","result":"ok" if mon_ok else "fail","detail":mon_detail, "telemetry": mon_telem})
                ok_up = mon_ok
            
            results[ip] = {"ok": ok_up, "steps": steps}
        else:
            ok_up, detail = _upload_fw(ip, firmware_path, sess)
            steps.append({"step":"upload","result":"ok" if ok_up else "fail","detail":detail})
            if ok_up:
                trig = _ws_trigger_upgrade(ip)
                steps.append({"step":"ws_trigger","result":"ok" if trig else "fail"})
            results[ip] = {"ok": ok_up, "steps": steps}
    logging.debug(f"[api_upgrade] results: {results}")
    return jsonify({"ok": True, "results": results})

@APP.post("/api/ping", endpoint="luma_api_ping")
def api_ping():
    data = luma_request.json_object()
    ip = (data.get("ip") or "").strip()
    timeout_ms = int(data.get("timeout_ms") or 1000)
    if not ip:
        return jsonify({"ok": False, "error": "missing ip"}), 400
    import subprocess as sp
    system = platform.system().lower()
    try:
        if "windows" in system:
            cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
        else:
            secs = max(1, int(round(timeout_ms/1000.0)))
            cmd = ["ping", "-c", "1", "-W", str(secs), ip]
        rc = _subprocess_run_no_window(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=max(1, int(round(timeout_ms/1000.0))+2))
        out = rc.stdout + rc.stderr
        reachable = rc.returncode == 0 or ("bytes=32" in out and "TTL=" in out)
        return jsonify({"ok": True, "reachable": bool(reachable)})
    except Exception as e:
        return jsonify({"ok": True, "reachable": False, "error": type(e).__name__})

@APP.get("/api/app_version", endpoint="luma_api_app_version")
def api_app_version():
    """Return the application version"""
    return jsonify({"ok": True, "version": __version__})

# -------- LOCAL APPLICATION PORT (local computer only) --------
# The port of the local application listener the launcher starts on 127.0.0.1. It is not the
# Remote Access listener, which has its own port and is not touched here. The launcher registers
# a controller; in any other run (`python lumaserver.py`, tests) there is none and the answers
# say so. Classified LOCAL_ONLY: a remote session never reaches these, and the handler checks
# again so the answer does not rest on the gate alone.
def _local_port_response(action, body=None):
    if not luma_remote.is_local_request(request.environ):
        return jsonify({"ok": False, "error": "not found"}), 404
    payload, status = luma_portcontrol.api(action, body)
    return jsonify(payload), status


@APP.get("/api/local_port")
def api_local_port_state():
    return _local_port_response("state")


@APP.post("/api/local_port/<action>")
def api_local_port_action(action):
    return _local_port_response(action, luma_request.json_object())


# -------- APPLICATION UPDATE CHECK (GitHub Releases) --------
# Family spec Part 2.2. This checks whether a newer LumaSuite *application*
# build has been published -- unrelated to device/firmware version checks
# (Part 3/13). Checked only on demand (Settings opened / "Check Now"
# clicked), never on a timer, so it never becomes background polling.
_UPDATE_CHECK_REPO = "Hall-Research-Technologies/LumaSuite"
_UPDATE_CHECK_TIMEOUT_SEC = 4
_UPDATE_CHECK_CACHE_TTL_SEC = 600
_update_check_cache = {"ts": 0.0, "result": None}
_update_check_lock = threading.Lock()

def _parse_version_tuple(text):
    """Best-effort 'vX.Y.Z' -> (X, Y, Z). Returns None if not parseable."""
    if not text:
        return None
    m = re.match(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(text).strip())
    if not m:
        return None
    return tuple(int(g) if g else 0 for g in m.groups())

def _compare_versions(a, b):
    """Return -1/0/1 for a</==/> b, treating unparseable input as unknown (None)."""
    if a is None or b is None:
        return None
    return (a > b) - (a < b)

def _is_official_release_url(url):
    """Reject anything but an https URL on github.com (under this repo) or
    objects.githubusercontent.com. GitHub's API response is otherwise handed
    straight to the browser as a link href, which would make a malformed or
    tampered response an open redirect."""
    if not url:
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    if parsed.hostname == "objects.githubusercontent.com":
        return True
    if parsed.hostname == "github.com":
        return parsed.path.startswith("/%s/" % _UPDATE_CHECK_REPO)
    return False

def _platform_asset_suffixes(platform_name=None, machine=None):
    """The tails of the release assets built for this computer, or () when that cannot be said.

    The release workflow names its archives LumaSuite-<tag>-windows-x86_64.zip,
    LumaSuite-<tag>-macos-x86_64.zip, -macos-arm64.zip and LumaSuite-<tag>-linux-x86_64.tar.gz: the
    operating system and the architecture, both stated. A bare executable named for Windows x86-64
    (-windows-x86_64.exe, how a release candidate is handed out) is the same artifact. A computer that
    matches none of them is not offered a download at all: the wrong build is worse than the releases page.
    """
    platform_name = platform_name or sys.platform
    machine = (machine or platform.machine() or "").lower()
    x86 = machine in ("x86_64", "amd64")
    if platform_name.startswith("win"):
        return ("-windows-x86_64.zip", "-windows-x86_64.exe") if x86 else ()
    if platform_name == "darwin":
        if machine in ("arm64", "aarch64"):
            return ("-macos-arm64.zip",)
        return ("-macos-x86_64.zip",) if x86 else ()
    if platform_name.startswith("linux"):
        return ("-linux-x86_64.tar.gz",) if x86 else ()
    return ()

# A production release is exactly v<major>.<minor>.<patch>. v1.1.0-rc4, V1.1.0, v1.1.0\n and
# v1.1 are not release tags; \Z, never $, so a trailing newline does not qualify.
_PRODUCTION_TAG_RE = re.compile(r"v\d+\.\d+\.\d+\Z")

def _is_production_release(payload):
    """True only for a published, non-prerelease release whose tag is exactly v<M>.<m>.<p>.

    The update check reads GitHub's "latest" release, which is whatever the owner last published as
    non-draft and non-prerelease. That is a channel, not an identity, so the tag is checked as well:
    a candidate that was published by mistake is reported as unable to check, never offered."""
    if not isinstance(payload, dict):
        return False
    if payload.get("draft") or payload.get("prerelease"):
        return False
    return bool(_PRODUCTION_TAG_RE.match(str(payload.get("tag_name") or "")))

def _pick_platform_asset(assets, platform_name=None, machine=None, tag=None):
    """The download built for this computer. With `tag` given (the update check always gives it), the
    asset name must be exactly LumaSuite-<tag><platform suffix>, so a candidate build's
    executable, which carries extra words in its name, can never match a production release."""
    suffixes = _platform_asset_suffixes(platform_name, machine)
    if not suffixes:
        return None
    for suffix in suffixes:           # in order of preference: the archive before a bare executable
        for asset in assets:
            name = str((asset or {}).get("name") or "").lower()
            if tag is not None:
                if name != ("lumasuite-%s%s" % (tag, suffix)).lower():
                    continue
            elif not name.startswith("lumasuite-"):
                continue
            if name.endswith(suffix) and asset.get("browser_download_url"):
                return asset["browser_download_url"]
    return None

def _run_update_check():
    installed_tuple = _parse_version_tuple(__version__)
    try:
        resp = requests.get(
            "https://api.github.com/repos/%s/releases/latest" % _UPDATE_CHECK_REPO,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "LumaSuite/%s" % __version__},
            timeout=_UPDATE_CHECK_TIMEOUT_SEC,
        )
    except Exception as exc:
        return {"state": "unable_to_check", "detail": type(exc).__name__}

    if resp.status_code != 200:
        # A 404 (no releases yet) is not an application error and must not
        # be shown as "current" -- neither answer is provable here.
        return {"state": "unable_to_check", "detail": "http_%s" % resp.status_code}

    try:
        payload = resp.json()
    except Exception:
        return {"state": "unable_to_check", "detail": "malformed_response"}

    if not _is_production_release(payload):
        # Not a production release (draft, prerelease or a tag that is not exactly vX.Y.Z). Neither
        # "current" nor "update available" is provable from it.
        return {"state": "unable_to_check", "detail": "not_a_production_release",
                "latest_version": str((payload or {}).get("tag_name") or "") if isinstance(payload, dict) else ""}

    latest_tag = payload.get("tag_name") or payload.get("name") or ""
    latest_tuple = _parse_version_tuple(latest_tag)
    cmp_result = _compare_versions(latest_tuple, installed_tuple)
    if cmp_result is None:
        return {"state": "unable_to_check", "detail": "unparseable_version", "latest_version": latest_tag}

    asset_url = _pick_platform_asset(payload.get("assets") or [], tag=latest_tag)

    html_url = payload.get("html_url")
    if not _is_official_release_url(html_url):
        html_url = None
    if not _is_official_release_url(asset_url):
        asset_url = None

    state = "update_available" if cmp_result > 0 else "current"
    return {
        "state": state,
        "latest_version": latest_tag.lstrip("vV") or latest_tag,
        "installed_version": __version__,
        "html_url": html_url,
        "asset_url": asset_url,
    }

@APP.get("/api/update_check", endpoint="luma_api_update_check")
def api_update_check():
    force = request.args.get("force") in ("1", "true", "True")
    now = time.time()
    with _update_check_lock:
        cached = _update_check_cache["result"]
        age = now - _update_check_cache["ts"]
        if cached and not force and age < _UPDATE_CHECK_CACHE_TTL_SEC:
            out = dict(cached)
            out["ok"] = True
            out["checked_at"] = _update_check_cache["ts"]
            out["from_cache"] = True
            return jsonify(out)

    result = _run_update_check()
    with _update_check_lock:
        _update_check_cache["ts"] = now
        _update_check_cache["result"] = result

    out = dict(result)
    out["ok"] = True
    out["checked_at"] = now
    out["from_cache"] = False
    return jsonify(out)

@APP.route("/license", endpoint="luma_license_text")
def license_text():
    """The licence information the running build carries, as plain text.

    LumaSuite's own licence is a decision that has not been made, so no LICENSE file ships and this
    never invents one. When one is added it is served first. Otherwise the answer says plainly that
    there is none and carries the third-party notices, which are always bundled.
    """
    for candidate in (os.path.join(BASE_DIR, "LICENSE"), os.path.join(os.getcwd(), "LICENSE")):
        try:
            if os.path.isfile(candidate):
                with open(candidate, "r", encoding="utf-8") as fh:
                    return Response(fh.read(), mimetype="text/plain")
        except Exception as exc:
            logging.info("Could not read %s: %s", candidate, type(exc).__name__)
    notices = os.path.join(BASE_DIR, "THIRD_PARTY_NOTICES.md")
    try:
        with open(notices, "r", encoding="utf-8") as fh:
            body = fh.read()
    except OSError as exc:
        logging.warning("Third-party notices are missing from this build: %s", type(exc).__name__)
        return Response("No LumaSuite licence file is included in this build, and the third-party notices "
                        "could not be read.\n", status=404, mimetype="text/plain")
    header = ("No LumaSuite licence file is included in this build.\n"
              "The third-party components it contains, and their licences, follow. The full text of each is "
              "at /licenses/<package>/<file>.\n\n")
    return Response(header + body, mimetype="text/plain")


def _licence_manifest():
    try:
        with open(os.path.join(BASE_DIR, "licenses", "MANIFEST.json"), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        logging.warning("Licence manifest unavailable: %s", type(exc).__name__)
        return None


@APP.route("/licenses/<path:name>", endpoint="luma_licence_file")
def licence_file(name):
    """One bundled licence text. Only a path the manifest lists is ever opened."""
    manifest = _licence_manifest()
    if not manifest:
        return "Licence texts are not available in this build", 404
    allowed = {}
    for entry in manifest.get("shipped", []):
        for filename in entry.get("files", []):
            allowed["%s/%s" % (entry["name"], filename)] = True
        for sub in entry.get("bundled", []):
            for filename in sub.get("files", []):
                allowed["%s/%s" % (entry["name"], filename)] = True
    for entry in manifest.get("runtime", []):
        for filename in entry.get("files", []):
            allowed["_runtime/%s" % filename] = True
    if name not in allowed:
        return "Not found", 404
    path = os.path.join(BASE_DIR, "licenses", *name.split("/"))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return Response(fh.read(), mimetype="text/plain")
    except OSError as exc:
        logging.warning("Licence text unreadable (%s): %s", name, type(exc).__name__)
        return "Not found", 404

# --- [producer integration] BEGIN ---

import os, glob, json, base64
from flask import request, jsonify

# expose lowercase alias for runners that import "app"
try:
    app = APP
except NameError:
    # if your file uses lowercase already, keep it
    try:
        APP = app  # no-op if app already defined
    except NameError:
        raise  # neither app nor APP exists; not our setup

# Whitelist of JSON-RPC methods the proxy will allow
PRODUCER_ALLOWED_METHODS = {
    "ProfileSelection.Get", "ProfileSelection.Set",
    "Rtmp.Get", "Rtmp.Set","RtmpRunStatus.Get",
    "OsdText.Get", "OsdText.Set",
    "ImageDisplay.Get", "ImageDisplay.Set",
    "OsdLogo.Get", "OsdLogo.Set",
    "ImageReset.Set",
    "AudioInputMute.Get", "AudioInputMute.Set",
    "AudioInSelection.Get", "AudioInSelection.Set",
    "VideoInputMute.Get", "VideoInputMute.Set",
    "MainStreamVideoEncode.Get", "MainStreamVideoEncode.Set",
    "EdidInput.Get", "EdidInput.Set",
    "EdidMem1File.Get",
    "VideoOutTiming.Get", "VideoOutTiming.Set",
    "VideoOutSupportedTimingList.Get",
    "FrontPanelBlinkLed.Set",
    "System.Get", "SystemBlinkLed.Set",
    "HdcpAnnouncement.Get", "HdcpAnnouncement.Set",
    "Platform.Reboot",
    "LoginPassword.Set",
}

# No longer called: api_producer_jsonrpc now goes through
# _resolve_ws_auth_response (Primary/Fallback, family spec Part 4), which
# subsumes this. Left in place rather than deleted in case anything else
# is added against it later.
def _producer_merge_password(params: dict) -> dict:
    p = dict(params or {})
    # Use configured server password unless caller overrides
    p.setdefault("password", CONFIG.get("password", "password"))
    return p

def _producer_mint_http_bearer(username: str = "admin", password: str = "password", role: int = 2) -> str:
    payload_meta = json.dumps({"create_time": str(int(time.time())), "role": role}).encode("utf-8")
    payload_auth = json.dumps({"password": password, "username": username}).encode("utf-8")
    part1 = base64.b64encode(payload_meta).decode("utf-8").rstrip("=")
    part2 = base64.b64encode(payload_auth).decode("utf-8").rstrip("=")
    return f"{part1}.{part2}"

def _slate_ips_from_request(raw_ips) -> List[str]:
    if isinstance(raw_ips, str):
        raw_ips = raw_ips.strip()
        if not raw_ips:
            return []
        try:
            parsed = json.loads(raw_ips)
        except Exception:
            parsed = [x.strip() for x in raw_ips.split(",")]
    elif isinstance(raw_ips, list):
        parsed = raw_ips
    else:
        parsed = []

    out = []
    seen = set()
    for ip in parsed:
        ip = str(ip or "").strip()
        if not ip or ip in seen:
            continue
        try:
            ipaddress.ip_address(ip)
        except Exception:
            continue
        seen.add(ip)
        out.append(ip)
    return out

def _slate_upload_one(ip: str, filename: str, blob: bytes, mimetype: str, timeout: float = 15.0) -> Dict[str, Any]:
    username = CONFIG.get("username", "admin") or "admin"
    url = f"http://{ip}/upload/priv"
    files = {"preImage": (filename, blob, mimetype)}
    try:
        resp = None
        for slot, password in _http_credential_candidates(ip):
            headers = {"Authorization": f"Bearer {_producer_mint_http_bearer(username, password, role=0)}"}
            resp = requests.post(url, files=files, headers=headers, timeout=timeout, allow_redirects=False)
            if resp.status_code in (401, 403):
                headers = {"Authorization": f"Bearer {_producer_mint_http_bearer(username, password, role=2)}"}
                resp = requests.post(url, files=files, headers=headers, timeout=timeout, allow_redirects=False)
            if resp.status_code not in (401, 403):
                if 200 <= resp.status_code < 300:
                    _note_http_credential(ip, slot)
                break               # answered for a reason other than the credential
        ok = 200 <= resp.status_code < 300
        detail = (resp.text or "")[:240]
        return {"ok": ok, "status": resp.status_code, "detail": detail}
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}

def _slate_image_size(blob: bytes) -> Tuple[int, int]:
    if blob.startswith(b"\x89PNG\r\n\x1a\n") and len(blob) >= 24:
        return int.from_bytes(blob[16:20], "big"), int.from_bytes(blob[20:24], "big")
    if blob.startswith(b"\xff\xd8"):
        i = 2
        while i + 9 < len(blob):
            while i < len(blob) and blob[i] == 0xff:
                i += 1
            if i >= len(blob):
                break
            marker = blob[i]
            i += 1
            if marker in (0x01,) or 0xd0 <= marker <= 0xd9:
                continue
            if i + 2 > len(blob):
                break
            seg_len = int.from_bytes(blob[i:i+2], "big")
            if seg_len < 2 or i + seg_len > len(blob):
                break
            if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                if seg_len >= 7:
                    height = int.from_bytes(blob[i+3:i+5], "big")
                    width = int.from_bytes(blob[i+5:i+7], "big")
                    return width, height
                break
            i += seg_len
    return 0, 0

@app.post("/api/slate_upload")
def api_slate_upload():
    try:
        ips = _slate_ips_from_request(request.form.get("ips"))
        if not ips:
            return jsonify({"ok": False, "error": "select at least one encoder/decoder"}), 400
        upload = request.files.get("file")
        if not upload or not upload.filename:
            return jsonify({"ok": False, "error": "missing image file"}), 400
        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg"):
            return jsonify({"ok": False, "error": "image must be png or jpg"}), 400
        blob = upload.read()
        if not blob:
            return jsonify({"ok": False, "error": "empty image file"}), 400
        width, height = _slate_image_size(blob)
        if (width, height) not in ((1280, 720), (1920, 1080)):
            detail = f"{width}x{height}" if width and height else "unknown"
            return jsonify({"ok": False, "error": f"image must be 1280x720 or 1920x1080 (got {detail})"}), 400
        filename = "slate" + (".jpg" if ext == ".jpeg" else ext)
        mimetype = upload.mimetype or ("image/png" if ext == ".png" else "image/jpeg")

        results: Dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=min(8, len(ips))) as ex:
            futs = {ex.submit(_slate_upload_one, ip, filename, blob, mimetype): ip for ip in ips}
            for fut in as_completed(futs):
                ip = futs[fut]
                try:
                    results[ip] = fut.result()
                except Exception as e:
                    results[ip] = {"ok": False, "error": str(e)}
        return jsonify({"ok": any(r.get("ok") for r in results.values()), "results": results})
    except Exception as e:
        logging.exception("[slate_upload] failed")
        return luma_request.internal_error(e)

@app.post("/api/slate_default")
def api_slate_default():
    try:
        body = luma_request.json_object()
        ips = _slate_ips_from_request(body.get("ips"))
        if not ips:
            return jsonify({"ok": False, "error": "select at least one encoder/decoder"}), 400
        timeout = float(body.get("timeout") or CONFIG.get("TIMEOUT", 3.0))
        results: Dict[str, Any] = {}

        def reset_one(ip: str) -> Dict[str, Any]:
            resp = _ws_call_auth(ip, "ImageReset.Set", {}, timeout)
            if resp is None:
                return {"ok": False, "error": "device_no_response"}
            if resp.get("error"):
                err = resp.get("error")
                return {"ok": False, "error": err if isinstance(err, str) else json.dumps(err)}
            return {"ok": True, "response": resp.get("result", resp)}

        with ThreadPoolExecutor(max_workers=min(8, len(ips))) as ex:
            futs = {ex.submit(reset_one, ip): ip for ip in ips}
            for fut in as_completed(futs):
                ip = futs[fut]
                try:
                    results[ip] = fut.result()
                except Exception as e:
                    results[ip] = {"ok": False, "error": str(e)}
        return jsonify({"ok": any(r.get("ok") for r in results.values()), "results": results})
    except Exception as e:
        logging.exception("[slate_default] failed")
        return luma_request.internal_error(e)

@app.post("/api/producer/jsonrpc")
def api_producer_jsonrpc():
    """
    Proxy JSON-RPC to a unit to avoid CORS.
    Body: { "ip": "203.0.113.41", "method": "Rtmp.Set", "params": {...}, "timeout": 3.0, "id": "custom_id" }
    """
    try:
        body = luma_request.json_object(force=True)
        ip = body.get("ip")
        method = body.get("method")
        params = body.get("params")
        request_id = body.get("id")  # Preserve original ID from frontend
        if params is None:
            params = {}
        timeout = float(body.get("timeout") or 3.0)

        if not ip or not method:
            return jsonify({"error": "ip and method are required"}), 400
        if method not in PRODUCER_ALLOWED_METHODS:
            return jsonify({"error": f"method not allowed: {method}"}), 400

        # Params keys only (never values) in the debug log -- unchanged
        # discipline from before this proxy went through the shared
        # Primary/Fallback resolver, which does its own redacted audit
        # logging (_audit_device_request) regardless of this line.
        try:
            param_keys = list(params.keys()) if isinstance(params, dict) else [type(params).__name__]
        except Exception:
            param_keys = []
        logging.info(f"[producer-proxy] Calling {method} on {ip} with params keys: {param_keys}")

        # Primary/Fallback credential resolution (family spec Part 4),
        # shared with _ws_call_auth so both authenticated call paths behave
        # identically. Preserves the original request id from the frontend.
        # honor_caller_password_override=True: this proxy is the one path
        # a caller (e.g. the Reboot button, which sends its own idea of
        # the current password in params.password -- see ui/index.html)
        # can legitimately supply an explicit auth override; it is used
        # verbatim, once, and never mutates Settings or the credential
        # cache. Any other key in params (e.g. a LoginPassword.Set new-
        # value field) is untouched -- only the "password" key is treated
        # as the authentication credential.
        forced_id = request_id if request_id else None
        resp = _resolve_ws_auth_response(ip, method, params, timeout, forced_id=forced_id,
                                          honor_caller_password_override=True)
        if resp is None:
            logging.warning(f"[producer-proxy] No response from {ip} for {method}")
            return jsonify({"error": "device_no_response", "method": method})
        logging.debug("[producer-proxy] response fields: %s", sorted(resp) if isinstance(resp, dict) else type(resp).__name__)
        return jsonify(resp)
    except Exception as e:
        logging.error(f"[producer-proxy] Exception: {e}")
        return luma_request.internal_error(e)

@app.post("/api/producer/change_password")
def api_producer_change_password():
    """Proxy legacy HTTP password change to a unit.

    Body: { "ip": "10.1.1.13", "username": "admin", "oldpassword": "password", "newpassword": "newpass", "timeout": 8.0 }
    Device success contract: HTTP 200 + JSON {"code": 0}
    """
    try:
        body = luma_request.json_object(force=True)
        ip = str(body.get("ip") or "").strip()
        username = str(body.get("username") or CONFIG.get("username", "admin") or "admin").strip() or "admin"
        oldpassword = str(body.get("oldpassword") or "")
        newpassword = str(body.get("newpassword") or "")
        timeout = float(body.get("timeout") or 8.0)

        if not ip or not oldpassword or not newpassword:
            return jsonify({"ok": False, "error": "ip, oldpassword and newpassword are required"}), 400

        if not _is_allowed_password(newpassword):
            return jsonify({
                "ok": False,
                "error": "invalid_password",
                "details": "newpassword must be at least 6 characters and may only include A-Z, a-z, 0-9, and !@#$%^&*",
            }), 400

        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return jsonify({"ok": False, "error": "invalid ip"}), 400

        encoded_old = base64.b64encode(oldpassword.encode("utf-8")).decode("ascii")
        encoded_new = base64.b64encode(newpassword.encode("utf-8")).decode("ascii")
        bearer = _producer_mint_http_bearer(username=username, password=oldpassword, role=2)
        url = f"{CONFIG.get('http_scheme', 'http')}://{ip}/change/password"

        headers = {
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
        }
        payload = {
            "username": username,
            "newpassword": encoded_new,
            "oldpassword": encoded_old,
        }

        logging.info(f"[producer-password] POST legacy password change to {ip}")
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout, verify=bool(CONFIG.get("http_verify_tls", False)), allow_redirects=False)

        response_json = None
        response_text = ""
        try:
            response_json = resp.json()
        except Exception:
            response_text = resp.text[:200]

        ok = resp.ok and isinstance(response_json, dict) and response_json.get("code") == 0
        if ok:
            return jsonify({"ok": True, "result": response_json})

        return jsonify({
            "ok": False,
            "status": resp.status_code,
            "result": response_json,
            "error": response_text or "password_change_failed",
        }), 502
    except Exception as e:
        logging.error(f"[producer-password] Exception: {e}")
        return luma_request.internal_error(e)

@APP.post("/api/producer/test_device")
def test_device():
    """
    Diagnostic endpoint to test device connectivity.
    Body: { "ip": "203.0.113.41", "timeout": 3.0 }
    """
    try:
        body = luma_request.json_object(force=True)
        ip = body.get("ip")
        timeout = float(body.get("timeout") or 3.0)
        
        if not ip:
            return jsonify({"error": "ip is required"}), 400
        
        # Log diagnostic info
        logging.info(f"[test_device] Testing connectivity to {ip} (timeout={timeout}s)")
        
        # Test TCP connectivity first
        tcp_ok = _tcp_probe(ip, int(CONFIG.get("http_port", 80)), int(CONFIG.get("TCP_TIMEOUT_MS", 400)))
        logging.debug(f"[test_device] TCP probe to {ip}: {tcp_ok}")
        
        # Test WebSocket connectivity    
        ws_results = {}
        for p in CONFIG["ws_paths"]:
            url = _ws_url(ip, p)
            try:
                ws = create_connection(url, timeout=timeout, redirect_limit=0)
                ws.settimeout(timeout)
                
                # Try simple ping
                ping_payload = {"jsonrpc": "2.0", "id": "test_ping", "method": "System.Ping"}
                ws.send(json.dumps(ping_payload))
                raw = ws.recv()
                
                try:
                    data = json.loads(raw)
                    ws_results[p] = {"status": "ok", "response": data}
                    logging.debug("[test_device] WebSocket %s answered (%s)", p, type(data).__name__)
                except Exception as je:
                    ws_results[p] = {"status": "invalid_json", "raw": raw[:100]}
                    logging.warning("[test_device] Invalid JSON from %s (%d characters)", p, len(raw or ""))
                    
                try:
                    ws.close()
                except Exception:
                    pass
                    
                # Return first successful path
                if ws_results[p]["status"] == "ok":
                    return jsonify({"ok": True, "ip": ip, "tcp": tcp_ok, "websocket": p, "details": ws_results[p]["response"]})
            except socket.timeout:
                ws_results[p] = {"status": "timeout"}
                logging.debug(f"[test_device] WebSocket timeout on {p}")
            except ConnectionRefusedError:
                ws_results[p] = {"status": "refused"}
                logging.debug(f"[test_device] Connection refused on {p}")
            except Exception as e:
                ws_results[p] = {"status": "error", "message": str(e)}
                logging.debug(f"[test_device] Error on {p}: {e}")
        
        # No successful WebSocket connection
        return jsonify({
            "ok": False, 
            "ip": ip,
            "tcp": tcp_ok, 
            "websocket": "failed",
            "attempts": ws_results,
            "message": f"Could not connect to WebSocket on {ip} via paths: {list(ws_results.keys())}"
        }), 503
        
    except Exception as e:
        logging.error(f"[test_device] Exception: {e}")
        return luma_request.internal_error(e)

# --- [producer integration] END ---
# --- [producer uploads] BEGIN ---
import base64, time
from flask import request, jsonify, send_from_directory
import requests

def _mint_bearer(username="admin", password="password"):
    p1 = json.dumps({"create_time": str(int(time.time())), "role": 0}).encode()
    p2 = json.dumps({"password": password, "username": username}).encode()
    b1 = base64.b64encode(p1).decode().rstrip("=")
    b2 = base64.b64encode(p2).decode().rstrip("=")
    return f"{b1}.{b2}"

def _save_slot_file(folder, filename, slot):
    """
    Ensure saved name ends with slot number before extension: *{slot}.jpg|png
    Example: 'foo.png', slot 3 -> 'foo_3.png'
    """
    name, ext = os.path.splitext(filename)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name) + f"_{slot}" + ext.lower()
    path = os.path.join(folder, safe)
    with open(path, "wb") as f:
        f.write(request.files["file"].read())
    return path


# ====== PRODUCER: ASSETS + PUSH EXISTING FILES ======
import os, re, json, time, base64
from flask import jsonify, request
import requests

def _ui_root():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")

def _mint_bearer(username="admin", password="password"):
    p1 = json.dumps({"create_time": str(int(time.time())), "role": 0}).encode()
    p2 = json.dumps({"password": password, "username": username}).encode()
    b1 = base64.b64encode(p1).decode().rstrip("=")
    b2 = base64.b64encode(p2).decode().rstrip("=")
    return f"{b1}.{b2}"

def _scan_assets():
    """
    Return structure:
    {
      "images": { "1": {"jpg": "/ui/imagestream/foo1.jpg"}, "2": {"png": "/ui/imagestream/bar2.png", "jpg": ...}, ... },
      "logos":  { "1": {"png": "/ui/logos/logo1.png"}, ... }
    }
    Files match '*<slot>.jpg|png' (slot = trailing digits before extension).
    """
    # Allow paths to be configured via CONFIG; fall back to bundled UI paths
    root = _ui_root()
    # Respect configured paths, but only use them if they exist. Otherwise fall back
    # to the bundled UI directories so the UI continues working.
    cfg_img = CONFIG.get("imagestream_path")
    cfg_logo = CONFIG.get("logos_path")
    ui_img = os.path.join(root, "imagestream")
    ui_logo = os.path.join(root, "logos")

    if isinstance(cfg_img, str) and os.path.isdir(os.path.abspath(cfg_img)):
        img_dir = os.path.abspath(cfg_img)
    else:
        img_dir = os.path.abspath(ui_img)
        os.makedirs(img_dir, exist_ok=True)

    if isinstance(cfg_logo, str) and os.path.isdir(os.path.abspath(cfg_logo)):
        logo_dir = os.path.abspath(cfg_logo)
    else:
        logo_dir = os.path.abspath(ui_logo)
        os.makedirs(logo_dir, exist_ok=True)

    pat = re.compile(r".*?(\d+)\.(jpg|jpeg|png)$", re.IGNORECASE)
    out = {"images": {}, "logos": {}}

    def scan_dir(dpath, web_prefix, kind=""):
        found = {}
        for name in os.listdir(dpath):
            full = os.path.join(dpath, name)
            if not os.path.isfile(full):
                continue
            m = pat.match(name)
            if not m:
                continue
            slot, ext = m.group(1), m.group(2).lower()
            ext = "jpg" if ext == "jpeg" else ext
            found.setdefault(slot, {})
            # If the directory is inside the UI_DIR we can point to /ui/...
            try:
                ui_dir_abs = os.path.abspath(UI_DIR)
                full_dir_abs = os.path.abspath(dpath)
                if os.path.commonpath([ui_dir_abs, full_dir_abs]) == ui_dir_abs:
                    rel = os.path.relpath(full_dir_abs, ui_dir_abs).replace(os.sep, '/')
                    prefix = f"/ui/{rel}" if rel and rel != '.' else "/ui"
                    found[slot][ext] = f"{prefix}/{name}"
                else:
                    # Serve via API endpoint for files outside UI_DIR
                    prefix = f"/api/producer2/asset/{kind}"
                    found[slot][ext] = f"{prefix}/{name}"
            except Exception:
                found[slot][ext] = f"{web_prefix}/{name}"
        return found
    out["images"] = scan_dir(img_dir, "/ui/imagestream", kind="imagestream")
    out["logos"]  = scan_dir(logo_dir, "/ui/logos", kind="logos")
    return out


@APP.get("/api/producer2/asset/<kind>/<path:filename>")
def api_producer_asset(kind, filename):
    """Serve asset files from configured directories when they live outside the UI tree."""
    mapping = {
        "imagestream": "imagestream_path",
        "logos": "logos_path",
        "firmware": "firmware_path",
    }
    key = mapping.get(kind)
    if not key:
        abort(404)
    dirpath = CONFIG.get(key) or ""
    if not dirpath:
        abort(404)
    dirpath = os.path.abspath(dirpath)
    full = os.path.join(dirpath, filename)
    if not os.path.isfile(full):
        abort(404)
    # send_from_directory will handle safe file serving
    return send_from_directory(dirpath, filename)


@APP.get('/api/list_dir')
def api_list_dir():
    """Return a JSON listing of directories under the given path.
    Query params:
      - path: optional filesystem path. If missing, returns BASE_DIR.
    Returns:
      { path: abs_path, parent: parent_path_or_null, entries: [{name, is_dir}] }
    """
    p = (request.args.get('path') or '').strip()
    if not p:
        p = BASE_DIR
    try:
        # Normalize Windows-style paths that may arrive as '/C:/path' from browser encoding
        if os.name == 'nt' and re.match(r'^/[A-Za-z]:', p):
            p = p.lstrip('/')
        abs_p = os.path.abspath(p)
        if not os.path.isdir(abs_p):
            return jsonify({"ok": False, "error": "not_a_directory", "path": abs_p}), 400
        entries = []
        try:
            for name in sorted(os.listdir(abs_p)):
                full = os.path.join(abs_p, name)
                try:
                    entries.append({"name": name, "is_dir": os.path.isdir(full)})
                except Exception:
                    # ignore entries we can't stat
                    continue
        except Exception:
            entries = []
        parent = os.path.dirname(abs_p) if abs_p and os.path.dirname(abs_p) != abs_p else None
        return jsonify({"ok": True, "path": abs_p, "parent": parent, "entries": entries})
    except Exception as e:
        return luma_request.internal_error(e)

# ====== END PRODUCER BLOCK ======
# --- [producer persistent state] BEGIN ---
def _load_producer_state():
    try:
        if os.path.exists(PRODUCER_STATE_FILE):
            with open(PRODUCER_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("ticker", {"dir": "rtl", "rows": []})
                    data.setdefault("streams", [])
                    return data
    except Exception as e:
        logging.warning(f"[producer_state] load failed: {e}")
    return {"ticker": {"dir": "rtl", "rows": []}, "streams": []}

def _save_producer_state(data):
    try:
        if not luma_store.atomic_write_json(PRODUCER_STATE_FILE, data):
            raise OSError("producer state could not be written")
        logging.info(f"[producer_state] saved -> {PRODUCER_STATE_FILE}")
    except Exception as e:
        logging.warning(f"[producer_state] save failed: {e}")

@APP.get("/api/producer2/state")
def get_producer_state():
    """Return persistent ticker and RTMP stream data"""
    return jsonify(_load_producer_state())

@APP.post("/api/producer2/state")
def post_producer_state():
    """Write persistent ticker and RTMP stream data"""
    data = luma_request.json_object()
    _save_producer_state(data)
    return jsonify({"ok": True})
    
@app.route("/api/producer2/state", methods=["GET","POST"])
def api_producer2_state():
    if request.method == "POST":
        data = luma_request.json_object(force=True)
        if not luma_store.atomic_write_json(PRODUCER2_STATE_FILE, data):
            return jsonify({"ok": False, "error": "could not save"}), 500
        return jsonify({"ok": True})
    if os.path.exists(PRODUCER2_STATE_FILE):
        with open(PRODUCER2_STATE_FILE, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    return jsonify({"streams": [], "ticker": {}})

# --- [producer persistent state] END ---

# ------------------------------------------------------------------ backup
#
# Export and restore are LOCAL_ONLY, and that is a security decision rather than a
# convenience one. A backup contains the device credentials, which this
# application never hands to anything but the computer it runs on; an export
# endpoint reachable from the network would be a second, quieter way to the same
# secret. It also matches how a disaster unfolds: somebody is standing at the
# replacement PC with the backup file, and the local operator is trusted by the
# socket the request arrived on -- which is what lets a technician who never knew
# the old administrator's password rebuild the deployment anyway.

BACKUP_SNAPSHOT_DIR = os.path.join(DATA_DIR, "restore-snapshot")

_restore_lock = threading.Lock()
_restore_in_progress = False
#: Set when a restore left the files and the running process disagreeing.
_restart_required = False


@APP.before_request
def _refuse_writes_while_restoring():
    """Configuration is not written while a restore runs, or after one that left the
    process disagreeing with the disk. Reads and sign-in are unaffected."""
    if not (_restore_in_progress or _restart_required):
        return None
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if request.path.startswith(("/api/backup/", "/api/auth/")):
        return None
    reason = "restore_in_progress" if _restore_in_progress else "restart_required"
    return jsonify({
        "ok": False, "reason": reason,
        "error": ("A restore is running. Try again in a moment." if _restore_in_progress else
                  "The configuration was restored but LumaSuite could not load all of it. "
                  "Close and reopen LumaSuite before changing anything.")}), 409


def _backup_upload():
    """The uploaded file's bytes, however the caller sent them."""
    upload = request.files.get("backup")
    if upload is not None:
        return upload.read()
    if request.data:
        return request.data
    return None


def _restore_write(path, content):
    """The one way a restore writes a file: atomic, narrowed, and loud on failure."""
    if not luma_store.atomic_write_json(path, content, private=True):
        raise OSError("could not write " + os.path.basename(str(path)))


@APP.post("/api/backup/export")
def api_backup_export():
    """Encrypt this deployment into one file the operator saves somewhere else."""
    data = luma_request.json_object()
    passphrase = data.get("passphrase") or ""
    # Named fields only, and never the passphrase: a verbatim request dump would
    # put it in the log.
    logging.info("[backup] export requested")
    try:
        payload = luma_backup.build_payload(DATA_DIR, _load_units_snapshot(), app_version=__version__)
        blob = luma_backup.seal(payload, passphrase, app_version=__version__)
    except luma_backup.BackupError as e:
        logging.warning("[backup] export refused: %s", e.reason)
        return jsonify({"ok": False, "reason": e.reason, "error": e.message}), 400
    summary = luma_backup.summarise(payload, {"created_at": "", "lumasuite_version": __version__,
                                              "format": luma_backup.FORMAT_VERSION})
    REMOTE.store.audit("backup.exported", actor=None, detail={"sections": len(summary["sections"])})
    response = APP.response_class(blob, mimetype="application/octet-stream")
    response.headers["Content-Disposition"] = 'attachment; filename="%s"' % luma_backup.suggested_filename(__version__)
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-LumaSuite-Backup-Sections"] = str(len(summary["sections"]))
    return response


@APP.post("/api/backup/inspect")
def api_backup_inspect():
    """Decrypt and validate, and change absolutely nothing.

    This is what the confirmation screen is built from, so an operator decides
    with the real contents in front of them rather than a filename.
    """
    blob = _backup_upload()
    if blob is None:
        return jsonify({"ok": False, "reason": "no_file", "error": "Choose a backup file first."}), 400
    try:
        header, payload = luma_backup.unseal(blob, request.form.get("passphrase") or "")
    except luma_backup.BackupError as e:
        logging.warning("[backup] inspect refused: %s", e.reason)
        return jsonify({"ok": False, "reason": e.reason, "error": e.message}), 400
    problems = luma_backup.validate(payload)
    if problems:
        return jsonify({"ok": False, "reason": "invalid",
                        "error": "This backup is missing something it needs: " + "; ".join(problems),
                        "problems": problems}), 400
    return jsonify({"ok": True, "summary": luma_backup.summarise(payload, header, DATA_DIR)})


@APP.post("/api/backup/restore")
def api_backup_restore():
    """Replace this deployment's configuration, or leave it exactly as it is.

    Read, authenticate, validate, snapshot, apply, verify. Nothing is written
    until every one of the first four has succeeded, and if the apply fails
    partway the snapshot goes back.
    """
    blob = _backup_upload()
    if blob is None:
        return jsonify({"ok": False, "reason": "no_file", "error": "Choose a backup file first."}), 400
    logging.info("[backup] restore requested")
    try:
        header, payload = luma_backup.unseal(blob, request.form.get("passphrase") or "")
    except luma_backup.BackupError as e:
        logging.warning("[backup] restore refused: %s", e.reason)
        return jsonify({"ok": False, "reason": e.reason, "error": e.message}), 400
    problems = luma_backup.validate(payload)
    if problems:
        logging.warning("[backup] restore refused: invalid payload")
        return jsonify({"ok": False, "reason": "invalid",
                        "error": "This backup is missing something it needs: " + "; ".join(problems),
                        "problems": problems}), 400
    summary = luma_backup.summarise(payload, header, DATA_DIR)

    # One restore at a time, and nothing else writing configuration while it runs.
    # The account, name and group stores keep their contents in memory and rewrite
    # the whole file on any change, so a change made between "users.json restored"
    # and "reloaded from disk" would write the old accounts straight back over the
    # restored ones. Holding their locks across the whole transaction closes that;
    # the flag makes every other configuration write answer 409 rather than queue.
    global _restore_in_progress
    if not _restore_lock.acquire(blocking=False):
        return jsonify({"ok": False, "reason": "restore_in_progress",
                        "error": "A restore is already running."}), 409
    _restore_in_progress = True
    try:
        with REMOTE.store._lock, REMOTE.aliases._lock, REMOTE.groups._lock:
            return _restore_locked(payload, summary)
    finally:
        _restore_in_progress = False
        _restore_lock.release()


_ABSENT = object()


def _restore_merge_cache(payload):
    """Put the backup's learned device facts onto the inventory as it is right now.

    The inventory is read, merged and written back inside ONE `cache_lock` section, so
    what a scan, a matrix refresh or a liveness check wrote a moment earlier is the thing
    merged into, and nothing is written from a copy that has since gone stale. The merge
    only fills a field the live row does not have, and it fills the live row in place
    (never swapping in a copy, never adding a row), so a unit that moved address keeps
    the one row it has and a unit that was removed is not brought back.

    Returns (matched, unmatched, learned) where `learned` lists every field written as
    (key, field, previous value or _ABSENT, new value) so it can be taken back out.
    """
    learned = []
    with cache_lock:
        rows = [(key, row) for key, row in cache_units.items() if isinstance(row, dict)]
        merged, matched, unmatched = luma_backup.merge_devices(payload, [row for _, row in rows])
        try:
            for (key, row), new_row in zip(rows, merged):
                for field, value in new_row.items():
                    previous = row.get(field, _ABSENT)
                    if previous is _ABSENT or previous != value:
                        row[field] = value
                        learned.append((key, field, previous, value))
        except BaseException:
            _restore_unmerge_cache(learned, _held=True)
            raise
    return matched, unmatched, learned


def _restore_unmerge_cache(learned, _held=False):
    """Take back exactly what `_restore_merge_cache` wrote, and nothing a writer added since.

    A field is put back only while it still holds the value the merge wrote; one a scan has
    changed in the meantime is that scan's, not ours. Returns how many fields were restored.
    """
    def undo():
        done = 0
        for key, field, previous, value in reversed(learned):
            row = cache_units.get(key)
            if not isinstance(row, dict) or row.get(field, _ABSENT) != value:
                continue
            if previous is _ABSENT:
                row.pop(field, None)
            else:
                row[field] = previous
            done += 1
        return done
    if _held:
        return undo()
    with cache_lock:
        return undo()


def _restore_locked(payload, summary):
    """Snapshot, apply, reload -- with every store the restore replaces held."""
    global _restart_required

    # The way back exists before there is anything to come back from.
    try:
        saved = luma_backup.snapshot(DATA_DIR, BACKUP_SNAPSHOT_DIR)
    except OSError as e:
        logging.error("[backup] could not take a rollback snapshot: %s", e)
        return jsonify({"ok": False, "reason": "no_snapshot",
                        "error": "The current configuration could not be saved first, so "
                                 "nothing was changed."}), 500
    try:
        applied = luma_backup.apply_payload(payload, DATA_DIR, write_json=_restore_write)
    except Exception as e:
        logging.error("[backup] restore failed partway: %s", type(e).__name__)
        restored, failures = luma_backup.rollback(BACKUP_SNAPSHOT_DIR, DATA_DIR, saved)
        status = "FAILED \u2014 ROLLED BACK" if not failures else "FAILED \u2014 ROLLBACK INCOMPLETE"
        body = {"ok": False, "reason": "apply_failed", "status": status,
                "error": "The restore could not be completed. " +
                         ("The previous configuration has been put back." if not failures else
                          "Some files could not be put back: " + ", ".join(failures)),
                "rolled_back": restored, "rollback_failures": failures}
        if failures:
            # Never claim a restoration that did not happen, and keep the way back: the
            # snapshot is the only complete copy of the old state. Further saves are
            # refused too, because the process still holds the old state and would
            # write it over whatever is on disk.
            _restart_required = True
            body["recovery_snapshot"] = str(BACKUP_SNAPSHOT_DIR)
            logging.error("[backup] rollback INCOMPLETE (%s); the previous configuration "
                          "is preserved in %s", ", ".join(failures), BACKUP_SNAPSHOT_DIR)
        return jsonify(body), 500

    # Re-read everything from what is now on disk, so a restored deployment works
    # without a restart.
    matched = unmatched = 0
    devices_saved = True
    ended = {"gone": 0, "password": 0, "direct": 0, "changed": 0}
    try:
        # What each session was opened against, taken before the accounts are replaced.
        fingerprints = REMOTE.store.credential_fingerprints()
        REMOTE.store._load()
        # A session survives only if its account is security-equivalent afterwards: an account
        # the backup removed or disabled, or restored with another password, role, permission
        # or Direct Access link, must not keep a session opened for the one it replaced.
        ended = REMOTE.store.revoke_sessions_outdated_by(fingerprints)
        REMOTE.aliases._load()
        REMOTE.groups.reload()
        _load_persisted_config()
        _invalidate_credential_cache("backup_restored")
        matched, unmatched, learned = _restore_merge_cache(payload)
        inventory_saved = _persist_units_cache()
        if learned and not inventory_saved:
            # The restored files are fine, but the device details the backup supplied could not
            # be saved. Never say they were: put the inventory back to what it was before this
            # restore touched it, say so, and leave the next scan to fill them in.
            devices_saved = False
            devices_undone = _restore_unmerge_cache(learned)
            logging.error("[backup] restored, but the device inventory could not be saved; "
                          "%d learned field(s) taken back out of memory", devices_undone)
    except Exception as e:
        # The files are written and valid; what failed is this process picking them
        # up. Saying so is better than rolling back a restore that succeeded.
        logging.error("[backup] restored, but reloading in place failed: %s", type(e).__name__)
        # The files are the restored ones; the process may still hold the old state. The
        # next ordinary save would write that over the restore, so saving stops until the
        # application is restarted.
        _restart_required = True
        return jsonify({"ok": True, "status": "RESTORED \u2014 RESTART REQUIRED", "summary": summary,
                        "applied": applied,
                        "error": "The configuration was restored, but LumaSuite could not load "
                                 "all of it without restarting. Close and reopen LumaSuite."}), 200
    _restart_required = False
    # Past this point the restore HAS happened, so nothing left to do may report it
    # as a failure.
    try:
        REMOTE.store.audit("backup.restored", actor=None,
                           detail={"files": len(applied["written"]),
                                   "sessions_ended": sum(ended.values())})
        admins = [a.get("username") for a in REMOTE.store.list_accounts()
                  if a.get("role") == "admin" and a.get("enabled")]
    except Exception:
        admins = []
    note = ("If nobody knows the restored administrator passwords, set a new one in Accounts on "
            "this computer.") if admins else (
            "No administrator account exists now. On this computer, open LumaSuite and create one "
            "in Accounts; nobody can sign in remotely until you do.")
    body = {"ok": True, "status": "RESTORED", "summary": summary, "applied": applied,
            "devices": {"matched": matched, "awaiting_scan": unmatched, "saved": devices_saved},
            "sessions_ended": ended,
            "admin_recovery": {"administrators": admins, "note": note}}
    if not devices_saved:
        body["warning"] = ("The configuration was restored, but the device details from the backup "
                           "could not be saved to this computer's disk, so they were not applied. "
                           "Run a Scan; LumaSuite will learn them again from the equipment.")
    return jsonify(body)


# ---------------------------------------------------------------- previews
#
# Native device pictures for the restricted routing page. The browser never
# fetches a device and never learns an address: the server fetches, after the
# caller has been authorised, and returns the image bytes.
#
#   display (decoder)   /thumbnail/thumbnail1.jpg   -- the decoder's own picture
#   source (encoder)    /stream?...                 -- the mechanism LumaSuite's own
#                                                      pages have always used
#
# The encoder's `thumbnail1.jpg` has NOT been verified on hardware, so it is not
# assumed: `LUMA_ENCODER_PREVIEW` selects `stream` (default, the known-working
# mechanism), `thumbnail`, or `auto` (thumbnail first, then stream).

PREVIEW_TIMEOUT_SECONDS = 4.0
PREVIEW_MAX_BYTES = 2 * 1024 * 1024
PREVIEW_TTL_SECONDS = float(os.environ.get("LUMA_PREVIEW_TTL", "5"))
ENCODER_PREVIEW_MODE = (os.environ.get("LUMA_ENCODER_PREVIEW") or "stream").strip().lower()
_preview_cache = luma_statecache.StateCache(ttl=PREVIEW_TTL_SECONDS)


def _preview_http_get(url, **kwargs):
    """The one place a preview touches the network, so it can be fenced and stubbed.

    Redirects are never followed. The address is a device's and the path is fixed, so a 3xx can
    only be a device (or something impersonating one) trying to point this server at a URL of
    its choosing -- another host, this computer's own services, a cloud metadata address. A
    redirect is simply "no preview".
    """
    kwargs["allow_redirects"] = False
    return requests.get(url, **kwargs)


def _preview_address_ok(address) -> bool:
    """Whether `address` is an ordinary device address a preview may be fetched from.

    A literal IP that is not this computer, not 'any', not multicast, and not otherwise
    reserved. The unit cache is built from what a scan found, so it is not trusted to hold
    something safe to put in a URL.
    """
    try:
        ip = ipaddress.ip_address(str(address).strip())
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_reserved)


def _valid_jpeg(data: bytes) -> bool:
    """Whether `data` is a JPEG. Only a JPEG is ever passed to a browser as one.

    Checked by decoding the header, not by the device's Content-Type and not by the first two
    bytes: another image format (an SVG, a GIF, a PNG) must not be relayed as `image/jpeg`.
    """
    if data[:2] != b"\xff\xd8":
        return False
    try:
        from PIL import Image
        image = Image.open(io.BytesIO(data))
        if image.format != "JPEG":
            return False
        image.verify()
        return True
    except ImportError:
        return data[-2:] == b"\xff\xd9"
    except Exception:
        return False


def _preview_chunks(resp):
    """The body in whatever pieces arrive, so the caller's deadline is checked on every one.

    `iter_content(n)` blocks until n bytes have arrived, so a device sending one byte at a
    time would never let the deadline be looked at -- it would hold a server thread for as long
    as it kept dribbling. `read1` returns what is there.
    """
    read1 = getattr(resp.raw, "read1", None)
    if read1 is None:                                  # an old urllib3: smallest safe piece
        yield from resp.iter_content(256)
        return
    while True:
        chunk = read1(4096)
        if not chunk:
            return
        yield chunk


def _preview_still(url):
    """One still from a device URL: a plain image, or the first frame of an MJPEG stream."""
    resp = _preview_http_get(url, stream=True, timeout=(PREVIEW_TIMEOUT_SECONDS, PREVIEW_TIMEOUT_SECONDS))
    try:
        if resp.status_code != 200:
            return None
        ctype = str(resp.headers.get("Content-Type", "")).split(";")[0].strip().lower()
        deadline = time.monotonic() + PREVIEW_TIMEOUT_SECONDS * 2
        buf = bytearray()
        for chunk in _preview_chunks(resp):
            buf.extend(chunk)
            if len(buf) > PREVIEW_MAX_BYTES or time.monotonic() > deadline:
                return None
            if ctype == "image/jpeg":
                continue            # a plain picture: read it to the end
            start = buf.find(b"\xff\xd8")
            if start < 0:
                continue
            end = buf.find(b"\xff\xd9", start + 2)
            while end >= 0:
                frame = bytes(buf[start:end + 2])
                if _valid_jpeg(frame):
                    return frame
                end = buf.find(b"\xff\xd9", end + 2)
        if ctype == "image/jpeg" and buf and _valid_jpeg(bytes(buf)):
            return bytes(buf)
        return None
    finally:
        try:
            resp.close()
        except Exception:
            pass


def _preview_urls(kind, address, unit_type):
    thumb = f"http://{address}/thumbnail/thumbnail1.jpg"
    stream = f"http://{address}/stream?resolution=320x180&fps=15&bitrate=512"
    if kind == "display":
        if unit_type == "switcher":
            # A CS31 serves no picture (its preview paths answer 404). Asking would be device
            # traffic for nothing, and showing anything else would be a fake preview.
            return []
        return [thumb]
    if ENCODER_PREVIEW_MODE == "thumbnail":
        return [thumb]
    if ENCODER_PREVIEW_MODE == "auto":
        return [thumb, stream]
    return [stream]


def _fetch_preview_uncached(kind, address):
    if not _preview_address_ok(address):
        logging.info("[preview] %s: refused an address that is not an ordinary device address", kind)
        return {"image": None, "reason": "preview_unavailable"}
    with cache_lock:
        unit_type = (cache_units.get(address, {}).get("type") or "").lower()
    for url in _preview_urls(kind, address, unit_type):
        luma_previewstats.note("device", kind)
        try:
            frame = _preview_still(url)
        except Exception as exc:
            logging.info("[preview] %s %s: %s", kind, "fetch failed", type(exc).__name__)
            continue
        if frame:
            return {"image": frame, "type": "image/jpeg"}
    return {"image": None, "reason": "preview_unavailable"}


#: `fresh` asks for a new picture instead of the shared one. Honoured at most this often per
#: device: a caller repeating it would otherwise turn "twenty-five viewers cost one fetch" into
#: one fetch per request.
PREVIEW_FRESH_MIN_INTERVAL = 2.0
_preview_fresh_at = {}
_preview_fresh_lock = threading.Lock()


def _fetch_preview(kind, address, fresh=False):
    """A preview picture, shared: ten pages looking at one device cost one fetch."""
    key = f"{kind}:{address}"
    if fresh:
        now = time.monotonic()
        with _preview_fresh_lock:
            if len(_preview_fresh_at) > 4096:
                for stale in [k for k, t in _preview_fresh_at.items()
                              if now - t > PREVIEW_FRESH_MIN_INTERVAL]:
                    del _preview_fresh_at[stale]
            allowed = now - _preview_fresh_at.get(key, -1e9) >= PREVIEW_FRESH_MIN_INTERVAL
            if allowed:
                _preview_fresh_at[key] = now
        if allowed:
            _preview_cache.invalidate(key)
    return _preview_cache.get(key, lambda: _fetch_preview_uncached(kind, address))


# --------------------------------------------------------- shared observation
#
# What the restricted page shows about displays comes from the host's cache. This
# refreshes it, once per window for everybody, so a room of phones is one device
# poll rather than one each.

_observe_cache = luma_statecache.StateCache(ttl=float(os.environ.get("LUMA_OBSERVE_TTL", "5")))


#: The longest a page read waits for the shared device refresh. A refresh that meets an unreachable
#: unit outlasts this, and the page is then served from what was last read, with its age.
OBSERVE_WAIT_SECONDS = float(os.environ.get("LUMA_OBSERVE_WAIT", "0.5"))


def _observe_matrix_state():
    return _observe_cache.get_bounded("matrix", _refresh_matrix_state_core, OBSERVE_WAIT_SECONDS)


def _wall_directory():
    """The saved walls, read from the file as it is now (never a copy kept here)."""
    return [w for w in _load_video_wall_state().get("walls", []) if isinstance(w, dict)]


def _forget_missing_wall_grants():
    """Drop account grants for walls that no longer exist, so none outlives its wall."""
    try:
        keep = {str(w.get("id") or "") for w in _wall_directory()}
        return REMOTE.store.forget_walls(keep)
    except Exception as exc:
        logging.warning("[video_wall] could not clean wall grants: %s", type(exc).__name__)
        return 0


class _WallMember:
    """One decoder of a saved wall, ready to be routed. `run()` is one device operation."""

    def __init__(self, plan, cell, identity):
        self.plan = plan
        self.cell = cell
        self.identity = identity

    def run(self):
        return self.plan.route_member(self)


class _WallPlan:
    """A saved wall prepared for one routing plane: who will be written, and how it reads.

    `early` is a finished answer when nothing can be written at all (source offline or
    silent, no followed cells); otherwise `members` are the decoders to route -- by the
    caller, in whatever order or concurrency it likes, each under its own display lock --
    and `finish(rows)` turns their outcomes into the wall's one answer. Membership is
    resolved here, on the server, from the saved wall and the inventory as they are now.
    """

    def __init__(self, wall, source_identity, plane):
        self.plane = plane
        self.early = None
        self.members = []
        self.source_identity = source_identity
        self.wall = wall
        units = _load_units_snapshot()
        rows = luma_walls.members(wall, units, _vw_device_id)
        followed = [(c, i, u) for c, i, u in rows
                    if (c.get("source_mode") or "wall") == "wall" and not _vw_cell_region(wall, c)]
        self.total = len(followed)
        self.other = len(rows) - self.total
        self.result = {"ok": False, "status": "failed", "routed": 0, "total": self.total,
                       "other_sources": self.other}
        source_unit = next((u for u in units if luma_projection.unit_identity(u) == source_identity
                            and luma_projection.unit_kind(u) == "source"), None)
        if source_unit is None:
            self.early = dict(self.result, status="not_found")
            return
        if not luma_projection.is_online(source_unit):
            self.early = dict(self.result, status="offline")
            return
        if not self.total:
            self.early = dict(self.result, status="failed")
            return
        # The encoder's address is proven too, once per wall: a re-leased source address would
        # otherwise supply another device's stream to every member.
        source_reason = _wall_identity_refusal(source_unit, units)
        if source_reason:
            logging.warning("[wall-route] the source's address was not confirmed (%s); nothing was sent",
                            source_reason)
            self.early = dict(self.result, reason=source_reason,
                              status="unreachable" if source_reason == "identity_unconfirmed" else "failed")
            return
        self.source_unit = source_unit
        self.source_id = _vw_device_id(source_unit)
        # The stream is read from the encoder now. The administrator's Video Wall page may fall
        # back to the name the cache remembers; a User's route never does -- a source that does
        # not answer is not routed from what it once said.
        live = _vw_call(source_unit.get("ip"), "VideoMultiIpStream.Get") if source_unit.get("ip") else {}
        live_result = live.get("result") if live.get("ok") and isinstance(live.get("result"), dict) else {}
        self.stream = str(live_result.get("streamname") or "").strip()
        if not self.stream:
            self.early = dict(self.result, status="unreachable")
            return
        self.routed_wall = dict(wall, source_id=self.source_id)
        self.members = [_WallMember(self, c, i) for c, i, _u in followed]

    # ---- one decoder -------------------------------------------------------
    def _held(self, ip, cell):
        rows_, cols_, row_, col_, _enable, _src = _vw_effective_matrix_and_source(self.routed_wall, cell)
        matrix = f"{rows_}_{cols_}_{row_}_{col_}"
        for attempt in range(ROUTE_READBACK_ATTEMPTS):
            selected = _vw_decoder_selected_stream(ip)
            info = _vw_decoder_wall_info(ip)
            if (selected.get("ok") and selected.get("streamname") == self.stream
                    and info.get("ok") and bool(info.get("vmenable"))
                    and info.get("vwmatrix") == matrix):
                return True
            if attempt + 1 < ROUTE_READBACK_ATTEMPTS:
                time.sleep(ROUTE_READBACK_PAUSE_SECONDS)
        return False

    @staticmethod
    def _failed(outcome):
        status = outcome.get("status") or "failed"
        return {"ok": False, "result": "failed" if status == "refused" else status,
                "reason": outcome.get("reason") or ""}

    def route_member(self, member):
        """Route one decoder for the plane asked for, and read it back. Never raises."""
        cell, identity = member.cell, member.identity
        if not identity:
            return {"ok": False, "result": "not_found"}
        # Read again now: the lock may have been waited for, and a decoder can have gone since.
        units = _load_units_snapshot()
        held = [u for u in units if luma_projection.unit_identity(u) == identity]
        if not held:
            return {"ok": False, "result": "not_found"}
        unit = held[0]            # one identity under two addresses is refused just below
        if not luma_projection.is_online(unit):
            return {"ok": False, "result": "offline"}
        ip = unit.get("ip")
        source_ip = self.source_unit.get("ip")
        # Who is at that address now? Asked before any write, for every plane.
        reason = _wall_identity_refusal(unit, units)
        if reason:
            logging.warning("[wall-route] a member's address was not confirmed (%s); nothing was sent", reason)
            return {"ok": False,
                    "result": "unreachable" if reason == "identity_unconfirmed" else "failed",
                    "reason": reason}
        try:
            audio = "unchanged"
            if self.plane == luma_breakaway.MODE_AUDIO:
                outcome = _route_mode_for_users(ip, luma_breakaway.MODE_AUDIO, source_ip)
                if not outcome.get("ok"):
                    return self._failed(outcome)
                return {"ok": True, "result": "verified", "audio": "routed"}
            if self.plane == luma_breakaway.MODE_VIDEO:
                # Video only: the engine pins this display's audio first, so the wall's own
                # stream selection below cannot drag the audio along with the picture.
                outcome = _route_mode_for_users(ip, luma_breakaway.MODE_VIDEO, source_ip)
                if not outcome.get("ok"):
                    return self._failed(outcome)
            applied = _vw_apply_source_cell(self.routed_wall, cell, confirmed=True)
            if not applied.get("ok"):
                return {"ok": False, "result": "failed"}
            if not self._held(ip, cell):
                return {"ok": False, "result": "not_verified"}
            if self.plane == luma_breakaway.MODE_AV:
                # Both planes to the source, whatever a breakaway left behind, and read back.
                outcome = _route_av_for_users(ip, source_ip)
                if not outcome.get("ok"):
                    return self._failed(outcome)
                audio = outcome.get("audio") or "unknown"
            return {"ok": True, "result": "verified", "audio": audio}
        except Exception as exc:
            logging.warning("[wall-route] a cell raised %s", type(exc).__name__)
            return {"ok": False, "result": "failed"}

    # ---- the wall's one answer ---------------------------------------------
    def finish(self, rows):
        verified = sum(1 for r in rows if r.get("ok"))
        failure = next((r.get("result") for r in rows if not r.get("ok")), "")
        result = dict(self.result, routed=verified)
        if verified == self.total:
            result.update(ok=True, status="verified")
            if self.plane != luma_breakaway.MODE_AUDIO:
                # The saved wall agrees with what the decoders now hold: its source is the one
                # that was just verified, so the Video Wall page does not show a stale source.
                # Audio alone does not change which picture the wall shows.
                try:
                    data = _load_video_wall_state()
                    for saved in data.get("walls", []):
                        if isinstance(saved, dict) and saved.get("id") == self.wall.get("id"):
                            saved["source_id"] = self.source_id
                            saved["updated_at"] = int(time.time())
                    for index, saved in enumerate(data.get("walls", [])):
                        if isinstance(saved, dict) and saved.get("id") == self.wall.get("id"):
                            data["walls"][index] = _vw_enrich_wall(saved)
                    _save_video_wall_state(data)
                except Exception as exc:
                    logging.warning("[wall-route] saved wall not updated: %s", type(exc).__name__)
        elif verified:
            result["status"] = "partial"
        else:
            result["status"] = failure or "failed"
        _vw_invalidate_detect_cache()
        _observe_cache.invalidate("matrix")
        return result


def _plan_wall_route(wall, source_identity, plane=luma_breakaway.MODE_AV):
    """Prepare a saved wall for routing. See `_WallPlan`."""
    return _WallPlan(wall, source_identity, plane)


def _route_wall_for_users(wall, source_identity, plane=luma_breakaway.MODE_AV):
    """Send one permitted source to a saved wall, and read every decoder back.

    The wall is the administrator's saved definition, used as it stands: its grid, its
    decoders and its regions are not changed and nothing is created. What a wall does
    is show one source across its displays, so routing to it means making the wall's
    source the one chosen -- through the same cell operation the Video Wall page uses
    (`_vw_apply_source_cell`) -- and then asking each decoder what it holds.

    `plane` is what the Matrix mode asked for: audio and video (the default), the video
    alone with each member's audio kept, or the audio alone with each member's video
    kept. Cells that follow the wall are routed; a cell the administrator gave its own
    source, or put in a region, keeps it (counted in `other_sources`). A decoder that is
    offline or no longer in the inventory is reported as such and is never written to;
    nothing short of every followed cell holding the new state is reported as success.

    This is the whole wall in one call, member after member. The application's request
    handler does not use it: it takes `_plan_wall_route` and dispatches every member in
    the one bounded parallel batch all multi-display routes share, so the displays change
    together. Returns {ok, status, routed, total, other_sources}.
    """
    plan = _WallPlan(wall, source_identity, plane)
    if plan.early is not None:
        return plan.early
    return plan.finish([member.run() for member in plan.members])


_route_wall_for_users.plan = _plan_wall_route


def _route_av_for_users(decoder_ip, encoder_ip):
    """A restricted User's audio+video route: the one implementation behind every surface.

    Standard Routing, a drop and the User Matrix all arrive here. Audio and video are both
    sent to the source explicitly through the administrator's breakaway engine, so a display
    a breakaway left with its audio elsewhere is brought to the source in full, and a display
    that follows video is left following. A source with no audio stream, and a switcher (which
    has no audio path), take the verified video route as they always did, and the answer says
    whether the audio follows. The shared read is dropped afterwards.
    """
    try:
        with cache_lock:
            is_switcher = (cache_units.get(decoder_ip, {}).get("type") or "").lower() == "switcher"
        if not is_switcher:
            outcome = _route_breakaway(decoder_ip, luma_breakaway.MODE_USER_AV,
                                       encoder_ip, encoder_ip)
            if outcome.get("reason") != "no_source_audio":
                return outcome            # nothing was written when the source had no audio
        return _route_av(decoder_ip, encoder_ip, allow_cached_stream=False, check_audio=True)
    finally:
        _observe_cache.invalidate("matrix")


def _unroute_for_users(decoder_ip):
    """A restricted User's Unroute: one display, video and audio subscribed to nothing, verified.

    The engine is `luma_breakaway.unroute`, built on the device calls measured on hardware. A
    switcher has no audio path and no cleared-stream call has been observed on one, so it is
    refused and never written to. The cache follows what the decoder reported afterwards.
    """
    try:
        with cache_lock:
            is_switcher = (cache_units.get(decoder_ip, {}).get("type") or "").lower() == "switcher"
        if is_switcher:
            return {"ok": False, "status": "refused", "reason": "not_supported"}
        timeout = float(CONFIG.get("TIMEOUT", 3.0))
        outcome = luma_breakaway.unroute(
            lambda method, params: _ws_call_auth(decoder_ip, method, params, timeout),
            password=CONFIG.get("password", "password"))
        after = outcome.get("after")
        if isinstance(after, dict):
            with cache_lock:
                unit = cache_units.get(decoder_ip)
                if isinstance(unit, dict):
                    dec = unit["dec"] if isinstance(unit.get("dec"), dict) else {}
                    unit["dec"] = dec
                    dec.setdefault("video", {})["streamname"] = after.get("video", "")
                    audio = dec.setdefault("audio", {})
                    audio["streamname"] = after.get("audio", "")
                    if isinstance(after.get("follow"), bool):
                        audio["followvideoenable"] = after["follow"]
                    audio["ip"] = after.get("audio_address", "")
                    if outcome.get("ok"):
                        unit.pop("routed_encoder_ip", None)
                        unit.pop("routed_streamname", None)
            _persist_units_cache()
        if outcome.get("ok"):
            logging.info("[unroute] %s verified (%d write(s))", decoder_ip, len(outcome.get("writes") or []))
        else:
            logging.warning("[unroute] %s: %s (%s, rollback %s)", decoder_ip, outcome.get("reason"),
                            outcome.get("status"), outcome.get("rollback"))
        return outcome
    finally:
        _observe_cache.invalidate("matrix")


def _verify_route_for_users(decoder_ip, encoder_ip):
    """Ask a display, read-only, whether it now holds this source's video stream.

    A route that was written but whose read-back did not agree in time is `not_verified`:
    some displays take longer than the read-back window to report a stream they have already
    taken. This is the later look. It writes nothing and reads exactly what the route's own
    read-back read -- the encoder's stream name and the decoder's selected stream -- so a
    display that really did not take the route is still reported as one that did not.

    Returns {"status": "verified" | "not_verified" | "unreachable"}. The cache follows the
    device, as it does after a route, but only when the display agrees.
    """
    timeout = float(CONFIG.get("TIMEOUT", 3.0))
    try:
        enc = _ws_call_auth(encoder_ip, "VideoMultiIpStream.Get", {}, timeout)
        wanted = ((enc or {}).get("result") or {}).get("streamname") if isinstance(enc, dict) else None
    except Exception as e:
        logging.info(f"[route_check] encoder read failed: {type(e).__name__}")
        wanted = None
    wanted = str(wanted or "").strip()
    if not wanted:
        return {"status": "unreachable"}
    with cache_lock:
        is_switcher = (cache_units.get(decoder_ip, {}).get("type") or "").lower() == "switcher"
    try:
        if is_switcher:
            held = _ws_get_selected_stream_switcher(decoder_ip, timeout)
        else:
            resp = _ws_call_auth(decoder_ip, "SelectVideoStream.Get",
                                 {"password": CONFIG.get("password", "password")}, timeout)
            held = ((resp or {}).get("result") or {}).get("streamname") if isinstance(resp, dict) else None
    except Exception as e:
        logging.info(f"[route_check] display read failed: {type(e).__name__}")
        held = None
    if not isinstance(held, str):
        return {"status": "unreachable"}            # it did not answer; an empty name is an answer
    held = held.strip()
    with cache_lock:
        unit = cache_units.get(decoder_ip)
        if isinstance(unit, dict):
            dec = unit["dec"] if isinstance(unit.get("dec"), dict) else {}
            unit["dec"] = dec
            video = dec["video"] if isinstance(dec.get("video"), dict) else {}
            dec["video"] = video
            if held:
                video["streamname"] = held
            if held == wanted:
                unit["routed_encoder_ip"] = encoder_ip
                unit["routed_streamname"] = wanted
    if held == wanted:
        _persist_units_cache()
        _observe_cache.invalidate("matrix")
        return {"status": "verified"}
    return {"status": "not_verified"}


def _route_mode_for_users(decoder_ip, mode, encoder_ip):
    """Video-only or audio-only for a matrix User, through the administrator's engine.

    This is `_route_breakaway` unchanged -- the same plan, the same pin of the other
    stream, the same read-back and rollback -- so a User's matrix has no routing
    logic of its own. A switcher has no audio path and is refused, as it is for an
    administrator. Breakaway is two of these (video from one source, audio from
    another), never a third operation.
    """
    try:
        with cache_lock:
            is_switcher = (cache_units.get(decoder_ip, {}).get("type") or "").lower() == "switcher"
        if is_switcher or mode not in (luma_breakaway.MODE_VIDEO, luma_breakaway.MODE_AUDIO):
            return {"ok": False, "status": "refused", "reason": "not_supported"}
        return _route_breakaway(
            decoder_ip, mode,
            encoder_ip if mode == luma_breakaway.MODE_VIDEO else None,
            encoder_ip if mode == luma_breakaway.MODE_AUDIO else None)
    finally:
        _observe_cache.invalidate("matrix")


def _load_units_snapshot():
    """A deep copy of the cached units, so a reader never sees a poll mid-write."""
    with cache_lock:
        return copy.deepcopy(list(cache_units.values()))


def _destination_inventory():
    """Every address the application currently holds for a device."""
    with cache_lock:
        return {str(ip) for ip in cache_units}


def _remote_destination_check(rule, payload):
    """None, or the refusal, for a remote request naming a device it may not reach."""
    try:
        luma_destinations.check(rule, payload, _destination_inventory, _expand_targets)
    except luma_destinations.Refusal as refusal:
        logging.warning("[GATE] remote request to %s refused: %s", rule, refusal.reason)
        return refusal
    return None


def _identify_for_route(ip):
    """The hardware address the device at `ip` reports now, or '' if it will not say.

    The one read a restricted route makes of a destination before writing to it, so an
    address that has been re-leased to another display is refused instead of written to.
    It is the same read the scan identifies units by.
    """
    return _ws_get_mac(ip, float(CONFIG.get("TIMEOUT", 3.0))) or ""


REMOTE = luma_wiring.RemoteSystem(
    APP, data_dir=DATA_DIR, ui_dir=UI_DIR, default_port=PORT,
    load_cache=_load_units_snapshot, route_av=_route_av_for_users,
    observe=_observe_matrix_state, preview_fetch=_fetch_preview,
    destination_check=_remote_destination_check,
    walls=luma_walls.WallDirectory(_wall_directory, _vw_device_id),
    route_wall=_route_wall_for_users, route_mode=_route_mode_for_users,
    identify=_identify_for_route, verify_route=_verify_route_for_users,
    unroute=_unroute_for_users)


def start_remote_access():
    """Restore remote access if the administrator left it on. Never raises."""
    try:
        return REMOTE.restore()
    except Exception as exc:                       # the local application must start
        logging.warning("[REMOTE] restore failed: %s", type(exc).__name__)
        return {"restored": False, "reason": "error"}


if __name__ == "__main__":
    # The application's own listener is the local one, so it binds loopback by
    # default (it used to bind every interface, which with remote access in the
    # product would have meant a network-facing listener nobody had asked for).
    # LUMA_HOST can still name another address; a listener not bound to loopback
    # is tagged remote and gated like any other network listener.
    bind_host = (os.environ.get("LUMA_HOST") or "127.0.0.1").strip()
    selected_port = PORT
    while selected_port < PORT + 100:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind((bind_host, selected_port))
            break
        except OSError:
            selected_port += 1

    if selected_port != PORT:
        logging.warning(f"[startup] port {PORT} busy; using {selected_port}")

    logging.info(f"=== Luma API on http://127.0.0.1:{selected_port}/ ===")
    logging.info(f"[startup] UI_DIR={UI_DIR}")
    logging.info(f"[startup] FIRMWARE_DIR={FIRMWARE_DIR}")
    start_remote_access()
    start_firmware_checks()
    luma_remote.serve_tagged(bind_host, selected_port, APP, threaded=True).serve_forever()
