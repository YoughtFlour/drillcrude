#!/usr/bin/env python3
"""
CRUDE Driller v6.7 - Solver optimization (Decimal revenue, suffix-stripped alts, async receipts)
Architecture: 3 async loops (drilling, claiming, monitoring)
Key improvement: LLM only identifies company, Python computes artifact deterministically
"""

import asyncio
import json
import os
import re
import time
import random
import secrets
import aiohttp
import sys
import math
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

# Load .env file if exists
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _v = _v.split("#")[0].strip()  # remove inline comments
            os.environ.setdefault(_k.strip(), _v)

# ============ PATHS (auto-detect: same dir as script) ============
SCRIPT_DIR = Path(__file__).parent.resolve()

PID_FILE = SCRIPT_DIR / "crude_driller.pid"
STATE_FILE = SCRIPT_DIR / "crude_driller_state.json"
LOG_FILE = SCRIPT_DIR / "crude_driller.log"
DEBUG_LOG_FILE = SCRIPT_DIR / "crude_debug.log"

# ============ SINGLE INSTANCE LOCK ============
def _is_pid_running(pid):
    """Check if a PID is running (cross-platform)"""
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, OSError):
            return False

def acquire_lock():
    """Ensure only one instance is running"""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            if _is_pid_running(old_pid):
                print(f"Another instance is already running (PID {old_pid})")
                sys.exit(1)
        except (ValueError, OSError):
            pass
        PID_FILE.unlink(missing_ok=True)
    PID_FILE.write_text(str(os.getpid()))

acquire_lock()

# ============ CONFIG ============
BANKR_API_KEY = os.getenv("BANKR_API_KEY", "")
COORDINATOR_URL = os.getenv("COORDINATOR_URL", "https://coordinator-production-38c0.up.railway.app")
DRILLER_ADDRESS = os.getenv("DRILLER_ADDRESS", "")
DRILLER_DEBUG = os.getenv("DRILLER_DEBUG", "false").lower() == "true"
DRILLER_QUIET = os.getenv("DRILLER_QUIET", "false").lower() == "true"

# LLM Config
LLM_BACKEND = os.getenv("LLM_BACKEND", "openrouter")  # "openrouter" or "zai"
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-4o-mini")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
ZAI_API_KEY = os.getenv("ZAI_API_KEY", "")

# Timing
DRILL_DELAY_MIN = float(os.environ.get("DRILL_DELAY_MIN", "3.0"))
DRILL_DELAY_MAX = float(os.environ.get("DRILL_DELAY_MAX", "60.0"))
DRILL_DELAY_INIT = float(os.environ.get("DRILL_DELAY", "1.0"))  # minimal — receipt posting provides natural ~4s pacing
CLAIM_INTERVAL = 1800  # 30 min
MONITOR_INTERVAL = 300  # 5 min

# Telegram notifications (optional)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Staking tier (wildcat=25M/1cr, platform=50M/2cr, deepwater=100M/3cr)
DRILLER_TIER = os.getenv("DRILLER_TIER", "platform").lower().strip()
TIER_CREDITS = {"wildcat": 1, "platform": 2, "deepwater": 3}
if DRILLER_TIER not in TIER_CREDITS:
    DRILLER_TIER = "platform"  # safe fallback

# Staking amounts (wei = tokens * 10^18)
TIER_STAKE_WEI = {
    "wildcat":  "25000000000000000000000000",
    "platform": "50000000000000000000000000",
    "deepwater":"100000000000000000000000000",
}
TIER_STAKE_DISPLAY = {"wildcat": "25M", "platform": "50M", "deepwater": "100M"}

# ============ LOGGING (buffered I/O) ============
LOG_MAX_BYTES = 10 * 1024 * 1024   # 10 MB — rotate when exceeded
LOG_KEEP_BYTES = 5 * 1024 * 1024   # keep last 5 MB after rotation
_log_check_counter = 0
_LOG_FLUSH_INTERVAL = 2        # flush main log every N seconds
_DEBUG_FLUSH_INTERVAL = 5      # flush debug log every N seconds
_log_buffer = []
_log_flush_ts = 0.0
_debug_buffer = []
_debug_flush_ts = 0.0

def _rotate_if_needed(filepath):
    """Rotate log file: when >10 MB, keep only last 5 MB."""
    global _log_check_counter
    _log_check_counter += 1
    if _log_check_counter % 200 != 0:  # check every ~200 writes, not every time
        return
    try:
        size = os.path.getsize(filepath)
        if size <= LOG_MAX_BYTES:
            return
        with open(filepath, "rb") as f:
            f.seek(size - LOG_KEEP_BYTES)
            tail = f.read()
        # Find first newline to avoid partial line
        nl = tail.find(b"\n")
        if nl >= 0:
            tail = tail[nl + 1:]
        with open(filepath, "wb") as f:
            f.write(b"[LOG ROTATED - kept last 5 MB]\n")
            f.write(tail)
    except Exception:
        pass

def _flush_log():
    """Flush buffered log lines to disk in one write."""
    global _log_buffer, _log_flush_ts
    if not _log_buffer:
        return
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("".join(_log_buffer))
        _log_buffer = []
        _log_flush_ts = time.time()
        _rotate_if_needed(LOG_FILE)
    except (PermissionError, OSError):
        pass

def _flush_debug():
    """Flush buffered debug lines to disk in one write."""
    global _debug_buffer, _debug_flush_ts
    if not _debug_buffer:
        return
    try:
        with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write("".join(_debug_buffer))
        _debug_buffer = []
        _debug_flush_ts = time.time()
        _rotate_if_needed(DEBUG_LOG_FILE)
    except (PermissionError, OSError):
        _debug_buffer = []  # drop on error rather than growing forever

def log(msg, level="INFO"):
    global _log_buffer, _log_flush_ts
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}\n"
    # Quiet mode: console only for important messages
    if not DRILLER_QUIET or level in ("ERROR", "WARN") or "ACCEPTED" in msg or "GUSHER" in msg or "credits" in msg.lower():
        print(line, end="", flush=True)
    _log_buffer.append(line)
    # Flush on error immediately, otherwise every N seconds
    now = time.time()
    if level == "ERROR" or now - _log_flush_ts >= _LOG_FLUSH_INTERVAL:
        _flush_log()

def debug_log(label, data):
    """Buffer debug data, flush periodically to reduce disk I/O."""
    global _debug_buffer, _debug_flush_ts
    if not DRILLER_DEBUG:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"\n{'='*60}\n[{ts}] {label}\n{'='*60}\n"
    if isinstance(data, (dict, list)):
        entry += json.dumps(data, indent=2, ensure_ascii=False)
    else:
        entry += str(data)
    entry += "\n"
    _debug_buffer.append(entry)
    now = time.time()
    if now - _debug_flush_ts >= _DEBUG_FLUSH_INTERVAL:
        _flush_debug()

# ============ TELEGRAM NOTIFICATIONS ============
_tg_session = None
_tg_error_ts = 0  # rate-limit error notifications

async def tg_init():
    """Create shared session for Telegram notifications."""
    global _tg_session
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        _tg_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        )

async def tg_close():
    """Close Telegram session."""
    global _tg_session
    if _tg_session:
        await _tg_session.close()
        _tg_session = None

async def tg_notify(msg, silent=False):
    """Send Telegram notification. Non-blocking, never raises."""
    if not _tg_session:
        return
    try:
        await _tg_session.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "disable_notification": silent
            }
        )
    except Exception:
        pass  # TG down — ignore, don't break drilling

async def tg_error(msg):
    """Send error notification with 5-min cooldown to prevent spam."""
    global _tg_error_ts
    now = time.time()
    if now - _tg_error_ts < 300:  # 5 min cooldown
        return
    _tg_error_ts = now
    await tg_notify(msg)

# ============ EXCEPTIONS ============
class AuthError(Exception): pass
class ForbiddenError(Exception): pass
class StaleError(Exception): pass
class RateLimitError(Exception): pass
class ServerError(Exception): pass

# ============ BACKOFF ============
async def backoff_sleep(attempt, base=2.0, cap=60.0):
    delay = min(base * (2 ** attempt), cap)
    jitter = delay * random.uniform(0, 0.25)
    await asyncio.sleep(delay + jitter)

# ============ STATE ============
class State:
    def __init__(self):
        self.drilled_epochs = set()
        self.total_solves = 0
        self.total_failures = 0
        self.total_credits = 0
        self.gushers = 0
        self.consecutive_failures = 0
        self._no_cid_attempts = 0
        self.site_stats = {}  # {"shallow/standard": {"accept": 0, "reject": 0}, ...}
        self.start_time = time.time()
        self.load()

    def load(self):
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                self.drilled_epochs = set(data.get("drilled_epochs", []))
                self.total_solves = data.get("total_solves", 0)
                self.total_failures = data.get("total_failures", 0)
                self.total_credits = data.get("total_credits", 0)
                self.gushers = data.get("gushers", 0)
                self.site_stats = data.get("site_stats", {})
                log(f"State loaded: {self.total_solves} solves, {self.total_credits} credits")
            except Exception as e:
                log(f"State load error: {e}", "WARN")

    def record_site(self, depth, richness, accepted):
        key = f"{depth}/{richness}"
        if key not in self.site_stats:
            self.site_stats[key] = {"accept": 0, "reject": 0}
        self.site_stats[key]["accept" if accepted else "reject"] += 1

    _SAVE_INTERVAL = 30  # save to disk max once per 30 seconds

    def save(self, force=False):
        now = time.time()
        if not force and hasattr(self, '_last_save') and now - self._last_save < self._SAVE_INTERVAL:
            return  # throttle disk writes
        self._last_save = now
        data = {
            "drilled_epochs": list(self.drilled_epochs),
            "total_solves": self.total_solves,
            "total_failures": self.total_failures,
            "total_credits": self.total_credits,
            "gushers": self.gushers,
            "site_stats": self.site_stats
        }
        try:
            STATE_FILE.write_text(json.dumps(data, separators=(',', ':')))
        except (PermissionError, OSError):
            pass

state = State()

# ============ BANKR CLIENT ============
class BankrClient:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.bankr.bot"
        self.session = None

    async def init(self):
        conn = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300, keepalive_timeout=30)
        self.session = aiohttp.ClientSession(connector=conn)

    async def close(self):
        if self.session:
            await self.session.close()

    async def sign(self, message):
        for attempt in range(5):
            try:
                async with self.session.post(
                    f"{self.base_url}/agent/sign",
                    json={"signatureType": "personal_sign", "message": message},
                    headers={"Content-Type": "application/json", "X-API-Key": self.api_key}
                ) as resp:
                    data = await resp.json()
                    return data["signature"]
            except Exception as e:
                if attempt < 4:
                    log(f"Bankr sign retry {attempt+1}/5: {e}", "WARN")
                    await asyncio.sleep(10)
                else:
                    raise

    async def submit_tx(self, tx, description="Transaction"):
        for attempt in range(5):
            try:
                async with self.session.post(
                    f"{self.base_url}/agent/submit",
                    json={
                        "transaction": {
                            "to": tx["to"],
                            "chainId": tx["chainId"],
                            "value": tx.get("value", "0"),
                            "data": tx["data"]
                        },
                        "description": description,
                        "waitForConfirmation": True
                    },
                    headers={"Content-Type": "application/json", "X-API-Key": self.api_key}
                ) as resp:
                    return await resp.json()
            except Exception as e:
                if attempt < 4:
                    log(f"Bankr submit retry {attempt+1}/5: {e}", "WARN")
                    await asyncio.sleep(10)
                else:
                    raise

    async def get_balances(self):
        async with self.session.post(
            f"{self.base_url}/agent/prompt",
            json={"prompt": "what are my balances on base?"},
            headers={"Content-Type": "application/json", "X-API-Key": self.api_key}
        ) as resp:
            data = await resp.json()
            job_id = data.get("jobId")

        for _ in range(20):
            await asyncio.sleep(2)
            async with self.session.get(
                f"{self.base_url}/agent/job/{job_id}",
                headers={"X-API-Key": self.api_key}
            ) as resp:
                data = await resp.json()
                if data.get("status") == "completed":
                    return data.get("response", "")
        return "timeout"

