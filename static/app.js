/* ============================================================
   主神空間 RPG — Frontend Logic (multi-story + drawer UI)
   ============================================================ */

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
    function onOk() { cleanup(true); }
    function onCancel() { cleanup(false); }
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

const API = {
  init: () => fetch("/api/init", { method: "POST" }).then(r => r.json()),

  messages: (branchId = "main", offset = 0, limit = 99999, afterIndex = null, tail = null) => {
    let url = `/api/messages?branch_id=${branchId}&offset=${offset}&limit=${limit}`;
    if (afterIndex != null) url += `&after_index=${afterIndex}`;
    if (tail != null) url += `&tail=${tail}`;
    return fetch(url).then(r => r.json());
  },

  send: (message, branchId = "main") =>
    fetch("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, branch_id: branchId }),
    }).then(r => r.json()),

  status: (branchId = "main") =>
    fetch(`/api/status?branch_id=${branchId}`).then(r => r.json()),

  branches: () => fetch("/api/branches").then(r => r.json()),

  createBranch: (name, parentBranchId, branchPointIndex) =>
    fetch("/api/branches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        parent_branch_id: parentBranchId,
        branch_point_index: branchPointIndex,
      }),
    }).then(r => r.json()),

  createBlankBranch: (name) =>
    fetch("/api/branches/blank", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }).then(r => r.json()),

  switchBranch: (branchId) =>
    fetch("/api/branches/switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ branch_id: branchId }),
    }).then(r => r.json()),

  deleteBranch: (branchId) =>
    fetch(`/api/branches/${branchId}`, { method: "DELETE" }).then(r => r.json()),

  editBranch: (parentBranchId, branchPointIndex, editedMessage) =>
    fetch("/api/branches/edit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        parent_branch_id: parentBranchId,
        branch_point_index: branchPointIndex,
        edited_message: editedMessage,
      }),
    }).then(r => r.json()),

  regenerateBranch: (parentBranchId, branchPointIndex) =>
    fetch("/api/branches/regenerate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        parent_branch_id: parentBranchId,
        branch_point_index: branchPointIndex,
      }),
    }).then(r => r.json()),

  mergeBranch: (branchId) =>
    fetch("/api/branches/merge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ branch_id: branchId }),
    }).then(r => r.json()),

  promoteBranch: (branchId) =>
    fetch("/api/branches/promote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ branch_id: branchId }),
    }).then(r => r.json()),

  renameBranch: (branchId, newName) =>
    fetch(`/api/branches/${branchId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: newName }),
    }).then(r => r.json()),

  // Story APIs
  stories: () => fetch("/api/stories").then(r => r.json()),

  createStory: (data) =>
    fetch("/api/stories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }).then(r => r.json()),

  switchStory: (storyId) =>
    fetch("/api/stories/switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ story_id: storyId }),
    }).then(r => r.json()),

  deleteStory: (storyId) =>
    fetch(`/api/stories/${storyId}`, { method: "DELETE" }).then(r => r.json()),

  // NPC APIs
  npcs: (branchId) => fetch(`/api/npcs?branch_id=${branchId || "main"}`).then(r => r.json()),
  deleteNpc: (npcId) =>
    fetch(`/api/npcs/${npcId}`, { method: "DELETE" }).then(r => r.json()),

  // Event APIs
  events: (branchId) =>
    fetch(`/api/events?branch_id=${branchId || ""}`).then(r => r.json()),
  updateEventStatus: (eventId, status) =>
    fetch(`/api/events/${eventId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    }).then(r => r.json()),

  // Image APIs
  imageStatus: (filename) =>
    fetch(`/api/images/status?filename=${encodeURIComponent(filename)}`).then(r => r.json()),

  // NPC Activities
  npcActivities: (branchId) =>
    fetch(`/api/npc-activities?branch_id=${branchId || "main"}`).then(r => r.json()),

  // Auto-play Summaries
  autoPlaySummaries: (branchId) =>
    fetch(`/api/auto-play/summaries?branch_id=${branchId || "main"}`).then(r => r.json()),
};

// ---------------------------------------------------------------------------
// SSE Streaming helper
// ---------------------------------------------------------------------------
async function streamFromSSE(url, body, onChunk, onDone, onError, signal) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: signal,
  });
  if (!resp.ok) {
    onError("HTTP " + resp.status);
    return;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finished = false;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const lines = buffer.split("\n");
    buffer = lines.pop();  // keep incomplete line in buffer

    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      try {
        const data = JSON.parse(line.slice(6));
        if (data.type === "text") {
          onChunk(data.chunk);
        } else if (data.type === "done") {
          finished = true;
          onDone(data);
        } else if (data.type === "error") {
          finished = true;
          onError(data.message);
        }
      } catch (e) {
        // skip malformed lines
      }
    }
  }

  // Guard: stream ended without "done" or "error" event
  if (!finished) {
    onError("連線中斷");
  }
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let allMessages = [];
let totalMessages = 0;
let loadedOffset = 0;
let originalCount = 0;
let isSending = false;

// Branch state
let currentBranchId = "main";
let branches = {};
let forkPoints = {};
let activeStreamController = null;
let siblingGroups = {};

// Story state
let currentStoryId = null;
let stories = {};
let characterSchema = {};

// Live polling state (auto-play branches)
let _livePollingTimer = null;
let _livePollingBranchId = null;

function isAutoBranch(branchId) {
  return branchId && branchId.startsWith("auto_");
}

function startLivePolling(branchId) {
  stopLivePolling();
  _livePollingBranchId = branchId;

  // Disable input during live view
  const $input = document.getElementById("user-input");
  const $sendBtn = document.getElementById("send-btn");
  if ($input) { $input.disabled = true; $input.placeholder = "自動遊玩中，僅供觀看..."; }
  if ($sendBtn) $sendBtn.disabled = true;

  updateLiveIndicator(true, null);

  const poll = async () => {
    if (_livePollingBranchId !== branchId) return;
    try {
      let lastIndex = -1;
      for (const m of allMessages) {
        const idx = m.index ?? 0;
        if (idx > lastIndex) lastIndex = idx;
      }
      const data = await API.messages(branchId, 0, 99999, lastIndex);
      if (_livePollingBranchId !== branchId) return;

      if (data.messages && data.messages.length > 0) {
        for (const msg of data.messages) {
          allMessages.push(msg);
          appendMessage(msg);
        }
        smartScrollToBottom();
      }

      updateLiveIndicator(true, data.auto_play_state);

      // Refresh summaries if drawer is open and new summaries available
      if (data.summary_count != null && data.summary_count > _lastSummaryCount) {
        if (!$drawer.classList.contains("closed")) {
          loadSummaries();
        }
      }

      if (data.live_status === "finished") {
        stopLivePolling();
        loadSummaries();
        return;
      }
    } catch (e) { /* ignore fetch errors, retry next cycle */ }
    if (_livePollingBranchId === branchId) {
      _livePollingTimer = setTimeout(poll, 3000);
    }
  };
  _livePollingTimer = setTimeout(poll, 1000);
}

function stopLivePolling() {
  if (_livePollingTimer) {
    clearTimeout(_livePollingTimer);
    _livePollingTimer = null;
  }
  _livePollingBranchId = null;

  // Re-enable input
  const $input = document.getElementById("user-input");
  const $sendBtn = document.getElementById("send-btn");
  if ($input) { $input.disabled = false; $input.placeholder = "輸入你的行動..."; }
  if ($sendBtn) $sendBtn.disabled = false;

  updateLiveIndicator(false, null);
}

