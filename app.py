import os
import sys
import json
import time
import hashlib
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

import requests
from flask import Flask, render_template, request, jsonify, send_file

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MyGPAutoClaim")
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# --- Configuration & Paths ---
BASE_URL = "https://mygp.grameenphone.com"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

# Railway persistent volume support (falls back to BASE_DIR locally)
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", BASE_DIR)
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = BASE_DIR

ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")

DEFAULT_REWARDS = [
    (1, "5 SMS", "5 SMS (3 Days)"),
    (2, "10 SMS", "10 SMS (3 Days)"),
    (3, "15 SMS", "15 SMS (3 Days)"),
    (4, "30 SMS", "30 SMS (3 Days)"),
    (5, "MCA", "Missed Call Alert (30 Days)"),
    (6, "OTT Pass", "OTT Entertainment Pass (7 Days)"),
    (7, "1 GB", "1 GB Internet (3 Days)")
]

AIR_TRANSACTION_MAP = {
    1: "CMPSMS5TK0V3DA2LGNSTRK1",
    2: "CMPSMS10TK0V3DA2LGNSTRK2",
    3: "CMPSMS15TK0V3DA2LGNSTRK3",
    4: "CMPSMS30TK0V3DA2LGNSTRK4",
    5: "CMPMCA0TK0V30DA2LGNSTRK5",
    6: "SBPASS_0TK_HOICHOI_30",
    7: "3GCMP1GB77DA0TKV1LGNSTRK7"
}

app = Flask(__name__, template_folder=TEMPLATES_DIR)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())

# In-memory Activity Logs for Frontend UI
activity_logs = []
last_refresh_attempts = {}
_streak_cache = {}

def get_bd_now() -> datetime:
    """Current Bangladesh Standard Time (UTC+6)."""
    return datetime.now(timezone(timedelta(hours=6)))

def add_log(msg: str, level: str = "INFO"):
    now_str = get_bd_now().strftime("%d-%b %I:%M:%S %p")
    activity_logs.append({"timestamp": now_str, "level": level, "message": msg})
    if len(activity_logs) > 100:
        activity_logs.pop(0)
    if level == "ERROR":
        logger.error(msg)
    elif level == "WARN":
        logger.warning(msg)
    else:
        logger.info(msg)

def normalize_msisdn(phone: str) -> str:
    cleaned = "".join(filter(str.isdigit, str(phone)))
    if cleaned.startswith("880") and len(cleaned) == 13:
        return cleaned
    if cleaned.startswith("0") and len(cleaned) == 11:
        return "88" + cleaned
    if len(cleaned) == 10 and cleaned.startswith("1"):
        return "880" + cleaned
    return cleaned

def get_device_id(msisdn: str) -> str:
    return hashlib.md5(f"mygp_device_{msisdn}".encode()).hexdigest()[:16]

# --- Storage Helpers ---
def load_accounts() -> Dict[str, Any]:
    if not os.path.exists(ACCOUNTS_FILE):
        return {}
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return {normalize_msisdn(a.get("msisdn", "")): a for a in data if a.get("msisdn")}
            return data if isinstance(data, dict) else {}
    except Exception as e:
        add_log(f"Error reading accounts.json: {e}", "ERROR")
        return {}

def save_accounts(accounts: Dict[str, Any]):
    try:
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2, ensure_ascii=False)
    except Exception as e:
        add_log(f"Error saving accounts.json: {e}", "ERROR")

