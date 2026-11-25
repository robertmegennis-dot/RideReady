#!/usr/bin/env python3
"""
RideReady Sync — local bridge between your Strava + Whoop accounts and the
RideReady dashboard (rideready.html).

How it works:
  1. Run this script (start-windows.bat / start-mac.command, or `python3 sync.py`)
  2. Your browser opens http://localhost:8765 — the RideReady dashboard with a
     sync banner at the top. Click "Connect Strava" / "Connect Whoop" and log in
     (one time only; tokens are saved in tokens.json next to this script).
  3. Every 15 minutes it pulls new rides from Strava and sleep/recovery/strain
     from Whoop and serves them into the dashboard automatically.

Requires: Python 3.9+ and a config.json (see SETUP.md). No third-party packages.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
APP_HTML = BASE / 'rideready.html'
CONFIG_PATH = BASE / 'config.json'
# Cloud deploys (Railway) set RR_DATA_DIR to a mounted volume so data survives restarts;
# locally everything lives next to the script.
DATA_DIR = Path(os.environ.get('RR_DATA_DIR') or BASE)
TOKENS_PATH = DATA_DIR / 'tokens.json'
DATA_PATH = DATA_DIR / 'data.json'
PROFILE_PATH = DATA_DIR / 'profile.json'
DEBUG_PATH = DATA_DIR / 'debug-last-sync.json'

STRAVA_AUTH = 'https://www.strava.com/oauth/authorize'
STRAVA_TOKEN = 'https://www.strava.com/oauth/token'
STRAVA_API = 'https://www.strava.com/api/v3'
WHOOP_AUTH = 'https://api.prod.whoop.com/oauth/oauth2/auth'
WHOOP_TOKEN = 'https://api.prod.whoop.com/oauth/oauth2/token'
WHOOP_API = 'https://api.prod.whoop.com/developer/v2'
WHOOP_SCOPES = 'offline read:sleep read:recovery read:cycles read:workout'
STRAVA_SCOPES = 'activity:read_all'
RIDE_SPORTS = {'Ride', 'VirtualRide', 'GravelRide', 'MountainBikeRide', 'VirtualMountainBikeRide'}
SYNC_DAYS = 14          # how far back to sync
POLL_SECONDS = 15 * 60
SYNC_VERSION = '2.1'    # 2.1 = cloud-ready: env-var config, RR_DATA_DIR volume, PUBLIC_URL redirects, PORT binding

class SyncError(Exception):
    pass

# ---------------------------------------------------------------- utilities
def now():
    return datetime.now(timezone.utc)

def iso_utc(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def parse_iso(ts):
    """Whoop/Strava timestamps like '2026-11-24T11:00:00.000Z' -> aware datetime."""
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace('Z', '+00:00'))

def local_date(ts):
    """UTC ISO timestamp -> your local 'YYYY-MM-DD' key."""
    dt = parse_iso(ts)
    if dt is None:
        return None
    return dt.astimezone().strftime('%Y-%m-%d')

def parse_hms(s):
    """'7h 58m 18s' -> hours (float), or None."""
    if not isinstance(s, str):
        return None
    m = re.match(r'\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?\s*$', s)
    if not m:
        return None
    h, mi, sec = (int(g) if g else 0 for g in m.groups())
    return round(h + mi / 60 + sec / 3600, 2)

def http_json(url, method='GET', data=None, form=False, headers=None, timeout=30):
    """Tiny HTTP helper. data: dict -> JSON body, or form-encoded when form=True."""
    hdr = {'User-Agent': UA, **(headers or {})}
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            hdr['Content-Type'] = 'application/x-www-form-urlencoded'
        else:
            body = json.dumps(data).encode()
            hdr['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=body, method=method, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode()[:300]
        except Exception:
            pass
        raise SyncError(f'{method} {url} -> HTTP {e.code}: {detail}') from e

def hget(url, token, **params):
    """Authenticated GET with Bearer token, params, optional pagination."""
    q = {k: v for k, v in params.items() if v is not None}
    url = f'{url}?{urllib.parse.urlencode(q)}' if q else url
    return http_json(url, headers={'Authorization': f'Bearer {token}'})

def paginate(url, token, **params):
    """Follow Whoop's pagination; returns list of records."""
    out, nxt, guard = [], params.pop('nextToken', None), 0
    while guard < 40:
        p = dict(params)
        if nxt:
            p['nextToken'] = nxt
        resp = hget(url, token, **p)
        out.extend(resp.get('records') or [])
        nxt = resp.get('nextToken') or resp.get('next_token')  # v2 uses snake_case
        if not nxt:
            break
        guard += 1
    return out

