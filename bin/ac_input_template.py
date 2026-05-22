"""
ac_input_template.py  v2
Template per gli script generati dal wizard API Connect.

Novità v2:
- usa execute_chain() da ac_http per multi-call con auth per-call e join
- error policy per-call gestita in ac_http
- logging via ScriptRunContext
"""

import sys
import os

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

# ── Configurazione generata dal wizard ──────────────────────────
CONFIG = {
    "name": "__TEMPLATE__",

    # Step 1 — Auth globale
    "auth_type": "oauth2_cc",
    "credential_realm": "api_connect:oauth2_cc:erp_prod",
    "token_url": "https://auth.example.com/oauth/token",
    "oauth_scope": "read",
    "apikey_param": "",

    # Step 2 — Chain di call
    "calls": [
        {
            "id": 1,
            "name": "Lista transazioni",
            "url": "https://erp.corp.it/rest/txn",
            "method": "GET",
            "headers": '{"Accept": "application/json"}',
            "body": "",
            # inherited = usa auth globale
            "auth_type": "inherited",
            "credential_realm": "",
            "apikey_param": "",
            "error_policy": "retry_429",
            "join_key": ""
        },
        {
            "id": 2,
            "name": "Dettaglio transazione",
            "url": "https://erp.corp.it/rest/txn/{{id}}/details",
            "method": "GET",
            "headers": '{"Accept": "application/json"}',
            "body": "",
            # Override: questa call usa auth diversa
            "auth_type": "api_key_header",
            "credential_realm": "api_connect:api_key:erp_detail",
            "apikey_param": "X-Detail-Key",
            "error_policy": "skip_404",
            "join_key": "id"        # merge su campo "id"
        }
    ],

    # Paginazione (ultima call)
    "pagination_type": "offset",
    "page_param": "page",
    "cursor_path": "",
    "max_pages": 100,
    "schedule": "*/10 * * * *",

    # Step 3 — Parsing
    "response_format": "json",
    "array_root": "$.data[*]",
    "extracted_fields": [
        {"path": "$.data[*].id",        "alias": "id"},
        {"path": "$.data[*].timestamp", "alias": "timestamp"},
    ],

    # Step 4 — Tracciato
    "field_mapping": {
        "time":           "timestamp",
        "hostname":       "__static__:erp.corp.it",
        "nomeapp":        "__static__:ERP_CORP",
        "tipoazione":     "operation",
        "clientip":       "src_ip",
        "username":       "user_name",
        "tipooperazione": "",
        "valorePrima":    "prev_balance",
        "valoreDP":       "new_balance",
        "target":         "target_account",
        "note":           "",
    },

    # Step 5 — Output
    "index": "finance",
    "sourcetype": "api_connect:erp_transactions",
    "source": "https://erp.corp.it/rest/txn",
    "host": "",
    "checkpoint": True,
    "checkpoint_field": "timestamp",

    # Step 6 — Logger
    "logger_source": "api_connect:erp_transactions:runner",
}
# ────────────────────────────────────────────────────────────────


class APIConnectInput(smi.Script):

    def get_scheme(self):
        name   = CONFIG.get('name', 'api_connect_input')
        scheme = smi.Scheme(name)
        scheme.description     = f'API Connect input v2: {name}'
        scheme.use_external_validation = False
        scheme.streaming_mode  = smi.Scheme.streaming_mode_xml
        return scheme

    def validate_input(self, validation_definition):
        pass

    def stream_events(self, inputs, ew):
        session_key = self._input_definition.metadata.get('session_key', '')
        logger      = get_logger(CONFIG.get('logger_source', 'api_connect:runner'))

        for input_name in inputs.inputs:
            with ScriptRunContext(logger, input_name) as ctx:
                count = self._run_input(input_name, ew, session_key, logger)
                ctx.set_count(count)

    def _run_input(self, input_name, ew, session_key, logger):
        cfg  = CONFIG
        name = cfg.get('name', input_name)

        pagination_cfg = {
            'pagination_type': cfg.get('pagination_type', 'none'),
            'page_param':      cfg.get('page_param', 'page'),
            'cursor_path':     cfg.get('cursor_path', ''),
            'max_pages':       cfg.get('max_pages', 100),
            'array_root':      cfg.get('array_root', ''),
        }

        # Esegui la chain completa (auth per-call, join, enrichment)
        records = execute_chain(
            calls          = cfg.get('calls', []),
            global_cfg     = cfg,
            session_key    = session_key,
            pagination_cfg = pagination_cfg,
        )

        # Checkpoint dedup
        checkpoint_enabled = cfg.get('checkpoint', False)
        checkpoint_field   = cfg.get('checkpoint_field', '')
        last_cp  = load_checkpoint(name) if checkpoint_enabled else None
        new_cp   = last_cp

        sent = 0
        for record in records:
            mapped = apply_field_mapping(record, cfg.get('field_mapping', {}))

            # Checkpoint
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
            ev.stanza     = input_name
            ev.data       = event_str
            ev.index      = cfg.get('index', 'main')
            ev.sourceType = cfg.get('sourcetype', 'api_connect')
            ev.source     = cfg.get('source', input_name)
            if cfg.get('host'):
                ev.host   = cfg['host']

            t = mapped.get('time')
            if t:
                try:
                    ev.time = float(t)
                except (ValueError, TypeError):
                    pass

            ew.write_event(ev)
            sent += 1

        if checkpoint_enabled and new_cp and new_cp != last_cp:
            save_checkpoint(name, new_cp)
            logger.info('Checkpoint aggiornato: %s', new_cp)

        return sent


if __name__ == '__main__':
    sys.exit(APIConnectInput().run(sys.argv))
