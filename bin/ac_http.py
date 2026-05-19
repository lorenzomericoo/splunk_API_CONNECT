"""
ac_http.py
Client HTTP condiviso per tutti gli script generati da API Connect.

Gestisce:
- Autenticazione: Bearer, Basic, API Key (header/query), OAuth2 CC
- Paginazione: offset, cursor, link-header
- Parsing risposta: JSON, JSON array, CSV, TSV, XML, testo+regex
- Retry con backoff esponenziale
- Checkpoint dedup
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


# ──────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────

def get_credential(realm: str, session_key: str) -> Tuple[str, str]:
    """
    Legge username e clear_password da password.conf tramite splunklib.
    Il realm è nella forma  api_connect:<tipo>:<label>.
    Restituisce (username, secret).  Se non trovato restituisce ('', '').
    """
    if not realm or not session_key:
        return '', ''
    try:
        svc = splunk_client.connect(token=session_key)
        for pw in svc.storage_passwords:
            if pw.realm == realm:
                uname = pw['username']
                # Strip meta JSON prefix (usato per OAuth2 extra fields)
                if '||' in uname:
                    uname = uname.split('||', 1)[1]
                return uname, pw['clear_password']
    except Exception as e:
        import logging
        logging.getLogger('api_connect.ac_http').error('get_credential(%s): %s', realm, e)
    return '', ''


def get_oauth2_token(token_url: str, client_id: str, client_secret: str,
                     scope: str = '') -> str:
    """Ottiene un access_token via OAuth2 Client Credentials flow."""
    data: Dict[str, str] = {'grant_type': 'client_credentials'}
    if scope:
        data['scope'] = scope
    body = urllib.parse.urlencode(data).encode()
    creds = base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()
    req = urllib.request.Request(
        token_url, data=body,
        headers={
            'Authorization': f'Basic {creds}',
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read())
            return payload.get('access_token', '')
    except Exception as e:
        import logging
        logging.getLogger('api_connect.ac_http').error('OAuth2 token error: %s', e)
        return ''


def build_auth(cfg: Dict, session_key: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Costruisce (headers, query_params) per il tipo di autenticazione configurato.
    """
    auth_type = cfg.get('auth_type', '')
    realm = cfg.get('credential_realm', '')
    username, secret = get_credential(realm, session_key)

    headers: Dict[str, str] = {}
    params: Dict[str, str] = {}

    if auth_type == 'bearer':
        headers['Authorization'] = f'Bearer {secret}'
    elif auth_type == 'basic':
        token = base64.b64encode(f'{username}:{secret}'.encode()).decode()
        headers['Authorization'] = f'Basic {token}'
    elif auth_type == 'api_key_header':
        key_name = cfg.get('apikey_param', 'X-API-Key')
        headers[key_name] = secret
    elif auth_type == 'api_key_query':
        key_name = cfg.get('apikey_param', 'api_key')
        params[key_name] = secret
    elif auth_type == 'oauth2_cc':
        token_url = cfg.get('token_url', '')
        scope = cfg.get('oauth_scope', '')
        token = get_oauth2_token(token_url, username, secret, scope)
        if token:
            headers['Authorization'] = f'Bearer {token}'

    return headers, params


# ──────────────────────────────────────────────
# HTTP request with retry
# ──────────────────────────────────────────────

