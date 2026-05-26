"""
ac_transforms.py  v1
Libreria di trasformazioni per-campo per API Connect.

Ogni trasformazione è una funzione pura (stringa → stringa/numero/None)
invocabile dal wizard tramite nome + parametri JSON.

UTILIZZO NEL CONFIG:
    "field_transforms": {
        "timestamp": [
            {"fn": "iso_to_epoch"},
            {"fn": "default", "value": "0"}
        ],
        "username": [
            {"fn": "lower"},
            {"fn": "replace", "old": " ", "new": "."}
        ],
        "amount": [
            {"fn": "to_float"},
            {"fn": "round", "decimals": 2}
        ],
        "full_name": [
            {"fn": "split", "sep": " ", "index": 0}
        ],
        "src_ip": [
            {"fn": "regex_extract", "pattern": r"(\\d+\\.\\d+\\.\\d+\\.\\d+)"}
        ],
        "role_code": [
            {"fn": "lookup_csv",
             "path": "$APP/lookups/roles.csv",
             "key_col": "code",
             "val_col": "label"}
        ]
    }

AGGIUNGERE UNA NUOVA TRASFORMAZIONE:
    1. Implementa la funzione in questa libreria (firma: fn(value, **params) -> Any)
    2. Registrala in TRANSFORM_REGISTRY con nome, label, params schema
    3. Il wizard la mostra automaticamente nel dropdown per-campo
"""

import csv
import hashlib
import io
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# ════════════════════════════════════════════════════════════════
# Registro trasformazioni — usato dal wizard per il dropdown
# ════════════════════════════════════════════════════════════════

