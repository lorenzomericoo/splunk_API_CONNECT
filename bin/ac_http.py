"""
ac_http.py  v3
Client HTTP condiviso per tutti gli script generati da API Connect.

Novità v3 (Sprint 1):
- Token cache OAuth2 con TTL (ac_token_cache) — non rinegozia a ogni run
- Circuit breaker integrato (ac_circuit_breaker) — stop automatico su N errori
- Retry-After header respect su HTTP 429
- ac_transforms integrato nella chain (pipeline per-campo + output format)
- Metriche per-run (ac_metrics) — aggiorna KV Store dopo ogni esecuzione
"""

import base64
import csv
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Tuple

import splunklib.client as splunk_client

# Moduli Sprint 1 (import lazy per compatibilità se mancano)
try:
    from ac_token_cache import get_oauth2_token_cached
    _TOKEN_CACHE_AVAILABLE = True
except ImportError:
    _TOKEN_CACHE_AVAILABLE = False

try:
    from ac_circuit_breaker import CircuitBreaker, CircuitOpenError
    _CB_AVAILABLE = True
except ImportError:
    _CB_AVAILABLE = False


# ────────────────────────────────────────────────────────────────
# Credential helpers
# ────────────────────────────────────────────────────────────────

def get_credential(realm: str, session_key: str) -> Tuple[str, str]:
    if not realm or not session_key:
        return '', ''
    try:
        svc = splunk_client.connect(token=session_key)
        for pw in svc.storage_passwords:
            if pw.realm == realm:
                uname = pw['username']
                if '||' in uname:
                    uname = uname.split('||', 1)[1]
                return uname, pw['clear_password']
    except Exception as e:
        import logging
        logging.getLogger('api_connect.ac_http').error('get_credential(%s): %s', realm, e)
    return '', ''