def do_request(url: str, method: str, headers: Dict[str, str],
               body_data: Optional[bytes] = None,
               timeout: int = 60,
               max_retries: int = 3) -> Tuple[str, int, str]:
    """
    Esegue una richiesta HTTP con retry e backoff esponenziale.
    Restituisce (body_str, status_code, content_type).
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        if attempt > 0:
            time.sleep(2 ** attempt)
        try:
            req = urllib.request.Request(url, data=body_data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content_type = resp.headers.get('Content-Type', '')
                raw = resp.read()
                charset = _extract_charset(content_type)
                return raw.decode(charset, errors='replace'), resp.status, content_type
        except urllib.error.HTTPError as e:
            content_type = e.headers.get('Content-Type', '')
            raw = e.read()
            charset = _extract_charset(content_type)
            body = raw.decode(charset, errors='replace')
            # Non ritentare su 4xx
            if 400 <= e.code < 500:
                return body, e.code, content_type
            last_exc = e
        except Exception as e:
            last_exc = e

    raise RuntimeError(f'Request failed after {max_retries} attempts: {last_exc}')


def _extract_charset(content_type: str) -> str:
    if 'charset=' in content_type:
        return content_type.split('charset=')[-1].split(';')[0].strip() or 'utf-8'
    return 'utf-8'


# ──────────────────────────────────────────────
# Paginated fetch
# ──────────────────────────────────────────────

def fetch_all_pages(call_cfg: Dict, auth_headers: Dict, auth_params: Dict,
                    pagination_cfg: Dict) -> Iterator[Tuple[str, str]]:
    """
    Generatore che itera su tutte le pagine e yield (body_str, content_type).
    """
    url = call_cfg.get('url', '')
    method = call_cfg.get('method', 'GET').upper()
    pagination_type = pagination_cfg.get('pagination_type', 'none')
    page_param = pagination_cfg.get('page_param', 'page')
    cursor_path = pagination_cfg.get('cursor_path', '')
    max_pages = int(pagination_cfg.get('max_pages', 100))

    extra_headers: Dict[str, str] = {}
    if call_cfg.get('headers'):
        try:
            extra_headers = json.loads(call_cfg['headers'])
        except Exception:
            pass

    headers = {
        'User-Agent': 'Splunk-APIConnect/1.0',
        'Accept': 'application/json, text/csv, */*',
        **auth_headers,
        **extra_headers,
    }

    body_data: Optional[bytes] = None
    if method in ('POST', 'PUT', 'PATCH') and call_cfg.get('body'):
        body_data = call_cfg['body'].encode('utf-8')
        if 'Content-Type' not in headers:
            headers['Content-Type'] = 'application/json'

    page = 0
    cursor = None

    while page < max_pages:
        page_url = _build_page_url(url, pagination_type, page_param, page, cursor, auth_params)

        body, status, ctype = do_request(page_url, method, headers, body_data)

        if status < 200 or status >= 300:
            import logging
            logging.getLogger('api_connect.ac_http').error(
                'HTTP %d from %s — stop pagination', status, page_url
            )
            break

        yield body, ctype

        if pagination_type == 'none':
            break

        # Determine next cursor / page
        if pagination_type == 'offset':
            try:
                data = json.loads(body)
                records = _extract_array(data, pagination_cfg.get('array_root', ''))
                if not records:
                    break
                page += 1
            except Exception:
                break

        elif pagination_type == 'cursor':
            try:
                data = json.loads(body)
                cursor = _jsonpath_single(data, cursor_path)
                if not cursor:
                    break
                page += 1
            except Exception:
                break

        elif pagination_type == 'link_header':
            # Link header handled via response headers — simplified to single page
            break
        else:
            break


def _build_page_url(base_url: str, pagination_type: str, page_param: str,
                    page: int, cursor, auth_params: Dict) -> str:
    url = base_url
    qs: Dict[str, str] = dict(auth_params)
    if pagination_type == 'offset' and page > 0:
        qs[page_param] = str(page)
    elif pagination_type == 'cursor' and cursor:
        qs[page_param] = str(cursor)
    if qs:
        sep = '&' if '?' in url else '?'
        url = url + sep + urllib.parse.urlencode(qs)
    return url


# ──────────────────────────────────────────────
# Response parsing
# ──────────────────────────────────────────────

def parse_response(body: str, content_type: str, cfg: Dict) -> List[Dict]:
    """
    Converte il body della risposta in una lista di record (dict).
    Supporta JSON, JSON array, CSV, TSV, XML, testo + regex.
    """
    fmt = cfg.get('response_format', 'json')
    array_root = cfg.get('array_root', '')

    # Detect CSV from content-type even if format is declared as json
    if 'text/csv' in content_type or 'application/csv' in content_type:
        fmt = 'csv'
    elif 'text/tab' in content_type:
        fmt = 'tsv'

    if fmt in ('csv', 'tsv'):
        return _parse_csv(body, delimiter='\t' if fmt == 'tsv' else ',')

    if fmt == 'xml':
        return _parse_xml(body)

    if fmt == 'text':
        pattern = cfg.get('text_regex', '')
        return _parse_text(body, pattern)

    # JSON (default)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        # Fallback: try CSV
        try:
            return _parse_csv(body)
        except Exception:
            return [{'raw': body}]

    if array_root:
        data = _extract_array(data, array_root)

    if isinstance(data, list):
        return [r if isinstance(r, dict) else {'value': r} for r in data]
    if isinstance(data, dict):
        return [data]
    return [{'value': data}]


def _parse_csv(body: str, delimiter: str = ',') -> List[Dict]:
    try:
        reader = csv.DictReader(io.StringIO(body), delimiter=delimiter)
        return [dict(row) for row in reader]
    except Exception:
        # Fallback: split lines
        lines = [l for l in body.splitlines() if l.strip()]
        if not lines:
            return []
        headers = [h.strip() for h in lines[0].split(delimiter)]
        records = []
        for line in lines[1:]:
            values = [v.strip() for v in line.split(delimiter)]
            records.append(dict(zip(headers, values)))
        return records


def _parse_xml(body: str) -> List[Dict]:
    """Parsing XML semplificato — converte ogni elemento foglia in un dict."""
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(body)
        records = []
        for child in root:
            record = {}
            for elem in child.iter():
                if elem.text and elem.text.strip():
                    record[elem.tag.split('}')[-1]] = elem.text.strip()
            if record:
                records.append(record)
        return records if records else [{'raw': body}]
    except Exception:
        return [{'raw': body}]


def _parse_text(body: str, pattern: str) -> List[Dict]:
    """Parsing testo libero con regex named groups."""
    if not pattern:
        return [{'raw': line} for line in body.splitlines() if line.strip()]
    try:
        compiled = re.compile(pattern)
        records = []
        for line in body.splitlines():
            m = compiled.search(line)
            if m:
                record = m.groupdict() if m.groupdict() else {'match': m.group(0)}
                records.append(record)
        return records
    except re.error:
        return [{'raw': line} for line in body.splitlines() if line.strip()]


def _extract_array(data: Any, array_root: str) -> Any:
    """Naviga un JSONPath semplice ($.a.b[*]) e restituisce il valore."""
    if not array_root:
        return data
    parts = [p for p in re.split(r'[.\[\]]+', array_root.lstrip('$.')) if p and p != '*']
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


def _jsonpath_single(data: Any, path: str) -> Any:
    """Estrae un singolo valore da un JSONPath semplice."""
    return _extract_array(data, path)


# ──────────────────────────────────────────────
# Field extraction & tracciato mapping
# ──────────────────────────────────────────────

def extract_value(record: Dict, path: str) -> Any:
    """
    Estrae un valore da un record seguendo un JSONPath semplice
    o una chiave diretta.
    """
    if not path:
        return None
    # Se è un path JSONPath naviga
    if path.startswith('$.') or '.' in path or '[' in path:
        return _extract_array(record, path)
    # Altrimenti è una chiave diretta
    return record.get(path)


def apply_field_mapping(record: Dict, field_mapping: Dict[str, str],
                        static_values: Optional[Dict[str, str]] = None) -> Dict:
    """
    Applica il mapping tracciato al record.
    field_mapping: { 'time': '$.timestamp', 'username': '$.user_name', ... }
    static_values: { 'hostname': 'erp.corp.it', 'nomeapp': 'ERP' }
    Restituisce un dict con i campi del tracciato standard + tutti i campi originali.
    """
    result = dict(record)  # Mantieni tutti i campi originali
    static_values = static_values or {}

    for std_field, src in field_mapping.items():
        if not src:
            continue
        if src.startswith('__static__:'):
            result[std_field] = src.split(':', 1)[1]
        elif src in static_values:
            result[std_field] = static_values[src]
        else:
            val = extract_value(record, src)
            if val is not None:
                result[std_field] = val

    return result


# ──────────────────────────────────────────────
# Template interpolation for cascade calls
# ──────────────────────────────────────────────

def interpolate(template: str, context: Dict) -> str:
    """
    Sostituisce {{ campo }} e {{ a.b.c }} con i valori dal context.
    Usato per le chiamate in cascata dove l'output della call N
    alimenta parametri della call N+1.
    """
    def replacer(m):
        key = m.group(1).strip()
        val = extract_value(context, key)
        return str(val) if val is not None else ''
    return re.sub(r'\{\{([^}]+)\}\}', replacer, template)


# ──────────────────────────────────────────────
# Checkpoint
# ──────────────────────────────────────────────

CHECKPOINT_BASE = os.path.join(
    os.environ.get('SPLUNK_HOME', '/opt/splunk'),
    'var', 'lib', 'splunk', 'modinputs', 'api_connect'
)


def _checkpoint_path(input_name: str) -> str:
    safe = re.sub(r'[^a-zA-Z0-9_\-]', '_', input_name)
    return os.path.join(CHECKPOINT_BASE, safe + '.checkpoint')


def load_checkpoint(input_name: str) -> Optional[str]:
    path = _checkpoint_path(input_name)
    try:
        with open(path, 'r') as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def save_checkpoint(input_name: str, value: str) -> None:
    path = _checkpoint_path(input_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(str(value))


# ──────────────────────────────────────────────
# Event formatting
# ──────────────────────────────────────────────

TRACCIATO_FIELDS = [
    'time', 'hostname', 'nomeapp', 'tipoazione', 'clientip',
    'username', 'tipooperazione', 'valorePrima', 'valoreDP', 'target', 'note'
]


def format_event(record: Dict) -> str:
    """
    Serializza un record nel formato KV standard aziendale:
      time="..." hostname="..." nomeapp="..." ...
    I campi del tracciato vengono emessi per primi, poi tutti gli altri.
    """
    parts = []
    seen = set()

    # Tracciato fields first (ordered)
    for field in TRACCIATO_FIELDS:
        if field in record and record[field] is not None and str(record[field]) != '':
            val = str(record[field]).replace('"', '\\"')
            parts.append(f'{field}="{val}"')
            seen.add(field)

    # Remaining fields
    for k, v in record.items():
        if k in seen:
            continue
        if v is None or str(v) == '':
            continue
        val = str(v).replace('"', '\\"')
        parts.append(f'{k}="{val}"')

    return ' '.join(parts)
