/* Admin form for editing menu.json + playlist.json.
   - Authenticates via HTTP Basic against the Flask API.
   - Stores credentials in localStorage so uncle stays logged in across visits / app launches.
   - Renders a form from the data, collects values on save, POSTs the updated JSON. */

const loginEl = document.getElementById('login');
const adminEl = document.getElementById('admin');
const bodyEl = document.getElementById('adminBody');
const saveBtn = document.getElementById('saveBtn');
const saveStatus = document.getElementById('saveStatus');

let menu = null;
let playlist = null;

// Files queued for deletion on next Save (so we don't unlink before commit).
const pendingFileDeletes = new Set();

// ---------- AUTH ----------
// Using localStorage so uncle stays logged in across visits / app launches.

function getAuth() {
  return localStorage.getItem('bsg_auth');
}

function setAuth(user, pass) {
  const token = btoa(`${user}:${pass}`);
  localStorage.setItem('bsg_auth', token);
  return token;
}

function clearAuth() { localStorage.removeItem('bsg_auth'); }

async function tryLoadMenu() {
  const auth = getAuth();
  if (!auth) { showLogin(); return; }
  try {
    const [menuRes, playRes] = await Promise.all([
      fetch('/api/menu', { headers: { Authorization: `Basic ${auth}` } }),
      fetch('/api/playlist', { headers: { Authorization: `Basic ${auth}` } }),
    ]);
    if (menuRes.status === 401) { clearAuth(); showLogin(); return; }
    if (!menuRes.ok) throw new Error('Failed: ' + menuRes.status);
    menu = await menuRes.json();
    playlist = playRes.ok ? await playRes.json() : { slides: [] };
    showAdmin();
    renderForm(menu);
  } catch (e) {
    console.error(e);
    showLogin();
  }
}

function showLogin() {
  loginEl.style.display = 'block';
  adminEl.style.display = 'none';
  document.getElementById('loginUser').focus();
}

function showAdmin() {
  loginEl.style.display = 'none';
  adminEl.style.display = 'block';
}

document.getElementById('loginBtn').onclick = doLogin;
document.getElementById('loginPass').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });

async function doLogin() {
  const user = document.getElementById('loginUser').value.trim();
  const pass = document.getElementById('loginPass').value;
  const errEl = document.getElementById('loginError');
  errEl.style.display = 'none';
  if (!user || !pass) { errEl.textContent = 'Enter a username and password'; errEl.style.display = 'block'; return; }
  setAuth(user, pass);
  try {
    const res = await fetch('/api/menu', { headers: { Authorization: `Basic ${getAuth()}` } });
    if (res.status === 401) {
      clearAuth();
      errEl.textContent = 'Wrong username or password';
      errEl.style.display = 'block';
      return;
    }
    if (!res.ok) throw new Error('Error: ' + res.status);
    menu = await res.json();
    const playRes = await fetch('/api/playlist', { headers: { Authorization: `Basic ${getAuth()}` } });
    playlist = playRes.ok ? await playRes.json() : { slides: [] };
    showAdmin();
    renderForm(menu);
  } catch (e) {
    errEl.textContent = 'Could not connect to server';
    errEl.style.display = 'block';
    clearAuth();
  }
}

// ---------- RENDERING ----------

function el(tag, opts = {}, ...children) {
  const e = document.createElement(tag);
  if (opts.class) e.className = opts.class;
  if (opts.style) e.style.cssText = opts.style;
  if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) e.setAttribute(k, v);
  if (opts.text) e.textContent = opts.text;
  children.forEach(c => c && e.appendChild(c));
  return e;
}

function section(title, noteText) {
  const sec = el('section', { class: 'section' });
  sec.appendChild(el('div', { class: 'section-head', text: title }));
  if (noteText) sec.appendChild(el('div', { class: 'section-note', text: noteText }));
  const body = el('div', { class: 'section-body' });
  sec.appendChild(body);
  return { sec, body };
}

function labeledInput(labelText, value, dataPath, opts = {}) {
  const wrap = el('div', { class: 'field' });
  wrap.appendChild(el('label', { class: 'field-label', text: labelText }));
  const input = document.createElement(opts.textarea ? 'textarea' : 'input');
  if (!opts.textarea) input.type = 'text';
  input.value = value ?? '';
  input.dataset.path = dataPath;
  input.setAttribute('autocomplete', 'off');
  input.setAttribute('autocapitalize', 'off');
  input.setAttribute('autocorrect', 'off');
  input.setAttribute('spellcheck', 'false');
  if (opts.price) {
    input.className = 'price-input';
    // Show decimal keypad on iPad/iPhone for price fields
    input.setAttribute('inputmode', 'decimal');
  }
  if (opts.placeholder) input.placeholder = opts.placeholder;
  wrap.appendChild(input);
  return wrap;
}

// Sandwich category labels for the "Move to" dropdown
const SANDWICH_CATEGORIES = {
  italianGrinders: 'Italian Grinders',
  hotGrinders: 'Hot Grinders',
  coldGrinders: 'Cold Grinders',
  localFavorites: 'Local Favorites',
  paninis: 'Paninis',
};

// Capture current input values, apply a mutation to the menu structure,
// then re-render. Snapshot-via-collectForm preserves any in-progress edits.
function mutateMenuAndReRender(mutationFn) {
  const updated = collectForm();
  mutationFn(updated);
  menu = updated;
  renderForm(menu);
}

