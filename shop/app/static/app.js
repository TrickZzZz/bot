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

// Key length dropdown. The real <select> stays in the form and is what gets sent;
// on screen it is replaced by a styled list. Without JavaScript the plain select works.
document.querySelectorAll('[data-dd]').forEach(function (root) {
  var select = root.querySelector('select');
  if (!select || select.options.length < 2) { return; }

  var options = Array.prototype.slice.call(select.options);
  var uid = select.id || 'dd';
  var label = document.querySelector('label[for="' + uid + '"]');
  var items = [];
  var active = 0;
  var isOpen = false;
  var typed = '';
  var typedTimer = null;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text) { node.textContent = text; }
    return node;
  }

  // One key length: name and optional tag, a short note, and the price.
  function row(opt) {
    var wrap = el('span', 'dd-row');
    var main = el('span', 'dd-main');
    main.appendChild(el('span', 'dd-name', opt.dataset.name || opt.textContent));
    if (opt.hasAttribute('data-best')) { main.appendChild(el('span', 'badge', 'Best value')); }
    wrap.appendChild(main);
    if (opt.dataset.note) { wrap.appendChild(el('span', 'dd-note', opt.dataset.note)); }
    if (opt.dataset.price) { wrap.appendChild(el('span', 'dd-price', opt.dataset.price)); }
    return wrap;
  }

  var svgNS = 'http://www.w3.org/2000/svg';
  var chev = document.createElementNS(svgNS, 'svg');
  chev.setAttribute('class', 'dd-chev');
  chev.setAttribute('viewBox', '0 0 20 20');
  chev.setAttribute('aria-hidden', 'true');
  chev.setAttribute('focusable', 'false');
  var chevPath = document.createElementNS(svgNS, 'path');
  chevPath.setAttribute('d', 'M5 7.5l5 5 5-5');
  chevPath.setAttribute('fill', 'none');
  chevPath.setAttribute('stroke', 'currentColor');
  chevPath.setAttribute('stroke-width', '2');
  chevPath.setAttribute('stroke-linecap', 'round');
  chevPath.setAttribute('stroke-linejoin', 'round');
  chev.appendChild(chevPath);

  var trigger = el('button', 'dd-trigger');
  trigger.type = 'button';
  trigger.id = uid + '-button';
  trigger.setAttribute('aria-haspopup', 'listbox');
  trigger.setAttribute('aria-expanded', 'false');
  trigger.setAttribute('aria-controls', uid + '-list');
  if (label) {
    label.id = label.id || uid + '-label';
    // Read out as "Key length, 7 days, £2.00 ...": the label, then what is picked.
    trigger.setAttribute('aria-labelledby', label.id + ' ' + trigger.id);
  }

  var list = el('ul', 'dd-list');
  list.id = uid + '-list';
  list.setAttribute('role', 'listbox');
  list.tabIndex = -1;
  list.hidden = true;
  if (label) { list.setAttribute('aria-labelledby', label.id); }

  // Show what is picked on the button and mark it in the list.
  function paint() {
    trigger.replaceChildren(row(options[select.selectedIndex]), chev);
    items.forEach(function (li, i) {
      li.setAttribute('aria-selected', i === select.selectedIndex ? 'true' : 'false');
    });
  }

  function setActive(i) {
    active = Math.max(0, Math.min(items.length - 1, i));
    items.forEach(function (li, j) { li.classList.toggle('is-active', j === active); });
    list.setAttribute('aria-activedescendant', items[active].id);
  }

  function openList() {
    if (isOpen) { return; }
    isOpen = true;
    list.hidden = false;
    root.classList.add('is-open');
    trigger.setAttribute('aria-expanded', 'true');
    // Open upwards when there is no room below.
    root.classList.remove('dd-up');
    var box = trigger.getBoundingClientRect();
    var need = list.offsetHeight + 12;
    if (window.innerHeight - box.bottom < need && box.top > need) { root.classList.add('dd-up'); }
    setActive(select.selectedIndex);
    list.focus({ preventScroll: true });
    list.scrollIntoView({ block: 'nearest' });
  }

  function closeList(toTrigger) {
    if (!isOpen) { return; }
    isOpen = false;
    list.hidden = true;
    root.classList.remove('is-open');
    trigger.setAttribute('aria-expanded', 'false');
    if (toTrigger) { trigger.focus(); }
  }

  function choose(i) {
    var changed = select.selectedIndex !== i;
    select.selectedIndex = i;
    paint();
    closeList(true);
    if (changed) { select.dispatchEvent(new Event('change', { bubbles: true })); }
  }

  // Typing "3" jumps to "30 days"; typing it again cycles through matches.
  function typeahead(ch) {
    clearTimeout(typedTimer);
    typed += ch.toLowerCase();
    typedTimer = setTimeout(function () { typed = ''; }, 700);
    var from = typed.length === 1 ? active + 1 : active;
    for (var n = 0; n < options.length; n++) {
      var i = (from + n) % options.length;
      var name = (options[i].dataset.name || options[i].textContent).toLowerCase();
      if (name.indexOf(typed) === 0) { setActive(i); return; }
    }
  }

  options.forEach(function (opt, i) {
    var li = el('li');
    li.id = uid + '-option-' + i;
    li.setAttribute('role', 'option');
    li.appendChild(row(opt));
    li.addEventListener('click', function () { choose(i); });
    li.addEventListener('pointermove', function () { if (active !== i) { setActive(i); } });
    list.appendChild(li);
    items.push(li);
  });

  trigger.addEventListener('click', function () {
    if (isOpen) { closeList(true); } else { openList(); }
  });
  trigger.addEventListener('keydown', function (event) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      openList();
    }
  });

  list.addEventListener('keydown', function (event) {
    var key = event.key;
    if (key === 'ArrowDown') { setActive(active + 1); }
    else if (key === 'ArrowUp') { setActive(active - 1); }
    else if (key === 'Home') { setActive(0); }
    else if (key === 'End') { setActive(items.length - 1); }
    else if (key === 'Enter' || key === ' ') { choose(active); }
    else if (key === 'Escape') { closeList(true); }
    else if (key === 'Tab') { closeList(false); return; }
    else if (key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) { typeahead(key); return; }
    else { return; }
    event.preventDefault();
  });

  document.addEventListener('pointerdown', function (event) {
    if (isOpen && !root.contains(event.target)) { closeList(false); }
  });

  // Clicking the label should land on the button, as it does for a normal select.
  if (label) {
    label.addEventListener('click', function (event) {
      event.preventDefault();
      trigger.focus();
    });
  }

  paint();
  root.appendChild(trigger);
  root.appendChild(list);
  select.hidden = true;
  select.tabIndex = -1;
  // Back button: the browser may bring back an earlier choice in the real select.
  window.addEventListener('pageshow', paint);
});

