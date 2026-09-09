"""Hesla, session cookie, API klíče. Jen standardní knihovna, žádné externí závislosti."""

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

PBKDF2_ITER = 200_000
SESSION_TTL = 30 * 24 * 3600
SESSION_COOKIE = "opx_session"
KEY_PREFIX = "opx_"


# ------------------------------------------------------------------ hesla

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITER).hex()
    return "pbkdf2$" + str(PBKDF2_ITER) + "$" + salt + "$" + dk


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, dk = stored.split("$")
        if algo != "pbkdf2":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters)).hex()
        return hmac.compare_digest(calc, dk)
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------- session

def _sign(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def make_session(secret: str, user_id: int) -> str:
    payload = str(user_id) + ":" + str(int(time.time()) + SESSION_TTL)
    return payload + ":" + _sign(secret, payload)


def parse_session(secret: str, token: str):
    """Vrátí user_id, nebo None když je cookie neplatná či prošlá."""
    if not token or not secret:
        return None
    try:
        uid, exp, sig = token.split(":")
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(secret, uid + ":" + exp), sig):
        return None
    if int(exp) < time.time():
        return None
    return int(uid)


def csrf_token(secret: str, session_token: str) -> str:
    return _sign(secret, "csrf:" + (session_token or ""))[:32]


# --------------------------------------------------------------- API klíče

def new_api_key():
    """Vrátí (plaintext, hash, prefix). Plaintext se ukáže jen jednou."""
    plain = KEY_PREFIX + secrets.token_urlsafe(30)
    return plain, hash_key(plain), plain[:10]


def hash_key(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


def model_allowed(patterns, model: str) -> bool:
    """Glob vzory (`gemma4:*`, `*-mini`, `gemma4`). Prázdný seznam = všechno.
    Vzor bez dvojtečky pokrývá i všechny tagy modelu (`gemma4` → `gemma4:12b`)."""
    if not patterns:
        return True
    if not model:
        return False
    base = model.split(":", 1)[0]
    for pat in patterns:
        if fnmatchcase(model, pat):
            return True
        if ":" not in pat and fnmatchcase(base, pat):
            return True
    return False


def parse_model_patterns(raw) -> list:
    """Z textu (čárky / mezery / řádky) nebo seznamu udělá čistý seznam vzorů."""
    if not raw:
        return []
    items = raw if isinstance(raw, (list, tuple)) else re.split(r"[,\s]+", str(raw))
    out = []
    for it in items:
        it = str(it).strip()
        if it and it not in out:
            out.append(it)
    return out


@dataclass
class Principal:
    kind: str                      # "session" | "key"
    name: str
    role: str                      # "admin" | "client"
    allowed: list = field(default_factory=list)    # poskytovatelé (slugy); prázdné = všichni
    models: list = field(default_factory=list)     # glob vzory modelů; prázdné = všechny
    key_id: int = None
    user_id: int = None
    rate_per_min: int = 0                          # 0 = výchozí z nastavení
    max_jobs: int = 0                              # 0 = výchozí z nastavení

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def may_use(self, provider_slug: str) -> bool:
        return self.is_admin or not self.allowed or provider_slug in self.allowed

    def may_model(self, model: str) -> bool:
        """Seznam modelů platí pro každý klíč bez ohledu na roli (session z GUI omezená není)."""
        return model_allowed(self.models, model)


def _bearer_candidates(request) -> list:
    """Kde všude může klient poslat proxy klíč — podle SDK jednotlivých poskytovatelů."""
    out = []
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        out.append(auth[7:].strip())
    for h in ("x-api-key", "x-goog-api-key"):
        v = request.headers.get(h)
        if v:
            out.append(v.strip())
    return out


def principal_from_request(request, db):
    """Proxy API klíč (Bearer / x-api-key / x-goog-api-key), jinak session cookie, jinak None."""
    for cand in _bearer_candidates(request):
        if not cand.startswith(KEY_PREFIX):
            continue
        row = db.get_key_by_hash(hash_key(cand))
        if row and not row["disabled"]:
            db.touch_key(row["id"])
            allowed = [s for s in (row["allowed_providers"] or "").split(",") if s]
            models = parse_model_patterns(row.get("allowed_models") or "")
            return Principal("key", row["name"], row["role"], allowed, models, key_id=row["id"],
                             rate_per_min=int(row.get("rate_per_min") or 0),
                             max_jobs=int(row.get("max_jobs") or 0))
        return None  # klíč vypadá jako náš, ale neplatí → nepouštět dál
    uid = parse_session(db.secret, request.cookies.get(SESSION_COOKIE))
    if uid is not None:
        user = db.get_user(uid)
        if user:
            return Principal("session", user["username"], "admin", user_id=user["id"])
    return None


def uses_proxy_key(request) -> bool:
    return any(c.startswith(KEY_PREFIX) for c in _bearer_candidates(request))