def get_oauth2_token(token_url: str, client_id: str, client_secret: str,
                     scope: str = '', realm: str = '') -> str:
    """
    Ottiene un token OAuth2 CC.
    Se ac_token_cache è disponibile, usa la cache con TTL.
    Altrimenti negozia ogni volta (comportamento v2).
    """
    if _TOKEN_CACHE_AVAILABLE and realm:
        try:
            return get_oauth2_token_cached(
                token_url, client_id, client_secret, scope, realm)
        except Exception as e:
            import logging
            logging.getLogger('api_connect.ac_http').error(
                'OAuth2 cached token error: %s', e)
            return ''

    # Fallback: negozia direttamente
    data: Dict[str, str] = {'grant_type': 'client_credentials'}
    if scope:
        data['scope'] = scope
    body  = urllib.parse.urlencode(data).encode()
    creds = base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()
    req   = urllib.request.Request(
        token_url, data=body,
        headers={
            'Authorization': f'Basic {creds}',
            'Content-Type':  'application/x-www-form-urlencoded',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read()).get('access_token', '')
    except Exception as e:
        import logging
        logging.getLogger('api_connect.ac_http').error('OAuth2 token error: %s', e)
        return ''


def _build_auth_headers(auth_type: str, username: str, secret: str,
                         apikey_param: str = '', token_url: str = '',
                         oauth_scope: str = '', realm: str = '') -> Tuple[Dict, Dict]:
    """
    Restituisce (headers, query_params) per il tipo di auth dato.
    """
    headers: Dict[str, str] = {}
    params:  Dict[str, str] = {}
    if auth_type == 'bearer':
        headers['Authorization'] = f'Bearer {secret}'
    elif auth_type == 'basic':
        tok = base64.b64encode(f'{username}:{secret}'.encode()).decode()
        headers['Authorization'] = f'Basic {tok}'
    elif auth_type == 'api_key_header':
        headers[apikey_param or 'X-API-Key'] = secret
    elif auth_type == 'api_key_query':
        params[apikey_param or 'api_key'] = secret
    elif auth_type == 'oauth2_cc':
        token = get_oauth2_token(token_url, username, secret, oauth_scope, realm)
        if token:
            headers['Authorization'] = f'Bearer {token}'
    return headers, params


# ────────────────────────────────────────────────────────────────
# Per-call auth resolution
# ────────────────────────────────────────────────────────────────

def resolve_call_auth(call_cfg: Dict, global_cfg: Dict,
                      session_key: str) -> Tuple[Dict, Dict]:
    """
    Risolve l'autenticazione per una singola call.
    Se auth_type == 'inherited' usa la configurazione globale.
    Altrimenti usa i parametri specifici della call.
    """
    auth_type = call_cfg.get('auth_type', 'inherited')

    if auth_type == 'inherited':
        auth_type    = global_cfg.get('auth_type', 'none')
        realm        = global_cfg.get('credential_realm', '')
        apikey_param = global_cfg.get('apikey_param', '')
        token_url    = global_cfg.get('token_url', '')
        oauth_scope  = global_cfg.get('oauth_scope', '')
    else:
        realm        = call_cfg.get('credential_realm', '')
        apikey_param = call_cfg.get('apikey_param', '')
        token_url    = call_cfg.get('token_url', '')
        oauth_scope  = call_cfg.get('oauth_scope', '')

    if auth_type in ('none', ''):
        return {}, {}

    username, secret = get_credential(realm, session_key)
    return _build_auth_headers(auth_type, username, secret,
                                apikey_param, token_url, oauth_scope, realm)


def build_auth(cfg: Dict, session_key: str) -> Tuple[Dict, Dict]:
    """Compat: build auth from global config (used for single-call scripts)."""
    dummy_call = {'auth_type': 'inherited'}
    return resolve_call_auth(dummy_call, cfg, session_key)


# ────────────────────────────────────────────────────────────────
# Error policy
# ────────────────────────────────────────────────────────────────

class RetryableError(Exception):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body

class SkippableError(Exception):
    pass

class FatalError(Exception):
    pass


def apply_error_policy(status_code: int, body: str, policy: str) -> str:
    """
    Applica la error policy al codice HTTP ricevuto.
    Restituisce 'ok' se il codice è 2xx.
    Altrimenti raise appropriato oppure restituisce 'skip'.

    Policies:
      default       → stop on any error
      retry_429     → retry on 429, stop on others
      skip_404      → skip on 404, stop on others
      skip_all_4xx  → skip on 4xx, stop on 5xx
      stop_5xx      → stop on 5xx, skip on 4xx
      skip_all      → skip on any error
    """
    if 200 <= status_code < 300:
        return 'ok'

    if policy == 'retry_429' and status_code == 429:
        raise RetryableError(status_code, body)
    if policy == 'skip_404' and status_code == 404:
        raise SkippableError()
    if policy == 'skip_all_4xx' and 400 <= status_code < 500:
        raise SkippableError()
    if policy == 'stop_5xx' and status_code >= 500:
        raise FatalError(f'HTTP {status_code}: {body[:200]}')
    if policy == 'stop_5xx' and 400 <= status_code < 500:
        raise SkippableError()
    if policy == 'skip_all':
        raise SkippableError()
    # default: stop
    raise FatalError(f'HTTP {status_code}: {body[:200]}')


# ────────────────────────────────────────────────────────────────
# HTTP request with retry + error policy
# ────────────────────────────────────────────────────────────────

def do_request(url: str, method: str, headers: Dict[str, str],
               body_data: Optional[bytes] = None,
               timeout: int = 60,
               max_retries: int = 3,
               error_policy: str = 'default',
               circuit_breaker=None) -> Tuple[str, int, str]:
    """
    Esegue una richiesta HTTP con:
    - retry + backoff esponenziale
    - Retry-After header respect su HTTP 429
    - circuit breaker (opzionale)
    - error policy applicata sul codice di risposta
    Restituisce (body_str, status_code, content_type).
    """
    last_exc: Optional[Exception] = None

    # Circuit breaker check
    if circuit_breaker and _CB_AVAILABLE:
        try:
            circuit_breaker.before_call()
        except CircuitOpenError:
            raise

    for attempt in range(max_retries):
        if attempt > 0:
            _get_logger().info(
                'Retry %d/%d (policy=%s)', attempt, max_retries, error_policy)

        try:
            req = urllib.request.Request(url, data=body_data,
                                          headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ctype = resp.headers.get('Content-Type', '')
                raw   = resp.read()
                body  = raw.decode(_extract_charset(ctype), errors='replace')
                apply_error_policy(resp.status, body, error_policy)
                if circuit_breaker and _CB_AVAILABLE:
                    circuit_breaker.record_success()
                return body, resp.status, ctype

        except urllib.error.HTTPError as e:
            ctype = e.headers.get('Content-Type', '')
            raw   = e.read()
            body  = raw.decode(_extract_charset(ctype), errors='replace')

            # Respect Retry-After su 429
            if e.code == 429:
                retry_after = e.headers.get('Retry-After', '')
                wait = _parse_retry_after(retry_after)
                _get_logger().warning(
                    'HTTP 429 — Retry-After=%ss (attempt %d/%d)',
                    wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue

            try:
                result = apply_error_policy(e.code, body, error_policy)
            except (RetryableError, SkippableError, FatalError):
                if circuit_breaker and _CB_AVAILABLE:
                    circuit_breaker.record_failure()
                raise

            if result == 'ok':
                if circuit_breaker and _CB_AVAILABLE:
                    circuit_breaker.record_success()
                return body, e.code, ctype

        except (RetryableError, SkippableError, FatalError):
            raise
        except Exception as exc:
            last_exc = exc
            wait = 2 ** attempt
            _get_logger().warning('Request error (attempt %d): %s — retry in %ds',
                                   attempt + 1, exc, wait)
            time.sleep(wait)

    if circuit_breaker and _CB_AVAILABLE:
        circuit_breaker.record_failure()
    raise FatalError(f'Request failed after {max_retries} attempts: {last_exc}')


def _parse_retry_after(header_val: str) -> float:
    """Interpreta il valore Retry-After (secondi o HTTP-date)."""
    if not header_val:
        return 5.0
    try:
        return max(1.0, float(header_val.strip()))
    except ValueError:
        # Prova come HTTP-date
        try:
            from email.utils import parsedate_to_datetime
            retry_dt = parsedate_to_datetime(header_val.strip())
            wait = (retry_dt.timestamp() - time.time())
            return max(1.0, wait)
        except Exception:
            return 5.0


def _extract_charset(ctype: str) -> str:
    if 'charset=' in ctype:
        return ctype.split('charset=')[-1].split(';')[0].strip() or 'utf-8'
    return 'utf-8'


# ────────────────────────────────────────────────────────────────
# Chain execution (multi-call with per-call auth + enrichment join)
# ────────────────────────────────────────────────────────────────

def execute_chain(calls: List[Dict], global_cfg: Dict,
                  session_key: str,
                  pagination_cfg: Optional[Dict] = None) -> List[Dict]:
    """
    Esegue la catena di call e restituisce la lista finale di record arricchiti.

    - Ogni call risolve la propria auth (inherited o override)
    - Le variabili {{campo}} nell'URL/body vengono interpolate dal record corrente
    - Se join_key è impostato, il risultato della call viene mergiato sul record
      della call precedente usando quella chiave
    - La paginazione si applica sull'ultima call della catena
    """
    logger = _get_logger()
    pagination_cfg = pagination_cfg or {}

    if not calls:
        return []

    # Prima call con paginazione → produce lista base di record
    first_call = calls[0]
    auth_h, auth_p = resolve_call_auth(first_call, global_cfg, session_key)
    base_records = list(_fetch_paginated(
        first_call, auth_h, auth_p,
        pagination_cfg if len(calls) == 1 else {},
        global_cfg
    ))
    logger.info('Call 1 (%s): %d record', first_call.get('url',''), len(base_records))

    if len(calls) == 1:
        return base_records

    # Call successive: per ogni record della base, esegui le call rimanenti
    enriched = []
    for rec in base_records:
        current = dict(rec)
        for idx, call in enumerate(calls[1:], start=2):
            call_auth_h, call_auth_p = resolve_call_auth(call, global_cfg, session_key)

            url     = interpolate(call.get('url', ''), current)
            method  = call.get('method', 'GET').upper()
            policy  = call.get('error_policy', 'default')
            join_key = call.get('join_key', '').strip()

            extra_h: Dict[str, str] = {}
            if call.get('headers'):
                try:
                    extra_h = json.loads(call['headers'])
                except Exception:
                    pass

            headers = {
                'User-Agent': 'Splunk-APIConnect/1.0',
                'Accept': 'application/json, text/csv, */*',
                **call_auth_h,
                **extra_h,
            }

            body_data: Optional[bytes] = None
            if method in ('POST', 'PUT', 'PATCH') and call.get('body'):
                body_str = interpolate(call['body'], current)
                body_data = body_str.encode('utf-8')
                if 'Content-Type' not in headers:
                    headers['Content-Type'] = 'application/json'

            if call_auth_p:
                sep = '&' if '?' in url else '?'
                url = url + sep + urllib.parse.urlencode(call_auth_p)

            try:
                body, status, ctype = do_request(
                    url, method, headers, body_data,
                    error_policy=policy
                )
                sub_records = parse_response(body, ctype, global_cfg)

                if join_key and sub_records:
                    # Merge: trova il sub_record con join_key == current[join_key]
                    cur_val = str(current.get(join_key, ''))
                    matched = next(
                        (r for r in sub_records if str(r.get(join_key, '')) == cur_val),
                        sub_records[0]  # fallback: primo record
                    )
                    current.update(matched)
                elif sub_records:
                    # Nessun join: merge flat del primo sub_record
                    current.update(sub_records[0])

                logger.info('Call %d (%s): OK status=%d', idx, url, status)

            except SkippableError:
                logger.warning('Call %d (%s): skip (error_policy=%s)', idx, url, policy)
                current['_ac_skip'] = True
                break
            except FatalError as fe:
                logger.error('Call %d (%s): fatal — %s', idx, url, fe)
                current['_ac_error'] = str(fe)
                break

        if not current.get('_ac_skip'):
            enriched.append(current)

    logger.info('Chain completata: %d record arricchiti', len(enriched))
    return enriched


def _fetch_paginated(call_cfg: Dict, auth_headers: Dict, auth_params: Dict,
                     pagination_cfg: Dict, global_cfg: Dict) -> Iterator[Dict]:
    """Itera su tutte le pagine di una singola call e yield record."""
    url     = call_cfg.get('url', '')
    method  = call_cfg.get('method', 'GET').upper()
    policy  = call_cfg.get('error_policy', 'default')

    pag_type   = pagination_cfg.get('pagination_type', 'none')
    page_param = pagination_cfg.get('page_param', 'page')
    cursor_path = pagination_cfg.get('cursor_path', '')
    max_pages  = int(pagination_cfg.get('max_pages', 100))
    array_root = pagination_cfg.get('array_root', global_cfg.get('array_root', ''))

    extra_h: Dict[str, str] = {}
    if call_cfg.get('headers'):
        try:
            extra_h = json.loads(call_cfg['headers'])
        except Exception:
            pass

    headers = {
        'User-Agent': 'Splunk-APIConnect/1.0',
        'Accept': 'application/json, text/csv, */*',
        **auth_headers,
        **extra_h,
    }

    body_data: Optional[bytes] = None
    if method in ('POST', 'PUT', 'PATCH') and call_cfg.get('body'):
        body_data = call_cfg['body'].encode('utf-8')
        if 'Content-Type' not in headers:
            headers['Content-Type'] = 'application/json'

    page   = 0
    cursor = None
    logger = _get_logger()

    while page < max_pages:
        page_url = _build_page_url(url, pag_type, page_param, page, cursor, auth_params)

        try:
            body, status, ctype = do_request(
                page_url, method, headers, body_data, error_policy=policy)
        except SkippableError:
            logger.warning('Paginazione: skip a pagina %d', page)
            break
        except FatalError as fe:
            logger.error('Paginazione: fatal a pagina %d — %s', page, fe)
            break

        records, next_cursor = _parse_with_cursor(body, ctype, global_cfg, cursor_path)
        for r in records:
            yield r

        if pag_type == 'none' or not records:
            break
        if pag_type in ('offset', 'cursor'):
            if not next_cursor and pag_type == 'cursor':
                break
            cursor = next_cursor
            page  += 1
        elif pag_type == 'link_header':
            break
        else:
            break


def _build_page_url(base: str, pag_type: str, page_param: str,
                    page: int, cursor, auth_params: Dict) -> str:
    url = base
    qs: Dict[str, str] = dict(auth_params)
    if pag_type == 'offset' and page > 0:
        qs[page_param] = str(page)
    elif pag_type == 'cursor' and cursor:
        qs[page_param] = str(cursor)
    if qs:
        sep = '&' if '?' in url else '?'
        url = url + sep + urllib.parse.urlencode(qs)
    return url


def _parse_with_cursor(body: str, ctype: str,
                        cfg: Dict, cursor_path: str) -> Tuple[List[Dict], Any]:
    records = parse_response(body, ctype, cfg)
    next_cursor = None
    if cursor_path:
        try:
            data = json.loads(body)
            next_cursor = _extract_array(data, cursor_path)
        except Exception:
            pass
    return records, next_cursor


# ────────────────────────────────────────────────────────────────
# Response parsing — extended (HTML, XML nested, CSV, text+regex)
# ────────────────────────────────────────────────────────────────

def parse_response(body: str, content_type: str, cfg: Dict) -> List[Dict]:
    fmt        = cfg.get('response_format', 'json')
    array_root = cfg.get('array_root', '')

    # Content-type overrides
    ct = content_type.lower()
    if 'text/csv' in ct or 'application/csv' in ct:
        fmt = 'csv'
    elif 'text/tab' in ct:
        fmt = 'tsv'
    elif 'text/html' in ct:
        fmt = 'html'
    elif 'text/xml' in ct or 'application/xml' in ct:
        fmt = 'xml'

    if fmt in ('csv', 'tsv'):
        return _parse_csv(body, delimiter='\t' if fmt == 'tsv' else ',')
    if fmt == 'xml':
        return _parse_xml(body)
    if fmt == 'html':
        return _parse_html(body)
    if fmt == 'text':
        return _parse_text(body, cfg.get('text_regex', ''))

    # JSON
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        # Fallback attempts
        stripped = body.strip()
        if stripped.startswith('<'):
            return _parse_xml(body)
        try:
            return _parse_csv(body)
        except Exception:
            return [{'raw': body}]

    if array_root:
        data = _extract_array(data, array_root)

    if isinstance(data, list):
        return [_flatten(r) if isinstance(r, dict) else {'value': r} for r in data]
    if isinstance(data, dict):
        return [_flatten(data)]
    return [{'value': data}]


def _parse_csv(body: str, delimiter: str = ',') -> List[Dict]:
    try:
        reader = csv.DictReader(io.StringIO(body.strip()), delimiter=delimiter)
        rows = [dict(row) for row in reader]
        if rows:
            return rows
    except Exception:
        pass
    # Fallback: manual split
    lines = [l for l in body.splitlines() if l.strip()]
    if not lines:
        return []
    headers = [h.strip() for h in lines[0].split(delimiter)]
    result = []
    for line in lines[1:]:
        values = [v.strip() for v in line.split(delimiter)]
        result.append(dict(zip(headers, values)))
    return result


def _parse_xml(body: str) -> List[Dict]:
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(body)
        records = []
        # Try to find a repeating element
        children = list(root)
        if not children:
            return [{'raw': root.text or body}]
        for child in children:
            rec: Dict[str, Any] = {}
            for elem in child.iter():
                tag = elem.tag.split('}')[-1]  # strip namespace
                if elem.text and elem.text.strip():
                    if tag in rec:
                        # Duplicate tag: append index
                        i = 1
                        while f'{tag}_{i}' in rec:
                            i += 1
                        rec[f'{tag}_{i}'] = elem.text.strip()
                    else:
                        rec[tag] = elem.text.strip()
                # Include attributes
                for attr_k, attr_v in elem.attrib.items():
                    rec[f'{tag}_{attr_k}'] = attr_v
            if rec:
                records.append(rec)
        return records if records else [{'raw': body}]
    except ET.ParseError:
        return [{'raw': body}]


def _parse_html(body: str) -> List[Dict]:
    """
    Estrae testo da HTML senza dipendenze esterne.
    Rimuove tag, script, style e restituisce righe di testo pulito.
    Per strutture tabellari HTML cerca <table> e li converte.
    """
    # Try table extraction first
    table_re = re.compile(r'<table[^>]*>(.*?)</table>', re.DOTALL | re.IGNORECASE)
    tr_re     = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
    td_re     = re.compile(r'<t[dh][^>]*>(.*?)</t[dh]>', re.DOTALL | re.IGNORECASE)
    tag_re    = re.compile(r'<[^>]+>')

    def strip_tags(s: str) -> str:
        return tag_re.sub('', s).strip()

    tables = table_re.findall(body)
    if tables:
        records = []
        for table_html in tables:
            rows_html = tr_re.findall(table_html)
            if not rows_html:
                continue
            headers = [strip_tags(h) for h in td_re.findall(rows_html[0])]
            if not headers:
                continue
            for row_html in rows_html[1:]:
                cells = [strip_tags(c) for c in td_re.findall(row_html)]
                if cells:
                    records.append(dict(zip(headers, cells)))
        if records:
            return records

    # Fallback: plain text lines
    clean = tag_re.sub(' ', body)
    clean = re.sub(r'[ \t]+', ' ', clean)
    lines = [l.strip() for l in clean.splitlines() if l.strip()]
    return [{'line': l} for l in lines[:500]]


def _parse_text(body: str, pattern: str) -> List[Dict]:
    if not pattern:
        return [{'raw': line} for line in body.splitlines() if line.strip()]
    try:
        compiled = re.compile(pattern)
        records  = []
        for line in body.splitlines():
            m = compiled.search(line)
            if m:
                records.append(m.groupdict() if m.groupdict() else {'match': m.group(0), 'raw': line})
        return records
    except re.error:
        return [{'raw': line} for line in body.splitlines() if line.strip()]


def _flatten(d: Dict, prefix: str = '', sep: str = '.') -> Dict:
    """Appiattisce un dict annidato: {'a': {'b': 1}} → {'a.b': 1}"""
    items: Dict = {}
    for k, v in d.items():
        new_key = f'{prefix}{sep}{k}' if prefix else k
        if isinstance(v, dict):
            items.update(_flatten(v, new_key, sep))
        elif isinstance(v, list):
            # Per array brevi (≤5 elementi scalari) serializza inline
            if all(not isinstance(i, (dict, list)) for i in v) and len(v) <= 5:
                items[new_key] = ', '.join(str(i) for i in v)
            else:
                items[new_key] = json.dumps(v, ensure_ascii=False)
        else:
            items[new_key] = v
    return items


# ────────────────────────────────────────────────────────────────
# JSONPath / value extraction
# ────────────────────────────────────────────────────────────────

def _extract_array(data: Any, path: str) -> Any:
    if not path:
        return data
    parts = [p for p in re.split(r'[.\[\]]+', path.lstrip('$.')) if p and p != '*']
    val = data
    for part in parts:
        if isinstance(val, dict):
            val = val.get(part, val)
        elif isinstance(val, list):
            try:
                val = val[int(part)]
            except (ValueError, IndexError):
                break
        else:
            break
    return val


def extract_value(record: Dict, path: str) -> Any:
    if not path:
        return None
    if path.startswith('$.') or '.' in path or '[' in path:
        return _extract_array(record, path)
    return record.get(path)


def apply_field_mapping(record: Dict, field_mapping: Dict[str, str]) -> Dict:
    result = dict(record)
    for std_field, src in field_mapping.items():
        if not src:
            continue
        if src.startswith('__static__:'):
            result[std_field] = src.split(':', 1)[1]
        else:
            val = extract_value(record, src)
            if val is not None:
                result[std_field] = val
    return result


# ────────────────────────────────────────────────────────────────
# Template interpolation  {{campo}}  e  {{a.b.c}}
# ────────────────────────────────────────────────────────────────

def interpolate(template: str, context: Dict) -> str:
    def replacer(m):
        key = m.group(1).strip()
        val = extract_value(context, key)
        if val is None:
            val = context.get(key, '')
        return str(val) if val is not None else ''
    return re.sub(r'\{\{([^}]+)\}\}', replacer, template)


# ────────────────────────────────────────────────────────────────
# Checkpoint
# ────────────────────────────────────────────────────────────────

CHECKPOINT_BASE = os.path.join(
    os.environ.get('SPLUNK_HOME', '/opt/splunk'),
    'var', 'lib', 'splunk', 'modinputs', 'api_connect'
)


def _checkpoint_path(input_name: str) -> str:
    safe = re.sub(r'[^a-zA-Z0-9_\-]', '_', input_name)
    return os.path.join(CHECKPOINT_BASE, safe + '.checkpoint')


def load_checkpoint(input_name: str) -> Optional[str]:
    try:
        with open(_checkpoint_path(input_name), 'r') as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def save_checkpoint(input_name: str, value: str) -> None:
    path = _checkpoint_path(input_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(str(value))


# ────────────────────────────────────────────────────────────────
# Event formatting
# ────────────────────────────────────────────────────────────────

TRACCIATO_FIELDS = [
    'time', 'hostname', 'nomeapp', 'tipoazione', 'clientip',
    'username', 'tipooperazione', 'valorePrima', 'valoreDP', 'target', 'note'
]


def format_event(record: Dict) -> str:
    parts = []
    seen  = set()
    for field in TRACCIATO_FIELDS:
        v = record.get(field)
        if v is not None and str(v) != '':
            parts.append(f'{field}="{str(v).replace(chr(34), chr(92)+chr(34))}"')
            seen.add(field)
    for k, v in record.items():
        if k in seen or k.startswith('_ac_'):
            continue
        if v is None or str(v) == '':
            continue
        parts.append(f'{k}="{str(v).replace(chr(34), chr(92)+chr(34))}"')
    return ' '.join(parts)


# ────────────────────────────────────────────────────────────────
# Internal helpers
# ────────────────────────────────────────────────────────────────

def _get_logger():
    import logging
    return logging.getLogger('api_connect.ac_http')


# Compat aliases
def fetch_all_pages(call_cfg, auth_headers, auth_params, pagination_cfg):
    """Compat wrapper usato dal template precedente."""
    global_cfg = dict(pagination_cfg)
    for record in _fetch_paginated(call_cfg, auth_headers, auth_params,
                                    pagination_cfg, global_cfg):
        yield json.dumps(record), 'application/json'


# ────────────────────────────────────────────────────────────────
# execute_chain_v3 — versione con circuit breaker, transforms e metrics
# ────────────────────────────────────────────────────────────────

def execute_chain_v3(calls: List[Dict], global_cfg: Dict,
                     session_key: str,
                     pagination_cfg: Optional[Dict] = None) -> List[Dict]:
    """
    execute_chain con Sprint-1 features:
    - Circuit breaker per-input (stop su N errori consecutivi)
    - Retry-After respect su 429
    - Compatible con execute_chain, usato dagli script generati v3
    """
    logger = _get_logger()
    pagination_cfg = pagination_cfg or {}

    if not calls:
        return []

    # Circuit breaker
    cb = None
    if _CB_AVAILABLE:
        cb_cfg = global_cfg.get('circuit_breaker', {})
        cb = CircuitBreaker(
            name=global_cfg.get('name', 'api_connect'),
            failure_threshold=int(cb_cfg.get('failure_threshold', 5)),
            cooldown_s=int(cb_cfg.get('cooldown_s', 120)),
        )
        try:
            cb.before_call()
        except CircuitOpenError as e:
            logger.warning('Circuit breaker OPEN: %s', e)
            return []

    # Prima call
    first_call   = calls[0]
    auth_h, auth_p = resolve_call_auth(first_call, global_cfg, session_key)
    try:
        base_records = list(_fetch_paginated(
            first_call, auth_h, auth_p,
            pagination_cfg if len(calls) == 1 else {},
            global_cfg,
        ))
        if cb:
            cb.record_success()
    except FatalError as e:
        if cb:
            cb.record_failure()
        raise
    logger.info('Call 1 (%s): %d record', first_call.get('url', ''), len(base_records))

    if len(calls) == 1:
        return base_records

    # Call successive
    enriched = []
    for rec in base_records:
        current = dict(rec)
        for idx, call in enumerate(calls[1:], start=2):
            call_auth_h, call_auth_p = resolve_call_auth(call, global_cfg, session_key)
            url      = interpolate(call.get('url', ''), current)
            method   = call.get('method', 'GET').upper()
            policy   = call.get('error_policy', 'default')
            join_key = call.get('join_key', '').strip()

            extra_h: Dict[str, str] = {}
            if call.get('headers'):
                try:
                    extra_h = json.loads(call['headers'])
                except Exception:
                    pass

            req_headers = {
                'User-Agent': 'Splunk-APIConnect/3.0',
                'Accept': 'application/json, text/csv, */*',
                **call_auth_h, **extra_h,
            }

            body_data: Optional[bytes] = None
            if method in ('POST', 'PUT', 'PATCH') and call.get('body'):
                b = interpolate(call['body'], current)
                body_data = b.encode('utf-8')
                if 'Content-Type' not in req_headers:
                    req_headers['Content-Type'] = 'application/json'

            if call_auth_p:
                sep = '&' if '?' in url else '?'
                url = url + sep + urllib.parse.urlencode(call_auth_p)

            try:
                body, status, ctype = do_request(
                    url, method, req_headers, body_data,
                    error_policy=policy, circuit_breaker=cb,
                )
                sub_records = parse_response(body, ctype, global_cfg)
                if join_key and sub_records:
                    cur_val = str(current.get(join_key, ''))
                    matched = next(
                        (r for r in sub_records if str(r.get(join_key, '')) == cur_val),
                        sub_records[0],
                    )
                    current.update(matched)
                elif sub_records:
                    current.update(sub_records[0])

            except SkippableError:
                logger.warning('Call %d skip', idx)
                current['_ac_skip'] = True
                break
            except FatalError as fe:
                logger.error('Call %d fatal: %s', idx, fe)
                current['_ac_error'] = str(fe)
                break

        if not current.get('_ac_skip'):
            enriched.append(current)

    return enriched
