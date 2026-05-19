"""
ac_logger.py
Logger standard API Connect.

Scrive su stderr (Splunk lo cattura in index=_internal)
con sourcetype=custom_script_logger e la source configurata.

Utilizzo negli script generati:
    from ac_logger import get_logger
    logger = get_logger('api_connect:mio_input:runner')
    logger.info('Avvio esecuzione')
    logger.error('Errore: %s', str(e))

Il formato produce eventi KV leggibili da SPL:
    index=_internal sourcetype=custom_script_logger
    source=api_connect:mio_input:runner
    | stats count by level, message
"""

import logging
import sys
import os


class SplunkInternalFormatter(logging.Formatter):
    """
    Formatta i log come KV pairs compatibili con il parser Splunk
    per index=_internal sourcetype=custom_script_logger.
    Esempio output:
        2024-06-14 14:30:00,123 level=INFO source=api_connect:foo:runner pid=12345 message="Completato: 42 record inviati"
    """

    def __init__(self, source):
        super().__init__()
        self.source = source
        self.pid = os.getpid()

    def format(self, record):
        # Escape double quotes nel messaggio
        msg = self.formatMessage(record).replace('"', '\\"')
        return (
            f'{self.formatTime(record, "%Y-%m-%d %H:%M:%S,%f")[:-3]} '
            f'level={record.levelname} '
            f'source={self.source} '
            f'pid={self.pid} '
            f'message="{msg}"'
        )

    def formatMessage(self, record):
        return record.getMessage()


def get_logger(source: str, level: int = logging.INFO) -> logging.Logger:
    """
    Restituisce un logger configurato per scrivere su stderr
    nel formato atteso da Splunk _internal con custom_script_logger.

    Args:
        source: valore della source (es. 'api_connect:auth_events:runner')
        level:  livello minimo di log (default INFO)

    Returns:
        logging.Logger pronto all'uso
    """
    logger_name = f'api_connect.{source}'
    logger = logging.getLogger(logger_name)

    # Evita handler duplicati se il modulo viene importato più volte
    if logger.handlers:
        return logger

    logger.setLevel(level)
    logger.propagate = False

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(SplunkInternalFormatter(source))
    handler.setLevel(level)
    logger.addHandler(handler)

    return logger


class ScriptRunContext:
    """
    Context manager che logga inizio/fine esecuzione con metriche.

    Utilizzo:
        with ScriptRunContext(logger, 'api_erp_transactions') as ctx:
            records = fetch_and_process()
            ctx.set_count(len(records))
    """

    def __init__(self, logger: logging.Logger, input_name: str):
        self.logger = logger
        self.input_name = input_name
        self._count = 0
        self._start = None

    def set_count(self, n: int):
        self._count = n

    def __enter__(self):
        import time
        self._start = time.time()
        self.logger.info('Avvio esecuzione input=%s', self.input_name)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        import time
        elapsed_ms = int((time.time() - self._start) * 1000)
        if exc_type is None:
            self.logger.info(
                'Completato input=%s records=%d elapsed_ms=%d',
                self.input_name, self._count, elapsed_ms
            )
        else:
            self.logger.error(
                'Errore input=%s elapsed_ms=%d error="%s"',
                self.input_name, elapsed_ms, str(exc_val)
            )
        # Non sopprimere eccezioni
        return False
