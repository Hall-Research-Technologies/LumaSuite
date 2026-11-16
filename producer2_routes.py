from flask import Blueprint, jsonify, request, current_app, send_from_directory

import luma_request
import os, re, json, time, base64
import requests

producer2_bp = Blueprint("producer2", __name__, url_prefix="/api/producer2")

# ---------- Paths (dynamic: consult app config if available) ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR   = os.path.join(BASE_DIR, "ui")

def _ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass

def _resolve_asset_dir(kind: str):
    """Return (filesystem_dir, web_prefix) for asset kind ('imagestream'|'logos').
    Prefers application config `LUMA_CONFIG` if present and the path exists; otherwise
    falls back to bundled `ui/<kind>` directory.
    """
    cfg = current_app.config.get('LUMA_CONFIG', {}) if current_app else {}
    key = 'imagestream_path' if kind == 'images' or kind == 'imagestream' else 'logos_path'
    candidate = cfg.get(key) if isinstance(cfg, dict) else None
    if isinstance(candidate, str) and os.path.isdir(os.path.abspath(candidate)):
        return os.path.abspath(candidate), f"/api/producer2/asset/{'imagestream' if kind.startswith('imag') else 'logos'}"
    # fallback into UI tree
    d = os.path.join(UI_DIR, 'imagestream' if kind.startswith('imag') else 'logos')
    _ensure_dir(d)
    return os.path.abspath(d), f"/ui/{'imagestream' if kind.startswith('imag') else 'logos'}"

SLOT_PAT = re.compile(r"^(.+)\.(jpg|jpeg|png)$", re.IGNORECASE)

# ---------- Helpers ----------
def _mint_bearer(username="admin", password=None, trim_padding=False):
    cfg = current_app.config.get('LUMA_CONFIG', {}) if current_app else {}
    pwd = password if password is not None else cfg.get('password', '')
    p1 = json.dumps({"create_time": str(int(time.time())), "role": 0}).encode("utf-8")
    p2 = json.dumps({"password": pwd, "username": username}).encode("utf-8")
    b1 = base64.b64encode(p1).decode("ascii")
    b2 = base64.b64encode(p2).decode("ascii")
    if trim_padding:
        b1 = b1.rstrip("=")
        b2 = b2.rstrip("=")
    return f"{b1}.{b2}"

def _scan_dir(dpath, web_prefix):
    found = {}
    try:
        files = list(os.listdir(dpath))
        current_app.logger.info(f"[_scan_dir] scanning {dpath}, found {len(files)} entries")
        for name in files:
            full = os.path.join(dpath, name)
            if not os.path.isfile(full):
                current_app.logger.debug(f"[_scan_dir] skip non-file: {name}")
                continue
            m = SLOT_PAT.match(name)
            if not m:
                current_app.logger.debug(f"[_scan_dir] skip non-matching: {name}")
                continue
            slot_name, ext = m.group(1), m.group(2).lower()
            ext = "jpg" if ext == "jpeg" else ext
            # Use filename as slot key (supports both numbered like "1" and named like "mazie")
            found.setdefault(slot_name, {})
            found[slot_name][ext] = f"{web_prefix}/{name}"
            current_app.logger.info(f"[_scan_dir] matched: {name} -> slot {slot_name}, ext {ext}")
    except Exception as e:
        current_app.logger.error(f"[_scan_dir] error scanning {dpath}: {e}")
    return found

def _scan_assets():
    img_dir, img_prefix = _resolve_asset_dir('imagestream')
    logo_dir, logo_prefix = _resolve_asset_dir('logos')
    return {
        "images": _scan_dir(img_dir, img_prefix),
        "logos":  _scan_dir(logo_dir, logo_prefix),
    }

def _choose_slot_file(kind, slot, ext_pref=None):
    assets = _scan_assets()[kind]
    ent = assets.get(str(slot))
    if not ent:
        return None, None, None
    if ext_pref and ext_pref in ent:
        chosen = ext_pref
    else:
        chosen = "jpg" if "jpg" in ent else ("png" if "png" in ent else None)
    if not chosen:
        return None, None, None
    web_url = ent[chosen]
    # Derive filesystem path from web_url:
    if web_url.startswith("/ui/"):
        # local UI tree
        rel = web_url[len("/ui/"):].lstrip("/")
        abs_path = os.path.join(UI_DIR, rel.replace("/", os.sep))
    elif web_url.startswith("/api/producer2/asset/"):
        # format: /api/producer2/asset/<kind>/<filename>
        parts = web_url.split("/")
        try:
            kind_part = parts[4]
            filename = "/".join(parts[5:])
            cfg = current_app.config.get('LUMA_CONFIG', {}) if current_app else {}
            key = 'imagestream_path' if kind_part == 'imagestream' else ('logos_path' if kind_part == 'logos' else None)
            base_dir = cfg.get(key) if isinstance(cfg, dict) else None
            if base_dir:
                abs_path = os.path.join(os.path.abspath(base_dir), filename.replace("/", os.sep))
            else:
                abs_path = None
        except Exception:
            abs_path = None
    else:
        abs_path = None

    return abs_path, web_url, chosen

def _forward_file(ip, field_name, file_tuple, url_path, username="admin", password=None, timeout=15, trim_padding=False):
    headers = {"Authorization": f"Bearer {_mint_bearer(username, password, trim_padding=trim_padding)}"}
    files = { field_name: file_tuple }  # ("filename.ext", bytes, "image/png")
    url = f"http://{ip}{url_path}"
    return requests.post(url, files=files, headers=headers, timeout=timeout, allow_redirects=False)

# ---------- Routes ----------
@producer2_bp.get("/assets")
def assets():
    return jsonify(_scan_assets())