# ---------------------------------------------------------------- state
_lock = threading.Lock()

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default

CFG = {**{
    'strava_client_id': '', 'strava_client_secret': '',
    'whoop_client_id': '', 'whoop_client_secret': '',
    'port': 8765, 'open_browser': True,
    # v2: cloud/profile/vision settings
    'password': '',                    # set when deployed publicly; empty = no login (local mode)
    'nous_api_key': '',                # portal.nousresearch.com API key, for photo scanning
    'vision_model': 'Qwen/Qwen2.5-VL-72B-Instruct',
}, **load_json(CONFIG_PATH, {})}

STATE = os.urandom(8).hex()   # CSRF state — Whoop requires ≥8 chars
UA = 'Mozilla/5.0 (X11; Linux x86_64) RideReadySync/1.0'

def _env_cfg(cfg):
    """Overlay environment variables (Railway/cloud deploys) on top of config.json.
    Template placeholders (PUT_YOUR_/PASTE_YOUR_) are treated as empty."""
    env_map = {
        'STRAVA_CLIENT_ID': 'strava_client_id', 'STRAVA_CLIENT_SECRET': 'strava_client_secret',
        'WHOOP_CLIENT_ID': 'whoop_client_id', 'WHOOP_CLIENT_SECRET': 'whoop_client_secret',
        'NOUS_API_KEY': 'nous_api_key', 'RR_PASSWORD': 'password',
        'PUBLIC_URL': 'public_url', 'VISION_MODEL': 'vision_model',
    }
    out = dict(cfg)
    for env, key in env_map.items():
        if os.environ.get(env):
            out[key] = os.environ[env]
    for key in ('strava_client_id', 'strava_client_secret', 'whoop_client_id', 'whoop_client_secret',
                'nous_api_key', 'password', 'public_url'):
        v = str(out.get(key) or '')
        if 'PUT_YOUR' in v or 'PASTE_YOUR' in v or 'PASTE_...' in v:
            out[key] = ''
    return out

CFG = _env_cfg(CFG)

TOKENS = load_json(TOKENS_PATH, {'strava': None, 'whoop': None})
DATA = load_json(DATA_PATH, {'updated': None, 'days': {}})
LAST_ERRORS = {'strava': None, 'whoop': None}
CALLBACKS = {'strava': None, 'whoop': None}

def _cb(service, msg):
    """Record a login-attempt event (shown on /diag and in the sync window)."""
    CALLBACKS[service] = f'{time.strftime("%H:%M:%S")} — {msg}'
    print(f'[connect] {service}: {msg}')

def save_tokens():
    with _lock:
        TOKENS_PATH.write_text(json.dumps(TOKENS, indent=2))

def save_data(days):
    # keep only recent days
    cutoff = (now() - timedelta(days=SYNC_DAYS + 7)).strftime('%Y-%m-%d')
    days = {k: v for k, v in days.items() if k >= cutoff}
    with _lock:
        DATA['days'] = days
        DATA['updated'] = iso_utc(now())
        DATA_PATH.write_text(json.dumps(DATA, indent=2))

def save_debug(sample):
    try:
        DEBUG_PATH.write_text(json.dumps(sample, indent=2)[:20000])
    except Exception:
        pass

# ---------------------------------------------------------------- strava
def strava_refresh():
    """Ensure a fresh Strava access token (refresh tokens rotate — always persist)."""
    t = TOKENS.get('strava')
    if not t:
        return None
    if t.get('expires_at', 0) > time.time() + 60:
        return t['access_token']
    resp = http_json(STRAVA_TOKEN, method='POST', form=True, data={
        'client_id': CFG['strava_client_id'],
        'client_secret': CFG['strava_client_secret'],
        'grant_type': 'refresh_token',
        'refresh_token': t['refresh_token'],
    })
    TOKENS['strava'] = {
        'access_token': resp['access_token'],
        'refresh_token': resp['refresh_token'],   # rotated!
        'expires_at': resp['expires_at'],
    }
    save_tokens()
    return TOKENS['strava']['access_token']

