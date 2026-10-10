/* Shared presentation and transport for world and card chat. No game state or initialization. */

/** Light haptic tap — silent no-op on unsupported browsers. */
function haptic(ms = 10) {
  if (navigator.vibrate) navigator.vibrate(ms);
}

function showToast(msg, duration = 2000) {
  const el = document.createElement("div");
  el.className = "toast-msg";
  el.textContent = msg;
  document.body.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 300);
  }, duration);
}

function showConfirm(msg) {
  return new Promise((resolve) => {
    const overlay = document.getElementById("confirm-modal");
    const msgEl = document.getElementById("confirm-modal-msg");
    const okBtn = document.getElementById("confirm-modal-ok");
    const cancelBtn = document.getElementById("confirm-modal-cancel");
    msgEl.textContent = msg;
    overlay.classList.remove("hidden");

    function cleanup(result) {
      overlay.classList.add("hidden");
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      overlay.removeEventListener("click", onOverlay);
      document.removeEventListener("keydown", onKey);
      resolve(result);
    }
    function onOk() { haptic(); cleanup(true); }
    function onCancel() { haptic(); cleanup(false); }
    function onOverlay(e) { if (e.target === overlay) cleanup(false); }
    function onKey(e) {
      if (e.key === "Enter") { e.preventDefault(); cleanup(true); }
      if (e.key === "Escape") { e.preventDefault(); cleanup(false); }
    }

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    overlay.addEventListener("click", onOverlay);
    document.addEventListener("keydown", onKey);
  });
}

function showAlert(msg) {
  return new Promise((resolve) => {
    const overlay = document.getElementById("alert-modal");
    const msgEl = document.getElementById("alert-modal-msg");
    const okBtn = document.getElementById("alert-modal-ok");
    msgEl.textContent = msg;
    overlay.classList.remove("hidden");

    function cleanup() {
      overlay.classList.add("hidden");
      okBtn.removeEventListener("click", onOk);
      overlay.removeEventListener("click", onOverlay);
      document.removeEventListener("keydown", onKey);
      resolve();
    }
    function onOk() { haptic(); cleanup(); }
    function onOverlay(e) { if (e.target === overlay) cleanup(); }
    function onKey(e) {
      if (e.key === "Enter" || e.key === "Escape") { e.preventDefault(); cleanup(); }
    }

    okBtn.addEventListener("click", onOk);
    overlay.addEventListener("click", onOverlay);
    document.addEventListener("keydown", onKey);
  });
}

function showPrompt(msg, defaultValue = "") {
  return new Promise((resolve) => {
    const overlay = document.getElementById("prompt-modal");
    const msgEl = document.getElementById("prompt-modal-msg");
    const input = document.getElementById("prompt-modal-input");
    const okBtn = document.getElementById("prompt-modal-ok");
    const cancelBtn = document.getElementById("prompt-modal-cancel");
    msgEl.textContent = msg;
    input.value = defaultValue;
    overlay.classList.remove("hidden");
    input.focus();
    input.select();

    function cleanup(result) {
      overlay.classList.add("hidden");
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      overlay.removeEventListener("click", onOverlay);
      document.removeEventListener("keydown", onKey);
      resolve(result);
    }
    function onOk() { haptic(); cleanup(input.value); }
    function onCancel() { haptic(); cleanup(null); }
    function onOverlay(e) { if (e.target === overlay) cleanup(null); }
    function onKey(e) {
      if (e.key === "Enter") { e.preventDefault(); cleanup(input.value); }
      if (e.key === "Escape") { e.preventDefault(); cleanup(null); }
    }

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    overlay.addEventListener("click", onOverlay);
    document.addEventListener("keydown", onKey);
  });
}

async function streamFromSSE(url, body, onChunk, onDone, onError, signal) {
  const resp = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body), signal,
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    onError(data.error || `HTTP ${resp.status}`);
    return;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let finished = false;
  function lineEvent(line) {
    if (finished || !line.startsWith('data:')) return;
    let data;
    try { data = JSON.parse(line.slice(5).trim()); }
    catch { return; }
    if (data.type === 'text') onChunk(data.chunk);
    else if (data.type === 'done') { finished = true; onDone(data); }
    else if (data.type === 'error') { finished = true; onError(data.message); }
  }
  try {
    while (!finished) {
      const { done, value } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) lineEvent(line);
      if (done) { if (buffer) lineEvent(buffer); break; }
    }
    if (!finished) onError('連線中斷');
  } finally {
    // A terminal event completes the UI even if the server keeps the connection open.
    if (finished) await reader.cancel();
    reader.releaseLock();
  }
}

function stripHiddenTags(text) {
  text = text.replace(/<!--STATE\s*[\s\S]*?STATE-->/g, "");
  text = text.replace(/<!--LORE\s*[\s\S]*?LORE-->/g, "");
  text = text.replace(/<!--NPC\s*[\s\S]*?NPC-->/g, "");
  text = text.replace(/<!--EVENT\s*[\s\S]*?EVENT-->/g, "");
  text = text.replace(/<!--IMG\s*[\s\S]*?IMG-->/g, "");
  text = text.replace(/<!--ACTIONS\s*-->/g, "");
  // Truncate any unclosed tag at the end (partial tag still streaming)
  const partialTagIdx = text.lastIndexOf("<!--");
  if (partialTagIdx !== -1) {
    const afterTag = text.slice(partialTagIdx);
    if (!afterTag.includes("-->")) {
      text = text.slice(0, partialTagIdx);
    }
  }
  return text.trim();
}