# ============ COORDINATOR CLIENT ============
class CoordinatorClient:
    def __init__(self, url, driller, bankr):
        self.url = url
        self.driller = driller
        self.bankr = bankr
        self.token = None
        self.token_expires = None
        self.session = None
        self._auth_lock = asyncio.Lock()

    async def init(self):
        conn = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300, keepalive_timeout=30)
        self.session = aiohttp.ClientSession(connector=conn)

    async def close(self):
        if self.session:
            await self.session.close()

    def _token_near_expiry(self):
        if not self.token_expires:
            return False
        try:
            exp_str = self.token_expires.replace("Z", "+00:00")
            exp = datetime.fromisoformat(exp_str)
            return (exp - datetime.now(timezone.utc)).total_seconds() < 60
        except Exception:
            return False

    def reset_auth(self):
        """Clear cached token so next ensure_auth() forces re-authentication."""
        self.token = None
        self.token_expires = None

    async def ensure_auth(self):
        async with self._auth_lock:
            if self.token and not self._token_near_expiry():
                return
            await self._do_auth()

    async def _do_auth(self):
        async with self.session.post(
            f"{self.url}/v1/auth/nonce",
            json={"miner": self.driller},
            headers={"Content-Type": "application/json"}
        ) as resp:
            nonce_data = await resp.json()
            if "message" not in nonce_data:
                raise AuthError(f"Nonce request failed: {nonce_data}")

        message = nonce_data["message"]
        signature = await self.bankr.sign(message)

        async with self.session.post(
            f"{self.url}/v1/auth/verify",
            json={"miner": self.driller, "message": message, "signature": signature},
            headers={"Content-Type": "application/json"}
        ) as resp:
            verify_data = await resp.json()
            if "token" not in verify_data:
                raise AuthError(f"Verify failed: {verify_data}")

        self.token = verify_data["token"]
        self.token_expires = verify_data.get("expiresAt")
        log(f"Auth OK, expires: {self.token_expires}")

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}"
        }

    def _check_status(self, status, data, headers=None):
        """Check HTTP status and raise appropriate exception"""
        if status == 401:
            self.token = None  # force re-auth
            raise AuthError(data.get("error", "Unauthorized"))
        elif status == 403:
            raise ForbiddenError(data.get("error", "Forbidden - insufficient stake?"))
        elif status == 404:
            raise StaleError(data.get("error", "Not found / stale challenge"))
        elif status == 429:
            retry_after = None
            if headers:
                retry_after = headers.get("Retry-After") or headers.get("retry-after")
            debug_log("429_RESPONSE", {"body": data, "retry_after": retry_after, "headers": {k: v for k, v in (headers or {}).items() if k.lower() in ("retry-after", "x-ratelimit-remaining", "x-ratelimit-reset", "x-ratelimit-limit")}})
            err = RateLimitError(data.get("error", "Rate limited"))
            err.retry_after = float(retry_after) if retry_after else None
            raise err
        elif status >= 500:
            raise ServerError(data.get("error", f"Server error {status}"))

    async def get_sites(self):
        headers = self._headers() if self.token else {}
        async with self.session.get(f"{self.url}/v1/sites", headers=headers) as resp:
            return await resp.json()

    async def get_credits(self):
        async with self.session.get(f"{self.url}/v1/credits?miner={self.driller}") as resp:
            return await resp.json()

    async def get_epoch(self):
        async with self.session.get(f"{self.url}/v1/epoch") as resp:
            return await resp.json()

    async def drill(self, site_id, nonce):
        async with self.session.get(
            f"{self.url}/v1/drill?miner={self.driller}&siteId={site_id}&nonce={nonce}",
            headers=self._headers()
        ) as resp:
            data = await resp.json()
            self._check_status(resp.status, data, resp.headers)
            return data

    async def submit(self, challenge_id, artifact, nonce, site_id, trace):
        async with self.session.post(
            f"{self.url}/v1/submit",
            json={
                "miner": self.driller,
                "challengeId": challenge_id,
                "artifact": artifact,
                "siteId": site_id,
                "requestNonce": nonce,
                "trace": trace
            },
            headers=self._headers()
        ) as resp:
            data = await resp.json()
            self._check_status(resp.status, data, resp.headers)
            return data

    async def get_claim_calldata(self, epochs):
        epoch_str = ",".join(map(str, epochs))
        async with self.session.get(f"{self.url}/v1/claim-calldata?epochs={epoch_str}") as resp:
            return await resp.json()

    async def get_stake_approve_calldata(self, amount_wei):
        async with self.session.get(
            f"{self.url}/v1/stake-approve-calldata?amount={amount_wei}"
        ) as resp:
            return await resp.json()

    async def get_stake_calldata(self, amount_wei):
        async with self.session.get(
            f"{self.url}/v1/stake-calldata?amount={amount_wei}"
        ) as resp:
            return await resp.json()


async def _offer_auto_stake(bankr, coord):
    """Called on first 403 in drilling_loop — offer interactive staking."""
    tier = DRILLER_TIER
    amount_display = TIER_STAKE_DISPLAY[tier]
    amount_wei = TIER_STAKE_WEI[tier]

    log(f"{'='*50}")
    log(f"⚠️  Stake required: {amount_display} $CRUDE ({tier} tier)")
    log(f"    Auto-stake now? (y/n)")
    log(f"{'='*50}")

    try:
        answer = await asyncio.get_event_loop().run_in_executor(
            None, lambda: input(">>> Stake? (y/n): ").strip().lower()
        )
    except (EOFError, KeyboardInterrupt):
        log("Headless mode — auto-stake unavailable. Stake manually via drillcrude.com", "WARN")
        return False

    if answer not in ("y", "yes"):
        log("Staking cancelled by user.")
        return False

    # Step 1: Approve
    log(f"Approving {amount_display} $CRUDE for staking...")
    try:
        approve_data = await coord.get_stake_approve_calldata(amount_wei)
        tx = approve_data.get("transaction")
        if not tx:
            log(f"Approve calldata error: {approve_data}", "ERROR")
            return False
        result = await bankr.submit_tx(tx, f"Approve {amount_display} CRUDE for staking")
        if not result.get("success"):
            log(f"Approve tx failed: {result}", "ERROR")
            return False
        log(f"✅ Approve OK: {result.get('transactionHash', '?')[:16]}...")
    except Exception as e:
        log(f"Approve failed: {e}", "ERROR")
        return False

    # Step 2: Stake
    log(f"Staking {amount_display} $CRUDE...")
    try:
        stake_data = await coord.get_stake_calldata(amount_wei)
        tx = stake_data.get("transaction")
        if not tx:
            log(f"Stake calldata error: {stake_data}", "ERROR")
            return False
        result = await bankr.submit_tx(tx, f"Stake {amount_display} CRUDE")
        if not result.get("success"):
            log(f"Stake tx failed: {result}", "ERROR")
            if tier != "wildcat":
                log(f"💡 Try DRILLER_TIER=wildcat (only 25M $CRUDE needed)", "WARN")
            return False
        log(f"✅ Staked {amount_display} $CRUDE! Hash: {result.get('transactionHash', '?')[:16]}...")
        await tg_notify(f"⛏ <b>Staked {amount_display} $CRUDE</b> ({tier} tier)")
        return True
    except Exception as e:
        log(f"Stake failed: {e}", "ERROR")
        if tier != "wildcat":
            log(f"💡 Insufficient funds? Try DRILLER_TIER=wildcat (25M)", "WARN")
        return False


# ============ DETERMINISTIC DOCUMENT PARSER (NO LLM) ============
import dataclasses
from typing import Optional, List, Tuple

@dataclasses.dataclass
class CompanyData:
    name: str
    paragraph_idx: int = 0
    employees: Optional[int] = None
    founded: Optional[int] = None
    revenue_millions: Optional[int] = None
    margin: Optional[int] = None
    city: Optional[str] = None
    sector: Optional[str] = None
    # Raw values as they appear in the document (for trace validation)
    employees_raw: Optional[str] = None
    founded_raw: Optional[str] = None
    revenue_raw: Optional[str] = None
    margin_raw: Optional[str] = None

# Regex patterns for data extraction (case-insensitive)
_EMPLOYEES_RE = [
    re.compile(r'([\d,]+)\s+employees', re.I),
    re.compile(r'employs?\s+([\d,]+)', re.I),
    re.compile(r'workforce\s+of\s+([\d,]+)', re.I),
    re.compile(r'(?:team|staff)\s+of\s+([\d,]+)', re.I),
    re.compile(r'([\d,]+)\s+(?:people|workers|staff)\b', re.I),
    re.compile(r'([\d,]+)-(?:person|employee|member)', re.I),
    re.compile(r'headcount\s+of\s+([\d,]+)', re.I),
]

_FOUNDED_RE = [
    re.compile(r'(?:founded|established|incorporated|started|formed|launched|created|organized)\s+in\s+(\d{4})', re.I),
    re.compile(r'since\s+(\d{4})', re.I),
    re.compile(r'dating\s+back\s+to\s+(\d{4})', re.I),
    re.compile(r'origins?\s+in\s+(\d{4})', re.I),
    re.compile(r'in\s+operation\s+since\s+(\d{4})', re.I),
    re.compile(r'opened\s+(?:its\s+doors\s+)?in\s+(\d{4})', re.I),
    re.compile(r'traces?\s+(?:its\s+)?(?:roots?|history)\s+(?:back\s+)?to\s+(\d{4})', re.I),
]

_REVENUE_RE = [
    re.compile(r'\$\s*([\d.]+)\s*[Bb](?:illion)?', re.I),
    re.compile(r'\$\s*([\d.]+)\s*[Mm](?:illion)?', re.I),
    re.compile(r'revenue\s+of\s+\$\s*([\d.]+)\s*([BbMm])', re.I),
    re.compile(r'\$\s*([\d.]+)\s*([BbMm]).*?revenue', re.I),
]

_MARGIN_RE = [
    re.compile(r'operating\s+margin\s+(?:of\s+|at\s+|around\s+|near\s+|approximately\s+)?(\d+(?:\.\d+)?)\s*%', re.I),
    re.compile(r'(\d+(?:\.\d+)?)\s*%\s+operating\s+margin', re.I),
    re.compile(r'margin\s+of\s+(\d+(?:\.\d+)?)\s*%', re.I),
    re.compile(r'(\d+(?:\.\d+)?)\s*%\s*margin', re.I),
]

_CITY_RES = [
    re.compile(r'headquartered\s+in\s+([\w\s]+?)(?:\.|,|\s+Founded|\s+The|\s+It|\s+Since|\s+With)', re.I),
    re.compile(r'(?:based|located|operating)\s+(?:out\s+of\s+|in\s+)([\w\s]+?)(?:\.|,|\s+Founded|\s+The|\s+It|\s+Since|\s+With)', re.I),
    re.compile(r'operations?\s+in\s+([\w\s]+?)(?:\.|,|\s+the\s+company)', re.I),
]
_SECTOR_RES = [
    re.compile(r'is\s+a[n]?\s+([\w\s-]+?)\s+(?:company|firm|operator|provider|enterprise)', re.I),
    re.compile(r'(?:leading|major|prominent|established)\s+([\w\s-]+?)\s+(?:company|firm|operator|provider)', re.I),
    re.compile(r'in\s+the\s+([\w\s-]+?)\s+(?:sector|industry|space|market|segment)', re.I),
]
# Keep backward compat for parse_companies
_CITY_RE = _CITY_RES[0]
_SECTOR_RE = _SECTOR_RES[0]