function updateLiveIndicator(isLive, autoState) {
  let el = document.getElementById("live-indicator");
  if (!isLive) {
    if (el) el.remove();
    return;
  }
  if (!el) {
    el = document.createElement("div");
    el.id = "live-indicator";
    document.getElementById("header").appendChild(el);
  }
  let text = "\u25CF LIVE";
  if (autoState) {
    if (autoState.turn != null) text += ` \u2014 Turn ${autoState.turn}`;
    if (autoState.phase) text += ` (${autoState.phase})`;
  }
  el.textContent = text;
}

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------
const $messages = document.getElementById("message-list");
const $loadBtn = document.getElementById("load-earlier");
const $input = document.getElementById("user-input");
const $sendBtn = document.getElementById("send-btn");
const $loading = document.getElementById("loading-indicator");

// Drawer DOM refs
const $drawer = document.getElementById("drawer");
const $drawerOverlay = document.getElementById("drawer-overlay");
const $drawerToggleBtn = document.getElementById("drawer-toggle-btn");
const $drawerCloseBtn = document.getElementById("drawer-close-btn");
const $storyList = document.getElementById("story-list");
const $branchList = document.getElementById("branch-list");
const $newStoryBtn = document.getElementById("new-story-btn");
const $newBranchBtn = document.getElementById("new-branch-btn");
const $newBlankBranchBtn = document.getElementById("new-blank-branch-btn");
const $promoteBtn = document.getElementById("promote-branch-btn");
const $storyModal = document.getElementById("new-story-modal");

// ---------------------------------------------------------------------------
// Drawer open/close
// ---------------------------------------------------------------------------
function openDrawer() {
  $drawer.classList.remove("closed");
  $drawerOverlay.classList.remove("hidden");
  renderStoryList();
  renderBranchList();
  loadNpcs();
  loadEvents();
  loadSummaries();
}

function closeDrawer() {
  $drawer.classList.add("closed");
  $drawerOverlay.classList.add("hidden");
}

$drawerToggleBtn.addEventListener("click", openDrawer);
$drawerCloseBtn.addEventListener("click", closeDrawer);
$drawerOverlay.addEventListener("click", closeDrawer);

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
async function init() {
  showInitOverlay();

  try {
    // 1. Init backend (triggers migration)
    const initResult = await API.init();
    originalCount = initResult.original_count || 0;
    currentBranchId = initResult.active_branch_id || "main";
    currentStoryId = initResult.active_story_id || null;
    characterSchema = initResult.character_schema || {};
    updateInitStatus("載入故事資訊…");

    // 2. Load stories
    await loadStories();

    // 3. Load branches
    updateInitStatus("載入分支資訊…");
    await loadBranches();

    // 4. Load messages for active branch
    updateInitStatus("載入對話紀錄…");
    if (isAutoBranch(currentBranchId)) {
      await loadMessages(currentBranchId, { tail: 100 });
    } else {
      await loadMessages(currentBranchId);
    }

    // 5. Load character status
    updateInitStatus("載入角色狀態…");
    const status = await API.status(currentBranchId);
    renderCharacterStatus(status);

    // 6. Load NPCs, events, and summaries
    updateInitStatus("載入角色與事件…");
    await Promise.all([loadNpcs(), loadEvents(), loadSummaries()]);

    removeInitOverlay();
    scrollToBottom();

    // Show "load earlier" button for auto branches with truncated messages
    if (isAutoBranch(currentBranchId) && allMessages.length < totalMessages) {
      $loadBtn.style.display = "";
      $loadBtn.onclick = async () => {
        $loadBtn.style.display = "none";
        await loadMessages(currentBranchId);
        scrollToBottom();
      };
    }

    // Start live polling if current branch is auto-play
    if (isAutoBranch(currentBranchId)) {
      startLivePolling(currentBranchId);
    }
  } catch (err) {
    console.error("Init failed:", err);
    updateInitStatus("初始化失敗：" + err.message);
  }
}

// ---------------------------------------------------------------------------
// Story management
// ---------------------------------------------------------------------------
async function loadStories() {
  const result = await API.stories();
  stories = result.stories || {};
  currentStoryId = result.active_story_id || currentStoryId;
}

function renderStoryList() {
  $storyList.innerHTML = "";

  const sortedStories = Object.values(stories).sort((a, b) =>
    (a.created_at || "").localeCompare(b.created_at || "")
  );

  for (const story of sortedStories) {
    const item = document.createElement("div");
    item.className = "drawer-item" + (story.id === currentStoryId ? " active" : "");

    const label = document.createElement("span");
    label.className = "drawer-item-label";
    label.textContent = story.name;
    if (story.description) label.title = story.description;
    item.appendChild(label);

    // Delete button (only if more than 1 story)
    if (Object.keys(stories).length > 1) {
      const del = document.createElement("span");
      del.className = "drawer-item-delete";
      del.textContent = "\u2715";
      del.title = "刪除故事";
      del.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm(`確定要刪除故事「${story.name}」？所有分支和對話都會被刪除！`)) return;
        const res = await API.deleteStory(story.id);
        if (res.ok) {
          await loadStories();
          if (currentStoryId === story.id) {
            await switchToStory(res.active_story_id);
          }
          renderStoryList();
        } else {
          alert(res.error || "刪除失敗");
        }
      });
      item.appendChild(del);
    }

    item.addEventListener("click", () => {
      if (story.id !== currentStoryId) {
        switchToStory(story.id);
        closeDrawer();
      }
    });

    $storyList.appendChild(item);
  }
}

async function switchToStory(storyId) {
  stopLivePolling();
  try {
    const result = await API.switchStory(storyId);
    if (!result.ok) {
      alert(result.error || "切換故事失敗");
      return;
    }

    currentStoryId = result.active_story_id;
    currentBranchId = result.active_branch_id || "main";
    originalCount = result.original_count || 0;
    characterSchema = result.character_schema || {};

    // Reload branches, messages, status for the new story
    await loadBranches();
    await loadMessages(currentBranchId);
    const status = await API.status(currentBranchId);
    renderCharacterStatus(status);
    await Promise.all([loadNpcs(), loadEvents(), loadSummaries()]);
    scrollToBottom();
  } catch (err) {
    alert("切換故事錯誤：" + err.message);
  }
}

// ---------------------------------------------------------------------------
// Branch management
// ---------------------------------------------------------------------------
async function loadBranches() {
  const result = await API.branches();
  branches = result.branches || {};
  currentBranchId = result.active_branch_id || currentBranchId;
  // Show/hide promote button
  $promoteBtn.style.display = currentBranchId === "main" ? "none" : "";
}

function renderBranchList() {
  $branchList.innerHTML = "";

  const items = buildBranchTree();

  // Group items into root groups (each depth-0 + its descendants)
  const groups = [];
  for (const entry of items) {
    if (entry.depth === 0) {
      groups.push({ root: entry, children: [] });
    } else if (groups.length > 0) {
      groups[groups.length - 1].children.push(entry);
    }
  }

  // Find which root group owns the current branch
  function rootOwnsCurrentBranch(group) {
    if (group.root.branch.id === currentBranchId) return true;
    return group.children.some(c => c.branch.id === currentBranchId);
  }

  for (const group of groups) {
    const hasChildren = group.children.length > 0;
    const isExpanded = rootOwnsCurrentBranch(group);

    // -- Root header --
    const rootItem = createBranchItem(group.root.branch, 0, hasChildren, isExpanded);
    $branchList.appendChild(rootItem);

    // -- Children container (collapsible) --
    if (hasChildren) {
      const childrenContainer = document.createElement("div");
      childrenContainer.className = "branch-group-children" + (isExpanded ? "" : " collapsed");
      childrenContainer.dataset.rootId = group.root.branch.id;

      for (const { branch, depth } of group.children) {
        const childItem = createBranchItem(branch, depth, false, false);
        childrenContainer.appendChild(childItem);
      }
      $branchList.appendChild(childrenContainer);
    }
  }
}

