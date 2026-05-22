# API Connect for Splunk — v2

Wizard GUI stile **Postman** per la creazione di Modular Input che interrogano API REST esterne, con Chain Builder visuale, parsing della risposta e mapping al tracciato standard aziendale.

---

## Novità v2 rispetto a v1

| Feature | v1 | v2 |
|---|---|---|
| Autenticazione per-call | ✗ | ✅ ogni call ha la sua auth indipendente |
| Chain Builder visuale | ✗ | ✅ card drag-and-drop stile Postman |
| Risposta live per-card | ✗ | ✅ Raw / Tree / Variabili per ogni call |
| Variabili `{{campo}}` | ✅ | ✅ chip cliccabili tra le card |
| Join tra endpoint | ✗ | ✅ merge su chiave configurabile |
| Error policy per-call | ✗ | ✅ retry/skip/stop/fallback per HTTP code |
| HTML parsing | ✗ | ✅ table extraction senza dipendenze esterne |
| XML nested flatten | ✗ | ✅ namespace-aware, attributi inclusi |
| CSV/TSV auto-detect | parziale | ✅ da Content-Type e fallback |
| OAuth2 CC | ✅ | ✅ + override per singola call |

---

## Requisiti

| Componente | Versione |
|---|---|
| Splunk Enterprise / Heavy Forwarder | 9.0+ |
| Python (Splunk embedded) | 3.7+ |
| Sistema operativo | Linux, macOS, Windows |

Nessuna dipendenza Python esterna — tutto stdlib + splunklib.

---

## Installazione

```bash
# 1. Copia l'app
cp api_connect.spl $SPLUNK_HOME/etc/apps/

# 2. Installa via Splunk CLI
$SPLUNK_HOME/bin/splunk install app $SPLUNK_HOME/etc/apps/api_connect.spl \
    -auth admin:password

# 3. Imposta permessi
chmod 755 $SPLUNK_HOME/etc/apps/api_connect/bin/*.py

# 4. Riavvia
$SPLUNK_HOME/bin/splunk restart
```

Oppure installa da Splunk Web: **Apps → Manage Apps → Install from file**.

---

## Struttura app

```
api_connect/
├── appserver/static/
│   ├── css/api_connect.css              # Stili Splunk-native
│   └── js/
│       ├── api_connect_dashboard.js     # Dashboard + CRUD tabella
│       ├── api_connect_builder.js       # Wizard 7 step + Chain Builder
│       └── api_connect_credentials.js  # Credential Manager CRUD
├── bin/
│   ├── ac_http.py                       # Client HTTP v2 (auth chain, join, parsers)
│   ├── ac_logger.py                     # Logger → _internal
│   ├── ac_input_template.py             # Template script generati
│   ├── api_connect_rest.py              # REST handler: test call + KV proxy
│   └── api_connect_generate.py         # REST handler: genera script + inputs.conf
└── default/
    ├── app.conf / nav/ / setup.xml
    ├── collections.conf                 # KV Store schema
    ├── restmap.conf                     # Endpoint REST custom
    ├── props.conf / transforms.conf
    ├── savedsearches.conf               # 5 ricerche + 2 alert
    ├── macros.conf                      # 5 macro SPL
    ├── eventtypes.conf / tags.conf
    └── data/ui/views/
        ├── dashboard.xml
        ├── input_builder.xml
        └── credential_manager.xml
```

---

## Wizard — 7 step

| Step | Contenuto |
|---|---|
| 1 — Auth globale | Tipo default (Bearer/Basic/API Key/OAuth2 CC) + credenziale da `password.conf`. Ogni call può sovrascriverla. |
| 2 — Chain Builder | Card drag-and-drop: URL, method, headers, body, **auth override per-call**, **error policy per-call**, **join su chiave**. Ogni card ha la risposta live con tab Raw/Tree/Variabili. Tra le card: chip delle variabili disponibili cliccabili per inserirle in URL/body. |
| 3 — Parsing | Tree interattivo popolato dalle risposte del Chain Builder. Click per selezionare campi. Supporto: JSON, CSV, TSV, XML, HTML (table extract), testo+regex. |
| 4 — Tracciato | Mapping al tracciato standard: `time\|hostname\|nomeapp\|tipoazione\|clientip\|username\|tipooperazione\|valorePrima\|valoreDP\|target\|note` |
| 5 — Output | Index, sourcetype, source, host, checkpoint dedup. |
| 6 — Logger | Source per `index=_internal sourcetype=custom_script_logger`. |
| 7 — Genera | Riepilogo + generazione script Python e stanza `inputs.conf`. |

