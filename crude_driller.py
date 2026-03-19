#!/usr/bin/env python3
"""
CRUDE Driller v6.1 - Alternate Retry + Space Fixes
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

# LLM Config
LLM_BACKEND = os.getenv("LLM_BACKEND", "openrouter")  # "openrouter" or "zai"
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-4o-mini")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
ZAI_API_KEY = os.getenv("ZAI_API_KEY", "")

# Timing
DRILL_DELAY = 0  # no delay — coordinator doesn't rate limit, network latency is enough
CLAIM_INTERVAL = 1800  # 30 min
MONITOR_INTERVAL = 300  # 5 min

# ============ LOGGING ============
LOG_MAX_BYTES = 10 * 1024 * 1024   # 10 MB — rotate when exceeded
LOG_KEEP_BYTES = 5 * 1024 * 1024   # keep last 5 MB after rotation
_log_check_counter = 0

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

def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    _rotate_if_needed(LOG_FILE)

def debug_log(label, data):
    """Write detailed debug data to separate file"""
    if not DRILLER_DEBUG:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\n[{ts}] {label}\n{'='*60}\n")
        if isinstance(data, (dict, list)):
            f.write(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            f.write(str(data))
        f.write("\n")
    _rotate_if_needed(DEBUG_LOG_FILE)

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
                log(f"State loaded: {self.total_solves} solves, {self.total_credits} credits")
            except Exception as e:
                log(f"State load error: {e}", "WARN")

    def save(self):
        data = {
            "drilled_epochs": list(self.drilled_epochs),
            "total_solves": self.total_solves,
            "total_failures": self.total_failures,
            "total_credits": self.total_credits,
            "gushers": self.gushers
        }
        STATE_FILE.write_text(json.dumps(data, indent=2))

state = State()

# ============ BANKR CLIENT ============
class BankrClient:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.bankr.bot"
        self.session = None

    async def init(self):
        self.session = aiohttp.ClientSession()

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
        self.session = aiohttp.ClientSession()

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

    def _check_status(self, status, data):
        """Check HTTP status and raise appropriate exception"""
        if status == 401:
            self.token = None  # force re-auth
            raise AuthError(data.get("error", "Unauthorized"))
        elif status == 403:
            raise ForbiddenError(data.get("error", "Forbidden - insufficient stake?"))
        elif status == 404:
            raise StaleError(data.get("error", "Not found / stale challenge"))
        elif status == 429:
            raise RateLimitError(data.get("error", "Rate limited"))
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
            self._check_status(resp.status, data)
            return data

    async def submit(self, challenge_id, artifact, nonce, site_id, trace):
        async with self.session.post(
            f"{self.url}/v1/submit",
            json={
                "miner": self.driller,
                "challengeId": challenge_id,
                "artifact": artifact,
                "nonce": nonce,
                "siteId": site_id,
                "requestNonce": nonce,
                "trace": trace
            },
            headers=self._headers()
        ) as resp:
            data = await resp.json()
            self._check_status(resp.status, data)
            return data

    async def get_claim_calldata(self, epochs):
        epoch_str = ",".join(map(str, epochs))
        async with self.session.get(f"{self.url}/v1/claim-calldata?epochs={epoch_str}") as resp:
            return await resp.json()

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

# Question type classification
_QUESTION_MAP = [
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


def _extract_revenue_millions(text):
    """Extract revenue in millions from paragraph text"""
    for pat in _REVENUE_RE:
        m = pat.search(text)
        if m:
            val = float(m.group(1))
            # Check if it's billions or millions
            full_match = m.group(0).lower()
            if 'b' in full_match.replace('$', '').replace(str(m.group(1)), '', 1)[:5]:
                return int(val * 1000)
            else:
                return int(val)
    return None


def _extract_margin(text):
    """Extract operating margin percentage from paragraph text"""
    for pat in _MARGIN_RE:
        m = pat.search(text)
        if m:
            return int(float(m.group(1)))
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
            cd = CompanyData(
                name=company,
                paragraph_idx=pi + 1,  # 1-indexed for trace
                employees=_extract_int(para_text, _EMPLOYEES_RE),
                founded=_extract_int(para_text, _FOUNDED_RE),
                revenue_millions=_extract_revenue_millions(para_text),
                margin=_extract_margin(para_text),
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
    }


def deterministic_pass1(doc, questions, companies):
    """
    Pure-Python Pass 1: parse doc, answer question, extract data.
    Returns (company_name, extracted_data_dict, tied_alternates) or (None, None, []) on failure.
    tied_alternates is a list of (company_name, data_dict) for other companies that tied on the question field.
    """
    try:
        debug_log("DET_DOC", doc[:2000])  # Log raw document for debugging
        parsed = parse_companies(doc, companies)
        debug_log("DET_PARSED", {c.name: {"emp": c.employees, "yr": c.founded, "rev": c.revenue_millions, "margin": c.margin, "para_idx": c.paragraph_idx} for c in parsed})

        # Use first question (challenges always have 1)
        q = questions[0] if questions else ""
        qtype = parse_question(q)
        if qtype is None:
            debug_log("DET_QUESTION_UNKNOWN", q)
            return None, None, []

        field, direction = qtype
        debug_log("DET_QUESTION", {"q": q, "field": field, "direction": direction})

        # Filter companies that have the required field
        valid = [c for c in parsed if getattr(c, field) is not None]
        if not valid:
            debug_log("DET_NO_VALID", {"field": field, "total": len(parsed)})
            return None, None, []

        # Select winner (use paragraph_idx as tiebreaker — first in doc wins)
        if direction == 'max':
            winner = max(valid, key=lambda c: (getattr(c, field), -(c.paragraph_idx or 999)))
        else:
            winner = min(valid, key=lambda c: (getattr(c, field), c.paragraph_idx or 999))

        winner_val = getattr(winner, field)

        # Find tied candidates (same field value, different company)
        tied = [c for c in valid if getattr(c, field) == winner_val and c.name != winner.name]
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
        return winner.name, data, tied_alternates

    except Exception as e:
        debug_log("DET_ERROR", {"error": str(e)})
        return None, None, []


# ============ LOCAL COMPUTE ENGINE ============
def _parse_revenue_to_millions(rev_str):
    """Parse revenue string to millions: '$8.5B' -> 8500, '4.4B' -> 4400, '750M' -> 750"""
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
                rev = _parse_revenue_to_millions(extracted_data.get("revenue") or extracted_data.get("Q1_REVENUE", ""))
                if rev is not None:
                    return str(rev % n), "revenue_mod", []

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
            # Primary: strip spaces first, then take first N reversed
            name_ns = company_name.replace(' ', '')
            primary = name_ns[:n][::-1]
            # Alternate: with spaces (original behavior)
            alt_with_spaces = company_name[:n][::-1]
            alts = [alt_with_spaces] if alt_with_spaces != primary else []
            return primary, "first_n_reversed", alts

        # "letters at 1-indexed positions X, Y, Z in the answer company's canonical name"
        m = re.search(r'positions?\s+([\d,\s]+)\s+in', c)
        if not m:
            m = re.search(r'positions?\s+([\d,\s]+)', c)
        if m and "letter" in c:
            positions = [int(p.strip()) for p in m.group(1).split(',') if p.strip()]
            name_ns = company_name.replace(' ', '')
            alts = []

            # Detect if constraint explicitly says to strip spaces
            strip_spaces = "strip" in c and "space" in c or "space-free" in c or "spacefree" in c

            if strip_spaces:
                # Primary: no-spaces indexing (constraint explicitly says so)
                result = []
                valid = True
                for p in positions:
                    if 1 <= p <= len(name_ns):
                        result.append(name_ns[p - 1])
                    else:
                        valid = False
                        break
                if valid and result:
                    primary = ''.join(result)
                    # Alt: with-spaces indexing (in case coordinator disagrees)
                    result_ws = []
                    valid_ws = True
                    for p in positions:
                        if 1 <= p <= len(company_name):
                            result_ws.append(company_name[p - 1])
                        else:
                            valid_ws = False
                            break
                    if valid_ws and result_ws:
                        alt = ''.join(result_ws)
                        if alt != primary:
                            alts.append(alt)
                    return primary, "letter_positions", alts
            else:
                # No explicit strip instruction — try WITH spaces first (coordinator default)
                result_ws = []
                valid_ws = True
                for p in positions:
                    if 1 <= p <= len(company_name):
                        result_ws.append(company_name[p - 1])
                    else:
                        valid_ws = False
                        break

                # Also compute no-spaces variant
                result_ns = []
                valid_ns = True
                for p in positions:
                    if 1 <= p <= len(name_ns):
                        result_ns.append(name_ns[p - 1])
                    else:
                        valid_ns = False
                        break

                if valid_ws and result_ws:
                    primary = ''.join(result_ws)
                    if valid_ns and result_ns:
                        alt = ''.join(result_ns)
                        if alt != primary:
                            alts.append(alt)
                    return primary, "letter_positions", alts
                elif valid_ns and result_ns:
                    return ''.join(result_ns), "letter_positions", []

        # "every Nth letter" (strip spaces, 1-indexed)
        m = re.search(r'every\s+(\d+)(?:th|st|nd|rd)\s+letter', c)
        if m:
            n = int(m.group(1))
            name_ns = company_name.replace(' ', '')
            primary = ''.join(name_ns[i - 1] for i in range(n, len(name_ns) + 1, n))
            # Alt: with spaces
            alt = ''.join(company_name[i - 1] for i in range(n, len(company_name) + 1, n))
            alts = [alt] if alt != primary else []
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
        det_company, det_data, tied_alternates = deterministic_pass1(doc, questions, companies)
        if det_company:
            artifact, method, alt_artifacts = compute_artifact_locally(det_company, det_data, transform_constraint)
            if artifact is not None:
                log(f"⚡ DETERMINISTIC: '{artifact}' via {method} (company: {det_company})")
                debug_log("DETERMINISTIC_OK", {"artifact": artifact, "method": method, "company": det_company, "data": det_data,
                                               "alt_artifacts": alt_artifacts, "tied_companies": [t[0] for t in tied_alternates]})

                # Build alternates list: first space-variant alts, then tied-company alts
                alternates = []
                for alt_art in alt_artifacts:
                    alternates.append((alt_art, det_company, det_data))
                for alt_name, alt_data in tied_alternates:
                    alt_art, alt_method, alt_art_alts = compute_artifact_locally(alt_name, alt_data, transform_constraint)
                    if alt_art is not None:
                        alternates.append((alt_art, alt_name, alt_data))
                        for aa in alt_art_alts:
                            alternates.append((aa, alt_name, alt_data))

                return artifact, det_company, det_data, alternates
            else:
                debug_log("COMPUTE_MISS", {"company": det_company, "constraint": transform_constraint, "data": {k: v for k, v in det_data.items() if not k.startswith('_')}})
                validated_answers = {"Q1": det_company}
                result = self._llm_pass2(doc, det_company, det_data, validated_answers, constraints)
                return result[0], result[1], result[2], []
        else:
            debug_log("DET_FALLBACK_LLM", "Deterministic parser failed, using LLM")

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
def pick_best_site(sites, tier="wildcat"):
    """Pick best site: richness > low depletion"""
    if tier == "wildcat":
        allowed = ["shallow"]
    elif tier == "platform":
        allowed = ["shallow", "medium"]
    else:
        allowed = ["shallow", "medium", "deep"]

    valid = [s for s in sites
             if s.get("estimatedDepth") in allowed
             and s.get("depletionPct", 100) < 100]

    if not valid:
        return None

    # Support both possible field names for richness
    richness_order = {"bonanza": 0, "rich": 1, "standard": 2}
    def get_richness(s):
        r = s.get("richness") or s.get("reserveEstimate", "standard")
        return richness_order.get(str(r).lower(), 2)

    valid.sort(key=lambda s: (get_richness(s), s.get("depletionPct", 0)))
    return valid[0]

# ============ BACKGROUND RECEIPT POSTER ============
_pending_receipts = asyncio.Queue()
_receipt_cooldown = 10  # seconds between receipt submissions to avoid in-flight limit

async def receipt_poster(bankr):
    """Background loop: posts receipts on-chain, throttled to avoid in-flight tx limit"""
    log("Receipt poster started (background, throttled)")
    last_posted = 0
    while True:
        try:
            tx, desc = await _pending_receipts.get()

            # Drain queue — keep only the LATEST receipt if multiple queued up
            latest_tx, latest_desc = tx, desc
            drained = 0
            while not _pending_receipts.empty():
                try:
                    latest_tx, latest_desc = _pending_receipts.get_nowait()
                    drained += 1
                except asyncio.QueueEmpty:
                    break
            if drained:
                debug_log("RECEIPT_DRAIN", f"Skipped {drained} stale receipts, posting latest")

            # Throttle: wait until cooldown since last post
            now = time.time()
            wait = _receipt_cooldown - (now - last_posted)
            if wait > 0:
                await asyncio.sleep(wait)

            for attempt in range(2):
                try:
                    tx_result = await bankr.submit_tx(latest_tx, latest_desc)
                    if tx_result.get("success"):
                        log(f"Receipt posted: {tx_result.get('transactionHash', '?')[:16]}...")
                    else:
                        err = tx_result.get("error", "?")
                        if "in-flight" in str(err).lower():
                            debug_log("RECEIPT_INFLIGHT", "In-flight limit, will retry next cycle")
                            await asyncio.sleep(15)
                            continue
                        debug_log("RECEIPT_FAIL", err)
                    break
                except Exception as e:
                    if attempt < 1:
                        await asyncio.sleep(5)
                    else:
                        debug_log("RECEIPT_DROP", str(e))
            last_posted = time.time()
        except asyncio.CancelledError:
            break
        except Exception as e:
            log(f"Receipt poster error: {e}", "ERROR")

# ============ MAIN LOOPS ============
async def drilling_loop(bankr, coord, solver):
    """Pipeline drilling loop: receipt posting runs in background"""
    log("Drilling loop started (pipeline mode)")
    retry_attempt = 0
    sites_logged = False
    _cached_sites = None
    _sites_ts = 0

    while True:
        try:
            # Ensure auth (with expiry check)
            await coord.ensure_auth()

            # Cache sites for 10 seconds to avoid redundant API calls
            now = time.time()
            if _cached_sites is None or now - _sites_ts > 10:
                sites_data = await coord.get_sites()
                _cached_sites = sites_data.get("sites", [])
                epoch_id = sites_data.get("epochId")
                _sites_ts = now

                if not sites_logged and _cached_sites:
                    debug_log("SITE_STRUCTURE", _cached_sites[0])
                    sites_logged = True

            # Pick best site
            site = pick_best_site(_cached_sites)
            if not site:
                log("No valid sites, waiting 30s...")
                _cached_sites = None  # force refresh next time
                await asyncio.sleep(30)
                continue

            site_id = site["siteId"]
            richness = site.get("richness") or site.get("reserveEstimate", "?")
            log(f"Site: {site.get('region', '?')} ({richness}, {site.get('depletionPct', '?')}%)")

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
                log(f"403 Forbidden: {e} — check stake level", "ERROR")
                await asyncio.sleep(300)
                continue
            except RateLimitError:
                log("429 rate limited on drill", "WARN")
                await backoff_sleep(retry_attempt)
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
                            if stale_artifact and validate_artifact(stale_artifact):
                                stale_para = stale_extracted.get("_paragraph_idx", 1)
                                stale_trace = [
                                    {"type": "extract_value", "paragraph": stale_para, "entity": stale_company, "field": "company_name", "value": stale_company},
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
                                        await _pending_receipts.put((tx, "CRUDE drilling receipt"))
                                    state.save()
                                else:
                                    debug_log("STALE_REJECTED", stale_result.get("reason", "?"))
                                continue  # drill closed, move on
                            else:
                                # Can't solve — submit dummy to close it
                                dummy_trace = [{"type": "apply_constraint", "description": "close stale", "operation": "none", "result": "0"}]
                                await coord.submit(cid, "0", nonce, site_id, dummy_trace)
                                continue
                        except Exception as e:
                            debug_log("STALE_SOLVE_ERR", str(e))
                    elif cid:
                        # Has challengeId but no doc — just submit dummy to close
                        try:
                            dummy_trace = [{"type": "apply_constraint", "description": "close stale", "operation": "none", "result": "0"}]
                            await coord.submit(cid, "0", nonce, site_id, dummy_trace)
                        except Exception:
                            pass
                        await asyncio.sleep(2)
                    else:
                        # No challengeId — coordinator won't give us the drill data
                        # Escalate wait: the stale drill will expire on coordinator side
                        no_cid_attempts = getattr(state, '_no_cid_attempts', 0) + 1
                        state._no_cid_attempts = no_cid_attempts
                        if no_cid_attempts == 1:
                            log("Stale drill (no challengeId), waiting for expiry...", "WARN")
                        wait_secs = min(30 * no_cid_attempts, 120)  # 30, 60, 90, 120 max
                        debug_log("STALE_NO_CID", f"attempt {no_cid_attempts}, waiting {wait_secs}s")
                        try:
                            coord._token = None
                            await coord.ensure_auth()
                        except Exception:
                            pass
                        await asyncio.sleep(wait_secs)
                        if no_cid_attempts >= 5:
                            log("Stale drill won't clear after 5 attempts, continuing anyway...", "WARN")
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
                        dummy_trace = [{"type": "apply_constraint", "description": reason, "operation": "none", "result": "0"}]
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

            employees = extracted.get("employees") or extracted.get("Q1_EMPLOYEES", "N/A")
            founded = extracted.get("founded") or extracted.get("Q1_FOUNDED", "N/A")
            revenue = extracted.get("revenue") or extracted.get("Q1_REVENUE", "N/A")
            margin = extracted.get("margin") or extracted.get("Q1_MARGIN", "N/A")
            trace = [
                {"type": "extract_value", "paragraph": company_para, "entity": company, "field": "company_name", "value": company},
                {"type": "extract_value", "paragraph": company_para, "entity": company, "field": "employees", "value": str(employees)},
                {"type": "extract_value", "paragraph": company_para, "entity": company, "field": "founded", "value": str(founded)},
                {"type": "extract_value", "paragraph": company_para, "entity": company, "field": "revenue", "value": str(revenue)},
                {"type": "extract_value", "paragraph": company_para, "entity": company, "field": "operating_margin", "value": str(margin)},
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
            except RateLimitError:
                log("429 rate limited on submit", "WARN")
                await backoff_sleep(retry_attempt)
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
                log(f"✅ ACCEPTED! +{credits} credits (total: {state.total_credits})")

                if result.get("gusher"):
                    state.gushers += 1
                    log(f"🎉 GUSHER: {result['gusher']}!")

                # Post receipt in BACKGROUND — don't block next drill
                tx = result.get("transaction", {})
                if tx:
                    await _pending_receipts.put((tx, "CRUDE drilling receipt"))
                else:
                    log("No transaction in accepted result!", "ERROR")

                state.save()
            else:
                reason = result.get("reason", str(result))
                debug_log("REJECTION", {"reason": reason, "artifact": artifact, "company": company})

                # === RETRY WITH ALTERNATES ===
                retry_accepted = False
                if alternates:
                    debug_log("RETRY_ALTS", f"Primary rejected, trying {len(alternates)} alternate(s)")
                    for alt_artifact, alt_company, alt_extracted in alternates:
                        if not alt_artifact or not validate_artifact(alt_artifact):
                            continue
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
                            {"type": "extract_value", "paragraph": alt_para, "entity": alt_company, "field": "company_name", "value": alt_company},
                            {"type": "extract_value", "paragraph": alt_para, "entity": alt_company, "field": "employees", "value": str(alt_extracted.get("employees", "N/A"))},
                            {"type": "extract_value", "paragraph": alt_para, "entity": alt_company, "field": "founded", "value": str(alt_extracted.get("founded", "N/A"))},
                            {"type": "extract_value", "paragraph": alt_para, "entity": alt_company, "field": "revenue", "value": str(alt_extracted.get("revenue", "N/A"))},
                            {"type": "extract_value", "paragraph": alt_para, "entity": alt_company, "field": "operating_margin", "value": str(alt_extracted.get("margin", "N/A"))},
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
                        except (StaleError, RateLimitError, ServerError):
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
                            log(f"✅ ALT ACCEPTED! '{alt_artifact}' (company: {alt_company}) +{credits} credits (total: {state.total_credits})")
                            if alt_result.get("gusher"):
                                state.gushers += 1
                                log(f"🎉 GUSHER: {alt_result['gusher']}!")
                            tx = alt_result.get("transaction", {})
                            if tx:
                                await _pending_receipts.put((tx, "CRUDE drilling receipt"))
                            state.save()
                            retry_accepted = True
                            break
                        else:
                            debug_log("ALT_REJECTION", {"artifact": alt_artifact, "company": alt_company,
                                                        "reason": alt_result.get("reason", "?")})

                if not retry_accepted:
                    state.total_failures += 1
                    state.consecutive_failures += 1
                    debug_log("REJECTED", {"reason": reason, "alts_tried": len(alternates) if alternates else 0})
                    state.save()

                    # Circuit breaker
                    if state.consecutive_failures >= 10:
                        log(f"10 consecutive failures! Pausing 5 min...", "ERROR")
                        await asyncio.sleep(300)
                        state.consecutive_failures = 0

            await asyncio.sleep(DRILL_DELAY)

        except (AuthError, ForbiddenError, StaleError, RateLimitError, ServerError):
            raise
        except Exception as e:
            log(f"Drilling error: {e}", "ERROR")
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

    while True:
        try:
            await asyncio.sleep(MONITOR_INTERVAL)

            runtime = int(time.time() - state.start_time)
            hours = runtime // 3600
            mins = (runtime % 3600) // 60

            total = state.total_solves + state.total_failures
            rate = (state.total_solves / total * 100) if total > 0 else 0

            log(f"Stats: {state.total_solves}/{total} ({rate:.1f}%) | {state.total_credits} credits | {state.gushers} gushers | {hours}h{mins}m")

            balances = await bankr.get_balances()
            if isinstance(balances, str) and balances != "timeout":
                log(f"Balances: {balances[:150]}")

        except Exception as e:
            log(f"Monitor error: {e}", "ERROR")

# ============ MAIN ============
async def main():
    log("=" * 60)
    log("CRUDE Driller v6.1 - Alternate Retry + Space Fixes")
    log("=" * 60)
    if DRILLER_DEBUG:
        log("DEBUG MODE ENABLED - verbose logging to crude_debug.log")

    # Validate required config
    missing = []
    if not BANKR_API_KEY:
        missing.append("BANKR_API_KEY")
    if not DRILLER_ADDRESS:
        missing.append("DRILLER_ADDRESS")
    if LLM_BACKEND == "openrouter" and not OPENROUTER_API_KEY:
        missing.append("OPENROUTER_API_KEY")
    if LLM_BACKEND == "zai" and not ZAI_API_KEY:
        missing.append("ZAI_API_KEY")
    if missing:
        log(f"Missing required config: {', '.join(missing)}", "ERROR")
        log("Copy .env.example to .env and fill in your values", "ERROR")
        return

    bankr = BankrClient(BANKR_API_KEY)
    coord = CoordinatorClient(COORDINATOR_URL, DRILLER_ADDRESS, bankr)
    solver = LLMSolver(LLM_BACKEND, LLM_MODEL, OPENROUTER_API_KEY, ZAI_API_KEY)
    log(f"LLM: {LLM_MODEL} via {LLM_BACKEND}")

    await bankr.init()
    await coord.init()

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

    shutdown_event = asyncio.Event()

    async def _guarded(coro):
        try:
            await coro
        except asyncio.CancelledError:
            pass

    try:
        await asyncio.gather(
            _guarded(drilling_loop(bankr, coord, solver)),
            _guarded(receipt_poster(bankr)),
            _guarded(claim_loop(bankr, coord)),
            _guarded(monitor_loop(bankr, coord))
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        log("Shutting down...")
        state.save()
        await bankr.close()
        await coord.close()
        log(f"Final: {state.total_solves}/{state.total_solves + state.total_failures} | {state.total_credits} credits")
        PID_FILE.unlink(missing_ok=True)

def _run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")

if __name__ == "__main__":
    _run()
