/* Website admin — edits the public bankstgrinders.com files via the Pi.
   Save commits locally; Publish pushes to GitHub (Netlify auto-deploys). */

const loginEl = document.getElementById('login');
const adminEl = document.getElementById('admin');
const bodyEl = document.getElementById('adminBody');
const saveBtn = document.getElementById('saveBtn');
const publishBtn = document.getElementById('publishBtn');
const saveStatus = document.getElementById('saveStatus');

let hours = null;             // [{ name, open, close, closed }, ...]
let announcement = { message: '', expires: '' };
let unpublishedCount = 0;
let publishable = true;       // false when the Pi has no GitHub upstream configured

// ---------- AUTH (matches tv/admin.js) ----------

function getAuth() { return localStorage.getItem('bsg_auth'); }
function setAuth(user, pass) {
  const token = btoa(`${user}:${pass}`);
  localStorage.setItem('bsg_auth', token);
  return token;
}
function clearAuth() { localStorage.removeItem('bsg_auth'); }

function showLogin() {
  loginEl.style.display = 'block';
  adminEl.style.display = 'none';
  document.getElementById('loginUser').focus();
}
function showAdmin() {
  loginEl.style.display = 'none';
  adminEl.style.display = 'block';
}

document.getElementById('loginBtn').onclick = (e) => { e.preventDefault(); doLogin(); };
// Wire form submit too — Enter from a username field submits the form rather
// than firing the button's click, so without this, hitting Return wouldn't log in.
const loginForm = document.querySelector('#login form');
if (loginForm) loginForm.addEventListener('submit', (e) => { e.preventDefault(); doLogin(); });
document.getElementById('logoutBtn').onclick = () => { clearAuth(); showLogin(); };

function applyLoaded(data) {
  hours = (data.hours && data.hours.days) || [];
  announcement = data.announcement
    ? { message: data.announcement.message || '', expires: data.announcement.expires || '' }
    : { message: '', expires: '' };
}

async function doLogin() {
  const user = document.getElementById('loginUser').value.trim();
  const pass = document.getElementById('loginPass').value;
  const errEl = document.getElementById('loginError');
  errEl.style.display = 'none';
  if (!user || !pass) { errEl.textContent = 'Enter a username and password'; errEl.style.display = 'block'; return; }
  setAuth(user, pass);
  try {
    const res = await fetch('/api/site/state', { headers: { Authorization: `Basic ${getAuth()}` } });
    if (res.status === 401) {
      clearAuth();
      errEl.textContent = 'Wrong username or password';
      errEl.style.display = 'block';
      return;
    }
    if (!res.ok) {
      const txt = await res.text();
      throw new Error(txt || 'Error: ' + res.status);
    }
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'failed to load site state');
    applyLoaded(data);
    showAdmin();
    await refreshStatus();
    render();
  } catch (e) {
    errEl.textContent = String(e.message || e);
    errEl.style.display = 'block';
    clearAuth();
  }
}

async function loadInitial() {
  if (!getAuth()) { showLogin(); return; }
  try {
    const res = await fetch('/api/site/state', { headers: { Authorization: `Basic ${getAuth()}` } });
    if (res.status === 401) { clearAuth(); showLogin(); return; }
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'failed to load');
    applyLoaded(data);
    showAdmin();
    await refreshStatus();
    render();
  } catch (e) {
    console.error(e);
    showLogin();
  }
}

// ---------- STATUS ----------

function applyState(data) {
  unpublishedCount = data && data.unpublished != null ? Number(data.unpublished) || 0 : 0;
  publishable = !(data && data.publishable === false);
}

async function refreshStatus() {
  try {
    const res = await fetch('/api/site/status', { headers: { Authorization: `Basic ${getAuth()}` } });
    if (res.status === 401) { clearAuth(); showLogin(); return; }
    if (!res.ok) return;
    const data = await res.json();
    if (data.ok) applyState(data);
  } catch (e) {
    // status is informational; failures are ignored
  }
  paintPublishBtn();
  paintBanner();
}