# Question type classification
_QUESTION_MAP = [
    (re.compile(r'highest.*revenue[- ]+per[- ]+employee', re.I), ('revenue_per_employee', 'max')),
    (re.compile(r'highest.*revenue', re.I),             ('revenue_millions', 'max')),
    (re.compile(r'largest.*revenue', re.I),              ('revenue_millions', 'max')),
    (re.compile(r'greatest.*revenue', re.I),             ('revenue_millions', 'max')),
    (re.compile(r'most.*revenue', re.I),                 ('revenue_millions', 'max')),
    (re.compile(r'lowest.*revenue', re.I),               ('revenue_millions', 'min')),
    (re.compile(r'smallest.*revenue', re.I),             ('revenue_millions', 'min')),
    (re.compile(r'least.*revenue', re.I),                ('revenue_millions', 'min')),
    (re.compile(r'highest.*margin', re.I),               ('margin', 'max')),
    (re.compile(r'largest.*margin', re.I),               ('margin', 'max')),
    (re.compile(r'greatest.*margin', re.I),              ('margin', 'max')),
    (re.compile(r'lowest.*margin', re.I),                ('margin', 'min')),
    (re.compile(r'smallest.*margin', re.I),              ('margin', 'min')),
    (re.compile(r'narrowest.*margin', re.I),             ('margin', 'min')),
    (re.compile(r'most\s+employees', re.I),              ('employees', 'max')),
    (re.compile(r'(?:largest|biggest)\s+(?:work)?(?:force|team|staff)', re.I),  ('employees', 'max')),
    (re.compile(r'(?:largest|biggest)\s+(?:number|count)\s+of\s+employee', re.I),  ('employees', 'max')),
    (re.compile(r'highest.*employee', re.I),             ('employees', 'max')),
    (re.compile(r'fewest\s+employees', re.I),            ('employees', 'min')),
    (re.compile(r'(?:smallest|lowest).*(?:work)?(?:force|team|staff|employee)', re.I), ('employees', 'min')),
    (re.compile(r'founded.*earliest', re.I),             ('founded', 'min')),
    (re.compile(r'oldest', re.I),                        ('founded', 'min')),
    (re.compile(r'longest.*(?:history|operation|business)', re.I), ('founded', 'min')),
    (re.compile(r'founded.*(?:most\s+)?recently', re.I), ('founded', 'max')),
    (re.compile(r'newest', re.I),                        ('founded', 'max')),
    (re.compile(r'youngest', re.I),                      ('founded', 'max')),
    (re.compile(r'most\s+recently\s+(?:founded|established|created)', re.I), ('founded', 'max')),
]


def _extract_int(text, patterns):
    """Try each regex pattern, return first matched integer or None"""
    for pat in patterns:
        m = pat.search(text)
        if m:
            return int(m.group(1).replace(',', ''))
    return None


def _extract_int_raw(text, patterns):
    """Try each regex pattern, return (parsed_int, raw_match_text) or (None, None)"""
    for pat in patterns:
        m = pat.search(text)
        if m:
            return int(m.group(1).replace(',', '')), m.group(1)
    return None, None


def _extract_revenue_millions(text):
    """Extract revenue in millions from paragraph text"""
    for pat in _REVENUE_RE:
        m = pat.search(text)
        if m:
            val = float(m.group(1))
            # Check if it's billions or millions
            full_match = m.group(0).lower()
            if 'b' in full_match.replace('$', '').replace(str(m.group(1)), '', 1)[:5]:
                return round(val * 1000)
            else:
                return round(val)
    return None


def _extract_revenue_raw(text):
    """Extract raw revenue string as it appears in document (e.g. '$8.7B')"""
    for pat in _REVENUE_RE:
        m = pat.search(text)
        if m:
            return m.group(0).strip().rstrip('.,;')
    return None


def _extract_margin(text):
    """Extract operating margin percentage from paragraph text"""
    for pat in _MARGIN_RE:
        m = pat.search(text)
        if m:
            return round(float(m.group(1)))
    return None


def _extract_margin_raw(text):
    """Extract raw margin string as it appears in document"""
    for pat in _MARGIN_RE:
        m = pat.search(text)
        if m:
            return m.group(1) + "%"
    return None


def parse_companies(doc: str, companies: list) -> List[CompanyData]:
    """Parse document into structured company data. No LLM needed."""
    paragraphs = [p.strip() for p in doc.split('\n\n') if p.strip()]
    # Also try single newline split if we get too few paragraphs
    if len(paragraphs) < len(companies) // 2:
        paragraphs = [p.strip() for p in doc.split('\n') if p.strip() and len(p.strip()) > 50]

    # Map each paragraph to its matching company (1 paragraph = 1 company)
    para_to_company = {}
    for i, para in enumerate(paragraphs):
        for company in companies:
            if company in para:
                if i not in para_to_company:
                    para_to_company[i] = company
                break
        else:
            # Try case-insensitive
            for company in companies:
                if company.lower() in para.lower():
                    if i not in para_to_company:
                        para_to_company[i] = company
                    break

    # Reverse map: company -> paragraph index
    company_to_para = {v: k for k, v in para_to_company.items()}

    result = []
    for company in companies:
        if company in company_to_para:
            pi = company_to_para[company]
            para_text = paragraphs[pi]
            city_m = None
            for _cre in _CITY_RES:
                city_m = _cre.search(para_text)
                if city_m:
                    break
            sector_m = None
            for _sre in _SECTOR_RES:
                sector_m = _sre.search(para_text)
                if sector_m:
                    break
            emp_val, emp_raw = _extract_int_raw(para_text, _EMPLOYEES_RE)
            yr_val, yr_raw = _extract_int_raw(para_text, _FOUNDED_RE)
            cd = CompanyData(
                name=company,
                paragraph_idx=pi + 1,  # 1-indexed for trace
                employees=emp_val,
                founded=yr_val,
                revenue_millions=_extract_revenue_millions(para_text),
                margin=_extract_margin(para_text),
                city=city_m.group(1).strip() if city_m else None,
                sector=sector_m.group(1).strip().lower() if sector_m else None,
                employees_raw=emp_raw,
                founded_raw=yr_raw,
                revenue_raw=_extract_revenue_raw(para_text),
                margin_raw=_extract_margin_raw(para_text),
            )
            result.append(cd)
        else:
            result.append(CompanyData(name=company))

    return result


def parse_question(question: str) -> Optional[Tuple[str, str]]:
    """Classify question into (field, direction). Returns None if unknown."""
    for pat, result in _QUESTION_MAP:
        if pat.search(question):
            return result
    return None


def _build_company_data(c):
    """Build extracted_data dict from a CompanyData object."""
    return {
        "Q1_ANSWER": c.name,
        "Q1_EMPLOYEES": str(c.employees) if c.employees is not None else "N/A",
        "Q1_FOUNDED": str(c.founded) if c.founded is not None else "N/A",
        "Q1_REVENUE": f"{c.revenue_millions}M" if c.revenue_millions is not None else "N/A",
        "Q1_MARGIN": str(c.margin) if c.margin is not None else "N/A",
        "employees": str(c.employees) if c.employees is not None else "N/A",
        "founded": str(c.founded) if c.founded is not None else "N/A",
        "revenue": f"{c.revenue_millions}M" if c.revenue_millions is not None else "N/A",
        "margin": str(c.margin) if c.margin is not None else "N/A",
        "_paragraph_idx": c.paragraph_idx,
        # Raw values for trace (as they appear in document)
        "employees_raw": c.employees_raw or (str(c.employees) if c.employees is not None else "N/A"),
        "founded_raw": c.founded_raw or (str(c.founded) if c.founded is not None else "N/A"),
        "revenue_raw": c.revenue_raw or (f"${c.revenue_millions}M" if c.revenue_millions is not None else "N/A"),
        "margin_raw": c.margin_raw or (f"{c.margin}%" if c.margin is not None else "N/A"),
    }


# Pre-compiled question filter regexes (avoid re-compiling every cycle)
_Q_FILTER_CITY_RE = re.compile(r'headquartered\s+in\s+([\w\s]+?)(?:,|\s+which|\s+that|\s+has|\s+had|\s+reported|\s+with|\?)', re.I)
_Q_FILTER_SECTOR_RES = [
    re.compile(r'in\s+the\s+([\w\s-]+?)\s+(?:sector|industry|segment)', re.I),
    re.compile(r'(?:among|of)\s+(?:all\s+)?(?:the\s+)?([\w\s-]+?)\s+(?:companies|firms)', re.I),
    re.compile(r'which\s+([\w\s-]+?)\s+company', re.I),
]
_Q_FILTER_YEAR_BEFORE_RE = re.compile(r'founded\s+(?:before|prior\s+to)\s+(\d{4})', re.I)
_Q_FILTER_YEAR_AFTER_RE = re.compile(r'founded\s+(?:after|since)\s+(\d{4})', re.I)


def deterministic_pass1(doc, questions, companies):
    """
    Pure-Python Pass 1: parse doc, answer question, extract data.
    Returns (company_name, extracted_data_dict, tied_alternates, parsed_companies) or (None, None, [], []) on failure.
    tied_alternates is a list of (company_name, data_dict) for other companies that tied on the question field.
    parsed_companies is the list of ParsedCompany objects for constraint-field tie detection.
    """
    try:
        debug_log("DET_DOC", doc[:2000])  # Log raw document for debugging
        parsed = parse_companies(doc, companies)
        debug_log("DET_PARSED", {c.name: {"emp": c.employees, "yr": c.founded, "rev": c.revenue_millions, "margin": c.margin, "city": c.city, "sector": c.sector, "para_idx": c.paragraph_idx} for c in parsed})

        # Use first question (challenges always have 1)
        q = questions[0] if questions else ""
        qtype = parse_question(q)
        if qtype is None:
            debug_log("DET_QUESTION_UNKNOWN", q)
            return None, None, []

        field, direction = qtype

        # Extract question filters (city, sector, year)
        q_filter_city = None
        q_filter_sector = None
        q_filter_year_before = None
        q_filter_year_after = None

        city_match = _Q_FILTER_CITY_RE.search(q)
        if city_match:
            q_filter_city = city_match.group(1).strip()

        # Sector: "in the X sector" or "Which X company"
        sector_match = None
        for _sfre in _Q_FILTER_SECTOR_RES:
            sector_match = _sfre.search(q)
            if sector_match:
                break
        if sector_match:
            raw_sector = sector_match.group(1).strip().lower()
            for prefix in ["companies in the ", "companies ", "all "]:
                if raw_sector.startswith(prefix):
                    raw_sector = raw_sector[len(prefix):]
            q_filter_sector = raw_sector

        # Year filter: "founded before 1990", "founded after 2000"
        year_match = _Q_FILTER_YEAR_BEFORE_RE.search(q)
        if year_match:
            q_filter_year_before = int(year_match.group(1))
        year_match2 = _Q_FILTER_YEAR_AFTER_RE.search(q)
        if year_match2:
            q_filter_year_after = int(year_match2.group(1))

        debug_log("DET_QUESTION", {"q": q, "field": field, "direction": direction,
                                    "filter_city": q_filter_city, "filter_sector": q_filter_sector,
                                    "filter_year_before": q_filter_year_before, "filter_year_after": q_filter_year_after})

        # Handle computed fields (ratios)
        if field == 'revenue_per_employee':
            valid = [c for c in parsed if c.revenue_millions is not None and c.employees is not None and c.employees > 0]
        else:
            valid = [c for c in parsed if getattr(c, field) is not None]

        # Apply question filters (city, sector)
        if q_filter_city and valid:
            filtered = [c for c in valid if c.city and q_filter_city.lower() in c.city.lower()]
            if filtered:
                debug_log("DET_FILTER_CITY", {"city": q_filter_city, "before": len(valid), "after": len(filtered),
                                               "matched": [c.name for c in filtered]})
                valid = filtered
            else:
                debug_log("DET_FILTER_CITY_MISS", {"city": q_filter_city, "companies_cities": {c.name: c.city for c in valid}})
        if q_filter_sector and valid:
            filtered = [c for c in valid if c.sector and (q_filter_sector in c.sector or c.sector in q_filter_sector)]
            if filtered:
                debug_log("DET_FILTER_SECTOR", {"sector": q_filter_sector, "before": len(valid), "after": len(filtered),
                                                 "matched": [c.name for c in filtered]})
                valid = filtered
            else:
                debug_log("DET_FILTER_SECTOR_MISS", {"sector": q_filter_sector, "companies_sectors": {c.name: c.sector for c in valid}})
        if q_filter_year_before and valid:
            filtered = [c for c in valid if c.founded is not None and c.founded < q_filter_year_before]
            if filtered:
                debug_log("DET_FILTER_YEAR", {"before": q_filter_year_before, "count": len(filtered)})
                valid = filtered
        if q_filter_year_after and valid:
            filtered = [c for c in valid if c.founded is not None and c.founded > q_filter_year_after]
            if filtered:
                debug_log("DET_FILTER_YEAR", {"after": q_filter_year_after, "count": len(filtered)})
                valid = filtered
        if not valid:
            debug_log("DET_NO_VALID", {"field": field, "total": len(parsed)})
            return None, None, []

        # Select winner (use paragraph_idx as tiebreaker — first in doc wins)
        def _get_val(c):
            if field == 'revenue_per_employee':
                return c.revenue_millions * 1_000_000 / c.employees  # revenue in $ / employees
            return getattr(c, field)

        if direction == 'max':
            winner = max(valid, key=lambda c: (_get_val(c), -(c.paragraph_idx or 999)))
        else:
            winner = min(valid, key=lambda c: (_get_val(c), c.paragraph_idx or 999))

        winner_val = _get_val(winner)

        # Find tied candidates (same field value, different company)
        tied = [c for c in valid if _get_val(c) == winner_val and c.name != winner.name]
        # Sort tied candidates by paragraph_idx (opposite order from winner's tiebreaker)
        if direction == 'max':
            tied.sort(key=lambda c: c.paragraph_idx or 999)  # winner used -(para_idx), so alternates are later ones
        else:
            tied.sort(key=lambda c: -(c.paragraph_idx or 999))

        tied_alternates = [(c.name, _build_company_data(c)) for c in tied]

        data = _build_company_data(winner)

        if tied_alternates:
            debug_log("DET_WINNER", {"company": winner.name, "field": field, "value": winner_val, "data": data,
                                     "tied_with": [t[0] for t in tied_alternates]})
        else:
            debug_log("DET_WINNER", {"company": winner.name, "field": field, "value": winner_val, "data": data})
        return winner.name, data, tied_alternates, parsed

    except Exception as e:
        debug_log("DET_ERROR", {"error": str(e)})
        return None, None, [], []