def strava_exchange(code):
    resp = http_json(STRAVA_TOKEN, method='POST', form=True, data={
        'client_id': CFG['strava_client_id'],
        'client_secret': CFG['strava_client_secret'],
        'code': code, 'grant_type': 'authorization_code',
    })
    TOKENS['strava'] = {'access_token': resp['access_token'],
                        'refresh_token': resp['refresh_token'],
                        'expires_at': resp['expires_at']}
    save_tokens()

def sync_strava(days, debug):
    if not TOKENS.get('strava'):
        return
    tok = strava_refresh()
    after = int((now() - timedelta(days=SYNC_DAYS)).timestamp())
    acts = hget(f'{STRAVA_API}/athlete/activities', tok, after=after, per_page=100)
    debug['strava_activity_sample'] = acts[:1]
    for a in acts:
        sport = a.get('sport_type') or a.get('type')
        if sport not in RIDE_SPORTS:
            continue
        k = (a.get('start_date_local') or a.get('start_date') or '')[:10]
        if not k:
            continue
        ride = {'minutes': (a.get('moving_time') or a.get('elapsed_time') or 0) // 60,
                'avgPower': None, 'notes': a.get('name') or ''}
        if ride['minutes'] <= 0:
            continue
        prev = days.get(k, {}).get('ride')
        try:  # detailed activity for average watts (only when a power meter recorded)
            det = hget(f"{STRAVA_API}/activities/{a['id']}", tok)
            if det.get('device_watts'):
                ride['avgPower'] = round(det.get('average_watts') or 0)
                # rolling FTP input: compute power-curve MMPs once per ride
                if not (prev or {}).get('_mmp'):
                    mmp = ride_mmps(a['id'], tok)
                    if mmp:
                        ride['_mmp'] = mmp
        except SyncError:
            pass
        # keep whichever has more info; prefer newer sync
        if not prev or prev.get('_src') != 'strava' or (ride['minutes'] >= prev.get('minutes', 0)) or ('_mmp' in ride and '_mmp' not in prev):
            ride['_src'] = 'strava'
            if prev and '_mmp' in prev and '_mmp' not in ride:
                ride['_mmp'] = prev['_mmp']
            days.setdefault(k, {})['ride'] = ride

# ---------------------------------------------------------------- whoop
def whoop_refresh():
    t = TOKENS.get('whoop')
    if not t:
        return None
    if t.get('expires_at', 0) > time.time() + 60:
        return t['access_token']
    resp = http_json(WHOOP_TOKEN, method='POST', form=True, data={
        'grant_type': 'refresh_token',
        'refresh_token': t['refresh_token'],
        'client_id': CFG['whoop_client_id'],
        'client_secret': CFG['whoop_client_secret'],
    })
    TOKENS['whoop'] = {'access_token': resp['access_token'],
                       'refresh_token': resp.get('refresh_token', t['refresh_token']),
                       'expires_at': int(time.time()) + resp.get('expires_in', 3000)}
    save_tokens()
    return TOKENS['whoop']['access_token']

def whoop_exchange(code):
    # NOTE: Whoop's OAuth server only accepts form-encoded bodies — JSON gets
    # "The POST body can not be empty" (verified against the live endpoint).
    # Hydra also requires the SAME redirect_uri in the token exchange as in authorize.
    resp = http_json(WHOOP_TOKEN, method='POST', form=True, data={
        'grant_type': 'authorization_code',
        'code': code,
        'client_id': CFG['whoop_client_id'],
        'client_secret': CFG['whoop_client_secret'],
        'redirect_uri': redirect_uri('whoop'),
    })
    TOKENS['whoop'] = {'access_token': resp['access_token'],
                       'refresh_token': resp['refresh_token'],
                       'expires_at': int(time.time()) + resp.get('expires_in', 3000)}
    save_tokens()

def sleep_hours(sleep_item):
    """Best-effort sleep duration in hours from a Whoop sleep record."""
    score = sleep_item.get('score') or {}
    ss = score.get('stage_summary') or {}
    # v2 payloads ship *_milli fields (milliseconds); some builds use *_str or plain seconds
    for key in ('total_in_bed_time_milli', 'total_sleep_time_milli'):
        v = ss.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return round(v / 3_600_000, 2)
    for key in ('total_in_bed_time', 'total_sleep_time'):
        v = ss.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return round(v / 3600, 2)
        for cand in (v, ss.get(key + '_str'), ss.get(key.replace('_time', '_time_str'))):
            h = parse_hms(cand) if isinstance(cand, str) else None
            if h:
                return h
    for key in ('sleep_duration', 'duration'):
        v = score.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return round(v / 3600, 2)
    start, end = parse_iso(sleep_item.get('start')), parse_iso(sleep_item.get('end'))
    if start and end and end > start:
        return round((end - start).total_seconds() / 3600, 2)
    return None

