// Assay server UI. Server-rendered pages work without this file; it adds live filtering, the screenshot dialog's
// contents, copy buttons, the theme switch and picker placement where the platform lacks it. No framework, no build.
// The platform does the rest: <dialog> (focus, Esc, inert page), popover (light dismiss), invoker commands.
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const invokers = 'commandForElement' in HTMLButtonElement.prototype;

// ---- live filtering of a list: [data-filter-root] > [data-filter-list] > [data-item data-<facet>="a b"] -----------
for (const root of $$('[data-filter-root]')) {
  const items = $$('[data-item]', root);
  const groups = $$('[data-group]', root);
  const search = root.querySelector('[data-search]');
  const shown = root.querySelector('[data-shown]');
  const status = root.querySelector('[data-filter-status]');
  const active = {};
  let timer;
  const apply = () => {
    const q = (search?.value || '').trim().toLowerCase();
    const ok = items.map(it => Object.entries(active).every(([k, v]) => !v || (it.dataset[k] || '').split(' ').includes(v))
      && (!q || (it.dataset.text || it.textContent).toLowerCase().includes(q)));
    items.forEach((it, i) => { it.hidden = !ok[i]; });                  // reads first, then writes
    groups.forEach(g => { g.hidden = !$$('[data-item]', g).some(i => !i.hidden); });
    const n = ok.filter(Boolean).length;
    if (shown) shown.textContent = n;
    clearTimeout(timer);                                                // one polite announcement once typing stops
    if (status) timer = setTimeout(() => { status.textContent = `${n} of ${items.length} shown`; }, 500);
    const empty = root.querySelector('[data-empty]');
    if (empty) empty.hidden = n !== 0;
  };
  for (const b of $$('[data-facet]', root)) {
    b.addEventListener('click', () => {
      const k = b.dataset.facet;
      active[k] = active[k] === b.dataset.value ? '' : b.dataset.value;
      for (const x of $$(`[data-facet="${k}"]`, root)) x.setAttribute('aria-pressed', String(active[k] === x.dataset.value));
      apply();
    });
  }
  search?.addEventListener('input', apply);
  root.querySelector('[data-sort]')?.addEventListener('change', e => {     // reorder the existing nodes, never rebuild
    const key = e.target.value, parent = items[0]?.parentElement;
    if (!parent) return;
    const by = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
    const sorted = [...items].sort((a, b) => key === 'name' ? by.compare(a.dataset.name, b.dataset.name)
      : Number(b.dataset[key] || 0) - Number(a.dataset[key] || 0));
    for (const it of sorted) parent.moveBefore ? parent.moveBefore(it, null) : parent.append(it);
  });
  search?.form?.addEventListener('submit', e => { e.preventDefault(); apply(); });
  if (search?.value) apply();
}

// ---- the screenshot dialog: <button command=show-modal commandfor=lightbox data-full data-caption> ---------------
const box = document.getElementById('lightbox');
const fill = b => {
  const body = box.querySelector('.lb-body');
  const img = body.querySelector('img') || body.appendChild(Object.assign(new Image(), { decoding: 'async' }));
  img.src = b.dataset.full;
  img.alt = b.querySelector('img')?.alt || '';
  box.querySelector('[data-caption]').textContent = b.dataset.caption || 'Screenshot';
};
if (box) {
  box.addEventListener('command', e => { if (e.command === 'show-modal') fill(e.source); });
  if (!invokers) {
    document.addEventListener('click', e => {
      const b = e.target.closest('button[commandfor="lightbox"]');
      if (!b) return;
      fill(b);
      box.showModal();
    });
  }
  if (!('closedBy' in HTMLDialogElement.prototype)) {     // light dismiss: a press and release both outside the box
    let outside = false;
    const out = e => { const r = box.getBoundingClientRect();
      return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom; };
    box.addEventListener('pointerdown', e => { outside = out(e); });
    box.addEventListener('pointerup', e => { if (outside && out(e)) box.close(); outside = false; });
  }
}

