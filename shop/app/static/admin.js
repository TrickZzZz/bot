// Hosted scripts page (/admin): upload a script, make a new link, remove a script.
// Every request carries X-Admin-Request, which the server requires for changes made with the
// browser's sign-in (another website's request can't add it).
(function () {
  'use strict';

  var box = document.getElementById('admin-status');
  var HEADERS = { 'X-Admin-Request': '1' };

  function say(text, bad) {
    box.hidden = false;
    box.textContent = text;
    box.classList.toggle('warning', !!bad);
  }

  function send(method, url, body) {
    return fetch(url, { method: method, headers: HEADERS, body: body, credentials: 'same-origin', cache: 'no-store' })
      .then(function (resp) {
        return resp.json().catch(function () { return {}; }).then(function (data) {
          if (!resp.ok) { throw new Error(data.error || ('The server answered ' + resp.status + '.')); }
          return data;
        });
      });
  }

  var form = document.getElementById('upload-form');
  if (form) {
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var file = document.getElementById('script-file').files[0];
      var name = document.getElementById('script-name').value.trim();
      if (!file) { say('Choose a file first.', true); return; }
      say('Uploading ' + Math.max(1, Math.round(file.size / 1024)) + ' KB...');
      send('PUT', '/admin/scripts/' + encodeURIComponent(name), file)
        .then(function () { location.reload(); })
        .catch(function (err) { say(err.message, true); });
    });
  }

  document.querySelectorAll('[data-admin]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var name = btn.dataset.name;
      var rotate = btn.dataset.admin === 'rotate';
      var ask = rotate
        ? 'Make a new link for "' + name + '"? The old link stops working.'
        : 'Remove "' + name + '"? Its link stops working.';
      if (!window.confirm(ask)) { return; }
      var url = '/admin/scripts/' + encodeURIComponent(name) + (rotate ? '/rotate' : '');
      send(rotate ? 'POST' : 'DELETE', url)
        .then(function () { location.reload(); })
        .catch(function (err) { say(err.message, true); });
    });
  });
})();