// Checkout form: the buy button stays locked until the terms box is ticked, its label
// follows the chosen key length, and a second click can't start a second checkout
// session. (Without JavaScript the browser still insists on the tick, and the server
// refuses checkout without it.)
document.querySelectorAll('form.buy-form').forEach(function (form) {
  var box = form.querySelector('input[name="agree"]');
  var hint = form.querySelector('[data-terms-hint]');
  var select = form.querySelector('select[name="plan"]');
  var buttons = Array.prototype.slice.call(form.querySelectorAll('button[type="submit"]'));
  var locked = buttons.map(function (b) { return b.disabled; }); // true when checkout isn't switched on
  var labels = buttons.map(function (b) { return b.textContent; });
  var submitting = false;

  // "Buy 7-day key" for whichever length is picked; the plain label if nothing is picked.
  function labelFor(i) {
    var opt = select && select.options[select.selectedIndex];
    return opt && opt.dataset.label ? 'Buy ' + opt.dataset.label : labels[i];
  }

  function sync() {
    var ok = !!(box && box.checked);
    buttons.forEach(function (b, i) {
      b.disabled = locked[i] || !ok;
      if (!submitting) { b.textContent = labelFor(i); }
    });
    if (hint) { hint.hidden = ok || locked.every(Boolean); }
  }

  if (box) { box.addEventListener('change', sync); }
  if (select) { select.addEventListener('change', sync); }

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
    sync();
  });

  sync();
});