function createBranchItem(branch, depth, hasChildren, isExpanded) {
    const item = document.createElement("div");
    item.className = "drawer-item" + (branch.id === currentBranchId ? " active" : "");
    if (depth > 0) item.style.paddingLeft = (12 + depth * 16) + "px";

    // Toggle arrow for depth-0 items with children
    if (depth === 0 && hasChildren) {
      const arrow = document.createElement("span");
      arrow.className = "branch-toggle-arrow" + (isExpanded ? " expanded" : "");
      arrow.textContent = "\u25B6";
      arrow.addEventListener("click", (e) => {
        e.stopPropagation();
        toggleBranchGroup(branch.id, arrow);
      });
      item.appendChild(arrow);
    }

    const label = document.createElement("span");
    label.className = "drawer-item-label";
    label.textContent = branch.name;
    item.appendChild(label);

    if (branch.id.startsWith("auto_")) {
      const badge = document.createElement("span");
      badge.className = "auto-branch-badge";
      badge.textContent = "AUTO";
      item.appendChild(badge);
    }

    // Rename & Delete buttons (not for main)
    if (branch.id !== "main") {
      const ren = document.createElement("span");
      ren.className = "drawer-item-rename";
      ren.textContent = "\u270E";
      ren.title = "重新命名";
      ren.addEventListener("click", (e) => {
        e.stopPropagation();
        startRenamingBranch(item, branch);
      });
      item.appendChild(ren);

      const del = document.createElement("span");
      del.className = "drawer-item-delete";
      del.textContent = "\u2715";
      del.title = "刪除分支";
      del.addEventListener("click", async (e) => {
        e.stopPropagation();
        const descCount = countDescendants(branch.id);
        let msg = `確定要刪除分支「${branch.name}」？`;
        if (descCount > 0) {
          msg += `\n（包含 ${descCount} 個子分支也會一併刪除）`;
        }
        if (!(await showConfirm(msg))) return;
        const res = await API.deleteBranch(branch.id);
        if (res.ok) {
          await loadBranches();
          if (currentBranchId === branch.id || !branches[currentBranchId]) {
            await switchToBranch("main");
          }
          renderBranchList();
        } else {
          alert(res.error || "刪除失敗");
        }
      });
      item.appendChild(del);

      const merge = document.createElement("span");
      merge.className = "drawer-item-merge";
      merge.textContent = "\u2934";
      merge.title = "合併到上層分支";
      merge.addEventListener("click", async (e) => {
        e.stopPropagation();
        const parentBranch = branches[branch.parent_branch_id];
        const parentName = parentBranch ? parentBranch.name : branch.parent_branch_id;
        if (!(await showConfirm(`確定要將分支「${branch.name}」合併到上層分支「${parentName}」嗎？`))) return;
        const res = await API.mergeBranch(branch.id);
        if (res.ok) {
          await loadBranches();
          await switchToBranch(res.parent_branch_id);
          renderBranchList();
        } else {
          alert(res.error || "合併失敗");
        }
      });
      item.appendChild(merge);
    }

    // Show branch ID on the active branch
    if (branch.id === currentBranchId) {
      const idTag = document.createElement("div");
      idTag.className = "branch-id-tag";
      idTag.textContent = branch.id;
      item.appendChild(idTag);
    }

    item.addEventListener("click", () => {
      if (branch.id !== currentBranchId) {
        switchToBranch(branch.id);
        closeDrawer();
      }
    });

    return item;
}

function toggleBranchGroup(rootId, arrowEl) {
  // Collapse all other groups, expand this one (accordion)
  const allContainers = $branchList.querySelectorAll(".branch-group-children");
  const allArrows = $branchList.querySelectorAll(".branch-toggle-arrow");
  const target = $branchList.querySelector(`.branch-group-children[data-root-id="${rootId}"]`);
  if (!target) return;

  const wasCollapsed = target.classList.contains("collapsed");

  // Collapse everything first
  allContainers.forEach(c => c.classList.add("collapsed"));
  allArrows.forEach(a => a.classList.remove("expanded"));

  // If it was collapsed, expand it
  if (wasCollapsed) {
    target.classList.remove("collapsed");
    arrowEl.classList.add("expanded");
  }
}

function startRenamingBranch(item, branch) {
  const label = item.querySelector(".drawer-item-label");
  const renBtn = item.querySelector(".drawer-item-rename");
  const delBtn = item.querySelector(".drawer-item-delete");

  // Hide action buttons during edit
  if (renBtn) renBtn.style.display = "none";
  if (delBtn) delBtn.style.display = "none";

  const input = document.createElement("input");
  input.type = "text";
  input.className = "branch-rename-input";
  input.value = branch.name;

  label.replaceWith(input);
  input.focus();
  input.select();

  let saved = false;

  async function save() {
    if (saved) return;
    saved = true;
    const newName = input.value.trim();
    if (!newName || newName === branch.name) {
      renderBranchList();
      return;
    }
    const res = await API.renameBranch(branch.id, newName);
    if (res.ok) {
      await loadBranches();
      renderBranchList();
    } else {
      alert(res.error || "重新命名失敗");
      renderBranchList();
    }
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); save(); }
    if (e.key === "Escape") { e.preventDefault(); renderBranchList(); }
  });
  input.addEventListener("blur", save);
}

function countDescendants(branchId) {
  let count = 0;
  const queue = [branchId];
  while (queue.length) {
    const bid = queue.shift();
    for (const b of Object.values(branches)) {
      if (b.parent_branch_id === bid && b.id !== branchId) {
        count++;
        queue.push(b.id);
      }
    }
  }
  return count;
}

function buildBranchTree() {
  const result = [];
  const children = {};

  for (const b of Object.values(branches)) {
    // Blank branches render at top level (same as main) despite having parent_branch_id
    const parent = b.blank ? "__root__" : (b.parent_branch_id || "__root__");
    if (!children[parent]) children[parent] = [];
    children[parent].push(b);
  }

  function walk(parentId, depth) {
    const kids = children[parentId] || [];
    kids.sort((a, b) => {
      if (a.id === "main") return -1;
      if (b.id === "main") return 1;
      return (a.created_at || "").localeCompare(b.created_at || "");
    });
    for (const branch of kids) {
      result.push({ branch, depth });
      walk(branch.id, depth + 1);
    }
  }

  walk("__root__", 0);
  return result;
}