function paintPublishBtn() {
  if (!publishable) {
    publishBtn.classList.add('idle');
    publishBtn.textContent = 'Publish unavailable';
    publishBtn.disabled = true;
    return;
  }
  publishBtn.disabled = false;
  if (unpublishedCount > 0) {
    publishBtn.classList.remove('idle');
    publishBtn.textContent = `Publish to Site (${unpublishedCount})`;
  } else {
    publishBtn.classList.add('idle');
    publishBtn.textContent = 'Publish to Site';
  }
}

function paintBanner() {
  const banner = document.getElementById('publishBanner');
  if (!banner) return;
  if (!publishable) {
    banner.className = 'publish-banner dirty';
    banner.innerHTML = `<span><b>GitHub publishing isn't set up on this Pi.</b> Saves still work locally, but publishing to bankstgrinders.com requires a one-time setup.</span>
      <span style="font-style: italic; color: var(--text-muted);">Run <code>bash tv/setup-github-auth.sh</code> on the Pi.</span>`;
    return;
  }
  if (unpublishedCount > 0) {
    banner.className = 'publish-banner dirty';
    banner.innerHTML = `<span><b>${unpublishedCount} change${unpublishedCount === 1 ? '' : 's'}</b> saved on this Pi but not yet on the live site.</span>
      <span style="font-style: italic; color: var(--text-muted);">Click <b>Publish to Site</b> when you're ready.</span>`;
  } else {
    banner.className = 'publish-banner clean';
    banner.innerHTML = `<span><b>Live site is up to date</b> with everything saved here.</span>
      <span style="font-style: italic;">Edit, Save, then Publish.</span>`;
  }
}

// ---------- RENDER ----------

const DAY_NAMES = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];

function render() {
  bodyEl.innerHTML = '';

  // Status banner
  const banner = document.createElement('div');
  banner.id = 'publishBanner';
  banner.className = 'publish-banner clean';
  bodyEl.appendChild(banner);
  paintBanner();

  renderAnnouncementPanel();
  renderHoursPanel();
}

function renderAnnouncementPanel() {
  const panel = document.createElement('div');
  panel.className = 'panel';
  panel.innerHTML = `
    <h2>Announcement</h2>
    <div class="panel-sub">Shows as a banner near the top of the homepage. Leave the message blank to hide it.</div>
    <textarea id="annMsg" class="text-input" rows="3" maxlength="500"
      placeholder="e.g. Closing early tomorrow at 3pm.&#10;Reopens Saturday at 9.">${escAttr(announcement.message)}</textarea>
    <div class="ann-row">
      <label class="ann-expiry-label">
        <span class="field-label-inline">Auto-hide on</span>
        <input id="annExpires" class="text-input ann-expiry-input" type="date" value="${escAttr(announcement.expires)}" />
      </label>
      <button type="button" id="annClear" class="ann-clear-btn">Clear announcement</button>
    </div>
    <div class="hint-line">The banner hides itself automatically after the date you pick — leave blank for &ldquo;until I clear it.&rdquo;</div>
  `;
  bodyEl.appendChild(panel);

  const msgEl = panel.querySelector('#annMsg');
  const expiresEl = panel.querySelector('#annExpires');
  const clearBtn = panel.querySelector('#annClear');
  msgEl.addEventListener('input', () => { announcement.message = msgEl.value; });
  expiresEl.addEventListener('input', () => { announcement.expires = expiresEl.value; });
  clearBtn.addEventListener('click', () => {
    announcement.message = '';
    announcement.expires = '';
    msgEl.value = '';
    expiresEl.value = '';
    msgEl.focus();
  });
}

