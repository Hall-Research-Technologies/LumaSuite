
from __future__ import annotations
# --- CSV Export Endpoint for Device Manager ---
from io import StringIO
import csv
from datetime import datetime as dt
import os, sys, json, time, re, threading, socket, ipaddress, platform, subprocess, logging, glob, io, uuid
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
from flask import Flask, jsonify, request, send_from_directory, abort, Response
import hashlib
import requests
from websocket import create_connection
from concurrent.futures import ThreadPoolExecutor, as_completed

# Passwords are restricted to alphanumeric plus a fixed special-character set.
ALLOWED_PASSWORD_RE = re.compile(r"^[A-Za-z0-9!@#$%^&*]+$")


def _is_allowed_password(value: str) -> bool:
    return isinstance(value, str) and len(value) >= 6 and bool(ALLOWED_PASSWORD_RE.fullmatch(value))

# --- Application Version ---
__version__ = "1.0.2"

# --- CSV Export Endpoint for Device Manager ---

# --------- MATRIX ROUTING ENDPOINT ---------
APP = Flask(__name__)
app = APP  # lowercase alias for external importers

# --- CSV Export Endpoint for Device Manager ---
# (Moved after blueprint registration to avoid route override)


# --------- MATRIX ROUTING ENDPOINT ---------
APP = Flask(__name__)
app = APP  # lowercase alias for external importers