function moveSandwichItem(categoryKey, index, delta) {
  mutateMenuAndReRender(m => {
    const items = m.sandwiches[categoryKey].items;
    const j = index + delta;
    if (j < 0 || j >= items.length) return;
    [items[index], items[j]] = [items[j], items[index]];
  });
}

function moveSandwichItemToCategory(srcKey, index, destKey) {
  if (srcKey === destKey) return;
  mutateMenuAndReRender(m => {
    const item = m.sandwiches[srcKey].items.splice(index, 1)[0];
    if (item) m.sandwiches[destKey].items.push(item);
  });
}

function addSandwichItem(categoryKey) {
  mutateMenuAndReRender(m => {
    m.sandwiches[categoryKey].items.push({ name: '', desc: '', price: '' });
  });
  // Scroll the new (empty) row into view and focus its name field
  setTimeout(() => {
    const newName = bodyEl.querySelector(
      `input[data-path="sandwiches.${categoryKey}.items.${menu.sandwiches[categoryKey].items.length - 1}.name"]`
    );
    if (newName) {
      newName.scrollIntoView({ behavior: 'smooth', block: 'center' });
      newName.focus();
    }
  }, 50);
}

function setSandwichItemSize(categoryKey, index, sizePct) {
  // Clamp to 50-200; treat 100 as "remove the field" so the JSON stays
  // clean for items at default size. Trim empty-string inputs to 100
  // (default) instead of letting Number('') === 0 clamp to 50.
  const trimmed = String(sizePct == null ? '' : sizePct).trim();
  let v = trimmed === '' ? 100 : Math.round(Number(trimmed));
  if (!Number.isFinite(v)) v = 100;
  v = Math.max(50, Math.min(200, v));
  // Mutate menu in place — NO re-render. Size has no admin-visible side
  // effect (the result is on the TV, not in the form), and re-rendering
  // would destroy the size <input> and steal focus mid-edit, breaking
  // the spinner / arrow-step tweaking flow. The change is captured on
  // the next Save Changes via collectForm starting from menu state.
  const cat = menu && menu.sandwiches && menu.sandwiches[categoryKey];
  const item = cat && cat.items && cat.items[index];
  if (!item) return;
  if (v === 100) delete item.size;
  else item.size = v;
}

function setSandwichItemDescSize(categoryKey, index, val) {
  // descSize is OPTIONAL and INDEPENDENT of item.size. When unset, desc
  // follows --item-scale via CSS fallback. When set (50-200), it
  // overrides the desc scale only. Empty input → delete the field.
  const cat = menu && menu.sandwiches && menu.sandwiches[categoryKey];
  const item = cat && cat.items && cat.items[index];
  if (!item) return;
  const trimmed = String(val == null ? '' : val).trim();
  if (trimmed === '') { delete item.descSize; return; }
  let v = Math.round(Number(trimmed));
  if (!Number.isFinite(v)) { delete item.descSize; return; }
  v = Math.max(50, Math.min(200, v));
  item.descSize = v;
}

function setSandwichItemDescBold(categoryKey, index, bold) {
  const cat = menu && menu.sandwiches && menu.sandwiches[categoryKey];
  const item = cat && cat.items && cat.items[index];
  if (!item) return;
  if (bold) item.descBold = true;
  else delete item.descBold;
}

// "The Works" note size/bold controls — same in-place mutation pattern
// as the per-item knobs (no re-render, preserves input focus).
function setSandwichCategoryNoteSize(categoryKey, val) {
  const cat = menu && menu.sandwiches && menu.sandwiches[categoryKey];
  if (!cat) return;
  // Trim first — empty input means "back to default" (100), not 0.
  // Without this, Number('') is 0, which is finite, and the clamp
  // below would silently store 50.
  const trimmed = String(val == null ? '' : val).trim();
  let v = trimmed === '' ? 100 : Math.round(Number(trimmed));
  if (!Number.isFinite(v)) v = 100;
  v = Math.max(50, Math.min(200, v));
  if (v === 100) delete cat.noteSize;
  else cat.noteSize = v;
}

function setSandwichCategoryNoteBold(categoryKey, bold) {
  const cat = menu && menu.sandwiches && menu.sandwiches[categoryKey];
  if (!cat) return;
  if (bold) cat.noteBold = true;
  else delete cat.noteBold;
}

function deleteSandwichItem(categoryKey, index) {
  const item = menu.sandwiches[categoryKey].items[index];
  const label = (item && item.name) ? item.name : 'this item';
  if (!confirm(`Remove "${label}" from the menu?\n\n(Takes effect when you tap Save Changes. Saved menus are backed up automatically — recoverable from data/backups/.)`)) return;
  mutateMenuAndReRender(m => {
    m.sandwiches[categoryKey].items.splice(index, 1);
  });
}