def _detect_constraint_field(constraint_text):
    """Determine which data field a constraint operates on."""
    c = constraint_text.lower()
    if "revenue" in c:
        return "revenue_millions"
    if "employee" in c:
        return "employees"
    if "founding" in c or "founded" in c:
        return "founded"
    if "margin" in c:
        return "margin"
    return None


# ============ LOCAL COMPUTE ENGINE ============
def _parse_revenue_to_millions_decimal(rev_str):
    """Parse revenue string to millions using Decimal (exact): '$8.7B' -> 8700 (not 8699)"""
    if not rev_str or rev_str == "N/A":
        return None
    rev_str = rev_str.replace("$", "").replace(",", "").strip()
    m = re.match(r'([\d.]+)\s*[Bb]', rev_str)
    if m:
        return int(Decimal(m.group(1)) * 1000)
    m = re.match(r'([\d.]+)\s*[Mm]', rev_str)
    if m:
        return int(Decimal(m.group(1)))
    m = re.match(r'(\d+)', rev_str)
    if m:
        return int(m.group(1))
    return None

def _parse_revenue_to_millions(rev_str):
    """Parse revenue string to millions: '$8.5B' -> 8500, '4.4B' -> 4400, '750M' -> 750"""
    if not rev_str or rev_str == "N/A":
        return None
    rev_str = rev_str.replace("$", "").replace(",", "").strip()
    m = re.match(r'([\d.]+)\s*[Bb]', rev_str)
    if m:
        return round(float(m.group(1)) * 1000)
    m = re.match(r'([\d.]+)\s*[Mm]', rev_str)
    if m:
        return round(float(m.group(1)))
    m = re.match(r'(\d+)', rev_str)
    if m:
        return int(m.group(1))
    return None

def _parse_revenue_to_millions_trunc(rev_str):
    """Same as above but with int() truncation instead of round()"""
    if not rev_str or rev_str == "N/A":
        return None
    rev_str = rev_str.replace("$", "").replace(",", "").strip()
    m = re.match(r'([\d.]+)\s*[Bb]', rev_str)
    if m:
        return int(float(m.group(1)) * 1000)
    m = re.match(r'([\d.]+)\s*[Mm]', rev_str)
    if m:
        return int(float(m.group(1)))
    m = re.match(r'(\d+)', rev_str)
    if m:
        return int(m.group(1))
    return None

def _parse_int(s):
    """Parse integer from string, handling commas and N/A"""
    if not s or s == "N/A":
        return None
    s = s.replace(",", "").replace("$", "").replace("%", "").strip()
    m = re.match(r'-?\d+', s)
    return int(m.group()) if m else None

def compute_artifact_locally(company_name, extracted_data, constraint_text):
    """
    Compute artifact deterministically in Python.
    Returns (artifact, method, alt_artifacts) or (None, None, []).
    alt_artifacts is a list of alternative artifact strings (e.g., space variants).
    """
    c = constraint_text.lower()

    try:
        # --- MOD operations ---
        # "employees mod N"
        if "employee" in c and "mod" in c:
            m = re.search(r'mod\s+(\d+)', c)
            if m:
                n = int(m.group(1))
                emp = _parse_int(extracted_data.get("employees") or extracted_data.get("Q1_EMPLOYEES", ""))
                if emp is not None:
                    return str(emp % n), "employees_mod", []

        # "founding_year mod N"
        if "founding" in c and "mod" in c:
            m = re.search(r'mod\s+(\d+)', c)
            if m:
                n = int(m.group(1))
                year = _parse_int(extracted_data.get("founded") or extracted_data.get("Q1_FOUNDED", ""))
                if year is not None:
                    return str(year % n), "founding_mod", []

        # "revenue_millions mod N"
        if "revenue" in c and "mod" in c:
            m = re.search(r'mod\s+(\d+)', c)
            if m:
                n = int(m.group(1))
                rev_str = extracted_data.get("revenue") or extracted_data.get("Q1_REVENUE", "")
                # Primary: Decimal-based (exact arithmetic, no float imprecision)
                rev_dec = _parse_revenue_to_millions_decimal(rev_str)
                if rev_dec is not None:
                    primary = str(rev_dec % n)
                    alts = []
                    seen = {primary}
                    # Alt 1: float round()
                    rev_round = _parse_revenue_to_millions(rev_str)
                    if rev_round is not None:
                        a = str(rev_round % n)
                        if a not in seen:
                            seen.add(a)
                            alts.append(a)
                    # Alt 2: float int() truncation
                    rev_trunc = _parse_revenue_to_millions_trunc(rev_str)
                    if rev_trunc is not None:
                        a = str(rev_trunc % n)
                        if a not in seen:
                            seen.add(a)
                            alts.append(a)
                    # Alt 3: math.ceil on float
                    rev_str_clean = rev_str.replace("$", "").replace(",", "").strip()
                    m2 = re.match(r'([\d.]+)\s*[Bb]', rev_str_clean)
                    if m2:
                        rev_ceil = math.ceil(float(m2.group(1)) * 1000)
                        a = str(rev_ceil % n)
                        if a not in seen:
                            seen.add(a)
                            alts.append(a)
                    return primary, "revenue_mod", alts

        # "margin ... mod N" or "margin × N" or "margin * N"
        if "margin" in c:
            margin_val = _parse_int(extracted_data.get("margin") or extracted_data.get("Q1_MARGIN", ""))
            if margin_val is not None:
                if "mod" in c:
                    m = re.search(r'mod\s+(\d+)', c)
                    if m:
                        return str(margin_val % int(m.group(1))), "margin_mod", []
                m = re.search(r'[×*]\s*(\d+)', constraint_text)  # use original case for × char
                if m:
                    return str(margin_val * int(m.group(1))), "margin_mul", []
                m = re.search(r'margin\s*[–—-]\s*(\d+)', c)
                if m:
                    return str(margin_val - int(m.group(1))), "margin_sub", []
                m = re.search(r'margin\s*\+\s*(\d+)', c)
                if m:
                    return str(margin_val + int(m.group(1))), "margin_add", []

        # --- STRING operations ---
        # "first letter of each whitespace-delimited word"
        if "first letter of each" in c and "word" in c:
            return ''.join(w[0] for w in company_name.split() if w), "first_letters", []

        # "first N characters ... reversed"
        m = re.search(r'first\s+(\d+)\s+characters?.*reversed', c)
        if m:
            n = int(m.group(1))
            alts = []
            seen = set()
            # Primary: strip spaces first, then take first N reversed
            name_ns = company_name.replace(' ', '')
            primary = name_ns[:n][::-1]
            seen.add(primary)
            # Alt 1: with spaces
            alt_ws = company_name[:n][::-1]
            if alt_ws not in seen:
                seen.add(alt_ws)
                alts.append(alt_ws)
            # Alt 2: suffix stripped (remove Ltd, Inc, Co, Corp, etc.) then no-spaces
            name_stripped = re.sub(r'\s+(Ltd|Inc|Co|Corp|LLC|LP|Plc|Group|Partners|Holdings|Services|Systems|Engineering)\.?$', '', company_name, flags=re.IGNORECASE)
            if name_stripped != company_name:
                ns_stripped = name_stripped.replace(' ', '')
                alt_s = ns_stripped[:n][::-1]
                if alt_s not in seen:
                    seen.add(alt_s)
                    alts.append(alt_s)
                # Also with spaces on stripped name
                alt_sw = name_stripped[:n][::-1]
                if alt_sw not in seen:
                    seen.add(alt_sw)
                    alts.append(alt_sw)
            return primary, "first_n_reversed", alts

        # "letters at 1-indexed positions X, Y, Z in the answer company's canonical name"
        m = re.search(r'positions?\s+([\d,\s]+)\s+in', c)
        if not m:
            m = re.search(r'positions?\s+([\d,\s]+)', c)
        if m and "letter" in c:
            positions = [int(p.strip()) for p in m.group(1).split(',') if p.strip()]
            name_ns = company_name.replace(' ', '')
            alts = []
            seen = set()

            def _extract_at_positions(name, pos_list):
                result = []
                for p in pos_list:
                    if 1 <= p <= len(name):
                        result.append(name[p - 1])
                    else:
                        return None
                return ''.join(result) if result else None

            # Detect if constraint explicitly says to strip spaces
            strip_spaces = "strip" in c and "space" in c or "space-free" in c or "spacefree" in c

            if strip_spaces:
                primary = _extract_at_positions(name_ns, positions)
                if primary:
                    seen.add(primary)
                    # Alt: with-spaces
                    alt = _extract_at_positions(company_name, positions)
                    if alt and alt not in seen:
                        seen.add(alt)
                        alts.append(alt)
            else:
                # Primary: WITH spaces (coordinator default for "canonical name")
                primary = _extract_at_positions(company_name, positions)
                if primary:
                    seen.add(primary)
                    # Alt 1: no-spaces
                    alt = _extract_at_positions(name_ns, positions)
                    if alt and alt not in seen:
                        seen.add(alt)
                        alts.append(alt)
                else:
                    # Fallback to no-spaces if with-spaces is out of bounds
                    primary = _extract_at_positions(name_ns, positions)
                    if primary:
                        seen.add(primary)

            # Additional alts: suffix-stripped name variants
            if primary:
                name_stripped = re.sub(r'\s+(Ltd|Inc|Co|Corp|LLC|LP|Plc|Group|Partners|Holdings|Services|Systems|Engineering)\.?$', '', company_name, flags=re.IGNORECASE)
                if name_stripped != company_name:
                    for variant in [name_stripped, name_stripped.replace(' ', '')]:
                        alt = _extract_at_positions(variant, positions)
                        if alt and alt not in seen:
                            seen.add(alt)
                            alts.append(alt)
            if primary:
                return primary, "letter_positions", alts

        # "every Nth letter" (strip spaces, 1-indexed)
        m = re.search(r'every\s+(\d+)(?:th|st|nd|rd)\s+letter', c)
        if m:
            n = int(m.group(1))
            name_ns = company_name.replace(' ', '')
            primary = ''.join(name_ns[i - 1] for i in range(n, len(name_ns) + 1, n))
            alts = []
            # Alt 1: with spaces, 1-indexed
            alt_ws = ''.join(company_name[i - 1] for i in range(n, len(company_name) + 1, n))
            if alt_ws != primary:
                alts.append(alt_ws)
            # Alt 2: no spaces, 0-indexed (positions n-1, 2n-1, 3n-1, ...)
            alt_0 = ''.join(name_ns[i] for i in range(n - 1, len(name_ns), n))
            if alt_0 != primary and alt_0 not in alts:
                alts.append(alt_0)
            # Alt 3: with spaces, 0-indexed
            alt_ws0 = ''.join(company_name[i] for i in range(n - 1, len(company_name), n))
            if alt_ws0 != primary and alt_ws0 not in alts:
                alts.append(alt_ws0)
            return primary, "every_nth", alts

        # "employees × N" or "employees * N"
        if "employee" in c and ("×" in constraint_text or "*" in constraint_text):
            m = re.search(r'[×*]\s*(\d+)', constraint_text)
            if m:
                n = int(m.group(1))
                emp = _parse_int(extracted_data.get("employees") or extracted_data.get("Q1_EMPLOYEES", ""))
                if emp is not None:
                    return str(emp * n), "employees_mul", []

    except Exception as e:
        debug_log("LOCAL_COMPUTE_ERROR", {"error": str(e), "constraint": constraint_text})

    return None, None, []

