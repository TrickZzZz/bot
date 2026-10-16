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

// Loot reel: a row of screenshots that moves on by itself, and can be swiped,
// paused and stepped through. Without JavaScript it is still a plain row of
// pictures you can scroll, and the controls stay hidden.
document.querySelectorAll('[data-reel]').forEach(function (reel) {
  var track = reel.querySelector('[data-reel-track]');
  var bar = reel.querySelector('[data-reel-bar]');
  var slides = track ? Array.prototype.slice.call(track.children) : [];
  if (!track || !bar || slides.length < 2) { return; }

  var dots = Array.prototype.slice.call(reel.querySelectorAll('[data-reel-go]'));
  var toggle = reel.querySelector('[data-reel-toggle]');
  var prev = reel.querySelector('[data-reel-prev]');
  var next = reel.querySelector('[data-reel-next]');
  var interval = Math.max(1500, parseInt(reel.dataset.interval, 10) || 4500);
  var motion = window.matchMedia('(prefers-reduced-motion: reduce)');

  var userPaused = motion.matches; // people who ask for less motion start paused
  var hovering = false;            // a mouse is over the reel
  var focused = false;             // keyboard focus is on a picture
  var onScreen = true;             // the reel is in view
  var tabShown = !document.hidden; // the tab is in front
  var current = 0;
  var timer = null;
  var moving = false;              // a scroll we started is still under way
  var settleTimer = null;
  var frame = false;

  function indexNow() {
    var max = track.scrollWidth - track.clientWidth;
    if (max > 0 && track.scrollLeft >= max - 2) { return slides.length - 1; }
    var best = 0;
    var bestGap = Infinity;
    slides.forEach(function (slide, i) {
      var gap = Math.abs(slide.offsetLeft - track.scrollLeft);
      if (gap < bestGap) { best = i; bestGap = gap; }
    });
    return best;
  }

  function mark(i) {
    current = i;
    dots.forEach(function (dot, j) {
      if (j === i) { dot.setAttribute('aria-current', 'true'); }
      else { dot.removeAttribute('aria-current'); }
    });
  }

  function running() {
    return !userPaused && !hovering && !focused && onScreen && tabShown &&
      track.scrollWidth > track.clientWidth + 4;
  }

  function schedule() {
    clearTimeout(timer);
    timer = null;
    if (running()) { timer = setTimeout(function () { go(current + 1); }, interval); }
  }

  function settleSoon(ms) {
    clearTimeout(settleTimer);
    settleTimer = setTimeout(function () { moving = false; }, ms);
  }

  function go(i) {
    mark((i + slides.length) % slides.length);
    moving = true;
    settleSoon(1200); // covers the case where no scroll happens at all
    track.scrollTo({ left: slides[current].offsetLeft, behavior: motion.matches ? 'auto' : 'smooth' });
    schedule();
  }

  function showToggle() { if (toggle) { toggle.textContent = userPaused ? 'Play' : 'Pause'; } }

  // Swiping, dragging the scrollbar or arrow keys: follow the picture on screen
  // and give the person a full interval before the reel moves again.
  track.addEventListener('scroll', function () {
    if (moving) { settleSoon(160); return; }
    if (frame) { return; }
    frame = true;
    requestAnimationFrame(function () {
      frame = false;
      var i = indexNow();
      if (i !== current) { mark(i); }
      schedule();
    });
  }, { passive: true });

  // The person has taken over: stop treating the scroll as ours.
  ['pointerdown', 'touchstart', 'keydown'].forEach(function (name) {
    track.addEventListener(name, function () { moving = false; }, { passive: true });
  });
  track.addEventListener('wheel', function (event) {
    if (Math.abs(event.deltaX) > Math.abs(event.deltaY)) { moving = false; }
  }, { passive: true });

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
  window.addEventListener('resize', schedule);

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

  bar.hidden = false;
  mark(indexNow());
  showToggle();
  schedule();
});