async function switchToBranch(branchId, { scrollToIndex, preserveScroll, forcePreserve } = {}) {
  const container = document.getElementById("messages");
  let savedScrollTop = 0;
  let isAtBottom = false;

  if (preserveScroll || forcePreserve) {
    savedScrollTop = container.scrollTop;
    isAtBottom = (container.scrollTop + container.clientHeight >= container.scrollHeight - 50);
  }

  await API.switchBranch(branchId);
  currentBranchId = branchId;
  $promoteBtn.style.display = currentBranchId === "main" ? "none" : "";

  if (scrollToIndex != null || preserveScroll) {
    $messages.style.minHeight = $messages.scrollHeight + "px";
  }

  if (isAutoBranch(branchId)) {
    await loadMessages(branchId, { tail: 100 });
  } else {
    await loadMessages(branchId);
  }
  const status = await API.status(branchId);
  renderCharacterStatus(status);
  loadNpcs();
  loadEvents();
  loadSummaries();

  // Show/hide "load earlier" button for auto branches with truncated messages
  if (isAutoBranch(branchId) && allMessages.length < totalMessages) {
    $loadBtn.style.display = "";
    $loadBtn.onclick = async () => {
      $loadBtn.style.display = "none";
      await loadMessages(branchId);
      scrollToBottom();
    };
  } else {
    $loadBtn.style.display = "none";
  }

  if (isAutoBranch(branchId)) {
    startLivePolling(branchId);
  } else {
    stopLivePolling();
  }

  if (forcePreserve) {
    container.scrollTop = savedScrollTop;
    requestAnimationFrame(() => {
      container.scrollTop = savedScrollTop;
      $messages.style.minHeight = "";
    });
    return;
  }

  if (preserveScroll) {
    if (isAtBottom) {
      scrollToBottom();
      $messages.style.minHeight = "";
    } else {
      container.scrollTop = savedScrollTop;
      requestAnimationFrame(() => {
        container.scrollTop = savedScrollTop;
        $messages.style.minHeight = "";
      });
    }
    return;
  }

  if (scrollToIndex != null) {
    $messages.style.minHeight = "";
    const target = $messages.querySelector(`.message[data-index="${scrollToIndex}"]`);
    if (target) {
      target.scrollIntoView({ block: "center" });
    } else {
      scrollToBottom();
    }
    return;
  }
  scrollToBottom();
}

async function loadMessages(branchId, { tail } = {}) {
  const msgResult = tail
    ? await API.messages(branchId, 0, 99999, null, tail)
    : await API.messages(branchId, 0, 99999);
  totalMessages = msgResult.total;
  allMessages = msgResult.messages;
  originalCount = msgResult.original_count || originalCount;
  forkPoints = msgResult.fork_points || {};
  siblingGroups = msgResult.sibling_groups || {};
  renderMessages(allMessages);
}

// ---------------------------------------------------------------------------
// Create branch flow
// ---------------------------------------------------------------------------
async function createBranchFromIndex(msgIndex) {
  const name = prompt("為新分支命名：");
  if (!name || !name.trim()) return;

  const res = await API.createBranch(name.trim(), currentBranchId, msgIndex);
  if (res.ok && res.branch) {
    await loadBranches();
    await switchToBranch(res.branch.id);
  } else {
    alert(res.error || "建立分支失敗");
  }
}

// ---------------------------------------------------------------------------
// Edit flow
// ---------------------------------------------------------------------------
function startEditing(msgEl, msg) {
  msgEl.classList.add("editing");

  const contentEl = msgEl.querySelector(".content");
  const originalHtml = contentEl.innerHTML;

  const textarea = document.createElement("textarea");
  textarea.className = "edit-textarea";
  textarea.value = msg.content;
  textarea.rows = Math.max(3, msg.content.split("\n").length);

  const actions = document.createElement("div");
  actions.className = "edit-actions";

  const saveBtn = document.createElement("button");
  saveBtn.className = "edit-save-btn";
  saveBtn.textContent = "送出修改";

  const cancelBtn = document.createElement("button");
  cancelBtn.className = "edit-cancel-btn";
  cancelBtn.textContent = "取消";

  actions.appendChild(cancelBtn);
  actions.appendChild(saveBtn);

  contentEl.innerHTML = "";
  contentEl.appendChild(textarea);
  contentEl.appendChild(actions);
  textarea.focus();

  cancelBtn.addEventListener("click", () => {
    msgEl.classList.remove("editing");
    contentEl.innerHTML = originalHtml;
  });

  saveBtn.addEventListener("click", () => {
    const newText = textarea.value.trim();
    if (!newText) return;
    if (newText === msg.content) {
      msgEl.classList.remove("editing");
      contentEl.innerHTML = originalHtml;
      return;
    }
    submitEdit(msg, newText);
  });

  textarea.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      saveBtn.click();
    }
    if (e.key === "Escape") {
      cancelBtn.click();
    }
  });
}