# ============ LLM SOLVER (SINGLE-PASS + LOCAL COMPUTE) ============
from openai import OpenAI as _OpenAI
try:
    from zai import ZaiClient as _ZaiClient
except ImportError:
    _ZaiClient = None

REASONING_MARKERS = [
    "let me", "first,", "q1:", "answer:", "looking at",
    "the artifact", "step by", "analyze", "document shows",
    "based on", "therefore", "thus", "the answer"
]

def clean_artifact(raw):
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if len(lines) > 2 else lines)
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    return lines[-1] if lines else text

def validate_artifact(artifact):
    lower = artifact.lower()
    return not any(marker in lower for marker in REASONING_MARKERS)

def _extract_llm_content(resp):
    """Extract text content from LLM response, handling reasoning_content fallback for GLM-5"""
    msg = resp.choices[0].message
    content = msg.content or ""
    if content.strip():
        return content

    # GLM-5 / DeepSeek R1 put answer in reasoning_content
    for attr in ['reasoning_content', 'reasoning', 'thought']:
        val = getattr(msg, attr, None)
        if val and val.strip():
            debug_log("REASONING_FALLBACK", {"field": attr, "length": len(val)})
            return val

    # Try model_extra dict (some SDKs put non-standard fields there)
    extra = getattr(msg, 'model_extra', {}) or {}
    for key in ['reasoning_content', 'reasoning', 'thought', 'thinking']:
        if key in extra and extra[key]:
            debug_log("REASONING_FALLBACK_EXTRA", {"field": key, "length": len(str(extra[key]))})
            return str(extra[key])

    return content

class LLMSolver:
    def __init__(self, backend, model, openrouter_key="", zai_key=""):
        self.model = model
        self.backend = backend
        if backend == "zai":
            self.client = _ZaiClient(api_key=zai_key)
        else:
            self.client = _OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=openrouter_key
            )

    def _find_transform_constraint(self, constraints):
        """Find the constraint that describes the transformation to compute"""
        for c in constraints:
            cl = c.lower()
            if any(kw in cl for kw in ["compute the artifact", "output the", "take the answer"]):
                return c
        # Fallback: return the 4th constraint (0-indexed: 3) which is usually the transform
        if len(constraints) >= 4:
            return constraints[3]
        return constraints[-1] if constraints else ""

    def solve(self, doc, questions, constraints, companies):
        """
        Smart solver: deterministic first, LLM fallback.
        Returns (artifact, company, extracted_data, alternates).
        alternates is a list of (artifact, company, extracted_data) tuples to try on rejection.
        """
        debug_log("CHALLENGE_INPUT", {
            "doc_len": len(doc),
            "questions": questions,
            "constraints": constraints,
            "companies": companies
        })

        transform_constraint = self._find_transform_constraint(constraints)

        # === TRY FULLY DETERMINISTIC SOLVE (no LLM, ~0.1ms) ===
        det_company, det_data, tied_alternates, parsed = deterministic_pass1(doc, questions, companies)

        # Add constraint-field tied alternates (companies with same constraint value but different question value)
        if det_company and transform_constraint and parsed:
            constraint_field = _detect_constraint_field(transform_constraint)
            if constraint_field:
                winner_cval = None
                for c in parsed:
                    if c.name == det_company:
                        winner_cval = getattr(c, constraint_field, None)
                        break
                if winner_cval is not None:
                    existing_names = {det_company} | {t[0] for t in tied_alternates}
                    constraint_tied = []
                    for c in parsed:
                        if c.name not in existing_names and getattr(c, constraint_field, None) == winner_cval:
                            constraint_tied.append((c.name, _build_company_data(c)))
                    if constraint_tied:
                        debug_log("CONSTRAINT_TIED", {"field": constraint_field, "value": winner_cval,
                                                       "companies": [t[0] for t in constraint_tied]})
                        tied_alternates.extend(constraint_tied)

        if det_company:
            artifact, method, alt_artifacts = compute_artifact_locally(det_company, det_data, transform_constraint)
            if artifact is not None:
                log(f"⚡ DETERMINISTIC: '{artifact}' via {method} (company: {det_company})")
                debug_log("DETERMINISTIC_OK", {"artifact": artifact, "method": method, "company": det_company, "data": det_data,
                                               "alt_artifacts": alt_artifacts, "tied_companies": [t[0] for t in tied_alternates]})

                # Build alternates list: first space-variant alts, then tied-company alts
                alternates = []
                # Track (artifact, company) pairs to avoid true duplicates
                seen_pairs = {(artifact, det_company)}
                for alt_art in alt_artifacts:
                    if (alt_art, det_company) not in seen_pairs:
                        seen_pairs.add((alt_art, det_company))
                        alternates.append((alt_art, det_company, det_data))
                for alt_name, alt_data in tied_alternates:
                    alt_art, alt_method, alt_art_alts = compute_artifact_locally(alt_name, alt_data, transform_constraint)
                    if alt_art is not None and (alt_art, alt_name) not in seen_pairs:
                        seen_pairs.add((alt_art, alt_name))
                        alternates.append((alt_art, alt_name, alt_data))
                    for aa in alt_art_alts:
                        if (aa, alt_name) not in seen_pairs:
                            seen_pairs.add((aa, alt_name))
                            alternates.append((aa, alt_name, alt_data))

                return artifact, det_company, det_data, alternates
            else:
                debug_log("COMPUTE_MISS", {"company": det_company, "constraint": transform_constraint, "data": {k: v for k, v in det_data.items() if not k.startswith('_')}})
                if not hasattr(self, 'client') or self.client is None:
                    debug_log("NO_LLM", "LLM disabled, cannot fallback")
                    return "", "", {}, []
                validated_answers = {"Q1": det_company}
                result = self._llm_pass2(doc, det_company, det_data, validated_answers, constraints)
                return result[0], result[1], result[2], []
        else:
            debug_log("DET_FALLBACK_LLM", "Deterministic parser failed, using LLM")

        if not hasattr(self, 'client') or self.client is None:
            debug_log("NO_LLM", "LLM disabled, deterministic failed — skipping challenge")
            return "", "", {}, []

        # === LLM FALLBACK PATH ===
        q_text = "\n".join(f"Q{i+1}: {q}" for i, q in enumerate(questions))
        companies_text = "\n".join(f"- {c}" for c in companies)

        # === SINGLE PASS: Answer questions + extract ALL data ===
        pass1_prompt = f"""Read this document carefully.

DOCUMENT:
{doc}

QUESTIONS:
{q_text}

VALID COMPANIES (use EXACT names from this list):
{companies_text}

For each question, identify the answer company from the document. Then extract ALL available numerical data about that company.

Output in this EXACT format (one line per field):
Q1_ANSWER: ExactCompanyName
Q1_EMPLOYEES: <number, no commas>
Q1_FOUNDED: <year>
Q1_REVENUE: <amount, e.g. 8.5B or 750M>
Q1_MARGIN: <operating margin as integer, e.g. 18 for 18%>

Rules:
- Company names must EXACTLY match one from the valid companies list
- Use only numbers explicitly stated in the document
- If a data point is not mentioned, write "N/A"
- No explanations, just the data lines
"""

        try:
            resp1 = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a precise data extraction engine. Read documents and extract exact company names and numerical data. Output only the requested format, no explanations."},
                    {"role": "user", "content": pass1_prompt}
                ],
                max_tokens=1024,
                temperature=0.1
            )
            pass1_content = _extract_llm_content(resp1)
            debug_log("PASS1_RESPONSE", pass1_content)
        except Exception as e:
            log(f"Pass 1 solver error: {e}", "ERROR")
            return "", "", {}, []

        # Parse pass 1
        answers = {}
        extracted_data = {}
        for line in pass1_content.strip().split('\n'):
            line = line.strip()
            if not line or ':' not in line:
                continue
            key, _, val = line.partition(':')
            key = key.strip().upper()
            val = val.strip()
            if key.endswith("_ANSWER"):
                qi = key.replace("_ANSWER", "")
                answers[qi] = val
            elif "_" in key:
                # Store both with prefix (Q1_EMPLOYEES) and without (employees)
                extracted_data[key] = val
                field = key.split("_", 1)[1].lower() if "_" in key else key.lower()
                extracted_data[field] = val

        # Pre-validate company names
        companies_lower = {c.lower(): c for c in companies}
        validated_answers = {}
        for k, v in answers.items():
            if v.lower() in companies_lower:
                validated_answers[k] = companies_lower[v.lower()]
            else:
                for cl, co in companies_lower.items():
                    if cl in v.lower() or v.lower() in cl:
                        validated_answers[k] = co
                        break
                else:
                    log(f"Pre-validation: '{v}' not in companies list for {k}", "WARN")
                    validated_answers[k] = v

        if not validated_answers:
            log("Pass 1 produced no valid answers", "WARN")
            debug_log("PASS1_PARSE_FAIL", pass1_content)
            return "", "", {}, []

        company_counts = {}
        for v in validated_answers.values():
            company_counts[v] = company_counts.get(v, 0) + 1
        primary_company = max(company_counts, key=company_counts.get) if company_counts else ""

        # === TRY LOCAL COMPUTE FIRST (deterministic, 100% accurate) ===
        artifact, method, alt_artifacts = compute_artifact_locally(primary_company, extracted_data, transform_constraint)

        if artifact is not None:
            log(f"Local compute: '{artifact}' via {method}")
            debug_log("LOCAL_COMPUTE_OK", {"artifact": artifact, "method": method, "company": primary_company, "data": extracted_data})
            alternates = [(a, primary_company, extracted_data) for a in alt_artifacts]
            return artifact, primary_company, extracted_data, alternates

        # === FALLBACK: Pass 2 (LLM computes) ===
        result = self._llm_pass2(doc, primary_company, extracted_data, validated_answers, constraints)
        return result[0], result[1], result[2], []

    def _llm_pass2(self, doc, primary_company, extracted_data, validated_answers, constraints):
        """LLM Pass 2: compute artifact when local compute can't handle the constraint."""
        debug_log("LLM_PASS2", "Local compute missed, using LLM")
        answers_text = "\n".join(f"{k}: {v}" for k, v in validated_answers.items())
        data_text = "\n".join(f"{k}: {v}" for k, v in extracted_data.items())
        constraints_text = "\n".join(f"- {c}" for c in constraints)

        pass2_prompt = f"""Compute the artifact value. You have all the information needed.

DOCUMENT (for reference):
{doc}

ANSWER COMPANY: {primary_company}

EXTRACTED DATA:
{data_text}

QUESTION ANSWERS:
{answers_text}

CONSTRAINTS:
{constraints_text}

INSTRUCTIONS:
- Read the constraints carefully. They tell you EXACTLY what to compute.
- "employees mod N" means: take the employee count number, compute remainder when divided by N
- "letters at positions X, Y, Z" means: take the company name as a string, extract the characters at those 1-indexed positions, concatenate them
- "founding_year mod N" means: take the year number, compute remainder when divided by N
- "first letter of each word" means: take first character of each word in the company name, concatenate

Show your work, then output the final answer after ###ARTIFACT###

Example:
Company: "Test Corp Inc", employees: 1500, constraint: "employees mod 7"
Work: 1500 / 7 = 214 remainder 2
###ARTIFACT###
2"""

        try:
            resp2 = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a precise computation engine. Follow instructions exactly. Show your work, then output the final answer after ###ARTIFACT###."},
                    {"role": "user", "content": pass2_prompt}
                ],
                max_tokens=512,
                temperature=0.1
            )
            pass2_content = _extract_llm_content(resp2)
            debug_log("PASS2_RESPONSE", pass2_content)
        except Exception as e:
            log(f"Pass 2 solver error: {e}", "ERROR")
            return "", primary_company, extracted_data

        # Extract artifact after marker
        artifact = ""
        if "###ARTIFACT###" in pass2_content:
            after = pass2_content.split("###ARTIFACT###", 1)[1].strip()
            artifact = clean_artifact(after)
        else:
            artifact = clean_artifact(pass2_content)

        debug_log("FINAL_RESULT", {"artifact": artifact, "company": primary_company})
        return artifact, primary_company, extracted_data