// ---- copy buttons: <button command="--copy" commandfor="<code id>"> next to a role=status bubble ------------------
const timers = new WeakMap();
const report = (btn, ok) => {
  const tip = btn.parentElement.querySelector('[role=status]');
  const msg = ok ? 'Copied' : 'Copy failed';
  btn.toggleAttribute('data-copied', ok);
  btn.toggleAttribute('data-failed', !ok);
  if (tip) tip.textContent = tip.textContent.startsWith(msg) ? tip.textContent + ' ' : msg;   // re-announced
  clearTimeout(timers.get(btn));
  timers.set(btn, setTimeout(() => {
    btn.removeAttribute('data-copied'); btn.removeAttribute('data-failed');
    if (tip) tip.textContent = '';
  }, 2000));
};
const copy = (btn, target) => {
  if (!target) return;
  navigator.clipboard.writeText(target.innerText.trim()).then(() => report(btn, true), () => report(btn, false));
};
if (invokers) {
  document.addEventListener('command', e => { if (e.command === '--copy') copy(e.source, e.target); }, true);
} else {
  document.addEventListener('click', e => {
    const b = e.target.closest('button[command="--copy"]');
    if (b) copy(b, document.getElementById(b.getAttribute('commandfor')));
  });
}

// ---- pickers: a popover list of links under its button; placed by CSS anchor positioning, or here without it ------
document.addEventListener('toggle', e => {
  const p = e.target;
  if (!(p instanceof HTMLElement) || !p.matches('.picker')) return;
  const t = document.querySelector(`[popovertarget="${CSS.escape(p.id)}"]`);
  t?.setAttribute('aria-expanded', String(e.newState === 'open'));
  if (!t || e.newState !== 'open' || CSS.supports('position-area: block-end')) return;
  const r = t.getBoundingClientRect(), h = p.offsetHeight, w = p.offsetWidth;
  p.style.setProperty('top', `${(r.bottom + h + 8 <= innerHeight || r.top < h) ? r.bottom + 4 : r.top - h - 4}px`);
  p.style.setProperty('left', `${Math.max(8, Math.min(r.left, innerWidth - w - 8))}px`);
}, true);

// ---- theme: follows the system until the person picks the other scheme; the server renders the pick (no flash) ------
const mq = matchMedia('(prefers-color-scheme: dark)');
const themeBtns = $$('[data-theme-toggle]');
const root = document.documentElement;
const shownScheme = () => root.getAttribute('data-theme') || (mq.matches ? 'dark' : 'light');
const syncThemeBtns = () => {
  const next = shownScheme() === 'dark' ? 'light' : 'dark';
  for (const b of themeBtns) { b.hidden = false; b.dataset.next = next; b.setAttribute('aria-label', `Switch to ${next} theme`); }
};
for (const b of themeBtns) {
  b.addEventListener('click', () => {
    const next = shownScheme() === 'dark' ? 'light' : 'dark';
    const sys = mq.matches ? 'dark' : 'light';
    const meta = document.querySelector('meta[name="color-scheme"]');
    const pick = next === sys ? '' : next;     // back to the system: forget the choice instead of storing it
    if (pick) root.setAttribute('data-theme', pick); else root.removeAttribute('data-theme');
    document.cookie = pick ? `assay_theme=${pick}; Path=/; Max-Age=31536000; SameSite=Lax` : 'assay_theme=; Path=/; Max-Age=0; SameSite=Lax';
    meta?.setAttribute('content', pick || 'light dark');
    syncThemeBtns();
  });
}
mq.addEventListener('change', syncThemeBtns);
syncThemeBtns();

// ---- tabs on the setup page: role=tablist buttons, arrow keys move between them -----------------------------------
for (const list of $$('[role=tablist]')) {
  const tabs = $$('[role=tab]', list);
  const show = t => {
    for (const x of tabs) {
      const on = x === t;
      x.setAttribute('aria-selected', String(on));
      x.tabIndex = on ? 0 : -1;
      document.getElementById(x.getAttribute('aria-controls')).hidden = !on;
    }
  };
  list.addEventListener('click', e => { const t = e.target.closest('[role=tab]'); if (t) show(t); });
  list.addEventListener('keydown', e => {
    const i = tabs.indexOf(document.activeElement);
    const j = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (i < 0 || j === undefined) return;
    e.preventDefault();
    const t = tabs[(j + tabs.length) % tabs.length];
    t.focus(); show(t);
  });
}

// ---- show or hide a password: only its display changes; password managers still see a password field -------------
for (const b of $$('[data-reveal]')) {
  b.addEventListener('click', () => {
    const input = document.getElementById(b.dataset.reveal);
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    b.setAttribute('aria-pressed', String(show));
    b.textContent = show ? 'Hide' : 'Show';
  });
}

// ---- on a phone the project navigation is a row of tabs: the current one is in view -----------------------------------
const snavRow = document.querySelector('.snav');
const current = snavRow?.querySelector('.it.on, [aria-current="page"]');
if (snavRow && current && snavRow.scrollWidth > snavRow.clientWidth) snavRow.scrollLeft = current.offsetLeft - 12;