TRANSFORM_REGISTRY: List[Dict] = [
    # ── Stringhe ────────────────────────────────────────────────
    {
        "name": "upper",
        "label": "MAIUSCOLO",
        "desc": "Converte in uppercase",
        "params": [],
        "example": "mario.rossi → MARIO.ROSSI",
    },
    {
        "name": "lower",
        "label": "minuscolo",
        "desc": "Converte in lowercase",
        "params": [],
        "example": "MARIO.ROSSI → mario.rossi",
    },
    {
        "name": "strip",
        "label": "Rimuovi spazi",
        "desc": "Trim spazi iniziali e finali",
        "params": [],
        "example": "  valore  → valore",
    },
    {
        "name": "replace",
        "label": "Sostituisci",
        "desc": "Sostituisce una sottostringa",
        "params": [
            {"name": "old", "type": "string", "required": True,  "placeholder": "es. @"},
            {"name": "new", "type": "string", "required": False, "placeholder": "es. _AT_"},
        ],
        "example": "mario@corp.it → mario_AT_corp.it",
    },
    {
        "name": "split",
        "label": "Split / estrai parte",
        "desc": "Divide per separatore e prende l'elemento N (0-based)",
        "params": [
            {"name": "sep",   "type": "string",  "required": True,  "placeholder": "es. @"},
            {"name": "index", "type": "integer", "required": True,  "placeholder": "es. 0"},
        ],
        "example": "mario@corp.it  sep=@  index=0  →  mario",
    },
    {
        "name": "truncate",
        "label": "Tronca",
        "desc": "Tronca a N caratteri",
        "params": [
            {"name": "length", "type": "integer", "required": True, "placeholder": "es. 50"},
        ],
        "example": "stringa molto lunga → stringa molt",
    },
    {
        "name": "pad_left",
        "label": "Pad sinistra",
        "desc": "Riempie a sinistra fino a lunghezza N",
        "params": [
            {"name": "length", "type": "integer", "required": True,  "placeholder": "es. 10"},
            {"name": "char",   "type": "string",  "required": False, "placeholder": "es. 0"},
        ],
        "example": "42  length=6  char=0  →  000042",
    },
    {
        "name": "concat",
        "label": "Concatena valore statico",
        "desc": "Aggiunge un prefisso e/o suffisso al valore",
        "params": [
            {"name": "prefix", "type": "string", "required": False, "placeholder": "es. user_"},
            {"name": "suffix", "type": "string", "required": False, "placeholder": "es. @corp.it"},
        ],
        "example": "mario  prefix=user_  suffix=@corp.it  →  user_mario@corp.it",
    },
    {
        "name": "regex_extract",
        "label": "Estrai con regex",
        "desc": "Estrae il primo match o gruppo named (?P<nome>...)",
        "params": [
            {"name": "pattern", "type": "string",  "required": True,  "placeholder": r"es. (\d+\.\d+\.\d+\.\d+)"},
            {"name": "group",   "type": "integer", "required": False, "placeholder": "1 (default primo gruppo)"},
        ],
        "example": r"ip=10.0.1.55 port=443  pattern=(\d+\.\d+\.\d+\.\d+)  →  10.0.1.55",
    },
    {
        "name": "regex_replace",
        "label": "Sostituisci con regex",
        "desc": "Sostituisce pattern regex con stringa",
        "params": [
            {"name": "pattern",     "type": "string", "required": True,  "placeholder": r"es. \s+"},
            {"name": "replacement", "type": "string", "required": False, "placeholder": "es. _"},
        ],
        "example": r"hello   world  pattern=\s+  replacement=_  →  hello_world",
    },
    # ── Date / Time ─────────────────────────────────────────────
    {
        "name": "iso_to_epoch",
        "label": "ISO 8601 → epoch",
        "desc": "Converte datetime ISO 8601 in epoch Unix (float)",
        "params": [],
        "example": "2024-06-14T14:30:00Z → 1718371800.0",
    },
    {
        "name": "epoch_to_iso",
        "label": "Epoch → ISO 8601",
        "desc": "Converte epoch Unix in stringa ISO 8601 UTC",
        "params": [],
        "example": "1718371800 → 2024-06-14T14:30:00Z",
    },
    {
        "name": "strptime_to_epoch",
        "label": "Data custom → epoch",
        "desc": "Converte data in formato personalizzato in epoch",
        "params": [
            {"name": "fmt", "type": "string", "required": True, "placeholder": "es. %d/%m/%Y %H:%M:%S"},
        ],
        "example": "14/06/2024 14:30:00  fmt=%d/%m/%Y %H:%M:%S  →  1718371800.0",
    },
    {
        "name": "epoch_to_splunk",
        "label": "Epoch → formato Splunk",
        "desc": "Converte epoch in stringa leggibile da Splunk (MM/DD/YYYY HH:MM:SS)",
        "params": [],
        "example": "1718371800 → 06/14/2024 14:30:00",
    },
    # ── Numeri ──────────────────────────────────────────────────
    {
        "name": "to_int",
        "label": "→ Intero",
        "desc": "Converte in intero (floor)",
        "params": [],
        "example": "1500.75 → 1500",
    },
    {
        "name": "to_float",
        "label": "→ Float",
        "desc": "Converte in numero decimale",
        "params": [],
        "example": '\"1.500,75\"  →  1500.75  (gestisce . e , come separatori)',
    },
    {
        "name": "round",
        "label": "Arrotonda",
        "desc": "Arrotonda a N decimali",
        "params": [
            {"name": "decimals", "type": "integer", "required": False, "placeholder": "2"},
        ],
        "example": "1500.756  decimals=2  →  1500.76",
    },
    {
        "name": "abs_val",
        "label": "Valore assoluto",
        "desc": "Rimuove il segno negativo",
        "params": [],
        "example": "-1500.75 → 1500.75",
    },
    {
        "name": "math_expr",
        "label": "Espressione matematica",
        "desc": "Applica un'espressione con x = valore corrente",
        "params": [
            {"name": "expr", "type": "string", "required": True, "placeholder": "es. x * 100  oppure  x / 1000"},
        ],
        "example": "1500  expr=x/100  →  15.0",
    },
    # ── Logica / Condizioni ──────────────────────────────────────
    {
        "name": "default",
        "label": "Valore di default",
        "desc": "Usa il valore di default se il campo è vuoto/None",
        "params": [
            {"name": "value", "type": "string", "required": True, "placeholder": "es. N/A"},
        ],
        "example": "None  value=N/A  →  N/A",
    },
    {
        "name": "map_values",
        "label": "Mappa valori (dizionario)",
        "desc": "Sostituisce valori secondo una mappa JSON",
        "params": [
            {"name": "mapping",  "type": "json",   "required": True,  "placeholder": '{"1":"login","2":"logout"}'},
            {"name": "fallback", "type": "string", "required": False, "placeholder": "es. unknown"},
        ],
        "example": '1  mapping={"1":"login"}  →  login',
    },
    {
        "name": "if_contains",
        "label": "Se contiene → valore",
        "desc": "Se il campo contiene la stringa, restituisce true_val, altrimenti false_val",
        "params": [
            {"name": "substring",  "type": "string", "required": True},
            {"name": "true_val",   "type": "string", "required": True,  "placeholder": "es. YES"},
            {"name": "false_val",  "type": "string", "required": False, "placeholder": "es. NO"},
        ],
        "example": "FAILED_LOGIN  substring=FAILED  true_val=failure  →  failure",
    },
    # ── Hash / Sicurezza ────────────────────────────────────────
    {
        "name": "sha256",
        "label": "SHA-256",
        "desc": "Hash SHA-256 del valore (utile per anonimizzare PII)",
        "params": [],
        "example": "mario.rossi → a1b2c3d4...",
    },
    {
        "name": "mask",
        "label": "Maschera (PII)",
        "desc": "Mostra solo i primi N e ultimi M caratteri, maschera il resto",
        "params": [
            {"name": "first", "type": "integer", "required": False, "placeholder": "4"},
            {"name": "last",  "type": "integer", "required": False, "placeholder": "4"},
            {"name": "char",  "type": "string",  "required": False, "placeholder": "*"},
        ],
        "example": "IT60X0542811101  first=4  last=4  →  IT60*******1101",
    },
    # ── Lookup ──────────────────────────────────────────────────
    {
        "name": "lookup_csv",
        "label": "Lookup da CSV",
        "desc": "Cerca il valore in un CSV locale e restituisce il campo corrispondente",
        "params": [
            {"name": "path",    "type": "string", "required": True, "placeholder": "$APP/lookups/nome.csv"},
            {"name": "key_col", "type": "string", "required": True, "placeholder": "codice"},
            {"name": "val_col", "type": "string", "required": True, "placeholder": "descrizione"},
        ],
        "example": "001  path=roles.csv  key_col=id  val_col=name  →  Administrator",
    },
    {
        "name": "lookup_json",
        "label": "Lookup da JSON inline",
        "desc": "Cerca il valore in un dizionario JSON inline",
        "params": [
            {"name": "mapping",  "type": "json",   "required": True},
            {"name": "fallback", "type": "string", "required": False, "placeholder": "es. UNKNOWN"},
        ],
        "example": '401  mapping={"200":"OK","401":"Unauth"}  →  Unauth',
    },
]

