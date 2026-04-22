/* Admin form for editing menu.json.
   - Authenticates via HTTP Basic against the Flask API.
   - Stores credentials in sessionStorage so reloads don't re-prompt in the same tab.
   - Renders a form from menu.json, collects values on save, POSTs the updated JSON. */

const loginEl = document.getElementById('login');
const adminEl = document.getElementById('admin');
const bodyEl = document.getElementById('adminBody');
const saveBtn = document.getElementById('saveBtn');
const saveStatus = document.getElementById('saveStatus');

let menu = null;

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
    const res = await fetch('/api/menu', { headers: { Authorization: `Basic ${auth}` } });
    if (res.status === 401) { clearAuth(); showLogin(); return; }
    if (!res.ok) throw new Error('Failed: ' + res.status);
    menu = await res.json();
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

function renderItemRow(item, basePath, fields = ['name', 'desc', 'price']) {
  const row = el('div', { class: fields.length === 2 ? 'item-row compact' : 'item-row' });
  if (fields.includes('name'))  row.appendChild(labeledInput('Name',  item.name,  `${basePath}.name`));
  if (fields.includes('desc'))  row.appendChild(labeledInput('Description', item.desc, `${basePath}.desc`));
  if (fields.includes('price')) row.appendChild(labeledInput('Price', item.price, `${basePath}.price`, { price: true }));
  return row;
}

function renderSandwichCategory(key, data, parent) {
  const { sec, body } = section(data.title, data.note);
  data.items.forEach((item, i) => {
    body.appendChild(renderItemRow(item, `sandwiches.${key}.items.${i}`));
  });
  parent.appendChild(sec);
}

function renderForm(m) {
  bodyEl.innerHTML = '';

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
    bodyEl.appendChild(sec);
  }

  // ----- HEADER EXTRAS -----
  {
    const { sec, body } = section('TV Menu Header');
    const grid = el('div', { class: 'info-grid' });
    grid.appendChild(labeledInput('Top Line', m.headerExtras.line1, 'headerExtras.line1'));
    grid.appendChild(labeledInput('Bottom Line', m.headerExtras.line2, 'headerExtras.line2'));
    body.appendChild(grid);
    bodyEl.appendChild(sec);
  }

  // ----- SANDWICH CATEGORIES -----
  for (const key of ['italianGrinders', 'hotGrinders', 'coldGrinders', 'localFavorites', 'paninis']) {
    renderSandwichCategory(key, m.sandwiches[key], bodyEl);
  }

  // ----- BREAKFAST -----
  {
    const { sec, body } = section(m.breakfast.title);
    m.breakfast.items.forEach((item, i) => {
      body.appendChild(renderItemRow(item, `breakfast.items.${i}`));
    });
    bodyEl.appendChild(sec);
  }

  // ----- MAKE IT A MEAL -----
  {
    const { sec, body } = section(m.makeItAMeal.title, `Meal base: $${m.makeItAMeal.basePrice}`);
    body.appendChild(labeledInput('Meal Base Price', m.makeItAMeal.basePrice, 'makeItAMeal.basePrice', { price: true }));
    m.makeItAMeal.items.forEach((item, i) => {
      body.appendChild(renderItemRow(item, `makeItAMeal.items.${i}`, ['name', 'price']));
    });
    bodyEl.appendChild(sec);
  }

  // ----- SIDES -----
  {
    const { sec, body } = section(m.sides.title, `All sides priced at $${m.sides.basePrice}`);
    body.appendChild(labeledInput('Base Price (all sides)', m.sides.basePrice, 'sides.basePrice', { price: true }));
    m.sides.items.forEach((item, i) => {
      body.appendChild(renderItemRow(item, `sides.items.${i}`, ['name']));
    });
    bodyEl.appendChild(sec);
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
    bodyEl.appendChild(sec);
  }

  // ----- EXTRAS -----
  {
    const { sec, body } = section(m.extras.title);
    m.extras.items.forEach((item, i) => {
      body.appendChild(renderItemRow(item, `extras.items.${i}`, ['name', 'price']));
    });
    bodyEl.appendChild(sec);
  }

  // ----- CATERING -----
  {
    const { sec, body } = section('Catering');
    body.appendChild(labeledInput('Title', m.catering.title, 'catering.title'));
    body.appendChild(labeledInput('Headline', m.catering.headline, 'catering.headline'));
    body.appendChild(labeledInput('Description', m.catering.description, 'catering.description', { textarea: true }));
    body.appendChild(labeledInput('Price', m.catering.price, 'catering.price', { price: true }));
    bodyEl.appendChild(sec);
  }
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

async function save() {
  saveBtn.disabled = true;
  saveStatus.className = 'save-status';
  saveStatus.textContent = '';
  const updated = collectForm();
  try {
    const res = await fetch('/api/menu', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Basic ${getAuth()}`
      },
      body: JSON.stringify(updated, null, 2)
    });
    if (res.status === 401) {
      clearAuth();
      showLogin();
      return;
    }
    if (!res.ok) throw new Error('Save failed: ' + res.status);
    menu = updated;
    saveStatus.className = 'save-status success';
    saveStatus.textContent = '✓ Saved — TVs updating';
    setTimeout(() => { saveStatus.className = 'save-status'; saveStatus.textContent = ''; }, 4000);
  } catch (e) {
    console.error(e);
    saveStatus.className = 'save-status error';
    saveStatus.textContent = '✗ Save failed — try again';
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
