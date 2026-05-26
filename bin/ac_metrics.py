"""
ac_metrics.py
Scrittura metriche per-run su KV Store api_connect_inputs.

Ogni esecuzione dello script aggiorna il record KV con:
  last_status, last_run, last_count, last_latency_ms,
  last_error, consecutive_failures, total_runs, total_records

Usato dalla dashboard per mostrare lo stato aggiornato di ogni input.
"""

import json
import logging
import os
import time
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger("api_connect.ac_metrics")

_APP_DIR = os.path.join(
    os.environ.get("SPLUNK_HOME", "/opt/splunk"),
    "etc", "apps", "api_connect"
)


class RunMetrics:
    """
    Raccoglie le metriche di un singolo run e le scrive su KV Store.

    Utilizzo:
        metrics = RunMetrics(session_key, input_name="api_erp_transactions")
        metrics.start()
        try:
            records = execute_chain(...)
            metrics.set_count(len(records))
            metrics.set_status("OK")
        except Exception as e:
            metrics.set_status("ERROR", str(e))
            raise
        finally:
            metrics.flush()
    """

    def __init__(self, session_key: str, input_name: str):
        self.session_key  = session_key
        self.input_name   = input_name
        self._start_ts    = None
        self._count       = 0
        self._status      = "OK"
        self._error       = ""
        self._latency_ms  = 0

    def start(self) -> None:
        self._start_ts = time.time()

    def set_count(self, n: int) -> None:
        self._count = n

    def set_status(self, status: str, error: str = "") -> None:
        self._status = status
        self._error  = error or ""

    def flush(self) -> None:
        """Scrive le metriche su KV Store tramite REST."""
        if self._start_ts:
            self._latency_ms = int((time.time() - self._start_ts) * 1000)

        payload = {
            "last_status":    self._status,
            "last_run":       time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            "last_count":     str(self._count),
            "last_latency_ms": str(self._latency_ms),
            "last_error":     self._error[:500],
        }

        try:
            import splunklib.client as client
            svc = client.connect(token=self.session_key)
            # Cerca la chiave KV esistente per questo input
            collection = svc.kvstore["api_connect_inputs"]
            results    = collection.data.query(query=json.dumps({"name": self.input_name}))
            if results:
                key = results[0]["_key"]
                # Merge: aggiorna solo i campi metrici
                existing = dict(results[0])
                existing.update(payload)
                existing.pop("_key", None)
                existing.pop("_user", None)
                collection.data.update(key, json.dumps(existing))
            else:
                logger.warning("Input '%s' non trovato in KV Store — metriche non scritte",
                               self.input_name)
        except Exception as e:
            logger.error("flush metrics per '%s' fallito: %s", self.input_name, e)


@contextmanager
def run_context(session_key: str, input_name: str, logger_inst=None):
    """
    Context manager che misura e persiste le metriche automaticamente.

    Utilizzo:
        with run_context(sk, "api_erp_transactions") as m:
            records = execute_chain(...)
            m.set_count(len(records))
    """
    metrics = RunMetrics(session_key, input_name)
    metrics.start()
    lg = logger_inst or logger
    try:
        yield metrics
        metrics.set_status("OK")
        lg.info(
            "Run completato input=%s records=%d latency_ms=%d",
            input_name, metrics._count,
            int((time.time() - metrics._start_ts) * 1000) if metrics._start_ts else 0
        )
    except Exception as e:
        metrics.set_status("ERROR", str(e))
        lg.error("Run fallito input=%s error=%s", input_name, e)
        raise
    finally:
        metrics.flush()