# --- MyGP Client ---
class MyGPClient:
    def __init__(self, msisdn: str, account_data: Optional[dict] = None):
        self.msisdn = normalize_msisdn(msisdn)
        self.data = account_data or {}
        self.user_id = self.data.get("id")
        self.access_token = self.data.get("access_token")
        self.refresh_token = self.data.get("refresh_token")
        self.device_id = self.data.get("device_id") or get_device_id(self.msisdn)
        self.expire_at = self.data.get("expire_at", 0)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Android/32 MyGP/541 (en)",
            "APP-MSISDN": self.msisdn,
            "X-REFERENCE-ID": self.device_id,
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8"
        })
        if self.access_token:
            self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})

    def is_token_expired(self) -> bool:
        if not self.access_token:
            return True
        if self.expire_at and isinstance(self.expire_at, (int, float)) and self.expire_at > 0:
            return time.time() >= (self.expire_at - 300)
        return False

    def refresh_auth_token(self) -> bool:
        if not self.refresh_token:
            add_log(f"No refresh_token found for {self.msisdn}", "WARN")
            return False

        url = f"{BASE_URL}/mygpapi/v2/refresh-token-all/android"
        params = {"lang": "en", "ng": "0"}
        payload = {"refresh_token": self.refresh_token}
        headers = {
            "User-Agent": "Android/32 MyGP/541 (en)",
            "APP-MSISDN": self.msisdn,
            "X-REFERENCE-ID": self.device_id,
            "Authorization": f"Bearer {self.access_token}" if self.access_token else "",
            "Content-Type": "application/json; charset=UTF-8"
        }

        add_log(f"Refreshing access token for {self.msisdn}...", "INFO")
        try:
            resp = requests.post(url, params=params, headers=headers, json=payload, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                new_access = data.get("access_token") or data.get("token")
                new_refresh = data.get("refresh_token")
                new_expire = data.get("expire_at", 0)

                if new_access:
                    self.access_token = new_access
                    if new_refresh:
                        self.refresh_token = new_refresh
                    self.expire_at = new_expire
                    self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})

                    accounts = load_accounts()
                    if self.msisdn in accounts:
                        accounts[self.msisdn]["access_token"] = self.access_token
                        if new_refresh:
                            accounts[self.msisdn]["refresh_token"] = self.refresh_token
                        accounts[self.msisdn]["expire_at"] = self.expire_at
                        accounts[self.msisdn]["token_refreshed_at"] = get_bd_now().strftime("%Y-%m-%d %I:%M:%S %p BD")
                        save_accounts(accounts)

                    add_log(f"Token successfully renewed for {self.msisdn}", "SUCCESS")
                    return True
            add_log(f"Token refresh failed for {self.msisdn}: {resp.status_code} - {resp.text}", "ERROR")
        except Exception as e:
            add_log(f"Network error refreshing token for {self.msisdn}: {e}", "ERROR")
        return False

    def request_otp(self) -> tuple[bool, str]:
        url = f"{BASE_URL}/mygpapi/v2/otp-login"
        params = {"msisdn": self.msisdn, "lang": "en", "ng": "0"}
        add_log(f"Sending OTP request to {self.msisdn}...", "INFO")
        try:
            resp = self.session.get(url, params=params, timeout=15)
            if resp.status_code == 200 and resp.json().get("result") == "success":
                add_log(f"OTP sent successfully to {self.msisdn}", "SUCCESS")
                return True, "OTP sent successfully to your phone!"
            msg = resp.json().get("message", resp.text) if resp.text else f"Status {resp.status_code}"
            add_log(f"OTP send failed for {self.msisdn}: {msg}", "ERROR")
            return False, msg
        except Exception as e:
            add_log(f"Network error requesting OTP for {self.msisdn}: {e}", "ERROR")
            return False, str(e)

    def verify_otp(self, otp: str) -> tuple[bool, str]:
        url = f"{BASE_URL}/mygpapi/v2/otp-login"
        params = {"lang": "en", "ng": "0"}
        payload = {
            "msisdn": self.msisdn,
            "otp": str(otp).strip(),
            "app_version": "5.33.0",
            "device_id": self.device_id,
            "device_model": "SM-S901U1",
            "device_name": "Samsung"
        }
        add_log(f"Verifying OTP for {self.msisdn}...", "INFO")
        try:
            resp = self.session.post(url, params=params, json=payload, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if "token" in data or "access_token" in data:
                    self.user_id = data.get("id")
                    self.access_token = data.get("access_token") or data.get("token")
                    self.refresh_token = data.get("refresh_token")
                    self.expire_at = data.get("expire_at", 0)
                    self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})

                    accounts = load_accounts()
                    existing_history = accounts.get(self.msisdn, {}).get("claim_history", [])
                    accounts[self.msisdn] = {
                        "id": self.user_id,
                        "msisdn": self.msisdn,
                        "access_token": self.access_token,
                        "refresh_token": self.refresh_token,
                        "device_id": self.device_id,
                        "expire_at": self.expire_at,
                        "last_login": get_bd_now().strftime("%Y-%m-%d %I:%M:%S %p BD"),
                        "claim_history": existing_history
                    }
                    save_accounts(accounts)
                    add_log(f"Login successful for {self.msisdn}. Account saved.", "SUCCESS")
                    return True, "Login successful!"
            msg = resp.json().get("message", resp.text) if resp.text else f"Status {resp.status_code}"
            add_log(f"OTP verification failed for {self.msisdn}: {msg}", "ERROR")
            return False, msg
        except Exception as e:
            add_log(f"Network error verifying OTP for {self.msisdn}: {e}", "ERROR")
            return False, str(e)

    def fetch_cmp_offers(self) -> dict:
        url = f"{BASE_URL}/mygpapi/v2/cmp-offers"
        params = {"lang": "en", "ng": "0", "id": self.user_id or "0"}
        now_ts = time.time()

        if self.is_token_expired():
            self.refresh_auth_token()

        try:
            resp = self.session.get(url, params=params, timeout=15)
            if resp.status_code == 401:
                last_att = last_refresh_attempts.get(self.msisdn, 0)
                if now_ts - last_att > 30:
                    last_refresh_attempts[self.msisdn] = now_ts
                    add_log(f"401 Unauthorized for {self.msisdn}. Auto-refreshing token...", "WARN")
                    if self.refresh_auth_token():
                        self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})
                        resp = self.session.get(url, params=params, timeout=15)

            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            add_log(f"Network error fetching offers for {self.msisdn}: {e}", "ERROR")
        return {}

    def get_streak_progress(self, force_network: bool = False) -> dict:
        """Determines streak status directly from live GP API server response."""
        global _streak_cache
        now_ts = time.time()

        if not force_network and self.msisdn in _streak_cache:
            cache_entry = _streak_cache[self.msisdn]
            if (now_ts - cache_entry.get("ts", 0)) < 15:
                return cache_entry["data"]

        accounts = load_accounts()
        acc = accounts.get(self.msisdn, {})
        claim_history = acc.get("claim_history", [])
        today_str = get_bd_now().strftime("%Y-%m-%d")
        today_claimed_days = {
            h.get("day") for h in claim_history
            if h.get("date") == today_str and h.get("status") == "SUCCESS"
        }

        progress = {
            "msisdn": self.msisdn,
            "total_days": 7,
            "completed_count": 0,
            "completed_days": [],
            "ready_to_claim": None,
            "locked_days": [],
            "details": []
        }

        offers = self.fetch_cmp_offers()
        streaks = offers.get("claim_streak", {}).get("streaks", [])

        if streaks:
            progress["total_days"] = len(streaks)
            for idx, item in enumerate(streaks):
                day_num = idx + 1
                name = item.get("name", f"Day {day_num} Reward")
                desc = item.get("description", "")
                category = str(item.get("product_offering_category", "")).upper()
                status = str(item.get("claim_status", "0"))
                item_type = str(item.get("type", "")).lower()

                is_claim_today = (status == "1" or category == "CLAIM_TODAY" or item_type == "claim_today")
                is_server_completed = (status == "2" or category == "CLAIM_PROGRESS")
                is_claimed_today = (day_num in today_claimed_days)

                if is_claim_today:
                    if is_claimed_today:
                        state = "COMPLETED"
                        progress["completed_days"].append((day_num, name))
                    else:
                        state = "READY TO CLAIM"
                        item["day"] = day_num
                        progress["ready_to_claim"] = item
                elif is_server_completed or is_claimed_today:
                    state = "COMPLETED"
                    progress["completed_days"].append((day_num, name))
                else:
                    state = "LOCKED"
                    progress["locked_days"].append((day_num, name))

                progress["details"].append({
                    "day": day_num,
                    "name": name,
                    "desc": desc,
                    "state": state,
                    "raw_item": item
                })
            progress["completed_count"] = len(progress["completed_days"])
        else:
            last_completed = acc.get("last_completed_day", 0)
            last_date = acc.get("last_claim_date", "")
            is_done_today = (last_date == today_str and last_completed > 0) or bool(today_claimed_days)

            for day_num, name, desc in DEFAULT_REWARDS:
                if day_num <= last_completed:
                    state = "COMPLETED"
                    progress["completed_days"].append((day_num, name))
                elif day_num == last_completed + 1 and not is_done_today:
                    state = "READY TO CLAIM"
                    progress["ready_to_claim"] = {"name": name, "description": desc, "day": day_num}
                else:
                    state = "LOCKED"
                    progress["locked_days"].append((day_num, name))

                progress["details"].append({
                    "day": day_num,
                    "name": name,
                    "desc": desc,
                    "state": state,
                    "raw_item": {}
                })
            progress["completed_count"] = last_completed

        _streak_cache[self.msisdn] = {"ts": now_ts, "data": progress}
        return progress

    def sync_streak_from_server(self) -> dict:
        global _streak_cache
        offers = self.fetch_cmp_offers()
        streaks = offers.get("claim_streak", {}).get("streaks", [])

        if streaks:
            completed_days = sum(
                1 for s in streaks
                if str(s.get("claim_status", "0")) == "2" or str(s.get("product_offering_category", "")).upper() == "CLAIM_PROGRESS"
            )
            accounts = load_accounts()
            if self.msisdn in accounts:
                claim_history = accounts[self.msisdn].get("claim_history", [])
                today_str = get_bd_now().strftime("%Y-%m-%d")
                today_count = sum(1 for h in claim_history if h.get("date") == today_str and h.get("status") == "SUCCESS")
                accounts[self.msisdn]["last_completed_day"] = max(completed_days, today_count)
                save_accounts(accounts)

        if self.msisdn in _streak_cache:
            del _streak_cache[self.msisdn]
        return self.get_streak_progress(force_network=True)

    def claim_reward(self, item: dict) -> tuple[bool, str]:
        global _streak_cache
        url = f"{BASE_URL}/mygpapi/cmp/opt-in"
        params = {"lang": "en", "ng": "0", "id": self.user_id or "0"}

        payload = item.get("raw_item", item).copy() if isinstance(item, dict) else {}
        day_num = payload.get("day", 1)
        if not payload.get("air_transaction_type"):
            payload["air_transaction_type"] = AIR_TRANSACTION_MAP.get(day_num, "CMPSMS5TK0V3DA2LGNSTRK1")
        if not payload.get("campaign_id"):
            payload["campaign_id"] = f"TOD_D{day_num}"
        if not payload.get("keyword"):
            payload["keyword"] = f"TOD_D{day_num}"

        payload["product_offering_category"] = "CLAIM_TODAY"
        payload["purchase_method"] = "cmp_balance"
        payload["price"] = 0.0
        payload["claim_status"] = "1"
        payload["type"] = "claim_today"
        payload["is_lukano_reward"] = True

        headers = {
            "User-Agent": "Android/32 MyGP/541 (en)",
            "Accept-Language": "en",
            "APP-MSISDN": self.msisdn,
            "APP-MSISDN-OLD": self.msisdn,
            "X-REFERENCE-ID": self.device_id,
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json; charset=UTF-8"
        }

        reward_name = payload.get("name") or item.get("name", "Streak Reward")
        add_log(f"Attempting to claim {reward_name} (Day {day_num}) for {self.msisdn}...", "INFO")
        try:
            resp = self.session.post(url, params=params, headers=headers, json=payload, timeout=20)
            if resp.status_code == 401:
                add_log(f"401 during claim for {self.msisdn}. Refreshing token and retrying...", "WARN")
                if self.refresh_auth_token():
                    headers["Authorization"] = f"Bearer {self.access_token}"
                    self.session.headers.update(headers)
                    resp = self.session.post(url, params=params, headers=headers, json=payload, timeout=20)

            if resp.status_code == 200:
                data = resp.json()
                msg = data.get("message") or "SUCCESS"
                add_log(f"Claim SUCCESS for {self.msisdn}: {msg}", "SUCCESS")
                if self.msisdn in _streak_cache:
                    del _streak_cache[self.msisdn]
                return True, msg
            msg = resp.json().get("message", resp.text) if resp.text else f"Status {resp.status_code}"
            add_log(f"Claim response for {self.msisdn}: {msg}", "WARN")
            return False, msg
        except Exception as e:
            add_log(f"Network error claiming reward for {self.msisdn}: {e}", "ERROR")
            return False, str(e)

    def record_claim_history(self, day_num: int, name: str, desc: str, status_msg: str):
        if "processed" in status_msg.lower() or "success" in status_msg.lower():
            status_msg = "SUCCESS"

        accounts = load_accounts()
        acc = accounts.get(self.msisdn, {})
        history = acc.get("claim_history", [])
        now_bd = get_bd_now()
        date_str = now_bd.strftime("%Y-%m-%d")
        time_str = now_bd.strftime("%Y-%m-%d %I:%M:%S %p BD")

        if day_num == 1:
            history = [{
                "date": date_str,
                "time": time_str,
                "day": 1,
                "name": name,
                "description": desc,
                "status": status_msg
            }]
        else:
            exists = False
            for h in history:
                if h.get("day") == day_num:
                    h["status"] = status_msg
                    h["time"] = time_str
                    h["date"] = date_str
                    h["name"] = name
                    h["description"] = desc
                    exists = True
                    break
            if not exists:
                history.append({
                    "date": date_str,
                    "time": time_str,
                    "day": day_num,
                    "name": name,
                    "description": desc,
                    "status": status_msg
                })

        acc["claim_history"] = history
        acc["last_claim_date"] = date_str
        acc["last_claim_time"] = time_str
        acc["last_completed_day"] = day_num
        accounts[self.msisdn] = acc
        save_accounts(accounts)

    def process_daily_claim(self) -> dict:
        progress = self.get_streak_progress(force_network=True)
        accounts = load_accounts()
        acc = accounts.get(self.msisdn, {})
        claim_history = acc.get("claim_history", [])
        today_str = get_bd_now().strftime("%Y-%m-%d")

        if progress.get("ready_to_claim"):
            item = progress["ready_to_claim"]
            real_day = item.get("day") or (progress["completed_count"] + 1)

            already_claimed_today = any(
                h.get("date") == today_str and h.get("day") == real_day and h.get("status") == "SUCCESS"
                for h in claim_history
            )
            if already_claimed_today:
                return {
                    "success": True,
                    "claimed": False,
                    "message": f"Day {real_day} ({item.get('name')}) already submitted today! Processing on GP server.",
                    "progress": progress
                }

            success, msg = self.claim_reward(item)
            self.record_claim_history(real_day, item.get("name", ""), item.get("description", ""), msg)
            self.sync_streak_from_server()
            progress = self.get_streak_progress(force_network=False)
            return {
                "success": success,
                "claimed": True,
                "message": f"Claimed Day {real_day} ({item.get('name')}): {msg}",
                "progress": progress
            }

        last_time = acc.get("last_claim_time", "")
        return {
            "success": True,
            "claimed": False,
            "message": f"All available offers claimed! Next unlock at 12:00 AM BD (Last: {last_time or 'Done'}).",
            "progress": progress
        }