---

## Chain Builder — casi d'uso

### Auth diversa per call
```
Call 1: OAuth2 CC (globale)  →  GET /transactions
Call 2: API Key override      →  GET /transactions/{{id}}/details
Call 3: Basic override        →  POST /hr/lookup  body: {"user":"{{user_name}}"}
```

### Join tra endpoint
```
Call 1: GET /users            →  lista [{id, name, email}]
Call 2: GET /users/{{id}}/roles  join_key="id"  →  merge ruoli su ogni utente
```
Il campo `join_key` cerca nella risposta della call 2 il record con lo stesso valore
di `join_key` della call 1 e fonde i campi sul record corrente.

### Error policy per-call
| Policy | Comportamento |
|---|---|
| `default` | Stop su qualsiasi errore |
| `retry_429` | Retry 3× con backoff esponenziale su HTTP 429 |
| `skip_404` | Skip del record corrente su HTTP 404 |
| `skip_all_4xx` | Skip su qualsiasi 4xx |
| `stop_5xx` | Stop + log su 5xx, skip su 4xx |
| `skip_all` | Skip su qualsiasi errore |

---

## Formati risposta supportati

| Formato | Rilevamento | Note |
|---|---|---|
| JSON | `application/json` o default | Supporta JSONPath, array root, flatten nested |
| JSON array | Se la root è già un array | — |
| CSV | `text/csv` o header riga 1 | DictReader, fallback manuale |
| TSV | `text/tab-separated` | — |
| XML | `text/xml`, `application/xml` o `<` iniziale | Namespace-aware, attributi inclusi, flatten |
| HTML | `text/html` | Estrae `<table>` → righe dict; fallback testo pulito |
| Testo + regex | Manuale | Named groups `(?P<campo>...)` → campi |

---

## Tracciato standard aziendale

```
time | hostname | nomeapp | tipoazione | clientip | username |
tipooperazione | valorePrima | valoreDP | target | note
```

Campi **obbligatori**: `time`, `hostname`, `nomeapp`, `tipoazione`.

Esempio evento generato:
```
time="1718000000" hostname="erp.corp.it" nomeapp="ERP_CORP"
tipoazione="TRANSFER" clientip="10.0.1.55" username="mario.rossi"
valorePrima="5000.00" valoreDP="3500.00"
target="IT60X0542811101000000123456"
```

---

## Logger di esecuzione

Ogni script scrive su `stderr` (catturato da Splunk):

```
index=_internal sourcetype=custom_script_logger
source=api_connect:<nome_input>:runner
```

SPL di monitoraggio:
```spl
`ac_logs`
| rex field=message "records=(?<n>\d+)"
| rex field=message "elapsed_ms=(?<ms>\d+)"
| eval status=if(level="ERROR","ERROR","OK")
| stats latest(_time) as last_run latest(status) as status
         latest(n) as records latest(ms) as ms
  by source
```

---

## Macro SPL disponibili

| Macro | Uso |
|---|---|
| `` `ac_logs` `` | Tutti i log di tutti gli input |
| `` `ac_logs(nome)` `` | Log di un input specifico |
| `` `ac_input(nome)` `` | Eventi di un input specifico |
| `` `ac_tracciato` `` | Tutti gli eventi nel tracciato standard |
| `` `ac_status` `` | Stato corrente di tutti gli input |

---

## Sicurezza

- Le credenziali non transitano nel browser: la test call è eseguita lato server dal REST handler Python con il session token dell'utente corrente.
- I secret sono letti da `password.conf` tramite `splunklib.client` a runtime — mai scritti in chiaro negli script generati.
- Il realm è sempre prefissato `api_connect:<tipo>:<label>` per namespace isolation.

---

## Troubleshooting

| Problema | Soluzione |
|---|---|
| REST handler non risponde | `index=_internal sourcetype=splunkd component=AdminManager` |
| Credenziali non caricate | Verifica realm `api_connect:*` in `storage/passwords` |
| Script non eseguito | `chmod 755 $SPLUNK_HOME/etc/apps/api_connect/bin/ac_input_*.py` |
| KV Store vuoto | Verifica `collections.conf` e riavvia Splunk |
| OAuth2 token error | Controlla `token_url` e credenziali realm |
| 429 non retried | Imposta `error_policy: retry_429` nella call |
| Join non funziona | Verifica che `join_key` esista in entrambe le risposte |

---

## Versione

`2.0.0` — Chain Builder + per-call auth + join + error policy + HTML/XML parsers
