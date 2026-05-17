const $ = (id) => document.getElementById(id);

const state = {
  authenticated: false,
  user: "",
  isAdmin: false,
  totpRequired: false,
  activeId: null,
  sessions: [],
  streaming: false,
};

const els = {
  sessionList: $("session-list"),
  newSession: $("new-session"),
  account: $("set-key"),
  usersBtn: $("users-btn"),
  passkeysBtn: $("passkeys-btn"),
  refresh: $("refresh"),
  stopSession: $("stop-session"),
  activeTitle: $("active-title"),
  activeMeta: $("active-meta"),
  messages: $("messages"),
  promptForm: $("prompt-form"),
  promptInput: $("prompt-input"),
  sendBtn: $("send-btn"),
  newDialog: $("new-dialog"),
  loginDialog: $("key-dialog"),
  loginForm: $("login-form"),
  authError: $("auth-error"),
  usersDialog: $("users-dialog"),
  usersList: $("users-list"),
  userForm: $("user-form"),
  userResult: $("user-result"),
  passkeyLogin: $("passkey-login"),
  passkeysDialog: $("passkeys-dialog"),
  passkeysList: $("passkeys-list"),
  passkeyResult: $("passkey-result"),
  passkeyRegister: $("passkey-register"),
};

function authHeaders() {
  return {};
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
      ...(opts.headers || {}),
    },
  });
  if (res.status === 401) {
    openLoginDialog();
    throw new Error("login required");
  }
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res;
}

function setAuth(data) {
  state.authenticated = Boolean(data.authenticated);
  state.user = data.user || "";
  state.isAdmin = Boolean(data.is_admin);
  state.totpRequired = Boolean(data.totp_required);
  els.account.textContent = state.authenticated ? `Logout ${state.user}` : "Login";
  els.usersBtn.hidden = !state.isAdmin;
  els.passkeysBtn.hidden = !state.authenticated;
}

async function loadAuthStatus() {
  const res = await fetch("/auth/status", { credentials: "same-origin" });
  if (!res.ok) {
    setAuth({ authenticated: false });
    return;
  }
  setAuth(await res.json());
}

async function loadSessions() {
  if (!state.authenticated) return;
  try {
    const res = await api("/sessions");
    const { sessions } = await res.json();
    state.sessions = sessions;
    renderSessions();
  } catch (e) {
    console.warn(e);
  }
}

function renderSessions() {
  els.sessionList.innerHTML = "";
  if (state.sessions.length === 0) {
    const empty = document.createElement("div");
    empty.className = "muted";
    empty.style.padding = "12px";
    empty.textContent = "No active sessions.";
    els.sessionList.append(empty);
    return;
  }
  for (const s of state.sessions) {
    const div = document.createElement("div");
    div.className = "session-item" + (s.id === state.activeId ? " active" : "");
    const modelLine = s.model ? `<span class="sdir">${s.model}</span>` : "";
    div.innerHTML = `<span class="sid">${s.id}</span><span class="sdir">${s.cwd}</span>${modelLine}`;
    div.onclick = () => selectSession(s);
    els.sessionList.append(div);
  }
}

function selectSession(s) {
  state.activeId = s.id;
  els.activeTitle.textContent = `Session ${s.id}`;
  const modelPart = s.model ? ` · model: ${s.model}` : "";
  els.activeMeta.textContent = `cwd: ${s.cwd}${modelPart}`;
  els.stopSession.hidden = false;
  els.promptInput.disabled = false;
  els.sendBtn.disabled = false;
  if (els.messages.querySelector(".empty")) {
    els.messages.innerHTML = "";
  }
  renderSessions();
  els.promptInput.focus();
}

function clearActive() {
  state.activeId = null;
  els.activeTitle.textContent = "No session selected";
  els.activeMeta.textContent = "";
  els.stopSession.hidden = true;
  els.promptInput.disabled = true;
  els.sendBtn.disabled = true;
  els.messages.innerHTML = '<div class="empty">Create or pick a session to start chatting.</div>';
}