# Indice per lookup veloce
_REGISTRY_INDEX: Dict[str, Dict] = {t["name"]: t for t in TRANSFORM_REGISTRY}

# Cache CSV lookup (path → dict)
_CSV_CACHE: Dict[str, Dict[str, str]] = {}


# ════════════════════════════════════════════════════════════════
# Esecutore pipeline
# ════════════════════════════════════════════════════════════════

def apply_pipeline(value: Any, pipeline: List[Dict]) -> Any:
    """
    Applica una sequenza di trasformazioni a un valore.
    Ogni step è {"fn": "nome_funzione", ...params}.
    Restituisce il valore trasformato o None se un passo fallisce
    e non c'è un default configurato.
    """
    current = value
    for step in pipeline:
        fn_name = step.get("fn", "")
        params  = {k: v for k, v in step.items() if k != "fn"}
        try:
            current = _dispatch(fn_name, current, **params)
        except Exception as e:
            import logging
            logging.getLogger("api_connect.ac_transforms").warning(
                "transform %s(%r) failed: %s", fn_name, current, e
            )
            # Non interrompere la pipeline — passa None
            current = None
    return current


def apply_all_transforms(record: Dict, transforms_config: Dict[str, List[Dict]]) -> Dict:
    """
    Applica le pipeline di trasformazione a tutti i campi configurati.
    transforms_config: { "campo": [ {"fn": "lower"}, {"fn": "strip"} ] }
    Restituisce il record con i campi trasformati.
    """
    result = dict(record)
    for field, pipeline in transforms_config.items():
        if not pipeline:
            continue
        raw_val = result.get(field)
        result[field] = apply_pipeline(raw_val, pipeline)
    return result


