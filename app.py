#!/usr/bin/env python3
import re
import threading
import time
import requests
from flask import Flask, jsonify, request
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- CONFIGURATION ---
UPSTREAM = "https://horizon.policyboss.com:5443/quote/vehicle_info_loggedin"
SECRET_KEY = "SECRET-HZ07QRWY-JIBT-XRMQ-ZP95-J0RWP3DYRACW"
CLIENT_KEY = "CLIENT-CNTP6NYE-CU9N-DUZW-CSPI-SH1IS4DOVHB9"
SOURCE = "PB-BETA"
UPSTREAM_TIMEOUT = 30
CACHE_TTL = 3600  
MAX_CACHE = 4096

HEADERS = {
    "Content-Type": "application/json;charset=utf-8",
    "Accept": "application/json",
    "Origin": "https://www.policyboss.com",
    "Referer": "https://www.policyboss.com/car-insurance",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}

# --- IPRoyal HTTP PROXY CONFIGURATION (Fixed for Vercel) ---
PROXY_URL = "http://QAF011bD6k0KrTcs:sndOTtxLhyDnvHx9_country-in@geo.iproyal.com:12321"

PROXIES = {
    "http": PROXY_URL,
    "https": PROXY_URL,
}

REGEX = re.compile(
    r"^[A-Z]{2}\s?\d{1,2}\s?[A-Z]{1,3}\s?\d{4}$|^[A-Z]{2}\s?\d{1,2}\s?BH\s?\d{4}$",
    re.I,
)
REGEX_STRIP = re.compile(r"[\s-]+")

_local = threading.local()
_cache = {}
_cache_lock = threading.Lock()
_inflight = {}
_inflight_lock = threading.Lock()
_boot = time.time()

app = Flask(__name__)

def _session() -> requests.Session:
    s = getattr(_local, "sess", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        s.proxies.update(PROXIES)

        retries = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        s.mount("http://", adapter)
        s.mount("https://", adapter)

        _local.sess = s
    return s

def normalize_number(number: str) -> str:
    return REGEX_STRIP.sub("", number or "").upper()

def is_valid_number(number: str) -> bool:
    return bool(REGEX.match(number.strip()))

def _cache_get(key: str):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit["ts"] + CACHE_TTL > time.time():
            return hit["data"]
        if hit:
            _cache.pop(key, None)
    return None

def _cache_set(key: str, data):
    with _cache_lock:
        _cache[key] = {"ts": time.time(), "data": data}
        if len(_cache) > MAX_CACHE:
            now = time.time()
            for k in list(_cache):
                if _cache[k]["ts"] + CACHE_TTL < now:
                    _cache.pop(k, None)

def clean_upstream(raw: dict) -> dict:
    junk = {
        "Ip_Address", "Calling_Source", "Product_Id_Request", 
        "Ss_Id", "Channel", "Is_LM", "FastLaneId", "Match_Mode"
    }
    no_vahan = raw.get("0") == "N" or all(k.isdigit() for k in list(raw)[:6])
    out = {k: v for k, v in raw.items() if k not in junk and not k.isdigit()}
    out["found"] = not no_vahan and bool(out.get("Make_Name"))
    out["fastlane_response_obj"] = raw.get("FastlaneResponse_Obj")
    return out

def upstream_lookup(number: str, product_id: int):
    payload = {
        "secret_key": SECRET_KEY,
        "client_key": CLIENT_KEY,
        "RegistrationNumber": number,
        "product_id": product_id,
        "ss_id": 0,
        "source": SOURCE,
        "session_id": "",
    }
    resp = _session().post(UPSTREAM, json=payload, timeout=UPSTREAM_TIMEOUT)
    if resp.status_code == 403:
        raise RuntimeError("Upstream gate or captcha triggered.")
    if resp.status_code != 200:
        raise RuntimeError(f"Upstream HTTP Error {resp.status_code}")
    return resp.json()

def lookup(number: str, product_id: int = 1, use_cache: bool = True):
    norm = normalize_number(number)
    if not is_valid_number(norm):
        raise ValueError("Invalid Indian vehicle registration number format.")

    key = f"{norm}|{product_id}"
    if use_cache:
        hit = _cache_get(key)
        if hit is not None:
            return hit, True

    with _inflight_lock:
        holder = _inflight.get(key)
        if holder is None:
            holder = {
                "event": threading.Event(),
                "result": None,
                "owner": threading.get_ident(),
            }
            _inflight[key] = holder
            am_owner = True
        else:
            am_owner = False

    if not am_owner:
        holder["event"].wait(timeout=UPSTREAM_TIMEOUT + 5)
        res = holder["result"]
        if isinstance(res, Exception):
            raise res
        return res, False

    t0 = time.time()
    try:
        raw = upstream_lookup(norm, product_id)
        result = clean_upstream(raw)
    except Exception as exc:
        holder["result"] = exc
        raise
    finally:
        holder["event"].set()
        with _inflight_lock:
            _inflight.pop(key, None)

    holder["result"] = result
    result["lookup_ms"] = int((time.time() - t0) * 1000)
    if use_cache:
        _cache_set(key, result)
    return result, False

@app.route("/")
def index():
    return jsonify({
        "service": "vehicle-lookup-api",
        "status": "online",
        "endpoints": {
            "/api/vehicle": "GET|POST ?number=UP32AB4567&product_id=1|10|12",
            "/health": "Service uptime and cache statistics",
        },
    })

@app.route("/api/vehicle", methods=["GET", "POST"])
def vehicle_info():
    number = request.args.get("number") or (
        (request.get_json(silent=True) or {}).get("number")
        if request.method == "POST"
        else None
    )

    try:
        product_id = int(request.args.get("product_id", 1))
    except ValueError:
        product_id = 1
    product_id = max(1, min(product_id, 12))

    use_cache = request.args.get("cache", "yes").lower() != "no"

    if not number:
        return jsonify({"status": "Error", "message": "Missing 'number' parameter"}), 400

    norm = normalize_number(number)
    if not is_valid_number(norm):
        return jsonify({
            "status": "Error",
            "registration_number": norm,
            "message": "Invalid Indian vehicle registration number format.",
        }), 400

    try:
        result, cached = lookup(norm, product_id, use_cache)
    except ValueError as exc:
        return jsonify({
            "status": "Error",
            "registration_number": norm,
            "message": str(exc),
        }), 400
    except Exception as exc:
        return jsonify({
            "status": "Error",
            "registration_number": norm,
            "message": f"Upstream lookup error: {str(exc)}",
        }), 502

    return jsonify({
        "status": "Success",
        "registration_number": norm,
        "cached": cached,
        "data": result,
    })

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "cache_entries": len(_cache),
        "uptime_s": int(time.time() - _boot),
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7890, threaded=True)
