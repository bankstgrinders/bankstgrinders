/* Shared menu renderer used by all TV display pages.
   Fetches data/menu.json and exposes helpers to render sections.
   Auto-reloads the page when menu.json changes (every 30s poll). */

(() => {
  let lastEtag = null;
  let loaded = null;

  async function loadMenu() {
    const res = await fetch('./data/menu.json?t=' + Date.now(), { cache: 'no-store' });
    if (!res.ok) throw new Error('menu.json fetch failed: ' + res.status);
    const etag = res.headers.get('etag') || res.headers.get('last-modified') || '';
    const data = await res.json();
    return { data, etag };
  }

  function esc(str) {
    return String(str ?? '').replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
    );
  }

  function renderSandwichSection(section, container) {
    const heading = document.createElement('h2');
    heading.className = 'tv-col-heading';
    heading.textContent = section.title;
    container.appendChild(heading);

    if (section.note) {
      const note = document.createElement('div');
      note.className = 'tv-works-note';
      note.textContent = section.note;
      container.appendChild(note);
    }

    section.items.forEach(item => {
      container.appendChild(renderItem(item));
    });
  }

  // Build a single <.tv-item> DOM element for a sandwich/panini item.
  // Honors item.size (integer percent, 50-200; default 100) by setting
  // a --item-scale CSS custom property on the row, which scales all
  // child font-sizes via calc() in tv.css.
  function renderItem(item) {
    const el = document.createElement('div');
    el.className = 'tv-item';
    const size = Number(item.size);
    if (Number.isFinite(size) && size !== 100 && size >= 50 && size <= 200) {
      el.style.setProperty('--item-scale', String(size / 100));
    }
    el.innerHTML =
      `<span class="tv-item-name">${esc(item.name)}</span>` +
      `<span class="tv-item-price">${esc(item.price)}</span>` +
      (item.desc ? `<span class="tv-item-desc">${esc(item.desc)}</span>` : '');
    return el;
  }

  function pollForChanges(reloadOnChange = true) {
    setInterval(async () => {
      try {
        const res = await fetch('./data/menu.json?t=' + Date.now(), { cache: 'no-store' });
        const etag = res.headers.get('etag') || res.headers.get('last-modified') || '';
        if (lastEtag && etag && etag !== lastEtag && reloadOnChange) {
          location.reload();
        }
        if (etag) lastEtag = etag;
      } catch (_) { /* keep showing what we have */ }
    }, 30000);
  }

  window.BSG = {
    async getMenu() {
      if (!loaded) loaded = loadMenu();
      const { data, etag } = await loaded;
      lastEtag = etag;
      return data;
    },
    renderSandwichSection,
    renderItem,
    pollForChanges,
    esc
  };
})();
