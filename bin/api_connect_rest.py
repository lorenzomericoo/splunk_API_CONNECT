"""
api_connect_rest.py  v2
Custom REST handlers per API Connect.

Handlers:
  TestCallHandler     — esegue una singola call lato server (usa ac_http v2)
  InputsHandler       — proxy CRUD KV Store
  GenerateInputHandler→ in api_connect_generate.py
"""

import json
import logging
import os
import sys
import time

import splunk.admin as admin
import splunk.rest as rest

sys.path.insert(0, os.path.dirname(__file__))

from ac_http import (
    resolve_call_auth, do_request, parse_response,
    _extract_charset,
)
from ac_logger import get_logger

logger = logging.getLogger('splunk.api_connect.rest')


# ════════════════════════════════════════════════════════════════
# TestCallHandler
# ════════════════════════════════════════════════════════════════
class TestCallHandler(admin.MConfigHandler):
    """
    POST /servicesNS/nobody/api_connect/api_connect_test
    
    Parametri attesi:
      call_config   — JSON della singola call (url, method, headers, body,
                      auth_type, credential_realm, apikey_param, error_policy)
      global_config — JSON della configurazione globale (auth_type,
                      credential_realm, token_url, oauth_scope, apikey_param)
    
    Restituisce JSON con:
      status_code, latency_ms, content_type, body (max 100 KB), truncated
    """

    def setup(self):
        self.supportedArgs.addOptArg('call_config')
        self.supportedArgs.addOptArg('global_config')
        # Legacy params (v1 compat)
        self.supportedArgs.addOptArg('auth_type')
        self.supportedArgs.addOptArg('credential_realm')
        self.supportedArgs.addOptArg('token_url')
        self.supportedArgs.addOptArg('apikey_param')
        self.supportedArgs.addOptArg('calls')

    def handleCreate(self, confInfo):
        import urllib.request
        import urllib.error
        import urllib.parse

        try:
            sk = self.getSessionKey()

            # ── Parse input ──────────────────────────────────────────
            call_config_raw   = self.callerArgs.data.get('call_config',   ['{}'])[0] or '{}'
            global_config_raw = self.callerArgs.data.get('global_config', ['{}'])[0] or '{}'

            call_cfg   = json.loads(call_config_raw)
            global_cfg = json.loads(global_config_raw)

            # v1 compat: se call_config vuoto prova con params legacy
            if not call_cfg.get('url'):
                calls_raw = self.callerArgs.data.get('calls', ['[]'])[0] or '[]'
                legacy_calls = json.loads(calls_raw)
                if legacy_calls:
                    call_cfg = legacy_calls[0]

                if not global_cfg.get('auth_type'):
                    global_cfg = {
                        'auth_type':        self.callerArgs.data.get('auth_type',        ['none'])[0],
                        'credential_realm': self.callerArgs.data.get('credential_realm', [''])[0],
                        'token_url':        self.callerArgs.data.get('token_url',        [''])[0],
                        'apikey_param':     self.callerArgs.data.get('apikey_param',     [''])[0],
                    }

            url    = (call_cfg.get('url') or '').strip()
            method = (call_cfg.get('method') or 'GET').upper()
            policy = call_cfg.get('error_policy', 'default')

            if not url:
                raise ValueError('URL mancante nella configurazione della call')

            # ── Auth per-call ────────────────────────────────────────
            auth_headers, auth_params = resolve_call_auth(call_cfg, global_cfg, sk)

            # ── Build headers ────────────────────────────────────────
            extra_headers = {}
            if call_cfg.get('headers'):
                try:
                    extra_headers = json.loads(call_cfg['headers'])
                except Exception:
                    pass

            headers = {
                'User-Agent': 'Splunk-APIConnect/2.0',
                'Accept':     'application/json, text/csv, text/xml, */*',
            }
            headers.update(auth_headers)
            headers.update(extra_headers)

            # ── Body ─────────────────────────────────────────────────
            body_data = None
            if method in ('POST', 'PUT', 'PATCH') and call_cfg.get('body'):
                body_data = call_cfg['body'].encode('utf-8')
                if 'Content-Type' not in headers:
                    headers['Content-Type'] = 'application/json'

            # ── Auth query params ────────────────────────────────────
            if auth_params:
                sep = '&' if '?' in url else '?'
                url = url + sep + urllib.parse.urlencode(auth_params)

            # ── Execute ──────────────────────────────────────────────
            start_ts = time.time()
            req = urllib.request.Request(url, data=body_data,
                                          headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    latency_ms  = int((time.time() - start_ts) * 1000)
                    status_code = resp.status
                    ctype       = resp.headers.get('Content-Type', '')
                    raw_bytes   = resp.read()
            except urllib.error.HTTPError as e:
                latency_ms  = int((time.time() - start_ts) * 1000)
                status_code = e.code
                ctype       = e.headers.get('Content-Type', '')
                raw_bytes   = e.read()

            charset  = _extract_charset(ctype)
            body_str = raw_bytes.decode(charset, errors='replace')

            # ── CSV → JSON conversion ────────────────────────────────
            ctype_lower = ctype.lower()
            if 'text/csv' in ctype_lower or 'application/csv' in ctype_lower:
                try:
                    import csv, io
                    reader = csv.DictReader(io.StringIO(body_str))
                    rows   = list(reader)
                    body_str = json.dumps(rows[:200], indent=2, ensure_ascii=False)
                    ctype    = 'application/json (converted from CSV)'
                except Exception:
                    pass

            BODY_LIMIT = 100_000
            truncated  = len(body_str) > BODY_LIMIT

            result = {
                'status_code':  status_code,
                'latency_ms':   latency_ms,
                'content_type': ctype,
                'body':         body_str[:BODY_LIMIT],
                'truncated':    truncated,
            }

            confInfo['result']['payload'] = json.dumps(result)

        except Exception as e:
            logger.error('TestCallHandler: %s', str(e), exc_info=True)
            confInfo['result']['error']   = str(e)
            confInfo['result']['payload'] = json.dumps({
                'status_code':  0,
                'latency_ms':   0,
                'content_type': '',
                'body':         f'Errore lato server: {e}',
                'truncated':    False,
            })


# ════════════════════════════════════════════════════════════════
# InputsHandler  (proxy KV Store)
# ════════════════════════════════════════════════════════════════
class InputsHandler(admin.MConfigHandler):
    """
    Proxy REST → KV Store api_connect_inputs.
    Usato dalla dashboard JS per list/delete.
    """

    def setup(self):
        self.supportedArgs.addOptArg('key')

    def handleList(self, confInfo):
        try:
            _, content = rest.simpleRequest(
                '/servicesNS/nobody/api_connect/storage/collections/data/api_connect_inputs',
                sessionKey=self.getSessionKey(),
                getargs={'output_mode': 'json', 'count': 500},
            )
            confInfo['result']['data'] = content
        except Exception as e:
            confInfo['result']['error'] = str(e)

    def handleRemove(self, confInfo):
        key = self.callerArgs.id
        try:
            rest.simpleRequest(
                f'/servicesNS/nobody/api_connect/storage/collections/data/api_connect_inputs/{key}',
                sessionKey=self.getSessionKey(),
                method='DELETE',
            )
            # Tenta di disabilitare/rimuovere la stanza inputs.conf
            self._cleanup_inputs_conf(key)
        except Exception as e:
            confInfo['result']['error'] = str(e)

    def _cleanup_inputs_conf(self, key):
        """Tenta di disabilitare lo script corrispondente in inputs.conf."""
        try:
            script_name = f'ac_input_{key}.py'
            stanza = (
                f'script://$SPLUNK_HOME/etc/apps/api_connect/bin/{script_name}'
            )
            import urllib.parse
            enc = urllib.parse.quote(stanza, safe='')
            rest.simpleRequest(
                f'/servicesNS/nobody/api_connect/data/inputs/script/{enc}',
                sessionKey=self.getSessionKey(),
                postargs={'disabled': '1'},
                method='POST',
            )
        except Exception:
            pass  # Non critico
