/**
 * API Connect — Input Builder Wizard v2
 * Step 2 è ora un Chain Builder stile Postman:
 *  - card per ogni call con URL, method, headers, body, auth override, error policy
 *  - test live per-call con risposta (Raw/Tree/Variabili) nel pannello destro
 *  - connettori con chip delle variabili disponibili dalla call precedente
 *  - drag-to-reorder
 *  - cascade: {{variabile}} negli URL/body delle call successive
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
  var editKey = null;

  /* ── State ─────────────────────────────────────────────────── */
  var state = {
    name: '', auth_type: 'none', credential_realm: '',
    token_url: '', oauth_scope: '', apikey_param: '',
    /* calls: array di oggetti call (vedi newCallObj) */
    calls: [],
    pagination_type: 'none', page_param: 'page', cursor_path: '',
    max_pages: 100, schedule: '*/5 * * * *',
    response_format: 'json', array_root: '',
    extracted_fields: [], field_mapping: {},
    index: '', sourcetype: '', source: '', host: '',
    checkpoint: false, checkpoint_field: '',
    logger_source: ''
  };

  /* Le risposte live di ogni call (indicizzate per callId) */
  var callResponses = {};
  /* Variabili disponibili per ogni slot (indicizzate per callId) */
  var callVars = {};
  /* Contatore univoco per le call card */
  var callIdSeq = 0;

  var TRACCIATO_FIELDS = [
    { key:'time',           label:'time',           required:true,  hint:'Timestamp (epoch o ISO)' },
    { key:'hostname',       label:'hostname',        required:true,  hint:'Host sorgente evento' },
    { key:'nomeapp',        label:'nomeapp',         required:true,  hint:'Nome applicazione sorgente' },
    { key:'tipoazione',     label:'tipoazione',      required:true,  hint:'Tipo azione (login, logout…)' },
    { key:'clientip',       label:'clientip',        required:false, hint:'IP del client' },
    { key:'username',       label:'username',        required:false, hint:'Utente coinvolto' },
    { key:'tipooperazione', label:'tipooperazione',  required:false, hint:'Tipo operazione specifica' },
    { key:'valorePrima',    label:'valore prima',    required:false, hint:'Valore prima della modifica' },
    { key:'valoreDP',       label:'valore dopo',     required:false, hint:'Valore dopo la modifica' },
    { key:'target',         label:'target',          required:false, hint:'Risorsa target' },
    { key:'note',           label:'note',            required:false, hint:'Note aggiuntive libere' }
  ];

  var CRON_MAP = {
    '*/1 * * * *':'ogni 1 minuto','*/2 * * * *':'ogni 2 minuti',
    '*/5 * * * *':'ogni 5 minuti','*/10 * * * *':'ogni 10 minuti',
    '*/15 * * * *':'ogni 15 minuti','*/30 * * * *':'ogni 30 minuti',
    '0 * * * *':'ogni ora','0 */2 * * *':'ogni 2 ore',
    '0 0 * * *':'ogni giorno'
  };

  /* ── Utility ────────────────────────────────────────────────── */
  function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function cronHuman(e){ return CRON_MAP[e]||e; }

  function newCallObj(){
    callIdSeq++;
    return {
      id: callIdSeq, name: 'Chiamata '+callIdSeq,
      url:'', method:'GET', headers:'{}', body:'',
      auth_type:'inherited', credential_realm:'',
      apikey_param:'', token_url:'',
      error_policy:'default',
      join_key: '', join_mode: 'merge'
    };
  }

  /* ── Step navigation ─────────────────────────────────────────── */
  function goToStep(n){
    if(n<1||n>TOTAL_STEPS) return;
    collectCurrentStep();
    currentStep=n;
    renderStep();
  }

  function renderStep(){
    $('.ac-step-panel').removeClass('active');
    $('#step-'+currentStep).addClass('active');
    $('.ac-wizard-step').each(function(){
      var s=parseInt($(this).data('step'),10);
      $(this).removeClass('active done');
      if(s===currentStep) $(this).addClass('active');
      else if(s<currentStep) $(this).addClass('done');
    });
    $('#step-indicator').text('Step '+currentStep+' di '+TOTAL_STEPS);
    $('#btn-prev').prop('disabled',currentStep===1);
    if(currentStep===TOTAL_STEPS){
      $('#btn-next').hide(); $('#btn-generate').show(); renderSummary();
    } else {
      $('#btn-next').show(); $('#btn-generate').hide();
    }
    if(currentStep===2) renderChain();
    if(currentStep===4) renderParsingTab();
    if(currentStep===5) renderTracciato();
    if(currentStep===6) loadIndexes();
  }

  /* ── Collect state ───────────────────────────────────────────── */
  function collectCurrentStep(){
    switch(currentStep){
      case 1:
        state.name=$('#f-name').val().trim();
        state.auth_type=$('#f-auth-type').val();
        state.credential_realm=$('#f-credential').val();
        state.token_url=$('#f-token-url').val().trim();
        state.oauth_scope=$('#f-oauth-scope').val().trim();
        state.apikey_param=$('#f-apikey-param').val().trim();
        break;
      case 2: collectChainState(); break;
      case 3: break;
      case 4:
        state.response_format=$('#f-response-format').val();
        state.array_root=$('#f-array-root').val().trim();
        state.extracted_fields=collectExtractedFields();
        break;
      case 5: collectTracciato(); break;
      case 6:
        state.index=$('#f-index').val();
        state.sourcetype=$('#f-sourcetype').val().trim();
        state.source=$('#f-source').val().trim();
        state.host=$('#f-host').val().trim();
        state.checkpoint=$('#f-checkpoint').is(':checked');
        state.checkpoint_field=$('#f-checkpoint-field').val().trim();
        break;
      case 7:
        state.logger_source=$('#f-logger-source').val().trim();
        break;
    }
  }

  function collectChainState(){
    state.schedule=$('#f-schedule').val().trim();
    state.pagination_type=$('#f-pagination').val();
    state.page_param=$('#f-page-param').val().trim();
    state.cursor_path=$('#f-cursor-path').val().trim();
    state.max_pages=parseInt($('#f-max-pages').val(),10)||100;
    state.calls=[];
    $('.ac-call-card').each(function(){
      var $c=$(this);
      var id=parseInt($c.data('call-id'),10);
      var obj=state.calls.filter(function(c){return c.id===id;})[0]||{id:id};
      obj.name=$c.find('.ac-call-name-input').val().trim()||'Chiamata';
      obj.url=$c.find('.ac-call-url').val().trim();
      obj.method=$c.find('.ac-call-method').val();
      obj.headers=$c.find('.ac-call-headers').val().trim();
      obj.body=$c.find('.ac-call-body-input').val().trim();
      obj.auth_type=$c.find('.ac-call-auth-type').val();
      obj.credential_realm=$c.find('.ac-call-credential').val();
      obj.apikey_param=$c.find('.ac-call-apikey-param').val();
      obj.error_policy=$c.find('.ac-call-error-policy').val();
      obj.join_key=$c.find('.ac-call-join-key').val().trim();
      state.calls.push(obj);
    });
  }

  /* ── Step 1: Auth ────────────────────────────────────────────── */
  $('#f-auth-type').on('change',function(){
    var v=$(this).val();
    $('#auth-credential-group').toggle(v!==''&&v!=='none');
    $('#auth-oauth2-group').toggle(v==='oauth2_cc');
    $('#auth-apikey-group').toggle(v==='api_key_header'||v==='api_key_query');
    if(v!=='none'&&v!=='') loadCredentials('#f-credential');
  });

  function loadCredentials(sel){
    service.get('/servicesNS/-/api_connect/storage/passwords',{count:200,output_mode:'json'},function(err,resp){
      if(err) return;
      var $s=$(sel).empty().append('<option value="">— Seleziona credenziale —</option>');
      ((resp.data&&resp.data.entry)||[])
        .filter(function(e){return e.content&&e.content.realm&&e.content.realm.indexOf('api_connect:')===0;})
        .forEach(function(e){
          var r=e.content.realm;
          $s.append('<option value="'+esc(r)+'">'+esc(r.replace('api_connect:',''))+' ('+esc(e.content.username)+')</option>');
        });
    });
  }

  /* ── Step 2: Chain Builder ───────────────────────────────────── */
  function renderChain(){
    var $area=$('#ac-chain-area');
    if($area.find('.ac-call-card').length===0&&state.calls.length===0){
      addCallToChain();
    } else if(state.calls.length>0&&$area.find('.ac-call-card').length===0){
      state.calls.forEach(function(c){ addCallToChain(c); });
    }
    updateAllConnectors();
  }

  function addCallToChain(data){
    var call=data||newCallObj();
    if(!data) state.calls.push(call);
    var $area=$('#ac-chain-area');
    /* Connettore prima della card (tranne la prima) */
    if($area.find('.ac-call-card').length>0){
      $area.append(buildConnectorHtml(call.id));
    }
    $area.append(buildCallCardHtml(call));
    /* Carica credenziali nel select della call */
    loadCredentials('#ac-call-cred-'+call.id);
    /* Init response area */
    showRespPlaceholder(call.id);
  }

  function buildCallCardHtml(c){
    var authOverrideOptions=[
      '<option value="inherited"'+(c.auth_type==='inherited'?' selected':'')+'> Ereditata (auth globale)</option>',
      '<option value="none"'+(c.auth_type==='none'?' selected':'')+'> Nessuna</option>',
      '<option value="bearer"'+(c.auth_type==='bearer'?' selected':'')+'> Bearer Token</option>',
      '<option value="basic"'+(c.auth_type==='basic'?' selected':'')+'> Basic Auth</option>',
      '<option value="api_key_header"'+(c.auth_type==='api_key_header'?' selected':'')+'> API Key Header</option>',
      '<option value="api_key_query"'+(c.auth_type==='api_key_query'?' selected':'')+'> API Key Query</option>',
      '<option value="oauth2_cc"'+(c.auth_type==='oauth2_cc'?' selected':'')+'> OAuth2 CC</option>'
    ].join('');
    var errorPolicyOptions=[
      '<option value="default">Default (stop on error)</option>',
      '<option value="retry_429">429 → retry 3× backoff</option>',
      '<option value="skip_404">404 → skip record</option>',
      '<option value="skip_all_4xx">4xx → skip record</option>',
      '<option value="stop_5xx">5xx → stop + log _internal</option>',
      '<option value="skip_all">Tutti gli errori → skip</option>'
    ].join('');
    return [
      '<div class="ac-call-card" data-call-id="'+c.id+'">',
        '<div class="ac-call-header">',
          '<span class="ac-drag-handle" title="Trascina per riordinare">⠿</span>',
          '<div class="ac-call-num">'+c.id+'</div>',
          '<input class="ac-call-name-input" value="'+esc(c.name||'Chiamata '+c.id)+'" style="border:none;background:transparent;font-weight:600;font-size:13px;flex:1;outline:none;color:inherit"/>',
          '<span class="ac-method-badge ac-method-GET ac-call-method-badge">GET</span>',
          '<span class="ac-auth-tag ac-call-auth-display">inherited</span>',
          '<div class="ac-status-dot" id="ac-dot-'+c.id+'"></div>',
          '<span class="ac-call-latency" id="ac-lat-'+c.id+'"></span>',
          '<button class="btn btn-default ac-btn-sm ac-run-call-btn" data-call-id="'+c.id+'" title="Test questa call">▶ Run</button>',
          '<button class="btn btn-default ac-btn-sm ac-remove-call-btn" data-call-id="'+c.id+'" title="Rimuovi">✕</button>',
          '<span class="ac-call-chevron">›</span>',
        '</div>',
        '<div class="ac-call-body" id="ac-call-body-'+c.id+'">',
          /* LEFT */
          '<div class="ac-call-left">',
            '<div class="ac-cf-row">',
              '<span class="ac-cf-label">URL <span class="ac-required">*</span></span>',
              '<input class="ac-call-url" value="'+esc(c.url)+'" placeholder="https://api.example.com/v1/endpoint"/>',
            '</div>',
            '<div class="ac-cf-row">',
              '<span class="ac-cf-label">Metodo</span>',
              '<select class="ac-call-method">',
                '<option'+(c.method==='GET'?' selected':'')+'>GET</option>',
                '<option'+(c.method==='POST'?' selected':'')+'>POST</option>',
                '<option'+(c.method==='PUT'?' selected':'')+'>PUT</option>',
                '<option'+(c.method==='PATCH'?' selected':'')+'>PATCH</option>',
                '<option'+(c.method==='DELETE'?' selected':'')+'>DELETE</option>',
              '</select>',
            '</div>',
            '<div class="ac-cf-row">',
              '<span class="ac-cf-label">Headers extra (JSON)</span>',
              '<textarea class="ac-call-headers" rows="2">'+esc(c.headers||'{}')+' </textarea>',
            '</div>',
            '<div class="ac-cf-row">',
              '<span class="ac-cf-label">Body (POST/PUT)</span>',
              '<textarea class="ac-call-body-input" rows="3" placeholder=\'{"key": "{{var_da_call_prec}}"}\'>'+(c.body?esc(c.body):'')+'</textarea>',
            '</div>',
            '<div class="ac-cf-row">',
              '<span class="ac-cf-label">Auth override</span>',
              '<select class="ac-call-auth-type">'+authOverrideOptions+'</select>',
            '</div>',
            '<div class="ac-cf-row ac-call-cred-row" style="display:none">',
              '<span class="ac-cf-label">Credenziale</span>',
              '<div class="ac-cf-inline">',
                '<select class="ac-call-credential" id="ac-call-cred-'+c.id+'"><option>Caricamento...</option></select>',
              '</div>',
            '</div>',
            '<div class="ac-cf-row ac-call-apikey-row" style="display:none">',
              '<span class="ac-cf-label">Nome header/param</span>',
              '<input class="ac-call-apikey-param" value="'+esc(c.apikey_param||'')+'" placeholder="X-API-Key"/>',
            '</div>',
            '<div class="ac-cf-row">',
              '<span class="ac-cf-label">Error policy</span>',
              '<select class="ac-call-error-policy">'+errorPolicyOptions+'</select>',
            '</div>',
            '<div class="ac-cf-row">',
              '<span class="ac-cf-label">Join su (chiave per merge)</span>',
              '<input class="ac-call-join-key" value="'+esc(c.join_key||'')+'" placeholder="id — lascia vuoto per cascata semplice"/>',
            '</div>',
          '</div>',
          /* RIGHT: risposta */
          '<div class="ac-call-right">',
            '<div class="ac-resp-header">',
              '<span class="ac-resp-label">Risposta live</span>',
              '<span id="ac-resp-code-'+c.id+'" class="ac-badge ac-badge--none">—</span>',
              '<span id="ac-resp-lat-'+c.id+'" style="font-size:11px;color:var(--text-muted-color,#8b959e)"></span>',
            '</div>',
            '<div class="ac-resp-tabs">',
              '<button class="ac-resp-tab active" data-call-id="'+c.id+'" data-tab="raw">Raw</button>',
              '<button class="ac-resp-tab" data-call-id="'+c.id+'" data-tab="tree">Tree</button>',
              '<button class="ac-resp-tab" data-call-id="'+c.id+'" data-tab="vars">Variabili</button>',
            '</div>',
            '<div id="ac-rtab-raw-'+c.id+'"  class="ac-resp-panel active"><div class="ac-resp-placeholder" id="ac-resp-placeholder-'+c.id+'">Premi ▶ Run per testare la chiamata</div><pre class="ac-resp-code" id="ac-resp-raw-'+c.id+'" style="display:none"></pre></div>',
            '<div id="ac-rtab-tree-'+c.id+'" class="ac-resp-panel"><div class="ac-resp-tree" id="ac-resp-tree-'+c.id+'"></div></div>',
            '<div id="ac-rtab-vars-'+c.id+'" class="ac-resp-panel"><div class="ac-resp-vars" id="ac-resp-vars-'+c.id+'"></div></div>',
          '</div>',
        '</div>',
      '</div>'
    ].join('');
  }

  function buildConnectorHtml(callId){
    return '<div class="ac-chain-connector" id="ac-connector-before-'+callId+'">'+
      '<div class="ac-chain-connector-line"></div>'+
      '<div class="ac-chain-connector-body">'+
        '<span class="ac-chain-connector-label">cascata — variabili disponibili</span>'+
        '<div class="ac-var-chips" id="ac-connector-vars-'+callId+'">'+
          '<span style="font-size:11px;color:var(--text-muted-color,#8b959e);font-style:italic">Esegui la call precedente per vedere le variabili</span>'+
        '</div>'+
      '</div>'+
      '<div class="ac-chain-connector-line"></div>'+
    '</div>';
  }

  /* ── Drag to reorder ─────────────────────────────────────────── */
  var dragSrcId=null;
  $(document).on('dragstart','.ac-call-card',function(e){
    dragSrcId=$(this).data('call-id');
    $(this).addClass('ac-dragging');
    e.originalEvent.dataTransfer.effectAllowed='move';
  });
  $(document).on('dragend','.ac-call-card',function(){
    $(this).removeClass('ac-dragging');
    $('.ac-call-card').removeClass('ac-drag-over');
  });
  $(document).on('dragover','.ac-call-card',function(e){
    e.preventDefault();
    if($(this).data('call-id')!==dragSrcId) $(this).addClass('ac-drag-over');
  });
  $(document).on('dragleave','.ac-call-card',function(){
    $(this).removeClass('ac-drag-over');
  });
  $(document).on('drop','.ac-call-card',function(e){
    e.preventDefault();
    var targetId=$(this).data('call-id');
    $(this).removeClass('ac-drag-over');
    if(dragSrcId===targetId) return;
    /* swap nella catena DOM e in state.calls */
    collectChainState();
    var srcIdx=state.calls.findIndex(function(c){return c.id===dragSrcId;});
    var tgtIdx=state.calls.findIndex(function(c){return c.id===targetId;});
    if(srcIdx<0||tgtIdx<0) return;
    var tmp=state.calls.splice(srcIdx,1)[0];
    state.calls.splice(tgtIdx,0,tmp);
    /* Re-render chain */
    $('#ac-chain-area').empty();
    var savedCalls=state.calls.slice();
    state.calls=[];
    savedCalls.forEach(function(c){ addCallToChain(c); });
    updateAllConnectors();
  });

  /* make cards draggable */
  $(document).on('mouseenter','.ac-call-card',function(){
    $(this).attr('draggable','true');
  });

  /* ── Events on call cards ────────────────────────────────────── */
  /* Toggle card open/close */
  $(document).on('click','.ac-call-header',function(e){
    if($(e.target).is('button,input,select,textarea')) return;
    var $card=$(this).closest('.ac-call-card');
    var $body=$card.find('.ac-call-body');
    var $chev=$card.find('.ac-call-chevron');
    $body.toggleClass('ac-collapsed');
    $chev.css('transform',$body.hasClass('ac-collapsed')?'rotate(-90deg)':'rotate(0deg)');
  });

  /* Method change → update badge */
  $(document).on('change','.ac-call-method',function(){
    var m=$(this).val();
    var $badge=$(this).closest('.ac-call-card').find('.ac-call-method-badge');
    $badge.attr('class','ac-method-badge ac-method-'+m+' ac-call-method-badge').text(m);
  });

  /* Auth override change → show/hide cred + apikey rows */
  $(document).on('change','.ac-call-auth-type',function(){
    var v=$(this).val();
    var $card=$(this).closest('.ac-call-card');
    var id=$card.data('call-id');
    $card.find('.ac-call-cred-row').toggle(v!=='inherited'&&v!=='none');
    $card.find('.ac-call-apikey-row').toggle(v==='api_key_header'||v==='api_key_query');
    var $tag=$card.find('.ac-call-auth-display');
    if(v==='inherited'){$tag.attr('class','ac-auth-tag ac-call-auth-display').text('inherited');}
    else{$tag.attr('class','ac-auth-tag ac-auth-tag--override ac-call-auth-display').text(v);}
    if(v!=='inherited'&&v!=='none') loadCredentials('#ac-call-cred-'+id);
  });

  /* Remove call */
  $(document).on('click','.ac-remove-call-btn',function(e){
    e.stopPropagation();
    var id=$(this).data('call-id');
    if($('.ac-call-card').length<=1){alert('Deve esserci almeno una chiamata.');return;}
    state.calls=state.calls.filter(function(c){return c.id!==id;});
    var $card=$('.ac-call-card[data-call-id="'+id+'"]');
    var $conn=$('#ac-connector-before-'+id);
    $conn.remove(); $card.remove();
    updateAllConnectors();
  });

  /* Add call */
  $('#btn-add-call').on('click',function(){ addCallToChain(); updateAllConnectors(); });

  /* ── Run single call ─────────────────────────────────────────── */
  $(document).on('click','.ac-run-call-btn',function(e){
    e.stopPropagation();
    var id=$(this).data('call-id');
    runCall(id);
  });

  function runCall(callId){
    collectChainState();
    var callObj=state.calls.filter(function(c){return c.id===callId;})[0];
    if(!callObj||!callObj.url){
      alert('Inserisci prima un URL per questa chiamata.'); return;
    }
    /* Determine auth */
    var authType=callObj.auth_type==='inherited'?state.auth_type:callObj.auth_type;
    var credRealm=callObj.auth_type==='inherited'?state.credential_realm:callObj.credential_realm;

    /* Set running state */
    $('#ac-dot-'+callId).attr('class','ac-status-dot ac-status-dot--running');
    $('#ac-lat-'+callId).text('…').attr('class','ac-call-latency');
    var $card=$('.ac-call-card[data-call-id="'+callId+'"]');
    $card.attr('class','ac-call-card ac-call--running');
    $('#ac-resp-code-'+callId).attr('class','ac-badge ac-badge--none').text('…');
    $('#ac-resp-placeholder-'+callId).show();
    $('#ac-resp-raw-'+callId).hide();

    var payload={
      auth_type:authType,
      credential_realm:credRealm,
      token_url:state.token_url,
      apikey_param:callObj.apikey_param||state.apikey_param,
      calls:JSON.stringify([callObj])
    };

    service.post('/servicesNS/nobody/api_connect/api_connect_test',payload,function(err,resp){
      if(err){
        setCallError(callId,'Errore: '+err.message);
        return;
      }
      var r=resp.data||{};
      var code=r.status_code||0;
      var body=r.body||'';
      var latency=r.latency_ms?r.latency_ms+' ms':'—';
      callResponses[callId]={code:code,body:body,latency:latency,content_type:r.content_type||''};

      /* UI update */
      var codeCls=code>=200&&code<300?'ac-badge--2xx':code>=400&&code<500?'ac-badge--4xx':'ac-badge--5xx';
      $('#ac-resp-code-'+callId).attr('class','ac-badge '+codeCls).text('HTTP '+code);
      $('#ac-lat-'+callId).text(latency);
      $('#ac-resp-placeholder-'+callId).hide();

      /* Raw */
      $('#ac-resp-raw-'+callId).text(body).show();

      /* Tree */
      var treeHtml='';
      try{
        var parsed=JSON.parse(body);
        treeHtml=renderJsonTreeSelectable(parsed,'$',callId);
        callResponses[callId].parsed=parsed;
      } catch(ex){
        treeHtml='<span style="color:var(--text-muted-color,#8b959e);font-style:italic">Risposta non JSON</span>';
      }
      $('#ac-resp-tree-'+callId).html(treeHtml);

      /* Vars */
      var vars=extractVarsFromResponse(callResponses[callId].parsed||null,body);
      callVars[callId]=vars;
      renderVarsPanel(callId,vars);

      /* Update connectors delle call successive */
      updateConnectorAfter(callId,vars);

      /* Card state */
      if(code>=200&&code<300){
        $('#ac-dot-'+callId).attr('class','ac-status-dot ac-status-dot--ok');
        $card.attr('class','ac-call-card ac-call--ok');
        $('#ac-lat-'+callId).attr('class','ac-call-latency');
      } else {
        setCallError(callId,'HTTP '+code);
      }
    });
  }

  function setCallError(callId,msg){
    $('#ac-dot-'+callId).attr('class','ac-status-dot ac-status-dot--error');
    $('#ac-lat-'+callId).text(msg).attr('class','ac-call-latency ac-call-latency--error');
    $('.ac-call-card[data-call-id="'+callId+'"]').attr('class','ac-call-card ac-call--error');
  }

  /* ── JSON Tree (selectable for parsing step) ─────────────────── */
  function renderJsonTreeSelectable(val,path,callId){
    return renderNode(val,path,true,callId);
  }

  function renderNode(val,path,selectable,callId){
    if(val===null) return '<span class="jt-null">null</span>';
    if(typeof val==='boolean') return '<span class="jt-bool">'+val+'</span>';
    if(typeof val==='number')  return '<span class="jt-num">'+val+'</span>';
    if(typeof val==='string'){
      var s=esc(val.length>80?val.substring(0,80)+'…':val);
      if(!selectable) return '<span class="jt-str">"'+s+'"</span>';
      return '<span class="jt-leaf" data-path="'+esc(path)+'" data-call-id="'+callId+'"><span class="jt-str">"'+s+'"</span></span>';
    }
    if(Array.isArray(val)){
      if(!val.length) return '<span>[]</span>';
      var items=val.slice(0,8).map(function(v,i){
        return '<div style="padding-left:14px">'+renderNode(v,path+'['+i+']',selectable,callId)+'</div>';
      }).join('');
      if(val.length>8) items+='<div style="padding-left:14px;color:var(--text-muted-color,#8b959e);font-style:italic">…('+val.length+' elementi)</div>';
      return '<span>[</span>'+items+'<span>]</span>';
    }
    if(typeof val==='object'){
      var keys=Object.keys(val);
      if(!keys.length) return '<span>{}</span>';
      var pairs=keys.slice(0,40).map(function(k){
        var cp=path+'.'+k;
        var keySpan='<span class="jt-key">"'+esc(k)+'"</span>: ';
        var child=val[k];
        if(selectable&&(typeof child==='string'||typeof child==='number'||typeof child==='boolean')){
          return '<div style="padding-left:14px"><span class="jt-leaf" data-path="'+esc(cp)+'" data-call-id="'+callId+'">'+keySpan+renderNode(child,cp,false,callId)+'</span></div>';
        }
        return '<div style="padding-left:14px">'+keySpan+renderNode(child,cp,selectable,callId)+'</div>';
      }).join('');
      if(keys.length>40) pairs+='<div style="padding-left:14px;color:var(--text-muted-color,#8b959e);font-style:italic">…('+keys.length+' campi)</div>';
      return '<span>{</span>'+pairs+'<span>}</span>';
    }
    return esc(String(val));
  }

  /* Click leaf in tree → insert {{var}} into focused URL/body input */
  $(document).on('click','.ac-resp-tree .jt-leaf',function(){
    var path=$(this).data('path');
    $(this).toggleClass('selected');
    /* Offer to add to parsing fields */
    addFieldToParsingIfNotExists(path,'');
  });

  /* ── Variables extraction ────────────────────────────────────── */
  function extractVarsFromResponse(parsed,rawBody){
    var vars=[];
    if(!parsed) return vars;
    function walk(obj,prefix){
      if(typeof obj==='object'&&obj!==null&&!Array.isArray(obj)){
        Object.keys(obj).forEach(function(k){
          var p=prefix?prefix+'.'+k:k;
          if(typeof obj[k]==='string'||typeof obj[k]==='number'||typeof obj[k]==='boolean'){
            vars.push({path:p,sample:String(obj[k]).substring(0,30)});
          } else {
            walk(obj[k],p);
          }
        });
      } else if(Array.isArray(obj)&&obj.length>0){
        walk(obj[0],prefix+'[*]');
      }
    }
    var root=parsed;
    /* Unwrap common array roots */
    if(typeof parsed==='object'&&!Array.isArray(parsed)){
      var keys=Object.keys(parsed);
      keys.forEach(function(k){if(Array.isArray(parsed[k])&&parsed[k].length>0){walk(parsed[k][0],k+'[*]');}});
    }
    walk(parsed,'');
    return vars.slice(0,30);
  }

  function renderVarsPanel(callId,vars){
    var $panel=$('#ac-resp-vars-'+callId).empty();
    if(!vars.length){
      $panel.html('<span style="font-size:12px;color:var(--text-muted-color,#8b959e);font-style:italic">Nessuna variabile estratta</span>');
      return;
    }
    var chips=vars.map(function(v){
      return '<span class="ac-var-chip" data-var="{{'+v.path+'}}" title="Campione: '+esc(v.sample)+'" style="margin:2px 3px;cursor:pointer">{{'+esc(v.path)+'}}</span>';
    }).join('');
    $panel.html('<div style="margin-bottom:6px;font-size:11px;color:var(--text-muted-color,#8b959e)">Clicca un chip per copiarlo nell\'input focalizzato</div><div>'+chips+'</div>');
  }

  /* Click var chip → paste into last focused input */
  var lastFocused=null;
  $(document).on('focus','input,textarea',function(){ lastFocused=$(this); });
  $(document).on('click','.ac-var-chip',function(){
    var v=$(this).data('var');
    if(lastFocused&&lastFocused.length){
      var el=lastFocused[0];
      var start=el.selectionStart,end=el.selectionEnd;
      var val=lastFocused.val();
      lastFocused.val(val.substring(0,start)+v+val.substring(end));
      el.setSelectionRange(start+v.length,start+v.length);
      lastFocused.focus();
    }
  });

  /* ── Connectors ──────────────────────────────────────────────── */
  function updateConnectorAfter(callId,vars){
    /* Find the next call in DOM order */
    var $cards=$('.ac-call-card');
    var found=false;
    $cards.each(function(){
      if(found){
        var nextId=$(this).data('call-id');
        var $chips=$('#ac-connector-vars-'+nextId);
        if($chips.length){
          if(!vars||!vars.length){
            $chips.html('<span style="font-size:11px;color:var(--text-muted-color,#8b959e);font-style:italic">Nessuna variabile disponibile</span>');
          } else {
            var chips=vars.map(function(v){
              return '<span class="ac-var-chip" data-var="{{'+v.path+'}}" title="Campione: '+esc(v.sample)+'">{{'+esc(v.path)+'}}</span>';
            }).join('');
            $chips.html(chips);
          }
        }
        found=false;
      }
      if($(this).data('call-id')===callId) found=true;
    });
  }

  function updateAllConnectors(){
    /* Ensure connector IDs match current card order */
    var $cards=$('.ac-call-card');
    $cards.each(function(i){
      var id=$(this).data('call-id');
      var $conn=$('#ac-connector-before-'+id);
      if(i===0&&$conn.length) $conn.remove();
      if(i>0&&!$conn.length){
        $(this).before(buildConnectorHtml(id));
      }
    });
  }

  function showRespPlaceholder(callId){
    $('#ac-resp-placeholder-'+callId).show();
    $('#ac-resp-raw-'+callId).hide();
  }

  /* ── Response tab switching ──────────────────────────────────── */
  $(document).on('click','.ac-resp-tab',function(){
    var callId=$(this).data('call-id');
    var tab=$(this).data('tab');
    $(this).siblings().removeClass('active');
    $(this).addClass('active');
    var $card=$('.ac-call-card[data-call-id="'+callId+'"]');
    $card.find('.ac-resp-panel').removeClass('active');
    $card.find('#ac-rtab-'+tab+'-'+callId).addClass('active');
  });

  /* ── Step 3: no-op (test is in step 2 per-card) ─────────────── */
  /* Step 3 shows a merged view of all call responses */
  function renderParsingTab(){
    /* Populate the parsing tree with response from first call that returned data */
    var ids=Object.keys(callResponses);
    if(!ids.length){
      $('#parsing-tree').html('<p class="ac-hint">Esegui almeno una chiamata nello Step 2 per visualizzare il tree.</p>');
      return;
    }
    var resp=callResponses[ids[0]];
    try{
      var parsed=resp.parsed||JSON.parse(resp.body||'{}');
      $('#parsing-tree').html(renderNode(parsed,'$',true,'p'));
    } catch(e){
      $('#parsing-tree').html('<p class="ac-hint">Risposta non JSON — usa il parsing manuale.</p>');
    }
  }

  /* ── Parsing step leaf click ─────────────────────────────────── */
  $(document).on('click','#parsing-tree .jt-leaf',function(){
    var path=$(this).data('path');
    $(this).toggleClass('selected');
    if($(this).hasClass('selected')) addFieldToParsingIfNotExists(path,'');
    else $('.ac-field-row').filter(function(){return $(this).find('.field-path').val()===path;}).remove();
  });

  $('#btn-add-field').on('click',function(){ addFieldToParsingIfNotExists('',''); });

  function addFieldToParsingIfNotExists(path,alias){
    var exists=false;
    $('.field-path').each(function(){if($(this).val()===path&&path)exists=true;});
    if(exists) return;
    var $row=$('<div class="ac-field-row">'+
      '<input type="text" class="input-medium field-path ac-field-path" value="'+esc(path)+'" placeholder="$.campo o regex"/>'+
      '<span style="color:var(--text-muted-color,#8b959e);flex-shrink:0">→</span>'+
      '<input type="text" class="input-medium field-alias" value="'+esc(alias)+'" placeholder="alias"/>'+
      '<button class="btn btn-default btn-sm ac-btn-remove-field" type="button"><i class="icon-trash"></i></button>'+
    '</div>');
    $('#ac-fields-list').append($row);
  }

  function collectExtractedFields(){
    var f=[];
    $('.ac-field-row').each(function(){
      var p=$(this).find('.field-path').val().trim();
      var a=$(this).find('.field-alias').val().trim();
      if(p) f.push({path:p,alias:a||p.split('.').pop()});
    });
    return f;
  }

  $(document).on('click','.ac-btn-remove-field',function(){$(this).closest('.ac-field-row').remove();});

  /* ── Tracciato ───────────────────────────────────────────────── */
  function renderTracciato(){
    var fields=state.extracted_fields;
    var opts='<option value="">— non mappato —</option>'+
      fields.map(function(f){
        var l=f.alias||f.path;
        return '<option value="'+esc(f.path)+'">'+esc(l)+'</option>';
      }).join('')+
      '<option value="__static__">valore statico…</option>';
    var rows=TRACCIATO_FIELDS.map(function(tf){
      return '<div class="ac-tracciato-row">'+
        '<label class="ac-tracciato-label">'+tf.label+(tf.required?'<span class="ac-required">*</span>':'')+'</label>'+
        '<select id="map-'+tf.key+'" class="input-xlarge">'+opts+'</select>'+
        '<div class="ac-tracciato-hint">'+tf.hint+'</div>'+
      '</div>';
    }).join('');
    $('#ac-tracciato-grid').html(rows);
    TRACCIATO_FIELDS.forEach(function(tf){
      if(state.field_mapping[tf.key]) $('#map-'+tf.key).val(state.field_mapping[tf.key]);
    });
  }

  function collectTracciato(){
    TRACCIATO_FIELDS.forEach(function(tf){
      state.field_mapping[tf.key]=$('#map-'+tf.key).val().trim();
    });
  }

  /* ── Step 6: indexes ─────────────────────────────────────────── */
  function loadIndexes(){
    service.get('/servicesNS/-/-/data/indexes',{count:100,output_mode:'json'},function(err,resp){
      if(err) return;
      var $s=$('#f-index').empty().append('<option value="">— Seleziona index —</option>');
      ((resp.data&&resp.data.entry)||[]).filter(function(e){return !e.name.startsWith('_');})
        .forEach(function(e){ $s.append('<option value="'+esc(e.name)+'">'+esc(e.name)+'</option>'); });
      if(state.index) $s.val(state.index);
    });
  }

  $('#f-checkpoint').on('change',function(){$('#checkpoint-detail').toggle($(this).is(':checked'));});
  $('#f-pagination').on('change',function(){$('#pagination-details').toggle($(this).val()!=='none');});
  $('#f-schedule').on('input',function(){$('#cron-preview').text(cronHuman($(this).val().trim()));});

  /* ── Summary ─────────────────────────────────────────────────── */
  function renderSummary(){
    collectCurrentStep();
    var kv=[
      ['Nome input',state.name||'—'],['Auth globale',state.auth_type||'—'],
      ['Call configurate',state.calls.length],
      ['Call con auth override',state.calls.filter(function(c){return c.auth_type!=='inherited';}).length],
      ['Paginazione',state.pagination_type],['Schedule',state.schedule||'—'],
      ['Formato risposta',state.response_format],
      ['Campi estratti',state.extracted_fields.length],
      ['Tracciato mappato',Object.values(state.field_mapping).filter(Boolean).length+' / '+TRACCIATO_FIELDS.length],
      ['Index',state.index||'—'],['Sourcetype',state.sourcetype||'—'],
      ['Logger source',state.logger_source||'—']
    ];
    $('#ac-summary-content').html('<div class="ac-summary-kv">'+
      kv.map(function(p){return '<span class="ac-summary-key">'+esc(p[0])+'</span><span class="ac-summary-val">'+esc(String(p[1]))+'</span>';}).join('')+
    '</div>');
  }

  /* ── Generate ────────────────────────────────────────────────── */
  $('#btn-generate').on('click',function(){
    collectCurrentStep();
    var errors=[];
    if(!state.name) errors.push('Nome input mancante (Step 1)');
    if(!state.calls.length||!state.calls[0].url) errors.push('URL endpoint mancante (Step 2)');
    if(!state.schedule) errors.push('Schedule mancante (Step 2)');
    if(!state.index) errors.push('Index mancante (Step 6)');
    if(!state.sourcetype) errors.push('Sourcetype mancante (Step 6)');
    if(!state.source) errors.push('Source mancante (Step 6)');
    if(!state.logger_source) errors.push('Logger source mancante (Step 7)');
    TRACCIATO_FIELDS.filter(function(tf){return tf.required;}).forEach(function(tf){
      if(!state.field_mapping[tf.key]) errors.push('Campo obbligatorio non mappato: '+tf.label);
    });
    if(errors.length){alert('Errori:\n• '+errors.join('\n• '));return;}
    $('#ac-gen-modal').show();
    $('#gen-modal-title').text('Generazione in corso…');
    $('#gen-modal-body').html('<div class="ac-spinner"><i class="icon-rotate-right"></i> Generazione script…</div>');
    $('#gen-modal-footer').hide();
    service.post('/servicesNS/nobody/api_connect/api_connect_generate',{config:JSON.stringify(state)},function(err,resp){
      if(err){$('#gen-modal-title').text('Errore');$('#gen-modal-body').html('<p style="color:#c62828">'+esc(err.message)+'</p>');$('#gen-modal-footer').show();return;}
      var r=resp.data||{};
      $('#gen-modal-title').text('Input generato con successo!');
      $('#gen-modal-body').html(
        '<div class="ac-summary-box"><div class="ac-summary-kv">'+
        '<span class="ac-summary-key">Script</span><span class="ac-summary-val">'+esc(r.script_path||'—')+'</span>'+
        '<span class="ac-summary-key">Stanza</span><span class="ac-summary-val">'+esc(r.stanza||'—')+'</span>'+
        '</div></div>'+
        (r.script_preview?'<pre class="ac-code-block" style="margin-top:12px">'+esc(r.script_preview)+'</pre>':'')
      );
      $('#gen-modal-footer').show();
    });
  });

  $('#btn-save-draft').on('click',function(){
    collectCurrentStep();
    service.post('/servicesNS/nobody/api_connect/storage/collections/data/api_connect_inputs',
      {name:state.name||'__draft__',config:JSON.stringify(state),last_status:'DRAFT'},function(){});
    alert('Bozza salvata.');
  });

  $('#btn-prev').on('click',function(){goToStep(currentStep-1);});
  $('#btn-next').on('click',function(){goToStep(currentStep+1);});

  /* ── Edit mode ───────────────────────────────────────────────── */
  var urlParams=new URLSearchParams(window.location.search);
  editKey=urlParams.get('edit');
  if(editKey){
    service.get('/servicesNS/nobody/api_connect/storage/collections/data/api_connect_inputs/'+editKey,{},function(err,resp){
      if(err||!resp.data) return;
      try{$.extend(state,JSON.parse(resp.data.config||'{}'));populateStep1();}catch(e){}
    });
  }

  function populateStep1(){
    $('#f-name').val(state.name);
    $('#f-auth-type').val(state.auth_type).trigger('change');
    $('#f-schedule').val(state.schedule);
    $('#cron-preview').text(cronHuman(state.schedule));
  }

  /* ── Init ────────────────────────────────────────────────────── */
  renderStep();
});