#: The logo and image folders hold pictures. Whatever else is in a folder somebody chose --
#: a configuration file, an export -- is not theirs to be handed over.
_ASSET_IMAGE_TYPES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"})


@producer2_bp.get("/asset/<kind>/<path:filename>")
def asset_file(kind, filename):
    kind = (kind or "").lower()
    if kind not in ("imagestream", "logos"):
        return jsonify({"ok": False, "error": "invalid_kind"}), 400
    # Resolve directory using configured paths when present
    dir_path, _ = _resolve_asset_dir(kind)
    if not dir_path or not os.path.isdir(dir_path):
        return jsonify({"ok": False, "error": "missing_dir"}), 404
    if os.path.splitext(filename)[1].lower() not in _ASSET_IMAGE_TYPES:
        return jsonify({"ok": False, "error": "not_found"}), 404
    try:
        return send_from_directory(dir_path, filename, as_attachment=False)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "not_found"}), 404

@producer2_bp.post("/push/image")
def push_image():
    try:
        data = luma_request.json_object() or request.form
        ip = (data.get("ip") or "").strip()
        slot = str(data.get("slot") or "").strip()
        ext  = (data.get("ext") or "").strip().lower() or None
        username = (data.get("username") or "admin").strip()
        cfg = current_app.config.get('LUMA_CONFIG', {}) if current_app else {}
        password = (data.get("password") or cfg.get('password','')).strip()
        if not ip or not slot:
            return jsonify({"ok": False, "error": "missing ip or slot"}), 400

        abs_path, web_url, chosen_ext = _choose_slot_file("images", slot, ext)
        if not abs_path:
            return jsonify({"ok": False, "error": f"no image found for slot {slot}"}), 404

        with open(abs_path, "rb") as f:
            blob = f.read()
        mt = "image/jpeg" if chosen_ext == "jpg" else "image/png"

        # Try with padding first; some firmwares accept only padded form
        try:
            r = _forward_file(ip, "preImage", (os.path.basename(abs_path), blob, mt), "/upload/priv",
                              username=username, password=password, trim_padding=False)
        except Exception as e:
            current_app.logger.exception("push/image request error (padded)")
            return jsonify({"ok": False, "error": "The unit did not answer (" + type(e).__name__ + ")."}), 502

        if not (200 <= r.status_code < 300):
            # Retry once without padding (seen in some captures)
            try:
                r2 = _forward_file(ip, "preImage", (os.path.basename(abs_path), blob, mt), "/upload/priv",
                                   username=username, password=password, trim_padding=True)
                ok2 = (200 <= r2.status_code < 300)
                return jsonify({
                    "ok": ok2, "status": r2.status_code,
                    "pushed": {"slot": slot, "url": web_url, "ext": chosen_ext},
                    "note": "first try (padded) failed, retry padless executed",
                    "first_status": r.status_code, "first_text": r.text[:300]
                }), (200 if ok2 else 502)
            except Exception as e2:
                current_app.logger.exception("push/image retry error (padless)")
                return jsonify({"ok": False, "error": f"retry-error: {e2}", "first_status": r.status_code, "first_text": r.text[:300]}), 502

        return jsonify({"ok": True, "status": r.status_code, "pushed": {"slot": slot, "url": web_url, "ext": chosen_ext}})
    except Exception as e:
        current_app.logger.exception("push/image 500")
        return luma_request.internal_error(e)

@producer2_bp.post("/push/logo")
def push_logo():
    try:
        data = luma_request.json_object() or request.form
        ip = (data.get("ip") or "").strip()
        slot = str(data.get("slot") or "").strip()
        ext  = (data.get("ext") or "").strip().lower() or None
        username = (data.get("username") or "admin").strip()
        cfg = current_app.config.get('LUMA_CONFIG', {}) if current_app else {}
        password = (data.get("password") or cfg.get('password','')).strip()
        if not ip or not slot:
            return jsonify({"ok": False, "error": "missing ip or slot"}), 400

        abs_path, web_url, chosen_ext = _choose_slot_file("logos", slot, ext)
        if not abs_path:
            return jsonify({"ok": False, "error": f"no logo found for slot {slot}"}), 404

        with open(abs_path, "rb") as f:
            blob = f.read()
        mt = "image/jpeg" if chosen_ext == "jpg" else "image/png"

        try:
            r = _forward_file(ip, "logoImage", (os.path.basename(abs_path), blob, mt), "/upload/logo",
                              username=username, password=password, trim_padding=False)
        except Exception as e:
            current_app.logger.exception("push/logo request error (padded)")
            return jsonify({"ok": False, "error": "The unit did not answer (" + type(e).__name__ + ")."}), 502

        if not (200 <= r.status_code < 300):
            try:
                r2 = _forward_file(ip, "logoImage", (os.path.basename(abs_path), blob, mt), "/upload/logo",
                                   username=username, password=password, trim_padding=True)
                ok2 = (200 <= r2.status_code < 300)
                return jsonify({
                    "ok": ok2, "status": r2.status_code,
                    "pushed": {"slot": slot, "url": web_url, "ext": chosen_ext},
                    "note": "first try (padded) failed, retry padless executed",
                    "first_status": r.status_code, "first_text": r.text[:300]
                }), (200 if ok2 else 502)
            except Exception as e2:
                current_app.logger.exception("push/logo retry error (padless)")
                return jsonify({"ok": False, "error": f"retry-error: {e2}", "first_status": r.status_code, "first_text": r.text[:300]}), 502

        return jsonify({"ok": True, "status": r.status_code, "pushed": {"slot": slot, "url": web_url, "ext": chosen_ext}})
    except Exception as e:
        current_app.logger.exception("push/logo 500")
        return luma_request.internal_error(e)
