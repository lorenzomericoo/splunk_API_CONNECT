# API Connect for Splunk — v3

Wizard GUI stile **Postman** per la creazione di Modular Input che interrogano API REST esterne.
Chain Builder visuale con pipeline di trasformazioni per-campo, output format configurabile,
circuit breaker, token cache OAuth2 e metriche per-run automatiche.

---

## Novità v3 (Sprint 1)

| Feature | Dettaglio |
|---|---|
| **Pipeline trasformazioni** | 26 funzioni built-in per-campo: date, stringhe, numeri, hash, lookup CSV/JSON |
| **Output format builder** | pipe \| kv \| json \| csv \| custom — preview live nel wizard |
| **Token cache OAuth2** | Cache con TTL su file — non rinegozia a ogni run |
| **Circuit breaker** | Stop automatico dopo N errori con cooldown configurabile |
| **Retry-After respect** | HTTP 429 → attende il tempo indicato dal server |
| **Metriche per-run** | Aggiorna KV Store dopo ogni run: status, count, latency_ms, error |
| **Preview evento** | Step 9 mostra la stringa finale esatta prima della generazione |

---

## Prerequisiti

| Componente | Versione |
|---|---|
| Splunk Enterprise / Heavy Forwarder | 9.0+ |
| Python (Splunk embedded) | 3.7+ |

Nessuna dipendenza esterna — tutto stdlib + splunklib.

---

## Installazione

```bash
$SPLUNK_HOME/bin/splunk install app api_connect_v3.spl -auth admin:password
chmod 755 $SPLUNK_HOME/etc/apps/api_connect/bin/*.py
$SPLUNK_HOME/bin/splunk restart
```

---

## Struttura moduli Python

```
bin/
├── ac_http.py            # Client HTTP: auth per-call, chain, paginator, parsers
├── ac_transforms.py      # 26 funzioni trasformazione + pipeline executor + build_event_string
├── ac_token_cache.py     # OAuth2 token cache con TTL su file
├── ac_circuit_breaker.py # Circuit breaker CLOSED/OPEN/HALF_OPEN con persistenza
├── ac_metrics.py         # RunMetrics → KV Store dopo ogni run
├── ac_logger.py          # Logger → index=_internal sourcetype=custom_script_logger
├── ac_input_template.py  # Template per gli script generati
├── api_connect_rest.py   # REST handler: test call per-call-auth
└── api_connect_generate.py # REST handler: genera script v3
```

---

## Wizard — 9 step

| Step | Contenuto |
|---|---|
| 1 | **Auth globale** — default per tutte le call, sovrascrivibile per-call |
| 2 | **Chain Builder** — card drag-drop, risposta live per-card, auth override, error policy, join |
| 3 | **Parsing** — tree interattivo, JSONPath, CSV/TSV/XML/HTML/text+regex |
| 4 | **Pipeline trasformazioni** — per ogni campo: sequenza di funzioni in cascata |
| 5 | **Tracciato standard** — mapping a time\|hostname\|nomeapp\|tipoazione\|clientip\|username\|tipooperazione\|valorePrima\|valoreDP\|target\|note |
| 6 | **Output format + Destinazione** — pipe/kv/json/csv/custom · index/sourcetype/source · checkpoint |
| 7 | **Resilienza** — circuit breaker: soglia fallimenti + cooldown |
| 8 | **Logger** — source per custom_script_logger |
| 9 | **Preview & Genera** — stringa evento live + riepilogo + generazione script |

---

## Trasformazioni disponibili (ac_transforms.py)

### Stringhe
| Funzione | Parametri | Esempio |
|---|---|---|
| `upper` | — | `mario` → `MARIO` |
| `lower` | — | `MARIO` → `mario` |
| `strip` | — | `  valore  ` → `valore` |
| `replace` | old, new | `@` → `_AT_` |
| `split` | sep, index | `mario@corp.it` sep=`@` idx=`0` → `mario` |
| `truncate` | length | → primi N caratteri |
| `pad_left` | length, char | `42` → `000042` |
| `concat` | prefix, suffix | → `prefix` + valore + `suffix` |
| `regex_extract` | pattern, group | estrae primo gruppo/named group |
| `regex_replace` | pattern, replacement | sostituzione regex |