def sync_whoop(days, debug):
    if not TOKENS.get('whoop'):
        return
    tok = whoop_refresh()
    start = iso_utc(now() - timedelta(days=SYNC_DAYS))
    end = iso_utc(now())

    sleeps = paginate(f'{WHOOP_API}/activity/sleep', tok, start=start, end=end, limit=25)
    debug['whoop_sleep_sample'] = sleeps[:1]
    for s in sleeps:
        k = local_date(s.get('end')) or local_date(s.get('start'))  # date you woke up
        if not k:
            continue
        hrs = sleep_hours(s)
        if hrs is not None:
            days.setdefault(k, {}).setdefault('whoop', {})['sleepHrs'] = hrs

    recs = paginate(f'{WHOOP_API}/recovery', tok, start=start, end=end, limit=25)
    debug['whoop_recovery_sample'] = recs[:1]
    for r in recs:
        # v2 recovery records have NO 'timestamp' — use created_at (morning of the sleep)
        k = local_date(r.get('created_at') or r.get('timestamp') or r.get('updated_at'))
        if not k:
            continue
        sc = r.get('score') or {}
        w = days.setdefault(k, {}).setdefault('whoop', {})
        w['recovery'] = sc.get('recovery_score')
        w['hrv'] = sc.get('hrv_rmssd_milli') if sc.get('hrv_rmssd_milli') is not None else sc.get('hrv_rmssd_ms')
        w['rhr'] = sc.get('resting_heart_rate')

    cycles = paginate(f'{WHOOP_API}/cycle', tok, start=start, end=end, limit=25)
    debug['whoop_cycle_sample'] = cycles[:1]
    for c in cycles:
        k = local_date(c.get('start'))
        if not k:
            continue
        strain = (c.get('score') or {}).get('strain')
        if strain is not None:
            w = days.setdefault(k, {}).setdefault('whoop', {})
            w['strain'] = max(strain, w.get('strain') or 0)

    for k in list(days):  # drop empty whoop dicts
        w = days.get(k, {}).get('whoop')
        if w is not None and not any(v is not None for v in w.values()):
            days[k].pop('whoop', None)

# ---------------------------------------------------------------- rolling FTP
MMP_WINDOWS = [(300, 'mmp300'), (480, 'mmp480'), (1200, 'mmp1200'), (3600, 'mmp3600')]

def max_mean_power(power, seconds):
    """Best rolling-average power (W) for a given window over a power stream."""
    n = len(power)
    w = min(seconds, n)
    if w <= 0:
        return None
    best = cur = sum(power[:w])
    for i in range(w, n):
        cur += power[i] - power[i - w]
        if cur > best:
            best = cur
    return round(best / w)

def ride_mmps(activity_id, tok):
    """Fetch the power stream for a ride and compute MMPs (5/8/20/60 min)."""
    try:
        st = hget(f'{STRAVA_API}/activities/{activity_id}/streams', tok,
                 keys='power', key_by_type='true')
        power = st.get('power') if isinstance(st, dict) else None
        if not power:
            return None
        out = {}
        for secs, key in MMP_WINDOWS:
            v = max_mean_power(power, secs)
            if v:
                out[key] = v
        return out or None
    except SyncError:
        return None

def rolling_ftp(days):
    """FTP estimate = 95% of your best 20-minute power in the last 90 days."""
    cutoff = (now() - timedelta(days=90)).strftime('%Y-%m-%d')
    best20 = 0
    for k, v in days.items():
        if k < cutoff:
            continue
        m = (v.get('ride') or {}).get('_mmp') or {}
        if m.get('mmp1200', 0) > best20:
            best20 = m['mmp1200']
    if not best20:
        return None
    return round(best20 * 0.95)


def strip_private(days):
    """Remove internal keys before serving to the dashboard."""
    out = {}
    for k, v in days.items():
        v2 = {}
        if v.get('ride'):
            v2['ride'] = {kk: vv for kk, vv in v['ride'].items() if not kk.startswith('_')}
        if v.get('whoop'):
            v2['whoop'] = {kk: vv for kk, vv in v['whoop'].items() if vv is not None}
        if v2:
            out[k] = v2
    return out