function renderItemActions(reorderInfo) {
  const bar = el('div', { class: 'item-actions' });

  const upBtn = el('button', { class: 'item-btn', text: '↑' });
  upBtn.title = 'Move up in this category';
  upBtn.disabled = reorderInfo.index === 0;
  upBtn.onclick = () => moveSandwichItem(reorderInfo.categoryKey, reorderInfo.index, -1);

  const downBtn = el('button', { class: 'item-btn', text: '↓' });
  downBtn.title = 'Move down in this category';
  downBtn.disabled = reorderInfo.index === reorderInfo.total - 1;
  downBtn.onclick = () => moveSandwichItem(reorderInfo.categoryKey, reorderInfo.index, 1);

  bar.appendChild(upBtn);
  bar.appendChild(downBtn);

  // Cross-category move (only for sandwich items)
  if (reorderInfo.kind === 'sandwiches') {
    const select = document.createElement('select');
    select.className = 'item-move-select';
    const placeholder = document.createElement('option');
    placeholder.textContent = 'Move to category…';
    placeholder.value = '';
    select.appendChild(placeholder);
    for (const [key, label] of Object.entries(SANDWICH_CATEGORIES)) {
      if (key === reorderInfo.categoryKey) continue;
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = label;
      select.appendChild(opt);
    }
    select.onchange = () => {
      if (select.value) {
        moveSandwichItemToCategory(reorderInfo.categoryKey, reorderInfo.index, select.value);
      }
    };
    bar.appendChild(select);

    // Per-item size knob: 50-200%, default 100 = normal. Lets the user
    // shrink one item that wraps onto a second line, or bump up a hero
    // sandwich. Saved as item.size (omitted when 100).
    const sizeWrap = el('label', { class: 'item-size-wrap' });
    sizeWrap.appendChild(el('span', { class: 'item-size-label', text: 'Size' }));
    const sizeInput = document.createElement('input');
    sizeInput.type = 'number';
    sizeInput.className = 'item-size-input';
    sizeInput.min = '50';
    sizeInput.max = '200';
    sizeInput.step = '5';
    sizeInput.setAttribute('inputmode', 'numeric');
    sizeInput.title = '50–200, 100 = normal size';
    const currentSize = (menu && menu.sandwiches && menu.sandwiches[reorderInfo.categoryKey]
      && menu.sandwiches[reorderInfo.categoryKey].items[reorderInfo.index]
      && menu.sandwiches[reorderInfo.categoryKey].items[reorderInfo.index].size) || 100;
    sizeInput.value = String(currentSize);
    sizeInput.onchange = () => {
      setSandwichItemSize(reorderInfo.categoryKey, reorderInfo.index, sizeInput.value);
    };
    sizeWrap.appendChild(sizeInput);
    sizeWrap.appendChild(el('span', { class: 'item-size-suffix', text: '%' }));
    bar.appendChild(sizeWrap);

    // Description-only overrides: size (independent of item size) + bold toggle.
    // Both are optional. Leaving Desc size blank means "follow item size".
    const descWrap = el('label', { class: 'item-size-wrap' });
    descWrap.appendChild(el('span', { class: 'item-size-label', text: 'Desc' }));
    const descSizeInput = document.createElement('input');
    descSizeInput.type = 'number';
    descSizeInput.className = 'item-size-input';
    descSizeInput.min = '50';
    descSizeInput.max = '200';
    descSizeInput.step = '5';
    descSizeInput.placeholder = 'auto';
    descSizeInput.setAttribute('inputmode', 'numeric');
    descSizeInput.title = 'Desc size override (50–200). Blank = follow item size.';
    const itemRef = (menu && menu.sandwiches && menu.sandwiches[reorderInfo.categoryKey]
      && menu.sandwiches[reorderInfo.categoryKey].items[reorderInfo.index]) || {};
    descSizeInput.value = (Number.isFinite(itemRef.descSize) && itemRef.descSize >= 50 && itemRef.descSize <= 200)
      ? String(itemRef.descSize) : '';
    descSizeInput.onchange = () => {
      setSandwichItemDescSize(reorderInfo.categoryKey, reorderInfo.index, descSizeInput.value);
    };
    descWrap.appendChild(descSizeInput);
    descWrap.appendChild(el('span', { class: 'item-size-suffix', text: '%' }));

    const boldLabel = el('label', { class: 'item-size-wrap', style: 'cursor: pointer;' });
    const boldCheck = document.createElement('input');
    boldCheck.type = 'checkbox';
    boldCheck.className = 'item-bold-check';
    boldCheck.checked = itemRef.descBold === true;
    boldCheck.onchange = () => {
      setSandwichItemDescBold(reorderInfo.categoryKey, reorderInfo.index, boldCheck.checked);
    };
    boldLabel.appendChild(boldCheck);
    boldLabel.appendChild(el('span', { class: 'item-size-label', text: 'Bold' }));

    bar.appendChild(descWrap);
    bar.appendChild(boldLabel);

    const delBtn = el('button', { class: 'item-btn danger', text: 'Delete' });
    delBtn.title = 'Remove this sandwich from the menu';
    delBtn.onclick = () => deleteSandwichItem(reorderInfo.categoryKey, reorderInfo.index);
    bar.appendChild(delBtn);
  }

  return bar;
}

function renderItemRow(item, basePath, optsOrFields = {}) {
  // Backwards-compatible: callers may pass an array of field names directly,
  // or an object { fields, reorder } for richer options.
  const opts = Array.isArray(optsOrFields) ? { fields: optsOrFields } : optsOrFields;
  const fields = opts.fields || ['name', 'desc', 'price'];
  const row = el('div', { class: fields.length === 2 ? 'item-row compact' : 'item-row' });
  if (fields.includes('name'))  row.appendChild(labeledInput('Name',  item.name,  `${basePath}.name`));
  if (fields.includes('desc'))  row.appendChild(labeledInput('Description', item.desc, `${basePath}.desc`));
  if (fields.includes('price')) row.appendChild(labeledInput('Price', item.price, `${basePath}.price`, { price: true }));
  if (opts.reorder) row.appendChild(renderItemActions(opts.reorder));
  return row;
}

