"""
ac_circuit_breaker.py
Circuit breaker per gli script API Connect.

Impedisce che uno script continui a martellare un'API down,
proteggendo sia l'API esterna che il sistema Splunk.

STATI:
  CLOSED     → normale, le chiamate passano
  OPEN       → stop totale per cooldown_s secondi
  HALF_OPEN  → una chiamata di test; se ok → CLOSED, se fallisce → OPEN

UTILIZZO:
    from ac_circuit_breaker import CircuitBreaker, CircuitOpenError

    cb = CircuitBreaker(name="api_erp_transactions",
                        failure_threshold=5,
                        cooldown_s=120)

    with cb:
        response = do_request(url, ...)
        cb.record_success()

    # oppure manuale:
    try:
        cb.before_call()
        response = do_request(...)
        cb.record_success()
    except CircuitOpenError:
        logger.warning("Circuit aperto — skip run")
        return 0
    except Exception as e:
        cb.record_failure()
        raise
"""

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("api_connect.ac_circuit_breaker")

_CB_BASE = os.path.join(
    os.environ.get("SPLUNK_HOME", "/opt/splunk"),
    "var", "lib", "splunk", "modinputs", "api_connect", "circuit_breakers"
)

# Stati
_CLOSED    = "CLOSED"
_OPEN      = "OPEN"
_HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(Exception):
    """Raised quando il circuit breaker è aperto e blocca la chiamata."""
    def __init__(self, name: str, cooldown_remaining: float):
        self.name              = name
        self.cooldown_remaining = cooldown_remaining
        super().__init__(
            f"Circuit breaker OPEN per '{name}'. "
            f"Ripristino tra {cooldown_remaining:.0f}s"
        )


class CircuitBreaker:
    """
    Circuit breaker con persistenza su file JSON.

    Parametri:
      name              : identificatore univoco (es. nome input)
      failure_threshold : numero di fallimenti consecutivi prima di aprire
      cooldown_s        : secondi di pausa in stato OPEN
      success_threshold : successi consecutivi in HALF_OPEN per tornare CLOSED
    """

    def __init__(self,
                 name: str,
                 failure_threshold: int = 5,
                 cooldown_s: int = 120,
                 success_threshold: int = 2):
        self.name              = name
        self.failure_threshold = failure_threshold
        self.cooldown_s        = cooldown_s
        self.success_threshold = success_threshold
        self._state_path       = self._build_path(name)
        os.makedirs(_CB_BASE, exist_ok=True)

    # ── Context manager interface ────────────────────────────────
    def __enter__(self):
        self.before_call()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.record_success()
        elif exc_type not in (CircuitOpenError,):
            self.record_failure()
        return False  # non sopprimere eccezioni

    # ── Public API ───────────────────────────────────────────────
    def before_call(self) -> None:
        """Chiama prima di ogni request. Raise CircuitOpenError se aperto."""
        state = self._load()
        current = state.get("status", _CLOSED)

        if current == _CLOSED:
            return

        if current == _OPEN:
            opened_at = state.get("opened_at", 0)
            elapsed   = time.time() - opened_at
            remaining = self.cooldown_s - elapsed
            if remaining > 0:
                logger.warning(
                    "CircuitBreaker OPEN per '%s'. Ripristino tra %.0fs",
                    self.name, remaining
                )
                raise CircuitOpenError(self.name, remaining)
            # Cooldown scaduto → HALF_OPEN
            state["status"] = _HALF_OPEN
            state["half_open_successes"] = 0
            self._save(state)
            logger.info("CircuitBreaker '%s' → HALF_OPEN (test call)", self.name)

        # HALF_OPEN: lascia passare una chiamata di test
        if current == _HALF_OPEN:
            return

    def record_success(self) -> None:
        """Registra un successo. In HALF_OPEN conta verso la riapertura."""
        state   = self._load()
        current = state.get("status", _CLOSED)

        if current == _CLOSED:
            # Reset contatore fallimenti
            if state.get("consecutive_failures", 0) > 0:
                state["consecutive_failures"] = 0
                self._save(state)
            return

        if current == _HALF_OPEN:
            state["half_open_successes"] = state.get("half_open_successes", 0) + 1
            if state["half_open_successes"] >= self.success_threshold:
                state["status"]               = _CLOSED
                state["consecutive_failures"] = 0
                state["half_open_successes"]  = 0
                logger.info("CircuitBreaker '%s' → CLOSED ✓", self.name)
            self._save(state)

    def record_failure(self) -> None:
        """Registra un fallimento. Se supera la soglia → OPEN."""
        state    = self._load()
        current  = state.get("status", _CLOSED)
        failures = state.get("consecutive_failures", 0) + 1
        state["consecutive_failures"] = failures
        state["last_failure_at"]      = time.time()

        if current == _HALF_OPEN or failures >= self.failure_threshold:
            state["status"]    = _OPEN
            state["opened_at"] = time.time()
            logger.error(
                "CircuitBreaker '%s' → OPEN dopo %d fallimenti consecutivi. "
                "Cooldown: %ds",
                self.name, failures, self.cooldown_s
            )
        self._save(state)

    def reset(self) -> None:
        """Reset manuale del circuit breaker."""
        self._save({"status": _CLOSED, "consecutive_failures": 0})
        logger.info("CircuitBreaker '%s' reset manuale → CLOSED", self.name)

    @property
    def is_open(self) -> bool:
        state = self._load()
        if state.get("status") != _OPEN:
            return False
        elapsed = time.time() - state.get("opened_at", 0)
        return elapsed < self.cooldown_s

    @property
    def status(self) -> str:
        return self._load().get("status", _CLOSED)

    def get_stats(self) -> dict:
        state = self._load()
        return {
            "name":                 self.name,
            "status":               state.get("status", _CLOSED),
            "consecutive_failures": state.get("consecutive_failures", 0),
            "failure_threshold":    self.failure_threshold,
            "cooldown_s":           self.cooldown_s,
            "opened_at":            state.get("opened_at"),
            "last_failure_at":      state.get("last_failure_at"),
        }

    # ── Private ──────────────────────────────────────────────────
    @staticmethod
    def _build_path(name: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        return os.path.join(_CB_BASE, f"{safe}.cb.json")

    def _load(self) -> dict:
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"status": _CLOSED, "consecutive_failures": 0}

    def _save(self, state: dict) -> None:
        tmp = self._state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, self._state_path)