def do_sync():
    days = {**DATA.get('days', {})}
    debug = {'time': iso_utc(now())}
    for name, fn in (('strava', sync_strava), ('whoop', sync_whoop)):
        try:
            fn(days, debug)
            LAST_ERRORS[name] = None
        except Exception as e:
            LAST_ERRORS[name] = str(e)[:300]
            print(f'[sync] {name} error: {e}', file=sys.stderr)
    save_debug(debug)
    save_data(days)

def poll_loop():
    while True:
        do_sync()
        time.sleep(POLL_SECONDS)

# ---------------------------------------------------------------- server
def redirect_uri(service):
    public = (CFG.get('public_url') or '').rstrip('/')
    if public:
        return f'{public}/callback/{service}'
    return f"http://localhost:{CFG['port']}/callback/{service}"

def connect_url(service):
    if service == 'strava':
        q = urllib.parse.urlencode({'client_id': CFG['strava_client_id'],
                                    'redirect_uri': redirect_uri('strava'),
                                    'response_type': 'code',
                                    'approval_prompt': 'auto',
                                    'scope': STRAVA_SCOPES,
                                    'state': STATE})
        return f'{STRAVA_AUTH}?{q}'
    q = urllib.parse.urlencode({'client_id': CFG['whoop_client_id'],
                                'redirect_uri': redirect_uri('whoop'),
                                'response_type': 'code',
                                'scope': WHOOP_SCOPES,
                                'state': STATE})
    return f'{WHOOP_AUTH}?{q}'

# ---------------- diagnosis (safe: sends no secrets anywhere) ----------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def probe(service):
    """Hit the provider's authorize URL exactly like 'Connect' would and report."""
    cid = str(CFG.get(f'{service}_client_id') or '')
    out = {'service': service, 'client_id': cid or '(empty)'}
    if not cid or 'PUT_YOUR' in cid:
        out['ok'] = False
        out['verdict'] = (f"The sync has no real {service.capitalize()} Client ID — config.json "
                          f"isn't filled in (or wasn't saved, or the sync wasn't restarted after editing).")
        return out
    url = connect_url(service)
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(urllib.request.Request(url, headers={'User-Agent': UA}), timeout=15) as r:
            code, loc = r.status, (r.headers.get('Location') or '')
            body = r.read(4000).decode('utf-8', 'ignore').lower()
    except urllib.error.HTTPError as e:
        code, loc, body = e.code, (e.headers.get('Location') or ''), ''
    except Exception as e:
        out['ok'] = False
        out['verdict'] = f'Could not reach {service.capitalize()}\'s website from your machine: {str(e)[:150]}'
        return out
    out['http'] = code
    if service == 'strava':
        if code == 200 or (code in (301, 302) and 'login' in (loc or '')):
            out['ok'] = True
            out['verdict'] = (f"✓ Strava accepts Client ID {cid} and the localhost redirect (it asks you to log in — normal). "
                              f"Connect WILL work. If it errors AFTER you log in, the app's Authorization Callback Domain "
                              f"on strava.com/settings/api isn't exactly “localhost”.")
        elif code == 400:
            out['ok'] = False
            out['verdict'] = (f"✗ Strava does NOT recognize Client ID “{cid}”. That app does not exist on their side. "
                              f"Go to strava.com/settings/api → create the app (Authorization Callback Domain: exactly “localhost”) "
                              f"→ put the new Client ID and Client Secret into config.json → save → restart the sync.")
        else:
            out['ok'] = False
            out['verdict'] = (f'Strava answered HTTP {code}. If you just edited config.json, restart the sync; '
                              f'otherwise re-check the app at strava.com/settings/api.')
    else:
        loc_l = (loc or '').lower()
        if 'invalid_client' in loc_l:
            out['ok'] = False
            out['verdict'] = (f"✗ Whoop does NOT recognize Client ID “{cid}” — this app doesn't exist at developer.whoop.com "
                              f"(or wasn't saved). Create it there (Redirect URI exactly “http://localhost:8765/callback/whoop”), "
                              f"put the new ID + Secret into config.json, restart the sync.")
        elif 'error=' in loc_l and 'invalid_state' not in loc_l:
            out['ok'] = False
            out['verdict'] = f'Whoop rejected the request: {loc.split("error=")[1].split("&")[0]} — copy this page to Hermes.'
        elif code in (400, 401, 403, 404):
            out['ok'] = False
            out['verdict'] = (f'Whoop answered HTTP {code}. If you just edited config.json, restart the sync; '
                              f'otherwise re-check the app at developer.whoop.com.')
        else:
            out['ok'] = True
            out['verdict'] = (f"✓ Whoop accepts Client ID {cid} and the redirect URI. "
                              f"Connect WILL work — if you still see errors, close the sync window, restart it, and retry.")
    return out