function renderSandwichCategory(key, data, parent) {
  const { sec, body } = section(data.title);

  // Editable "works" note for the category — shown above the items as a
  // small italic line on the static menu (e.g. "The Works: lettuce,
  // tomato, onion, mayo..."). Empty for categories that don't need one.
  body.appendChild(labeledInput(
    '"The Works" note (small italic line above the items)',
    data.note,
    `sandwiches.${key}.note`,
    { textarea: true, placeholder: 'Optional — leave blank for none' }
  ));

  // Size + bold controls for the works note, in a small toolbar.
  const noteCtrls = el('div', { class: 'item-actions', style: 'margin-bottom: 14px;' });

  const sizeWrap = el('label', { class: 'item-size-wrap' });
  sizeWrap.appendChild(el('span', { class: 'item-size-label', text: 'Note size' }));
  const sizeInput = document.createElement('input');
  sizeInput.type = 'number';
  sizeInput.className = 'item-size-input';
  sizeInput.min = '50';
  sizeInput.max = '200';
  sizeInput.step = '5';
  sizeInput.setAttribute('inputmode', 'numeric');
  sizeInput.title = '50–200, 100 = normal size';
  sizeInput.value = String(
    (Number.isFinite(data.noteSize) && data.noteSize >= 50 && data.noteSize <= 200)
      ? data.noteSize : 100
  );
  sizeInput.onchange = () => {
    setSandwichCategoryNoteSize(key, sizeInput.value);
  };
  sizeWrap.appendChild(sizeInput);
  sizeWrap.appendChild(el('span', { class: 'item-size-suffix', text: '%' }));

  const boldLabel = el('label', { class: 'item-size-wrap', style: 'cursor: pointer;' });
  const boldCheck = document.createElement('input');
  boldCheck.type = 'checkbox';
  boldCheck.className = 'item-bold-check';
  boldCheck.checked = data.noteBold === true;
  boldCheck.onchange = () => {
    setSandwichCategoryNoteBold(key, boldCheck.checked);
  };
  boldLabel.appendChild(boldCheck);
  boldLabel.appendChild(el('span', { class: 'item-size-label', text: 'Bold' }));

  noteCtrls.appendChild(sizeWrap);
  noteCtrls.appendChild(boldLabel);
  body.appendChild(noteCtrls);

  const total = data.items.length;
  data.items.forEach((item, i) => {
    body.appendChild(renderItemRow(item, `sandwiches.${key}.items.${i}`, {
      reorder: { kind: 'sandwiches', categoryKey: key, index: i, total }
    }));
  });

  const addBtn = el('button', { class: 'add-item-btn', text: `+ Add to ${data.title}` });
  addBtn.onclick = () => addSandwichItem(key);
  body.appendChild(addBtn);

  parent.appendChild(sec);
}

function renderForm(m) {
  bodyEl.innerHTML = '';

  // Two panes — TV #1 (the static menu) and TV #2 (the rotation). Both
  // are rendered into the DOM together so collectForm() can read every
  // input even when its tab isn't active. CSS shows only the active one.
  const tv1Pane = el('div', { class: 'tab-pane', attrs: { 'data-pane': 'tv1' } });
  const tv2Pane = el('div', { class: 'tab-pane', attrs: { 'data-pane': 'tv2' } });

  renderTv1Sections(m, tv1Pane);
  renderRotationSection(tv2Pane);

  bodyEl.appendChild(tv1Pane);
  bodyEl.appendChild(tv2Pane);

  // Apply current tab from localStorage (default tv1)
  applyActiveTab(localStorage.getItem('bsg_active_tab') || 'tv1');
}