function addMsg(kind, content, opts = {}) {
  const div = document.createElement("div");
  div.className = `msg ${kind}`;
  if (kind === "tool") {
    const safeInput = JSON.stringify(opts.input ?? {}, null, 2);
    div.innerHTML = `<details><summary><span class="tool-name">${escapeHtml(opts.name || "tool")}</span> ▾</summary><pre>${escapeHtml(safeInput)}</pre></details>`;
  } else {
    div.textContent = content;
  }
  els.messages.append(div);
  els.messages.scrollTop = els.messages.scrollHeight;
  return div;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function sendPrompt(prompt) {
  if (!state.activeId || state.streaming) return;
  state.streaming = true;
  els.sendBtn.disabled = true;
  els.promptInput.disabled = true;
  addMsg("user", prompt);

  try {
    const res = await api(`/sessions/${state.activeId}/query`, {
      method: "POST",
      body: JSON.stringify({ prompt }),
    });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let ev;
        try { ev = JSON.parse(line); } catch { continue; }
        handleEvent(ev);
      }
    }
  } catch (e) {
    addMsg("error", `Error: ${e.message}`);
  } finally {
    state.streaming = false;
    els.sendBtn.disabled = false;
    els.promptInput.disabled = false;
    els.promptInput.focus();
  }
}

function handleEvent(ev) {
  if (ev.type === "text") {
    addMsg("assistant", ev.content);
  } else if (ev.type === "tool") {
    addMsg("tool", null, { name: ev.name, input: ev.input });
  } else if (ev.type === "done") {
    const parts = [];
    if (ev.turns != null) parts.push(`${ev.turns} turn${ev.turns === 1 ? "" : "s"}`);
    if (ev.cost_usd != null) parts.push(`$${ev.cost_usd.toFixed(4)}`);
    addMsg("done", parts.length ? `— ${parts.join(" · ")} —` : "— done —");
  }
}

async function createSession(payload) {
  const res = await api("/sessions", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  await loadSessions();
  const fresh = state.sessions.find((s) => s.id === data.session_id);
  if (fresh) selectSession(fresh);
}

async function stopActiveSession() {
  if (!state.activeId) return;
  if (!confirm(`Stop session ${state.activeId}?`)) return;
  await api(`/sessions/${state.activeId}`, { method: "DELETE" });
  clearActive();
  await loadSessions();
}

/* dialogs */
function openLoginDialog() {
  els.authError.textContent = "";
  els.loginDialog.querySelector('input[name="username"]').value = state.user || "";
  els.loginDialog.querySelector('input[name="password"]').value = "";
  const totpLabel = els.loginDialog.querySelector("[data-totp]");
  totpLabel.hidden = !state.totpRequired;
  els.loginDialog.querySelector('input[name="totp"]').value = "";
  if (!els.loginDialog.open) els.loginDialog.showModal();
}

async function loginFromDialog() {
  const fd = new FormData(els.loginForm);
  els.authError.textContent = "";
  try {
    const res = await fetch("/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: (fd.get("username") || "").toString().trim(),
        password: (fd.get("password") || "").toString(),
        totp: (fd.get("totp") || "").toString().trim() || null,
      }),
    });
    if (!res.ok) throw new Error("Invalid login");
    setAuth(await res.json());
    els.loginDialog.close();
    await loadSessions();
  } catch (e) {
    els.authError.textContent = e.message;
  }
}

async function logout() {
  await fetch("/auth/logout", { method: "POST", credentials: "same-origin" });
  setAuth({ authenticated: false });
  state.sessions = [];
  renderSessions();
  clearActive();
  openLoginDialog();
}

els.loginForm.addEventListener("submit", (e) => {
  e.preventDefault();
  loginFromDialog();
});

function b64ToBuf(value) {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - normalized.length % 4) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