def diag_html():
    rows = ''
    for svc in ('strava', 'whoop'):
        r = probe(svc)
        color = '#4ade80' if r.get('ok') else '#f87171'
        rows += (f'<div style="border:1px solid #22304a;border-radius:10px;padding:14px;margin:10px 0;background:#0e1524">'
                 f'<div style="font-weight:700;margin-bottom:6px">{r["service"].capitalize()} '
                 f'<span style="color:#8fa3bf;font-weight:400">(Client ID: {r.get("client_id")}, '
                 f'test reply: HTTP {r.get("http", "—")})</span></div>'
                 f'<div style="color:{color}">{r.get("verdict")}</div>'
                 f'<div style="color:#a78bfa;font-size:13px;margin-top:6px">Last login attempt seen by the sync: '
                 f'{CALLBACKS.get(r["service"]) or "none yet — click Connect on the dashboard, then reopen this page"}</div></div>')
    st = bool(TOKENS.get('strava')); wh = bool(TOKENS.get('whoop'))
    tokens = (f'<p>Accounts connected so far: <b style="color:{"#4ade80" if st else "#f87171"}">Strava {"✓" if st else "✗"}</b> · '
              f'<b style="color:{"#4ade80" if wh else "#f87171"}">Whoop {"✓" if wh else "✗"}</b></p>')
    errs = ''.join(f'<p style="color:#f87171">Last {n} sync error: {e}</p>' for n, e in LAST_ERRORS.items() if e)
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>RideReady Sync — Diagnostics</title></head>'
            f'<body style="background:#0b1220;color:#e2e8f0;font:15px -apple-system,Segoe UI,sans-serif;'
            f'max-width:760px;margin:30px auto;padding:0 16px">'
            f'<h2>&#9889; RideReady Sync v{SYNC_VERSION} — Diagnostics</h2>'
            f'<p style="color:#8fa3bf">This page tested your Client IDs against Strava and Whoop just now '
            f'(no secrets were sent anywhere — only the public ID, same as the Connect buttons do).</p>'
            f'{rows}{tokens}{errs}'
            f'<p style="color:#8fa3bf;margin-top:24px">If anything above is unclear or you need help, '
            f'select this whole page (Ctrl+A / Cmd+A), copy, and paste it to Hermes.</p></body></html>')

# ---------------- auth + profile + vision (v2) ----------------
import hashlib

def _token():
    return hashlib.sha256(('rr' + CFG.get('password', '')).encode()).hexdigest()[:32]

def auth_enabled():
    return bool(CFG.get('password'))

def load_profile():
    return load_json(PROFILE_PATH, None)

def save_profile(state):
    PROFILE_PATH.write_text(json.dumps(state))

VISION_PROMPTS = {
    'fridge': ('Look at this photo of a fridge/pantry. List every distinct food or drink item you can see, '
               'as plain short item names (e.g. "chicken breast", "spinach", "eggs", "greek yogurt", "almond milk"). '
               'Ignore packaging brands and non-food items. Respond ONLY with JSON in exactly this shape: '
               '{"items": ["item one", "item two"]}'),
    'label': ('Look at this photo of a prepared meal or its nutrition label. Extract the meal name and its '
              'per-serving nutrition. Respond ONLY with JSON in exactly this shape: '
              '{"name": "meal name", "kcal": 0, "p": 0, "c": 0, "f": 0, "fib": 0} '
              '(kcal = calories, p = protein grams, c = carbs grams, f = fat grams, fib = fiber grams).'),
}

