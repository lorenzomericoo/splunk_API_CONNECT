"""
api_connect_generate.py
GenerateInputHandler separato — genera lo script Python finale
sostituendo il template ac_input_template.py con la configurazione reale
dell'input, e registra la stanza in inputs.conf via REST.
"""

import json
import os
import re
import sys
import textwrap
import urllib.parse
import logging
import datetime

import splunk.admin as admin
import splunk.rest as rest

logger = logging.getLogger('splunk.api_connect.generate')


class GenerateInputHandler(admin.MConfigHandler):

    SCRIPT_HEADER = textwrap.dedent("""\
        #!/usr/bin/env python3
        \"\"\"
        Modular Input generato da API Connect
        Input   : {name}
        Generato: {generated_at}
        
        NON modificare manualmente: questo file viene rigenerato
        dal wizard API Connect ogni volta che si salva l'input.
        Per modificare la configurazione usa l'interfaccia web.
        \"\"\"

        import sys
        import os

        _APP_BIN = os.path.dirname(os.path.abspath(__file__))
        if _APP_BIN not in sys.path:
            sys.path.insert(0, _APP_BIN)

        import splunklib.modularinput as smi

        from ac_logger import get_logger, ScriptRunContext
        from ac_http import (
            build_auth, fetch_all_pages, parse_response,
            apply_field_mapping, interpolate, format_event,
            load_checkpoint, save_checkpoint,
        )

        # ── Configurazione generata dal wizard ──────────────────────────
        CONFIG = {config_json}
        # ────────────────────────────────────────────────────────────────


        class APIConnectInput(smi.Script):

            def get_scheme(self):
                scheme = smi.Scheme("{name}")
                scheme.description = "API Connect input: {name}"
                scheme.use_external_validation = False
                scheme.streaming_mode = smi.Scheme.streaming_mode_xml
                return scheme

            def validate_input(self, validation_definition):
                pass

            def stream_events(self, inputs, ew):
                session_key = self._input_definition.metadata.get('session_key', '')
                logger = get_logger(CONFIG.get('logger_source', 'api_connect:{name}:runner'))
                for input_name in inputs.inputs:
                    with ScriptRunContext(logger, input_name) as ctx:
                        count = self._run_input(input_name, ew, session_key, logger)
                        ctx.set_count(count)

            def _run_input(self, input_name, ew, session_key, logger):
                cfg = CONFIG
                name = cfg.get('name', input_name)

                auth_headers, auth_params = build_auth(cfg, session_key)

                checkpoint_enabled = cfg.get('checkpoint', False)
                checkpoint_field   = cfg.get('checkpoint_field', '')
                last_cp  = load_checkpoint(name) if checkpoint_enabled else None
                new_cp   = last_cp

                calls = cfg.get('calls', [])
                pagination_cfg = {{
                    'pagination_type': cfg.get('pagination_type', 'none'),
                    'page_param':      cfg.get('page_param', 'page'),
                    'cursor_path':     cfg.get('cursor_path', ''),
                    'max_pages':       cfg.get('max_pages', 100),
                    'array_root':      cfg.get('array_root', ''),
                }}

                sent = 0
                prev_record = {{}}

                for call_idx, call_cfg in enumerate(calls):
                    resolved = dict(call_cfg)
                    resolved['url']  = interpolate(call_cfg.get('url', ''),  prev_record)
                    if call_cfg.get('body'):
                        resolved['body'] = interpolate(call_cfg['body'], prev_record)

                    logger.info('Chiamata %d/%d url=%s', call_idx+1, len(calls), resolved['url'])

                    page_records = []
                    for body, ctype in fetch_all_pages(
                            resolved, auth_headers, auth_params, pagination_cfg):
                        page_records.extend(parse_response(body, ctype, cfg))

                    logger.info('Chiamata %d: %d record grezzi', call_idx+1, len(page_records))

                    for record in page_records:
                        mapped = apply_field_mapping(record, cfg.get('field_mapping', {{}}))

                        if checkpoint_enabled and checkpoint_field:
                            cp_val = mapped.get(checkpoint_field) or record.get(checkpoint_field)
                            if cp_val is not None:
                                cp_str = str(cp_val)
                                if last_cp and cp_str <= last_cp:
                                    continue
                                if new_cp is None or cp_str > new_cp:
                                    new_cp = cp_str

                        event_str = format_event(mapped)
                        ev = smi.Event()
                        ev.stanza    = input_name
                        ev.data      = event_str
                        ev.index     = cfg.get('index', 'main')
                        ev.sourceType = cfg.get('sourcetype', 'api_connect')
                        ev.source    = cfg.get('source', input_name)
                        if cfg.get('host'):
                            ev.host  = cfg['host']

                        t = mapped.get('time')
                        if t:
                            try: ev.time = float(t)
                            except (ValueError, TypeError): pass

                        ew.write_event(ev)
                        sent += 1

                    if page_records:
                        prev_record = page_records[-1]

                if checkpoint_enabled and new_cp and new_cp != last_cp:
                    save_checkpoint(name, new_cp)
                    logger.info('Checkpoint aggiornato: %s', new_cp)

                return sent


        if __name__ == '__main__':
            sys.exit(APIConnectInput().run(sys.argv))
    """)

    def setup(self):
        self.supportedArgs.addOptArg('config')
        self.supportedArgs.addOptArg('_key')

    def handleCreate(self, confInfo):
        try:
            config_raw = self.callerArgs.data.get('config', ['{}'])[0] or '{}'
            edit_key   = self.callerArgs.data.get('_key', [None])[0]
            cfg = json.loads(config_raw)

            name = cfg.get('name', '').strip()
            if not name:
                raise ValueError('Nome input mancante')
            if not re.match(r'^[a-zA-Z][a-zA-Z0-9_]*$', name):
                raise ValueError(
                    'Nome non valido: solo lettere/numeri/underscore, iniziare con lettera')

            generated_at = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')

            # Serializza CONFIG con indent per leggibilità
            config_json = json.dumps(cfg, indent=4, ensure_ascii=False)

            script_content = self.SCRIPT_HEADER.format(
                name=name,
                generated_at=generated_at,
                config_json=config_json,
            )

            # Scrivi lo script in bin/
            app_dir = os.path.join(
                os.environ.get('SPLUNK_HOME', '/opt/splunk'),
                'etc', 'apps', 'api_connect', 'bin'
            )
            script_filename = f'ac_input_{name}.py'
            script_path = os.path.join(app_dir, script_filename)

            with open(script_path, 'w', encoding='utf-8') as f:
                f.write(script_content)
            os.chmod(script_path, 0o755)

            # Stanza inputs.conf
            stanza = f'script://$SPLUNK_HOME/etc/apps/api_connect/bin/{script_filename}'
            self._register_inputs_conf(name, cfg, stanza)

            # Salva / aggiorna KV Store
            kv_payload = {
                'name': name,
                'label': cfg.get('label', name),
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
                'enabled': 'true',
            }

            kv_url = '/servicesNS/nobody/api_connect/storage/collections/data/api_connect_inputs'
            if edit_key:
                kv_url += f'/{edit_key}'

            rest.simpleRequest(
                kv_url,
                sessionKey=self.getSessionKey(),
                jsonargs=json.dumps(kv_payload),
                method='POST',
            )

            preview = script_content[:4000]
            if len(script_content) > 4000:
                preview += '\n\n# ... (troncato — file completo in bin/)'

            confInfo['result']['script_path']    = script_path
            confInfo['result']['stanza']         = stanza
            confInfo['result']['script_preview'] = preview
            confInfo['result']['success']        = 'true'

        except Exception as e:
            logger.error('GenerateInputHandler: %s', str(e), exc_info=True)
            confInfo['result']['error']   = str(e)
            confInfo['result']['success'] = 'false'

    # ------------------------------------------------------------------ #
    def _register_inputs_conf(self, name, cfg, stanza):
        """Registra (o aggiorna) la stanza script in inputs.conf via REST."""
        url = '/servicesNS/nobody/api_connect/data/inputs/script'

        interval = self._cron_to_seconds(cfg.get('schedule', '300'))
        payload = {
            'name':       stanza,
            'interval':   str(interval),
            'index':      cfg.get('index', 'main'),
            'sourcetype': cfg.get('sourcetype', 'api_connect'),
            'source':     cfg.get('source', stanza),
            'disabled':   '0',
            'passAuth':   'splunk-system-user',
        }
        if cfg.get('host'):
            payload['host'] = cfg['host']

        try:
            rest.simpleRequest(url, sessionKey=self.getSessionKey(),
                               postargs=payload, method='POST')
        except Exception:
            # Potrebbe esistere già — tenta update
            try:
                encoded = urllib.parse.quote(stanza, safe='')
                rest.simpleRequest(f'{url}/{encoded}',
                                   sessionKey=self.getSessionKey(),
                                   postargs=payload, method='POST')
            except Exception as e2:
                logger.warning('inputs.conf update: %s', e2)

    def _cron_to_seconds(self, expr):
        """Converte espressioni cron comuni in secondi per inputs.conf."""
        try:
            return int(expr)
        except (ValueError, TypeError):
            pass
        expr = (expr or '').strip()
        # */N * * * *  →  N*60
        m = re.match(r'^\*/(\d+)\s+\*\s+\*\s+\*\s+\*$', expr)
        if m:
            return int(m.group(1)) * 60
        # 0 */N * * *  →  N*3600
        m2 = re.match(r'^0\s+\*/(\d+)\s+\*\s+\*\s+\*$', expr)
        if m2:
            return int(m2.group(1)) * 3600
        # 0 0 * * *  →  86400
        if re.match(r'^0\s+0\s+\*\s+\*\s+\*$', expr):
            return 86400
        return 300  # default 5 min