# --- Batch Actions ---
def sync_all_accounts():
    accounts = load_accounts()
    if not accounts:
        return
    add_log(f"Syncing live GP streak progress for {len(accounts)} account(s)...", "INFO")
    for msisdn, acc_data in accounts.items():
        try:
            client = MyGPClient(msisdn, acc_data)
            client.sync_streak_from_server()
        except Exception as e:
            add_log(f"Sync error for {msisdn}: {e}", "ERROR")
    add_log("Account streaks successfully synced with GP server.", "SUCCESS")

def run_all_claims() -> dict:
    accounts = load_accounts()
    if not accounts:
        add_log("No accounts found in accounts.json to claim.", "WARN")
        return {}

    add_log(f"Starting claim check for {len(accounts)} account(s)...", "INFO")
    results = {}
    for msisdn, acc_data in accounts.items():
        try:
            client = MyGPClient(msisdn, acc_data)
            results[msisdn] = client.process_daily_claim()
        except Exception as e:
            add_log(f"Claim loop error for {msisdn}: {e}", "ERROR")
            results[msisdn] = {"success": False, "claimed": False, "message": str(e)}
    return results

# --- Background Daemon (24/7 Auto-Worker) ---
def background_scheduler_loop():
    time.sleep(3)
    add_log("24/7 Auto-Claim Daemon started.", "SUCCESS")

    try:
        run_all_claims()
    except Exception as e:
        add_log(f"Startup claim error: {e}", "ERROR")

    while True:
        try:
            now_bd = get_bd_now()
            is_midnight_window = (now_bd.hour == 23 and now_bd.minute >= 58) or (now_bd.hour == 0 and now_bd.minute <= 35)

            accounts = load_accounts()
            if accounts:
                for msisdn, acc_data in accounts.items():
                    try:
                        client = MyGPClient(msisdn, acc_data)
                        expire_at = acc_data.get("expire_at", 0)
                        if client.is_token_expired() or (expire_at > 0 and (expire_at - time.time()) < 14400):
                            add_log(f"Proactive token renewal for {msisdn}...", "INFO")
                            client.refresh_auth_token()

                        res = client.process_daily_claim()
                        if res.get("claimed"):
                            add_log(f"Auto-claimed successfully for {msisdn}: {res.get('message')}", "SUCCESS")
                    except Exception as e:
                        add_log(f"Auto-claim loop error for {msisdn}: {e}", "ERROR")

            time.sleep(60 if is_midnight_window else 300)
        except Exception as e:
            add_log(f"Daemon error: {e}", "ERROR")
            time.sleep(60)