function renderTv1Sections(m, parent) {
  // ----- INFO -----
  {
    const { sec, body } = section('Restaurant Info');
    const grid = el('div', { class: 'info-grid' });
    grid.appendChild(labeledInput('Name', m.info.name, 'info.name'));
    grid.appendChild(labeledInput('Phone', m.info.phone, 'info.phone'));
    grid.appendChild(labeledInput('Slogan', m.info.slogan, 'info.slogan', { textarea: true }));
    grid.appendChild(labeledInput('Established', m.info.established, 'info.established'));
    grid.appendChild(labeledInput('Address Line 1', m.info.addressLine1, 'info.addressLine1'));
    grid.appendChild(labeledInput('Address Line 2', m.info.addressLine2, 'info.addressLine2'));
    grid.appendChild(labeledInput('Hours — Short', m.info.hoursShort, 'info.hoursShort'));
    grid.appendChild(labeledInput('Hours — Days', m.info.hoursDays, 'info.hoursDays'));
    grid.appendChild(labeledInput('Hours — Time', m.info.hoursTime, 'info.hoursTime'));
    grid.appendChild(labeledInput('Hours — Closed Note', m.info.hoursClosed, 'info.hoursClosed'));
    body.appendChild(grid);
    parent.appendChild(sec);
  }

  // ----- HEADER EXTRAS -----
  {
    const { sec, body } = section('TV Menu Header');
    const grid = el('div', { class: 'info-grid' });
    grid.appendChild(labeledInput('Top Line', m.headerExtras.line1, 'headerExtras.line1'));
    grid.appendChild(labeledInput('Bottom Line', m.headerExtras.line2, 'headerExtras.line2'));
    body.appendChild(grid);
    parent.appendChild(sec);
  }

  // ----- SANDWICH CATEGORIES -----
  for (const key of ['italianGrinders', 'hotGrinders', 'coldGrinders', 'localFavorites', 'paninis']) {
    renderSandwichCategory(key, m.sandwiches[key], parent);
  }

  // ----- BREAKFAST -----
  {
    const { sec, body } = section(m.breakfast.title);
    m.breakfast.items.forEach((item, i) => {
      body.appendChild(renderItemRow(item, `breakfast.items.${i}`));
    });
    parent.appendChild(sec);
  }

  // ----- MAKE IT A MEAL -----
  {
    const { sec, body } = section(m.makeItAMeal.title, `Meal base: $${m.makeItAMeal.basePrice}`);
    body.appendChild(labeledInput('Meal Base Price', m.makeItAMeal.basePrice, 'makeItAMeal.basePrice', { price: true }));
    m.makeItAMeal.items.forEach((item, i) => {
      body.appendChild(renderItemRow(item, `makeItAMeal.items.${i}`, ['name', 'price']));
    });
    parent.appendChild(sec);
  }

  // ----- SIDES -----
  {
    const { sec, body } = section(m.sides.title, `All sides priced at $${m.sides.basePrice}`);
    body.appendChild(labeledInput('Base Price (all sides)', m.sides.basePrice, 'sides.basePrice', { price: true }));
    m.sides.items.forEach((item, i) => {
      body.appendChild(renderItemRow(item, `sides.items.${i}`, ['name']));
    });
    parent.appendChild(sec);
  }

  // ----- SOUP SPECIALS -----
  {
    const { sec, body } = section(m.soupSpecials.title, `All soup specials priced at $${m.soupSpecials.basePrice}`);
    body.appendChild(labeledInput('Base Price (all specials)', m.soupSpecials.basePrice, 'soupSpecials.basePrice', { price: true }));
    const order = [['1','Monday'], ['2','Tuesday'], ['3','Wednesday'], ['4','Thursday'], ['5','Friday']];
    for (const [key, label] of order) {
      const d = m.soupSpecials.byDay[key];
      if (!d) continue;
      const row = el('div', { class: 'day-row' });
      row.appendChild(el('div', { class: 'day-label', text: label }));
      row.appendChild(labeledInput('Sandwich', d.name, `soupSpecials.byDay.${key}.name`));
      row.appendChild(labeledInput('Soup / note', d.desc, `soupSpecials.byDay.${key}.desc`));
      body.appendChild(row);
    }
    parent.appendChild(sec);
  }

  // ----- EXTRAS -----
  {
    const { sec, body } = section(m.extras.title);
    m.extras.items.forEach((item, i) => {
      body.appendChild(renderItemRow(item, `extras.items.${i}`, ['name', 'price']));
    });
    parent.appendChild(sec);
  }

  // ----- CATERING -----
  {
    const { sec, body } = section('Catering');
    body.appendChild(labeledInput('Title', m.catering.title, 'catering.title'));
    body.appendChild(labeledInput('Headline', m.catering.headline, 'catering.headline'));
    body.appendChild(labeledInput('Description', m.catering.description, 'catering.description', { textarea: true }));
    body.appendChild(labeledInput('Price', m.catering.price, 'catering.price', { price: true }));
    parent.appendChild(sec);
  }
}

// Switch the active tab pane. Uses display:none on inactive panes so
// inputs in the other tab still exist in the DOM and are picked up by
// collectForm() when the user clicks Save Changes.
function applyActiveTab(tabId) {
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tabId);
  });
  document.querySelectorAll('.tab-pane').forEach(p => {
    p.classList.toggle('active', p.dataset.pane === tabId);
  });
  localStorage.setItem('bsg_active_tab', tabId);
  // Scroll to top so user lands at the start of the newly-shown pane
  window.scrollTo({ top: 0, behavior: 'instant' });
}

document.getElementById('adminTabs').addEventListener('click', (e) => {
  const btn = e.target.closest('.tab-btn');
  if (btn && btn.dataset.tab) applyActiveTab(btn.dataset.tab);
});

// ---------- ROTATION ----------

function renderRotationSection(parent) {
  const { sec, body } = section(
    'Rotating TV (TV #2)',
    'Slides loop in order. Use ↑ / ↓ to reorder. Tap Save Changes to push edits — TV #2 updates within ~90 seconds.'
  );

  const list = el('div', { class: 'slide-list' });
  body.appendChild(list);
  renderSlideList(list);

  // Add controls
  const addRow = el('div', { class: 'slide-add-row' });

  const imgInput = document.createElement('input');
  imgInput.type = 'file';
  imgInput.accept = 'image/*';
  imgInput.style.display = 'none';
  imgInput.onchange = () => handleUpload(imgInput.files[0], 'image').then(() => imgInput.value = '');

  const vidInput = document.createElement('input');
  vidInput.type = 'file';
  vidInput.accept = 'video/*';
  vidInput.style.display = 'none';
  vidInput.onchange = () => handleUpload(vidInput.files[0], 'video').then(() => vidInput.value = '');

  const imgBtn = el('button', { class: 'add-btn', text: '+ Image' });
  imgBtn.onclick = () => imgInput.click();
  const vidBtn = el('button', { class: 'add-btn', text: '+ Video' });
  vidBtn.onclick = () => vidInput.click();
  const txtBtn = el('button', { class: 'add-btn gold', text: '+ Text Slide' });
  txtBtn.onclick = () => showTextForm(body);

  const sectionBtn = el('button', { class: 'add-btn', text: '+ Menu Section' });
  sectionBtn.onclick = () => showMenuSectionPicker(body);

  addRow.appendChild(imgBtn);
  addRow.appendChild(vidBtn);
  addRow.appendChild(txtBtn);
  addRow.appendChild(sectionBtn);
  addRow.appendChild(imgInput);
  addRow.appendChild(vidInput);

  const progress = el('div', { class: 'upload-progress', attrs: { id: 'uploadProgress' } });
  body.appendChild(addRow);
  body.appendChild(progress);

  parent.appendChild(sec);
}