function renderHoursPanel() {
  const panel = document.createElement('div');
  panel.className = 'panel';
  panel.innerHTML = `
    <h2>Hours</h2>
    <div class="panel-sub">Shown in the dropdown on the homepage. Use times like <i>9:00 AM</i> and <i>6:00 PM</i>.</div>
    <div id="hoursRows"></div>
    <div class="hint-line">Tip: the &ldquo;Open / Closed&rdquo; indicator reads from these times directly — change a day, the indicator updates with it.</div>
  `;
  bodyEl.appendChild(panel);

  const rowsEl = panel.querySelector('#hoursRows');
  hours.forEach((day, idx) => {
    const row = document.createElement('div');
    row.className = 'hours-row' + (day.closed ? ' closed' : '');
    row.innerHTML = `
      <div class="day-label">${day.name || DAY_NAMES[idx]}</div>
      <input class="text-input" type="text" placeholder="9:00 AM" value="${escAttr(day.open)}" />
      <input class="text-input" type="text" placeholder="6:00 PM" value="${escAttr(day.close)}" />
      <label class="closed-toggle">
        <input type="checkbox" ${day.closed ? 'checked' : ''} />
        Closed
      </label>
    `;
    const [openInput, closeInput] = row.querySelectorAll('.text-input');
    const closedBox = row.querySelector('.closed-toggle input');
    openInput.addEventListener('input', () => { hours[idx].open = openInput.value; });
    closeInput.addEventListener('input', () => { hours[idx].close = closeInput.value; });
    closedBox.addEventListener('change', () => {
      hours[idx].closed = closedBox.checked;
      row.classList.toggle('closed', closedBox.checked);
    });
    rowsEl.appendChild(row);
  });
}

function escAttr(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

// ---------- SAVE ----------

saveBtn.onclick = async () => {
  // Validate locally first for friendly errors
  for (const d of hours) {
    if (!d.closed && (!d.open.trim() || !d.close.trim())) {
      setStatus(`${d.name}: enter open & close times, or check Closed.`, 'err');
      return;
    }
  }
  saveBtn.disabled = true;
  setStatus('Saving…', '');
  try {
    const res = await fetch('/api/site/save', {
      method: 'POST',
      headers: {
        Authorization: `Basic ${getAuth()}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        hours: { days: hours },
        announcement: {
          message: (announcement.message || '').trim(),
          expires: (announcement.expires || '').trim(),
        },
      }),
    });
    if (res.status === 401) { clearAuth(); showLogin(); return; }
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      setStatus(data.error || `Save failed (${res.status})`, 'err');
      return;
    }
    if (data.committed) {
      setStatus('Saved. Click Publish to push it live.', 'ok');
    } else {
      setStatus('No changes to save.', 'ok');
    }
    applyState(data);
    paintPublishBtn();
    paintBanner();
  } catch (e) {
    setStatus(String(e.message || e), 'err');
  } finally {
    saveBtn.disabled = false;
  }
};

// ---------- PUBLISH ----------

publishBtn.onclick = async () => {
  if (unpublishedCount === 0) {
    setStatus('Nothing new to publish.', 'ok');
    return;
  }
  if (!confirm(`Publish ${unpublishedCount} change${unpublishedCount === 1 ? '' : 's'} to bankstgrinders.com? This will be live in about 30 seconds.`)) return;

  publishBtn.disabled = true;
  saveBtn.disabled = true;
  setStatus('Publishing…', '');
  try {
    const res = await fetch('/api/site/publish', {
      method: 'POST',
      headers: { Authorization: `Basic ${getAuth()}` },
    });
    if (res.status === 401) { clearAuth(); showLogin(); return; }
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      setStatus(data.error || `Publish failed (${res.status})`, 'err');
      return;
    }
    setStatus(`Published ${data.pushed} change${data.pushed === 1 ? '' : 's'}. Live in ~30 sec.`, 'ok');
    applyState(data);
    paintPublishBtn();
    paintBanner();
  } catch (e) {
    setStatus(String(e.message || e), 'err');
  } finally {
    publishBtn.disabled = false;
    saveBtn.disabled = false;
  }
};

function setStatus(msg, kind) {
  saveStatus.textContent = msg;
  saveStatus.className = 'save-status' + (kind ? ' ' + kind : '');
  if (kind === 'ok') {
    setTimeout(() => {
      if (saveStatus.textContent === msg) {
        saveStatus.textContent = '';
        saveStatus.className = 'save-status';
      }
    }, 6000);
  }
}

loadInitial();
