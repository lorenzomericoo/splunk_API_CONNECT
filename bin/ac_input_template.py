"""
ac_input_TEMPLATE.py
Template base per gli script generati dal wizard API Connect.

NON modificare questo file direttamente.
Gli script reali vengono generati da GenerateInputHandler
in api_connect_rest.py e scritti come:
  $SPLUNK_HOME/etc/apps/api_connect/bin/ac_input_<nome>.py

Questo template è documentativo e usato come riferimento
dal REST handler per la generazione via stringa.
"""

import sys
import os

# Assicura che il bin dell'app sia nel path per i moduli condivisi
_APP_BIN = os.path.dirname(os.path.abspath(__file__))
if _APP_BIN not in sys.path:
    sys.path.insert(0, _APP_BIN)

import json
import time

import splunklib.modularinput as smi

from ac_logger import get_logger, ScriptRunContext
from ac_http import (
    build_auth, fetch_all_pages, parse_response,
    apply_field_mapping, interpolate, format_event,
    load_checkpoint, save_checkpoint,
)

# ── Configurazione generata dal wizard ──────────────────────────────────────
# Questo blocco viene sostituito dal GenerateInputHandler con i dati reali.
CONFIG = {
    # Step 1 — Auth
    "name": "__TEMPLATE__",
    "auth_type": "bearer",                        # bearer | basic | api_key_header | api_key_query | oauth2_cc
    "credential_realm": "api_connect:bearer:__label__",
    "token_url": "",                              # solo per oauth2_cc
    "oauth_scope": "",
    "apikey_param": "X-API-Key",

    # Step 2 — Endpoint
    "calls": [
        {
            "url": "https://api.example.com/v1/events",
            "method": "GET",
            "headers": "{}",
            "body": "",
            "chain_input": ""                     # campo da passare alla call successiva
        }
    ],
    "pagination_type": "none",                   # none | offset | cursor | link_header
    "page_param": "page",
    "cursor_path": "$.next_cursor",
    "max_pages": 100,
    "schedule": "*/5 * * * *",

    # Step 4 — Parsing
    "response_format": "json",                   # json | json_array | csv | tsv | xml | text
    "array_root": "$.data[*]",
    "extracted_fields": [
        {"path": "$.data[*].id",        "alias": "id"},
        {"path": "$.data[*].timestamp", "alias": "timestamp"},
        {"path": "$.data[*].username",  "alias": "username"},
    ],

    # Step 5 — Tracciato
    "field_mapping": {
        "time":           "$.data[*].timestamp",
        "hostname":       "__static__:api.example.com",
        "nomeapp":        "__static__:ExampleApp",
        "tipoazione":     "$.data[*].action",
        "clientip":       "$.data[*].ip",
        "username":       "$.data[*].username",
        "tipooperazione": "",
        "valorePrima":    "",
        "valoreDP":       "",
        "target":         "$.data[*].resource",
        "note":           "",
    },

    # Step 6 — Output
    "index": "main",
    "sourcetype": "api_connect:example",
    "source": "https://api.example.com/v1/events",
    "host": "",
    "checkpoint": True,
    "checkpoint_field": "timestamp",

    # Step 7 — Logger
    "logger_source": "api_connect:example:runner",
}
# ────────────────────────────────────────────────────────────────────────────


class APIConnectInput(smi.Script):
    """
    Modular Input generato da API Connect.
    Eredita da splunklib.modularinput.Script per integrazione nativa
    con il framework Splunk.
    """

    def get_scheme(self) -> smi.Scheme:
        name = CONFIG.get('name', 'api_connect_input')
        scheme = smi.Scheme(name)
        scheme.description = f'API Connect input: {name}'
        scheme.use_external_validation = False
        scheme.streaming_mode = smi.Scheme.streaming_mode_xml
        return scheme

    def validate_input(self, validation_definition):
        pass  # Validazione gestita dal wizard

    def stream_events(self, inputs: smi.InputDefinition, ew: smi.EventWriter):
        session_key = self._input_definition.metadata.get('session_key', '')
        logger = get_logger(CONFIG.get('logger_source', 'api_connect:runner'))

        for input_name in inputs.inputs:
            with ScriptRunContext(logger, input_name) as ctx:
                count = self._run_input(input_name, ew, session_key, logger)
                ctx.set_count(count)

    def _run_input(self, input_name: str, ew: smi.EventWriter,
                   session_key: str, logger) -> int:
        cfg = CONFIG
        name = cfg.get('name', input_name)

        # Build auth once (OAuth2 token is cached per-run)
        auth_headers, auth_params = build_auth(cfg, session_key)

        # Checkpoint
        checkpoint_enabled = cfg.get('checkpoint', False)
        checkpoint_field = cfg.get('checkpoint_field', '')
        last_cp = load_checkpoint(name) if checkpoint_enabled else None
        new_cp = last_cp

        calls = cfg.get('calls', [])
        pagination_cfg = {
            'pagination_type': cfg.get('pagination_type', 'none'),
            'page_param': cfg.get('page_param', 'page'),
            'cursor_path': cfg.get('cursor_path', ''),
            'max_pages': cfg.get('max_pages', 100),
            'array_root': cfg.get('array_root', ''),
        }

        sent = 0
        prev_record = {}  # context for cascade calls

        for call_idx, call_cfg in enumerate(calls):
            # Interpolate cascade placeholders from previous call output
            resolved_call = dict(call_cfg)
            resolved_call['url'] = interpolate(call_cfg.get('url', ''), prev_record)
            if call_cfg.get('body'):
                resolved_call['body'] = interpolate(call_cfg['body'], prev_record)

            logger.info('Chiamata %d/%d url=%s', call_idx + 1, len(calls), resolved_call['url'])

            page_records = []
            for body, ctype in fetch_all_pages(resolved_call, auth_headers, auth_params, pagination_cfg):
                records = parse_response(body, ctype, cfg)
                page_records.extend(records)

            logger.info('Chiamata %d: %d record grezzi ricevuti', call_idx + 1, len(page_records))

            for record in page_records:
                # Apply tracciato mapping
                mapped = apply_field_mapping(record, cfg.get('field_mapping', {}))

                # Checkpoint dedup
                if checkpoint_enabled and checkpoint_field:
                    cp_val = mapped.get(checkpoint_field) or record.get(checkpoint_field)
                    if cp_val is not None:
                        cp_str = str(cp_val)
                        if last_cp and cp_str <= last_cp:
                            continue
                        if new_cp is None or cp_str > new_cp:
                            new_cp = cp_str

                # Format and write event
                event_str = format_event(mapped)
                event = smi.Event()
                event.stanza = input_name
                event.data = event_str

                # Set timestamp if time field is present
                t = mapped.get('time')
                if t:
                    try:
                        event.time = float(t)
                    except (ValueError, TypeError):
                        pass  # Splunk uses ingest time

                # Override index/host/sourcetype/source from config
                if cfg.get('index'):
                    event.index = cfg['index']
                if cfg.get('host'):
                    event.host = cfg['host']
                if cfg.get('sourcetype'):
                    event.sourceType = cfg['sourcetype']
                if cfg.get('source'):
                    event.source = cfg['source']

                ew.write_event(event)
                sent += 1

            # Use last record as context for next cascade call
            if page_records:
                prev_record = page_records[-1]

        # Save checkpoint
        if checkpoint_enabled and new_cp and new_cp != last_cp:
            save_checkpoint(name, new_cp)
            logger.info('Checkpoint aggiornato: %s', new_cp)

        return sent


if __name__ == '__main__':
    sys.exit(APIConnectInput().run(sys.argv))