async function submitEdit(msg, newText) {
  const parentBranchId = msg.owner_branch_id || currentBranchId;
  const branchPointIndex = msg.index - 1;

  // Exit editing UI immediately
  const msgEl = $messages.querySelector(`.message[data-index="${msg.index}"]`);
  if (msgEl) {
    msgEl.classList.remove("editing");
    const contentEl = msgEl.querySelector(".content");
    if (contentEl) contentEl.innerHTML = markdownToHtml(newText);
    msg.content = newText; // Update local message object to prevent stale content on re-edit
  }

  $loading.style.display = "flex";
  $sendBtn.disabled = true;

  // Truncate subsequent messages in DOM
  while (msgEl.nextSibling) {
    msgEl.nextSibling.remove();
  }

  showPlaceholderSwitcher(msgEl, msg.index);

  if (activeStreamController) {
    activeStreamController.abort();
  }
  activeStreamController = new AbortController();

  let streamingEl = null;
  let streamedText = "";

  try {
    await streamFromSSE(
      "/api/branches/edit/stream",
      {
        parent_branch_id: parentBranchId,
        branch_point_index: branchPointIndex,
        edited_message: newText,
      },
      // onChunk — show streaming text in-place
      (chunk) => {
        streamedText += chunk;
        if (!streamingEl) {
          // First chunk: hide loading, show a temporary GM bubble at the end
          $loading.style.display = "none";
          streamingEl = document.createElement("div");
          streamingEl.className = "message gm";
          const rt = document.createElement("div");
          rt.className = "role-tag";
          rt.textContent = "GM";
          const ct = document.createElement("div");
          ct.className = "content";
          streamingEl.appendChild(rt);
          streamingEl.appendChild(ct);
          $messages.appendChild(streamingEl);
          // Initial scroll to message start for editing
          msgEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
        const ct = streamingEl.querySelector(".content");
        ct.innerHTML = markdownToHtml(stripHiddenTags(streamedText));
      },
      // onDone — switch to new branch
      async (data) => {
        if (streamingEl) streamingEl.remove();
        $loading.style.display = "none";
        if (data.branch) {
          await loadBranches();
          await switchToBranch(data.branch.id, { forcePreserve: true });
        }
        activeStreamController = null;
      },
      // onError
      (errMsg) => {
        if (streamingEl) streamingEl.remove();
        $loading.style.display = "none";
        if (errMsg !== "AbortError") {
          alert(errMsg || "編輯失敗");
        }
        activeStreamController = null;
      },
      activeStreamController.signal
    );
  } catch (err) {
    if (err.name === 'AbortError') return;
    if (streamingEl) streamingEl.remove();
    $loading.style.display = "none";
    alert("網路錯誤：" + err.message);
  }

  $sendBtn.disabled = false;
}

// ---------------------------------------------------------------------------
// Regenerate flow
// ---------------------------------------------------------------------------
async function regenerateGmMessage(msg, msgEl) {
  const parentBranchId = msg.owner_branch_id || currentBranchId;
  const branchPointIndex = msg.index - 1;

  const contentEl = msgEl.querySelector(".content");
  const actionBtn = msgEl.querySelector(".msg-action-btn");
  if (contentEl) {
    contentEl.textContent = "";
  }
  if (actionBtn) actionBtn.style.display = "none";
  $sendBtn.disabled = true;

  // Truncate subsequent messages in DOM
  while (msgEl.nextSibling) {
    msgEl.nextSibling.remove();
  }

  showPlaceholderSwitcher(msgEl, msg.index);

  if (activeStreamController) {
    activeStreamController.abort();
  }
  activeStreamController = new AbortController();

  let streamedText = "";

  try {
    await streamFromSSE(
      "/api/branches/regenerate/stream",
      {
        parent_branch_id: parentBranchId,
        branch_point_index: branchPointIndex,
      },
      // onChunk — stream directly into existing bubble
      (chunk) => {
        streamedText += chunk;
        if (contentEl) contentEl.innerHTML = markdownToHtml(stripHiddenTags(streamedText));
        // Do NOT auto-scroll to bottom during regeneration to maintain context
      },
      // onDone — switch to new branch
      async (data) => {
        if (data.branch) {
          await loadBranches();
          await switchToBranch(data.branch.id, { forcePreserve: true });
        }
        activeStreamController = null;
      },
      // onError
      (errMsg) => {
        if (errMsg !== "AbortError") {
          if (contentEl) contentEl.innerHTML = markdownToHtml(msg.content);
          if (actionBtn) actionBtn.style.display = "";
          alert(errMsg || "重新生成失敗");
        }
        activeStreamController = null;
      },
      activeStreamController.signal
    );
  } catch (err) {
    if (err.name === 'AbortError') return;
    if (contentEl) contentEl.innerHTML = markdownToHtml(msg.content);
    if (actionBtn) actionBtn.style.display = "";
    alert("網路錯誤：" + err.message);
  }

  $sendBtn.disabled = false;
}

function showPlaceholderSwitcher(msgEl, index) {
  const sibKey = String(index);
  let current = 2;
  let total = 2;

  if (siblingGroups[sibKey]) {
    total = siblingGroups[sibKey].total + 1;
    current = total;
  }

  // Remove existing switcher if any
  const existing = msgEl.querySelector(".sibling-switcher");
  if (existing) existing.remove();

  msgEl.classList.add("has-switcher");
  const switcher = document.createElement("div");
  switcher.className = "sibling-switcher";

  const leftBtn = document.createElement("button");
  leftBtn.className = "sw-arrow";
  leftBtn.textContent = "\u276E";
  leftBtn.disabled = true;

  const label = document.createElement("span");
  label.className = "sw-label";
  label.textContent = `${current}/${total}`;

  const rightBtn = document.createElement("button");
  rightBtn.className = "sw-arrow";
  rightBtn.textContent = "\u276F";
  rightBtn.disabled = true;

  switcher.appendChild(leftBtn);
  switcher.appendChild(label);
  switcher.appendChild(rightBtn);
  msgEl.appendChild(switcher);
}

// ---------------------------------------------------------------------------
// Render messages
// ---------------------------------------------------------------------------
function renderMessages(messages) {
  $messages.innerHTML = "";

  let dividerInserted = false;
  const currentBranch = branches[currentBranchId];
  const branchPointIndex = currentBranch ? currentBranch.branch_point_index : null;
  let branchDividerInserted = false;

  for (const msg of messages) {
    if (!dividerInserted && currentBranchId === "main" && msg.index >= originalCount && originalCount > 0) {
      const divider = document.createElement("div");
      divider.className = "new-messages-divider";
      divider.textContent = "\u2014 新對話 \u2014";
      $messages.appendChild(divider);
      dividerInserted = true;
    }

    if (!branchDividerInserted && branchPointIndex != null && currentBranchId !== "main") {
      if (msg.index > branchPointIndex && !msg.inherited) {
        const bpDiv = document.createElement("div");
        bpDiv.className = "branch-point-divider";
        bpDiv.textContent = "\u2014 \u2442 分支起點 \u2014";
        $messages.appendChild(bpDiv);
        branchDividerInserted = true;
      }
    }

    const el = document.createElement("div");
    el.className = `message ${msg.role}`;
    if (msg.inherited) el.classList.add("inherited");
    el.dataset.index = msg.index;

    const sibKey = String(msg.index);
    const hasSwitcher = siblingGroups[sibKey] && siblingGroups[sibKey].total >= 2;
    if (hasSwitcher) el.classList.add("has-switcher");

    const roleTag = document.createElement("div");
    roleTag.className = "role-tag";
    roleTag.textContent = msg.role === "user" ? "玩家" : "GM";

    const content = document.createElement("div");
    content.className = "content";
    content.innerHTML = markdownToHtml(msg.content);

    const actionBtn = document.createElement("button");
    actionBtn.className = "msg-action-btn";
    if (msg.role === "user") {
      actionBtn.textContent = "\u270E";
      actionBtn.title = "編輯此訊息";
      actionBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        startEditing(el, msg);
      });
    } else {
      actionBtn.textContent = "\u21BB";
      actionBtn.title = "重新生成";
      actionBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        regenerateGmMessage(msg, el);
      });
    }

    el.appendChild(roleTag);
    el.appendChild(content);

    // Render image if present
    if (msg.image && currentStoryId) {
      renderMessageImage(el, msg, currentStoryId);
    }

    el.appendChild(actionBtn);

    if (hasSwitcher) {
      const group = siblingGroups[sibKey];
      const switcher = document.createElement("div");
      switcher.className = "sibling-switcher";

      const leftBtn = document.createElement("button");
      leftBtn.className = "sw-arrow";
      leftBtn.textContent = "\u276E";
      leftBtn.disabled = group.current_variant <= 1;
      leftBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        const targetIdx = group.current_variant - 2;
        if (targetIdx >= 0) {
          switchToBranch(group.variants[targetIdx].branch_id, { preserveScroll: true });
        }
      });

      const label = document.createElement("span");
      label.className = "sw-label";
      label.textContent = `${group.current_variant}/${group.total}`;

      const rightBtn = document.createElement("button");
      rightBtn.className = "sw-arrow";
      rightBtn.textContent = "\u276F";
      rightBtn.disabled = group.current_variant >= group.total;
      rightBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        const targetIdx = group.current_variant;
        if (targetIdx < group.variants.length) {
          switchToBranch(group.variants[targetIdx].branch_id, { preserveScroll: true });
        }
      });

      switcher.appendChild(leftBtn);
      switcher.appendChild(label);
      switcher.appendChild(rightBtn);
      el.appendChild(switcher);
    }

    $messages.appendChild(el);
  }
}

