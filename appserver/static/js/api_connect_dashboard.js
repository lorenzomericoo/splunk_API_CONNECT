/**
 * API Connect — Dashboard JS
 * Carica gli input dal KV Store tramite REST e popola la tabella.
 */
require([
  'jquery',
  'splunkjs/mvc',
  'splunkjs/mvc/simplexml/ready!'
], function($, mvc) {
  'use strict';

  var service = mvc.createService();
  var deleteTarget = null;

  // ---- Utility ----
  function badge(status) {
    var cls = status === 'OK' ? 'ac-badge--ok' : status === 'ERROR' ? 'ac-badge--error' : 'ac-badge--none';
    return '<span class="ac-badge ' + cls + '">' + (status || '—') + '</span>';
  }

  function nextRun(cron) {
    if (!cron) return '—';
    // Simple human hint for common cron patterns
    var map = {
      '*/1 * * * *': 'ogni 1 min',
      '*/5 * * * *': 'ogni 5 min',
      '*/10 * * * *': 'ogni 10 min',
      '*/15 * * * *': 'ogni 15 min',
      '*/30 * * * *': 'ogni 30 min',
      '0 * * * *': 'ogni ora',
      '0 0 * * *': 'ogni giorno',
    };
    return map[cron] || cron;
  }

  function escHtml(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  // ---- Load inputs ----
  function loadInputs() {
    $('#ac-inputs-tbody').html(
      '<tr><td colspan="11" class="ac-loading"><div class="ac-spinner">' +
      '<i class="icon-rotate-right"></i> Caricamento...</div></td></tr>'
    );

    service.get('/servicesNS/nobody/api_connect/storage/collections/data/api_connect_inputs', {}, function(err, resp) {
      if (err) {
        $('#ac-inputs-tbody').html(
          '<tr><td colspan="11" class="ac-loading" style="color:#c62828">Errore caricamento: ' + escHtml(err.message) + '</td></tr>'
        );
        return;
      }

      var inputs = JSON.parse(resp.data.entry ? JSON.stringify(resp.data.entry) : '[]');
      // KV Store returns array directly for collections endpoint
      var data = resp.data;
      if (!Array.isArray(data)) data = [];

      updateStats(data);

      if (data.length === 0) {
        $('#ac-inputs-tbody').empty();
        $('#ac-empty-state').show();
        return;
      }

      $('#ac-empty-state').hide();
      var rows = data.map(function(item) {
        return buildRow(item);
      }).join('');
      $('#ac-inputs-tbody').html(rows);
    });
  }

  function updateStats(data) {
    var ok = data.filter(function(i){ return i.last_status === 'OK'; }).length;
    var err = data.filter(function(i){ return i.last_status === 'ERROR'; }).length;
    var totalLogs = data.reduce(function(acc, i){ return acc + (parseInt(i.last_count,10)||0); }, 0);
    $('#stat-total').text(data.length);
    $('#stat-ok').text(ok);
    $('#stat-error').text(err);
    $('#stat-logs').text(totalLogs.toLocaleString());
  }

  function buildRow(item) {
    var key = escHtml(item._key || '');
    var name = escHtml(item.name || item._key || '—');
    var url = escHtml(item.endpoint_url || '—');
    var auth = escHtml(item.auth_type || '—');
    var sched = escHtml(item.schedule || '—');
    var idx = escHtml(item.index || '—');
    var st = escHtml(item.sourcetype || '—');
    var lastRun = escHtml(item.last_run || '—');
    var latency = item.last_latency_ms ? item.last_latency_ms + ' ms' : '—';
    var logCount = item.last_count !== undefined ? parseInt(item.last_count,10).toLocaleString() : '—';

    return [
      '<tr data-key="' + key + '">',
        '<td><strong>' + name + '</strong></td>',
        '<td><code>' + url + '</code></td>',
        '<td>' + auth + '</td>',
        '<td><code>' + sched + '</code></td>',
        '<td>' + idx + ' / <em>' + st + '</em></td>',
        '<td>' + lastRun + '</td>',
        '<td>' + badge(item.last_status) + '</td>',
        '<td>' + logCount + '</td>',
        '<td>' + latency + '</td>',
        '<td>' + nextRun(item.schedule) + '</td>',
        '<td>',
          '<div style="display:flex;gap:6px">',
            '<a href="input_builder?edit=' + key + '" class="btn btn-default btn-sm">',
              '<i class="icon-pencil"></i> Modifica',
            '</a>',
            '<button class="btn btn-default btn-sm ac-btn-delete" data-key="' + key + '" data-name="' + name + '">',
              '<i class="icon-trash"></i>',
            '</button>',
          '</div>',
        '</td>',
      '</tr>'
    ].join('');
  }

  // ---- Delete ----
  $(document).on('click', '.ac-btn-delete', function() {
    deleteTarget = $(this).data('key');
    $('#ac-delete-name').text($(this).data('name'));
    $('#ac-delete-modal').show();
  });

  $('#ac-delete-cancel').on('click', function() {
    $('#ac-delete-modal').hide();
    deleteTarget = null;
  });

  $('#ac-delete-confirm').on('click', function() {
    if (!deleteTarget) return;
    service.del('/servicesNS/nobody/api_connect/storage/collections/data/api_connect_inputs/' + deleteTarget, {}, function(err) {
      $('#ac-delete-modal').hide();
      deleteTarget = null;
      if (err) {
        alert('Errore eliminazione: ' + err.message);
      } else {
        loadInputs();
      }
    });
  });

  // ---- Refresh ----
  $('#ac-refresh-btn').on('click', loadInputs);

  // ---- Init ----
  loadInputs();
});