def vision_scan(kind, image_data_url):
    if not CFG.get('nous_api_key'):
        return {'ok': False, 'error': 'Photo scanning needs a Nous API key in config.json (nous_api_key) — see SETUP.md.'}
    prompt = VISION_PROMPTS.get(kind, VISION_PROMPTS['fridge'])
    try:
        resp = http_json('https://inference-api.nousresearch.com/v1/chat/completions', method='POST', data={
            'model': CFG.get('vision_model') or 'Qwen/Qwen2.5-VL-72B-Instruct',
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': image_data_url}},
            ]}],
            'max_tokens': 800,
        }, headers={'Authorization': f"Bearer {CFG['nous_api_key']}"})
    except SyncError as e:
        return {'ok': False, 'error': f'Vision API call failed: {str(e)[:250]}'}
    content = ((resp.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
    # tolerate ```json fences
    txt = content.strip()
    if txt.startswith('```'):
        txt = txt.strip('`').lstrip('json').strip()
    try:
        start, end = txt.find('{'), txt.rfind('}')
        result = json.loads(txt[start:end + 1])
    except Exception:
        return {'ok': False, 'error': f'Could not parse the model reply: {txt[:200]}'}
    return {'ok': True, 'result': result}

def banner_html():
    def chip(name, ok):
        if ok:
            return f'<span style="color:#4ade80;font-weight:600">{name} &#10003;</span>'
        # href MUST be lowercase — the server routes are lowercase-only
        return (f'<a href="/connect/{name.lower()}" style="color:#38bdf8;font-weight:600">'
                f'Connect {name} &#8594;</a>')
    s_ok, w_ok = bool(TOKENS.get('strava')), bool(TOKENS.get('whoop'))
    errs = ''
    for name in ('strava', 'whoop'):
        if LAST_ERRORS.get(name):
            errs += f' <span style="color:#f87171">({name} error — see terminal)</span>'
    upd = DATA.get('updated')
    last = f'last sync {upd.replace("T"," ").replace("Z","")}' if upd else 'no sync yet'
    return (f'<div id="rr-sync-banner" style="background:#0e1524;border-bottom:1px solid #22304a;'
            f'padding:7px 18px;font:13px -apple-system,Segoe UI,sans-serif;color:#8fa3bf;'
            f'display:flex;gap:18px;align-items:center;flex-wrap:wrap">'
            f'<b style="color:#e2e8f0">&#9889; RideReady Sync</b>'
            f'{chip("Strava", s_ok)}{chip("Whoop", w_ok)}'
            f'<a href="/diag" style="color:#a78bfa;font-weight:600">Diagnose</a>'
            f'<span>{last}</span>{errs}'
            f'<span style="color:#64748b;margin-left:auto">v{SYNC_VERSION}</span></div>')

def serve_app():
    html = APP_HTML.read_text()
    payload = {'updated': DATA.get('updated'), 'days': strip_private(DATA.get('days', {})),
               'ftp_estimate': rolling_ftp(DATA.get('days', {})), 'auth': auth_enabled()}
    inject = ('<script>window.RIDEREADY_SYNC='
              + json.dumps(payload).replace('</', '<\\/')
              + ';</script>' + banner_html())
    return html.replace('<body>', '<body>' + inject, 1)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='text/html; charset=utf-8'):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to):
        self.send_response(302)
        self.send_header('Location', to)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _cookies(self):
        out = {}
        for part in (self.headers.get('Cookie') or '').split(';'):
            if '=' in part:
                k, _, v = part.strip().partition('=')
                out[k] = v
        return out

    def _authed(self):
        if not auth_enabled():
            return True
        return self._cookies().get('rr_token') == _token()

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), 'application/json')

    def do_GET(self):
        try:
            path, _, query = self._path.partition('?')
            path = path.lower()  # tolerate hand-typed URLs like /connect/Strava
            params = dict(urllib.parse.parse_qsl(query or ''))
            if path == '/':
                return self._send(200, serve_app())
            # data endpoints require login (when a password is set); the shell page does not
            if not self._authed() and path != '/favicon.ico' and not path.startswith('/callback/'):
                return self._json(401, {'error': 'login required'})
            if path == '/profile':
                p = load_profile()
                return self._send(200, json.dumps(p) if p else 'null', 'application/json')
            if path == '/diag':
                return self._send(200, diag_html())
            if path == '/connect/strava':
                return self._redirect(connect_url('strava'))
            if path == '/connect/whoop':
                return self._redirect(connect_url('whoop'))
            if path.startswith('/callback/'):
                service = path.split('/')[2]
                if params.get('error'):
                    _cb(service, f'{service.capitalize()} refused: {params.get("error")} — {params.get("error_description", "")[:120]}')
                    err = params.get('error')
                    hint = ''
                    if err == 'invalid_client':
                        hint = (' — the Client ID in config.json is not recognized. '
                                f'Re-check it on {"strava.com/settings/api" if service == "strava" else "developer.whoop.com"}, '
                                'update config.json, and restart the sync.')
                    return self._send(502, f'{service.capitalize()} refused the connection: {err}'
                                         f'{hint} (details: {params.get("error_description", "")})')
                if params.get('state') != STATE:
                    _cb(service, 'security check failed (state mismatch) — click Connect again from the dashboard')
                    return self._send(400, 'Security check failed (state mismatch) — click Connect again from the dashboard.')
                code = params.get('code')
                if not code:
                    _cb(service, 'login round-trip arrived with no authorization code — click Connect again')
                    return self._send(400, 'Missing authorization code. Try connecting again from the dashboard.')
                try:
                    if service == 'strava':
                        strava_exchange(code)
                    elif service == 'whoop':
                        whoop_exchange(code)
                    else:
                        return self._send(404, 'Unknown service')
                except SyncError as e:
                    _cb(service, f'token exchange failed: {str(e)[:160]}')
                    return self._send(502, f'Authorization failed: {e}')
                _cb(service, 'authorized successfully ✓ — pulling your data')
                do_sync()  # pull data immediately on first connect
                return self._redirect('/')
            if path == '/status':
                return self._send(200, json.dumps({
                    'strava_connected': bool(TOKENS.get('strava')),
                    'whoop_connected': bool(TOKENS.get('whoop')),
                    'last_sync': DATA.get('updated'),
                    'errors': LAST_ERRORS,
                }), 'application/json')
            if path == '/favicon.ico':
                return self._send(204, b'')
            self._send(404, 'Not found')
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._send(500, f'Error: {e}')
            except Exception:
                pass

    @property
    def _path(self):
        return self.path

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(length).decode() if length else ''
            body = json.loads(raw) if raw.strip() else {}
            path, _, _ = self.path.partition('?')
            path = path.lower()
            if path == '/login':
                if auth_enabled() and body.get('password') == CFG.get('password'):
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Set-Cookie',
                                     f'rr_token={_token()}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000')
                    self.send_header('Content-Length', '11')
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                    return
                return self._json(401, {'error': 'wrong password'})
            if not self._authed():
                return self._json(401, {'error': 'login required'})
            if path == '/vision':
                return self._json(200, vision_scan(body.get('kind', 'fridge'), body.get('image', '')))
            if path == '/profile':
                save_profile(body)
                return self._json(200, {'ok': True})
            self._json(404, {'error': 'not found'})
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._json(500, {'error': str(e)[:200]})
            except Exception:
                pass