// ---------------------------------------------------------------------------
// Strip hidden GM tags (client-side, mirrors backend _process_gm_response)
// ---------------------------------------------------------------------------
function stripHiddenTags(text) {
  text = text.replace(/<!--STATE\s*[\s\S]*?STATE-->/g, "");
  text = text.replace(/<!--LORE\s*[\s\S]*?LORE-->/g, "");
  text = text.replace(/<!--NPC\s*[\s\S]*?NPC-->/g, "");
  text = text.replace(/<!--EVENT\s*[\s\S]*?EVENT-->/g, "");
  text = text.replace(/<!--IMG\s*[\s\S]*?IMG-->/g, "");
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

// ---------------------------------------------------------------------------
// Markdown → HTML (basic)
// ---------------------------------------------------------------------------
function markdownToHtml(text) {
  if (!text) return "";
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

// ---------------------------------------------------------------------------
// Character status panel — schema-driven
// ---------------------------------------------------------------------------
function renderCharacterStatus(state) {
  const panel = document.getElementById("char-panel");
  panel.innerHTML = "";

  const schema = characterSchema;
  if (!schema || !schema.fields) {
    const pre = document.createElement("pre");
    pre.style.fontSize = "0.8rem";
    pre.textContent = JSON.stringify(state, null, 2);
    panel.appendChild(pre);
    return;
  }

  for (const field of schema.fields) {
    const div = document.createElement("div");
    div.className = "char-field" + (field.highlight ? " highlight" : "");

    const label = document.createElement("label");
    label.textContent = field.label;

    const span = document.createElement("span");
    let val = state[field.key];
    if (val == null) {
      span.textContent = "\u2014";
    } else if (field.type === "number") {
      span.textContent = Number(val).toLocaleString() + (field.suffix || "");
    } else {
      span.textContent = String(val);
    }

    div.appendChild(label);
    div.appendChild(span);
    panel.appendChild(div);
  }

  // Render extra fields not in schema (dynamically added by GM)
  const definedKeys = new Set(schema.fields.map(f => f.key));
  const listKeys = new Set((schema.lists || []).map(l => l.key));
  // Also exclude add/remove helper keys
  const helperKeys = new Set();
  for (const l of (schema.lists || [])) {
    if (l.state_add_key) helperKeys.add(l.state_add_key);
    if (l.state_remove_key) helperKeys.add(l.state_remove_key);
  }
  for (const [key, val] of Object.entries(state)) {
    if (definedKeys.has(key) || listKeys.has(key) || helperKeys.has(key)) continue;
    if (key === "reward_points_delta") continue;
    if (val == null || typeof val === "object") continue;
    const div = document.createElement("div");
    div.className = "char-field";
    const label = document.createElement("label");
    label.textContent = key;
    const span = document.createElement("span");
    span.textContent = String(val);
    div.appendChild(label);
    div.appendChild(span);
    panel.appendChild(div);
  }

  for (const listDef of (schema.lists || [])) {
    const h3 = document.createElement("h3");
    h3.textContent = listDef.label;
    panel.appendChild(h3);

    const ul = document.createElement("ul");
    const listType = listDef.type || "list";
    const data = state[listDef.key];

    if (listType === "map" && data && typeof data === "object") {
      Object.entries(data).forEach(([name, rel]) => {
        const li = document.createElement("li");
        li.textContent = `${name}：${rel}`;
        ul.appendChild(li);
      });
    } else if (Array.isArray(data)) {
      data.forEach(item => {
        const li = document.createElement("li");
        // Support "name — description" format
        const dashIdx = item.indexOf(" — ");
        if (dashIdx > 0) {
          const nameSpan = document.createElement("span");
          nameSpan.className = "item-name";
          nameSpan.textContent = item.substring(0, dashIdx);
          const descSpan = document.createElement("span");
          descSpan.className = "item-desc";
          descSpan.textContent = item.substring(dashIdx + 3);
          li.appendChild(nameSpan);
          li.appendChild(descSpan);
        } else {
          li.textContent = item;
        }
        ul.appendChild(li);
      });
    }

    panel.appendChild(ul);
  }
}

// ---------------------------------------------------------------------------
// NPC Panel
// ---------------------------------------------------------------------------
async function loadNpcs() {
  try {
    const result = await API.npcs(currentBranchId);
    renderNpcPanel(result.npcs || []);
  } catch (e) { /* ignore */ }
}

function renderNpcPanel(npcs) {
  const panel = document.getElementById("npc-panel");
  panel.innerHTML = "";

  if (!npcs.length) {
    const empty = document.createElement("div");
    empty.className = "npc-empty";
    empty.textContent = "尚無已記錄的 NPC";
    panel.appendChild(empty);
    return;
  }

  for (const npc of npcs) {
    const card = document.createElement("div");
    card.className = "npc-card";

    const header = document.createElement("div");
    header.className = "npc-card-header";

    const name = document.createElement("span");
    name.className = "npc-name";
    name.textContent = npc.name;

    const role = document.createElement("span");
    role.className = "npc-role";
    role.textContent = npc.role || "";

    header.appendChild(name);
    header.appendChild(role);
    card.appendChild(header);

    if (npc.relationship_to_player) {
      const rel = document.createElement("div");
      rel.className = "npc-detail";
      rel.innerHTML = `<label>關係</label><span>${escapeHtml(npc.relationship_to_player)}</span>`;
      card.appendChild(rel);
    }

    if (npc.current_status) {
      const status = document.createElement("div");
      status.className = "npc-detail";
      status.innerHTML = `<label>狀態</label><span>${escapeHtml(npc.current_status)}</span>`;
      card.appendChild(status);
    }

    // Big5 personality bars
    const p = npc.personality;
    if (p && typeof p === "object") {
      const big5 = [
        ["O", "開放性", p.openness],
        ["C", "盡責性", p.conscientiousness],
        ["E", "外向性", p.extraversion],
        ["A", "親和性", p.agreeableness],
        ["N", "神經質", p.neuroticism],
      ];
      const bars = document.createElement("div");
      bars.className = "big5-bars";
      for (const [abbr, label, val] of big5) {
        if (val == null) continue;
        const row = document.createElement("div");
        row.className = "big5-bar";
        row.innerHTML = `<span class="big5-label" title="${label}">${abbr}</span><div class="big5-track"><div class="big5-fill" style="width:${val * 10}%"></div></div>`;
        bars.appendChild(row);
      }
      card.appendChild(bars);

      if (p.summary) {
        const sum = document.createElement("div");
        sum.className = "npc-personality-summary";
        sum.textContent = p.summary;
        card.appendChild(sum);
      }
    }

    if (npc.notable_traits && npc.notable_traits.length) {
      const traits = document.createElement("div");
      traits.className = "npc-traits";
      traits.textContent = npc.notable_traits.join("、");
      card.appendChild(traits);
    }

    // NPC activities (last activity) — will be populated later
    const activityEl = document.createElement("div");
    activityEl.className = "npc-last-activity";
    activityEl.id = `npc-activity-${npc.id}`;
    card.appendChild(activityEl);

    panel.appendChild(card);
  }

  // Load NPC activities and populate
  loadNpcActivities(npcs);
}

async function loadNpcActivities(npcs) {
  try {
    const result = await API.npcActivities(currentBranchId);
    const activities = result.activities || [];
    if (!activities.length) return;

    const latest = activities[activities.length - 1];
    for (const act of (latest.activities || [])) {
      const npc = npcs.find(n => n.name === act.npc_name);
      if (!npc) continue;
      const el = document.getElementById(`npc-activity-${npc.id}`);
      if (el) {
        el.textContent = `${act.activity}（${act.mood || ""}，${act.location || ""}）`;
      }
    }
  } catch (e) { /* ignore */ }
}

// ---------------------------------------------------------------------------
// Events Panel
// ---------------------------------------------------------------------------
async function loadEvents() {
  try {
    const result = await API.events(currentBranchId);
    renderEventsPanel(result.events || []);
  } catch (e) { /* ignore */ }
}

function renderEventsPanel(events) {
  const panel = document.getElementById("events-panel");
  panel.innerHTML = "";

  if (!events.length) {
    const empty = document.createElement("div");
    empty.className = "npc-empty";
    empty.textContent = "尚無追蹤事件";
    panel.appendChild(empty);
    return;
  }

  for (const ev of events) {
    const item = document.createElement("div");
    item.className = "event-item";

    const badge = document.createElement("span");
    badge.className = "event-badge";
    badge.textContent = ev.event_type || "?";

    const statusClass = {
      planted: "event-status-planted",
      triggered: "event-status-triggered",
      resolved: "event-status-resolved",
      abandoned: "event-status-abandoned",
    }[ev.status] || "";

    const statusDot = document.createElement("span");
    statusDot.className = `event-status-dot ${statusClass}`;
    statusDot.title = ev.status;

    const title = document.createElement("span");
    title.className = "event-title";
    title.textContent = ev.title;

    const desc = document.createElement("div");
    desc.className = "event-desc";
    desc.textContent = ev.description;

    item.appendChild(badge);
    item.appendChild(statusDot);
    item.appendChild(title);
    item.appendChild(desc);
    panel.appendChild(item);
  }
}

// ---------------------------------------------------------------------------
// Summary Dashboard (auto-play only)
// ---------------------------------------------------------------------------
let _lastSummaryCount = 0;

async function loadSummaries() {
  const section = document.getElementById("summary-section");
  if (!isAutoBranch(currentBranchId)) {
    section.style.display = "none";
    return;
  }
  section.style.display = "";
  try {
    const result = await API.autoPlaySummaries(currentBranchId);
    const summaries = result.summaries || [];
    _lastSummaryCount = summaries.length;
    renderSummaryDashboard(summaries);
  } catch (e) { /* ignore */ }
}

function renderSummaryDashboard(summaries) {
  const metricsEl = document.getElementById("summary-metrics");
  const timelineEl = document.getElementById("summary-timeline");
  metricsEl.innerHTML = "";
  timelineEl.innerHTML = "";

  if (!summaries.length) {
    const empty = document.createElement("div");
    empty.className = "npc-empty";
    empty.textContent = "摘要將在第 5 回合後自動生成";
    timelineEl.appendChild(empty);
    return;
  }

  // Metrics bar
  const latest = summaries[summaries.length - 1];
  const totalTurns = (latest.turn_end || 0) + 1;
  const dungeonCount = latest.dungeon_count || 0;
  const currentPhase = latest.phase || "hub";

  const bar = document.createElement("div");
  bar.className = "summary-metrics-bar";
  bar.innerHTML =
    `<div class="summary-metric"><span class="summary-metric-value">${totalTurns}</span><span class="summary-metric-label">回合</span></div>` +
    `<div class="summary-metric"><span class="summary-metric-value">${dungeonCount}</span><span class="summary-metric-label">副本</span></div>` +
    `<div class="summary-metric"><span class="summary-metric-value summary-phase-badge ${currentPhase === "dungeon" ? "dungeon" : "hub"}">${currentPhase === "dungeon" ? "副本中" : "主神空間"}</span><span class="summary-metric-label">階段</span></div>`;
  metricsEl.appendChild(bar);

  // Summary cards (newest first)
  const reversed = [...summaries].reverse();
  for (const s of reversed) {
    const card = document.createElement("div");
    card.className = "summary-card";

    const header = document.createElement("div");
    header.className = "summary-card-header";

    const turnRange = document.createElement("span");
    turnRange.className = "summary-turn-range";
    turnRange.textContent = `Turn ${s.turn_start}-${s.turn_end}`;

    const phaseBadge = document.createElement("span");
    phaseBadge.className = `summary-phase-badge ${s.phase === "dungeon" ? "dungeon" : "hub"}`;
    phaseBadge.textContent = s.phase === "dungeon" ? "副本" : "主神空間";

    header.appendChild(turnRange);
    header.appendChild(phaseBadge);
    card.appendChild(header);

    const summaryText = document.createElement("div");
    summaryText.className = "summary-text";
    summaryText.textContent = s.summary;
    card.appendChild(summaryText);

    if (s.key_events && s.key_events.length) {
      const tags = document.createElement("div");
      tags.className = "summary-events";
      for (const ev of s.key_events) {
        const tag = document.createElement("span");
        tag.className = "summary-event-tag";
        tag.textContent = ev;
        tags.appendChild(tag);
      }
      card.appendChild(tags);
    }

    timelineEl.appendChild(card);
  }
}

// ---------------------------------------------------------------------------
// Image polling
// ---------------------------------------------------------------------------
const _imagePollers = {};

function startImagePolling(storyId, filename, imgEl) {
  if (_imagePollers[filename]) return;

  const maxWait = 60000;
  const interval = 3000;
  const startTime = Date.now();

  const poll = async () => {
    if (Date.now() - startTime > maxWait) {
      delete _imagePollers[filename];
      imgEl.parentElement.querySelector(".msg-image-placeholder")?.remove();
      return;
    }
    try {
      const res = await API.imageStatus(filename);
      if (res.ready) {
        delete _imagePollers[filename];
        imgEl.src = `/api/stories/${storyId}/images/${filename}`;
        imgEl.style.display = "";
        imgEl.parentElement.querySelector(".msg-image-placeholder")?.remove();
        return;
      }
    } catch (e) { /* ignore */ }
    _imagePollers[filename] = setTimeout(poll, interval);
  };
  _imagePollers[filename] = setTimeout(poll, interval);
}

function renderMessageImage(parentEl, msg, storyId, { fresh = false } = {}) {
  if (!msg.image) return;
  const wrapper = document.createElement("div");
  wrapper.className = "msg-image-wrapper";

  const img = document.createElement("img");
  img.className = "msg-image";

  if (msg.image.ready) {
    img.src = `/api/stories/${storyId}/images/${msg.image.filename}`;
  } else {
    img.style.display = "none";
    const placeholder = document.createElement("div");
    placeholder.className = "msg-image-placeholder";
    placeholder.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div><span>生成插圖中…</span>';
    wrapper.appendChild(placeholder);
    if (fresh) {
      // Just generated — poll for completion
      startImagePolling(storyId, msg.image.filename, img);
    } else {
      // Page load — check once, don't endlessly poll
      API.imageStatus(msg.image.filename).then(res => {
        if (res.ready) {
          img.src = `/api/stories/${storyId}/images/${msg.image.filename}`;
          img.style.display = "";
          placeholder.remove();
        } else {
          wrapper.remove();
        }
      }).catch(() => {
        wrapper.remove();
      });
    }
  }

  wrapper.appendChild(img);
  parentEl.appendChild(wrapper);
}

// ---------------------------------------------------------------------------
// Send message
// ---------------------------------------------------------------------------
async function sendMessage() {
  const text = $input.value.trim();
  if (!text || isSending) return;

  isSending = true;
  $sendBtn.disabled = true;
  $input.value = "";

  const playerMsg = { role: "user", content: text, index: allMessages.length, inherited: false, owner_branch_id: currentBranchId };
  allMessages.push(playerMsg);
  appendMessage(playerMsg);
  scrollToBottom();

  // Create GM bubble immediately for streaming
  const gmMsgIndex = allMessages.length;
  const gmEl = document.createElement("div");
  gmEl.className = "message gm";
  gmEl.dataset.index = gmMsgIndex;

  const roleTag = document.createElement("div");
  roleTag.className = "role-tag";
  roleTag.textContent = "GM";

  const contentEl = document.createElement("div");
  contentEl.className = "content";
  contentEl.textContent = "";

  gmEl.appendChild(roleTag);
  gmEl.appendChild(contentEl);
  $messages.appendChild(gmEl);
  scrollToBottom();

  let streamedText = "";

  try {
    await streamFromSSE(
      "/api/send/stream",
      { message: text, branch_id: currentBranchId },
      // onChunk
      (chunk) => {
        streamedText += chunk;
        contentEl.innerHTML = markdownToHtml(stripHiddenTags(streamedText));
        smartScrollToBottom();
      },
      // onDone
      async (data) => {
        const gmMsg = data.gm_msg;
        gmMsg.inherited = false;
        gmMsg.owner_branch_id = currentBranchId;
        allMessages.push(gmMsg);

        // Replace streamed text with final markdown-rendered content
        contentEl.innerHTML = markdownToHtml(gmMsg.content);

        // Add image if present
        if (gmMsg.image && currentStoryId) {
          renderMessageImage(gmEl, gmMsg, currentStoryId, { fresh: true });
        }

        // Add regen button
        const actionBtn = document.createElement("button");
        actionBtn.className = "msg-action-btn";
        actionBtn.textContent = "\u21BB";
        actionBtn.title = "重新生成";
        actionBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          regenerateGmMessage(gmMsg, gmEl);
        });
        gmEl.appendChild(actionBtn);

        smartScrollToBottom();
        renderCharacterStatus(await API.status(currentBranchId));
        loadNpcs();
        loadEvents();
      },
      // onError
      (msg) => {
        gmEl.remove();
        appendSystemError(msg || "未知錯誤");
      }
    );
  } catch (err) {
    gmEl.remove();
    appendSystemError("網路錯誤：" + err.message);
  }

  isSending = false;
  $sendBtn.disabled = false;
  $input.focus();
}

