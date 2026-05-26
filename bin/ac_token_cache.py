"""
ac_token_cache.py
Cache dei token OAuth2 con TTL su file.

Evita di rinegoziare il token a ogni run dello script.
Il token viene salvato su file nella directory dei checkpoint,
con TTL basato su expires_in restituito dal server (default 55 min).

Funzionamento:
  1. Lo script chiama get_cached_token(realm, fetch_fn)
  2. Se esiste un token valido in cache → restituisce quello
  3. Altrimenti chiama fetch_fn() → salva → restituisce

Thread-safe tramite file lock (portabile su Linux/Windows).
"""

import fcntl
import json
import logging
import os
import time
import urllib.parse
import urllib.request
import base64
from typing import Callable, Optional, Tuple

logger = logging.getLogger("api_connect.ac_token_cache")

_CACHE_BASE = os.path.join(
    os.environ.get("SPLUNK_HOME", "/opt/splunk"),
    "var", "lib", "splunk", "modinputs", "api_connect", "token_cache"
)

# Margine di sicurezza: rinnova il token 60s prima della scadenza
_SAFETY_MARGIN_S = 60


def _cache_path(realm: str) -> str:
    safe = realm.replace(":", "_").replace("/", "_").replace(" ", "_")
    return os.path.join(_CACHE_BASE, f"{safe}.token.json")


def get_cached_token(realm: str,
                     fetch_fn: Callable[[], Tuple[str, int]]) -> str:
    """
    Restituisce un access token valido per il realm dato.

    fetch_fn: callable che restituisce (access_token, expires_in_seconds)
              Viene chiamata solo se il cache è scaduto o mancante.

    Esempio:
        def fetch():
            token = get_oauth2_token(url, client_id, secret)
            return token, 3600

        bearer = get_cached_token("api_connect:oauth2_cc:erp", fetch)
    """
    os.makedirs(_CACHE_BASE, exist_ok=True)
    path = _cache_path(realm)

    # Leggi cache con lock condiviso (lettura)
    cached = _read_cache(path)
    if cached and _is_valid(cached):
        logger.debug("Token cache HIT per realm=%s, scade tra %ds",
                     realm, int(cached["expires_at"] - time.time()))
        return cached["access_token"]

    # Cache miss o scaduta → fetch con lock esclusivo
    logger.info("Token cache MISS per realm=%s — negoziazione nuovo token", realm)
    try:
        access_token, expires_in = fetch_fn()
    except Exception as e:
        logger.error("Token fetch fallito per realm=%s: %s", realm, e)
        # Se abbiamo un token scaduto ma ancora presente, usalo come fallback
        if cached and cached.get("access_token"):
            logger.warning("Uso token scaduto come fallback per realm=%s", realm)
            return cached["access_token"]
        raise

    if not access_token:
        raise ValueError(f"Token vuoto per realm={realm}")

    entry = {
        "access_token": access_token,
        "expires_at":   time.time() + max(0, int(expires_in) - _SAFETY_MARGIN_S),
        "realm":        realm,
        "fetched_at":   time.time(),
    }
    _write_cache(path, entry)
    logger.info("Token cachato per realm=%s, valido per %ds", realm, expires_in - _SAFETY_MARGIN_S)
    return access_token


def invalidate_token(realm: str) -> None:
    """Invalida esplicitamente il token (es. dopo un 401)."""
    path = _cache_path(realm)
    try:
        os.remove(path)
        logger.info("Token cache invalidata per realm=%s", realm)
    except FileNotFoundError:
        pass


def _is_valid(cached: dict) -> bool:
    return (
        cached.get("access_token")
        and isinstance(cached.get("expires_at"), (int, float))
        and time.time() < cached["expires_at"]
    )


def _read_cache(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_cache(path: str, entry: dict) -> None:
    # Scrivi in file temporaneo poi rinomina (atomico)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            json.dump(entry, f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    os.replace(tmp_path, path)


# ════════════════════════════════════════════════════════════════
# Fetch helper OAuth2 CC con cache integrata
# ════════════════════════════════════════════════════════════════

def get_oauth2_token_cached(token_url: str, client_id: str,
                             client_secret: str, scope: str = "",
                             realm: str = "") -> str:
    """
    Ottiene un token OAuth2 CC usando la cache.
    Se il token è in cache e valido, lo restituisce direttamente.
    Altrimenti negozia e mette in cache.

    realm: chiave univoca per la cache (di solito il credential_realm)
    """
    cache_key = realm or f"oauth2:{token_url}:{client_id}"

    def fetch() -> Tuple[str, int]:
        return _fetch_oauth2_cc(token_url, client_id, client_secret, scope)

    return get_cached_token(cache_key, fetch)


def _fetch_oauth2_cc(token_url: str, client_id: str,
                      client_secret: str, scope: str = "") -> Tuple[str, int]:
    """Chiama il token endpoint e restituisce (access_token, expires_in)."""
    data: dict = {"grant_type": "client_credentials"}
    if scope:
        data["scope"] = scope
    body   = urllib.parse.urlencode(data).encode()
    creds  = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req    = urllib.request.Request(
        token_url, data=body,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload    = json.loads(resp.read())
        token      = payload.get("access_token", "")
        expires_in = int(payload.get("expires_in", 3600))
        return token, expires_in