// Loot screenshots: one picture at a time, in the same spot. Each stays for a moment,
// then fades into the next. Without JavaScript they are a plain row of pictures you
// can scroll, and the controls stay hidden.
document.querySelectorAll('[data-reel]').forEach(function (reel) {
  var track = reel.querySelector('[data-reel-track]');
  var bar = reel.querySelector('[data-reel-bar]');
  var slides = track ? Array.prototype.slice.call(track.children) : [];
  if (!track || !bar || slides.length < 2) { return; }

  var dots = Array.prototype.slice.call(reel.querySelectorAll('[data-reel-go]'));
  var toggle = reel.querySelector('[data-reel-toggle]');
  var prev = reel.querySelector('[data-reel-prev]');
  var next = reel.querySelector('[data-reel-next]');
  var hold = Math.max(500, parseInt(reel.dataset.hold, 10) || 1500); // ms a picture is fully shown
  var fade = 600;                                                    // ms to fade into the next (same as the CSS)
  var motion = window.matchMedia('(prefers-reduced-motion: reduce)');

  var userPaused = motion.matches; // people who ask for less motion start paused
  var hovering = false;            // a mouse is over the reel
  var focused = false;             // keyboard focus is on a picture
  var onScreen = true;             // the reel is in view
  var tabShown = !document.hidden; // the tab is in front
  var current = 0;
  var timer = null;
  var cleanup = null;

  reel.classList.add('is-fade');
  slides.forEach(function (slide) {
    // They all share one spot, so all of them count as "on screen": fetch them now so
    // a picture is ready before its turn.
    var img = slide.querySelector('img');
    if (img) { img.loading = 'eager'; }
  });

  function mark(i) {
    dots.forEach(function (dot, j) {
      if (j === i) { dot.setAttribute('aria-current', 'true'); }
      else { dot.removeAttribute('aria-current'); }
    });
  }

  // The new picture fades in on top of the old one, which stays put underneath until
  // the fade is done, so the frame never dips to empty halfway through.
  function show(i) {
    var to = (i + slides.length) % slides.length;
    if (to === current && slides[to].classList.contains('is-on')) { return; }
    var from = slides[current];
    clearTimeout(cleanup);
    slides.forEach(function (slide) { slide.classList.remove('was-on'); });
    if (from !== slides[to]) {
      from.classList.remove('is-on');
      from.classList.add('was-on');
    }
    slides[to].classList.add('is-on');
    current = to;
    mark(to);
    // Let go of the old picture a beat after the fade ends, never before.
    cleanup = setTimeout(function () { from.classList.remove('was-on'); }, motion.matches ? 0 : fade + 60);
  }

  function running() {
    return !userPaused && !hovering && !focused && onScreen && tabShown;
  }

  function schedule() {
    clearTimeout(timer);
    timer = null;
    var on = running();
    track.setAttribute('aria-live', on ? 'off' : 'polite'); // announce changes only when it isn't moving on its own
    if (on) { timer = setTimeout(function () { go(current + 1); }, (motion.matches ? 0 : fade) + hold); }
  }

  function go(i) {
    show(i);
    schedule();
  }

  function showToggle() { if (toggle) { toggle.textContent = userPaused ? 'Play' : 'Pause'; } }

  reel.addEventListener('pointerenter', function (event) {
    if (event.pointerType === 'mouse') { hovering = true; schedule(); }
  });
  reel.addEventListener('pointerleave', function (event) {
    if (event.pointerType === 'mouse') { hovering = false; schedule(); }
  });

  track.addEventListener('focusin', function (event) {
    var visible = true;
    try { visible = event.target.matches(':focus-visible'); } catch (err) { /* older browsers: assume keyboard */ }
    focused = visible;
    schedule();
  });
  track.addEventListener('focusout', function () { focused = false; schedule(); });

  if ('IntersectionObserver' in window) {
    new IntersectionObserver(function (entries) {
      onScreen = entries[entries.length - 1].isIntersecting;
      schedule();
    }, { threshold: 0.25 }).observe(reel);
  }
  document.addEventListener('visibilitychange', function () { tabShown = !document.hidden; schedule(); });

  if (toggle) {
    toggle.addEventListener('click', function () { userPaused = !userPaused; showToggle(); schedule(); });
  }
  if (prev) { prev.addEventListener('click', function () { go(current - 1); }); }
  if (next) { next.addEventListener('click', function () { go(current + 1); }); }
  dots.forEach(function (dot) {
    dot.addEventListener('click', function () { go(parseInt(dot.dataset.reelGo, 10) || 0); });
  });

  var onMotionChange = function () {
    if (motion.matches) { userPaused = true; showToggle(); schedule(); }
  };
  if (motion.addEventListener) { motion.addEventListener('change', onMotionChange); }

  slides[0].classList.add('is-on');
  bar.hidden = false;
  mark(0);
  showToggle();
  schedule();
});
