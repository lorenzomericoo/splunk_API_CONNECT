# API Connect for Splunk

Wizard GUI per la creazione di **Modular Input** che interrogano API REST esterne,
con parsing visuale della risposta e mapping automatico al tracciato standard aziendale.

---

## Requisiti

| Componente | Versione minima |
|---|---|
| Splunk Enterprise / Heavy Forwarder | 9.0+ |
| Python (Splunk embedded) | 3.7+ |
| Sistema operativo | Linux, macOS, Windows |

---

## Installazione

1. Copia la cartella `api_connect/` in:
   ```
   $SPLUNK_HOME/etc/apps/api_connect/
   ```

2. Imposta i permessi:
   ```bash
   chmod -R 755 $SPLUNK_HOME/etc/apps/api_connect/bin/*.py
   ```

3. Riavvia Splunk:
   ```bash
   $SPLUNK_HOME/bin/splunk restart
   ```

4. Apri Splunk Web e vai su **App → API Connect**.

---

## Struttura app

```
api_connect/
├── appserver/
│   └── static/
│       ├── css/api_connect.css          # Stili (usa variabili CSS Splunk)
│       └── js/
│           ├── api_connect_dashboard.js # Dashboard logic
│           ├── api_connect_builder.js   # Wizard 7 step
│           └── api_connect_credentials.js # Credential Manager
├── bin/
│   └── api_connect_rest.py              # REST handlers (test, generate, inputs)
├── default/
│   ├── app.conf
│   ├── collections.conf                 # KV Store schema
│   ├── inputs.conf
│   ├── nav/default.xml
│   ├── props.conf
│   ├── restmap.conf                     # Custom REST endpoint declaration
│   ├── setup.xml
│   ├── transforms.conf
│   ├── web.conf
│   └── data/ui/views/
│       ├── dashboard.xml
│       ├── input_builder.xml
│       └── credential_manager.xml
└── metadata/
    └── default.meta
```

---

## Funzionalità

### Dashboard
- Tabella di tutti gli input configurati con stato, log inviati, latenza, prossima esecuzione
- Bottoni edit/delete per ogni input
- Counter riassuntivi in cima

### Input Builder (wizard 7 step)

| Step | Contenuto |
|---|---|
| 1 — Autenticazione | Tipo (Bearer, Basic, API Key Header/Query, OAuth2 CC) + credenziale da password.conf |
| 2 — Endpoint | URL, metodo, header, body, chiamate in cascata, paginazione (offset/cursor/link), cron schedule |
| 3 — Test chiamata | Chiamata live lato server, risposta visualizzata in Raw / Pretty / Tree |
| 4 — Parsing | Click sul campo JSON nel tree per estrarlo, oppure JSONPath manuale. Supporto JSON, JSON array, CSV, TSV, XML, testo + regex |
| 5 — Tracciato standard | Mapping dei campi estratti ai campi aziendali: `time\|hostname\|nomeapp\|tipoazione\|clientip\|username\|tipooperazione\|valorePrima\|valoreDP\|target\|note` |
| 6 — Output | Index, sourcetype, source, host, checkpoint dedup |
| 7 — Logger | Source per `index=_internal sourcetype=custom_script_logger` |

### Credential Manager
- CRUD completo su `password.conf` tramite `/storage/passwords` REST API
- Supporto Bearer Token, Basic Auth, API Key, OAuth2 Client Credentials
- Nessun secret in chiaro: tutto gestito da Splunk

---

## Tracciato standard aziendale

```
time | hostname | nomeapp | tipoazione | clientip | username |
tipooperazione | valorePrima | valoreDP | target | note
```

I campi **time**, **hostname**, **nomeapp**, **tipoazione** sono obbligatori al mapping.

---

## Logger di esecuzione

Ogni script generato usa un logger Python configurato per scrivere in:

```
index=_internal sourcetype=custom_script_logger source=<logger_source>
```

Ricerca SPL per monitorare l'esecuzione:
```spl
index=_internal sourcetype=custom_script_logger source="api_connect:*"
| eval status=if(like(message, "%Errore%") OR like(message, "%error%"), "ERROR", "OK")
| stats count by source, status
```

---

## Formato eventi generati

```
time="1718000000" hostname="api.example.com" nomeapp="MyApp"
tipoazione="login" clientip="192.168.1.1" username="jdoe"
tipooperazione="authentication" target="portal" note=""
```

---

## Note di sicurezza

- Le credenziali non transitano mai nel browser: la chiamata di test è eseguita lato server dal REST handler Python
- I secret sono letti da `password.conf` tramite `splunklib.client` con il session token dell'utente corrente
- I file `.py` generati non contengono secret in chiaro: leggono sempre da `password.conf` a runtime

---

## Troubleshooting

| Problema | Soluzione |
|---|---|
| REST handler non risponde | Verifica `$SPLUNK_HOME/var/log/splunk/splunkd.log` per errori Python |
| Credenziali non caricate | Controlla che il realm inizi con `api_connect:` |
| Script non eseguito | Verifica permessi `chmod 755` su `bin/ac_input_*.py` |
| KV Store vuoto | Controlla che `collections.conf` sia presente e Splunk sia stato riavviato |

---

## Versione

`1.0.0` — Generata da API Connect Wizard