function appendMessage(msg) {
  if (currentBranchId === "main" && msg.index === originalCount && originalCount > 0) {
    const existing = $messages.querySelector(".new-messages-divider");
    if (!existing) {
      const divider = document.createElement("div");
      divider.className = "new-messages-divider";
      divider.textContent = "\u2014 新對話 \u2014";
      $messages.appendChild(divider);
    }
  }

  const el = document.createElement("div");
  el.className = `message ${msg.role}`;
  if (msg.inherited) el.classList.add("inherited");
  el.dataset.index = msg.index;

  const roleTag = document.createElement("div");
  roleTag.className = "role-tag";
  roleTag.textContent = msg.role === "user" ? "玩家" : "GM";

  const content = document.createElement("div");
  content.className = "content";
  content.innerHTML = markdownToHtml(msg.content);

  const actionBtn = document.createElement("button");
  actionBtn.className = "msg-action-btn";
  if (msg.role === "user") {
    actionBtn.textContent = "\u270E";
    actionBtn.title = "編輯此訊息";
    actionBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      startEditing(el, msg);
    });
  } else {
    actionBtn.textContent = "\u21BB";
    actionBtn.title = "重新生成";
    actionBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      regenerateGmMessage(msg, el);
    });
  }

  el.appendChild(roleTag);
  el.appendChild(content);

  // Render image if present
  if (msg.image && currentStoryId) {
    renderMessageImage(el, msg, currentStoryId);
  }

  el.appendChild(actionBtn);
  $messages.appendChild(el);
}