function markdownToHtml(text) {
  if (!text) return "";
  text = stripHiddenTags(text);
  let html = escapeHtml(text);

  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");
  html = html.replace(/^#### (.+)$/gm, "<h4>$1</h4>");
  html = html.replace(/^### (.+)$/gm, "<h3>$1</h3>");
  html = html.replace(/^## (.+)$/gm, "<h2>$1</h2>");
  html = html.replace(/^# (.+)$/gm, "<h1>$1</h1>");
  html = html.replace(/【(.+?)】/g, '<span class="god-hint">【$1】</span>');
  html = html.replace(/^---$/gm, '<hr class="scene-break">');
  html = html.replace(/（(.+?)）/g, '<span class="aside">（$1）</span>');
  html = html.replace(/^[•·\-]\s+(.+)$/gm, "<li>$1</li>");
  html = html.replace(/<\/li>\n+<li>/g, "</li>\n<li>");
  html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, "<ul>$1</ul>");
  html = html.replace(/^\d+[.、]\s*(.+)$/gm, "<li>$1</li>");
  html = html.replace(/<\/li>\n+<li>/g, "</li>\n<li>");
  html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, "<ol>$1</ol>");
  html = html.replace(/\n{2,}/g, "</p><p>");
  html = html.replace(/\n/g, "<br>");
  html = "<p>" + html + "</p>";
  html = html.replace(/<p>\s*<\/p>/g, "");
  html = html.replace(/(<\/h[1234]>)(<br>|<\/p><p>)/g, "$1");
  html = html.replace(/(<\/[uo]l>)(<br>|<\/p><p>)/g, "$1");
  html = html.replace(/(<[uo]l>)(<br>|<\/p><p>)/g, "$1");
  html = html.replace(/(<hr class="scene-break">)(<br>|<\/p><p>)/g, "$1");

  return html;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function scrollToBottom() {
  const container = document.getElementById("messages");
  requestAnimationFrame(() => {
    container.scrollTop = container.scrollHeight;
  });
}

function smartScrollToBottom() {
  const container = document.getElementById("messages");
  // If user is within 100px of bottom, auto-scroll. Otherwise, stay where they are.
  const isNearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 100;
  if (isNearBottom) {
    scrollToBottom();
  }
}

function setChatDrawerOpen(open) {
  document.getElementById('drawer')?.classList.toggle('closed', !open);
  document.getElementById('drawer-overlay')?.classList.toggle('hidden', !open);
  document.getElementById('drawer-toggle-btn')?.setAttribute('aria-expanded', String(open));
}

function bindChatComposer(input, button, send) {
  button.addEventListener('click', send);
  input.addEventListener('keydown', e => {
    if (!e.isComposing && e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      send();
    }
  });
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
  });
}

function createChatMessage(msg) {
  const el = document.createElement('div');
  el.className = `message ${msg.role === 'user' ? 'user' : 'gm'}`;
  if (msg.index != null) el.dataset.index = msg.index;
  el.addEventListener('touchstart', () => {
    document.querySelectorAll('.message.touched').forEach(m => m.classList.remove('touched'));
    el.classList.add('touched');
  }, { passive: true });
  const roleTag = document.createElement('div');
  roleTag.className = 'role-tag';
  roleTag.textContent = msg.role === 'user' ? '玩家' : 'GM';
  const indexLabel = document.createElement('span');
  indexLabel.className = 'msg-index';
  indexLabel.textContent = msg.index == null ? '' : `#${msg.index}`;
  const content = document.createElement('div');
  content.className = 'content';
  content.innerHTML = markdownToHtml(msg.content);
  el.append(roleTag, indexLabel, content);
  return { el, roleTag, indexLabel, content };
}

const GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite"];
const VERTEX_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash"];
const OPENROUTER_MODELS = ["deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4-flash"];
const CLAUDE_MODELS = ["claude-sonnet-4-6", "claude-opus-4-6"];
const CODEX_MODELS = ["gpt-5.4", "gpt-5.4-mini"];
const PROVIDER_LABELS = { gemini: "Gemini", vertex: "Vertex", openrouter: "OpenRouter", claude_cli: "Claude", codex_agent: "Codex" };
const PROVIDER_MODELS = {
  gemini: GEMINI_MODELS,
  vertex: VERTEX_MODELS,
  openrouter: OPENROUTER_MODELS,
  claude_cli: CLAUDE_MODELS,
  codex_agent: CODEX_MODELS,
};

function populateChatModels(select, cfg) {
  select.replaceChildren();
  const currentModel = cfg[cfg.provider]?.model || '';
  const choices = { ...PROVIDER_MODELS };
  if (currentModel) choices[cfg.provider] = [...new Set([...(choices[cfg.provider] || []), currentModel])];
  for (const [provider, models] of Object.entries(choices)) {
    const group = document.createElement('optgroup');
    group.label = PROVIDER_LABELS[provider] || provider;
    for (const model of models) {
      const option = document.createElement('option');
      option.value = `${provider}|${model}`;
      option.textContent = model;
      option.selected = provider === cfg.provider && model === currentModel;
      group.appendChild(option);
    }
    select.appendChild(group);
  }
}
