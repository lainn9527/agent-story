/* ============================================================
   Lore Console — Frontend Logic
   ============================================================ */

(function () {
  "use strict";

  // ------------------------------------------------------------------
  // State
  // ------------------------------------------------------------------
  let allEntries = [];
  let categories = [];
  let collapsedCats = new Set(); // preserve collapsed state across re-renders
  let selectedTopic = null;
  let chatMessages = []; // {role, content} history for LLM
  let isStreaming = false;
  let editingEntry = null; // null = create, {topic, ...} = edit
  let streamAbortController = null;

  // ------------------------------------------------------------------
  // DOM refs
  // ------------------------------------------------------------------
  const $loreList = document.getElementById("lore-list");
  const $searchInput = document.getElementById("lore-search");
  const $addBtn = document.getElementById("lore-add-btn");
  const $chatMessages = document.getElementById("chat-messages");
  const $chatInput = document.getElementById("chat-input");
  const $chatSendBtn = document.getElementById("chat-send-btn");
  const $modal = document.getElementById("edit-modal");
  const $modalTitle = document.getElementById("modal-title");
  const $modalCategory = document.getElementById("modal-category");
  const $modalTopic = document.getElementById("modal-topic");
  const $modalContent = document.getElementById("modal-content");
  const $modalSave = document.getElementById("modal-save");
  const $modalCancel = document.getElementById("modal-cancel");
  const $modalClose = document.getElementById("modal-close");
  const $modalDelete = document.getElementById("modal-delete");
  const $storyName = document.getElementById("story-name");

  // ------------------------------------------------------------------
  // Helpers
  // ------------------------------------------------------------------
  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function truncate(str, len) {
    if (!str) return "";
    return str.length > len ? str.slice(0, len) + "..." : str;
  }

  // ------------------------------------------------------------------
  // API calls
  // ------------------------------------------------------------------
  async function fetchLoreAll() {
    const res = await fetch("/api/lore/all");
    const data = await res.json();
    if (data.ok) {
      allEntries = data.entries || [];
      categories = data.categories || [];
    }
    return data;
  }

  async function createEntry(entry) {
    const res = await fetch("/api/lore/entry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(entry),
    });
    return res.json();
  }

  async function updateEntry(topic, updates) {
    const res = await fetch("/api/lore/entry", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic, ...updates }),
    });
    return res.json();
  }

  async function deleteEntry(topic) {
    const res = await fetch("/api/lore/entry", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic }),
    });
    return res.json();
  }

  async function applyProposals(proposals) {
    const res = await fetch("/api/lore/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ proposals }),
    });
    return res.json();
  }

  // ------------------------------------------------------------------
  // Lore list rendering
  // ------------------------------------------------------------------
  function renderLoreList(filter) {
    const q = (filter || "").toLowerCase();
    const filtered = q
      ? allEntries.filter(
          (e) =>
            e.topic.toLowerCase().includes(q) ||
            (e.content || "").toLowerCase().includes(q) ||
            e.category.toLowerCase().includes(q)
        )
      : allEntries;

    // Group by category
    const groups = new Map();
    for (const e of filtered) {
      const cat = e.category || "其他";
      if (!groups.has(cat)) groups.set(cat, []);
      groups.get(cat).push(e);
    }

    // Preserve category order from API
    const orderedCats = categories.filter((c) => groups.has(c));
    for (const c of groups.keys()) {
      if (!orderedCats.includes(c)) orderedCats.push(c);
    }

    let html = "";
    for (const cat of orderedCats) {
      const entries = groups.get(cat);
      const collapsed = collapsedCats.has(cat) ? " collapsed" : "";
      html += `<div class="lore-category${collapsed}">`;
      html += `<div class="lore-cat-header" data-cat="${escapeHtml(cat)}">`;
      html += `<span class="lore-cat-arrow">&#x25BC;</span> `;
      html += `${escapeHtml(cat)}`;
      html += `<span class="lore-cat-count">(${entries.length})</span>`;
      html += `</div>`;
      html += `<div class="lore-cat-entries">`;
      for (const e of entries) {
        const sel = e.topic === selectedTopic ? " selected" : "";
        html += `<div class="lore-entry${sel}" data-topic="${escapeHtml(e.topic)}">`;
        html += `<span class="lore-entry-topic">${escapeHtml(e.topic)}</span>`;
        html += `<button class="lore-entry-edit" data-topic="${escapeHtml(e.topic)}" title="編輯">&#x270E;</button>`;
        html += `</div>`;
        html += `<div class="lore-entry-preview">${escapeHtml(truncate(e.content, 300))}</div>`;
      }
      html += `</div></div>`;
    }

    if (!html) {
      html = `<div style="padding:20px;color:var(--text-dim);text-align:center;">沒有符合的設定</div>`;
    }

    $loreList.innerHTML = html;

    // Bind click handlers
    $loreList.querySelectorAll(".lore-cat-header").forEach((el) => {
      el.addEventListener("click", () => {
        const cat = el.dataset.cat;
        el.parentElement.classList.toggle("collapsed");
        if (collapsedCats.has(cat)) collapsedCats.delete(cat);
        else collapsedCats.add(cat);
      });
    });

    $loreList.querySelectorAll(".lore-entry").forEach((el) => {
      el.addEventListener("click", (ev) => {
        if (ev.target.closest(".lore-entry-edit")) return;
        const topic = el.dataset.topic;
        selectedTopic = selectedTopic === topic ? null : topic;
        renderLoreList($searchInput.value);
      });
    });

    $loreList.querySelectorAll(".lore-entry-edit").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const topic = btn.dataset.topic;
        const entry = allEntries.find((e) => e.topic === topic);
        if (entry) openModal(entry);
      });
    });
  }

  // ------------------------------------------------------------------
  // Modal
  // ------------------------------------------------------------------
  function populateCategoryDropdown(selectedCat) {
    const cats = [...new Set([...categories, "其他"])];
    $modalCategory.innerHTML = cats
      .map(
        (c) =>
          `<option value="${escapeHtml(c)}"${c === selectedCat ? " selected" : ""}>${escapeHtml(c)}</option>`
      )
      .join("");
  }

  function openModal(entry) {
    editingEntry = entry || null;
    if (entry) {
      $modalTitle.textContent = "編輯設定";
      $modalDelete.style.display = "";
      populateCategoryDropdown(entry.category);
      $modalTopic.value = entry.topic;
      $modalContent.value = entry.content || "";
    } else {
      $modalTitle.textContent = "新增設定";
      $modalDelete.style.display = "none";
      populateCategoryDropdown(categories[0] || "其他");
      $modalTopic.value = "";
      $modalContent.value = "";
    }
    $modal.classList.remove("hidden");
    ($modalTopic.value ? $modalContent : $modalTopic).focus();
  }

  function closeModal() {
    $modal.classList.add("hidden");
    editingEntry = null;
  }

  async function saveModal() {
    const category = $modalCategory.value;
    const topic = $modalTopic.value.trim();
    const content = $modalContent.value.trim();
    if (!topic) {
      $modalTopic.focus();
      return;
    }

    if (editingEntry) {
      const updates = { category, content };
      if (topic !== editingEntry.topic) updates.new_topic = topic;
      const res = await updateEntry(editingEntry.topic, updates);
      if (!res.ok) {
        alert(res.error || "更新失敗");
        return;
      }
    } else {
      const res = await createEntry({ category, topic, content });
      if (!res.ok) {
        alert(res.error || "新增失敗");
        return;
      }
    }

    closeModal();
    await refreshLoreList();
  }

  async function deleteFromModal() {
    if (!editingEntry) return;
    if (!confirm(`確定要刪除「${editingEntry.topic}」？`)) return;
    const res = await deleteEntry(editingEntry.topic);
    if (!res.ok) {
      alert(res.error || "刪除失敗");
      return;
    }
    closeModal();
    selectedTopic = null;
    await refreshLoreList();
  }

  // ------------------------------------------------------------------
  // Refresh lore list
  // ------------------------------------------------------------------
  async function refreshLoreList() {
    await fetchLoreAll();
    renderLoreList($searchInput.value);
  }

  // ------------------------------------------------------------------
  // Chat
  // ------------------------------------------------------------------
  function appendChatMessage(role, content, proposals) {
    const div = document.createElement("div");
    div.className = `chat-msg ${role}`;

    const label = document.createElement("div");
    label.className = "chat-msg-label";
    label.textContent = role === "user" ? "你" : "AI 助手";
    div.appendChild(label);

    const body = document.createElement("div");
    body.className = "chat-msg-content";
    body.textContent = content;
    div.appendChild(body);

    if (proposals && proposals.length > 0) {
      const cardsDiv = document.createElement("div");
      cardsDiv.className = "proposal-cards";
      for (let i = 0; i < proposals.length; i++) {
        cardsDiv.appendChild(createProposalCard(proposals[i], i));
      }
      div.appendChild(cardsDiv);
    }

    $chatMessages.appendChild(div);
    $chatMessages.scrollTop = $chatMessages.scrollHeight;
    return { div, body };
  }

  function createProposalCard(proposal, idx) {
    const card = document.createElement("div");
    card.className = "proposal-card";
    card.dataset.idx = idx;

    const actionLabels = { add: "新增", edit: "修改", delete: "刪除" };
    const action = (proposal.action || "add").toLowerCase();
    const badgeClass = action;
    const badgeText = actionLabels[action] || action;

    let html = `<div class="proposal-header">`;
    html += `<span class="proposal-badge ${escapeHtml(badgeClass)}">${escapeHtml(badgeText)}</span>`;
    html += `<span class="proposal-topic">${escapeHtml(proposal.topic || "")}</span>`;
    if (proposal.category) {
      html += ` <span style="color:var(--text-dim);font-size:0.8rem;">(${escapeHtml(proposal.category)})</span>`;
    }
    html += `</div>`;

    if (proposal.content && action !== "delete") {
      const full = escapeHtml(proposal.content);
      const short = escapeHtml(truncate(proposal.content, 200));
      const needsExpand = proposal.content.length > 200;
      html += `<div class="proposal-content">${needsExpand ? short : full}</div>`;
      if (needsExpand) {
        html += `<button class="proposal-expand-btn">展開全文</button>`;
      }
    }

    html += `<div class="proposal-actions">`;
    html += `<button class="btn-accept">採用</button>`;
    html += `<button class="btn-reject">忽略</button>`;
    html += `</div>`;

    card.innerHTML = html;

    // Expand/collapse full content
    const expandBtn = card.querySelector(".proposal-expand-btn");
    if (expandBtn) {
      let expanded = false;
      const contentDiv = card.querySelector(".proposal-content");
      const full = escapeHtml(proposal.content);
      const short = escapeHtml(truncate(proposal.content, 200));
      expandBtn.addEventListener("click", () => {
        expanded = !expanded;
        contentDiv.innerHTML = expanded ? full : short;
        expandBtn.textContent = expanded ? "收起" : "展開全文";
      });
    }

    // Accept with confirmation for delete, error recovery
    card.querySelector(".btn-accept").addEventListener("click", async () => {
      if (action === "delete") {
        if (!confirm(`確定要刪除「${proposal.topic}」？`)) return;
      }
      const actionsDiv = card.querySelector(".proposal-actions");
      actionsDiv.innerHTML = '<span style="color:var(--text-dim);font-size:0.8rem;">套用中...</span>';
      try {
        const res = await applyProposals([proposal]);
        if (res.ok) {
          card.classList.add("applied");
          actionsDiv.innerHTML = '<span style="color:#7fdb96;font-size:0.8rem;">已採用</span>';
          await refreshLoreList();
        } else {
          actionsDiv.innerHTML =
            '<span style="color:#db7f7f;font-size:0.8rem;">套用失敗</span> ' +
            '<button class="btn-accept" style="margin-left:8px;">重試</button>';
        }
      } catch {
        actionsDiv.innerHTML =
          '<span style="color:#db7f7f;font-size:0.8rem;">連線錯誤</span> ' +
          '<button class="btn-accept" style="margin-left:8px;">重試</button>';
      }
    });

    card.querySelector(".btn-reject").addEventListener("click", () => {
      card.classList.add("rejected");
      card.querySelector(".proposal-actions").innerHTML =
        '<span style="color:var(--text-dim);font-size:0.8rem;">已忽略</span>';
    });

    return card;
  }

  function finishStreaming() {
    isStreaming = false;
    streamAbortController = null;
    $chatSendBtn.disabled = false;
  }

  async function sendChat() {
    const text = $chatInput.value.trim();
    if (!text || isStreaming) return;

    // Trim chat history to last 40 messages to avoid unbounded growth
    if (chatMessages.length > 40) {
      chatMessages = chatMessages.slice(-40);
    }

    chatMessages.push({ role: "user", content: text });
    appendChatMessage("user", text);
    $chatInput.value = "";
    isStreaming = true;
    $chatSendBtn.disabled = true;
    streamAbortController = new AbortController();

    const { div: msgDiv, body: msgBody } = appendChatMessage("assistant", "");
    msgBody.innerHTML = '<span class="streaming-cursor"></span>';

    let fullText = "";
    let proposals = [];

    try {
      const response = await fetch("/api/lore/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: chatMessages }),
        signal: streamAbortController.signal,
      });

      if (!response.ok) {
        fullText = `[伺服器錯誤: ${response.status}]`;
        msgBody.textContent = fullText;
        chatMessages.push({ role: "assistant", content: fullText });
        finishStreaming();
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          let event;
          try {
            event = JSON.parse(line.slice(6));
          } catch {
            continue;
          }

          if (event.type === "text") {
            fullText += event.chunk;
            msgBody.textContent = fullText;
            const cursor = document.createElement("span");
            cursor.className = "streaming-cursor";
            msgBody.appendChild(cursor);
            $chatMessages.scrollTop = $chatMessages.scrollHeight;
          } else if (event.type === "done") {
            fullText = event.response || fullText;
            proposals = event.proposals || [];
          } else if (event.type === "error") {
            fullText += `\n[Error: ${event.message}]`;
          }
        }
      }
    } catch (err) {
      if (err.name === "AbortError") {
        fullText += "\n[已中斷]";
      } else {
        fullText += `\n[連線錯誤: ${err.message}]`;
      }
    }

    // Finalize message
    msgBody.textContent = fullText;

    if (proposals.length > 0) {
      const cardsDiv = document.createElement("div");
      cardsDiv.className = "proposal-cards";
      for (let i = 0; i < proposals.length; i++) {
        cardsDiv.appendChild(createProposalCard(proposals[i], i));
      }
      msgDiv.appendChild(cardsDiv);
    }

    chatMessages.push({ role: "assistant", content: fullText });
    finishStreaming();
    $chatMessages.scrollTop = $chatMessages.scrollHeight;
  }

  // ------------------------------------------------------------------
  // Mobile tabs
  // ------------------------------------------------------------------
  function setupMobileTabs() {
    const tabs = document.querySelectorAll(".mobile-tab");
    const listPanel = document.getElementById("lore-list-panel");
    const chatPanel = document.getElementById("lore-chat-panel");

    tabs.forEach((tab) => {
      tab.addEventListener("click", () => {
        tabs.forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");

        if (tab.dataset.panel === "list") {
          listPanel.classList.remove("hidden-mobile");
          chatPanel.classList.add("hidden-mobile");
        } else {
          chatPanel.classList.remove("hidden-mobile");
          listPanel.classList.add("hidden-mobile");
        }
      });
    });
  }

  // ------------------------------------------------------------------
  // Fetch story name
  // ------------------------------------------------------------------
  async function fetchStoryName() {
    try {
      const res = await fetch("/api/stories");
      const data = await res.json();
      if (data.ok && data.active_story_id && data.stories) {
        const story = data.stories.find((s) => s.id === data.active_story_id);
        if (story) $storyName.textContent = story.name || story.id;
      }
    } catch {
      // ignore
    }
  }

  // ------------------------------------------------------------------
  // Init
  // ------------------------------------------------------------------
  async function init() {
    await fetchLoreAll();
    renderLoreList();
    fetchStoryName();
    setupMobileTabs();

    // Search
    $searchInput.addEventListener("input", () => {
      renderLoreList($searchInput.value);
    });

    // Add button
    $addBtn.addEventListener("click", () => openModal(null));

    // Modal handlers
    $modalSave.addEventListener("click", saveModal);
    $modalCancel.addEventListener("click", closeModal);
    $modalClose.addEventListener("click", closeModal);
    $modalDelete.addEventListener("click", deleteFromModal);
    $modal.addEventListener("click", (ev) => {
      if (ev.target === $modal) closeModal();
    });

    // Escape key closes modal
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && !$modal.classList.contains("hidden")) {
        closeModal();
      }
    });

    // Chat send
    $chatSendBtn.addEventListener("click", sendChat);
    $chatInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        sendChat();
      }
    });
  }

  init();
})();
