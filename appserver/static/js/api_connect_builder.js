/**
 * API Connect — Input Builder Wizard v3
 * 9 step: Auth → Chain Builder → Parsing → Trasformazioni →
 *         Tracciato → Output Format → Resilienza → Logger → Preview & Genera
 *
 * Novità v3:
 *  - Step 4: Transform pipeline per-campo con dropdown funzioni
 *  - Step 6: Output format selector (pipe/kv/json/csv/custom)
 *  - Step 7: Circuit breaker UI
 *  - Step 9: Preview evento live con record reale dalla test call
 */
require([
  'jquery',
  'splunkjs/mvc',
  'splunkjs/mvc/simplexml/ready!'
], function($, mvc) {
  'use strict';

  var service = mvc.createService();
  var TOTAL_STEPS = 9;
  var currentStep = 1;
  var editKey = null;

  /* ── Catalogo trasformazioni (specchio di TRANSFORM_REGISTRY Python) ── */
  var TRANSFORMS = [
    {name:'upper',         label:'MAIUSCOLO',            params:[]},
    {name:'lower',         label:'minuscolo',            params:[]},
    {name:'strip',         label:'Rimuovi spazi',        params:[]},
    {name:'replace',       label:'Sostituisci',          params:['old','new']},
    {name:'split',         label:'Split/estrai parte',   params:['sep','index']},
    {name:'truncate',      label:'Tronca',               params:['length']},
    {name:'pad_left',      label:'Pad sinistra',         params:['length','char']},
    {name:'concat',        label:'Concat prefisso/suff', params:['prefix','suffix']},
    {name:'regex_extract', label:'Estrai con regex',     params:['pattern','group']},
    {name:'regex_replace', label:'Sostituisci regex',    params:['pattern','replacement']},
    {name:'iso_to_epoch',  label:'ISO 8601 → epoch',     params:[]},
    {name:'epoch_to_iso',  label:'Epoch → ISO 8601',     params:[]},
    {name:'strptime_to_epoch', label:'Data custom → epoch', params:['fmt']},
    {name:'epoch_to_splunk',   label:'Epoch → Splunk date', params:[]},
    {name:'to_int',        label:'→ Intero',             params:[]},
    {name:'to_float',      label:'→ Float',              params:[]},
    {name:'round',         label:'Arrotonda',            params:['decimals']},
    {name:'abs_val',       label:'Valore assoluto',      params:[]},
    {name:'math_expr',     label:'Espressione math',     params:['expr']},
    {name:'default',       label:'Valore default',       params:['value']},
    {name:'map_values',    label:'Mappa valori (dict)',   params:['mapping','fallback']},
    {name:'if_contains',   label:'Se contiene → valore', params:['substring','true_val','false_val']},
    {name:'sha256',        label:'SHA-256 (hash PII)',   params:[]},
    {name:'mask',          label:'Maschera (PII)',        params:['first','last','char']},
    {name:'lookup_csv',    label:'Lookup CSV',            params:['path','key_col','val_col']},
    {name:'lookup_json',   label:'Lookup JSON inline',   params:['mapping','fallback']},
  ];

  var TRACCIATO_FIELDS = [
    {key:'time',          label:'time',          required:true,  hint:'Timestamp (epoch o ISO 8601)'},
    {key:'hostname',      label:'hostname',       required:true,  hint:'Host sorgente evento'},
    {key:'nomeapp',       label:'nomeapp',        required:true,  hint:'Nome applicazione sorgente'},
    {key:'tipoazione',    label:'tipoazione',     required:true,  hint:'Tipo azione (login, logout…)'},
    {key:'clientip',      label:'clientip',       required:false, hint:'IP del client'},
    {key:'username',      label:'username',       required:false, hint:'Utente coinvolto'},
    {key:'tipooperazione',label:'tipooperazione', required:false, hint:'Tipo operazione specifica'},
    {key:'valorePrima',   label:'valore prima',   required:false, hint:'Valore prima della modifica'},
    {key:'valoreDP',      label:'valore dopo',    required:false, hint:'Valore dopo la modifica'},
    {key:'target',        label:'target',         required:false, hint:'Risorsa target'},
    {key:'note',          label:'note',           required:false, hint:'Note aggiuntive'},
  ];

  var CRON_MAP = {
    '*/1 * * * *':'ogni 1 min','*/5 * * * *':'ogni 5 min',
    '*/10 * * * *':'ogni 10 min','*/15 * * * *':'ogni 15 min',
    '*/30 * * * *':'ogni 30 min','0 * * * *':'ogni ora',
    '0 0 * * *':'ogni giorno'
  };

  /* ── State ─────────────────────────────────────────────────── */
  var state = {
    name:'', auth_type:'none', credential_realm:'', token_url:'',
    oauth_scope:'', apikey_param:'',
    calls:[], pagination_type:'none', page_param:'page',
    cursor_path:'', max_pages:100, schedule:'*/5 * * * *',
    response_format:'json', text_regex:'', array_root:'',
    extracted_fields:[],
    field_transforms:{},   // { campo: [{fn,params}] }
    field_mapping:{},
    output_format:'kv', custom_sep:'|', null_value:'', include_extra:false,
    index:'', sourcetype:'', source:'', host:'',
    checkpoint:false, checkpoint_field:'',
    cb_enabled:true, cb_threshold:5, cb_cooldown:120,
    logger_source:''
  };

  var callResponses = {};
  var callVars      = {};
  var callIdSeq     = 0;
  var lastFocused   = null;

  /* ── Utility ── */
  function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function cronHuman(e){ return CRON_MAP[e]||e; }

  function newCallObj(){
    callIdSeq++;
    return {id:callIdSeq,name:'Chiamata '+callIdSeq,url:'',method:'GET',
            headers:'{}',body:'',auth_type:'inherited',credential_realm:'',
            apikey_param:'',error_policy:'default',join_key:''};
  }

  /* ═══════════════════════════════════════════════════════════
     STEP NAVIGATION
     ═══════════════════════════════════════════════════════════ */
  function goToStep(n){
    if(n<1||n>TOTAL_STEPS) return;
    collectCurrentStep();
    currentStep=n; renderStep();
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
      $('#btn-next').hide(); $('#btn-generate').show();
      renderSummary(); renderPreview();
    } else {
      $('#btn-next').show(); $('#btn-generate').hide();
    }
    // Step-specific init
    if(currentStep===2) renderChain();
    if(currentStep===3) renderParsingTree();
    if(currentStep===4) renderTransformsStep();
    if(currentStep===5) renderTracciato();
    if(currentStep===6){ loadIndexes(); renderOutputFormatHint(); }
  }

  /* ═══════════════════════════════════════════════════════════
     COLLECT STATE
     ═══════════════════════════════════════════════════════════ */
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
      case 2:
        collectChainState();
        state.schedule=$('#f-schedule').val().trim();
        state.pagination_type=$('#f-pagination').val();
        state.page_param=$('#f-page-param').val().trim();
        state.cursor_path=$('#f-cursor-path').val().trim();
        state.max_pages=parseInt($('#f-max-pages').val(),10)||100;
        break;
      case 3:
        state.response_format=$('#f-response-format').val();
        state.text_regex=$('#f-text-regex').val().trim();
        state.array_root=$('#f-array-root').val().trim();
        state.extracted_fields=collectExtractedFields();
        break;
      case 4: collectTransforms(); break;
      case 5: collectTracciato(); break;
      case 6:
        state.output_format=$('#f-output-format').val();
        state.custom_sep=$('#f-custom-sep').val()||'|';
        state.null_value=$('#f-null-value').val();
        state.include_extra=$('#f-include-extra').is(':checked');
        state.index=$('#f-index').val();
        state.sourcetype=$('#f-sourcetype').val().trim();
        state.source=$('#f-source').val().trim();
        state.host=$('#f-host').val().trim();
        state.checkpoint=$('#f-checkpoint').is(':checked');
        state.checkpoint_field=$('#f-checkpoint-field').val().trim();
        break;
      case 7:
        state.cb_enabled=$('#f-cb-enabled').is(':checked');
        state.cb_threshold=parseInt($('#f-cb-threshold').val(),10)||5;
        state.cb_cooldown=parseInt($('#f-cb-cooldown').val(),10)||120;
        break;
      case 8:
        state.logger_source=$('#f-logger-source').val().trim();
        break;
    }
  }

  /* ═══════════════════════════════════════════════════════════
     STEP 1: AUTH
     ═══════════════════════════════════════════════════════════ */
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
      var $s=$(sel).empty().append('<option value="">— Seleziona —</option>');
      ((resp.data&&resp.data.entry)||[])
        .filter(function(e){return e.content&&e.content.realm&&e.content.realm.indexOf('api_connect:')===0;})
        .forEach(function(e){
          var r=e.content.realm;
          $s.append('<option value="'+esc(r)+'">'+esc(r.replace('api_connect:',''))+' ('+esc(e.content.username)+')</option>');
        });
    });
  }

  /* ═══════════════════════════════════════════════════════════
     STEP 2: CHAIN BUILDER
     ═══════════════════════════════════════════════════════════ */
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
    if($area.find('.ac-call-card').length>0) $area.append(buildConnectorHtml(call.id));
    $area.append(buildCallCardHtml(call));
    loadCredentials('#ac-call-cred-'+call.id);
    showRespPlaceholder(call.id);
  }

  function buildCallCardHtml(c){
    var authOpts=['inherited','none','bearer','basic','api_key_header','api_key_query','oauth2_cc'].map(function(v){
      var labels={inherited:'Ereditata (globale)',none:'Nessuna',bearer:'Bearer Token',
                  basic:'Basic Auth',api_key_header:'API Key Header',api_key_query:'API Key Query',oauth2_cc:'OAuth2 CC'};
      return '<option value="'+v+'"'+(c.auth_type===v?' selected':'')+'>'+labels[v]+'</option>';
    }).join('');
    var policyOpts=['default','retry_429','skip_404','skip_all_4xx','stop_5xx','skip_all'].map(function(v){
      var labels={default:'Default (stop)',retry_429:'429→retry 3× + Retry-After',skip_404:'404→skip',
                  skip_all_4xx:'4xx→skip',stop_5xx:'5xx→stop',skip_all:'Tutto→skip'};
      return '<option value="'+v+'"'+(c.error_policy===v?' selected':'')+'>'+labels[v]+'</option>';
    }).join('');
    return [
      '<div class="ac-call-card" data-call-id="'+c.id+'" draggable="true">',
        '<div class="ac-call-header">',
          '<span class="ac-drag-handle">⠿</span>',
          '<div class="ac-call-num">'+c.id+'</div>',
          '<input class="ac-call-name-input" value="'+esc(c.name)+'" style="border:none;background:transparent;font-weight:600;font-size:13px;flex:1;outline:none"/>',
          '<span class="ac-method-badge ac-method-GET ac-call-method-badge">'+esc(c.method||'GET')+'</span>',
          '<span class="ac-auth-tag ac-call-auth-display">inherited</span>',
          '<div class="ac-status-dot" id="ac-dot-'+c.id+'"></div>',
          '<span class="ac-call-latency" id="ac-lat-'+c.id+'"></span>',
          '<button class="btn btn-default ac-btn-sm ac-run-call-btn" data-call-id="'+c.id+'">▶ Run</button>',
          '<button class="btn btn-default ac-btn-sm ac-remove-call-btn" data-call-id="'+c.id+'">✕</button>',
          '<span class="ac-call-chevron">›</span>',
        '</div>',
        '<div class="ac-call-body" id="ac-call-body-'+c.id+'">',
          '<div class="ac-call-left">',
            '<div class="ac-cf-row"><span class="ac-cf-label">URL *</span><input class="ac-call-url" value="'+esc(c.url)+'" placeholder="https://api.example.com/endpoint"/></div>',
            '<div class="ac-cf-row"><span class="ac-cf-label">Metodo</span>',
              '<select class="ac-call-method"><option'+(c.method==='GET'?' selected':'')+'>GET</option><option'+(c.method==='POST'?' selected':'')+'>POST</option><option'+(c.method==='PUT'?' selected':'')+'>PUT</option><option'+(c.method==='PATCH'?' selected':'')+'>PATCH</option><option'+(c.method==='DELETE'?' selected':'')+'>DELETE</option></select>',
            '</div>',
            '<div class="ac-cf-row"><span class="ac-cf-label">Headers extra (JSON)</span><textarea class="ac-call-headers" rows="2">'+esc(c.headers||'{}')+' </textarea></div>',
            '<div class="ac-cf-row"><span class="ac-cf-label">Body (POST/PUT/PATCH)</span><textarea class="ac-call-body-input" rows="2" placeholder=\'{"key":"{{var}}"}\'>'+(c.body?esc(c.body):'')+'</textarea></div>',
            '<div class="ac-cf-row"><span class="ac-cf-label">Auth override</span><select class="ac-call-auth-type">'+authOpts+'</select></div>',
            '<div class="ac-cf-row ac-call-cred-row" style="display:none"><span class="ac-cf-label">Credenziale</span><select class="ac-call-credential" id="ac-call-cred-'+c.id+'"><option>Caricamento…</option></select></div>',
            '<div class="ac-cf-row ac-call-apikey-row" style="display:none"><span class="ac-cf-label">Nome header/param</span><input class="ac-call-apikey-param" value="'+esc(c.apikey_param||'')+'" placeholder="X-API-Key"/></div>',
            '<div class="ac-cf-row"><span class="ac-cf-label">Error policy</span><select class="ac-call-error-policy">'+policyOpts+'</select></div>',
            '<div class="ac-cf-row"><span class="ac-cf-label">Join su (chiave merge)</span><input class="ac-call-join-key" value="'+esc(c.join_key||'')+'" placeholder="id — vuoto per cascata semplice"/></div>',
          '</div>',
          '<div class="ac-call-right">',
            '<div class="ac-resp-header"><span class="ac-resp-label">Risposta live</span><span id="ac-resp-code-'+c.id+'" class="ac-badge ac-badge--none">—</span><span id="ac-resp-lat-'+c.id+'" style="font-size:11px;color:var(--text-muted-color,#8b959e);margin-left:8px"></span></div>',
            '<div class="ac-resp-tabs">',
              '<button class="ac-resp-tab active" data-call-id="'+c.id+'" data-tab="raw">Raw</button>',
              '<button class="ac-resp-tab" data-call-id="'+c.id+'" data-tab="tree">Tree</button>',
              '<button class="ac-resp-tab" data-call-id="'+c.id+'" data-tab="vars">Variabili</button>',
            '</div>',
            '<div id="ac-rtab-raw-'+c.id+'" class="ac-resp-panel active"><div class="ac-resp-placeholder" id="ac-resp-placeholder-'+c.id+'">Premi ▶ Run</div><pre class="ac-resp-code" id="ac-resp-raw-'+c.id+'" style="display:none"></pre></div>',
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
          '<span style="font-size:11px;color:var(--text-muted-color,#8b959e);font-style:italic">Esegui la call precedente</span>'+
        '</div>'+
      '</div>'+
      '<div class="ac-chain-connector-line"></div>'+
    '</div>';
  }

  /* ── Run call ── */
  $(document).on('click','.ac-run-call-btn',function(e){
    e.stopPropagation();
    var id=$(this).data('call-id');
    runCall(id);
  });

  $('#btn-run-all-calls').on('click',function(){
    var ids=[];
    $('.ac-call-card').each(function(){ ids.push($(this).data('call-id')); });
    runCallSequence(ids,0);
  });

  function runCallSequence(ids,idx){
    if(idx>=ids.length){ $('#run-all-status').text('✅ Tutte le call completate'); return; }
    $('#run-all-status').text('Esecuzione call '+(idx+1)+'/'+ids.length+'…');
    runCall(ids[idx], function(){ runCallSequence(ids,idx+1); });
  }

  function runCall(callId, onDone){
    collectChainState();
    var callObj=state.calls.filter(function(c){return c.id===callId;})[0];
    if(!callObj||!callObj.url){ alert('Inserisci URL prima di testare.'); return; }
    var authType=callObj.auth_type==='inherited'?state.auth_type:callObj.auth_type;
    var credRealm=callObj.auth_type==='inherited'?state.credential_realm:callObj.credential_realm;

    $('#ac-dot-'+callId).attr('class','ac-status-dot ac-status-dot--running');
    $('.ac-call-card[data-call-id="'+callId+'"]').attr('class','ac-call-card ac-call--running');
    $('#ac-resp-code-'+callId).attr('class','ac-badge ac-badge--none').text('…');
    $('#ac-resp-placeholder-'+callId).show();
    $('#ac-resp-raw-'+callId).hide();

    var payload={
      call_config:   JSON.stringify(callObj),
      global_config: JSON.stringify({auth_type:authType,credential_realm:credRealm,
                                     token_url:state.token_url,apikey_param:state.apikey_param,
                                     oauth_scope:state.oauth_scope})
    };

    service.post('/servicesNS/nobody/api_connect/api_connect_test',payload,function(err,resp){
      var result={};
      try{
        var d=resp&&resp.data;
        result=d&&d.payload?JSON.parse(d.payload):{};
      }catch(ex){}
      if(err||!result.status_code){
        setCallError(callId, err?err.message:'Errore server');
        if(onDone) onDone(); return;
      }
      var code=result.status_code, body=result.body||'', latency=result.latency_ms+'ms';
      callResponses[callId]={code:code,body:body,latency:latency,content_type:result.content_type||''};

      var codeCls=code>=200&&code<300?'ac-badge--2xx':code>=500?'ac-badge--5xx':'ac-badge--4xx';
      $('#ac-resp-code-'+callId).attr('class','ac-badge '+codeCls).text('HTTP '+code);
      $('#ac-resp-lat-'+callId).text(latency);
      $('#ac-resp-placeholder-'+callId).hide();
      $('#ac-resp-raw-'+callId).text(body).show();

      var parsed=null;
      try{ parsed=JSON.parse(body); callResponses[callId].parsed=parsed; }catch(ex){}

      $('#ac-resp-tree-'+callId).html(parsed?renderJsonTree(parsed,'$',true,callId):'<span style="font-size:11px;color:var(--text-muted-color,#8b959e)">Non JSON</span>');

      var vars=extractVarsFromParsed(parsed);
      callVars[callId]=vars;
      renderVarsPanel(callId,vars);
      updateConnectorAfter(callId,vars);

      if(code>=200&&code<300){
        $('#ac-dot-'+callId).attr('class','ac-status-dot ac-status-dot--ok');
        $('.ac-call-card[data-call-id="'+callId+'"]').attr('class','ac-call-card ac-call--ok');
      } else {
        setCallError(callId,'HTTP '+code);
      }
      if(onDone) onDone();
    });
  }

  function setCallError(callId,msg){
    $('#ac-dot-'+callId).attr('class','ac-status-dot ac-status-dot--error');
    $('#ac-lat-'+callId).text(msg).attr('class','ac-call-latency ac-call-latency--error');
    $('.ac-call-card[data-call-id="'+callId+'"]').attr('class','ac-call-card ac-call--error');
  }

  /* ── Chain events ── */
  $(document).on('click','.ac-call-header',function(e){
    if($(e.target).is('button,input,select,textarea')) return;
    var $b=$(this).closest('.ac-call-card').find('.ac-call-body');
    var $ch=$(this).closest('.ac-call-card').find('.ac-call-chevron');
    $b.toggleClass('ac-collapsed');
    $ch.css('transform',$b.hasClass('ac-collapsed')?'rotate(-90deg)':'rotate(0deg)');
  });

  $(document).on('change','.ac-call-method',function(){
    var m=$(this).val();
    $(this).closest('.ac-call-card').find('.ac-call-method-badge')
      .attr('class','ac-method-badge ac-method-'+m+' ac-call-method-badge').text(m);
  });

  $(document).on('change','.ac-call-auth-type',function(){
    var v=$(this).val();
    var $card=$(this).closest('.ac-call-card');
    var id=$card.data('call-id');
    $card.find('.ac-call-cred-row').toggle(v!=='inherited'&&v!=='none');
    $card.find('.ac-call-apikey-row').toggle(v==='api_key_header'||v==='api_key_query');
    var $tag=$card.find('.ac-call-auth-display');
    $tag.attr('class',v!=='inherited'?'ac-auth-tag ac-auth-tag--override ac-call-auth-display':'ac-auth-tag ac-call-auth-display').text(v);
    if(v!=='inherited'&&v!=='none') loadCredentials('#ac-call-cred-'+id);
  });

  $(document).on('click','.ac-remove-call-btn',function(e){
    e.stopPropagation();
    var id=$(this).data('call-id');
    if($('.ac-call-card').length<=1){alert('Almeno una call richiesta.');return;}
    state.calls=state.calls.filter(function(c){return c.id!==id;});
    $('#ac-connector-before-'+id).remove();
    $('.ac-call-card[data-call-id="'+id+'"]').remove();
    updateAllConnectors();
  });

  $('#btn-add-call').on('click',function(){ addCallToChain(); updateAllConnectors(); });

  $(document).on('click','.ac-resp-tab',function(){
    var id=$(this).data('call-id'), tab=$(this).data('tab');
    $(this).siblings().removeClass('active'); $(this).addClass('active');
    var $card=$('.ac-call-card[data-call-id="'+id+'"]');
    $card.find('.ac-resp-panel').removeClass('active');
    $card.find('#ac-rtab-'+tab+'-'+id).addClass('active');
  });

  $(document).on('focus','input,textarea',function(){ lastFocused=$(this); });

  $(document).on('click','.ac-var-chip',function(){
    var v=$(this).data('var');
    if(lastFocused&&lastFocused.length){
      var el=lastFocused[0];
      var s=el.selectionStart,e=el.selectionEnd,val=lastFocused.val();
      lastFocused.val(val.substring(0,s)+v+val.substring(e));
      el.setSelectionRange(s+v.length,s+v.length);
      lastFocused.focus();
    }
  });

  function collectChainState(){
    state.calls=[];
    $('.ac-call-card').each(function(){
      var $c=$(this);
      var id=parseInt($c.data('call-id'),10);
      state.calls.push({
        id:id,
        name:$c.find('.ac-call-name-input').val().trim()||'Chiamata '+id,
        url:$c.find('.ac-call-url').val().trim(),
        method:$c.find('.ac-call-method').val(),
        headers:$c.find('.ac-call-headers').val().trim(),
        body:$c.find('.ac-call-body-input').val().trim(),
        auth_type:$c.find('.ac-call-auth-type').val(),
        credential_realm:$c.find('.ac-call-credential').val(),
        apikey_param:$c.find('.ac-call-apikey-param').val(),
        error_policy:$c.find('.ac-call-error-policy').val(),
        join_key:$c.find('.ac-call-join-key').val().trim()
      });
    });
  }

  function showRespPlaceholder(id){
    $('#ac-resp-placeholder-'+id).show();
    $('#ac-resp-raw-'+id).hide();
  }

  function updateAllConnectors(){
    $('.ac-call-card').each(function(i){
      var id=$(this).data('call-id');
      var $conn=$('#ac-connector-before-'+id);
      if(i===0&&$conn.length) $conn.remove();
      if(i>0&&!$conn.length) $(this).before(buildConnectorHtml(id));
    });
  }

  function updateConnectorAfter(callId,vars){
    var found=false;
    $('.ac-call-card').each(function(){
      if(found){
        var nextId=$(this).data('call-id');
        var $chips=$('#ac-connector-vars-'+nextId);
        if($chips.length){
          if(!vars||!vars.length){
            $chips.html('<span style="font-size:11px;color:var(--text-muted-color,#8b959e);font-style:italic">Nessuna variabile</span>');
          } else {
            $chips.html(vars.map(function(v){
              return '<span class="ac-var-chip" data-var="{{'+v.p+'}}" title="'+esc(v.s)+'">{{'+esc(v.p)+'}}</span>';
            }).join(''));
          }
        }
        found=false;
      }
      if($(this).data('call-id')===callId) found=true;
    });
  }

  /* ── JSON Tree ── */
  function renderJsonTree(val,path,selectable,callId){
    return '<div>'+renderNode(val,path,selectable,callId)+'</div>';
  }

  function renderNode(val,path,sel,cid){
    if(val===null) return '<span class="jt-null">null</span>';
    if(typeof val==='boolean') return '<span class="jt-bool">'+val+'</span>';
    if(typeof val==='number')  return '<span class="jt-num">'+val+'</span>';
    if(typeof val==='string'){
      var s=esc(val.length>80?val.substring(0,80)+'…':val);
      if(!sel) return '<span class="jt-str">"'+s+'"</span>';
      return '<span class="jt-leaf" data-path="'+esc(path)+'" data-call-id="'+(cid||'')+'" title="'+esc(path)+'"><span class="jt-str">"'+s+'"</span></span>';
    }
    if(Array.isArray(val)){
      if(!val.length) return '<span>[]</span>';
      var items=val.slice(0,8).map(function(v,i){
        return '<div style="padding-left:14px">'+renderNode(v,path+'['+i+']',sel,cid)+'</div>';
      }).join('');
      if(val.length>8) items+='<div style="padding-left:14px;color:var(--text-muted-color,#8b959e);font-style:italic">…('+val.length+' elementi)</div>';
      return '<span>[</span>'+items+'<span>]</span>';
    }
    if(typeof val==='object'){
      var keys=Object.keys(val);
      if(!keys.length) return '<span>{}</span>';
      var pairs=keys.slice(0,40).map(function(k){
        var cp=path+'.'+k;
        var ks='<span class="jt-key">"'+esc(k)+'"</span>: ';
        var child=val[k];
        if(sel&&(typeof child==='string'||typeof child==='number'||typeof child==='boolean')){
          return '<div style="padding-left:14px"><span class="jt-leaf" data-path="'+esc(cp)+'" data-call-id="'+(cid||'')+'" title="'+esc(cp)+'">'+ks+renderNode(child,cp,false,cid)+'</span></div>';
        }
        return '<div style="padding-left:14px">'+ks+renderNode(child,cp,sel,cid)+'</div>';
      }).join('');
      if(keys.length>40) pairs+='<div style="padding-left:14px;color:var(--text-muted-color,#8b959e);font-style:italic">…('+keys.length+' campi)</div>';
      return '<span>{</span>'+pairs+'<span>}</span>';
    }
    return esc(String(val));
  }

  function extractVarsFromParsed(parsed){
    var vars=[];
    if(!parsed) return vars;
    function walk(obj,prefix){
      if(typeof obj==='object'&&obj!==null&&!Array.isArray(obj)){
        Object.keys(obj).forEach(function(k){
          var p=prefix?prefix+'.'+k:k;
          if(typeof obj[k]==='string'||typeof obj[k]==='number'||typeof obj[k]==='boolean'){
            vars.push({p:p,s:String(obj[k]).substring(0,30)});
          } else { walk(obj[k],p); }
        });
      } else if(Array.isArray(obj)&&obj.length>0){ walk(obj[0],prefix+'[*]'); }
    }
    walk(parsed,'');
    return vars.slice(0,30);
  }

  function renderVarsPanel(callId,vars){
    var $p=$('#ac-resp-vars-'+callId).empty();
    if(!vars.length){ $p.html('<span style="font-size:12px;color:var(--text-muted-color,#8b959e);font-style:italic">Nessuna variabile</span>'); return; }
    $p.html('<div style="margin-bottom:5px;font-size:11px;color:var(--text-muted-color,#8b959e)">Clicca per inserire nel campo attivo</div>'+
      vars.map(function(v){ return '<span class="ac-var-chip" data-var="{{'+v.p+'}}" title="'+esc(v.s)+'">{{'+esc(v.p)+'}}</span>'; }).join(' '));
  }

  $('#f-schedule').on('input',function(){ $('#cron-preview').text(cronHuman($(this).val().trim())); });
  $('#f-pagination').on('change',function(){ $('#pagination-details').toggle($(this).val()!=='none'); });

  /* ═══════════════════════════════════════════════════════════
     STEP 3: PARSING
     ═══════════════════════════════════════════════════════════ */
  function renderParsingTree(){
    var ids=Object.keys(callResponses);
    if(!ids.length){ $('#parsing-tree').html('<p class="ac-hint">Esegui almeno una call nello Step 2.</p>'); return; }
    var resp=callResponses[ids[0]];
    try{
      var parsed=resp.parsed||JSON.parse(resp.body||'{}');
      $('#parsing-tree').html(renderNode(parsed,'$',true,'p'));
    } catch(e){ $('#parsing-tree').html('<p class="ac-hint">Risposta non JSON — usa campo manuale.</p>'); }
  }

  $(document).on('click','#parsing-tree .jt-leaf',function(){
    var path=$(this).data('path');
    $(this).toggleClass('selected');
    if($(this).hasClass('selected')) addFieldRow(path,'');
    else $('.ac-field-row').filter(function(){ return $(this).find('.field-path').val()===path; }).remove();
  });

  $('#btn-add-field').on('click',function(){ addFieldRow('',''); });

  function addFieldRow(path,alias){
    var exists=false;
    $('.field-path').each(function(){if($(this).val()===path&&path)exists=true;});
    if(exists) return;
    var $r=$('<div class="ac-field-row">'+
      '<input class="input-medium field-path ac-field-path" value="'+esc(path)+'" placeholder="$.campo o regex"/>'+
      '<span style="color:var(--text-muted-color,#8b959e);flex-shrink:0">→</span>'+
      '<input class="input-medium field-alias" value="'+esc(alias)+'" placeholder="alias"/>'+
      '<button class="btn btn-default btn-sm ac-btn-remove-field" type="button"><i class="icon-trash"></i></button>'+
    '</div>');
    $('#ac-fields-list').append($r);
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

  $('#f-response-format').on('change',function(){
    $('#text-regex-group').toggle($(this).val()==='text');
  });

  /* ═══════════════════════════════════════════════════════════
     STEP 4: TRASFORMAZIONI
     ═══════════════════════════════════════════════════════════ */
  function renderTransformsStep(){
    var fields=state.extracted_fields;
    var $area=$('#ac-transforms-area').empty();

    if(!fields.length){
      $area.html('<p class="ac-hint">Nessun campo estratto — vai allo Step 3 e seleziona i campi.</p>');
      return;
    }

    // Dropdown HTML per le trasformazioni
    var fnOpts='<option value="">— aggiungi trasformazione —</option>'+
      TRANSFORMS.map(function(t){ return '<option value="'+t.name+'">'+esc(t.label)+'</option>'; }).join('');

    fields.forEach(function(f){
      var key=f.alias||f.path;
      var pipeline=state.field_transforms[key]||[];
      var $card=$('<div class="ac-call-card" style="margin-bottom:10px">'+
        '<div class="ac-call-header" style="cursor:default">'+
          '<div class="ac-call-num" style="font-size:10px;width:auto;border-radius:3px;padding:0 6px">'+esc(key)+'</div>'+
          '<span class="ac-call-title">'+esc(f.path)+'</span>'+
          '<button class="btn btn-default ac-btn-sm ac-add-fn-btn" data-field="'+esc(key)+'">+ Trasformazione</button>'+
        '</div>'+
        '<div class="ac-call-body" style="display:block">'+
          '<div class="ac-call-left" id="ac-pipeline-'+esc(key)+'" style="min-height:40px">'+
            (pipeline.length?'':'<span class="ac-hint">Nessuna trasformazione — il valore passa invariato.</span>')+
          '</div>'+
          '<div class="ac-call-right" style="font-size:12px;color:var(--text-muted-color,#8b959e)">'+
            '<strong>Suggerimenti per "'+esc(key)+'":</strong><br/>'+
            getSuggestions(key)+
          '</div>'+
        '</div>'+
      '</div>');
      $area.append($card);

      // Renderizza pipeline esistente
      pipeline.forEach(function(step,i){ renderPipelineStep(key,step,i); });
    });

    // Legend
    var $leg=$('#ac-transforms-legend').empty();
    TRANSFORMS.forEach(function(t){
      $leg.append('<span class="ac-badge ac-badge--none" style="cursor:default;font-size:10px" title="'+esc(t.label)+'">'+esc(t.name)+'</span>');
    });
  }

  function getSuggestions(field){
    var map={
      time:'iso_to_epoch oppure strptime_to_epoch',
      username:'lower → strip',
      clientip:'regex_extract per estrarre solo IP',
      valorePrima:'to_float → round(2)',
      valoreDP:'to_float → round(2)',
      nomeapp:'upper',
    };
    return map[field]||'Nessun suggerimento specifico';
  }

  function renderPipelineStep(field,step,idx){
    var $pipeline=$('#ac-pipeline-'+field).find('.ac-hint').remove().end();
    var paramHtml='';
    var tDef=TRANSFORMS.filter(function(t){return t.name===step.fn;})[0];
    if(tDef&&tDef.params.length){
      paramHtml=tDef.params.map(function(p){
        var val=step[p]||'';
        return '<input class="input-medium ac-fn-param" data-param="'+esc(p)+'" value="'+esc(val)+'" placeholder="'+esc(p)+'"/>';
      }).join(' ');
    }
    var $step=$('<div class="ac-field-row ac-pipeline-step" data-field="'+esc(field)+'" data-idx="'+idx+'">'+
      '<span class="ac-badge ac-badge--info" style="flex-shrink:0">'+esc(step.fn)+'</span>'+
      (paramHtml?'<span style="flex-shrink:0;color:var(--text-muted-color,#8b959e)">params:</span>'+paramHtml:'<span style="color:var(--text-muted-color,#8b959e);font-size:11px;font-style:italic">nessun parametro</span>')+
      '<button class="btn btn-default btn-sm ac-remove-fn-btn" data-field="'+esc(field)+'" data-idx="'+idx+'"><i class="icon-trash"></i></button>'+
    '</div>');
    $pipeline.append($step);
  }

  $(document).on('click','.ac-add-fn-btn',function(){
    var field=$(this).data('field');
    // Mostra mini-dropdown
    var $sel=$('<select class="input-large" style="margin:6px 14px">'+
      '<option value="">— scegli trasformazione —</option>'+
      TRANSFORMS.map(function(t){ return '<option value="'+t.name+'">'+esc(t.label)+'</option>'; }).join('')+
    '</select>');
    var $container=$(this).closest('.ac-call-card').find('.ac-call-left');
    $container.append($sel);
    $sel.focus().on('change',function(){
      var fn=$(this).val();
      if(!fn){ $sel.remove(); return; }
      if(!state.field_transforms[field]) state.field_transforms[field]=[];
      var step={fn:fn};
      var tDef=TRANSFORMS.filter(function(t){return t.name===fn;})[0];
      if(tDef) tDef.params.forEach(function(p){ step[p]=''; });
      state.field_transforms[field].push(step);
      var idx=state.field_transforms[field].length-1;
      $sel.remove();
      $container.find('.ac-hint').remove();
      renderPipelineStep(field,step,idx);
    });
  });

  $(document).on('click','.ac-remove-fn-btn',function(){
    var field=$(this).data('field');
    var idx=parseInt($(this).data('idx'),10);
    if(state.field_transforms[field]) state.field_transforms[field].splice(idx,1);
    $(this).closest('.ac-pipeline-step').remove();
    if($('#ac-pipeline-'+field).children().length===0){
      $('#ac-pipeline-'+field).append('<span class="ac-hint">Nessuna trasformazione.</span>');
    }
  });

  $(document).on('change','.ac-fn-param',function(){
    var $step=$(this).closest('.ac-pipeline-step');
    var field=$step.data('field');
    var idx=parseInt($step.data('idx'),10);
    var param=$(this).data('param');
    if(state.field_transforms[field]&&state.field_transforms[field][idx]){
      state.field_transforms[field][idx][param]=$(this).val();
    }
  });

  function collectTransforms(){
    // Già aggiornato in tempo reale via eventi — nulla da fare
  }

  /* ═══════════════════════════════════════════════════════════
     STEP 5: TRACCIATO
     ═══════════════════════════════════════════════════════════ */
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

  /* ═══════════════════════════════════════════════════════════
     STEP 6: OUTPUT FORMAT
     ═══════════════════════════════════════════════════════════ */
  var OUTPUT_HINTS={
    pipe:'Pipe-separated: time|hostname|nomeapp|… — tracciato aziendale esatto.',
    kv:'Key-Value: time="…" hostname="…" — compatibile con Splunk field extraction automatica.',
    json:'JSON: {"time":"…","hostname":"…"} — per sourcetype con _json.',
    csv:'CSV: time,hostname,nomeapp,… — per pipeline CSV.',
    custom:'Separatore personalizzato — definisci sotto.'
  };

  function renderOutputFormatHint(){
    var fmt=$('#f-output-format').val()||'kv';
    $('#output-format-hint').text(OUTPUT_HINTS[fmt]||'');
    $('#custom-sep-group').toggle(fmt==='custom');
  }

  $('#f-output-format').on('change',renderOutputFormatHint);
  $('#f-checkpoint').on('change',function(){ $('#checkpoint-detail').toggle($(this).is(':checked')); });

  function loadIndexes(){
    service.get('/servicesNS/-/-/data/indexes',{count:100,output_mode:'json'},function(err,resp){
      if(err) return;
      var $s=$('#f-index').empty().append('<option value="">— Seleziona index —</option>');
      ((resp.data&&resp.data.entry)||[]).filter(function(e){return !e.name.startsWith('_');})
        .forEach(function(e){ $s.append('<option value="'+esc(e.name)+'">'+esc(e.name)+'</option>'); });
      if(state.index) $s.val(state.index);
    });
  }

  $('#f-cb-enabled').on('change',function(){
    $('#cb-detail').toggle($(this).is(':checked'));
  });

  /* ═══════════════════════════════════════════════════════════
     STEP 9: PREVIEW & SUMMARY
     ═══════════════════════════════════════════════════════════ */
  function renderPreview(){
    collectCurrentStep();
    // Prova a usare la risposta della prima call come record campione
    var sampleRecord=null;
    var ids=Object.keys(callResponses);
    if(ids.length){
      var resp=callResponses[ids[0]];
      if(resp&&resp.parsed){
        var p=resp.parsed;
        if(Array.isArray(p)&&p.length) sampleRecord=p[0];
        else if(p&&typeof p==='object'){
          // prova array_root
          var root=state.array_root.replace(/^\$\./,'').replace(/\[\*\]$/,'');
          if(root&&p[root]&&Array.isArray(p[root])&&p[root].length) sampleRecord=p[root][0];
          else sampleRecord=p;
        }
      }
    }

    if(!sampleRecord){
      $('#ac-mapped-preview').text('Nessun dato disponibile — esegui le call nello Step 2.');
      $('#ac-event-string-preview').text('—');
    } else {
      // Applica mapping tracciato
      var mapped={};
      TRACCIATO_FIELDS.forEach(function(tf){
        var src=state.field_mapping[tf.key]||'';
        if(!src) return;
        if(src.startsWith('__static__:')){ mapped[tf.key]=src.split(':')[1]; return; }
        // Estrai valore dal record
        var val=sampleRecord[src];
        if(val===undefined){
          // prova dotted path
          var parts=src.replace(/^\$\./,'').replace(/\[\*\]/g,'').split('.');
          val=sampleRecord;
          parts.forEach(function(p){ val=val&&typeof val==='object'?val[p]:val; });
        }
        if(val!==undefined) mapped[tf.key]=val;
      });

      // Costruisci stringa evento in base al formato
      var fmt=state.output_format||'kv';
      var sep=fmt==='pipe'?'|':fmt==='custom'?state.custom_sep:'|';
      var nullVal=state.null_value||'';
      var eventStr='';
      if(fmt==='pipe'||fmt==='custom'){
        eventStr=TRACCIATO_FIELDS.map(function(tf){
          return mapped[tf.key]!==undefined?String(mapped[tf.key]):nullVal;
        }).join(sep);
      } else if(fmt==='json'){
        eventStr=JSON.stringify(mapped,null,2);
      } else if(fmt==='csv'){
        eventStr=TRACCIATO_FIELDS.map(function(tf){
          var v=mapped[tf.key]!==undefined?String(mapped[tf.key]):nullVal;
          return v.indexOf(',')>=0?'"'+v+'"':v;
        }).join(',');
      } else {
        // kv
        eventStr=Object.entries(mapped).map(function(kv){
          return kv[0]+'="'+String(kv[1]||nullVal).replace(/"/g,'\\"')+'"';
        }).join(' ');
      }

      $('#ac-mapped-preview').text(JSON.stringify(mapped,null,2));
      $('#ac-event-string-preview').text(eventStr||'—');
    }

    renderSummary();
  }

  $('#btn-refresh-preview').on('click',renderPreview);

  function renderSummary(){
    collectCurrentStep();
    var transforms_count=Object.values(state.field_transforms).reduce(function(acc,p){ return acc+(p?p.length:0); },0);
    var kv=[
      ['Nome input',state.name||'—'],
      ['Auth globale',state.auth_type||'—'],
      ['Call configurate',state.calls.length],
      ['Auth override',state.calls.filter(function(c){return c.auth_type!=='inherited';}).length+' call'],
      ['Paginazione',state.pagination_type],
      ['Schedule',state.schedule||'—'],
      ['Formato risposta',state.response_format],
      ['Campi estratti',state.extracted_fields.length],
      ['Trasformazioni',transforms_count+' step in '+Object.keys(state.field_transforms).length+' campi'],
      ['Tracciato mappato',Object.values(state.field_mapping).filter(Boolean).length+' / '+TRACCIATO_FIELDS.length],
      ['Formato output',state.output_format],
      ['Index',state.index||'—'],
      ['Sourcetype',state.sourcetype||'—'],
      ['Circuit breaker',state.cb_enabled?'ON (soglia='+state.cb_threshold+', cooldown='+state.cb_cooldown+'s)':'OFF'],
      ['Logger source',state.logger_source||'—'],
    ];
    $('#ac-summary-content').html('<div class="ac-summary-kv">'+
      kv.map(function(p){ return '<span class="ac-summary-key">'+esc(p[0])+'</span><span class="ac-summary-val">'+esc(String(p[1]))+'</span>'; }).join('')+
    '</div>');
  }

  /* ═══════════════════════════════════════════════════════════
     GENERATE
     ═══════════════════════════════════════════════════════════ */
  $('#btn-generate').on('click',function(){
    collectCurrentStep();
    var errors=[];
    if(!state.name) errors.push('Nome input mancante (Step 1)');
    if(!state.calls.length||!state.calls[0].url) errors.push('URL endpoint mancante (Step 2)');
    if(!state.schedule) errors.push('Schedule mancante (Step 2)');
    if(!state.index) errors.push('Index mancante (Step 6)');
    if(!state.sourcetype) errors.push('Sourcetype mancante (Step 6)');
    if(!state.source) errors.push('Source mancante (Step 6)');
    if(!state.logger_source) errors.push('Logger source mancante (Step 8)');
    TRACCIATO_FIELDS.filter(function(tf){return tf.required;}).forEach(function(tf){
      if(!state.field_mapping[tf.key]) errors.push('Campo obbligatorio non mappato: '+tf.label);
    });
    if(errors.length){ alert('Errori:\n• '+errors.join('\n• ')); return; }

    // Costruisci config completo v3
    var cfg=$.extend(true,{},state);
    cfg.circuit_breaker=state.cb_enabled
      ?{failure_threshold:state.cb_threshold,cooldown_s:state.cb_cooldown}
      :null;
    cfg.output_config={
      format:state.output_format,
      separator:state.custom_sep,
      null_value:state.null_value,
      include_extra:state.include_extra,
      fields:TRACCIATO_FIELDS.map(function(tf){return tf.key;})
    };

    $('#ac-gen-modal').show();
    $('#gen-modal-title').text('Generazione…');
    $('#gen-modal-body').html('<div class="ac-spinner"><i class="icon-rotate-right"></i> Generazione script v3…</div>');
    $('#gen-modal-footer').hide();

    service.post('/servicesNS/nobody/api_connect/api_connect_generate',
      {config:JSON.stringify(cfg)},
      function(err,resp){
        if(err){
          $('#gen-modal-title').text('Errore');
          $('#gen-modal-body').html('<p style="color:#c62828">'+esc(err.message)+'</p>');
          $('#gen-modal-footer').show(); return;
        }
        var r=resp.data||{};
        $('#gen-modal-title').text('✅ Input generato con successo!');
        $('#gen-modal-body').html(
          '<div class="ac-summary-box"><div class="ac-summary-kv">'+
          '<span class="ac-summary-key">Script</span><span class="ac-summary-val">'+esc(r.script_path||'—')+'</span>'+
          '<span class="ac-summary-key">Stanza</span><span class="ac-summary-val">'+esc(r.stanza||'—')+'</span>'+
          '</div></div>'+
          (r.script_preview?'<pre class="ac-code-block" style="margin-top:12px;max-height:260px">'+esc(r.script_preview)+'</pre>':'')
        );
        $('#gen-modal-footer').show();
      }
    );
  });

  $('#btn-save-draft').on('click',function(){
    collectCurrentStep();
    service.post('/servicesNS/nobody/api_connect/storage/collections/data/api_connect_inputs',
      {name:state.name||'__draft__',config:JSON.stringify(state),last_status:'DRAFT'},function(){});
    alert('Bozza salvata.');
  });

  $('#btn-prev').on('click',function(){ goToStep(currentStep-1); });
  $('#btn-next').on('click',function(){ goToStep(currentStep+1); });

  /* ── Edit mode ── */
  var urlParams=new URLSearchParams(window.location.search);
  editKey=urlParams.get('edit');
  if(editKey){
    service.get('/servicesNS/nobody/api_connect/storage/collections/data/api_connect_inputs/'+editKey,{},function(err,resp){
      if(err||!resp.data) return;
      try{
        $.extend(state,JSON.parse(resp.data.config||'{}'));
        $('#f-name').val(state.name);
        $('#f-auth-type').val(state.auth_type).trigger('change');
        $('#f-schedule').val(state.schedule);
        $('#cron-preview').text(cronHuman(state.schedule));
      }catch(e){}
    });
  }

  /* ── Init ── */
  renderStep();
});