# ============ SITE PICKER ============
def _site_ev_score(site, featured_region=None):
    """Calculate expected credit score for a site (higher = better).
    Considers richness multiplier, featured basin bonus, and depletion."""
    # Richness category → approximate multiplier midpoint
    richness_map = {"bonanza": 6.0, "rich": 3.5, "standard": 1.0}
    r = site.get("richness") or site.get("reserveEstimate", "standard")
    richness_mult = richness_map.get(str(r).lower(), 1.0)

    # If API provides numeric richnessMultiplier, use it (more precise)
    if site.get("richnessMultiplier"):
        try:
            richness_mult = float(site["richnessMultiplier"])
        except (ValueError, TypeError):
            pass

    # Base credits × richness (tier-dependent)
    ev = float(TIER_CREDITS.get(DRILLER_TIER, 2)) * richness_mult

    # Featured basin bonus (+1 credit)
    if featured_region and site.get("region", "").lower() == featured_region.lower():
        ev += 1.0

    # Slight penalty for high depletion (might deplete mid-drill)
    depl = site.get("depletionPct", 0)
    if depl > 90:
        ev *= 0.9  # small penalty, still worth it if rich

    return ev

def pick_best_site(sites, tier="wildcat", featured_region=None, min_richness=None):
    """Pick best site by expected credit value.
    min_richness: if set ('rich'/'bonanza'), skip standard sites."""
    if tier == "wildcat":
        allowed = ["shallow"]
    elif tier == "platform":
        allowed = ["shallow", "medium"]
    else:
        allowed = ["shallow", "medium", "deep"]

    valid = [s for s in sites
             if s.get("estimatedDepth") in allowed
             and s.get("depletionPct", 100) < 100]

    # Filter by minimum richness if requested
    if min_richness:
        richness_rank = {"bonanza": 0, "rich": 1, "standard": 2}
        min_rank = richness_rank.get(min_richness, 2)
        filtered = [s for s in valid
                    if richness_rank.get(str(s.get("richness") or s.get("reserveEstimate", "standard")).lower(), 2) <= min_rank]
        if filtered:
            valid = filtered

    if not valid:
        return None

    # Sort by EV score (descending), then by depletion (ascending)
    valid.sort(key=lambda s: (-_site_ev_score(s, featured_region), s.get("depletionPct", 0)))
    return valid[0]


def _count_sites_by_richness(sites, tier="platform"):
    """Count available sites by richness category."""
    allowed = {"wildcat": ["shallow"], "platform": ["shallow", "medium"],
               "deepwater": ["shallow", "medium", "deep"]}.get(tier, ["shallow", "medium"])
    counts = {"bonanza": 0, "rich": 0, "standard": 0}
    for s in sites:
        if s.get("estimatedDepth") in allowed and s.get("depletionPct", 100) < 100:
            r = str(s.get("richness") or s.get("reserveEstimate", "standard")).lower()
            if r in counts:
                counts[r] += 1
    return counts

# ============ BACKGROUND RECEIPT POSTER ============
_pending_receipts = asyncio.Queue()
_receipt_cooldown = int(os.getenv("RECEIPT_COOLDOWN", "30"))  # seconds between receipt tx (saves gas)

async def post_receipt_inline(bankr, tx, desc="CRUDE drilling receipt"):
    """Post receipt on-chain and wait for confirmation — unlocks next drill"""
    for attempt in range(3):
        try:
            tx_result = await bankr.submit_tx(tx, desc)
            if tx_result.get("success"):
                log(f"Receipt posted: {tx_result.get('transactionHash', '?')[:16]}...")
                return True
            else:
                err = tx_result.get("error", "?")
                if "in-flight" in str(err).lower():
                    debug_log("RECEIPT_INFLIGHT", f"In-flight limit, retrying in 5s (attempt {attempt+1})")
                    await asyncio.sleep(5)
                    continue
                debug_log("RECEIPT_FAIL", err)
                return False
        except Exception as e:
            if attempt < 2:
                await asyncio.sleep(3)
            else:
                debug_log("RECEIPT_DROP", str(e))
                return False
    return False

