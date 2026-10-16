// Copy buttons on the confirmation page.
document.querySelectorAll('[data-copy]').forEach(function (btn) {
  btn.addEventListener('click', async function () {
    var label = btn.dataset.label || btn.textContent;
    try {
      await navigator.clipboard.writeText(btn.dataset.copy);
      btn.textContent = 'Copied';
    } catch (err) {
      // Clipboard blocked: select the key so Ctrl+C / long-press works.
      var target = btn.dataset.fallback && document.querySelector(btn.dataset.fallback);
      if (target) {
        var range = document.createRange();
        range.selectNodeContents(target);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      }
      btn.textContent = 'Press Ctrl+C';
    }
    setTimeout(function () { btn.textContent = label; }, 2000);
  });
});

// Stop double-clicks creating two checkout sessions; undo if the user comes back.
document.querySelectorAll('form.buy').forEach(function (form) {
  var button = form.querySelector('button');
  var label = button.textContent;
  var startedDisabled = button.disabled; // true when checkout isn't switched on
  form.addEventListener('submit', function () {
    button.disabled = true;
    button.textContent = button.dataset.busy || 'Opening checkout…';
  });
  window.addEventListener('pageshow', function () {
    button.disabled = startedDisabled;
    button.textContent = label;
  });
});