### Date / Time
| Funzione | Esempio |
|---|---|
| `iso_to_epoch` | `2024-06-14T14:30:00Z` → `1718371800.0` |
| `epoch_to_iso` | `1718371800` → `2024-06-14T14:30:00Z` |
| `strptime_to_epoch` | fmt=`%d/%m/%Y %H:%M:%S` |
| `epoch_to_splunk` | → `06/14/2024 14:30:00` |

### Numeri
| Funzione | Esempio |
|---|---|
| `to_int` | `1500.75` → `1500` |
| `to_float` | `"1.500,75"` → `1500.75` (gestisce notazione europea) |
| `round` | decimals=2 |
| `abs_val` | `-1500` → `1500` |
| `math_expr` | expr=`x/100` |

### Logica
| Funzione | Esempio |
|---|---|
| `default` | value=`N/A` se vuoto/None |
| `map_values` | `{"1":"login","2":"logout"}` |
| `if_contains` | substring, true_val, false_val |

### Sicurezza / PII
| Funzione | Esempio |
|---|---|
| `sha256` | hash anonimizzazione |
| `mask` | first=4 last=4 → `IT60*******1101` |

### Lookup
| Funzione | Parametri |
|---|---|
| `lookup_csv` | path, key_col, val_col |
| `lookup_json` | mapping (JSON inline), fallback |

---

## Formati output evento

| Formato | Esempio |
|---|---|
| `pipe` | `2024-06-14T14:30:00Z\|erp.corp.it\|ERP_CORP\|TRANSFER\|10.0.1.55\|mario.rossi\|\|5000\|3500\|IT60X…\|` |
| `kv` | `time="1718371800" hostname="erp.corp.it" nomeapp="ERP_CORP" …` |
| `json` | `{"time":"1718371800","hostname":"erp.corp.it",…}` |
| `csv` | `1718371800,erp.corp.it,ERP_CORP,TRANSFER,…` |
| `custom` | separatore personalizzato |

---

## Circuit breaker

Stati: `CLOSED` → `OPEN` (dopo N fallimenti) → `HALF_OPEN` (dopo cooldown) → `CLOSED`

```
Monitoraggio:
index=_internal sourcetype=custom_script_logger source=api_connect:*
| rex field=message "circuit=(?<cb_state>\w+)"
| stats latest(cb_state) by source
```

Reset manuale da Python:
```python
from ac_circuit_breaker import CircuitBreaker
CircuitBreaker("api_erp_transactions").reset()
```

---

## Token cache OAuth2

Il token viene salvato in:
```
$SPLUNK_HOME/var/lib/splunk/modinputs/api_connect/token_cache/<realm>.token.json
```
Rinnovo automatico 60 secondi prima della scadenza.
Invalidazione manuale:
```python
from ac_token_cache import invalidate_token
invalidate_token("api_connect:oauth2_cc:erp_prod")
```

---

## Metriche per-run (dashboard)

Ogni run aggiorna il record KV Store con:

| Campo | Contenuto |
|---|---|
| `last_status` | `OK` / `ERROR` / `CB_OPEN` |
| `last_run` | timestamp UTC |
| `last_count` | record inviati |
| `last_latency_ms` | durata totale run |
| `last_error` | messaggio errore (max 500 char) |

SPL per monitoraggio:
```spl
`ac_status`
| eval status_class=case(last_status="OK","success",last_status="CB_OPEN","warning",true(),"error")
| table input, last_status, last_run, last_count, last_latency_ms, last_error
```

---

## Lookup CSV

Salva il file in `$APP/lookups/nome.csv` e usa la funzione `lookup_csv`:
```json
{"fn": "lookup_csv", "path": "$APP/lookups/roles.csv", "key_col": "code", "val_col": "label"}
```

---

## Versione

`3.0.0` — Sprint 1: transforms pipeline · output format · token cache · circuit breaker · Retry-After · metrics · preview evento