# ============ MAIN LOOPS ============
async def drilling_loop(bankr, coord, solver):
    """Drilling loop: drill → solve → submit → receipt (inline) → repeat"""
    log("Drilling loop started (inline receipts, ~4s/cycle)")
    retry_attempt = 0
    drill_delay = DRILL_DELAY_INIT  # adaptive delay
    sites_logged = False
    _cached_sites = None
    _sites_ts = 0
    _featured_region = None  # featured basin from drill response

    while True:
        try:
            # Await any pending receipt task from previous cycle
            receipt_task = getattr(state, '_receipt_task', None)
            if receipt_task and not receipt_task.done():
                try:
                    await receipt_task
                except Exception as e:
                    debug_log("RECEIPT_TASK_ERR", str(e))
                state._receipt_task = None

            # Ensure auth (with expiry check)
            await coord.ensure_auth()

            # Refresh sites every 15s (catch new rich/bonanza fast)
            now = time.time()
            if _cached_sites is None or now - _sites_ts > 15:
                sites_data = await coord.get_sites()
                _cached_sites = sites_data.get("sites", [])
                epoch_id = sites_data.get("epochId")
                _sites_ts = now

                if _cached_sites:
                    counts = _count_sites_by_richness(_cached_sites, DRILLER_TIER)
                    summary = [{"region": s.get("region", "?"), "depth": s.get("estimatedDepth", "?"),
                                "richness": s.get("richness") or s.get("reserveEstimate", "?"),
                                "depletion": s.get("depletionPct", "?")} for s in _cached_sites]
                    debug_log("SITES_AVAILABLE", {"counts": counts, "sites": summary})
                    if not sites_logged:
                        debug_log("SITE_STRUCTURE", _cached_sites[0])
                        sites_logged = True

            # Pick best site — maximize expected credits
            site = pick_best_site(_cached_sites, tier=DRILLER_TIER, featured_region=_featured_region)
            if not site:
                log("No valid sites, waiting 30s...")
                _cached_sites = None  # force refresh next time
                await asyncio.sleep(30)
                continue

            # Use whatever best site is available now — no waiting
            site_richness = str(site.get("richness") or site.get("reserveEstimate", "standard")).lower()

            site_id = site["siteId"]
            depth = site.get("estimatedDepth", "?")
            richness = site.get("richness") or site.get("reserveEstimate", "?")
            ev = _site_ev_score(site, _featured_region)
            log(f"Site: {site.get('region', '?')} ({depth}/{richness}, {site.get('depletionPct', '?')}%) EV={ev:.0f}")

            # Small delay before drill (cooldown handled by 429 handler)
            await asyncio.sleep(1)

            # Get challenge
            nonce = secrets.token_hex(16)
            try:
                challenge = await coord.drill(site_id, nonce)
            except AuthError:
                log("401 on drill, re-authing...", "WARN")
                await coord.ensure_auth()
                challenge = await coord.drill(site_id, nonce)
            except StaleError as e:
                err_msg = str(e).lower()
                if "unknown site" in err_msg or "not found" in err_msg:
                    log(f"Site gone (epoch ended?): {e} — refreshing sites...", "WARN")
                    _cached_sites = None  # force site refresh
                    await asyncio.sleep(5)
                    continue
                else:
                    log(f"Stale error on drill: {e}", "WARN")
                    await asyncio.sleep(3)
                    continue
            except ForbiddenError as e:
                log(f"403 Forbidden: {e}", "ERROR")
                if not getattr(state, '_stake_offered', False):
                    state._stake_offered = True
                    staked = await _offer_auto_stake(bankr, coord)
                    if staked:
                        log("Stake successful — resuming drilling...")
                        continue
                log("Check stake level. Waiting 5 min...", "WARN")
                await asyncio.sleep(300)
                continue
            except RateLimitError as e:
                # Parse exact wait time from coordinator: "Drill cooldown active — wait 26s"
                msg = str(e)
                m = re.search(r'wait\s+(\d+)s', msg)
                if m:
                    wait = int(m.group(1)) + 1
                    # Use cooldown time to fetch receipt for pending lot
                    pending_lot = getattr(state, '_pending_lot', None)
                    if pending_lot:
                        log(f"Cooldown {wait-1}s — polling receipt for {pending_lot[:20]}...")
                        state._pending_lot = None
                        receipt_posted = False
                        for poll in range(wait // 3):
                            await asyncio.sleep(3)
                            try:
                                async with coord.session.get(
                                    f"{coord.url}/v1/receipt-calldata?crudeLotId={pending_lot}&miner={coord.driller}",
                                    headers=coord._headers()
                                ) as rresp:
                                    rdata = await rresp.json()
                                    if rresp.status == 200:
                                        rtx = rdata.get("transaction", {})
                                        if rtx:
                                            await post_receipt_inline(bankr, rtx)
                                            receipt_posted = True
                                        break
                                    elif rresp.status != 409:
                                        debug_log("RECEIPT_ERR", f"{rresp.status}: {rdata}")
                                        break
                            except Exception as ex:
                                debug_log("RECEIPT_POLL_ERR", str(ex))
                                break
                        if not receipt_posted:
                            # Wait remaining cooldown
                            remaining = max(0, wait - (poll + 1) * 3)
                            if remaining > 0:
                                await asyncio.sleep(remaining)
                    else:
                        log(f"Drill cooldown {wait-1}s, waiting {wait}s")
                        await asyncio.sleep(wait)
                else:
                    retry_after = getattr(e, 'retry_after', None)
                    wait = retry_after or min(2.0 * (2 ** retry_attempt), 60.0)
                    log(f"429 on drill: '{e}' waiting {wait:.0f}s", "WARN")
                    await asyncio.sleep(wait)
                retry_attempt += 1
                continue
            except ServerError as e:
                log(f"Server error on drill: {e}", "WARN")
                await backoff_sleep(retry_attempt)
                retry_attempt += 1
                continue

            if "error" in challenge:
                err = challenge.get("error", "unknown")
                err_lower = str(err).lower()
                if "depleted" in err_lower:
                    debug_log("SITE_DEPLETED", site.get('region', '?'))
                    _cached_sites = None
                    await asyncio.sleep(3)
                    continue
                elif "unavailable" in err_lower:
                    debug_log("SITE_UNAVAILABLE", site.get('region', '?'))
                    _cached_sites = None
                    await asyncio.sleep(3)
                    continue
                elif "rate limit" in err_lower:
                    log("Coordinator RPC rate limited — waiting 45s...", "WARN")
                    await asyncio.sleep(45)
                    continue
                log(f"Drill error: {err}", "WARN")
                if "active drill" in err_lower:
                    cid = challenge.get("challengeId", "")
                    has_doc = bool(challenge.get("doc"))
                    debug_log("STALE_DRILL", {"challengeId": cid, "has_doc": has_doc, "keys": list(challenge.keys())})

                    if cid and has_doc:
                        # Stale drill has full challenge data — solve it properly!
                        log("Stale drill found, solving it...", "WARN")
                        try:
                            stale_artifact, stale_company, stale_extracted, stale_alts = solver.solve(
                                challenge.get("doc", ""),
                                challenge.get("questions", []),
                                challenge.get("constraints", []),
                                challenge.get("companies", [])
                            )
                            if stale_artifact and stale_company and validate_artifact(stale_artifact):
                                stale_para = stale_extracted.get("_paragraph_idx", 1)
                                stale_trace = [
                                    {"type": "locate_entity", "entity": stale_company, "paragraph": stale_para},
                                    {"type": "extract_value", "paragraph": stale_para, "entity": stale_company, "field": "employees", "value": str(stale_extracted.get("employees_raw", stale_extracted.get("employees", "0")))},
                                    {"type": "apply_constraint", "description": "solve stale drill", "operation": "compute", "result": stale_artifact}
                                ]
                                stale_result = await coord.submit(cid, stale_artifact, nonce, site_id, stale_trace)
                                if stale_result.get("status") == "accepted":
                                    credits = stale_result.get("refinedCredits", 1)
                                    state.total_solves += 1
                                    state.total_credits += credits
                                    log(f"Stale drill solved! +{credits} credits (total: {state.total_credits})")
                                    tx = stale_result.get("transaction", {})
                                    if tx:
                                        await post_receipt_inline(bankr, tx)
                                    state.save()
                                else:
                                    debug_log("STALE_REJECTED", stale_result.get("reason", "?"))
                                continue  # drill closed, move on
                            else:
                                # Can't solve — submit dummy to close it
                                dummy_trace = [{"type": "locate_entity", "entity": "unknown", "paragraph": 1}, {"type": "extract_value", "entity": "unknown", "field": "employees", "value": "0", "paragraph": 1}, {"type": "apply_constraint", "description": "close stale", "operation": "none", "result": "0"}]
                                await coord.submit(cid, "0", nonce, site_id, dummy_trace)
                                continue
                        except Exception as e:
                            debug_log("STALE_SOLVE_ERR", str(e))
                    elif cid:
                        # Has challengeId but no doc — just submit dummy to close
                        try:
                            dummy_trace = [{"type": "locate_entity", "entity": "unknown", "paragraph": 1}, {"type": "extract_value", "entity": "unknown", "field": "employees", "value": "0", "paragraph": 1}, {"type": "apply_constraint", "description": "close stale", "operation": "none", "result": "0"}]
                            await coord.submit(cid, "0", nonce, site_id, dummy_trace)
                        except Exception:
                            pass
                        await asyncio.sleep(2)
                    else:
                        # No challengeId — coordinator won't give us the drill data
                        # Wait for drill to expire on coordinator side (usually 2-5 min)
                        no_cid_attempts = getattr(state, '_no_cid_attempts', 0) + 1
                        state._no_cid_attempts = no_cid_attempts
                        if no_cid_attempts == 1:
                            log("Stale drill (no challengeId), waiting for expiry...", "WARN")
                        wait_secs = min(15 * no_cid_attempts, 60)  # 15, 30, 45, 60 max
                        debug_log("STALE_NO_CID", f"attempt {no_cid_attempts}, waiting {wait_secs}s")
                        try:
                            coord.reset_auth()
                            await coord.ensure_auth()
                        except Exception:
                            pass
                        await asyncio.sleep(wait_secs)
                        if no_cid_attempts >= 8:
                            log("Stale drill won't clear after 8 attempts, continuing anyway...", "WARN")
                            state._no_cid_attempts = 0
                else:
                    await asyncio.sleep(5)
                continue

            # Reset retry counters on successful drill
            retry_attempt = 0
            state._no_cid_attempts = 0

            # Track epoch
            challenge_epoch = challenge.get("epochId") or epoch_id
            if challenge_epoch:
                state.drilled_epochs.add(challenge_epoch)

            # Parse featured basin, missions, streak (Epoch 4+)
            fr = challenge.get("featuredRegion")
            if fr and fr != _featured_region:
                _featured_region = fr
                log(f"⭐ Featured basin: {fr} (+{challenge.get('featuredRegionBonusCredits', 1)} bonus)")
            missions = challenge.get("missions")
            if missions:
                debug_log("MISSIONS", missions)
            streak = challenge.get("streak")
            if streak:
                debug_log("STREAK", streak)

            # Solve
            artifact, company, extracted, alternates = solver.solve(
                challenge.get("doc", ""),
                challenge.get("questions", []),
                challenge.get("constraints", []),
                challenge.get("companies", [])
            )

            # Helper: close drill with dummy submit so it doesn't stay "active"
            async def _close_drill(reason):
                try:
                    cid = challenge.get("challengeId", "")
                    if cid:
                        dummy_trace = [{"type": "locate_entity", "entity": "unknown", "paragraph": 1}, {"type": "extract_value", "entity": "unknown", "field": "employees", "value": "0", "paragraph": 1}, {"type": "apply_constraint", "description": reason, "operation": "none", "result": "0"}]
                        await coord.submit(cid, "0", nonce, site_id, dummy_trace)
                except Exception:
                    pass

            # Skip if artifact is empty
            if not artifact:
                debug_log("EMPTY_ARTIFACT", "Solver returned empty")
                state.total_failures += 1
                state.save()
                await _close_drill("empty artifact")
                continue

            # Validate artifact
            if not validate_artifact(artifact):
                debug_log("REASONING_MARKERS", artifact[:120])
                state.total_failures += 1
                state.save()
                await _close_drill("invalid artifact")
                continue

            # Pre-validate company exists in list
            companies_list = challenge.get("companies", [])
            if company and companies_list:
                companies_lower = {c.lower() for c in companies_list}
                if company.lower() not in companies_lower:
                    debug_log("COMPANY_MISMATCH", {"company": company, "valid": list(companies_list)})
                    state.total_failures += 1
                    state.save()
                    await _close_drill("company mismatch")
                    continue

            if alternates:
                log(f"Solution: '{artifact}' (company: {company}) [+{len(alternates)} alts]")
            else:
                log(f"Solution: '{artifact}' (company: {company})")

            # Build trace
            company_para = extracted.get("_paragraph_idx", 0)
            if not company_para:
                doc_text = challenge.get("doc", "")
                paragraphs = [p.strip() for p in doc_text.split("\n\n") if p.strip()]
                company_para = 1
                for i, para in enumerate(paragraphs, 1):
                    if company in para:
                        company_para = i
                        break

            # Use raw values (as in document) for trace validation
            employees_raw = extracted.get("employees_raw") or extracted.get("employees") or extracted.get("Q1_EMPLOYEES", "N/A")
            founded_raw = extracted.get("founded_raw") or extracted.get("founded") or extracted.get("Q1_FOUNDED", "N/A")
            revenue_raw = extracted.get("revenue_raw") or extracted.get("revenue") or extracted.get("Q1_REVENUE", "N/A")
            margin_raw = extracted.get("margin_raw") or extracted.get("margin") or extracted.get("Q1_MARGIN", "N/A")
            trace = [
                {"type": "locate_entity", "entity": company, "paragraph": company_para},
                {"type": "extract_value", "paragraph": company_para, "entity": company, "field": "employees", "value": str(employees_raw)},
                {"type": "extract_value", "paragraph": company_para, "entity": company, "field": "founded", "value": str(founded_raw)},
                {"type": "extract_value", "paragraph": company_para, "entity": company, "field": "revenue", "value": str(revenue_raw)},
                {"type": "extract_value", "paragraph": company_para, "entity": company, "field": "margin", "value": str(margin_raw)},
                {"type": "apply_constraint", "description": f"Applied constraint transformation to {company}", "operation": "compute", "result": artifact}
            ]

            # Submit
            try:
                result = await coord.submit(
                    challenge.get("challengeId"),
                    artifact,
                    nonce,
                    site_id,
                    trace
                )
            except AuthError:
                log("401 on submit, re-authing and retrying...", "WARN")
                await coord.ensure_auth()
                result = await coord.submit(challenge.get("challengeId"), artifact, nonce, site_id, trace)
            except StaleError:
                log("404 stale challenge, fetching new one", "WARN")
                continue
            except RateLimitError as e:
                wait = getattr(e, 'retry_after', None) or min(2.0 * (2 ** retry_attempt), 30.0)
                log(f"429 rate limited on submit, waiting {wait:.1f}s", "WARN")
                await asyncio.sleep(wait)
                retry_attempt += 1
                continue
            except ServerError as e:
                log(f"Server error on submit: {e}", "WARN")
                await backoff_sleep(retry_attempt)
                retry_attempt += 1
                continue

            if result.get("status") == "accepted":
                credits = result.get("refinedCredits", 1)
                state.total_solves += 1
                state.total_credits += credits
                state.consecutive_failures = 0
                state.record_site(depth, richness, True)

                # Build accept message with bonuses
                bonus_parts = []
                if result.get("gusher"):
                    state.gushers += 1
                    bonus_parts.append(f"🎉 {result['gusher']}")
                breakdown = result.get("bonusBreakdown", {})
                if breakdown.get("featuredBasinBonus"):
                    bonus_parts.append(f"⭐+{breakdown['featuredBasinBonus']}")
                if breakdown.get("missionBonus"):
                    bonus_parts.append(f"🎯+{breakdown['missionBonus']}")
                if breakdown.get("streakBonus"):
                    bonus_parts.append(f"🔥streak+{breakdown['streakBonus']}")
                if breakdown.get("depletionBonus"):
                    bonus_parts.append(f"💥depletion+{breakdown['depletionBonus']}")
                # Black Gold events
                if result.get("blowout"):
                    burn_amt = result.get("blowoutBurnAmount", "?")
                    bonus_parts.append(f"🔥BLOWOUT({burn_amt} burned)")
                    if str(burn_amt) not in ("0", "0.0", "?"):
                        asyncio.create_task(tg_notify(
                            f"🔥 <b>BLOWOUT!</b> {burn_amt} burned\n"
                            f"Credits OK. Total: {state.total_credits}"
                        ))
                jackpot = result.get("jackpot", {})
                if jackpot.get("triggered"):
                    jp_credits = jackpot.get("bonusCredits", 0)
                    jp_reserve = jackpot.get("reserveAmount", "?")
                    bonus_parts.append(f"💎JACKPOT+{jp_credits}")
                    log(f"💎💎💎 JACKPOT ERUPTION! +{jp_credits} bonus credits! Reserve: {jp_reserve}")
                    asyncio.create_task(tg_notify(
                        f"💎 <b>JACKPOT ERUPTION!</b>\n"
                        f"+{jp_credits} bonus credits! Reserve: {jp_reserve}"
                    ))
                bonus_str = f" ({', '.join(bonus_parts)})" if bonus_parts else ""
                log(f"✅ ACCEPTED! +{credits} credits{bonus_str} (total: {state.total_credits})")

                # Post receipt as background task — cooldown is server-side and already ticking
                tx = result.get("transaction", {})
                if tx:
                    state._receipt_task = asyncio.create_task(post_receipt_inline(bankr, tx))
                else:
                    lot_id = result.get("crudeLotId")
                    log(f"No tx, crudeLotId={lot_id}, full={result}")
                    if lot_id:
                        state._pending_lot = lot_id

                state.save()
            else:
                reason = result.get("reason", str(result))
                log(f"❌ REJECTED: {reason} (artifact: '{artifact}', company: {company})")
                debug_log("REJECTION", {"reason": reason, "artifact": artifact, "company": company, "full_result": result})

                # === RETRY WITH ALTERNATES ===
                retry_accepted = False
                if alternates:
                    debug_log("RETRY_ALTS", f"Primary rejected, trying {len(alternates)} alternate(s)")
                    for alt_artifact, alt_company, alt_extracted in alternates:
                        if not alt_artifact or not alt_company or not validate_artifact(alt_artifact):
                            debug_log("ALT_SKIP", {"artifact": alt_artifact, "company": alt_company, "valid": validate_artifact(alt_artifact) if alt_artifact else False})
                            continue
                        debug_log("ALT_TRYING", {"artifact": alt_artifact, "company": alt_company})
                        # Build trace for alternate
                        alt_para = alt_extracted.get("_paragraph_idx", 0)
                        if not alt_para:
                            doc_text = challenge.get("doc", "")
                            alt_paras = [p.strip() for p in doc_text.split("\n\n") if p.strip()]
                            alt_para = 1
                            for i, para in enumerate(alt_paras, 1):
                                if alt_company in para:
                                    alt_para = i
                                    break
                        alt_trace = [
                            {"type": "locate_entity", "entity": alt_company, "paragraph": alt_para},
                            {"type": "extract_value", "paragraph": alt_para, "entity": alt_company, "field": "employees", "value": str(alt_extracted.get("employees_raw", alt_extracted.get("employees", "N/A")))},
                            {"type": "extract_value", "paragraph": alt_para, "entity": alt_company, "field": "founded", "value": str(alt_extracted.get("founded_raw", alt_extracted.get("founded", "N/A")))},
                            {"type": "extract_value", "paragraph": alt_para, "entity": alt_company, "field": "revenue", "value": str(alt_extracted.get("revenue_raw", alt_extracted.get("revenue", "N/A")))},
                            {"type": "extract_value", "paragraph": alt_para, "entity": alt_company, "field": "margin", "value": str(alt_extracted.get("margin_raw", alt_extracted.get("margin", "N/A")))},
                            {"type": "apply_constraint", "description": f"Applied constraint transformation to {alt_company}", "operation": "compute", "result": alt_artifact}
                        ]
                        try:
                            alt_result = await coord.submit(
                                challenge.get("challengeId"),
                                alt_artifact,
                                nonce,
                                site_id,
                                alt_trace
                            )
                        except (StaleError, RateLimitError, ServerError) as e:
                            debug_log("ALT_EXCEPTION", {"error": str(e), "type": type(e).__name__})
                            break
                        except AuthError:
                            await coord.ensure_auth()
                            try:
                                alt_result = await coord.submit(challenge.get("challengeId"), alt_artifact, nonce, site_id, alt_trace)
                            except Exception:
                                break

                        if alt_result.get("status") == "accepted":
                            credits = alt_result.get("refinedCredits", 1)
                            state.total_solves += 1
                            state.total_credits += credits
                            state.consecutive_failures = 0
                            state.record_site(depth, richness, True)
                            alt_bonus = []
                            if alt_result.get("gusher"):
                                state.gushers += 1
                                alt_bonus.append(f"🎉 {alt_result['gusher']}")
                            alt_bd = alt_result.get("bonusBreakdown", {})
                            if alt_bd.get("featuredBasinBonus"):
                                alt_bonus.append(f"⭐+{alt_bd['featuredBasinBonus']}")
                            if alt_bd.get("missionBonus"):
                                alt_bonus.append(f"🎯+{alt_bd['missionBonus']}")
                            if alt_bd.get("streakBonus"):
                                alt_bonus.append(f"🔥streak+{alt_bd['streakBonus']}")
                            if alt_bd.get("depletionBonus"):
                                alt_bonus.append(f"💥depletion+{alt_bd['depletionBonus']}")
                            if alt_result.get("blowout"):
                                alt_burn = alt_result.get('blowoutBurnAmount', '?')
                                alt_bonus.append(f"🔥BLOWOUT({alt_burn} burned)")
                                if str(alt_burn) not in ("0", "0.0", "?"):
                                    asyncio.create_task(tg_notify(
                                        f"🔥 <b>BLOWOUT!</b> {alt_burn} burned"
                                    ))
                            alt_jp = alt_result.get("jackpot", {})
                            if alt_jp.get("triggered"):
                                alt_bonus.append(f"💎JACKPOT+{alt_jp.get('bonusCredits', 0)}")
                                log(f"💎💎💎 JACKPOT ERUPTION! +{alt_jp.get('bonusCredits', 0)} bonus credits!")
                                asyncio.create_task(tg_notify(
                                    f"💎 <b>JACKPOT!</b> +{alt_jp.get('bonusCredits', 0)} bonus credits!"
                                ))
                            alt_bonus_str = f" ({', '.join(alt_bonus)})" if alt_bonus else ""
                            log(f"✅ ALT ACCEPTED! '{alt_artifact}' (company: {alt_company}) +{credits} credits{alt_bonus_str} (total: {state.total_credits})")
                            tx = alt_result.get("transaction", {})
                            if tx:
                                state._receipt_task = asyncio.create_task(post_receipt_inline(bankr, tx))
                            state.save()
                            retry_accepted = True
                            break
                        else:
                            debug_log("ALT_REJECTION", {"artifact": alt_artifact, "company": alt_company,
                                                        "reason": alt_result.get("reason", "?")})

                if not retry_accepted:
                    state.total_failures += 1
                    state.consecutive_failures += 1
                    state.record_site(depth, richness, False)
                    debug_log("REJECTED", {"reason": reason, "alts_tried": len(alternates) if alternates else 0})
                    state.save()

                    # Telegram: alert on 5 consecutive failures (once)
                    if state.consecutive_failures == 5:
                        asyncio.create_task(tg_notify(
                            f"⚠️ <b>5 rejects подряд!</b>\n{reason[:100]}"
                        ))

                    # Circuit breaker
                    if state.consecutive_failures >= 10:
                        log(f"10 consecutive failures! Pausing 30s...", "WARN")
                        asyncio.create_task(tg_notify(f"🔴 <b>10 rejects подряд!</b> Пауза 30s"))
                        await asyncio.sleep(30)
                        state.consecutive_failures = 0

        except (AuthError, ForbiddenError, StaleError, RateLimitError, ServerError):
            raise
        except Exception as e:
            log(f"Drilling error: {e}", "ERROR")
            asyncio.create_task(tg_error(f"🔴 <b>Error:</b> {str(e)[:150]}"))
            await backoff_sleep(retry_attempt, base=5.0)
            retry_attempt = min(retry_attempt + 1, 5)

async def claim_loop(bankr, coord):
    """Claim rewards every 30 min"""
    log("Claim loop started")

    while True:
        try:
            await asyncio.sleep(CLAIM_INTERVAL)
            await coord.ensure_auth()

            epoch_data = await coord.get_epoch()
            debug_log("EPOCH_DATA", epoch_data)
            prev_epoch = epoch_data.get("prevEpochId")

            if prev_epoch and prev_epoch in state.drilled_epochs:
                log(f"Claiming epoch {prev_epoch}...")
                claim_data = await coord.get_claim_calldata([prev_epoch])
                tx = claim_data.get("transaction", {})

                if tx:
                    result = await bankr.submit_tx(tx, f"Claim CRUDE epoch {prev_epoch}")
                    if result.get("success"):
                        log(f"Claimed epoch {prev_epoch}: {result.get('transactionHash', '?')[:16]}...")
                        asyncio.create_task(tg_notify(
                            f"💸 <b>Claimed epoch {prev_epoch}</b>\n"
                            f"{result.get('transactionHash', '?')[:16]}...",
                            silent=True
                        ))
                        state.drilled_epochs.discard(prev_epoch)
                        state.save()
                    else:
                        log(f"Claim failed: {result.get('error', '?')}", "ERROR")
                else:
                    log(f"No transaction in claim data for epoch {prev_epoch}", "WARN")

        except Exception as e:
            log(f"Claim error: {e}", "ERROR")

async def monitor_loop(bankr, coord):
    """Monitor stats every 5 min"""
    log("Monitor loop started")
    tg_report_count = 0

    while True:
        try:
            await asyncio.sleep(MONITOR_INTERVAL)

            runtime = int(time.time() - state.start_time)
            hours = runtime // 3600
            mins = (runtime % 3600) // 60

            total = state.total_solves + state.total_failures
            rate = (state.total_solves / total * 100) if total > 0 else 0

            log(f"Stats: {state.total_solves}/{total} ({rate:.1f}%) | {state.total_credits} credits | {state.gushers} gushers | {hours}h{mins}m")

            # Telegram hourly report
            tg_report_count += 1
            if tg_report_count % 12 == 0:  # every hour (12 × 5 min)
                credits_hr = int(state.total_credits / max(runtime, 1) * 3600)
                await tg_notify(
                    f"🛢 <b>Hourly</b> — {hours}h{mins}m\n"
                    f"📊 {state.total_solves}/{total} ({rate:.1f}%)\n"
                    f"💰 {state.total_credits} cr ({credits_hr}/hr)\n"
                    f"🎉 {state.gushers} gushers",
                    silent=True
                )

            # Per-site-type stats
            if state.site_stats:
                parts = []
                for stype, counts in sorted(state.site_stats.items()):
                    a, r = counts["accept"], counts["reject"]
                    t = a + r
                    pct = (a / t * 100) if t > 0 else 0
                    parts.append(f"{stype}:{a}/{t}({pct:.0f}%)")
                debug_log("SITE_STATS", " | ".join(parts))

            balances = await bankr.get_balances()
            if isinstance(balances, str) and balances != "timeout":
                log(f"Balances: {balances[:150]}")

        except Exception as e:
            log(f"Monitor error: {e}", "ERROR")

# ============ MAIN ============
async def main():
    log("=" * 60)
    log("CRUDE Driller v6.7 - Solver optimization (Decimal revenue, suffix-stripped alts, async receipts)")
    log("=" * 60)
    if DRILLER_DEBUG:
        log("DEBUG MODE ENABLED - verbose logging to crude_debug.log")

    # Validate required config
    missing = []
    if not BANKR_API_KEY:
        missing.append("BANKR_API_KEY")
    if not DRILLER_ADDRESS:
        missing.append("DRILLER_ADDRESS")
    if missing:
        log(f"Missing required config: {', '.join(missing)}", "ERROR")
        log("Copy .env.example to .env and fill in your values", "ERROR")
        return

    # LLM is optional — 95%+ challenges solved deterministically
    llm_ok = False
    if LLM_BACKEND == "openrouter" and OPENROUTER_API_KEY:
        llm_ok = True
    elif LLM_BACKEND == "zai" and ZAI_API_KEY:
        llm_ok = True

    bankr = BankrClient(BANKR_API_KEY)
    coord = CoordinatorClient(COORDINATOR_URL, DRILLER_ADDRESS, bankr)
    if llm_ok:
        solver = LLMSolver(LLM_BACKEND, LLM_MODEL, OPENROUTER_API_KEY, ZAI_API_KEY)
        log(f"LLM: {LLM_MODEL} via {LLM_BACKEND}")
    else:
        solver = LLMSolver.__new__(LLMSolver)
        solver.client = None
        solver.model = None
        solver.backend = None
        log("LLM: disabled (no API key) — deterministic solver only")
    log(f"Rig tier: {DRILLER_TIER} ({TIER_CREDITS[DRILLER_TIER]} credits/solve)")

    await bankr.init()
    await coord.init()
    await tg_init()

    # Initial auth (retry until success)
    for attempt in range(10):
        try:
            await coord.ensure_auth()
            break
        except Exception as e:
            log(f"Auth attempt {attempt+1} failed: {e}", "ERROR")
            if attempt < 9:
                wait = min(30, 5 * (attempt + 1))
                log(f"Retrying in {wait}s...", "WARN")
                await asyncio.sleep(wait)
            else:
                log("Auth failed after 10 attempts, exiting", "ERROR")
                return

    await tg_notify(
        f"🟢 <b>CRUDE Driller v6.7 started</b>\n"
        f"⛏ {DRILLER_ADDRESS[:8]}...{DRILLER_ADDRESS[-4:]}"
    )

    shutdown_event = asyncio.Event()

    async def _guarded(coro):
        try:
            await coro
        except asyncio.CancelledError:
            pass

    try:
        await asyncio.gather(
            _guarded(drilling_loop(bankr, coord, solver)),
            _guarded(claim_loop(bankr, coord)),
            _guarded(monitor_loop(bankr, coord))
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        log("Shutting down...")
        runtime = int(time.time() - state.start_time)
        await tg_notify(
            f"🔴 <b>Driller stopped</b> — {runtime//3600}h{(runtime%3600)//60}m\n"
            f"💰 {state.total_credits} credits | {state.total_solves} solves"
        )
        await tg_close()
        state.save(force=True)
        _flush_log()
        _flush_debug()
        await bankr.close()
        await coord.close()
        log(f"Final: {state.total_solves}/{state.total_solves + state.total_failures} | {state.total_credits} credits")
        _flush_log()
        PID_FILE.unlink(missing_ok=True)

def _run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")

if __name__ == "__main__":
    _run()