# ════════════════════════════════════════════════════════════════
# Dispatcher
# ════════════════════════════════════════════════════════════════

def _dispatch(fn_name: str, value: Any, **params) -> Any:
    funcs = {
        # stringhe
        "upper":         _upper,
        "lower":         _lower,
        "strip":         _strip,
        "replace":       _replace,
        "split":         _split,
        "truncate":      _truncate,
        "pad_left":      _pad_left,
        "concat":        _concat,
        "regex_extract": _regex_extract,
        "regex_replace": _regex_replace,
        # date/time
        "iso_to_epoch":      _iso_to_epoch,
        "epoch_to_iso":      _epoch_to_iso,
        "strptime_to_epoch": _strptime_to_epoch,
        "epoch_to_splunk":   _epoch_to_splunk,
        # numeri
        "to_int":    _to_int,
        "to_float":  _to_float,
        "round":     _round,
        "abs_val":   _abs_val,
        "math_expr": _math_expr,
        # logica
        "default":     _default,
        "map_values":  _map_values,
        "if_contains": _if_contains,
        # hash/sicurezza
        "sha256": _sha256,
        "mask":   _mask,
        # lookup
        "lookup_csv":  _lookup_csv,
        "lookup_json": _lookup_json,
    }
    fn = funcs.get(fn_name)
    if not fn:
        raise ValueError(f"Trasformazione sconosciuta: {fn_name!r}")
    return fn(value, **params)


# ════════════════════════════════════════════════════════════════
# Implementazioni
# ════════════════════════════════════════════════════════════════

def _str(v: Any) -> str:
    return "" if v is None else str(v)


# ── Stringhe ────────────────────────────────────────────────────

def _upper(v, **_):           return _str(v).upper()
def _lower(v, **_):           return _str(v).lower()
def _strip(v, **_):           return _str(v).strip()

def _replace(v, old="", new="", **_):
    return _str(v).replace(str(old), str(new))

def _split(v, sep="", index=0, **_):
    parts = _str(v).split(str(sep))
    idx   = int(index)
    return parts[idx] if 0 <= idx < len(parts) else ""

def _truncate(v, length=255, **_):
    return _str(v)[:int(length)]

def _pad_left(v, length=10, char="0", **_):
    return _str(v).rjust(int(length), str(char)[0] if char else " ")

def _concat(v, prefix="", suffix="", **_):
    return str(prefix) + _str(v) + str(suffix)

def _regex_extract(v, pattern="", group=1, **_):
    m = re.search(str(pattern), _str(v))
    if not m:
        return None
    g = int(group) if group else 1
    try:
        return m.group(g)
    except IndexError:
        # prova named groups
        d = m.groupdict()
        return list(d.values())[0] if d else m.group(0)

def _regex_replace(v, pattern="", replacement="", **_):
    return re.sub(str(pattern), str(replacement), _str(v))


# ── Date / Time ─────────────────────────────────────────────────

_ISO_FORMATS = [
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%S+00:00",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
]

def _iso_to_epoch(v, **_) -> float:
    s = _str(v).strip()
    # Prova parse diretto
    for fmt in _ISO_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    # Fallback: prova con fromisoformat (Python 3.7+)
    try:
        s2 = re.sub(r"Z$", "+00:00", s)
        dt = datetime.fromisoformat(s2)
        return dt.timestamp()
    except Exception:
        raise ValueError(f"Impossibile parsare data ISO: {s!r}")

def _epoch_to_iso(v, **_) -> str:
    ts = float(_str(v).strip())
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")

