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

// Checkout form: the buy buttons stay locked until the terms box is ticked, and a
// second click can't start a second checkout session. (Without JavaScript the
// browser still insists on the tick, and the server refuses checkout without it.)
document.querySelectorAll('form.buy-form').forEach(function (form) {
  var box = form.querySelector('input[name="agree"]');
  var hint = form.querySelector('[data-terms-hint]');
  var buttons = Array.prototype.slice.call(form.querySelectorAll('button[type="submit"]'));
  var locked = buttons.map(function (b) { return b.disabled; }); // true when checkout isn't switched on
  var labels = buttons.map(function (b) { return b.textContent; });
  var submitting = false;

  function sync() {
    var ok = !!(box && box.checked);
    buttons.forEach(function (b, i) { b.disabled = locked[i] || !ok; });
    if (hint) { hint.hidden = ok || locked.every(Boolean); }
  }

  if (box) { box.addEventListener('change', sync); }

  form.addEventListener('submit', function (event) {
    if (submitting || !(box && box.checked)) {
      event.preventDefault();
      return;
    }
    submitting = true;
    form.classList.add('busy');
    var btn = event.submitter;
    if (btn) { btn.textContent = btn.dataset.busy || 'Opening checkout…'; }
  });

  // Back button after Stripe: put everything back as it was.
  window.addEventListener('pageshow', function () {
    submitting = false;
    form.classList.remove('busy');
    buttons.forEach(function (b, i) { b.textContent = labels[i]; });
    sync();
  });

  sync();
});