# ---------------------------------------------------------------- main
def main():
    if not APP_HTML.exists():
        sys.exit(f'rideready.html not found next to sync.py ({BASE})')
    missing = [k for k in ('strava_client_id', 'strava_client_secret',
                           'whoop_client_id', 'whoop_client_secret') if not CFG.get(k)]
    if missing and not any(TOKENS.get(s) for s in ('strava', 'whoop')):
        print('!! config.json incomplete:', ', '.join(missing))
        print('   Fill it in first — see SETUP.md. Starting anyway so you can see the dashboard.')
    port = int(os.environ.get('PORT') or CFG.get('port') or 8765)
    cloud = bool(os.environ.get('PORT'))           # Railway sets PORT; bind all interfaces there
    threading.Thread(target=poll_loop, daemon=True).start()
    try:
        server = ThreadingHTTPServer(('0.0.0.0' if cloud else '127.0.0.1', port), Handler)
    except OSError as e:
        print()
        print('!! Could not start: ' + str(e))
        print(f'   Another RideReady Sync is probably STILL RUNNING and owns port {port}.')
        print('   Kill the old one:')
        print('     Mac:   open Activity Monitor -> search "python" -> select -> click the (X) Stop button -> Quit')
        print('     Windows: open Task Manager -> find "Python" -> End task')
        print('   Then run this start script again.')
        try:
            input('\nPress Enter to close this window... ')
        except Exception:
            pass
        sys.exit(1)
    url = f'http://localhost:{port}'
    print(f'RideReady Sync running -> {url}  (Ctrl+C to stop)' + ('  [cloud mode: 0.0.0.0:' + str(port) + ']' if cloud else ''))
    if CFG.get('open_browser', True) and not cloud:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nbye')

if __name__ == '__main__':
    main()
