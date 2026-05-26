"""
ac_input_template.py  v3
Template per gli script generati da API Connect.

Integra Sprint 1:
- ac_transforms: pipeline trasformazioni per-campo
- ac_transforms.build_event_string: output format configurabile (pipe/kv/json/csv)
- ac_token_cache: OAuth2 token caching con TTL
- ac_circuit_breaker: stop automatico su N errori consecutivi
- ac_metrics: aggiornamento KV Store dopo ogni run
- Retry-After respect su 429
"""

import sys
import os

_APP_BIN = os.path.dirname(os.path.abspath(__file__))
if _APP_BIN not in sys.path:
    sys.path.insert(0, _APP_BIN)

import json

import splunklib.modularinput as smi

from ac_logger import get_logger, ScriptRunContext
from ac_http import execute_chain_v3, apply_field_mapping, load_checkpoint, save_checkpoint
from ac_transforms import apply_all_transforms, build_event_string
from ac_metrics import RunMetrics

try:
    from ac_circuit_breaker import CircuitOpenError
except ImportError:
    class CircuitOpenError(Exception):
        pass

# ── Configurazione generata dal wizard ──────────────────────────
CONFIG = {
    "name": "__TEMPLATE__",

    # Step 1 — Auth globale
    "auth_type": "oauth2_cc",
    "credential_realm": "api_connect:oauth2_cc:erp_prod",
    "token_url": "https://auth.example.com/oauth/token",
    "oauth_scope": "read",
    "apikey_param": "",

    # Step 2 — Chain
    "calls": [
        {
            "id": 1, "name": "Lista",
            "url": "https://erp.corp.it/rest/txn",
            "method": "GET", "headers": "{}", "body": "",
            "auth_type": "inherited",
            "error_policy": "retry_429",
            "join_key": ""
        },
        {
            "id": 2, "name": "Dettaglio",
            "url": "https://erp.corp.it/rest/txn/{{id}}/details",
            "method": "GET", "headers": "{}", "body": "",
            "auth_type": "api_key_header",
            "credential_realm": "api_connect:api_key:erp_detail",
            "apikey_param": "X-Detail-Key",
            "error_policy": "skip_404",
            "join_key": "id"
        }
    ],

    # Paginazione
    "pagination_type": "offset",
    "page_param": "page",
    "cursor_path": "",
    "max_pages": 100,
    "schedule": "*/10 * * * *",

    # Step 3 — Parsing
    "response_format": "json",
    "array_root": "$.data[*]",

    # Step 4 — Tracciato
    "field_mapping": {
        "time":        "timestamp",
        "hostname":    "__static__:erp.corp.it",
        "nomeapp":     "__static__:ERP_CORP",
        "tipoazione":  "operation",
        "clientip":    "src_ip",
        "username":    "user_name",
        "valorePrima": "prev_balance",
        "valoreDP":    "new_balance",
        "target":      "target_account",
        "note":        "",
    },

    # Step 4b — Trasformazioni per-campo (pipeline)
    "field_transforms": {
        "time":     [{"fn": "iso_to_epoch"}],
        "username": [{"fn": "lower"}, {"fn": "strip"}],
        "valorePrima": [{"fn": "to_float"}, {"fn": "round", "decimals": 2}],
        "valoreDP":    [{"fn": "to_float"}, {"fn": "round", "decimals": 2}],
    },

    # Step 5 — Output format
    "output_config": {
        "format": "pipe",
        "fields": [
            "time","hostname","nomeapp","tipoazione",
            "clientip","username","tipooperazione",
            "valorePrima","valoreDP","target","note"
        ],
        "null_value": "",
        "include_extra": False,
    },

    # Step 5b — Destinazione
    "index": "finance",
    "sourcetype": "api_connect:erp_transactions",
    "source": "https://erp.corp.it/rest/txn",
    "host": "",
    "checkpoint": True,
    "checkpoint_field": "timestamp",

    # Step 6 — Logger
    "logger_source": "api_connect:erp_transactions:runner",

    # Circuit breaker (opzionale)
    "circuit_breaker": {
        "failure_threshold": 5,
        "cooldown_s": 120,
    },
}
# ────────────────────────────────────────────────────────────────


class APIConnectInput(smi.Script):

    def get_scheme(self):
        name   = CONFIG.get('name', 'api_connect_input')
        scheme = smi.Scheme(name)
        scheme.description     = f'API Connect v3: {name}'
        scheme.use_external_validation = False
        scheme.streaming_mode  = smi.Scheme.streaming_mode_xml
        return scheme

    def validate_input(self, validation_definition):
        pass

    def stream_events(self, inputs, ew):
        session_key = self._input_definition.metadata.get('session_key', '')
        logger      = get_logger(CONFIG.get('logger_source', 'api_connect:runner'))

        for input_name in inputs.inputs:
            metrics = RunMetrics(session_key, input_name)
            metrics.start()
            with ScriptRunContext(logger, input_name) as ctx:
                try:
                    count = self._run_input(input_name, ew, session_key, logger)
                    ctx.set_count(count)
                    metrics.set_count(count)
                    metrics.set_status('OK')
                except CircuitOpenError as e:
                    logger.warning('Circuit breaker OPEN: %s', e)
                    metrics.set_status('CB_OPEN', str(e))
                except Exception as e:
                    metrics.set_status('ERROR', str(e))
                    raise
                finally:
                    metrics.flush()

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

        # Esegui chain (con circuit breaker, retry-after, token cache)
        records = execute_chain_v3(
            calls          = cfg.get('calls', []),
            global_cfg     = cfg,
            session_key    = session_key,
            pagination_cfg = pagination_cfg,
        )

        # Checkpoint
        checkpoint_enabled = cfg.get('checkpoint', False)
        checkpoint_field   = cfg.get('checkpoint_field', '')
        last_cp  = load_checkpoint(name) if checkpoint_enabled else None
        new_cp   = last_cp

        # Configurazioni transform e output
        field_transforms = cfg.get('field_transforms', {})
        output_config    = cfg.get('output_config', {'format': 'kv'})

        sent = 0
        for record in records:
            # 1. Mapping tracciato
            mapped = apply_field_mapping(record, cfg.get('field_mapping', {}))

            # 2. Trasformazioni per-campo
            if field_transforms:
                mapped = apply_all_transforms(mapped, field_transforms)

            # 3. Checkpoint dedup
            if checkpoint_enabled and checkpoint_field:
                cp_val = mapped.get(checkpoint_field) or record.get(checkpoint_field)
                if cp_val is not None:
                    cp_str = str(cp_val)
                    if last_cp and cp_str <= last_cp:
                        continue
                    if new_cp is None or cp_str > new_cp:
                        new_cp = cp_str

            # 4. Formato output (pipe/kv/json/csv/custom)
            event_str = build_event_string(mapped, output_config)

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