def _strptime_to_epoch(v, fmt="%Y-%m-%d %H:%M:%S", **_) -> float:
    dt = datetime.strptime(_str(v).strip(), str(fmt))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()

def _epoch_to_splunk(v, **_) -> str:
    ts = float(_str(v).strip())
    return datetime.utcfromtimestamp(ts).strftime("%m/%d/%Y %H:%M:%S")


# ── Numeri ──────────────────────────────────────────────────────

def _to_int(v, **_) -> int:
    return int(float(_normalize_number(_str(v))))

def _to_float(v, **_) -> float:
    return float(_normalize_number(_str(v)))

def _normalize_number(s: str) -> str:
    """Normalizza 1.500,75 o 1,500.75 in 1500.75"""
    s = s.strip()
    # Rimuovi simboli valuta e spazi
    s = re.sub(r"[€$£¥\s]", "", s)
    # Se usa la virgola come decimale (italiano/europeo): 1.500,75
    if re.search(r",\d{1,2}$", s):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    return s

def _round(v, decimals=2, **_) -> float:
    return round(float(_normalize_number(_str(v))), int(decimals))

def _abs_val(v, **_) -> float:
    return abs(float(_normalize_number(_str(v))))

def _math_expr(v, expr="x", **_) -> Any:
    """Valuta un'espressione sicura con x = valore corrente."""
    try:
        x = float(_normalize_number(_str(v)))  # noqa: F841 (usata in eval)
    except ValueError:
        x = _str(v)  # noqa: F841
    # Whitelist di simboli sicuri
    allowed = set("x0123456789.+-*/%() ")
    expr_str = str(expr)
    if not all(c in allowed or c.isalpha() for c in expr_str):
        raise ValueError(f"Espressione non sicura: {expr_str!r}")
    # Funzioni math disponibili
    safe_globals = {"__builtins__": {}, "x": x,
                    "round": round, "abs": abs, "int": int, "float": float,
                    "sqrt": math.sqrt, "floor": math.floor, "ceil": math.ceil}
    return eval(expr_str, safe_globals)  # noqa: S307


# ── Logica ──────────────────────────────────────────────────────

def _default(v, value="", **_):
    if v is None or str(v).strip() == "":
        return value
    return v

def _map_values(v, mapping=None, fallback=None, **_):
    if mapping is None:
        return v
    if isinstance(mapping, str):
        mapping = json.loads(mapping)
    key = _str(v)
    return mapping.get(key, fallback if fallback is not None else v)

def _if_contains(v, substring="", true_val="", false_val=None, **_):
    if str(substring) in _str(v):
        return true_val
    return false_val if false_val is not None else v


# ── Hash / Sicurezza ────────────────────────────────────────────

def _sha256(v, **_) -> str:
    return hashlib.sha256(_str(v).encode()).hexdigest()

def _mask(v, first=4, last=4, char="*", **_) -> str:
    s    = _str(v)
    f    = int(first)
    l    = int(last)
    c    = str(char)[0] if char else "*"
    mid  = max(0, len(s) - f - l)
    return s[:f] + c * mid + s[len(s)-l:] if len(s) > f + l else s


# ── Lookup ──────────────────────────────────────────────────────

def _resolve_path(path: str) -> str:
    """Risolve $APP → cartella dell'app Splunk."""
    app_dir = os.path.join(
        os.environ.get("SPLUNK_HOME", "/opt/splunk"),
        "etc", "apps", "api_connect"
    )
    return path.replace("$APP", app_dir)

