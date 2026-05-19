/**
 * API Connect — Input Builder Wizard JS
 * Gestisce i 7 step del wizard: auth, endpoint, test, parsing,
 * tracciato, output, logger → genera lo script modular input.
 */
require([
  'jquery',
  'splunkjs/mvc',
  'splunkjs/mvc/simplexml/ready!'
], function($, mvc) {
  'use strict';

  var service = mvc.createService();
  var TOTAL_STEPS = 7;
  var currentStep = 1;
  var testResponseData = null; // raw parsed response for tree/parsing
  var callCount = 0;
  var editKey = null; // set if editing existing input

  // State object
  var state = {
    name: '', auth_type: '', credential_realm: '', token_url: '',
    oauth_scope: '', apikey_param: '',
    calls: [], pagination_type: 'none', page_param: '', cursor_path: '',
    max_pages: 100, schedule: '*/5 * * * *',
    response_format: 'json', array_root: '',
    extracted_fields: [],
    field_mapping: {},
    index: '', sourcetype: '', source: '', host: '',
    checkpoint: false, checkpoint_field: '',
    logger_source: ''
  };

  var TRACCIATO_FIELDS = [
    { key: 'time',          label: 'time',           required: true,  hint: 'Timestamp evento (epoch o ISO)' },
    { key: 'hostname',      label: 'hostname',        required: true,  hint: 'Host sorgente evento' },
    { key: 'nomeapp',       label: 'nomeapp',         required: true,  hint: 'Nome applicazione sorgente' },
    { key: 'tipoazione',    label: 'tipoazione',      required: true,  hint: 'Tipo di azione (login, logout, ...)' },
    { key: 'clientip',      label: 'clientip',        required: false, hint: 'IP del client' },
    { key: 'username',      label: 'username',        required: false, hint: 'Utente coinvolto' },
    { key: 'tipooperazione',label: 'tipooperazione',  required: false, hint: 'Tipo operazione specifica' },
    { key: 'valorePrima',   label: 'valore prima',    required: false, hint: 'Valore del campo prima della modifica' },
    { key: 'valoreDP',      label: 'valore dopo',     required: false, hint: 'Valore del campo dopo la modifica' },
    { key: 'target',        label: 'target',          required: false, hint: 'Oggetto/risorsa target' },
    { key: 'note',          label: 'note',            required: false, hint: 'Note aggiuntive libere' }
  ];

  // ---- CRON human preview ----
  var CRON_MAP = {
    '*/1 * * * *': 'ogni 1 minuto',
    '*/2 * * * *': 'ogni 2 minuti',
    '*/5 * * * *': 'ogni 5 minuti',
    '*/10 * * * *': 'ogni 10 minuti',
    '*/15 * * * *': 'ogni 15 minuti',
    '*/30 * * * *': 'ogni 30 minuti',
    '0 * * * *': 'ogni ora',
    '0 */2 * * *': 'ogni 2 ore',
    '0 0 * * *': 'ogni giorno a mezzanotte',
    '0 6 * * *': 'ogni giorno alle 06:00',
  };

  function cronHuman(expr) {
    return CRON_MAP[expr] || expr;
  }

  // ---- Utility ----
  function escHtml(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  // ---- Step navigation ----
  function goToStep(n) {
    if (n < 1 || n > TOTAL_STEPS) return;
    collectCurrentStep();
    currentStep = n;
    renderStep();
  }

  function renderStep() {
    $('.ac-step-panel').removeClass('active');
    $('#step-' + currentStep).addClass('active');

    $('.ac-wizard-step').each(function() {
      var s = parseInt($(this).data('step'), 10);
      $(this).removeClass('active done');
      if (s === currentStep) $(this).addClass('active');
      else if (s < currentStep) $(this).addClass('done');
    });

    $('#step-indicator').text('Step ' + currentStep + ' di ' + TOTAL_STEPS);
    $('#btn-prev').prop('disabled', currentStep === 1);

    if (currentStep === TOTAL_STEPS) {
      $('#btn-next').hide();
      $('#btn-generate').show();
      renderSummary();
    } else {
      $('#btn-next').show();
      $('#btn-generate').hide();
    }

    if (currentStep === 4) renderParsingTree();
    if (currentStep === 5) renderTracciato();
    if (currentStep === 6) loadIndexes();
  }

  // ---- Collect state from current step ----
  function collectCurrentStep() {
    switch (currentStep) {
      case 1:
        state.name = $('#f-name').val().trim();
        state.auth_type = $('#f-auth-type').val();
        state.credential_realm = $('#f-credential').val();
        state.token_url = $('#f-token-url').val().trim();
        state.oauth_scope = $('#f-oauth-scope').val().trim();
        state.apikey_param = $('#f-apikey-param').val().trim();
        break;
      case 2:
        collectCalls();
        state.pagination_type = $('#f-pagination').val();
        state.page_param = $('#f-page-param').val().trim();
        state.cursor_path = $('#f-cursor-path').val().trim();
        state.max_pages = parseInt($('#f-max-pages').val(), 10) || 100;
        state.schedule = $('#f-schedule').val().trim();
        break;
      case 3: break; // test only, nothing to collect
      case 4:
        state.response_format = $('#f-response-format').val();
        state.array_root = $('#f-array-root').val().trim();
        state.extracted_fields = collectExtractedFields();
        break;
      case 5:
        collectTracciato();
        break;
      case 6:
        state.index = $('#f-index').val();
        state.sourcetype = $('#f-sourcetype').val().trim();
        state.source = $('#f-source').val().trim();
        state.host = $('#f-host').val().trim();
        state.checkpoint = $('#f-checkpoint').is(':checked');
        state.checkpoint_field = $('#f-checkpoint-field').val().trim();
        break;
      case 7:
        state.logger_source = $('#f-logger-source').val().trim();
        break;
    }
  }

  function collectCalls() {
    state.calls = [];
    $('.ac-call-card').each(function(i) {
      var $c = $(this);
      state.calls.push({
        url: $c.find('.call-url').val().trim(),
        method: $c.find('.call-method').val(),
        headers: $c.find('.call-headers').val().trim(),
        body: $c.find('.call-body').val().trim(),
        chain_input: $c.find('.call-chain-input').val().trim()
      });
    });
  }

  function collectExtractedFields() {
    var fields = [];
    $('.ac-field-row').each(function() {
      var path = $(this).find('.field-path').val().trim();
      var alias = $(this).find('.field-alias').val().trim();
      if (path) fields.push({ path: path, alias: alias || path.split('.').pop() });
    });
    return fields;
  }

  function collectTracciato() {
    TRACCIATO_FIELDS.forEach(function(tf) {
      state.field_mapping[tf.key] = $('#map-' + tf.key).val().trim();
    });
  }

  // ---- Step 1: Auth type toggle ----
  $('#f-auth-type').on('change', function() {
    var v = $(this).val();
    $('#auth-credential-group').toggle(v !== '' && v !== 'none');
    $('#auth-oauth2-group').toggle(v === 'oauth2_cc');
    $('#auth-apikey-group').toggle(v === 'api_key_header' || v === 'api_key_query');
    if (v !== 'none' && v !== '') loadCredentials();
  });

  function loadCredentials() {
    service.get('/servicesNS/-/api_connect/storage/passwords', { count: 200, output_mode: 'json' }, function(err, resp) {
      if (err) return;
      var $sel = $('#f-credential').empty().append('<option value="">— Seleziona credenziale —</option>');
      var entries = (resp.data && resp.data.entry) ? resp.data.entry : [];
      entries.filter(function(e){ return e.content && e.content.realm && e.content.realm.indexOf('api_connect:') === 0; })
        .forEach(function(e) {
          var realm = e.content.realm;
          var label = realm.replace('api_connect:', '') + ' (' + e.content.username + ')';
          $sel.append('<option value="' + escHtml(realm) + '">' + escHtml(label) + '</option>');
        });
    });
  }

  // ---- Step 2: Endpoint calls ----
  function addCallCard(data) {
    callCount++;
    var idx = callCount;
    var d = data || { url: '', method: 'GET', headers: '', body: '', chain_input: '' };
    var $card = $([
      '<div class="ac-call-card" data-call="' + idx + '">',
        '<div class="ac-call-card-header">',
          '<span class="ac-call-number">Chiamata ' + idx + '</span>',
          idx > 1 ? '<button class="btn btn-default btn-sm ac-btn-remove-call"><i class="icon-minus"></i> Rimuovi</button>' : '',
        '</div>',
        '<div class="control-group">',
          '<label class="control-label">URL <span class="ac-required">*</span></label>',
          '<div class="controls">',
            '<input type="text" class="input-xlarge call-url" value="' + escHtml(d.url) + '" placeholder="https://api.example.com/v1/events"/>',
            idx > 1 ? '<span class="help-block">Puoi usare <code>{{campo}}</code> per inserire valori dalla chiamata precedente.</span>' : '',
          '</div>',
        '</div>',
        '<div class="control-group">',
          '<label class="control-label">Metodo</label>',
          '<div class="controls" style="display:flex;gap:8px;align-items:center">',
            '<select class="call-method input-small">',
              '<option value="GET"' + (d.method==='GET'?' selected':'') + '>GET</option>',
              '<option value="POST"' + (d.method==='POST'?' selected':'') + '>POST</option>',
              '<option value="PUT"' + (d.method==='PUT'?' selected':'') + '>PUT</option>',
              '<option value="PATCH"' + (d.method==='PATCH'?' selected':'') + '>PATCH</option>',
              '<option value="DELETE"' + (d.method==='DELETE'?' selected':'') + '>DELETE</option>',
            '</select>',
          '</div>',
        '</div>',
        '<div class="control-group">',
          '<label class="control-label">Headers aggiuntivi (JSON)</label>',
          '<div class="controls">',
            '<textarea class="input-xlarge call-headers" rows="2" placeholder=\'{"X-Custom": "value"}\'>' + escHtml(d.headers) + '</textarea>',
          '</div>',
        '</div>',
        '<div class="control-group">',
          '<label class="control-label">Body (per POST/PUT/PATCH)</label>',
          '<div class="controls">',
            '<textarea class="input-xlarge call-body" rows="3" placeholder=\'{"filter": "{{token_from_prev_call}}"}\'>' + escHtml(d.body) + '</textarea>',
          '</div>',
        '</div>',
      '</div>'
    ].join(''));
    $('#ac-calls-container').append($card);
  }

  $('#btn-add-call').on('click', function() { addCallCard(); });
  $(document).on('click', '.ac-btn-remove-call', function() {
    $(this).closest('.ac-call-card').remove();
  });

  $('#f-schedule').on('input', function() {
    $('#cron-preview').text(cronHuman($(this).val().trim()));
  });

  $('#f-pagination').on('change', function() {
    $('#pagination-details').toggle($(this).val() !== 'none');
  });

  // ---- Step 3: Test call ----
  $('#btn-run-test').on('click', function() {
    collectCurrentStep();
    if (!state.calls.length || !state.calls[0].url) {
      $('#test-status').text('Configura prima URL e metodo nello Step 2.').css('color','#c62828');
      return;
    }

    var $btn = $(this);
    $btn.prop('disabled', true).find('i').addClass('ac-spin');
    $('#test-status').text('Esecuzione in corso...').css('color','#8b959e');
    $('#test-response-wrap').hide();

    var payload = {
      auth_type: state.auth_type,
      credential_realm: state.credential_realm,
      token_url: state.token_url,
      apikey_param: state.apikey_param,
      calls: JSON.stringify(state.calls)
    };

    service.post('/servicesNS/nobody/api_connect/api_connect_test', payload, function(err, resp) {
      $btn.prop('disabled', false).find('i').removeClass('ac-spin');
      if (err) {
        $('#test-status').text('Errore: ' + err.message).css('color','#c62828');
        return;
      }

      var result = resp.data || {};
      var httpCode = result.status_code || '—';
      var latency = result.latency_ms ? result.latency_ms + ' ms' : '—';
      var ctype = result.content_type || '';
      var body = result.body || '';

      // HTTP code badge
      var codeCls = httpCode >= 200 && httpCode < 300 ? 'ac-badge--2xx' : httpCode >= 400 && httpCode < 500 ? 'ac-badge--4xx' : 'ac-badge--5xx';
      $('#test-http-code').attr('class', 'ac-badge ' + codeCls).text('HTTP ' + httpCode);
      $('#test-latency').text(latency);
      $('#test-content-type').text(ctype);

      $('#test-status').text('Completato').css('color','#2e7d32');

      // Raw
      $('#test-raw-output').text(body);

      // Pretty (JSON)
      try {
        testResponseData = JSON.parse(body);
        $('#test-pretty-output').text(JSON.stringify(testResponseData, null, 2));
      } catch(e) {
        testResponseData = body;
        $('#test-pretty-output').text(body);
      }

      // Tree
      if (typeof testResponseData === 'object') {
        $('#test-tree-output').html(renderJsonTree(testResponseData, '', false));
      } else {
        $('#test-tree-output').text('Risposta non JSON — usa tab Raw.');
      }

      $('#test-response-wrap').show();
    });
  });

  // Tab switching
  $(document).on('click', '.ac-tab', function() {
    var target = $(this).data('target');
    $(this).siblings().removeClass('active');
    $(this).addClass('active');
    $('.ac-tab-panel').removeClass('active');
    $('#' + target).addClass('active');
  });

  // ---- JSON Tree renderer ----
  function renderJsonTree(data, path, selectable) {
    var cls = selectable ? 'ac-json-tree--selectable' : '';
    return '<div class="' + cls + '">' + renderNode(data, path || '$', selectable) + '</div>';
  }

  function renderNode(val, path, selectable) {
    if (val === null) return spanNull('null');
    if (typeof val === 'boolean') return spanBool(String(val));
    if (typeof val === 'number') return spanNum(String(val));
    if (typeof val === 'string') {
      if (!selectable) return spanStr('"' + escHtml(val) + '"');
      return '<span class="jt-leaf" data-path="' + escHtml(path) + '" title="' + escHtml(path) + '">' +
             '<span class="jt-str">"' + escHtml(val.length > 60 ? val.substring(0,60)+'...' : val) + '"</span></span>';
    }
    if (Array.isArray(val)) {
      if (val.length === 0) return '<span class="jt-punct">[]</span>';
      var items = val.slice(0,10).map(function(v, i) {
        return '<div style="padding-left:16px">' + renderNode(v, path+'['+i+']', selectable) + '</div>';
      }).join('');
      if (val.length > 10) items += '<div style="padding-left:16px;color:#8b959e;font-style:italic">... (' + (val.length-10) + ' altri) ...</div>';
      return '<span class="jt-collapse" title="' + escHtml(path) + '">[</span>' + items + '<span>]</span>';
    }
    if (typeof val === 'object') {
      var keys = Object.keys(val);
      if (keys.length === 0) return '<span class="jt-punct">{}</span>';
      var pairs = keys.slice(0,50).map(function(k) {
        var childPath = path + '.' + k;
        var keySpan = '<span class="jt-key">"' + escHtml(k) + '"</span>: ';
        var childVal = val[k];
        if (selectable && (typeof childVal === 'string' || typeof childVal === 'number' || typeof childVal === 'boolean')) {
          return '<div style="padding-left:16px"><span class="jt-leaf" data-path="' + escHtml(childPath) + '" title="' + escHtml(childPath) + '">' + keySpan + renderNode(childVal, childPath, false) + '</span></div>';
        }
        return '<div style="padding-left:16px">' + keySpan + renderNode(childVal, childPath, selectable) + '</div>';
      }).join('');
      if (keys.length > 50) pairs += '<div style="padding-left:16px;color:#8b959e;font-style:italic">... (' + (keys.length-50) + ' altri campi) ...</div>';
      return '<span class="jt-collapse">{</span>' + pairs + '<span>}</span>';
    }
    return escHtml(String(val));
  }

  function spanStr(s) { return '<span class="jt-str">' + s + '</span>'; }
  function spanNum(s) { return '<span class="jt-num">' + s + '</span>'; }
  function spanBool(s) { return '<span class="jt-bool">' + s + '</span>'; }
  function spanNull(s) { return '<span class="jt-null">' + s + '</span>'; }

  // ---- Step 4: Parsing ----
  function renderParsingTree() {
    var $tree = $('#parsing-tree');
    if (!testResponseData) {
      $tree.html('<p class="ac-hint">Esegui prima il test (Step 3) per visualizzare il tree.</p>');
      return;
    }
    $tree.html(renderJsonTree(testResponseData, '$', true));
  }

  // Click on leaf to add field
  $(document).on('click', '#parsing-tree .jt-leaf', function() {
    var path = $(this).data('path');
    $(this).toggleClass('selected');
    if ($(this).hasClass('selected')) {
      addFieldRow(path, '');
    } else {
      // Remove field row with same path
      $('.ac-field-row').filter(function() {
        return $(this).find('.field-path').val() === path;
      }).remove();
    }
  });

  $('#btn-add-field').on('click', function() {
    addFieldRow('', '');
  });

  function addFieldRow(path, alias) {
    // Avoid duplicates
    var exists = false;
    $('.field-path').each(function() { if ($(this).val() === path && path) exists = true; });
    if (exists) return;

    var $row = $([
      '<div class="ac-field-row">',
        '<input type="text" class="input-medium field-path ac-field-path" value="' + escHtml(path) + '" placeholder="$.campo oppure regex"/>',
        '<span style="color:#8b959e;flex-shrink:0">→</span>',
        '<input type="text" class="input-medium field-alias" value="' + escHtml(alias) + '" placeholder="alias (opzionale)"/>',
        '<button class="btn btn-default btn-sm ac-btn-remove-field" type="button"><i class="icon-trash"></i></button>',
      '</div>'
    ].join(''));
    $('#ac-fields-list').append($row);
  }

  $(document).on('click', '.ac-btn-remove-field', function() {
    $(this).closest('.ac-field-row').remove();
  });

  // ---- Step 5: Tracciato ----
  function renderTracciato() {
    var fields = state.extracted_fields;
    var fieldOptions = '<option value="">— non mappato —</option>' +
      fields.map(function(f) {
        var lbl = f.alias || f.path;
        return '<option value="' + escHtml(f.path) + '">' + escHtml(lbl) + '</option>';
      }).join('') +
      '<option value="__static__">valore statico...</option>';

    var rows = TRACCIATO_FIELDS.map(function(tf) {
      var sel = state.field_mapping[tf.key] || '';
      return [
        '<div class="ac-tracciato-row">',
          '<label class="ac-tracciato-label">' + tf.label + (tf.required ? '<span class="ac-required">*</span>' : '') + '</label>',
          '<select id="map-' + tf.key + '" class="input-xlarge ac-map-select">',
            fieldOptions,
          '</select>',
          '<div class="ac-tracciato-hint">' + tf.hint + '</div>',
        '</div>'
      ].join('');
    }).join('');

    $('#ac-tracciato-grid').html(rows);

    // Restore selections
    TRACCIATO_FIELDS.forEach(function(tf) {
      if (state.field_mapping[tf.key]) {
        $('#map-' + tf.key).val(state.field_mapping[tf.key]);
      }
    });
  }

  // ---- Step 6: Load indexes ----
  function loadIndexes() {
    service.get('/servicesNS/-/-/data/indexes', { count: 100, output_mode: 'json' }, function(err, resp) {
      if (err) return;
      var $sel = $('#f-index').empty().append('<option value="">— Seleziona index —</option>');
      var entries = (resp.data && resp.data.entry) ? resp.data.entry : [];
      entries.filter(function(e){ return !e.name.startsWith('_'); })
        .forEach(function(e) {
          $sel.append('<option value="' + escHtml(e.name) + '">' + escHtml(e.name) + '</option>');
        });
      if (state.index) $sel.val(state.index);
    });
  }

  $('#f-checkpoint').on('change', function() {
    $('#checkpoint-detail').toggle($(this).is(':checked'));
  });

  // ---- Step 7: Summary ----
  function renderSummary() {
    collectCurrentStep();
    var kv = [
      ['Nome input', state.name || '—'],
      ['Auth', state.auth_type || '—'],
      ['Credenziale', state.credential_realm || '—'],
      ['Endpoint URL', (state.calls[0] || {}).url || '—'],
      ['Metodo', (state.calls[0] || {}).method || '—'],
      ['Chiamate cascata', state.calls.length],
      ['Paginazione', state.pagination_type],
      ['Schedule', state.schedule || '—'],
      ['Formato risposta', state.response_format],
      ['Campi estratti', state.extracted_fields.length],
      ['Campi tracciato mappati', Object.values(state.field_mapping).filter(Boolean).length + ' / ' + TRACCIATO_FIELDS.length],
      ['Index', state.index || '—'],
      ['Sourcetype', state.sourcetype || '—'],
      ['Source', state.source || '—'],
      ['Logger source', state.logger_source || '—'],
    ];
    var html = '<div class="ac-summary-kv">' +
      kv.map(function(p) {
        return '<span class="ac-summary-key">' + escHtml(p[0]) + '</span><span class="ac-summary-val">' + escHtml(String(p[1])) + '</span>';
      }).join('') +
      '</div>';
    $('#ac-summary-content').html(html);
  }

  // ---- Generate ----
  $('#btn-generate').on('click', function() {
    collectCurrentStep();

    // Validation
    var errors = [];
    if (!state.name) errors.push('Nome input mancante (Step 1)');
    if (!state.auth_type) errors.push('Tipo autenticazione non selezionato (Step 1)');
    if (!state.calls.length || !state.calls[0].url) errors.push('URL endpoint mancante (Step 2)');
    if (!state.schedule) errors.push('Schedule (cron) mancante (Step 2)');
    if (!state.index) errors.push('Index di destinazione mancante (Step 6)');
    if (!state.sourcetype) errors.push('Sourcetype mancante (Step 6)');
    if (!state.source) errors.push('Source mancante (Step 6)');
    if (!state.logger_source) errors.push('Logger source mancante (Step 7)');
    // Required tracciato
    TRACCIATO_FIELDS.filter(function(tf){ return tf.required; }).forEach(function(tf) {
      if (!state.field_mapping[tf.key]) errors.push('Campo obbligatorio tracciato non mappato: ' + tf.label);
    });

    if (errors.length) {
      alert('Errori di validazione:\n• ' + errors.join('\n• '));
      return;
    }

    $('#ac-gen-modal').show();
    $('#gen-modal-title').text('Generazione in corso...');
    $('#gen-modal-body').html('<div class="ac-spinner"><i class="icon-rotate-right"></i> Generazione script e configurazione...</div>');
    $('#gen-modal-footer').hide();

    var payload = { config: JSON.stringify(state) };
    if (editKey) payload._key = editKey;

    service.post('/servicesNS/nobody/api_connect/api_connect_generate', payload, function(err, resp) {
      if (err) {
        $('#gen-modal-title').text('Errore generazione');
        $('#gen-modal-body').html('<p style="color:#c62828">' + escHtml(err.message) + '</p>');
        $('#gen-modal-footer').show().find('#btn-view-script').hide();
        return;
      }

      var r = resp.data || {};
      $('#gen-modal-title').text('Input generato con successo!');
      $('#gen-modal-body').html([
        '<div class="ac-summary-box">',
          '<div class="ac-summary-kv">',
            '<span class="ac-summary-key">Script</span><span class="ac-summary-val">' + escHtml(r.script_path || '—') + '</span>',
            '<span class="ac-summary-key">inputs.conf</span><span class="ac-summary-val">' + escHtml(r.stanza || '—') + '</span>',
          '</div>',
        '</div>',
        r.script_preview ? '<pre class="ac-code-block" style="margin-top:12px;max-height:300px">' + escHtml(r.script_preview) + '</pre>' : ''
      ].join(''));
      $('#gen-modal-footer').show();
    });
  });

  $('#btn-view-script').on('click', function() {
    // Already shown in modal body
  });

  // ---- Save draft ----
  $('#btn-save-draft').on('click', function() {
    collectCurrentStep();
    var payload = JSON.stringify(state);
    try { localStorage.setItem('ac_draft_' + (state.name || 'draft'), payload); } catch(e) {}
    // Also save to KV Store as draft
    service.post('/servicesNS/nobody/api_connect/storage/collections/data/api_connect_inputs', {
      name: state.name || '__draft__',
      config: payload,
      last_status: 'DRAFT'
    }, function() {});
    alert('Bozza salvata.');
  });

  // ---- Prev / Next ----
  $('#btn-prev').on('click', function() { goToStep(currentStep - 1); });
  $('#btn-next').on('click', function() { goToStep(currentStep + 1); });

  // ---- Check for edit mode ----
  var urlParams = new URLSearchParams(window.location.search);
  editKey = urlParams.get('edit');
  if (editKey) {
    service.get('/servicesNS/nobody/api_connect/storage/collections/data/api_connect_inputs/' + editKey, {}, function(err, resp) {
      if (err || !resp.data) return;
      try {
        var saved = JSON.parse(resp.data.config || '{}');
        $.extend(state, saved);
        populateStep1();
      } catch(e) {}
    });
  }

  function populateStep1() {
    $('#f-name').val(state.name);
    $('#f-auth-type').val(state.auth_type).trigger('change');
    $('#f-schedule').val(state.schedule);
    $('#cron-preview').text(cronHuman(state.schedule));
  }

  // ---- Init ----
  addCallCard(); // first call card
  renderStep();
});