function bufToB64(value) {
  const bytes = new Uint8Array(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function prepareCredentialOptions(options) {
  options.challenge = b64ToBuf(options.challenge);
  if (options.user?.id) options.user.id = b64ToBuf(options.user.id);
  for (const cred of options.excludeCredentials || []) cred.id = b64ToBuf(cred.id);
  for (const cred of options.allowCredentials || []) cred.id = b64ToBuf(cred.id);
  return options;
}

function credentialToJson(credential) {
  const response = {};
  for (const key of Object.keys(credential.response)) {
    const value = credential.response[key];
    if (value instanceof ArrayBuffer) response[key] = bufToB64(value);
  }
  return {
    id: credential.id,
    rawId: bufToB64(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment,
    response,
  };
}

async function loginWithPasskey() {
  if (!window.PublicKeyCredential) {
    els.authError.textContent = "Passkeys are not supported by this browser";
    return;
  }
  const username = els.loginDialog.querySelector('input[name="username"]').value.trim() || null;
  els.authError.textContent = "";
  try {
    const optRes = await fetch("/auth/passkeys/login/options", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username }),
    });
    if (!optRes.ok) throw new Error("Passkey login is not available for this user");
    const data = await optRes.json();
    const credential = await navigator.credentials.get({ publicKey: prepareCredentialOptions(data.options) });
    const verifyRes = await fetch("/auth/passkeys/login/verify", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ challenge_id: data.challenge_id, credential: credentialToJson(credential) }),
    });
    if (!verifyRes.ok) throw new Error("Passkey login failed");
    setAuth(await verifyRes.json());
    els.loginDialog.close();
    await loadSessions();
  } catch (e) {
    els.authError.textContent = e.message;
  }
}

async function registerPasskey() {
  if (!window.PublicKeyCredential) {
    showPasskeyResult("Passkeys are not supported by this browser");
    return;
  }
  try {
    const data = await (await api("/auth/passkeys/register/options", { method: "POST", body: "{}" })).json();
    const credential = await navigator.credentials.create({ publicKey: prepareCredentialOptions(data.options) });
    await api("/auth/passkeys/register/verify", {
      method: "POST",
      body: JSON.stringify({ challenge_id: data.challenge_id, credential: credentialToJson(credential) }),
    });
    showPasskeyResult("Passkey added");
    await loadPasskeys();
  } catch (e) {
    showPasskeyResult(e.message);
  }
}

async function loadPasskeys() {
  const { passkeys } = await (await api("/auth/passkeys")).json();
  els.passkeysList.innerHTML = "";
  if (passkeys.length === 0) {
    const empty = document.createElement("div");
    empty.className = "muted";
    empty.textContent = "No passkeys enrolled.";
    els.passkeysList.append(empty);
    return;
  }
  for (const passkey of passkeys) {
    const row = document.createElement("div");
    row.className = "user-row";
    row.innerHTML = `
      <div>
        <strong>${escapeHtml(passkey.credential_id.slice(0, 18))}...</strong>
        <span>${escapeHtml(passkey.device_type || "passkey")} · ${passkey.backed_up ? "backed up" : "not backed up"}</span>
      </div>
      <div class="user-actions">
        <button type="button">Delete</button>
      </div>
    `;
    row.querySelector("button").onclick = async () => {
      await api(`/auth/passkeys/${encodeURIComponent(passkey.credential_id)}`, { method: "DELETE" });
      await loadPasskeys();
    };
    els.passkeysList.append(row);
  }
}

function showPasskeyResult(text) {
  els.passkeyResult.textContent = text;
}

els.passkeyLogin.onclick = loginWithPasskey;
els.passkeyRegister.onclick = registerPasskey;