# --- Matrix state refresh endpoint ---
@APP.post("/api/refresh_matrix_state")
def api_refresh_matrix_state():
    """Poll all known encoders and decoders for their real state and update the cache."""
    try:
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
            try:
                if 'decoder' in role:
                    streamname = None
                    # Check if this is a Switcher device
                    if device_type == 'switcher':
                        streamname = _ws_get_selected_stream_switcher(ip, poll_timeout)
                    else:
                        # Standard decoder API
                        resp = _ws_call_auth(ip, "SelectVideoStream.Get", {"password": CONFIG.get("password", "password")}, poll_timeout)
                        if resp and "result" in resp and isinstance(resp["result"], dict):
                            streamname = resp["result"].get("streamname")
                    
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
                    aresp = _ws_call_auth(ip, "AudioMultiIpStream.Get", {}, poll_timeout)
                    if aresp and "result" in aresp and isinstance(aresp["result"], dict):
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
        return jsonify({"ok": True, "updated": updated, "units": list(cache_units.values())})
    except Exception as e:
        logging.error(f"[refresh_matrix_state] Exception: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@APP.post("/api/route_matrix")
def api_route_matrix():
    data = request.get_json(silent=True) or {}
    decoder_ip = data.get("decoder_ip")
    encoder_ip = data.get("encoder_ip")
    
    # Validate IP addresses
    try:
        ipaddress.ip_address(decoder_ip)
        ipaddress.ip_address(encoder_ip)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Invalid IP address format"}), 400
    
    if not decoder_ip or not encoder_ip:
        return jsonify({"ok": False, "error": "Missing decoder_ip or encoder_ip"}), 400
    try:
        # Use a longer timeout for routing; switchers may be slower than encoders
        ROUTE_TIMEOUT = float(CONFIG.get("TIMEOUT", 3.0))
        # 1. Get encoder's current video stream info (fallback to cache if needed)
        streamname = None
        try:
            enc_video = _ws_call_auth(encoder_ip, "VideoMultiIpStream.Get", {}, ROUTE_TIMEOUT)
            if enc_video and "result" in enc_video:
                video_result = enc_video["result"]
                streamname = video_result.get("streamname")
        except Exception as e:
            logging.warning(f"[route_matrix] VideoMultiIpStream.Get failed for {encoder_ip}: {e}")

        if not streamname:
            with cache_lock:
                enc_unit = cache_units.get(encoder_ip, {})
                streamname = ((enc_unit.get("enc") or {}).get("video") or {}).get("streamname")

        if not streamname:
            return jsonify({"ok": False, "error": "Encoder did not return streamname"}), 500
        
        # 2. Check if decoder is a Switcher
        dec_unit = cache_units.get(decoder_ip, {})
        is_decoder_switcher = (dec_unit.get("type") or "").lower() == "switcher"
        
        # Set decoder to use this streamname
        if is_decoder_switcher:
            # Use Switcher API (top-level params, no password)
            set_resp = _ws_set_selected_stream_switcher(decoder_ip, streamname, ROUTE_TIMEOUT)
            if not set_resp:
                return jsonify({"ok": False, "error": "Failed to set Switcher stream"}), 500
            # Get confirmation for Switcher
            get_resp = _ws_get_selected_stream_switcher(decoder_ip, ROUTE_TIMEOUT)
        else:
            # Use standard decoder API (with password)
            params = {"selecttype": 0, "streamname": streamname, "password": CONFIG.get("password", "password")}
            set_resp = _ws_call_auth(decoder_ip, "SelectVideoStream.Set", params, ROUTE_TIMEOUT)
            if not set_resp or "result" not in set_resp:
                return jsonify({"ok": False, "error": "Failed to set decoder stream"}), 500
            # Get decoder's current stream info for confirmation
            get_resp = _ws_call_auth(decoder_ip, "SelectVideoStream.Get", {"password": CONFIG.get("password", "password")}, ROUTE_TIMEOUT)

        # 3. Update in-memory cache and persist
        with cache_lock:
            dec_unit = cache_units.get(decoder_ip, {})
            # Store the new encoder_ip and streamname (customize as needed for your UI)
            dec_unit["routed_encoder_ip"] = encoder_ip
            dec_unit["routed_streamname"] = streamname
            # Also update dec.dec.video.streamname in cache for UI feedback
            if "dec" not in dec_unit or not isinstance(dec_unit["dec"], dict):
                dec_unit["dec"] = {}
            if "video" not in dec_unit["dec"] or not isinstance(dec_unit["dec"]["video"], dict):
                dec_unit["dec"]["video"] = {}
            dec_unit["dec"]["video"]["streamname"] = streamname
            cache_units[decoder_ip] = dec_unit
        _persist_units_cache()

        return jsonify({"ok": True, "set_result": {"streamname": set_resp} if isinstance(set_resp, str) else set_resp, "get_result": get_resp if isinstance(get_resp, dict) else {"streamname": get_resp}})
    except Exception as e:
        import traceback
        logging.error(f"[route_matrix] Exception: {e}\n{traceback.format_exc()}")
        return jsonify({"ok": False, "error": str(e)}), 500
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
        data = request.get_json(silent=True) or {}
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
        return jsonify({"ok": False, "error": str(e)}), 500

@APP.post("/api/set_hdcp")
def api_set_hdcp():
    try:
        data = request.get_json(silent=True) or {}
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
        return jsonify({"ok": False, "error": str(e)}), 500

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
        return Response(f"Export failed: {e}", status=500)
PORT = int(os.environ.get("LUMA_PORT","8089"))

IS_FROZEN = getattr(sys, "frozen", False)
# When frozen, __file__ lives in a temp _MEIPASS; keep assets there but persist state beside the executable.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.dirname(sys.executable) if IS_FROZEN else BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)

UI_DIR = os.path.join(BASE_DIR, "ui")
FIRMWARE_DIR = os.path.join(BASE_DIR, "firmware")
CACHE_FILE = os.path.join(DATA_DIR, "units_cache.json")
# in-memory cache structures (needed before autoload)
cache_units: Dict[str, Dict[str, Any]] = {}
cache_lock = threading.Lock()
# --- baseline cache autoload (dual JSON support) ---
if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Handle both array format and {"units": [...]} format
            if isinstance(data, list):
                cache_units.update({u["ip"]: u for u in data if isinstance(u, dict) and "ip" in u})
            elif isinstance(data, dict) and "units" in data:
                cache_units.update({u["ip"]: u for u in data["units"] if isinstance(u, dict) and "ip" in u})
            logging.info(f"[startup] loaded {len(cache_units)} units from {CACHE_FILE}")
    except Exception as e:
        logging.warning(f"[startup] failed to load cache: {e}")
# --- end baseline cache autoload ---


CONFIG: Dict[str, Any] = {
    "username": "admin",
    "password": "password",
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

# File to persist producer-config (asset paths etc.)
CONFIG_FILE = os.path.join(DATA_DIR, "producer_config.json")

def _load_persisted_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    for k in ("logos_path","imagestream_path","firmware_path"):
                        v = data.get(k)
                        if isinstance(v, str) and v.strip():
                            # store absolute path for consistency
                            CONFIG[k] = os.path.abspath(v)
                    # Optional persisted creds: use if present; else keep defaults
                    u = data.get("username")
                    p = data.get("password")
                    if isinstance(u, str) and u.strip():
                        CONFIG["username"] = u.strip()
                    if isinstance(p, str) and p.strip():
                        CONFIG["password"] = p.strip()
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
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(to_write, f, indent=2)
        logging.info(f"[config] persisted config -> {CONFIG_FILE}")
    except Exception as e:
        logging.warning(f"[config] failed to persist config: {e}")

# Attempt to load any persisted config on startup
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
    data = request.get_json(silent=True) or {}
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
    for p in CONFIG["ws_paths"]:
        url = _ws_url(ip, p)
        ws = None
        try:
            ws = create_connection(url, timeout=timeout)
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
                    logging.debug(f"[ws_call] Raw response (attempt {attempt+1}): {raw[:200]}")
                    
                    # Parse response
                    try:
                        data = json.loads(raw)
                    except Exception as e:
                        logging.warning(f"[ws_call] Invalid JSON from {ip}: {raw[:100]}")
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
                    break  # Timeout, try next path
            
            # Exhausted all read attempts on this path
            logging.warning(f"[ws_call] Exhausted {max_read_attempts} read attempts on {p}")
                
        except socket.timeout:
            logging.debug(f"[ws_call] Connection timeout on {p}")
            continue
        except ConnectionRefusedError:
            logging.debug(f"[ws_call] Connection refused on {p}")
            continue
        except Exception as e:
            logging.debug(f"[ws_call] Error on {p}: {type(e).__name__}: {e}")
            continue
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass
    
    logging.warning(f"[ws_call] Failed on all paths for {ip}: {CONFIG['ws_paths']}")
    return None
def _ws_call_auth(ip: str, method: str, params: Optional[Any] = None, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """
    Wrapper around _ws_call that optionally injects params.password from CONFIG.
    If CONFIG["ws_include_password"] is False, sends params as-is.
    Params can be dict, bool, or any JSON-serializable value.
    """
    # Use UUID for truly unique request IDs to prevent response mixing
    unique_id = f"{method.replace('.', '_')}_{uuid.uuid4().hex[:8]}"
    payload: Dict[str, Any] = {"jsonrpc":"2.0", "id": unique_id, "method": method}
    
    # Handle non-dict params (e.g., SystemBlinkLed.Set takes a boolean)
    if isinstance(params, dict):
        p = dict(params or {})
        if CONFIG.get("ws_include_password", True):
            # do not override if caller explicitly set a password
            p.setdefault("password", CONFIG.get("password","password"))
        if p:
            payload["params"] = p
    elif params is not None:
        # Non-dict params (boolean, string, number, etc.)
        payload["params"] = params
    
    return _ws_call(ip, payload, timeout or CONFIG.get("TIMEOUT", 3.0))


def _normalize_mac(s: str) -> str:
    if not isinstance(s, str): return ""
    hexes = re.findall(r"[0-9a-fA-F]{2}", s)
    if len(hexes) == 6:
        return ":".join(h.lower() for h in hexes)
    return s.strip().lower()

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
                hosts = list(net.hosts())
                if len(hosts) > 4096: hosts = hosts[:4096]
                ips.extend([str(h) for h in hosts])
            except Exception:
                pass
            continue
        m = re.match(r"^(\d{1,3}(?:\.\d{1,3}){3})-(\d{1,3})$", part)
        if m:
            base = m.group(1); end = int(m.group(2))
            octs = base.split("."); start = int(octs[-1]); prefix = ".".join(octs[:-1])
            lo,hi = sorted([start,end])
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

def _persist_units_cache():
    """Save cache to disk, preserving existing fields not currently in memory"""
    try:
        existing = {}
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # Handle both array format and {"units": [...]} format
                    if isinstance(data, list):
                        existing = {u.get("ip"): u for u in data if isinstance(u, dict) and "ip" in u}
                    elif isinstance(data, dict) and "units" in data:
                        existing = {u.get("ip"): u for u in data["units"] if isinstance(u, dict) and "ip" in u}
            except Exception as e:
                logging.warning(f"[persist] error reading existing cache: {e}")
                existing = {}
        
        with cache_lock:
            merged = {}
            # Start with current cache_units as source of truth
            for ip, unit in cache_units.items():
                merged[ip] = dict(unit)
                # Preserve additional fields from disk for existing units
                if ip in existing:
                    for key, value in existing[ip].items():
                        if key not in merged[ip]:
                            merged[ip][key] = value
            rows = list(merged.values())
        
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
        logging.info(f"[persist] wrote {len(rows)} units -> {CACHE_FILE}")
    except Exception as e:
        logging.warning(f"[persist] failed {e}")

def _merge_disk_into_memory():
    """Merge disk cache into memory without clearing existing data if load fails"""
    if not os.path.exists(CACHE_FILE):
        logging.debug("[merge] No cache file exists yet")
        return 0
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Handle both array format and {"units": [...]} format
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict) and "units" in data:
            rows = data["units"]
        else:
            logging.warning(f"[merge] unexpected format: {type(data)}")
            return 0
        
        updated = 0
        with cache_lock:
            # Don't clear - merge instead to preserve any updates
            for r in rows:
                if not isinstance(r, dict):
                    continue
                ip = r.get("ip")
                if not ip:
                    continue
                cache_units[ip] = r
                updated += 1
        logging.info(f"[merge] loaded {updated} units from disk into memory")
        return updated
    except Exception as e:
        logging.warning(f"[merge] failed {e} - preserving existing in-memory cache")
        return 0

# ---------------- static/UI routes ----------------

@APP.get("/")
def root():
    if os.path.exists(os.path.join(UI_DIR, "index.html")):
        logging.info("[root] serving /ui/index.html")
        return send_from_directory(UI_DIR, "index.html")
    return "UI not found. Put index.html in ./ui/"

@APP.get("/ui/<path:filename>")
def ui_files(filename):
    path = os.path.join(UI_DIR, filename)
    if os.path.isfile(path):
        return send_from_directory(UI_DIR, filename)
    abort(404)

# ---------------- API routes ----------------

@APP.get("/api/adapters")
def api_adapters():
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
    return jsonify({
        "files": rows,
        "searched": checked,
        "base_dir": _firmware_search_roots()[0] if _firmware_search_roots() else os.path.abspath(FIRMWARE_DIR)
    })

@APP.get("/api/cache")
def api_cache():
    _merge_disk_into_memory()
    
    with cache_lock:
        rows = list(cache_units.values())
    
    # Return cache immediately for fast page load
    resp = jsonify({"units": rows})
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
    
    # Start verification in background thread (non-blocking)
    import threading
    bg_thread = threading.Thread(target=verify_hostnames_async, daemon=True)
    bg_thread.start()
    
    return resp

@APP.get("/api/heartbeat")
def api_heartbeat():
    """Simple heartbeat endpoint for connection monitoring.
    Returns immediately with minimal overhead.
    Used by browser to detect if server is reachable."""
    return jsonify({"ok": True, "timestamp": time.time()})

@APP.post("/api/clear_units")
def api_clear_units():
    # Atomic: clear memory and file together under single lock
    with cache_lock:
        cache_units.clear()
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)
            logging.info("[clear_units] reset units_cache.json to empty list")
        except Exception as e:
            logging.warning(f"[clear_units] failed to reset file: {e}")
            return jsonify({"ok": False, "error": f"failed to persist: {e}"}), 500
    return jsonify({"ok": True})

@APP.post("/api/remove_units")
def api_remove_units():
    """Remove selected units from cache and all storage"""
    data = request.get_json(silent=True) or {}
    ips_to_remove = data.get("ips") or []
    if not ips_to_remove:
        return jsonify({"ok": False, "error": "ips required"}), 400
    
    try:
        with cache_lock:
            removed_count = 0
            for ip in ips_to_remove:
                if ip in cache_units:
                    del cache_units[ip]
                    removed_count += 1
        
        # Persist updated cache
        _persist_units_cache()
        
        logging.info(f"[remove_units] removed {removed_count}/{len(ips_to_remove)} units")
        return jsonify({"ok": True, "removed": removed_count})
    except Exception as e:
        logging.error(f"[remove_units] error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@APP.route("/api/config", methods=["GET","POST"])
def api_config():
    # GET: return a minimal view of current config
    if request.method == "GET":
        pw = str(CONFIG.get("password",""))
        pw_hash = hashlib.sha256(pw.encode("utf-8")).hexdigest() if pw else ""
        include_pw = request.args.get("include_password") in ("1","true","True")
        resp = {
            "username": CONFIG.get("username"),
            # do not return sensitive headers or extra headers here
            "logos_path": CONFIG.get("logos_path"),
            "imagestream_path": CONFIG.get("imagestream_path"),
            "firmware_path": CONFIG.get("firmware_path"),
            "password_hash": pw_hash,
            "is_default_password": pw == "password",
        }
        if include_pw:
            resp["password"] = pw
        return jsonify(resp)

    # POST: accept updates for ports, creds and asset paths
    data = request.get_json(silent=True) or {}
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
            CONFIG["password"] = candidate_password
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
            # If new value is not empty, use it; otherwise keep existing
            elif not _is_empty_value(new_val):
                merged[key] = new_val
            # else: new_val is empty, keep existing_val
    
    return merged

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
        logging.error(f"[scan_one] {ip} full error data: {base}")
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

    return base

@APP.post("/api/scan")
def api_scan():
    data = request.get_json(silent=True) or {}
    logging.info(f"===== [API_SCAN] STARTING - received targets: {data.get('targets')} =====")
    logging.debug(f"[api_scan] received data: {data}")
    targets = data.get("targets") or ""
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

    # Build MAC-to-IP index once for O(1) lookups (prevents O(n²) complexity)
    mac_to_ip_index = {}
    with cache_lock:
        for cached_ip, cached_unit in cache_units.items():
            cached_mac = (cached_unit.get("mac") or "").strip().lower()
            if cached_mac:
                mac_to_ip_index[cached_mac] = cached_ip

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
                # Determine if this IP already existed OR if another IP had same MAC (IP change)
                existed = ip in cache_units
                mac_new = (info.get("mac") or "").strip().lower()
                replaced_old_ip = None
                if mac_new:
                    # O(1) lookup using pre-built MAC index instead of O(n) iteration
                    old_ip = mac_to_ip_index.get(mac_new)
                    if old_ip and old_ip != ip:
                        # IP changed for this MAC — remove old entry, treat as update
                        del cache_units[old_ip]
                        existed = True
                        replaced_old_ip = old_ip
                
                # Use deep merge to preserve existing values while updating with new data
                if existed:
                    # Unit already exists: merge new data while preserving existing values
                    cache_units[ip] = _deep_merge_unit(cache_units[ip], info)
                    counts["updated"] += 1
                else:
                    # New unit: add it as-is
                    cache_units[ip] = info
                    counts["added"] += 1
                
                if replaced_old_ip:
                    logging.info(f"[scan-fast] mac {mac_new} moved {replaced_old_ip} -> {ip}")

    dur = round(time.time()-start, 2)
    _persist_units_cache()
    with cache_lock:
        all_units = list(cache_units.values())
    logging.info(f"[scan-fast] done {counts} errors={len(errors)} duration={dur}s total_mem={len(all_units)}")
    
    # Return errors along with units
    return jsonify({"ok": True, "duration": dur, "scanned": counts["scanned"], "units": all_units, "errors": errors})

# -------- misc endpoints --------
@APP.post("/api/tcp_probe")
def api_tcp_probe():
    data = request.get_json(silent=True) or {}
    ip = (data.get("ip") or "").strip()
    timeout_ms = int(data.get("timeout_ms") or TCP_TIMEOUT_MS)
    if not ip:
        return jsonify({"ok": False, "error": "missing ip"}), 400
    alive = _tcp_probe(ip, 80, timeout_ms) or _tcp_probe(ip, 443, timeout_ms)
    return jsonify({"ok": True, "reachable": bool(alive)})

@APP.post("/api/version", endpoint="luma_api_version")
def api_version():
    data = request.get_json(silent=True) or {}
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
    data = request.get_json(silent=True) or {}
    ip = (data.get("ip") or "").strip()
    if not ip:
        return jsonify({"ok": False, "error":"missing ip"}), 400
    if not (_tcp_probe(ip,80) or _tcp_probe(ip,443)):
        return jsonify({"ok": True, "alive": False, "hostname": ""})
    name = _ws_get_hostname(ip, CONFIG["TIMEOUT"])
    return jsonify({"ok": True, "alive": bool(name), "hostname": name})

@APP.post("/api/set_hostname", endpoint="luma_api_set_hostname")
def api_set_hostname():
    data = request.get_json(silent=True) or {}
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
        return jsonify({"ok": False, "error": str(e)}), 500

@APP.post("/api/get_streamname", endpoint="luma_api_get_streamname")
def api_get_streamname():
    data = request.get_json(silent=True) or {}
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
        return jsonify({"ok": False, "error": str(e)}), 500

@APP.post("/api/set_streamname", endpoint="luma_api_set_streamname")
def api_set_streamname():
    data = request.get_json(silent=True) or {}
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
        return jsonify({"ok": False, "error": str(e)}), 500

@APP.post("/api/reset", endpoint="luma_api_reset")
def api_reset():
    data = request.get_json(silent=True) or {}
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
    payload = {"username": CONFIG["username"], "password": CONFIG["password"]}
    try:
        r = session.post(url, json=payload, timeout=CONFIG["TIMEOUT"], verify=CONFIG["http_verify_tls"], headers=headers)
        logging.info(f"[login] {ip} -> {r.status_code}")
        return r.status_code in (200,204)
    except requests.RequestException as e:
        logging.warning(f"[login] {ip} exc {e}")
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
    data = request.get_json(silent=True) or {}
    logging.debug(f"[api_upgrade] received data: {data}")
    targets = data.get("targets") or []
    firmware = data.get("file") or data.get("firmware")

    if data.get("username"): CONFIG["username"] = data["username"]
    if data.get("password"): CONFIG["password"] = data["password"]

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
    data = request.get_json(silent=True) or {}
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
        return jsonify({"ok": True, "reachable": False, "error": str(e)})

@APP.get("/api/app_version", endpoint="luma_api_app_version")
def api_app_version():
    """Return the application version"""
    return jsonify({"ok": True, "version": __version__})

# --- [producer integration] BEGIN ---

import os, glob, json
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

@app.post("/api/producer/jsonrpc")
def api_producer_jsonrpc():
    """
    Proxy JSON-RPC to a unit to avoid CORS.
    Body: { "ip": "192.168.100.41", "method": "Rtmp.Set", "params": {...}, "timeout": 3.0, "id": "custom_id" }
    """
    try:
        body = request.get_json(force=True, silent=False) or {}
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

        # Build payload manually to preserve original request ID from frontend
        payload = {
            "jsonrpc": "2.0",
            "id": request_id if request_id else f"{method.replace('.', '_')}_{uuid.uuid4().hex[:8]}",
            "method": method
        }
        
        # Handle params - add password if needed
        if isinstance(params, dict):
            # Dict params: merge with password
            merged_params = _producer_merge_password(params)
            logging.debug(f"[producer-proxy] Merged params: {list(merged_params.keys()) if merged_params else 'NONE'}")
            if merged_params:
                payload["params"] = merged_params
            else:
                logging.error(f"[producer-proxy] merged_params is empty/falsy! Original params: {params}")
        elif params:
            # Non-dict params (e.g., boolean, string) - pass through as-is
            payload["params"] = params
            logging.debug(f"[producer-proxy] Non-dict params: {type(params).__name__}")
        else:
            # Empty/None params: add just password for device auth
            payload["params"] = {"password": CONFIG.get("password", "password")}
            logging.debug(f"[producer-proxy] Added password to empty params")

        # Log the proxy attempt (don't log passwords)
        try:
            param_keys = list(payload.get("params", {}).keys()) if isinstance(payload.get("params"), dict) else [type(payload.get("params")).__name__]
        except Exception:
            param_keys = []
        logging.info(f"[producer-proxy] Calling {method} on {ip} with params keys: {param_keys}")
        
        # Verify password is present
        if isinstance(payload.get("params"), dict) and "password" not in payload.get("params", {}):
            logging.error(f"[producer-proxy] WARNING: No password in params for {method}! Params: {payload.get('params')}")
        
        logging.debug(f"[producer-proxy] Full payload (sanitized): {{jsonrpc:{payload.get('jsonrpc')}, method:{payload.get('method')}, id:{payload.get('id')}, params_keys:{param_keys}}}")

        # Call device directly with preserved ID
        resp = _ws_call(ip, payload, timeout)
        if resp is None:
            logging.warning(f"[producer-proxy] No response from {ip} for {method}")
            return jsonify({"error": "device_no_response", "method": method})
        logging.debug(f"[producer-proxy] response={json.dumps(resp, default=str)[:300]}")
        return jsonify(resp)
    except Exception as e:
        logging.error(f"[producer-proxy] Exception: {e}")
        return jsonify({"error": str(e)}), 500

@app.post("/api/producer/change_password")
def api_producer_change_password():
    """Proxy legacy HTTP password change to a unit.

    Body: { "ip": "10.1.1.13", "username": "admin", "oldpassword": "password", "newpassword": "newpass", "timeout": 8.0 }
    Device success contract: HTTP 200 + JSON {"code": 0}
    """
    try:
        body = request.get_json(force=True, silent=False) or {}
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
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout, verify=bool(CONFIG.get("http_verify_tls", False)))

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
        return jsonify({"ok": False, "error": str(e)}), 500

@APP.post("/api/producer/test_device")
def test_device():
    """
    Diagnostic endpoint to test device connectivity.
    Body: { "ip": "192.168.100.41", "timeout": 3.0 }
    """
    try:
        body = request.get_json(force=True, silent=False) or {}
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
                ws = create_connection(url, timeout=timeout)
                ws.settimeout(timeout)
                
                # Try simple ping
                ping_payload = {"jsonrpc": "2.0", "id": "test_ping", "method": "System.Ping"}
                ws.send(json.dumps(ping_payload))
                raw = ws.recv()
                
                try:
                    data = json.loads(raw)
                    ws_results[p] = {"status": "ok", "response": data}
                    logging.debug(f"[test_device] WebSocket {p} successful: {data}")
                except Exception as je:
                    ws_results[p] = {"status": "invalid_json", "raw": raw[:100]}
                    logging.warning(f"[test_device] Invalid JSON from {p}: {raw[:200]}")
                    
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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"ok": False, "error": str(e)}), 500

# ====== END PRODUCER BLOCK ======
# --- [producer persistent state] BEGIN ---
# Persistent ticker + stream data across all units
PRODUCER_STATE_FILE = os.path.join(DATA_DIR, "producer_state.json")

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
        with open(PRODUCER_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
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
    data = request.get_json(silent=True) or {}
    _save_producer_state(data)
    return jsonify({"ok": True})
    
@app.route("/api/producer2/state", methods=["GET","POST"])
def api_producer2_state():
    state_file = os.path.join(DATA_DIR, "producer2_state.json")
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return jsonify({"ok": True})
    if os.path.exists(state_file):
        with open(state_file, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    return jsonify({"streams": [], "ticker": {}})

# --- [producer persistent state] END ---

if __name__ == "__main__":
    logging.info(f"=== Luma API on http://127.0.0.1:{PORT}/ ===")
    logging.info(f"[startup] UI_DIR={UI_DIR}")
    logging.info(f"[startup] FIRMWARE_DIR={FIRMWARE_DIR}")
    APP.run(host="0.0.0.0", port=PORT)