def _lookup_csv(v, path="", key_col="", val_col="", **_):
    global _CSV_CACHE
    resolved = _resolve_path(str(path))
    cache_key = f"{resolved}:{key_col}:{val_col}"
    if cache_key not in _CSV_CACHE:
        mapping = {}
        try:
            with open(resolved, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    k = row.get(str(key_col), "")
                    mapping[k] = row.get(str(val_col), "")
        except FileNotFoundError:
            pass
        _CSV_CACHE[cache_key] = mapping
    return _CSV_CACHE[cache_key].get(_str(v), v)

def _lookup_json(v, mapping=None, fallback=None, **_):
    if mapping is None:
        return v
    if isinstance(mapping, str):
        mapping = json.loads(mapping)
    return mapping.get(_str(v), fallback if fallback is not None else v)


# ════════════════════════════════════════════════════════════════
# Output format builder — produce la stringa finale dell'evento
# ════════════════════════════════════════════════════════════════

def build_event_string(record: Dict, output_config: Dict) -> str:
    """
    Costruisce la stringa finale dell'evento secondo il formato configurato.

    output_config:
      {
        "format": "pipe",          # pipe | kv | json | csv | custom
        "fields": ["time","hostname","nomeapp",...],  # ordine e selezione
        "separator": "|",          # per formato custom
        "kv_sep": "=",             # per formato kv
        "quote_values": true,      # per kv: time="..." vs time=...
        "include_extra": false,    # includi campi non nel tracciato
        "null_value": ""           # stringa per valori None/vuoti
      }

    Formati:
      pipe   → time|hostname|nomeapp|tipoazione|...   (tracciato aziendale)
      kv     → time="..." hostname="..." nomeapp="..."
      json   → {"time":"...","hostname":"..."}
      csv    → time,hostname,nomeapp,...  (header opzionale)
      custom → separatore personalizzato
    """
    fmt        = output_config.get("format", "kv")
    fields     = output_config.get("fields", _DEFAULT_TRACCIATO)
    null_val   = output_config.get("null_value", "")
    inc_extra  = output_config.get("include_extra", False)

    # Costruisci lista valori nell'ordine dei campi
    selected = _collect_fields(record, fields, null_val, inc_extra)

    if fmt == "pipe":
        return "|".join(str(v) for v in selected.values())

    if fmt == "custom":
        sep = output_config.get("separator", "|")
        return sep.join(str(v) for v in selected.values())

    if fmt == "json":
        return json.dumps(selected, ensure_ascii=False)

    if fmt == "csv":
        buf = io.StringIO()
        w   = csv.writer(buf)
        w.writerow(list(selected.values()))
        return buf.getvalue().rstrip("\r\n")

    # Default: kv
    quote = output_config.get("quote_values", True)
    kv_sep = output_config.get("kv_sep", "=")
    parts = []
    for k, v in selected.items():
        val = str(v).replace('"', '\\"') if quote else str(v)
        if quote:
            parts.append(f'{k}{kv_sep}"{val}"')
        else:
            parts.append(f'{k}{kv_sep}{val}')
    return " ".join(parts)


_DEFAULT_TRACCIATO = [
    "time", "hostname", "nomeapp", "tipoazione",
    "clientip", "username", "tipooperazione",
    "valorePrima", "valoreDP", "target", "note",
]


def _collect_fields(record: Dict, fields: List[str],
                    null_val: str, include_extra: bool) -> Dict:
    result = {}
    seen   = set()
    for f in fields:
        v = record.get(f)
        result[f] = v if v is not None and str(v) != "" else null_val
        seen.add(f)
    if include_extra:
        for k, v in record.items():
            if k not in seen and not k.startswith("_ac_"):
                result[k] = v if v is not None else null_val
    return result


# ════════════════════════════════════════════════════════════════
# Preview helper — usato dal wizard Step 7
# ════════════════════════════════════════════════════════════════

def preview_event(sample_record: Dict, transforms_config: Dict,
                  field_mapping: Dict, output_config: Dict) -> Tuple[Dict, str]:
    """
    Esegue la pipeline completa su un record campione e restituisce
    (record_trasformato, stringa_evento_finale).
    Usato dal wizard per la preview live prima della generazione.
    """
    from ac_http import apply_field_mapping  # import locale

    # 1. Mapping tracciato
    mapped = apply_field_mapping(sample_record, field_mapping)

    # 2. Trasformazioni per-campo
    transformed = apply_all_transforms(mapped, transforms_config)

    # 3. Formato output
    event_str = build_event_string(transformed, output_config)

    return transformed, event_str