function appendSystemError(text) {
  const el = document.createElement("div");
  el.className = "message gm";
  el.innerHTML = `<div class="role-tag">系統</div><div class="content"><span class="god-hint">【系統錯誤】</span> ${escapeHtml(text)}</div>`;
  $messages.appendChild(el);
  scrollToBottom();
}

// ---------------------------------------------------------------------------
// New Story Modal
// ---------------------------------------------------------------------------
function openNewStoryModal() {
  document.getElementById("ns-name").value = "";
  document.getElementById("ns-desc").value = "";
  document.getElementById("ns-prompt").value = "";
  document.getElementById("ns-schema").value = "";
  document.getElementById("ns-state").value = "";
  $storyModal.classList.remove("hidden");
}

function closeNewStoryModal() {
  $storyModal.classList.add("hidden");
}

async function createNewStory() {
  const name = document.getElementById("ns-name").value.trim();
  if (!name) {
    alert("請輸入故事名稱");
    return;
  }

  const data = {
    name,
    description: document.getElementById("ns-desc").value.trim(),
    system_prompt: document.getElementById("ns-prompt").value.trim(),
  };

  const schemaText = document.getElementById("ns-schema").value.trim();
  if (schemaText) {
    try {
      data.character_schema = JSON.parse(schemaText);
    } catch (e) {
      alert("角色 Schema JSON 格式錯誤：" + e.message);
      return;
    }
  }

  const stateText = document.getElementById("ns-state").value.trim();
  if (stateText) {
    try {
      data.default_character_state = JSON.parse(stateText);
    } catch (e) {
      alert("初始角色狀態 JSON 格式錯誤：" + e.message);
      return;
    }
  }

  try {
    const res = await API.createStory(data);
    if (res.ok && res.story) {
      closeNewStoryModal();
      await loadStories();
      await switchToStory(res.story.id);
    } else {
      alert(res.error || "建立故事失敗");
    }
  } catch (err) {
    alert("網路錯誤：" + err.message);
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
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

// ---------------------------------------------------------------------------
// Init overlay
// ---------------------------------------------------------------------------
function showInitOverlay() {
  const overlay = document.createElement("div");
  overlay.id = "init-overlay";
  overlay.innerHTML = `<div class="spinner"></div><p id="init-status">正在初始化主神空間…</p>`;
  document.body.appendChild(overlay);
}

function updateInitStatus(text) {
  const el = document.getElementById("init-status");
  if (el) el.textContent = text;
}

function removeInitOverlay() {
  const el = document.getElementById("init-overlay");
  if (el) el.remove();
}

// ---------------------------------------------------------------------------
// Event listeners
// ---------------------------------------------------------------------------
$sendBtn.addEventListener("click", sendMessage);

$input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    sendMessage();
  }
});

$input.addEventListener("input", () => {
  $input.style.height = "auto";
  $input.style.height = Math.min($input.scrollHeight, 120) + "px";
});

// New branch button — branch from the last message
$newBranchBtn.addEventListener("click", () => {
  if (allMessages.length === 0) {
    alert("沒有訊息可以分支");
    return;
  }
  const lastIndex = allMessages[allMessages.length - 1].index;
  createBranchFromIndex(lastIndex);
});

// Blank branch button — start a fresh game from scratch
$newBlankBranchBtn.addEventListener("click", async () => {
  const name = prompt("為新的空白分支命名：");
  if (!name || !name.trim()) return;
  try {
    const res = await API.createBlankBranch(name.trim());
    if (!res.ok) {
      alert(res.error || "建立空白分支失敗");
      return;
    }
    closeDrawer();
    await loadBranches();
    await switchToBranch(res.branch.id);
    // Auto-send first message to trigger GM character creation
    $input.value = "開始一個全新的冒險。請引導我創建角色（名稱、性別、背景等），然後開始故事。";
    sendMessage();
  } catch (err) {
    alert("網路錯誤：" + err.message);
  }
});

// Promote branch button
$promoteBtn.addEventListener("click", async () => {
  if (currentBranchId === "main") return;
  const branch = branches[currentBranchId];
  const name = branch ? branch.name : currentBranchId;
  if (!confirm(`確定要將分支「${name}」的內容設為主時間線嗎？`)) return;

  $promoteBtn.disabled = true;
  try {
    const res = await API.promoteBranch(currentBranchId);
    if (res.ok) {
      currentBranchId = "main";
      await loadBranches();
      await loadMessages("main");
      const status = await API.status("main");
      renderCharacterStatus(status);
      scrollToBottom();
    } else {
      alert(res.error || "設為主時間線失敗");
    }
  } catch (err) {
    alert("網路錯誤：" + err.message);
  }
  $promoteBtn.disabled = false;
});

// New story button
$newStoryBtn.addEventListener("click", () => {
  closeDrawer();
  openNewStoryModal();
});

// New story modal actions
document.getElementById("ns-cancel").addEventListener("click", closeNewStoryModal);
document.getElementById("ns-create").addEventListener("click", createNewStory);

// Close modal on overlay click
$storyModal.addEventListener("click", (e) => {
  if (e.target === $storyModal) closeNewStoryModal();
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
init();
