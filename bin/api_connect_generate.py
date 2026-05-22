"""
api_connect_generate.py  v2
Genera lo script modular input usando la chain v2 (execute_chain).
"""

import datetime
import json
import logging
import os
import re
import sys
import textwrap
import urllib.parse

import splunk.admin as admin
import splunk.rest as rest

logger = logging.getLogger('splunk.api_connect.generate')


class GenerateInputHandler(admin.MConfigHandler):

    SCRIPT_HEADER = textwrap.dedent('''\
        #!/usr/bin/env python3
        """
        Modular Input generato da API Connect v2
        Input   : {name}
        Generato: {generated_at}

        NON modificare manualmente.
        """

        import sys, os
        _APP_BIN = os.path.dirname(os.path.abspath(__file__))
        if _APP_BIN not in sys.path:
            sys.path.insert(0, _APP_BIN)

        import json
        import splunklib.modularinput as smi
        from ac_logger import get_logger, ScriptRunContext
        from ac_http import (
            execute_chain, apply_field_mapping, format_event,
            load_checkpoint, save_checkpoint,
        )

        # ── Configurazione ───────────────────────────────────────────
        CONFIG = {config_json}
        # ────────────────────────────────────────────────────────────


        class APIConnectInput(smi.Script):

            def get_scheme(self):
                s = smi.Scheme("{name}")
                s.description = "API Connect v2: {name}"
                s.use_external_validation = False
                s.streaming_mode = smi.Scheme.streaming_mode_xml
                return s

            def validate_input(self, vd): pass

            def stream_events(self, inputs, ew):
                sk = self._input_definition.metadata.get('session_key', '')
                lg = get_logger(CONFIG.get('logger_source', 'api_connect:{name}:runner'))
                for input_name in inputs.inputs:
                    with ScriptRunContext(lg, input_name) as ctx:
                        ctx.set_count(self._run(input_name, ew, sk, lg))

            def _run(self, input_name, ew, sk, lg):
                cfg = CONFIG
                name = cfg.get('name', input_name)
                pagination_cfg = {{
                    'pagination_type': cfg.get('pagination_type','none'),
                    'page_param':      cfg.get('page_param','page'),
                    'cursor_path':     cfg.get('cursor_path',''),
                    'max_pages':       cfg.get('max_pages',100),
                    'array_root':      cfg.get('array_root',''),
                }}
                records = execute_chain(
                    calls=cfg.get('calls',[]),
                    global_cfg=cfg,
                    session_key=sk,
                    pagination_cfg=pagination_cfg,
                )
                checkpoint_enabled = cfg.get('checkpoint', False)
                checkpoint_field   = cfg.get('checkpoint_field', '')
                last_cp = load_checkpoint(name) if checkpoint_enabled else None
                new_cp  = last_cp
                sent = 0
                for record in records:
                    mapped = apply_field_mapping(record, cfg.get('field_mapping',{{}}))
                    if checkpoint_enabled and checkpoint_field:
                        cp_val = mapped.get(checkpoint_field) or record.get(checkpoint_field)
                        if cp_val is not None:
                            cp_str = str(cp_val)
                            if last_cp and cp_str <= last_cp: continue
                            if new_cp is None or cp_str > new_cp: new_cp = cp_str
                    ev = smi.Event()
                    ev.stanza    = input_name
                    ev.data      = format_event(mapped)
                    ev.index     = cfg.get('index','main')
                    ev.sourceType = cfg.get('sourcetype','api_connect')
                    ev.source    = cfg.get('source', input_name)
                    if cfg.get('host'): ev.host = cfg['host']
                    t = mapped.get('time')
                    if t:
                        try: ev.time = float(t)
                        except: pass
                    ew.write_event(ev)
                    sent += 1
                if checkpoint_enabled and new_cp and new_cp != last_cp:
                    save_checkpoint(name, new_cp)
                    lg.info('Checkpoint: %s', new_cp)
                return sent


        if __name__ == '__main__':
            sys.exit(APIConnectInput().run(sys.argv))
    ''')

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
                raise ValueError('Nome non valido: lettere/numeri/underscore, inizia con lettera')

            generated_at = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
            config_json  = json.dumps(cfg, indent=4, ensure_ascii=False)

            script_content = self.SCRIPT_HEADER.format(
                name=name,
                generated_at=generated_at,
                config_json=config_json,
            )

            app_dir = os.path.join(
                os.environ.get('SPLUNK_HOME', '/opt/splunk'),
                'etc', 'apps', 'api_connect', 'bin'
            )
            script_filename = f'ac_input_{name}.py'
            script_path = os.path.join(app_dir, script_filename)

            with open(script_path, 'w', encoding='utf-8') as f:
                f.write(script_content)
            os.chmod(script_path, 0o755)

            stanza = f'script://$SPLUNK_HOME/etc/apps/api_connect/bin/{script_filename}'
            self._register_inputs_conf(name, cfg, stanza)

            # Save to KV Store
            calls = cfg.get('calls', [{}])
            kv = {
                'name': name,
                'config': config_raw,
                'endpoint_url': calls[0].get('url', '') if calls else '',
                'auth_type': cfg.get('auth_type', ''),
                'credential_realm': cfg.get('credential_realm', ''),
                'call_count': str(len(calls)),
                'schedule': cfg.get('schedule', ''),
                'index': cfg.get('index', ''),
                'sourcetype': cfg.get('sourcetype', ''),
                'source': cfg.get('source', ''),
                'logger_source': cfg.get('logger_source', ''),
                'last_status': 'CONFIGURED',
                'last_run': '', 'last_count': '0', 'last_latency_ms': '0',
                'enabled': 'true',
            }
            kv_url = '/servicesNS/nobody/api_connect/storage/collections/data/api_connect_inputs'
            if edit_key:
                kv_url += f'/{edit_key}'
            rest.simpleRequest(kv_url, sessionKey=self.getSessionKey(),
                               jsonargs=json.dumps(kv), method='POST')

            preview = script_content[:4000]
            if len(script_content) > 4000:
                preview += '\n# … (troncato)'

            confInfo['result']['script_path']    = script_path
            confInfo['result']['stanza']         = stanza
            confInfo['result']['script_preview'] = preview
            confInfo['result']['success']        = 'true'

        except Exception as e:
            logger.error('GenerateInputHandler: %s', e, exc_info=True)
            confInfo['result']['error']   = str(e)
            confInfo['result']['success'] = 'false'

    def _register_inputs_conf(self, name, cfg, stanza):
        url     = '/servicesNS/nobody/api_connect/data/inputs/script'
        payload = {
            'name':       stanza,
            'interval':   str(self._cron_to_seconds(cfg.get('schedule', '300'))),
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
            try:
                enc = urllib.parse.quote(stanza, safe='')
                rest.simpleRequest(f'{url}/{enc}', sessionKey=self.getSessionKey(),
                                   postargs=payload, method='POST')
            except Exception as e2:
                logger.warning('inputs.conf update: %s', e2)

    def _cron_to_seconds(self, expr):
        try:
            return int(expr)
        except (ValueError, TypeError):
            pass
        expr = (expr or '').strip()
        m = re.match(r'^\*/(\d+)\s+\*\s+\*\s+\*\s+\*$', expr)
        if m: return int(m.group(1)) * 60
        m2 = re.match(r'^0\s+\*/(\d+)\s+\*\s+\*\s+\*$', expr)
        if m2: return int(m2.group(1)) * 3600
        if re.match(r'^0\s+0\s+\*\s+\*\s+\*$', expr): return 86400
        return 300