# --- Start daemon at import (needed for gunicorn) ---
_daemon_lock = threading.Lock()
_daemon_started = False

def start_daemon_once():
    global _daemon_started
    with _daemon_lock:
        if _daemon_started:
            return
        _daemon_started = True
        t = threading.Thread(target=background_scheduler_loop, daemon=True, name="AutoClaimDaemon")
        t.start()

# Only start if running under gunicorn (not the flask reloader)
if os.environ.get("WERKZEUG_RUN_MAIN") != "true" or os.environ.get("RAILWAY_ENVIRONMENT"):
    start_daemon_once()

# --- Web Routes & APIs ---
@app.route("/")
def index():
    try:
        return render_template("index.html")
    except Exception:
        index_path = os.path.join(TEMPLATES_DIR, "index.html")
        if os.path.exists(index_path):
            with open(index_path, "r", encoding="utf-8") as f:
                return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}
        return "<h1>Template index.html not found</h1>", 500

@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "time": get_bd_now().isoformat()})

@app.route("/api/status")
def api_status():
    accounts = load_accounts()
    account_list = []
    now_bd = get_bd_now()
    today_str = now_bd.strftime("%Y-%m-%d")

    for msisdn, acc_data in accounts.items():
        client = MyGPClient(msisdn, acc_data)
        progress = client.get_streak_progress(force_network=False)

        history = acc_data.get("claim_history", [])
        last_date = acc_data.get("last_claim_date", "")
        last_time = acc_data.get("last_claim_time", "N/A")

        ready_item = progress.get("ready_to_claim")
        claimed_today_for_ready = bool(
            ready_item and any(
                h.get("date") == today_str and h.get("day") == ready_item.get("day") and h.get("status") == "SUCCESS"
                for h in history
            )
        )
        is_done_today = not (ready_item and not claimed_today_for_ready)

        account_list.append({
            "msisdn": msisdn,
            "id": acc_data.get("id"),
            "completed_count": progress["completed_count"],
            "total_days": progress["total_days"],
            "ready_to_claim": ready_item if not claimed_today_for_ready else None,
            "is_done_today": is_done_today,
            "last_claim_date": last_date,
            "last_claim_time": last_time if last_time != "N/A" else (history[-1].get("time", "N/A") if history else "N/A"),
            "details": progress["details"],
            "history": history
        })

    next_midnight = (now_bd + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    seconds_to_midnight = max(0, int((next_midnight - now_bd).total_seconds()))

    return jsonify({
        "success": True,
        "current_bd_time": now_bd.strftime("%d-%b-%Y %I:%M:%S %p"),
        "seconds_to_midnight": seconds_to_midnight,
        "accounts": account_list,
        "total_accounts": len(account_list),
        "logs": activity_logs[-30:]
    })

@app.route("/api/otp/request", methods=["POST"])
def api_otp_request():
    data = request.json or {}
    phone = data.get("phone", "").strip()
    if not phone:
        return jsonify({"success": False, "message": "Phone number is required."}), 400
    client = MyGPClient(phone)
    success, msg = client.request_otp()
    return jsonify({"success": success, "message": msg, "msisdn": client.msisdn})

@app.route("/api/otp/verify", methods=["POST"])
def api_otp_verify():
    data = request.json or {}
    phone = data.get("phone", "").strip()
    otp = data.get("otp", "").strip()
    if not phone or not otp:
        return jsonify({"success": False, "message": "Phone and OTP are required."}), 400
    client = MyGPClient(phone)
    success, msg = client.verify_otp(otp)
    if success:
        client.sync_streak_from_server()
        client.process_daily_claim()
        return jsonify({"success": True, "message": "Account added, GP streak synced & initial claim processed!"})
    return jsonify({"success": False, "message": msg}), 400

@app.route("/api/claim/all", methods=["POST"])
def api_claim_all():
    results = run_all_claims()
    return jsonify({"success": True, "message": "All accounts checked and claimed!", "results": results})

@app.route("/api/claim/<phone>", methods=["POST"])
def api_claim_single(phone):
    accounts = load_accounts()
    msisdn = normalize_msisdn(phone)
    if msisdn not in accounts:
        return jsonify({"success": False, "message": "Account not found."}), 404
    client = MyGPClient(msisdn, accounts[msisdn])
    client.sync_streak_from_server()
    res = client.process_daily_claim()
    return jsonify({"success": True, "result": res})

@app.route("/api/tokens/refresh", methods=["POST"])
def api_refresh_tokens():
    accounts = load_accounts()
    refreshed = 0
    for msisdn, acc_data in accounts.items():
        client = MyGPClient(msisdn, acc_data)
        if client.refresh_auth_token():
            refreshed += 1
        client.sync_streak_from_server()
    return jsonify({"success": True, "message": f"Successfully refreshed {refreshed}/{len(accounts)} accounts & synced streaks."})

@app.route("/api/accounts/download")
def api_accounts_download():
    if os.path.exists(ACCOUNTS_FILE):
        return send_file(ACCOUNTS_FILE, mimetype="application/json", as_attachment=True, download_name="accounts.json")
    return jsonify({"success": False, "message": "accounts.json not found"}), 404

@app.route("/api/account/delete", methods=["POST"])
def api_delete_account():
    data = request.json or {}
    msisdn = normalize_msisdn(data.get("phone", ""))
    accounts = load_accounts()
    if msisdn in accounts:
        del accounts[msisdn]
        save_accounts(accounts)
        add_log(f"Account {msisdn} deleted.", "INFO")
        return jsonify({"success": True, "message": f"Account {msisdn} deleted successfully."})
    return jsonify({"success": False, "message": "Account not found."}), 404

# --- Cron Endpoint (Railway Cron / external cron) ---
@app.route("/api/cron/claim")
def api_cron_claim():
    secret = os.environ.get("CRON_SECRET", "")
    if secret:
        provided = request.headers.get("X-Cron-Secret") or request.args.get("secret")
        if provided != secret:
            return jsonify({"success": False, "message": "Unauthorized"}), 401
    results = run_all_claims()
    return jsonify({"success": True, "results": results})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