function renderSlideList(container) {
  container.innerHTML = '';
  const slides = playlist.slides || [];
  if (slides.length === 0) {
    container.appendChild(el('div', {
      style: 'padding: 20px; text-align: center; color: var(--text-muted); font-style: italic;',
      text: 'No slides yet — add an image, video, or text slide below.'
    }));
    return;
  }

  slides.forEach((slide, i) => container.appendChild(renderSlideCard(slide, i, slides.length)));
}

function renderSlideCard(slide, index, total) {
  const card = el('div', { class: 'slide-card' });

  // Thumbnail
  const thumb = el('div', { class: 'slide-thumb' });
  if (slide.type === 'image') {
    const img = document.createElement('img');
    img.src = '/tv/' + slide.src;
    thumb.appendChild(img);
  } else if (slide.type === 'video') {
    const v = document.createElement('video');
    v.src = '/tv/' + slide.src;
    v.muted = true;
    v.preload = 'metadata';
    thumb.appendChild(v);
  } else if (slide.type === 'text') {
    thumb.textContent = 'TEXT';
    thumb.style.background = 'var(--gold)';
    thumb.style.color = 'var(--brown-dark)';
  } else {
    thumb.textContent = 'HTML';
  }
  card.appendChild(thumb);

  // Meta + controls
  const meta = el('div', { class: 'slide-meta' });
  const line1Text = slide.type === 'text'
    ? (slide.title || '(untitled text)')
    : slide.src;
  meta.appendChild(el('div', { class: 'slide-meta-line1', text: `${(slide.type || '?').toUpperCase()} · ${line1Text}` }));

  const line2 = el('div', { class: 'slide-meta-line2' });

  // Duration (videos auto-advance, no editable duration)
  if (slide.type !== 'video') {
    const durLabel = el('label', { text: 'Show for ' });
    const dur = document.createElement('input');
    dur.type = 'number';
    dur.min = '1';
    dur.max = '120';
    dur.step = '1';
    dur.value = String(Math.round((slide.duration ?? (slide.type === 'image' ? 8000 : 10000)) / 1000));
    dur.setAttribute('inputmode', 'numeric');
    dur.onchange = () => {
      const secs = Math.max(1, Math.min(120, parseInt(dur.value, 10) || 8));
      slide.duration = secs * 1000;
    };
    durLabel.appendChild(dur);
    durLabel.appendChild(document.createTextNode(' sec'));
    line2.appendChild(durLabel);
  } else {
    line2.appendChild(el('span', { text: 'Auto-advance when video ends' }));
  }

  // Fit (image + video only)
  if (slide.type === 'image' || slide.type === 'video') {
    const fitLabel = el('label', { text: 'Fit ' });
    const fit = document.createElement('select');
    ['cover', 'contain'].forEach(v => {
      const o = document.createElement('option');
      o.value = v;
      o.textContent = v === 'cover' ? 'Fill (may crop)' : 'Letterbox (show all)';
      if ((slide.fit || 'cover') === v) o.selected = true;
      fit.appendChild(o);
    });
    fit.onchange = () => { slide.fit = fit.value; };
    fitLabel.appendChild(fit);
    line2.appendChild(fitLabel);
  }

  // Edit button for text slides
  if (slide.type === 'text') {
    const editBtn = el('button', { class: 'slide-btn', text: 'Edit text' });
    editBtn.onclick = () => showTextForm(document.querySelector('.slide-list').parentElement, index);
    line2.appendChild(editBtn);
  }

  meta.appendChild(line2);
  card.appendChild(meta);

  // Up/Down/Delete
  const actions = el('div', { class: 'slide-actions' });
  const upBtn = el('button', { class: 'slide-btn', text: '↑' });
  upBtn.disabled = index === 0;
  upBtn.onclick = () => moveSlide(index, -1);
  const downBtn = el('button', { class: 'slide-btn', text: '↓' });
  downBtn.disabled = index === total - 1;
  downBtn.onclick = () => moveSlide(index, 1);
  const delBtn = el('button', { class: 'slide-btn danger', text: 'Delete' });
  delBtn.onclick = () => deleteSlide(index);
  actions.appendChild(upBtn);
  actions.appendChild(downBtn);
  actions.appendChild(delBtn);
  card.appendChild(actions);

  return card;
}

function moveSlide(index, delta) {
  const slides = playlist.slides;
  const j = index + delta;
  if (j < 0 || j >= slides.length) return;
  [slides[index], slides[j]] = [slides[j], slides[index]];
  refreshSlideList();
}