async function loadUsers() {
  const res = await api("/auth/users");
  const { users } = await res.json();
  els.usersList.innerHTML = "";
  for (const user of users) {
    const row = document.createElement("div");
    row.className = "user-row";
    const flags = [
      user.is_admin ? "admin" : "user",
      user.totp_enabled ? "2FA" : "no 2FA",
      user.disabled ? "disabled" : "active",
    ].join(" · ");
    row.innerHTML = `
      <div>
        <strong>${escapeHtml(user.username)}</strong>
        <span>${escapeHtml(flags)}</span>
      </div>
      <div class="user-actions">
        <button type="button" data-action="reset-totp">Reset 2FA</button>
        <button type="button" data-action="disable">${user.disabled ? "Enable" : "Disable"}</button>
      </div>
    `;
    row.querySelector('[data-action="reset-totp"]').onclick = async () => {
      const data = await updateUser(user.username, { reset_totp: true });
      showUserResult(data.totp_secret ? `2FA secret for ${user.username}: ${data.totp_secret}` : "2FA reset");
      await loadUsers();
    };
    row.querySelector('[data-action="disable"]').onclick = async () => {
      await updateUser(user.username, { disabled: !user.disabled });
      await loadUsers();
    };
    els.usersList.append(row);
  }
}

async function updateUser(username, payload) {
  const res = await api(`/auth/users/${encodeURIComponent(username)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
  return res.json();
}

function showUserResult(text) {
  els.userResult.textContent = text;
}

async function createUserFromDialog() {
  const fd = new FormData(els.userForm);
  showUserResult("");
  const res = await api("/auth/users", {
    method: "POST",
    body: JSON.stringify({
      username: (fd.get("username") || "").toString().trim(),
      password: (fd.get("password") || "").toString(),
      is_admin: fd.get("is_admin") === "on",
      enable_totp: fd.get("enable_totp") === "on",
    }),
  });
  const data = await res.json();
  els.userForm.reset();
  els.userForm.querySelector('input[name="enable_totp"]').checked = true;
  showUserResult(data.totp_secret ? `2FA secret for ${data.user.username}: ${data.totp_secret}` : `Created ${data.user.username}`);
  await loadUsers();
}

els.userForm.addEventListener("submit", (e) => {
  e.preventDefault();
  createUserFromDialog().catch((err) => showUserResult(err.message));
});

els.newDialog.addEventListener("close", () => {
  if (els.newDialog.returnValue !== "create") return;
  const fd = new FormData(els.newDialog.querySelector("form"));
  const allowed = (fd.get("allowed_tools") || "")
    .toString()
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const maxTurnsRaw = fd.get("max_turns");
  const payload = {
    cwd: (fd.get("cwd") || "").trim() || null,
    system_prompt: fd.get("system_prompt") || null,
    permission_mode: fd.get("permission_mode") || "acceptEdits",
    allowed_tools: allowed,
    max_turns: maxTurnsRaw ? Number(maxTurnsRaw) : null,
    model: (fd.get("model") || "").trim() || null,
  };
  createSession(payload).catch((e) => alert(e.message));
});

/* wire UI */
els.newSession.onclick = () => {
  if (!state.authenticated) return openLoginDialog();
  els.newDialog.showModal();
};
els.account.onclick = () => state.authenticated ? logout() : openLoginDialog();
els.passkeysBtn.onclick = () => {
  els.passkeysDialog.showModal();
  showPasskeyResult("");
  loadPasskeys().catch((err) => showPasskeyResult(err.message));
};
els.usersBtn.onclick = () => {
  els.usersDialog.showModal();
  loadUsers().catch((err) => showUserResult(err.message));
};
els.refresh.onclick = loadSessions;
els.stopSession.onclick = stopActiveSession;

els.promptForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = els.promptInput.value.trim();
  if (!text) return;
  els.promptInput.value = "";
  sendPrompt(text);
});

els.promptInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    els.promptForm.requestSubmit();
  }
});

/* boot */
clearActive();
loadAuthStatus().then(() => {
  if (state.authenticated) {
    loadSessions();
  } else {
    openLoginDialog();
  }
});
