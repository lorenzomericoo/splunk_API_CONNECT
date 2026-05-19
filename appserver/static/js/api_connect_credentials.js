/**
 * API Connect — Credential Manager JS
 * CRUD su password.conf tramite /storage/passwords REST API di Splunk.
 * Il realm è sempre prefissato con "api_connect:" per namespace isolation.
 */
require([
  'jquery',
  'splunkjs/mvc',
  'splunkjs/mvc/simplexml/ready!'
], function($, mvc) {
  'use strict';

  var service = mvc.createService();
  var editTarget = null;  // realm of credential being edited
  var deleteTarget = null;

  var CRED_TYPE_LABELS = {
    bearer: 'Bearer Token',
    basic: 'Basic Auth',
    api_key: 'API Key',
    oauth2_cc: 'OAuth2 CC'
  };

  function escHtml(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  // ---- Load credentials ----
  function loadCredentials() {
    $('#ac-cred-tbody').html(
      '<tr><td colspan="6" class="ac-loading"><div class="ac-spinner">' +
      '<i class="icon-rotate-right"></i> Caricamento...</div></td></tr>'
    );

    service.get('/servicesNS/-/api_connect/storage/passwords', {
      count: 200,
      output_mode: 'json'
    }, function(err, resp) {
      if (err) {
        $('#ac-cred-tbody').html(
          '<tr><td colspan="6" style="color:#c62828;padding:16px">Errore: ' + escHtml(err.message) + '</td></tr>'
        );
        return;
      }

      var entries = (resp.data && resp.data.entry) ? resp.data.entry : [];
      // Filter only this app's credentials
      var mine = entries.filter(function(e) {
        return e.content && e.content.realm && e.content.realm.indexOf('api_connect:') === 0;
      });

      if (mine.length === 0) {
        $('#ac-cred-tbody').html(
          '<tr><td colspan="6" class="ac-loading" style="color:#8b959e">Nessuna credenziale configurata. Clicca "Nuova credenziale" per iniziare.</td></tr>'
        );
        return;
      }

      var rows = mine.map(function(e) {
        var c = e.content;
        var realm = c.realm || '';
        var label = realm.replace('api_connect:', '');
        var username = escHtml(c.username || '—');
        // Detect type from realm naming convention "api_connect:<type>:<label>"
        var parts = realm.split(':');
        var type = parts.length >= 3 ? CRED_TYPE_LABELS[parts[1]] || parts[1] : '—';
        var app = escHtml(e.acl && e.acl.app ? e.acl.app : 'api_connect');
        var updated = escHtml(e.updated ? new Date(e.updated).toLocaleString('it-IT') : '—');

        return [
          '<tr>',
            '<td><code>' + escHtml(label) + '</code></td>',
            '<td>' + username + '</td>',
            '<td><span class="ac-badge ac-badge--info">' + escHtml(type) + '</span></td>',
            '<td>' + app + '</td>',
            '<td>' + updated + '</td>',
            '<td>',
              '<div style="display:flex;gap:6px">',
                '<button class="btn btn-default btn-sm ac-btn-edit-cred" data-realm="' + escHtml(realm) + '" data-username="' + username + '">',
                  '<i class="icon-pencil"></i> Modifica',
                '</button>',
                '<button class="btn btn-default btn-sm ac-btn-del-cred" data-realm="' + escHtml(realm) + '" data-label="' + escHtml(label) + '">',
                  '<i class="icon-trash"></i>',
                '</button>',
              '</div>',
            '</td>',
          '</tr>'
        ].join('');
      }).join('');

      $('#ac-cred-tbody').html(rows);
    });
  }

  // ---- Show/hide credential type fields ----
  function updateCredTypeUI(type) {
    $('#cred-oauth2-group').toggle(type === 'oauth2_cc');
    var tokenGroup = type !== 'oauth2_cc';
    $('#cred-token-group').toggle(true); // always show username/password
    // Label adjustments
    if (type === 'bearer') {
      $('#cred-token-group label[for="cred-username"]').text('Label / Client ID');
      $('label[for="cred-password"]').text('Token');
    } else if (type === 'api_key') {
      $('label[for="cred-username"]').text('Nome param / header');
      $('label[for="cred-password"]').text('Valore API Key');
    } else if (type === 'basic') {
      $('label[for="cred-username"]').text('Username');
      $('label[for="cred-password"]').text('Password');
    } else if (type === 'oauth2_cc') {
      $('label[for="cred-username"]').text('Client ID');
      $('label[for="cred-password"]').text('Client Secret');
    }
  }

  $('#cred-type').on('change', function() {
    updateCredTypeUI($(this).val());
  });

  // ---- Open create modal ----
  $('#btn-new-cred').on('click', function() {
    editTarget = null;
    $('#cred-modal-title').text('Nuova credenziale');
    $('#cred-realm').val('').prop('readonly', false);
    $('#cred-type').val('bearer');
    $('#cred-username').val('');
    $('#cred-password').val('');
    $('#cred-token-url').val('');
    $('#cred-scope').val('');
    updateCredTypeUI('bearer');
    $('#ac-cred-modal').show();
  });

  // ---- Open edit modal ----
  $(document).on('click', '.ac-btn-edit-cred', function() {
    var realm = $(this).data('realm');
    var username = $(this).data('username');
    editTarget = realm;

    var parts = realm.replace('api_connect:', '').split(':');
    var type = parts.length >= 2 ? parts[0] : 'bearer';
    var label = parts.length >= 2 ? parts.slice(1).join(':') : parts[0];

    $('#cred-modal-title').text('Modifica credenziale');
    $('#cred-type').val(type);
    $('#cred-realm').val(label).prop('readonly', true);
    $('#cred-username').val(username);
    $('#cred-password').val(''); // never prefill secret
    updateCredTypeUI(type);
    $('#ac-cred-modal').show();
  });

  // ---- Toggle password visibility ----
  $(document).on('click', '.ac-btn-toggle-pw', function() {
    var $pw = $('#cred-password');
    var isText = $pw.attr('type') === 'text';
    $pw.attr('type', isText ? 'password' : 'text');
    $(this).find('i').toggleClass('icon-eye icon-eye-slash');
  });

  // ---- Save credential ----
  $('#cred-save').on('click', function() {
    var type = $('#cred-type').val();
    var label = $('#cred-realm').val().trim();
    var username = $('#cred-username').val().trim();
    var password = $('#cred-password').val();
    var tokenUrl = $('#cred-token-url').val().trim();
    var scope = $('#cred-scope').val().trim();

    if (!label) { alert('Label / Realm obbligatorio.'); return; }
    if (!username) { alert('Username / Client ID obbligatorio.'); return; }
    if (!editTarget && !password) { alert('Password / Token / Secret obbligatorio.'); return; }

    // Build realm: api_connect:<type>:<label>
    var realm = 'api_connect:' + type + ':' + label;

    // Embed extra fields into username as JSON prefix (workaround for password.conf single secret)
    var extraMeta = {};
    if (type === 'oauth2_cc') {
      extraMeta.token_url = tokenUrl;
      extraMeta.scope = scope;
    }
    var usernameVal = Object.keys(extraMeta).length
      ? JSON.stringify(extraMeta) + '||' + username
      : username;

    var $btn = $(this).prop('disabled', true).text('Salvataggio...');

    var endpoint = '/servicesNS/nobody/api_connect/storage/passwords';
    var method, url, data;

    if (editTarget) {
      // Update: DELETE + re-create (Splunk password update)
      url = endpoint + '/' + encodeURIComponent(editTarget.replace(':', '%3A').replace(':', '%3A'));
      service.del(url, {}, function() {
        createPassword(endpoint, realm, usernameVal, password || '__unchanged__', $btn);
      });
    } else {
      createPassword(endpoint, realm, usernameVal, password, $btn);
    }
  });

  function createPassword(endpoint, realm, username, password, $btn) {
    service.post(endpoint, {
      realm: realm,
      name: username,
      password: password
    }, function(err) {
      $btn.prop('disabled', false).text('Salva');
      if (err) {
        alert('Errore salvataggio: ' + err.message);
        return;
      }
      $('#ac-cred-modal').hide();
      loadCredentials();
    });
  }

  // ---- Delete ----
  $(document).on('click', '.ac-btn-del-cred', function() {
    deleteTarget = $(this).data('realm');
    $('#cred-delete-label').text($(this).data('label'));
    $('#ac-cred-delete-modal').show();
  });

  $('#cred-delete-cancel').on('click', function() {
    $('#ac-cred-delete-modal').hide();
    deleteTarget = null;
  });

  $('#cred-delete-confirm').on('click', function() {
    if (!deleteTarget) return;
    var encoded = encodeURIComponent(deleteTarget.replace(/:/g, '%3A'));
    service.del('/servicesNS/nobody/api_connect/storage/passwords/' + encoded, {}, function(err) {
      $('#ac-cred-delete-modal').hide();
      deleteTarget = null;
      if (err) { alert('Errore: ' + err.message); return; }
      loadCredentials();
    });
  });

  // ---- Cancel buttons ----
  $('#cred-cancel').on('click', function() { $('#ac-cred-modal').hide(); });

  // Close modal on overlay click
  $(document).on('click', '.ac-modal-overlay', function(e) {
    if ($(e.target).hasClass('ac-modal-overlay')) {
      $('.ac-modal-overlay').hide();
    }
  });

  // ---- Init ----
  loadCredentials();
});
