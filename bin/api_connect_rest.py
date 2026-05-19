"""
api_connect_rest.py
Custom REST handlers per API Connect.

- TestCallHandler  : esegue una chiamata API lato server e restituisce risposta raw
- GenerateInputHandler : genera lo script Python modular input + aggiorna inputs.conf
- InputsHandler    : helper per operazioni KV Store (usato come proxy opzionale)
"""

import json
import os
import sys
import time
import csv
import io
import re
import logging

import splunk.admin as admin
import splunk.rest as rest

# Aggiungi bin al path per import di splunklib
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

logger = logging.getLogger('splunk.api_connect.rest')


# ============================================================
# Handler: Test Call
# ============================================================
class TestCallHandler(admin.MConfigHandler):
    """
    POST /servicesNS/nobody/api_connect/api_connect_test
    Riceve la configurazione della chiamata, la esegue lato server
    e restituisce la risposta raw al browser.
    """

    def setup(self):
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
            auth_type = self.callerArgs.data.get('auth_type', [''])[0] or ''
            credential_realm = self.callerArgs.data.get('credential_realm', [''])[0] or ''
            token_url = self.callerArgs.data.get('token_url', [''])[0] or ''
            apikey_param = self.callerArgs.data.get('apikey_param', [''])[0] or ''
            calls_raw = self.callerArgs.data.get('calls', ['[]'])[0] or '[]'
            calls = json.loads(calls_raw)

            if not calls:
                raise ValueError('Nessuna chiamata configurata')

            # Resolve credentials
            username, secret = self._get_credential(credential_realm)

            # Build auth headers/params
            auth_headers, auth_params = self._build_auth(
                auth_type, username, secret, token_url, apikey_param
            )

            # Execute first call (test mode)
            call = calls[0]
            url = call.get('url', '').strip()
            method = call.get('method', 'GET').upper()

            extra_headers = {}
            if call.get('headers'):
                try:
                    extra_headers = json.loads(call['headers'])
                except Exception:
                    pass

            headers = {'User-Agent': 'Splunk-APIConnect/1.0', 'Accept': '*/*'}
            headers.update(auth_headers)
            headers.update(extra_headers)

            body_data = None
            if method in ('POST', 'PUT', 'PATCH') and call.get('body'):
                body_str = call['body']
                body_data = body_str.encode('utf-8')
                if 'Content-Type' not in headers:
                    headers['Content-Type'] = 'application/json'

            # Add query params if auth_params
            if auth_params:
                sep = '&' if '?' in url else '?'
                url = url + sep + urllib.parse.urlencode(auth_params)

            req = urllib.request.Request(url, data=body_data, headers=headers, method=method)

            start_ts = time.time()
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    latency_ms = int((time.time() - start_ts) * 1000)
                    status_code = resp.status
                    content_type = resp.headers.get('Content-Type', '')
                    raw_bytes = resp.read()
            except urllib.error.HTTPError as e:
                latency_ms = int((time.time() - start_ts) * 1000)
                status_code = e.code
                content_type = e.headers.get('Content-Type', '')
                raw_bytes = e.read()

            # Decode response
            charset = 'utf-8'
            if 'charset=' in content_type:
                charset = content_type.split('charset=')[-1].split(';')[0].strip()

            body_str = raw_bytes.decode(charset, errors='replace')

            # If CSV, convert to JSON for easier tree viewing
            if 'text/csv' in content_type or 'application/csv' in content_type:
                body_str = self._csv_to_json_str(body_str)
                content_type = 'application/json (converted from CSV)'

            result = {
                'status_code': status_code,
                'latency_ms': latency_ms,
                'content_type': content_type,
                'body': body_str[:50000],  # cap at 50KB for display
                'truncated': len(body_str) > 50000
            }

            confInfo['result']['payload'] = json.dumps(result)

        except Exception as e:
            logger.error('TestCallHandler error: %s', str(e), exc_info=True)
            confInfo['result']['error'] = str(e)

    def _get_credential(self, realm):
        if not realm:
            return '', ''
        try:
            _, content = rest.simpleRequest(
                '/servicesNS/nobody/api_connect/storage/passwords',
                sessionKey=self.getSessionKey(),
                getargs={'count': 200, 'output_mode': 'json'}
            )
            data = json.loads(content)
            for entry in data.get('entry', []):
                if entry.get('content', {}).get('realm') == realm:
                    c = entry['content']
                    username = c.get('username', '')
                    clear_pw = c.get('clear_password', '')
                    # Strip meta prefix if present
                    if '||' in username:
                        username = username.split('||', 1)[1]
                    return username, clear_pw
        except Exception as e:
            logger.warning('_get_credential error: %s', str(e))
        return '', ''

    def _build_auth(self, auth_type, username, secret, token_url, apikey_param):
        headers = {}
        params = {}
        if auth_type == 'bearer':
            headers['Authorization'] = 'Bearer ' + secret
        elif auth_type == 'basic':
            import base64
            token = base64.b64encode((username + ':' + secret).encode()).decode()
            headers['Authorization'] = 'Basic ' + token
        elif auth_type == 'api_key_header':
            headers[apikey_param or 'X-API-Key'] = secret
        elif auth_type == 'api_key_query':
            params[apikey_param or 'api_key'] = secret
        elif auth_type == 'oauth2_cc':
            token = self._get_oauth2_token(token_url, username, secret)
            if token:
                headers['Authorization'] = 'Bearer ' + token
        return headers, params

    def _get_oauth2_token(self, token_url, client_id, client_secret):
        import urllib.request
        import urllib.parse
        import base64
        try:
            body = urllib.parse.urlencode({'grant_type': 'client_credentials'}).encode()
            creds = base64.b64encode((client_id + ':' + client_secret).encode()).decode()
            req = urllib.request.Request(
                token_url, data=body,
                headers={'Authorization': 'Basic ' + creds, 'Content-Type': 'application/x-www-form-urlencoded'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                return data.get('access_token', '')
        except Exception as e:
            logger.error('OAuth2 token fetch error: %s', str(e))
            return ''

    def _csv_to_json_str(self, csv_str):
        try:
            reader = csv.DictReader(io.StringIO(csv_str))
            rows = list(reader)
            return json.dumps(rows, indent=2, ensure_ascii=False)
        except Exception:
            return csv_str


# ============================================================
# Handler: Generate Input
# ============================================================
class GenerateInputHandler(admin.MConfigHandler):
    """
    POST /servicesNS/nobody/api_connect/api_connect_generate
    Riceve la configurazione completa, genera lo script Python
    e aggiorna inputs.conf tramite REST.
    """

    SCRIPT_TEMPLATE = '''\
#!/usr/bin/env python3
"""
Modular Input generato da API Connect
Input: {name}
Generato: {generated_at}
"""

import sys
import os
import json
import time
import logging
import csv
import io
import re
import urllib.request
import urllib.error
import urllib.parse
import base64

# Splunk modular input SDK
sys.path.insert(0, os.path.join(os.environ.get('SPLUNK_HOME', '/opt/splunk'), 'lib', 'python3.x', 'site-packages'))

import splunklib.client as client
import splunklib.modularinput as smi

# ---- Logger standard ----
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s level=%(levelname)s source={logger_source} message="%(message)s"'
)
logger = logging.getLogger('api_connect.{name}')

# ---- Configuration (generata dal wizard) ----
CONFIG = {config_json}

TRACCIATO_FIELDS = ['time', 'hostname', 'nomeapp', 'tipoazione', 'clientip',
                    'username', 'tipooperazione', 'valorePrima', 'valoreDP', 'target', 'note']


class APIConnectInput(smi.Script):

    def get_scheme(self):
        scheme = smi.Scheme("{name}")
        scheme.description = "API Connect modular input: {name}"
        scheme.use_external_validation = False
        scheme.streaming_mode = smi.Scheme.streaming_mode_xml
        return scheme

    def validate_input(self, validation_definition):
        pass

    def stream_events(self, inputs, ew):
        for input_name, input_item in inputs.inputs.items():
            try:
                self._run(input_name, input_item, ew)
            except Exception as e:
                logger.error('stream_events error: %s', str(e), exc_info=True)

    def _run(self, input_name, input_item, ew):
        cfg = CONFIG
        session_key = self._input_definition.metadata.get('session_key', '')

        auth_headers, auth_params = self._build_auth(cfg, session_key)
        all_records = []

        calls = cfg.get('calls', [])
        prev_output = {}

        for idx, call_cfg in enumerate(calls):
            url = self._interpolate(call_cfg.get('url', ''), prev_output)
            method = call_cfg.get('method', 'GET').upper()
            extra_headers = {{}}
            if call_cfg.get('headers'):
                try:
                    extra_headers = json.loads(call_cfg['headers'])
                except Exception:
                    pass

            headers = {{'User-Agent': 'Splunk-APIConnect/1.0', 'Accept': '*/*'}}
            headers.update(auth_headers)
            headers.update(extra_headers)

            body_data = None
            if method in ('POST', 'PUT', 'PATCH') and call_cfg.get('body'):
                body_str = self._interpolate(call_cfg['body'], prev_output)
                body_data = body_str.encode('utf-8')
                if 'Content-Type' not in headers:
                    headers['Content-Type'] = 'application/json'

            # Pagination loop
            pagination_type = cfg.get('pagination_type', 'none')
            max_pages = int(cfg.get('max_pages', 100))
            page_param = cfg.get('page_param', 'page')
            cursor_path = cfg.get('cursor_path', '')

            page = 0
            cursor = None

            while True:
                page_url = url
                if pagination_type == 'offset' and page > 0:
                    sep = '&' if '?' in page_url else '?'
                    page_url = page_url + sep + page_param + '=' + str(page)
                elif pagination_type == 'cursor' and cursor:
                    sep = '&' if '?' in page_url else '?'
                    page_url = page_url + sep + page_param + '=' + urllib.parse.quote(str(cursor))

                if auth_params:
                    sep = '&' if '?' in page_url else '?'
                    page_url = page_url + sep + urllib.parse.urlencode(auth_params)

                response_body, status_code, content_type = self._do_request(
                    page_url, method, headers, body_data
                )

                if status_code < 200 or status_code >= 300:
                    logger.error('HTTP %d from %s', status_code, page_url)
                    break

                # Parse response
                records, next_cursor = self._parse_response(response_body, content_type, cfg, cursor_path)
                all_records.extend(records)
                prev_output = records[0] if records else {{}}

                if pagination_type == 'none':
                    break
                if pagination_type in ('offset', 'cursor'):
                    if not records or not next_cursor:
                        break
                    cursor = next_cursor
                    page += 1
                    if page >= max_pages:
                        logger.warning('Raggiunto max_pages=%d', max_pages)
                        break
                elif pagination_type == 'link_header':
                    # Implemented via response headers (simplified)
                    break
                else:
                    break

        # Checkpoint dedup
        checkpoint_enabled = cfg.get('checkpoint', False)
        checkpoint_field = cfg.get('checkpoint_field', '')
        last_checkpoint = self._load_checkpoint(input_name) if checkpoint_enabled else None

        sent = 0
        new_checkpoint = last_checkpoint

        for record in all_records:
            mapped = self._apply_tracciato(record, cfg.get('field_mapping', {{}}))

            # Checkpoint check
            if checkpoint_enabled and checkpoint_field and checkpoint_field in mapped:
                val = mapped[checkpoint_field]
                if last_checkpoint and str(val) <= str(last_checkpoint):
                    continue
                if new_checkpoint is None or str(val) > str(new_checkpoint):
                    new_checkpoint = str(val)

            event_str = ' '.join(
                k + '=' + json.dumps(str(v)) for k, v in mapped.items() if v is not None and v != ''
            )
            event = smi.Event()
            event.stanza = input_name
            event.data = event_str
            if mapped.get('time'):
                try:
                    event.time = float(mapped['time'])
                except Exception:
                    pass
            ew.write_event(event)
            sent += 1

        if checkpoint_enabled and new_checkpoint:
            self._save_checkpoint(input_name, new_checkpoint)

        logger.info('Completato: %d record inviati a index=%s sourcetype=%s',
                    sent, cfg.get('index', ''), cfg.get('sourcetype', ''))

    def _do_request(self, url, method, headers, body_data):
        req = urllib.request.Request(url, data=body_data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode('utf-8', errors='replace'), resp.status, resp.headers.get('Content-Type', '')
        except urllib.error.HTTPError as e:
            return e.read().decode('utf-8', errors='replace'), e.code, ''

    def _parse_response(self, body, content_type, cfg, cursor_path):
        fmt = cfg.get('response_format', 'json')
        array_root = cfg.get('array_root', '')
        records = []
        next_cursor = None

        if 'text/csv' in content_type or fmt in ('csv', 'tsv'):
            delimiter = '\\t' if fmt == 'tsv' else ','
            reader = csv.DictReader(io.StringIO(body), delimiter=delimiter)
            records = list(reader)
        elif fmt == 'text':
            records = [{{'raw': line}} for line in body.splitlines() if line.strip()]
        else:
            try:
                data = json.loads(body)
                if array_root:
                    parts = [p for p in re.split(r'[\\.\\[\\]]+', array_root.lstrip('$.')) if p]
                    for part in parts:
                        if isinstance(data, dict):
                            data = data.get(part, data)
                        elif isinstance(data, list):
                            try: data = data[int(part)]
                            except Exception: break
                if isinstance(data, list):
                    records = data
                elif isinstance(data, dict):
                    records = [data]
                else:
                    records = [{{'value': data}}]

                # Extract cursor
                if cursor_path:
                    try:
                        c_parts = [p for p in re.split(r'[\\.\\[\\]]+', cursor_path.lstrip('$.')) if p]
                        c_data = json.loads(body)
                        for part in c_parts:
                            if isinstance(c_data, dict): c_data = c_data.get(part)
                            elif isinstance(c_data, list): c_data = c_data[int(part)]
                        next_cursor = c_data
                    except Exception:
                        pass
            except json.JSONDecodeError:
                records = [{{'raw': body}}]

        return records, next_cursor

    def _apply_tracciato(self, record, field_mapping):
        result = {{}}
        for std_field, src_path in field_mapping.items():
            if not src_path:
                continue
            # Navigate dotted path
            val = self._extract_value(record, src_path)
            result[std_field] = val

        # Pass-through unmapped fields
        for k, v in record.items():
            if k not in result:
                result[k] = v

        return result

    def _extract_value(self, record, path):
        if not path or not isinstance(record, dict):
            return None
        parts = [p for p in re.split(r'[\\.\\[\\]]+', path.lstrip('$.')) if p]
        val = record
        for part in parts:
            if isinstance(val, dict):
                val = val.get(part)
            elif isinstance(val, list):
                try: val = val[int(part)]
                except Exception: return None
            else:
                return None
        return val

    def _interpolate(self, template, context):
        def replacer(m):
            key = m.group(1).strip()
            parts = key.split('.')
            val = context
            for p in parts:
                if isinstance(val, dict): val = val.get(p, '')
                else: val = ''
            return str(val) if val is not None else ''
        return re.sub(r'\\{{\\{{([^}}]+)\\}}\\}}', replacer, template)

    def _build_auth(self, cfg, session_key):
        auth_type = cfg.get('auth_type', '')
        realm = cfg.get('credential_realm', '')
        username, secret = self._get_credential(realm, session_key)
        headers = {{}}
        params = {{}}
        if auth_type == 'bearer':
            headers['Authorization'] = 'Bearer ' + secret
        elif auth_type == 'basic':
            token = base64.b64encode((username + ':' + secret).encode()).decode()
            headers['Authorization'] = 'Basic ' + token
        elif auth_type == 'api_key_header':
            headers[cfg.get('apikey_param', 'X-API-Key')] = secret
        elif auth_type == 'api_key_query':
            params[cfg.get('apikey_param', 'api_key')] = secret
        elif auth_type == 'oauth2_cc':
            token = self._get_oauth2_token(
                cfg.get('token_url', ''), username, secret
            )
            if token:
                headers['Authorization'] = 'Bearer ' + token
        return headers, params

    def _get_credential(self, realm, session_key):
        if not realm or not session_key:
            return '', ''
        try:
            svc = client.connect(token=session_key)
            for pw in svc.storage_passwords:
                if pw.realm == realm:
                    uname = pw['username']
                    if '||' in uname:
                        uname = uname.split('||', 1)[1]
                    return uname, pw['clear_password']
        except Exception as e:
            logger.error('_get_credential: %s', str(e))
        return '', ''

    def _get_oauth2_token(self, token_url, client_id, client_secret):
        if not token_url:
            return ''
        try:
            body = urllib.parse.urlencode({{'grant_type': 'client_credentials'}}).encode()
            creds = base64.b64encode((client_id + ':' + client_secret).encode()).decode()
            req = urllib.request.Request(
                token_url, data=body,
                headers={{'Authorization': 'Basic ' + creds,
                          'Content-Type': 'application/x-www-form-urlencoded'}},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                return data.get('access_token', '')
        except Exception as e:
            logger.error('OAuth2 error: %s', str(e))
            return ''

    def _load_checkpoint(self, input_name):
        path = self._checkpoint_path(input_name)
        try:
            with open(path, 'r') as f:
                return f.read().strip()
        except FileNotFoundError:
            return None

    def _save_checkpoint(self, input_name, value):
        path = self._checkpoint_path(input_name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(str(value))

    def _checkpoint_path(self, input_name):
        base = os.path.join(
            os.environ.get('SPLUNK_HOME', '/opt/splunk'),
            'var', 'lib', 'splunk', 'modinputs', 'api_connect'
        )
        safe = re.sub(r'[^a-zA-Z0-9_\\-]', '_', input_name)
        return os.path.join(base, safe + '.checkpoint')


if __name__ == '__main__':
    sys.exit(APIConnectInput().run(sys.argv))
'''

    def setup(self):
        self.supportedArgs.addOptArg('config')
        self.supportedArgs.addOptArg('_key')

    def handleCreate(self, confInfo):
        import datetime

        try:
            config_raw = self.callerArgs.data.get('config', ['{}'])[0] or '{}'
            edit_key = self.callerArgs.data.get('_key', [None])[0]
            cfg = json.loads(config_raw)

            name = cfg.get('name', '').strip()
            if not name:
                raise ValueError('Nome input mancante')

            # Validate name (alphanumeric + underscore)
            if not re.match(r'^[a-zA-Z][a-zA-Z0-9_]*$', name):
                raise ValueError('Nome input non valido: usare solo lettere, numeri e underscore, iniziare con lettera')

            generated_at = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')

            # Generate script content
            script_content = self.SCRIPT_TEMPLATE.format(
                name=name,
                generated_at=generated_at,
                logger_source=cfg.get('logger_source', 'api_connect:' + name),
                config_json=json.dumps(cfg, indent=4, ensure_ascii=False)
            )

            # Write script to bin/
            app_dir = os.path.join(
                os.environ.get('SPLUNK_HOME', '/opt/splunk'),
                'etc', 'apps', 'api_connect', 'bin'
            )
            script_filename = 'ac_input_' + name + '.py'
            script_path = os.path.join(app_dir, script_filename)

            with open(script_path, 'w', encoding='utf-8') as f:
                f.write(script_content)
            os.chmod(script_path, 0o755)

            # Register modular input scheme
            stanza_name = 'script://$SPLUNK_HOME/etc/apps/api_connect/bin/' + script_filename
            self._register_inputs_conf(name, cfg, stanza_name)

            # Save to KV Store
            kv_payload = {
                'name': name,
                'config': config_raw,
                'endpoint_url': (cfg.get('calls') or [{}])[0].get('url', ''),
                'auth_type': cfg.get('auth_type', ''),
                'credential_realm': cfg.get('credential_realm', ''),
                'schedule': cfg.get('schedule', ''),
                'index': cfg.get('index', ''),
                'sourcetype': cfg.get('sourcetype', ''),
                'source': cfg.get('source', ''),
                'logger_source': cfg.get('logger_source', ''),
                'last_status': 'CONFIGURED',
                'last_run': '',
                'last_count': '0',
                'last_latency_ms': '0',
                'enabled': 'true'
            }

            kv_url = '/servicesNS/nobody/api_connect/storage/collections/data/api_connect_inputs'
            if edit_key:
                kv_url += '/' + edit_key

            _, content = rest.simpleRequest(
                kv_url,
                sessionKey=self.getSessionKey(),
                jsonargs=json.dumps(kv_payload),
                method='POST' if not edit_key else 'POST'
            )

            script_preview = script_content[:3000] + ('\n... (troncato per visualizzazione)' if len(script_content) > 3000 else '')

            confInfo['result']['script_path'] = script_path
            confInfo['result']['stanza'] = stanza_name
            confInfo['result']['script_preview'] = script_preview
            confInfo['result']['success'] = 'true'

        except Exception as e:
            logger.error('GenerateInputHandler error: %s', str(e), exc_info=True)
            confInfo['result']['error'] = str(e)
            confInfo['result']['success'] = 'false'

    def _register_inputs_conf(self, name, cfg, stanza_name):
        """Crea o aggiorna la stanza in inputs.conf via REST."""
        url = '/servicesNS/nobody/api_connect/data/inputs/script'
        payload = {
            'name': stanza_name,
            'interval': self._cron_to_seconds(cfg.get('schedule', '300')),
            'index': cfg.get('index', 'main'),
            'sourcetype': cfg.get('sourcetype', 'api_connect'),
            'source': cfg.get('source', stanza_name),
            'disabled': '0',
            'passAuth': 'splunk-system-user'
        }
        if cfg.get('host'):
            payload['host'] = cfg['host']

        try:
            rest.simpleRequest(url, sessionKey=self.getSessionKey(), postargs=payload, method='POST')
        except Exception as e:
            logger.warning('inputs.conf registration warning (may already exist): %s', str(e))
            # Try update
            try:
                update_url = url + '/' + urllib.parse.quote(stanza_name, safe='')
                rest.simpleRequest(update_url, sessionKey=self.getSessionKey(), postargs=payload, method='POST')
            except Exception:
                pass

    def _cron_to_seconds(self, cron):
        """Estrae l'intervallo in secondi da espressioni cron semplici come */5 * * * *"""
        if not cron:
            return 300
        try:
            return int(cron)
        except ValueError:
            pass
        m = re.match(r'^\*/(\d+)\s+\*\s+\*\s+\*\s+\*$', cron.strip())
        if m:
            return int(m.group(1)) * 60
        m2 = re.match(r'^0\s+\*/(\d+)\s+\*\s+\*\s+\*$', cron.strip())
        if m2:
            return int(m2.group(1)) * 3600
        return 300


# ============================================================
# Handler: Inputs (proxy KV Store)
# ============================================================
class InputsHandler(admin.MConfigHandler):

    def setup(self):
        self.supportedArgs.addOptArg('key')

    def handleList(self, confInfo):
        try:
            _, content = rest.simpleRequest(
                '/servicesNS/nobody/api_connect/storage/collections/data/api_connect_inputs',
                sessionKey=self.getSessionKey(),
                getargs={'output_mode': 'json', 'count': 500}
            )
            data = json.loads(content)
            confInfo['result']['data'] = content
        except Exception as e:
            confInfo['result']['error'] = str(e)

    def handleRemove(self, confInfo):
        key = self.callerArgs.id
        try:
            _, content = rest.simpleRequest(
                '/servicesNS/nobody/api_connect/storage/collections/data/api_connect_inputs/' + key,
                sessionKey=self.getSessionKey(),
                method='DELETE'
            )
        except Exception as e:
            confInfo['result']['error'] = str(e)