function deleteSlide(index) {
  const slide = playlist.slides[index];
  const label = slide.type === 'text' ? (slide.title || 'this text slide') : slide.src;
  if (!confirm(`Remove "${label}" from rotation?\n\n(Takes effect when you tap Save Changes.)`)) return;
  playlist.slides.splice(index, 1);

  // Queue underlying file for deletion AFTER a successful Save.
  // We don't unlink now — if save fails or the user closes the tab, we'd
  // leave the live playlist pointing at a missing file.
  if ((slide.type === 'image' || slide.type === 'video') && slide.src && slide.src.startsWith('slides/')) {
    pendingFileDeletes.add(slide.src);
  }
  refreshSlideList();
}

async function flushPendingFileDeletes() {
  // Don't delete files that any remaining slide still references.
  const stillUsed = new Set(playlist.slides.map(s => s.src).filter(Boolean));
  for (const src of Array.from(pendingFileDeletes)) {
    if (stillUsed.has(src)) {
      pendingFileDeletes.delete(src);
      continue;
    }
    const filename = src.replace(/^slides\//, '');
    try {
      const res = await fetch('/api/slides/' + encodeURIComponent(filename), {
        method: 'DELETE',
        headers: { Authorization: `Basic ${getAuth()}` }
      });
      if (res.ok) pendingFileDeletes.delete(src);
    } catch (e) { console.warn('file delete failed', src, e); }
  }
}

function refreshSlideList() {
  const list = document.querySelector('.slide-list');
  if (list) renderSlideList(list);
}

async function handleUpload(file, type) {
  if (!file) return;
  const progress = document.getElementById('uploadProgress');

  // Videos are re-encoded server-side for smooth Pi playback. Set the right
  // expectation up front — the request can take a minute or more for a long
  // iPhone clip — so the user doesn't think it froze.
  const isVideo = type === 'video';
  progress.textContent = isVideo
    ? `Uploading & processing ${file.name} — this can take a minute, please don't close the page…`
    : `Uploading ${file.name}…`;

  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await fetch('/api/slides/upload', {
      method: 'POST',
      headers: { Authorization: `Basic ${getAuth()}` },
      body: fd
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    const out = await res.json();
    const slide = type === 'image'
      ? { type: 'image', src: out.src, duration: 8000, fit: 'cover' }
      : { type: 'video', src: out.src, fit: 'cover' };
    playlist.slides.push(slide);
    refreshSlideList();
    let msg = `✓ Added ${out.name} — don't forget to Save Changes`;
    if (out.warning) msg += ` (note: ${out.warning})`;
    progress.textContent = msg;
    setTimeout(() => { progress.textContent = ''; }, 8000);
  } catch (e) {
    progress.textContent = `✗ Upload failed: ${e.message}`;
  }
}

// Menu-section slide templates the user can drop into the rotation. Each
// is a pre-built HTML file in slides/ that fetches live data from menu.json.
// (breakfast.html and catering.html still exist on disk for historical
// playlist references but are intentionally NOT in the picker — the user
// doesn't want them in the daily rotation.)
const MENU_SECTION_TEMPLATES = [
  { src: 'slides/welcome.html',         label: 'Welcome / Our Story',  duration: 10000 },
  { src: 'slides/whats-a-grinder.html', label: "What's a Grinder?",    duration: 10000 },
  { src: 'slides/make-it-a-meal.html',  label: 'Make It A Meal',       duration: 10000 },
  { src: 'slides/sides.html',           label: 'Sides',                duration: 10000 },
  { src: 'slides/soup-specials.html',   label: 'Soup Specials',        duration: 12000 },
  { src: 'slides/order-ahead.html',     label: 'Order Ahead (phone)',  duration: 8000 },
  { src: 'slides/hours.html',           label: 'Hours',                duration: 8000 },
];

function showMenuSectionPicker(container) {
  // Remove any existing picker (so a second tap re-opens fresh, doesn't stack)
  const old = container.querySelector('.section-picker');
  if (old) { old.remove(); return; }

  const picker = el('div', { class: 'section-picker text-form' });
  picker.appendChild(el('div', {
    class: 'field-label',
    text: 'Choose a menu section to add to the rotation:'
  }));

  const grid = el('div', { style: 'display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-top: 8px;' });
  for (const tmpl of MENU_SECTION_TEMPLATES) {
    const btn = el('button', { class: 'add-btn', text: tmpl.label });
    btn.onclick = () => {
      playlist.slides.push({ type: 'html', src: tmpl.src, duration: tmpl.duration });
      picker.remove();
      refreshSlideList();
    };
    grid.appendChild(btn);
  }
  picker.appendChild(grid);

  const cancelRow = el('div', { style: 'margin-top: 10px;' });
  const cancelBtn = el('button', { class: 'slide-btn', text: 'Cancel' });
  cancelBtn.onclick = () => picker.remove();
  cancelRow.appendChild(cancelBtn);
  picker.appendChild(cancelRow);

  container.appendChild(picker);
}

function showTextForm(container, editIndex = null) {
  // Remove any existing form
  const old = container.querySelector('.text-form');
  if (old) old.remove();

  const editing = editIndex !== null ? playlist.slides[editIndex] : null;
  const form = el('div', { class: 'text-form' });

  const fields = [
    ['heading', 'Top banner (optional)', editing?.heading || '', false],
    ['title', 'Big headline', editing?.title || '', false],
    ['body', 'Body text (optional)', editing?.body || '', true],
    ['price', 'Big price (optional, e.g. $9.99)', editing?.price || '', false],
    ['subtitle', 'Subtitle / call to action (optional)', editing?.subtitle || '', false],
    ['footer', 'Footer line (optional)', editing?.footer || '', false],
  ];

  const inputs = {};
  for (const [key, label, val, ta] of fields) {
    const wrap = el('div', { class: 'field' });
    wrap.appendChild(el('label', { class: 'field-label', text: label }));
    const input = document.createElement(ta ? 'textarea' : 'input');
    if (!ta) input.type = 'text';
    if (ta) input.rows = 3;
    input.value = val;
    input.setAttribute('autocomplete', 'off');
    inputs[key] = input;
    wrap.appendChild(input);
    form.appendChild(wrap);
  }

  const dur = document.createElement('input');
  dur.type = 'number';
  dur.min = '1';
  dur.max = '120';
  dur.value = String(Math.round((editing?.duration ?? 10000) / 1000));
  const durWrap = el('div', { class: 'field' });
  durWrap.appendChild(el('label', { class: 'field-label', text: 'Show for (seconds)' }));
  durWrap.appendChild(dur);
  form.appendChild(durWrap);

  const btnRow = el('div', { style: 'display: flex; gap: 10px; margin-top: 8px;' });
  const okBtn = el('button', { class: 'add-btn', text: editing ? 'Update slide' : 'Add slide' });
  const cancelBtn = el('button', { class: 'slide-btn', text: 'Cancel' });
  okBtn.onclick = () => {
    if (!inputs.title.value.trim()) {
      alert('Headline is required');
      return;
    }
    const slide = {
      type: 'text',
      title: inputs.title.value.trim(),
      duration: Math.max(1, Math.min(120, parseInt(dur.value, 10) || 10)) * 1000,
    };
    for (const [key] of fields) {
      if (key === 'title') continue;
      const v = inputs[key].value.trim();
      if (v) slide[key] = v;
    }
    if (editing) playlist.slides[editIndex] = slide;
    else playlist.slides.push(slide);
    form.remove();
    refreshSlideList();
  };
  cancelBtn.onclick = () => form.remove();
  btnRow.appendChild(okBtn);
  btnRow.appendChild(cancelBtn);
  form.appendChild(btnRow);

  container.appendChild(form);
  inputs.title.focus();
}

// ---------- COLLECT + SAVE ----------

function setByPath(obj, path, value) {
  const parts = path.split('.');
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    const p = parts[i];
    if (cur[p] === undefined) {
      cur[p] = isNaN(Number(parts[i + 1])) ? {} : [];
    }
    cur = cur[p];
  }
  cur[parts[parts.length - 1]] = value;
}

function collectForm() {
  const out = JSON.parse(JSON.stringify(menu));
  const inputs = bodyEl.querySelectorAll('input[data-path], textarea[data-path]');
  inputs.forEach(i => setByPath(out, i.dataset.path, i.value));
  return out;
}

async function postJson(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Basic ${getAuth()}` },
    body: JSON.stringify(body, null, 2)
  });
  return res;
}

async function save() {
  saveBtn.disabled = true;
  saveStatus.className = 'save-status';
  saveStatus.textContent = '';
  const updated = collectForm();
  try {
    // 1) Save menu first.
    const menuRes = await postJson('/api/menu', updated);
    if (menuRes.status === 401) { clearAuth(); showLogin(); return; }
    if (!menuRes.ok) {
      const err = await menuRes.json().catch(() => ({}));
      throw new Error('Menu not saved: ' + (err.error || menuRes.status));
    }
    menu = updated;

    // 2) Then save playlist. Surface a precise error if this half fails so
    //    uncle knows the menu changes did stick but the rotation didn't.
    const playRes = await postJson('/api/playlist', playlist);
    if (playRes.status === 401) { clearAuth(); showLogin(); return; }
    if (!playRes.ok) {
      const err = await playRes.json().catch(() => ({}));
      saveStatus.className = 'save-status error';
      saveStatus.textContent = '✗ Menu saved, but rotation save failed: ' + (err.error || playRes.status);
      return;
    }

    // 3) Both committed — now safe to unlink any deleted upload files.
    await flushPendingFileDeletes();

    saveStatus.className = 'save-status success';
    saveStatus.textContent = '✓ Saved — TVs updating';
    setTimeout(() => { saveStatus.className = 'save-status'; saveStatus.textContent = ''; }, 4000);
  } catch (e) {
    console.error(e);
    saveStatus.className = 'save-status error';
    saveStatus.textContent = '✗ ' + (e.message || 'Save failed — try again');
  } finally {
    saveBtn.disabled = false;
  }
}

saveBtn.onclick = save;

// Logout
document.getElementById('logoutBtn').onclick = () => {
  if (!confirm('Sign out? You will need to re-enter the admin password.')) return;
  clearAuth();
  location.reload();
};

// Install hint — shown on iOS Safari when NOT opened from home screen.
// Silenced forever once user taps ×.
(function showInstallHint() {
  const hint = document.getElementById('installHint');
  if (!hint) return;
  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent);
  // navigator.standalone is iOS-specific: true when running from home screen
  const inStandalone = window.navigator.standalone === true || window.matchMedia('(display-mode: standalone)').matches;
  const dismissed = localStorage.getItem('bsg_install_hint_dismissed') === '1';
  if (isIOS && !inStandalone && !dismissed) {
    hint.style.display = 'block';
  }
  document.getElementById('installHintClose').onclick = () => {
    hint.style.display = 'none';
    localStorage.setItem('bsg_install_hint_dismissed', '1');
  };
})();

tryLoadMenu();
