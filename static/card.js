/**
 * Card Mode adapter: card data and actions on the shared chat UI.
 */

(function () {
  'use strict';

  const scriptTag = document.currentScript;
  const slug = (scriptTag && scriptTag.dataset.slug) || window.location.pathname.split('/').pop();

  let currentBranch = new URLSearchParams(window.location.search).get('branch_id');
  const exploreBtn = document.getElementById('cardExploreBtn');
  const exploreControl = document.getElementById('cardExploreControl');
  const exploreSize = document.getElementById('cardExploreSize');
  const exploreSizeKey = `card:${slug}:explorationSize`;
  let explorationMaterials = [], lastExplorationId = null, explorationDraft = null;
  function updateExploreLabel() {
    if (exploreBtn) exploreBtn.textContent = {any:'探索 ✦', small:'探索·小', large:'探索·大'}[exploreSize?.value || 'any'];
  }
  if (exploreSize) {
    try { const saved = localStorage.getItem(exploreSizeKey); if (['any','small','large'].includes(saved)) exploreSize.value = saved; } catch (_) {}
    updateExploreLabel();
    exploreSize.addEventListener('change', () => {
      try { localStorage.setItem(exploreSizeKey, exploreSize.value); } catch (_) {}
      updateExploreLabel();
    });
  }
  const memoryDialog = document.getElementById('cardMemoryDialog');
  const memoryRows = document.getElementById('cardMemoryRows');
  const memoryCharacter = document.getElementById('cardMemoryCharacter');
  const memoryError = document.getElementById('cardMemoryError');
  const memorySave = document.getElementById('cardMemorySave');
  let memorySession = null, memoryBusy = false;
  const memoryChanges = () => memorySession ? memorySession.characters.flatMap(ch => ch.memories
    .filter(m => m.draft !== m.text).map(m => ({character_id:ch.id, index:m.index, text:m.draft}))) : [];
  function memoryButtons() {
    memorySave.disabled = memoryBusy || !memoryChanges().length;
    document.getElementById('cardMemoryReload').disabled = memoryBusy;
    memoryCharacter.disabled = memoryBusy;
  }
  function renderMemoryRows() {
    memoryRows.replaceChildren();
    const ch = memorySession?.characters.find(c => c.id === memoryCharacter.value);
    for (const m of [...(ch?.memories || [])].reverse()) {
      if (m.draft === null) continue;
      const row = document.createElement('div'); row.className = 'memory-editor-row';
      const field = document.createElement('textarea'); field.value = m.draft; field.maxLength = 5000;
      field.rows = Math.min(8, Math.max(2, Math.ceil(m.draft.length / 45)));
      field.setAttribute('aria-label', `記憶 ${m.index + 1}`);
      field.disabled = memoryBusy;
      field.addEventListener('input', () => {m.draft = field.value; memoryButtons();});
      const del = document.createElement('button'); del.type = 'button'; del.textContent = '×';
      del.setAttribute('aria-label', `刪除記憶 ${m.index + 1}`); del.disabled = memoryBusy;
      del.addEventListener('click', () => {m.draft = null; renderMemoryRows();});
      row.append(field, del); memoryRows.append(row);
    }
    if (!memoryRows.childElementCount) memoryRows.textContent = '目前沒有記憶';
    memoryButtons();
  }
  function setMemoryData(data, branch) {
    memorySession = {...data, branch, characters:data.characters.map(ch => ({...ch,
      memories:ch.memories.map(m => ({...m, draft:m.text}))}))};
    memoryCharacter.replaceChildren(...data.characters.map(ch => new Option(ch.name, ch.id)));
    renderMemoryRows();
  }
  async function fetchMemory(branch) {
    const res = await fetch(`/api/card/${slug}/memories?branch_id=${encodeURIComponent(branch)}`);
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || '載入失敗');
    return data;
  }
  document.getElementById('cardMemoryOpen')?.addEventListener('click', async () => {
    if (memoryBusy || isGenerating || !currentBranch) return;
    memoryBusy = true; memoryError.textContent = ''; memorySession = null; memoryRows.replaceChildren();
    memoryCharacter.replaceChildren(); memoryDialog.showModal(); memoryButtons();
    const branch = currentBranch;
    try { setMemoryData(await fetchMemory(branch), branch); }
    catch (e) { memoryError.textContent = e.message; }
    finally { memoryBusy = false; renderMemoryRows(); }
  });
  async function confirmMemoryDiscard(message) {
    const dialog = document.getElementById('cardMemoryDiscard');
    document.getElementById('cardMemoryDiscardText').textContent = message;
    dialog.returnValue = 'cancel';
    return new Promise(resolve => {
      dialog.addEventListener('close', () => resolve(dialog.returnValue === 'discard'), {once:true});
      dialog.showModal();
    });
  }
  async function closeMemory() {
    if (memoryBusy) return;
    if (memoryChanges().length && !await confirmMemoryDiscard('放棄尚未保存的記憶修改？')) return;
    memoryDialog.close();
  }
  document.getElementById('cardMemoryClose')?.addEventListener('click', closeMemory);
  memoryDialog?.addEventListener('cancel', event => {event.preventDefault(); closeMemory();});
  memoryCharacter?.addEventListener('change', renderMemoryRows);
  document.getElementById('cardMemoryReload')?.addEventListener('click', async () => {
    if (memoryBusy) return;
    if (memoryChanges().length && !await confirmMemoryDiscard('重新載入會放棄目前草稿，確定？')) return;
    memoryBusy = true; memoryButtons(); memoryError.textContent = '';
    const branch = memorySession?.branch || currentBranch;
    try { setMemoryData(await fetchMemory(branch), branch); }
    catch (e) { memoryError.textContent = e.message; }
    finally { memoryBusy = false; renderMemoryRows(); }
  });
  memorySave?.addEventListener('click', async () => {
    if (memoryBusy || !memorySession || !memoryChanges().length) return;
    const changes = memoryChanges();
    if (changes.some(c => c.text !== null && !c.text.trim())) {
      memoryError.textContent = '記憶不能空白，請用 × 刪除。'; return;
    }
    memoryBusy = true; renderMemoryRows(); memoryError.textContent = '';
    try {
      const res = await fetch(`/api/card/${slug}/memories`, {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({branch_id:memorySession.branch, revision:memorySession.revision, changes})});
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || '保存失敗');
      setMemoryData(data, memorySession.branch);
      memoryError.textContent = '已保存，並留下還原備份。';
      await loadCardState({force:true});
    } catch (e) { memoryError.textContent = `${e.message}（未套用的草稿仍在此視窗）`; }
    finally { memoryBusy = false; renderMemoryRows(); }
  });
  window.addEventListener('beforeunload', event => {
    if (memoryDialog?.open && memoryChanges().length) {event.preventDefault(); event.returnValue = '';}
  });

  const branchSelect = document.getElementById('cardBranchSelect');
  const branchDialog = document.getElementById('cardBranchDialog');
  let forkIndex = null;
  let branchAction = 'fork';
  function updateBranches(data) {
    if (!data.branch_id) return;
    currentBranch = data.branch_id;
    const url = new URL(window.location.href);
    url.searchParams.set('branch_id', currentBranch);
    history.replaceState(null, '', url);
    branchSelect?.replaceChildren(...(data.branches || []).map(b => {
      const option = new Option(b.name + (b.parent_branch_id ? ` · #${b.branch_point_index}` : ''), b.id);
      option.selected = b.id === currentBranch;
      return option;
    }));
    const active = (data.branches || []).find(b => b.id === currentBranch);
    document.getElementById('cardBranchName').textContent = active?.name || '主線';
    const deleteButton = document.getElementById('cardBranchDelete');
    if (deleteButton) deleteButton.disabled = currentBranch === 'main';
  }
  async function switchBranch() {
    if (isGenerating) return;
    setBusy(true);
    try {
      const result = await ensemblePost('branches/switch', {branch_id:branchSelect.value});
      currentBranch = result.branch_id;
      lastRenderedMessages = ''; renderDirty = true;
    } catch (e) { showToast(e.message); }
    finally { setBusy(false); await loadCardState({force:true}); }
  }
  function openBranchDialog(index, action = 'fork') {
    if (isGenerating || !currentBranch) return;
    forkIndex = index; branchAction = action;
    document.getElementById('cardBranchTitle').textContent = action === 'fork' ? `從 #${index} 建立分支` : '重新命名分支';
    document.getElementById('cardBranchInput').value = action === 'fork' ? `從 #${index} 分支` : document.getElementById('cardBranchName').textContent;
    document.getElementById('cardBranchError').textContent = '';
    branchDialog.showModal();
  }
  branchSelect?.addEventListener('change', switchBranch);
  document.getElementById('cardBranchDelete')?.addEventListener('click', async () => {
    if (isGenerating || !currentBranch || currentBranch === 'main') return;
    const target = currentBranch;
    setBusy(true);
    try {
      if (!await showConfirm(`確定刪除分支「${document.getElementById('cardBranchName').textContent}」？子分支會保留並接回上一層，刪除後切回上層分支。`)) return;
      const result = await ensemblePost('branches/delete', {branch_id:target});
      currentBranch = result.branch_id;
      lastRenderedMessages = ''; renderDirty = true;
    } catch (e) { showToast(e.message); }
    finally { setBusy(false); await loadCardState({force:true}); }
  });
  document.getElementById('cardBranchRename')?.addEventListener('click', () => openBranchDialog(null, 'rename'));
  document.getElementById('cardBranchCancel')?.addEventListener('click', () => branchDialog.close());
  document.getElementById('cardBranchForm')?.addEventListener('submit', async event => {
    event.preventDefault(); if (isGenerating) return;
    setBusy(true);
    try {
      const result = await ensemblePost(`branches/${branchAction}`, {
        name:document.getElementById('cardBranchInput').value,
        ...(branchAction === 'fork' ? {message_index:forkIndex} : {})});
      currentBranch = result.branch_id;
      branchDialog.close(); lastRenderedMessages = ''; renderDirty = true;
    } catch (e) { document.getElementById('cardBranchError').textContent = e.message; }
    finally { setBusy(false); await loadCardState({force:true}); }
  });

  const editDialog = document.getElementById('cardEditDialog');
  const editForm = document.getElementById('cardEditForm');
  const editPreferencesKey = 'card:editPreferences';
  const editPreferences = {mode:'revise', destination:'fork'};
  try {
    const saved = JSON.parse(localStorage.getItem(editPreferencesKey));
    if (['revise', 'regenerate'].includes(saved?.mode)) editPreferences.mode = saved.mode;
    if (['fork', 'overwrite'].includes(saved?.destination)) editPreferences.destination = saved.destination;
  } catch (_) {}
  let editingTurn = null;
  function openEditDialog(msg, user, hasFuture) {
    if (isGenerating) return;
    editingTurn = {index:msg.index, reply:msg.content, branch:currentBranch, hasFuture};
    document.getElementById('cardEditTitle').textContent = `編輯 #${msg.index} 回合`;
    document.getElementById('cardEditMessage').value = user.content;
    document.getElementById('cardEditInstruction').value = '';
    editForm.elements.editMode.value = editPreferences.mode;
    editForm.elements.editDestination.value = editPreferences.destination;
    document.getElementById('cardEditError').textContent = '';
    updateEditNotice(); editDialog.showModal();
  }
  function updateEditNotice() {
    document.getElementById('cardEditNotice').textContent = editForm.elements.editDestination.value === 'fork'
      ? '新版本會從此回合重新演出；原路線保留。'
      : '將替換這一輪玩家輸入與回覆，並移除其後所有對話；記憶與狀態會回到本輪開始前重新整理。';
  }
  editForm?.addEventListener('change', () => {
    editPreferences.mode = editForm.elements.editMode.value;
    editPreferences.destination = editForm.elements.editDestination.value;
    try { localStorage.setItem(editPreferencesKey, JSON.stringify(editPreferences)); } catch (_) {}
    updateEditNotice();
  });
  document.getElementById('cardEditCancel')?.addEventListener('click', () => editDialog.close());
  document.getElementById('cardEditClearMessage')?.addEventListener('click', () => {
    const input = document.getElementById('cardEditMessage');
    input.value = '';
    input.focus();
  });
  editForm?.addEventListener('submit', async event => {
    event.preventDefault();
    if (isGenerating || !editingTurn) return;
    const message = document.getElementById('cardEditMessage').value.trim();
    if (!message) return;
    const body = {branch_id:editingTurn.branch, message_index:editingTurn.index, expected_reply:editingTurn.reply,
      message, instruction:document.getElementById('cardEditInstruction').value.trim(),
      mode:editForm.elements.editMode.value, destination:editForm.elements.editDestination.value, stream:true};
    editDialog.close();
    if (!await runStream('edit', body)) {
      document.getElementById('cardEditError').textContent = '尚未套用，編輯內容已保留，可調整後重試。';
      editDialog.showModal();
    }
  });

  let TIERS = [];
  const recallModeSelect = document.getElementById('recallModeSelect');
  const recallModeKey = `card:${slug}:recallMode`;
  if (recallModeSelect) {
    try {
      const saved = localStorage.getItem(recallModeKey);
      if (['off', 'auto', 'deep'].includes(saved)) recallModeSelect.value = saved;
    } catch (_) {}
    recallModeSelect.addEventListener('change', () => {
      try { localStorage.setItem(recallModeKey, recallModeSelect.value); } catch (_) {}
    });
  }
  const narrativeStyleSelect = document.getElementById('narrativeStyleSelect');
  const narrativeStyleKey = `card:${slug}:narrativeStyle`;
  if (narrativeStyleSelect) {
    try {
      const saved = localStorage.getItem(narrativeStyleKey);
      if (['original', 'natural'].includes(saved)) narrativeStyleSelect.value = saved;
    } catch (_) {}
    narrativeStyleSelect.addEventListener('change', () => {
      try { localStorage.setItem(narrativeStyleKey, narrativeStyleSelect.value); } catch (_) {}
    });
  }
  const plotModeSelect = document.getElementById('plotModeSelect');
  const sceneModeSelect = document.getElementById('sceneModeSelect');
  for (const [select, key, allowed] of [
    [plotModeSelect, 'plotMode', ['follow', 'explore', 'coauthor', 'legacy_20260908']],
    [sceneModeSelect, 'sceneMode', ['default', 'intimate']],
  ]) {
    if (!select) continue;
    try {
      const saved = localStorage.getItem(`card:${slug}:${key}`);
      if (allowed.includes(saved)) select.value = saved;
    } catch (_) {}
    select.addEventListener('change', () => {
      try { localStorage.setItem(`card:${slug}:${key}`, select.value); } catch (_) {}
    });
  }
  const responseLengthSelect = document.getElementById('responseLengthSelect');
  const responseLengthKey = `card:${slug}:responseLength`;
  const responseLengthButtons = document.querySelectorAll('#cardResponseLength [data-length]');
  function syncResponseLengthButtons() {
    responseLengthButtons.forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.length === responseLengthSelect?.value));
    });
  }
  if (responseLengthSelect) {
    try {
      const saved = localStorage.getItem(responseLengthKey);
      if (['short', 'medium', 'long', 'unlimited'].includes(saved)) responseLengthSelect.value = saved;
    } catch (_) {}
    responseLengthSelect.addEventListener('change', () => {
      try { localStorage.setItem(responseLengthKey, responseLengthSelect.value); } catch (_) {}
      syncResponseLengthButtons();
    });
    responseLengthButtons.forEach(button => button.addEventListener('click', () => {
      if (responseLengthSelect.disabled) return;
      responseLengthSelect.value = button.dataset.length;
      responseLengthSelect.dispatchEvent(new Event('change'));
    }));
    syncResponseLengthButtons();
  }

  // DOM 元素
  const messageList = document.getElementById('message-list');
  const messagesScroll = document.getElementById('messages');
  const userInput = document.getElementById('user-input');
  const sendBtn = document.getElementById('send-btn');
  const clearDraftBtn = document.getElementById('cardClearDraft');
  const resetBtn = document.getElementById('reset-btn');
  const drawerResetBtn = document.getElementById('drawerResetBtn');
  const loadingIndicator = document.getElementById('loading-indicator');
  const loadingText = document.getElementById('loading-text');

  // Header & Drawer 元素
  const drawerToggleBtn = document.getElementById('drawer-toggle-btn');
  const drawerCloseBtn = document.getElementById('drawer-close-btn');
  const drawerOverlay = document.getElementById('drawer-overlay');
  const drawer = document.getElementById('drawer');
  const stateHud = document.getElementById('state-hud');
  const hudTier = document.getElementById('hudTier');
  const hudClock = document.getElementById('hudClock');

  const navCharName = document.getElementById('navCharName');
  const sidebarName = document.getElementById('sidebarName');
  const sidebarRole = document.getElementById('sidebarRole');
  const sidebarPitch = document.getElementById('sidebarPitch');
  const sidebarTraits = document.getElementById('sidebarTraits');
  const sidebarFetishes = document.getElementById('sidebarFetishes');
  const sidebarSensitiveSpots = document.getElementById('sidebarSensitiveSpots');
  const drawerTierBadge = document.getElementById('drawerTierBadge');
  const ladderBar = document.getElementById('ladderBar');
  const currentTierNote = document.getElementById('currentTierNote');
  const clockTime = document.getElementById('clockTime');
  const clockLocation = document.getElementById('clockLocation');
  const clockWeather = document.getElementById('clockWeather');
  const memoryList = document.getElementById('memoryList');
  const inventoryList = document.getElementById('inventoryList');

  let isGenerating = false;
  function syncClearDraft() {
    if (clearDraftBtn) clearDraftBtn.disabled = isGenerating || !userInput.value.length;
  }
  userInput.addEventListener('input', syncClearDraft);
  clearDraftBtn?.addEventListener('click', () => {
    if (isGenerating) return;
    userInput.value = '';
    userInput.scrollTop = 0;
    explorationDraft = null;
    userInput.dispatchEvent(new Event('input', {bubbles:true}));
  });
  let currentMessages = [];
  let ensembleState = null;
  const focusSelect = document.getElementById('ensembleFocus');
  const presenceSelect = document.getElementById('ensemblePresence');
  const ensembleDialog = document.getElementById('ensembleDialog');
  const ensembleForm = document.getElementById('ensembleForm');
  let addingCast = false;
  function updateEnsemble(data) {
    ensembleState = data.ensemble ? data : null;
    document.getElementById('ensembleControls').hidden = !ensembleState;
    document.getElementById('ensembleCreateBtn').hidden = !!ensembleState;
    const recording = document.getElementById('ensembleRecording');
    const retry = document.getElementById('ensembleRetryRecording');
    const status = data.recorder?.status;
    if (recording) {
      recording.hidden = !ensembleState || !status || status === 'done';
      document.getElementById('ensembleRecordingText').textContent = status === 'error'
        ? '記憶整理失敗，對話已保存。' : '記憶整理中，可以繼續對話。';
      retry.hidden = status !== 'error';
      retry.disabled = isGenerating;
    }
    if (!ensembleState) return;
    focusSelect.replaceChildren(...data.cast.map(c => new Option(c.name, c.id)));
    focusSelect.value = data.focus;
    presenceSelect.value = data.cast.find(c => c.id === data.focus).presence;
    document.getElementById('ensembleCast').textContent = data.cast.map(c =>
      `${c.name} · ${{present:'在場',remote:'通訊中',offstage:'離場'}[c.presence]} · ${c.relationship.tier} · ${c.status || '尚無近況'}`).join('\n');
  }
  async function ensemblePost(action, body) {
    const res = await fetch(`/api/card/${slug}/${action}`, {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({branch_id:currentBranch, ...body})
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || '操作失敗');
    return data;
  }
  document.getElementById('ensembleRetryRecording')?.addEventListener('click', async () => {
    if (isGenerating) return;
    setBusy(true);
    try { await ensemblePost('retry-recording', {}); }
    catch (err) { showToast(err.message); }
    finally { setBusy(false); await loadCardState({force:true}); }
  });
  async function changeFocus() {
    if (isGenerating) return;
    const cid = focusSelect.value;
    const presence = ensembleState.cast.find(c => c.id === cid).presence;
    setBusy(true);
    try { await ensemblePost('focus', {character_id:cid, presence}); }
    catch (e) { showToast(e.message); }
    finally { setBusy(false); await loadCardState({force:true}); }
  }
  focusSelect?.addEventListener('change', changeFocus);
  presenceSelect?.addEventListener('change', async () => {
    if (isGenerating) return;
    setBusy(true);
    try { await ensemblePost('focus', {character_id:focusSelect.value, presence:presenceSelect.value}); }
    catch (e) { showToast(e.message); }
    finally { setBusy(false); await loadCardState({force:true}); }
  });
  function openCastForm(add) {
    if (isGenerating) return;
    addingCast = add; ensembleForm.reset();
    document.getElementById('ensembleIdLabel').hidden = add;
    ensembleForm.elements.target_slug.required = !add;
    ensembleForm.elements.target_slug.value = `${slug.slice(0,22)}_cast_${Date.now().toString(36).slice(-6)}`;
    document.getElementById('ensembleFormTitle').textContent = add ? '新增固定角色' : '建立多角色副本';
    document.getElementById('ensembleFormError').textContent = '';
    ensembleDialog.showModal();
  }
  document.getElementById('ensembleCreateBtn')?.addEventListener('click', () => openCastForm(false));
  document.getElementById('ensembleAddBtn')?.addEventListener('click', () => openCastForm(true));
  document.getElementById('ensembleCancel')?.addEventListener('click', () => ensembleDialog.close());
  ensembleForm?.addEventListener('submit', async event => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(ensembleForm));
    const target = values.target_slug; delete values.target_slug;
    values.memories = values.memories.split('\n').map(s => s.trim()).filter(Boolean);
    const submit = ensembleForm.querySelector('[type="submit"]'); submit.disabled = true;
    try {
      const result = await ensemblePost(addingCast ? 'add' : 'ensemble', addingCast ? values : {target_slug:target, character:values});
      ensembleDialog.close();
      if (addingCast) await loadCardState({force:true});
      else window.location.href = result.url;
    } catch(e) { document.getElementById('ensembleFormError').textContent = e.message; }
    finally { submit.disabled = false; }
  });

  // ---------------------------------------------------------------------------
  // 抽屜 (Drawer) 開關控制
  // ---------------------------------------------------------------------------

  function openDrawer() {
    setChatDrawerOpen(true);
  }

  function closeDrawer() {
    setChatDrawerOpen(false);
  }

  if (drawerToggleBtn) drawerToggleBtn.addEventListener('click', openDrawer);
  if (stateHud) stateHud.addEventListener('click', openDrawer);
  if (drawerCloseBtn) drawerCloseBtn.addEventListener('click', closeDrawer);
  if (drawerOverlay) drawerOverlay.addEventListener('click', closeDrawer);

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && drawer && !drawer.classList.contains('closed')) {
      closeDrawer();
    }
  });

  // ---------------------------------------------------------------------------
  // 狀態刷新：不覆蓋串流或跳動閱讀位置
  // ---------------------------------------------------------------------------

  let stateEpoch = 0;
  let refreshTimer;
  let lastRenderedMessages = '';
  let renderDirty = false;
  let stateLoading = false;

  async function loadCardState({ force = false } = {}) {
    if (stateLoading || (isGenerating && !force)) return;
    const epoch = stateEpoch;
    stateLoading = true;
    try {
      const res = await fetch(`/api/card/${slug}/state${currentBranch ? "?branch_id="+encodeURIComponent(currentBranch) : ""}`, { cache: 'no-store' });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
      if (epoch !== stateEpoch || (isGenerating && !force)) return;
      TIERS = data.relationship_tiers || [];
      updateBranches(data);
      updateSidebar(data);
      updateEnsemble(data);
      updateInstruction(data.persistent_instruction);
      const continueBtn = document.getElementById('cardContinueBtn');
      if (continueBtn) continueBtn.disabled = isGenerating;
      explorationMaterials = (data.exploration_materials || []).filter(m => m.id && m.seed);
      if (exploreBtn) {
        exploreControl.hidden = !explorationMaterials.length;
        exploreSize.disabled = isGenerating || !currentBranch || !explorationMaterials.length;
        exploreBtn.disabled = isGenerating || !currentBranch || !explorationMaterials.length;
      }
      const signature = JSON.stringify(data.messages || []);
      if (signature !== lastRenderedMessages || force || renderDirty) {
        const follow = !lastRenderedMessages || messagesScroll.scrollHeight - messagesScroll.scrollTop - messagesScroll.clientHeight < 100;
        const top = messagesScroll.scrollTop;
        currentMessages = data.messages || [];
        renderMessages(currentMessages);
        lastRenderedMessages = signature;
        renderDirty = false;
        if (follow) scrollToBottom();
        else messagesScroll.scrollTop = top;
      }
    } catch (err) {
      if (force || !lastRenderedMessages) showToast('載入失敗：' + err.message);
    } finally {
      stateLoading = false;
    }
  }

  function personaText(value) {
    return Array.isArray(value) ? value.join("\n") : (value || "");
  }

  function updateSidebar(data) {
    const card = data.card || {};
    const persona = card.persona || {};
    const charName = card.display_name || card.name || '未知角色';
    const charRole = personaText(persona.identity) || card.role || '';

    if (navCharName) navCharName.textContent = charName;
    const shortName = document.getElementById('navCharShort');
    if (shortName) shortName.textContent = charName;
    if (sidebarName) sidebarName.textContent = `${charName} 角色檔案`;
    if (sidebarRole) sidebarRole.textContent = charRole;
    if (sidebarPitch) sidebarPitch.textContent = card.pitch || '';
    const playerProfile = document.getElementById('playerProfile');
    const playerProfileSection = document.getElementById('playerProfileSection');
    const profile = typeof card.player?.profile === 'string' ? card.player.profile.trim() : '';
    if (playerProfile) playerProfile.textContent = profile;
    if (playerProfileSection) playerProfileSection.hidden = !profile;

    // 角色外觀與特徵
    if (sidebarTraits) {
      const traits = [];
      if (persona.appearance) {
        traits.push(`<strong>外觀：</strong>${escapeHtml(personaText(persona.appearance)).replace(/\n/g, "<br>")}`);
      }
      if (persona.body_details && persona.body_details.length) {
        traits.push(`<strong>肉體細節：</strong>${escapeHtml(persona.body_details.join('、'))}`);
      }
      if (persona.verbal_tics && persona.verbal_tics.length) {
        traits.push(`<strong>口癖：</strong>「${escapeHtml(persona.verbal_tics.join('」、「'))}」`);
      }
      if (persona.mannerisms && persona.mannerisms.length) {
        traits.push(`<strong>習慣小動作：</strong>${escapeHtml(persona.mannerisms.join('、'))}`);
      }
      sidebarTraits.innerHTML = traits.join('<br>') || '<div class="card-empty-hint">無外觀細節</div>';
    }

    // 性癖與偏好情境
    if (sidebarFetishes) {
      const fetishes = persona.fetishes || [];
      if (fetishes.length === 0) {
        sidebarFetishes.innerHTML = '<div class="card-empty-hint">尚無記載特殊性癖</div>';
      } else {
        sidebarFetishes.innerHTML = fetishes.map(f => `
          <div class="card-tag-badge fetish-badge">🔥 ${escapeHtml(f)}</div>
        `).join('');
      }
    }

    // 生理敏感點與弱點
    if (sidebarSensitiveSpots) {
      const spots = persona.sensitive_spots || [];
      if (spots.length === 0) {
        sidebarSensitiveSpots.innerHTML = '<div class="card-empty-hint">尚無記載敏感部位</div>';
      } else {
        sidebarSensitiveSpots.innerHTML = spots.map(s => `
          <div class="card-tag-badge spot-badge">⚡ ${escapeHtml(s)}</div>
        `).join('');
      }
    }

    // 關係階梯更新
    const rel = data.relationship || (data.state && data.state.relationship) || {};
    const currentTier = rel.tier || '陌生';
    const tierIdx = TIERS.indexOf(currentTier);

    if (hudTier) hudTier.textContent = currentTier;
    if (drawerTierBadge) drawerTierBadge.textContent = currentTier;

    if (ladderBar) {
      ladderBar.innerHTML = TIERS.map((tier, idx) => {
        let cls = 'ladder-step';
        if (idx < tierIdx) cls += ' passed';
        if (idx === tierIdx) cls += ' active';
        return `<div class="${cls}" title="${tier}"></div>`;
      }).join('');
    }

    if (currentTierNote) {
      const ladderConf = (card.relationship_ladder && card.relationship_ladder[currentTier]) || {};
      const unlockReq = ladderConf.unlock_requirement || '';
      const notes = ladderConf.notes || rel.note || '';
      const desc = [unlockReq ? `解鎖要求: ${unlockReq}` : '', notes].filter(Boolean).join(' · ');
      currentTierNote.textContent = desc || (tierIdx >= 0 ? `當前處於「${currentTier}」階段` : '');
    }

    if (card.relationship_enabled === false && !data.galgame) {
      if (hudTier) hudTier.textContent = '關係依劇情發展';
      if (drawerTierBadge) drawerTierBadge.textContent = '標籤已停用';
      if (ladderBar) ladderBar.innerHTML = '';
      if (currentTierNote) currentTierNote.textContent = '';
    }

    let galgamePanel = document.getElementById("galgamePanel");
    if (!galgamePanel && currentTierNote) {
      galgamePanel = document.createElement("div");
      galgamePanel.id = "galgamePanel";
      galgamePanel.className = "galgame-panel";
      currentTierNote.after(galgamePanel);
    }
    if (galgamePanel) {
      const game = data.galgame;
      galgamePanel.hidden = !game;
      if (game) {
        if (hudTier) hudTier.textContent = `${game.stage} · 好感 ${game.affinity}`;
        if (drawerTierBadge) drawerTierBadge.textContent = game.stage;
        if (ladderBar) ladderBar.innerHTML = "";
        if (currentTierNote) currentTierNote.textContent = "慢慢相處，每個劇情日最多增加 3 點好感";
        galgamePanel.innerHTML = [["好感",game.affinity],["信任",game.trust],["反感",game.resentment],["性慾",game.desire]].map(([label,value]) =>
          `<label>${label} <span>${Number(value)} / 100</span><progress aria-label="${label}" max="100" value="${Number(value)}"></progress></label>`
        ).join("") + `<p>${escapeHtml(game.last_event?.reason || "")}</p><details><summary>相處界線</summary><p>達標只代表可以考慮，仍需她當下願意。禮貌詢問不會因未達標扣分；擅自行動會。</p>${game.boundaries.map(b => `<div>${escapeHtml(b.label)}：好感 ${b.affinity}・信任 ${b.trust} ${b.eligible ? "可考慮" : "尚未達標"}</div>`).join("")}<p>反感 15 起戒備，40 起厭惡。道歉不直接加分，後續尊重才能慢慢修復。</p></details>`;
      }
    }

    // 時鐘環境更新
    const clock = data.clock || (data.state && data.state.world_clock) || {};
    const timeStr = clock.time_str || clock.time || '--:--';
    const locStr = clock.location || (card.setting && card.setting.place) || '--';
    const weatherStr = clock.weather ? `${clock.weather}` : '--';

    if (clockTime) clockTime.textContent = timeStr;
    if (clockLocation) clockLocation.textContent = locStr;
    if (clockWeather) clockWeather.textContent = weatherStr;
    if (hudClock) hudClock.textContent = `${timeStr !== '--:--' ? timeStr.split(' ')[1] || timeStr : ''} ${weatherStr !== '--' ? weatherStr : ''}`.trim() || '--:--';

    // 長期記憶更新
    if (memoryList) {
      const mems = data.memories || (data.state && data.state.long_term_memory) || [];
      if (mems.length === 0) {
        memoryList.innerHTML = '<div class="memory-item-empty">尚無長期記憶</div>';
      } else {
        memoryList.innerHTML = mems.map(m => `
          <div class="memory-item-card">
            <div>${escapeHtml(m)}</div>
          </div>
        `).join('');
      }
    }

    // 隨身物更新
    if (inventoryList) {
      const inv = data.inventory || (data.state && data.state.inventory) || {};
      const keys = Object.keys(inv).filter(k => inv[k] && inv[k] !== 'destroyed' && inv[k] !== 'lost');
      if (keys.length === 0) {
        inventoryList.innerHTML = '<span class="inv-chip empty">（身上空無一物）</span>';
      } else {
        inventoryList.innerHTML = keys.map(k => `
          <span class="inv-chip" title="${escapeHtml(inv[k]).replace(/"/g, '&quot;')}">${escapeHtml(k)}: ${escapeHtml(inv[k])}</span>
        `).join('');
      }
    }
  }

  // ---------------------------------------------------------------------------
  // 訊息列表渲染
  // ---------------------------------------------------------------------------

  function cardMessageContent(text) {
    // Both the specified emoji header and older bracket headers are supported.
    const match = (text || '').match(/^(?:📅[^\n]+|\[[^\n]*\])(?:\n|$)/);
    if (!match) return markdownToHtml(text);
    return `<div class="msg-turn-chip">${escapeHtml(match[0].trim())}</div>` + markdownToHtml(text.slice(match[0].length));
  }

  let displayedMessages = [];
  const instructionDialog = document.getElementById('cardInstructionDialog');
  const instructionOpen = document.getElementById('cardInstructionOpen');
  const instructionText = document.getElementById('cardInstructionText');
  const instructionEnabled = document.getElementById('cardInstructionEnabled');
  const instructionError = document.getElementById('cardInstructionError');
  const instructionSave = document.getElementById('cardInstructionSave');
  let instructionSession = null, instructionBusy = false;
  function updateInstruction(config) {
    instructionOpen.textContent = '指示';
    instructionOpen.classList.toggle('active', Boolean(config?.enabled));
    instructionOpen.setAttribute('aria-pressed', String(Boolean(config?.enabled)));
    instructionOpen.title = config?.enabled ? config.text.slice(0,120) : '為目前分支保存後續演出要求';
    instructionOpen.disabled = isGenerating || !currentBranch;
  }
  const instructionDirty = () => instructionSession?.config && (
    instructionText.value !== instructionSession.config.text || instructionEnabled.checked !== instructionSession.config.enabled);
  function instructionButtons() {
    instructionText.disabled = instructionEnabled.disabled = instructionBusy;
    ['cardInstructionClear','cardInstructionReload','cardInstructionClose'].forEach(id => document.getElementById(id).disabled = instructionBusy);
    instructionSave.disabled = instructionBusy || !instructionDirty();
  }
  function confirmInstructionDiscard(text) {
    const dialog = document.getElementById('cardInstructionDiscard');
    document.getElementById('cardInstructionDiscardText').textContent = text;
    return new Promise(resolve => {
      dialog.addEventListener('close', () => resolve(dialog.returnValue === 'discard'), {once:true});
      dialog.showModal();
    });
  }
  async function fetchInstruction(branch) {
    instructionBusy = true; instructionButtons(); instructionError.textContent = '';
    try {
      const response = await fetch(`/api/card/${slug}/instruction?branch_id=${encodeURIComponent(branch)}`, {cache:'no-store'});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || '持續指示載入失敗');
      instructionSession = {branch,config:data.persistent_instruction};
      instructionText.value = instructionSession.config.text;
      instructionEnabled.checked = instructionSession.config.enabled;
    } catch(e) {instructionError.textContent = e.message;}
    finally {instructionBusy = false; instructionButtons();}
  }
  instructionOpen?.addEventListener('click', async () => {
    if (isGenerating || instructionBusy || !currentBranch) return;
    instructionSession = {branch:currentBranch,config:null}; instructionText.value = ''; instructionEnabled.checked = false;
    instructionDialog.showModal(); await fetchInstruction(currentBranch); instructionText.focus();
  });
  async function closeInstruction() {
    if (instructionBusy) return;
    if (instructionDirty() && !await confirmInstructionDiscard('持續指示尚未保存，要放棄修改嗎？')) return;
    instructionDialog.close(); instructionSession = null;
  }
  document.getElementById('cardInstructionClose')?.addEventListener('click', closeInstruction);
  instructionDialog?.addEventListener('cancel', event => {event.preventDefault(); closeInstruction();});
  instructionText?.addEventListener('input', instructionButtons);
  instructionEnabled?.addEventListener('change', instructionButtons);
  document.getElementById('cardInstructionClear')?.addEventListener('click', () => {
    instructionText.value = ''; instructionEnabled.checked = false; instructionButtons(); instructionText.focus();
  });
  document.getElementById('cardInstructionReload')?.addEventListener('click', async () => {
    if (instructionBusy) return;
    if (instructionDirty() && !await confirmInstructionDiscard('重新載入會放棄目前草稿，要繼續嗎？')) return;
    await fetchInstruction(instructionSession?.branch || currentBranch);
  });
  document.getElementById('cardInstructionForm')?.addEventListener('submit', async event => {
    event.preventDefault(); if (instructionBusy || !instructionDirty()) return;
    const session = instructionSession;
    if (instructionEnabled.checked && !instructionText.value.trim()) {
      instructionError.textContent = '請填寫持續指示，或關閉此功能'; return;
    }
    instructionBusy = true; instructionButtons(); instructionError.textContent = '';
    try {
      const result = await ensemblePost('instruction', {branch_id:session.branch, text:instructionText.value,
        enabled:instructionEnabled.checked, expected_revision:session.config.revision});
      instructionDialog.close(); instructionSession = null;
      if (session.branch === currentBranch) {updateInstruction(result.persistent_instruction); await loadCardState({force:true});}
    } catch(e) {instructionError.textContent = e.message;}
    finally {instructionBusy = false; instructionButtons();}
  });
  const feedbackDialog = document.getElementById('cardFeedbackDialog');
  const feedbackComment = document.getElementById('cardFeedbackComment');
  const feedbackSave = document.getElementById('cardFeedbackSave');
  const feedbackError = document.getElementById('cardFeedbackError');
  const feedbackPending = new Set();
  let feedbackEditor = null, feedbackSaving = false;
  const feedbackKey = target => `${target.branch}:${target.feedback.version_id}`;
  function feedbackBody(target) {
    return {branch_id:target.branch, message_index:target.index,
      version_id:target.feedback.version_id, ...(target.group ? {group_id:target.group} : {})};
  }
  async function refreshFeedback(target, result) {
    target.feedback = result.feedback;
    if (target.version) target.version.feedback = result.feedback;
    if (target.branch === currentBranch) {
      const msg = displayedMessages.find(m => m.feedback?.version_id === result.feedback.version_id);
      if (msg) msg.feedback = result.feedback;
      renderDirty = true;
      await loadCardState({force:true});
    }
    if (candidateDialog.open) renderCandidate();
  }
  function feedbackControls(target) {
    const container = document.createElement('span'); container.className = 'card-feedback-controls';
    const thumb = document.createElement('button');
    thumb.type = 'button'; thumb.className = 'msg-action-btn card-feedback-like';
    thumb.textContent = '👍'; thumb.title = target.feedback.liked ? '取消讚' : '喜歡這次演出';
    thumb.setAttribute('aria-pressed', String(target.feedback.liked));
    thumb.disabled = isGenerating || feedbackPending.has(feedbackKey(target));
    thumb.addEventListener('click', async () => {
      const key = feedbackKey(target);
      if (isGenerating || feedbackPending.has(key)) return;
      feedbackPending.add(key); thumb.disabled = true;
      try {
        const result = await ensemblePost('feedback', {...feedbackBody(target), liked:!target.feedback.liked});
        await refreshFeedback(target, result);
      } catch(e) { showToast(e.message); }
      finally {
        feedbackPending.delete(key);
        // The state refresh may have replaced this control while the request ran.
        document.querySelectorAll('.card-feedback-like').forEach(button => {
          if (button.dataset.feedbackKey === key) button.disabled = isGenerating;
        });
      }
    });
    thumb.dataset.feedbackKey = feedbackKey(target); container.appendChild(thumb);
    if (target.feedback.liked || target.feedback.comment) {
      const note = document.createElement('button');
      note.type = 'button'; note.className = 'msg-action-btn card-feedback-note';
      note.textContent = target.feedback.comment ? '查看／編輯註記' : '加註';
      note.disabled = isGenerating; note.addEventListener('click', () => openFeedback(target));
      container.appendChild(note);
    }
    return container;
  }
  function openFeedback(target) {
    if (feedbackSaving || isGenerating) return;
    feedbackEditor = {...target, feedback:{...target.feedback}};
    document.getElementById('cardFeedbackTitle').textContent = `回覆 #${target.index} 的演出註記`;
    feedbackComment.value = target.feedback.comment || ''; feedbackError.textContent = '';
    feedbackSave.disabled = true; feedbackDialog.showModal(); feedbackComment.focus();
  }
  function feedbackDirty() {return feedbackEditor && feedbackComment.value !== feedbackEditor.feedback.comment;}
  function closeFeedback() {
    if (feedbackSaving) return;
    if (feedbackDirty()) document.getElementById('cardFeedbackDiscard').showModal();
    else {feedbackDialog.close(); feedbackEditor = null;}
  }
  feedbackComment?.addEventListener('input', () => {feedbackSave.disabled = feedbackSaving || !feedbackDirty();});
  document.getElementById('cardFeedbackClose')?.addEventListener('click', closeFeedback);
  document.getElementById('cardFeedbackCancel')?.addEventListener('click', closeFeedback);
  feedbackDialog?.addEventListener('cancel', event => {event.preventDefault(); closeFeedback();});
  document.getElementById('cardFeedbackDiscard')?.addEventListener('close', event => {
    if (event.target.returnValue === 'discard') {feedbackDialog.close(); feedbackEditor = null;}
  });
  document.getElementById('cardFeedbackForm')?.addEventListener('submit', async event => {
    event.preventDefault(); if (!feedbackEditor || feedbackSaving || !feedbackDirty()) return;
    const target = feedbackEditor;
    feedbackSaving = true; feedbackSave.disabled = true; feedbackComment.disabled = true; feedbackError.textContent = '';
    try {
      const result = await ensemblePost('feedback', {...feedbackBody(target), comment:feedbackComment.value,
        expected_comment_revision:target.feedback.comment_revision ?? null});
      feedbackDialog.close(); feedbackEditor = null;
      await refreshFeedback(target, result);
    } catch(e) {feedbackError.textContent = e.message;}
    finally {feedbackSaving = false; feedbackComment.disabled = false; feedbackSave.disabled = !feedbackDirty();}
  });
  const replyEditDialog = document.getElementById('cardReplyEditDialog');
  const replyEditText = document.getElementById('cardReplyEditText');
  const replyEditSave = document.getElementById('cardReplyEditSave');
  const replyEditError = document.getElementById('cardReplyEditError');
  let replyEditSession = null, replyEditBusy = false;
  function replyEditDirty() {return replyEditSession && replyEditText.value !== replyEditSession.content;}
  function replyEditButtons() {
    replyEditSave.disabled = replyEditBusy || !replyEditDirty() || !replyEditText.value.trim();
    replyEditText.disabled = replyEditBusy;
    document.getElementById('cardReplyEditClose').disabled = replyEditBusy;
    document.getElementById('cardReplyEditCancel').disabled = replyEditBusy;
  }
  function openReplyEdit(msg) {
    if (isGenerating || replyEditBusy || !currentBranch || displayedMessages.at(-1) !== msg) return;
    replyEditSession = {branch:currentBranch, index:msg.index, content:msg.content,
      version:msg.feedback?.version_id || msg.candidate_summary?.selected_id};
    replyEditText.value = msg.content; replyEditError.textContent = '';
    document.getElementById('cardReplyEditTitle').textContent = `編輯回覆 #${msg.index} 正文`;
    replyEditButtons(); replyEditDialog.showModal(); replyEditText.focus();
  }
  async function closeReplyEdit() {
    if (replyEditBusy) return;
    if (replyEditDirty() && !await confirmMemoryDiscard('正文尚未保存，要放棄修改嗎？')) return;
    replyEditDialog.close(); replyEditSession = null;
  }
  replyEditText?.addEventListener('input', replyEditButtons);
  document.getElementById('cardReplyEditClose')?.addEventListener('click', closeReplyEdit);
  document.getElementById('cardReplyEditCancel')?.addEventListener('click', closeReplyEdit);
  replyEditDialog?.addEventListener('cancel', event => {event.preventDefault(); closeReplyEdit();});
  document.getElementById('cardReplyEditForm')?.addEventListener('submit', async event => {
    event.preventDefault();
    if (!replyEditSession || replyEditBusy || !replyEditDirty() || !replyEditText.value.trim()) return;
    const session = replyEditSession;
    replyEditBusy = true; setBusy(true); replyEditButtons(); replyEditError.textContent = '';
    try {
      await ensemblePost('candidates', {branch_id:session.branch, action:'edit-text', message_index:session.index,
        expected_version_id:session.version, expected_reply:session.content, content:replyEditText.value});
      replyEditDialog.close(); replyEditSession = null; lastRenderedMessages = ''; renderDirty = true;
    } catch(e) {replyEditError.textContent = e.message;}
    finally {
      replyEditBusy = false; setBusy(false); replyEditButtons();
      await loadCardState({force:true});
    }
  });

  const candidateDialog = document.getElementById('cardCandidateDialog');
  let candidateSession = null, candidatePosition = 0, candidateBusy = false;
  function renderCandidate() {
    const session = candidateSession;
    const version = session?.versions[candidatePosition];
    document.getElementById('cardCandidateNumber').textContent = version
      ? `${session.favorites ? '收藏' : '第'} ${candidatePosition + 1}／${session.versions.length} ${session.favorites ? '' : '版'}${version.selected ? ' · 目前採用' : ''}` : '尚無收藏';
    document.getElementById('cardCandidatePrev').disabled = candidateBusy || candidatePosition <= 0;
    document.getElementById('cardCandidateNext').disabled = candidateBusy || !session || candidatePosition >= session.versions.length - 1;
    document.getElementById('cardCandidateFavorite').disabled = candidateBusy || !version;
    document.getElementById('cardCandidateFavorite').textContent = version?.favorite ? '★ 已收藏' : '☆ 收藏';
    const apply = document.getElementById('cardCandidateApply');
    apply.disabled = candidateBusy || !version || (!session.favorites && version.selected);
    apply.textContent = session?.favorites ? '查看此回合版本' : session?.latest ? '採用這版' : '從這版建立分支';
    document.getElementById('cardCandidateStats').textContent = session?.favorites
      ? '本路線收藏的回覆，包含未採用或已回退的版本。'
      : session ? `已記錄重生 ${session.regen_count} 次${session.failed_count ? `，其中 ${session.failed_count} 次失敗` : ''}。翻閱不會改動進度。` : '';
    document.getElementById('cardCandidateInput').textContent = version?.user_message || session?.user_message || '';
    const names = {original:'原有文風', natural:'自然互動', follow:'跟隨', explore:'探索', coauthor:'共同創作', legacy_20260908:'舊版（9/8）', default:'一般場景', intimate:'親密互動'};
    document.getElementById('cardCandidateMeta').textContent = version ? [version.text_only_edit ? '手動編輯' : '', version.model || '模型未記錄',
      ...['narrative_style','plot_mode','scene_mode'].map(k => names[version[k]]).filter(Boolean),
      version.generated_at ? new Date(version.generated_at).toLocaleString() : '舊版：生成時間未記錄',
      version.edited_at ? `編輯於 ${new Date(version.edited_at).toLocaleString()}` : ''].filter(Boolean).join(' · ') : '';
    document.getElementById('cardCandidateInstructionDetails').hidden = typeof version?.persistent_instruction !== 'string';
    document.getElementById('cardCandidateInstruction').textContent = version?.persistent_instruction || '這版未啟用持續指示。';
    document.getElementById('cardCandidateContent').innerHTML = version ? cardMessageContent(version.content) : '';
    const feedback = document.getElementById('cardCandidateFeedback'); feedback.replaceChildren();
    if (version?.feedback) feedback.appendChild(feedbackControls({branch:session.branch,
      index:version.message_index ?? session.message_index, group:version.group_id || session.group_id,
      feedback:version.feedback, version}));
    const list = document.getElementById('cardFavoriteList');
    list.hidden = !session?.favorites || !version;
    if (session?.favorites) {
      list.replaceChildren(...session.versions.map((v,i) => new Option(`#${v.message_index} · ${(v.user_message || '').slice(0,30)}`, i)));
      list.value = candidatePosition;
    }
  }
  async function openCandidates(index, favorites = false) {
    if (isGenerating || candidateBusy || !currentBranch) return;
    candidateBusy = true; candidateSession = null; candidatePosition = 0;
    document.getElementById('cardCandidateError').textContent = '';
    document.getElementById('cardCandidateTitle').textContent = favorites ? '收藏的回覆' : `回合 #${index} 的版本`;
    if (!candidateDialog.open) candidateDialog.showModal();
    renderCandidate();
    const branch = currentBranch;
    try {
      const res = await fetch(`/api/card/${slug}/candidates?branch_id=${encodeURIComponent(branch)}&${favorites ? 'favorites=1' : 'message_index='+index}`, {cache:'no-store'});
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || '版本載入失敗');
      candidateSession = {...data, branch, favorites, versions:favorites ? data.favorites : data.versions};
      candidatePosition = Math.max(0, candidateSession.versions.findIndex(v => v.selected));
    } catch(e) { document.getElementById('cardCandidateError').textContent = e.message; }
    finally { candidateBusy = false; renderCandidate(); }
  }
  document.getElementById('cardCandidatePrev')?.addEventListener('click', () => {candidatePosition--; renderCandidate();});
  document.getElementById('cardCandidateNext')?.addEventListener('click', () => {candidatePosition++; renderCandidate();});
  document.getElementById('cardFavoriteList')?.addEventListener('change', event => {candidatePosition = Number(event.target.value); renderCandidate();});
  document.getElementById('cardCandidateClose')?.addEventListener('click', () => {if (!candidateBusy) candidateDialog.close();});
  candidateDialog?.addEventListener('cancel', event => {if (candidateBusy) event.preventDefault();});
  document.getElementById('cardFavoritesOpen')?.addEventListener('click', () => openCandidates(null, true));
  document.getElementById('cardCandidateFavorite')?.addEventListener('click', async () => {
    const session = candidateSession, version = session?.versions[candidatePosition];
    if (candidateBusy || !version) return;
    candidateBusy = true; renderCandidate(); document.getElementById('cardCandidateError').textContent = '';
    try {
      await ensemblePost('candidates', {branch_id:session.branch, action:'favorite',
        group_id:version.group_id || session.group_id, version_id:version.id, favorite:!version.favorite});
      version.favorite = !version.favorite;
    } catch(e) { document.getElementById('cardCandidateError').textContent = e.message; }
    finally { candidateBusy = false; renderCandidate(); }
  });
  document.getElementById('cardCandidateApply')?.addEventListener('click', async () => {
    const session = candidateSession, version = session?.versions[candidatePosition];
    if (candidateBusy || !version) return;
    if (session.favorites) {
      // A removed turn can still be read/favorited; applying needs its retained timeline.
      const msg = displayedMessages.find(m => m.index === version.message_index);
      if (!msg) {document.getElementById('cardCandidateError').textContent = '這回合已不在目前路線，收藏仍保留。'; return;}
      await openCandidates(version.message_index);
      if (candidateSession?.group_id !== version.group_id) {
        document.getElementById('cardCandidateError').textContent = '目前同編號回合已是另一段對話，原收藏仍保留。'; return;
      }
      candidatePosition = Math.max(0, candidateSession.versions.findIndex(v => v.id === version.id)); renderCandidate(); return;
    }
    candidateBusy = true; setBusy(true); renderCandidate(); document.getElementById('cardCandidateError').textContent = '';
    try {
      const result = await ensemblePost('candidates', {branch_id:session.branch, action:'select',
        group_id:session.group_id, version_id:version.id, expected_version_id:session.selected_id, message_index:session.message_index});
      currentBranch = result.branch_id; candidateDialog.close(); lastRenderedMessages = ''; renderDirty = true;
    } catch(e) { document.getElementById('cardCandidateError').textContent = e.message; }
    finally { candidateBusy = false; setBusy(false); renderCandidate(); await loadCardState({force:true}); }
  });

  function renderMessages(messages) {
    displayedMessages = messages;
    messageList.replaceChildren();
    messages.forEach((msg, position) => {
      const { el, content } = createChatMessage(msg);
      if (msg.role !== 'user') content.innerHTML = cardMessageContent(msg.content);
      const actions = document.createElement('div');
      actions.className = 'msg-actions';
      function action(label, title, handler) {
        const button = document.createElement('button');
        button.className = 'msg-action-btn';
        button.textContent = label;
        button.title = title;
        button.disabled = isGenerating;
        button.addEventListener('click', handler);
        actions.appendChild(button);
      }
      if (msg.role === 'user' && messages[position+1]?.role === 'gm' && (!ensembleState || messages[position+1].index > ensembleState.baseline_index)) {
        action('✎', '編輯此回合', () => openEditDialog(messages[position+1], msg, position+1 < messages.length-1));
      }
      if (msg.role === 'gm' && Number.isInteger(msg.index) && (!ensembleState || msg.index >= ensembleState.baseline_index)) {
        if ((!ensembleState || msg.index > ensembleState.baseline_index) && position === messages.length - 1 && position > 0 && messages[position - 1].role === 'user') {
          action('↻', '重新生成', regenerateLast);
          action('編輯正文', '編輯最新回覆正文，保存為新版本', () => openReplyEdit(msg));
        }
        if (position > 0 && messages[position-1].role === 'user' && (!ensembleState || msg.index > ensembleState.baseline_index)) {
          action('✎', '編輯此回合', () => openEditDialog(msg, messages[position-1], position < messages.length-1));
          const summary = msg.candidate_summary;
          action(summary ? `第 ${summary.selected_number}／${summary.versions} 版` : '版本', '查看回覆版本與收藏', () => openCandidates(msg.index));
        }
        action('⑂', '從此處建立分支，保留原路線', () => openBranchDialog(msg.index));
        action('↩', '回到此回合結束，移除後續對話', () => rewindTo(msg.index));
      }
      if (msg.role === 'gm' && msg.feedback) actions.appendChild(feedbackControls({branch:currentBranch, index:msg.index, feedback:msg.feedback}));
      el.appendChild(actions);
      messageList.appendChild(el);
    });
  }

  function setBusy(busy) {
    isGenerating = busy;
    syncClearDraft();
    stateEpoch++;
    if (sendBtn) sendBtn.disabled = busy;
    if (resetBtn) resetBtn.disabled = busy;
    if (drawerResetBtn) drawerResetBtn.disabled = busy;
    const continueBtn = document.getElementById('cardContinueBtn');
    if (continueBtn) continueBtn.disabled = busy || !currentBranch;
    if (exploreBtn) exploreBtn.disabled = busy || !currentBranch || !explorationMaterials.length;
    if (exploreSize) exploreSize.disabled = busy || !currentBranch || !explorationMaterials.length;
    if (instructionOpen) instructionOpen.disabled = busy || !currentBranch;
    const favoritesBtn = document.getElementById('cardFavoritesOpen');
    if (favoritesBtn) favoritesBtn.disabled = busy;
    document.querySelectorAll('.msg-action-btn').forEach(b => b.disabled = busy);
    const modelSelect = document.getElementById('modelSelect');
    if (modelSelect) modelSelect.disabled = busy;
    if (responseLengthSelect) responseLengthSelect.disabled = busy;
    responseLengthButtons.forEach(button => { button.disabled = busy; });
    if (narrativeStyleSelect) narrativeStyleSelect.disabled = busy;
    if (plotModeSelect) plotModeSelect.disabled = busy;
    if (sceneModeSelect) sceneModeSelect.disabled = busy;
    if (recallModeSelect) recallModeSelect.disabled = busy;
    [branchSelect, document.getElementById('cardBranchRename'), document.querySelector('#cardBranchForm [type=submit]'), focusSelect, presenceSelect, document.getElementById('ensembleRetryRecording'), document.getElementById('ensembleCreateBtn'), document.getElementById('ensembleAddBtn')].forEach(e => { if(e) e.disabled = busy; });
  }

  async function runStream(endpoint, body, userText) {
    setBusy(true);
    const gm = { role: 'gm', content: '' };
    if (endpoint === 'edit') {
      renderMessages(displayedMessages.filter(m => m.index < body.message_index - 1));
      messageList.appendChild(createChatMessage({ role: 'user', content: body.message }).el);
    } else if (userText != null) {
      messageList.appendChild(createChatMessage({ role: 'user', content: userText }).el);
    } else if (endpoint !== 'edit') {
      messageList.lastElementChild?.remove();
    }
    const { el, content } = createChatMessage(gm);
    messageList.appendChild(el);
    loadingText.textContent = (recallModeSelect?.value || 'off') !== 'off' ? '角色回應中，可能需要翻閱往事…' : '角色回應中…';
    loadingIndicator.style.display = 'flex';
    scrollToBottom();
    let failure = '';
    try {
      await streamFromSSE(`/api/card/${slug}/${endpoint}`, { branch_id:currentBranch, ...body, response_length: responseLengthSelect?.value || 'medium', recall_mode: recallModeSelect?.value || 'off', narrative_style: narrativeStyleSelect?.value || 'original', plot_mode: plotModeSelect?.value || 'follow', scene_mode: sceneModeSelect?.value || 'default' },
        chunk => {
          const follow = messagesScroll.scrollHeight - messagesScroll.scrollTop - messagesScroll.clientHeight < 100;
          loadingIndicator.style.display = 'none';
          gm.content += chunk;
          content.innerHTML = cardMessageContent(gm.content);
          if (follow) scrollToBottom();
        },
        payload => {
          const result = payload.result || payload;
          if (result.ok !== false && result.branch_id) currentBranch = result.branch_id;
          if (result.ok === false) failure = result.error || '生成失敗';
          else if (result.narration != null) {
            gm.content = result.narration;
            content.innerHTML = cardMessageContent(gm.content);
          }
        },
        message => { failure = message; });
    } catch (err) {
      failure = err.message;
    } finally {
      loadingIndicator.style.display = 'none';
      setBusy(false);
      // Invalidate an older in-flight poll; the next poll will reconcile if it is still loading.
      renderDirty = true;
      await loadCardState({ force: true });
    }
    if (failure) {
      if (userText != null && !userInput.value) {
        userInput.value = userText;
        userInput.dispatchEvent(new Event('input'));
      }
      await showAlert('生成失敗：' + failure);
    }
    return !failure;
  }

  async function handleSend(e) {
    if (e) e.preventDefault();
    if (isGenerating) return;
    const text = userInput.value.trim();
    if (!text) return;
    userInput.value = '';
    userInput.style.height = 'auto';
    await runStream('send/stream', { message: text }, text);
  }

  function setComposerDraft(text, preview = false) {
    userInput.value = text;
    userInput.dispatchEvent(new Event('input', {bubbles:true}));
    if (preview) {
      userInput.blur();
      userInput.setSelectionRange(0, 0);
      userInput.scrollTop = 0;
      requestAnimationFrame(() => {
        if (userInput.value === text && document.activeElement !== userInput) userInput.scrollTop = 0;
      });
    } else {
      userInput.focus({preventScroll:true});
      userInput.setSelectionRange(text.length, text.length);
    }
  }

  document.getElementById('cardContinueBtn')?.addEventListener('click', () => {
    if (isGenerating || !currentBranch) return;
    const draft = userInput.value;
    const instruction = '（請繼續發展）';
    setComposerDraft(draft.trimEnd().endsWith(instruction) ? draft
      : draft + (draft.trim() ? '\n\n' : '') + instruction, true);
    handleSend();
  });

  exploreBtn?.addEventListener('click', () => {
    if (isGenerating || !currentBranch || !explorationMaterials.length) return;
    const size = exploreSize?.value || 'any';
    const eligible = explorationMaterials.filter(m => size === 'any' || (m.size || 'small') === size);
    const pool = eligible.filter(m => m.id !== lastExplorationId);
    const choices = pool.length ? pool : eligible;
    const material = choices.length ? choices[Math.floor(Math.random() * choices.length)] : null;
    const direction = explorationDraft?.text === userInput.value ? explorationDraft.direction : userInput.value.trim();
    if (!material) return;
    const message = material.title + '：' + material.seed
      + (direction ? '\n\n' + direction : '');
    explorationDraft = {text:message, direction};
    if (material) lastExplorationId = material.id;
    setComposerDraft(message, true);
  });

  async function regenerateLast() {
    if (isGenerating) return;
    setBusy(true);
    const confirmed = await showConfirm('重新生成最後一輪回應？將撤銷該回合的記錄。');
    if (!confirmed) { setBusy(false); return; }
    await runStream('regenerate', { stream: true });
  }

  async function mutateCard(endpoint, body, question) {
    if (isGenerating) return;
    setBusy(true);
    if (!await showConfirm(question)) { setBusy(false); return; }
    try {
      const res = await fetch(`/api/card/${slug}/${endpoint}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({branch_id:currentBranch, ...body}),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
      closeDrawer();
    } catch (err) {
      await showAlert('操作失敗：' + err.message);
    } finally {
      setBusy(false);
      renderDirty = true;
      await loadCardState({ force: true });
    }
  }

  function rewindTo(index) {
    return mutateCard('rewind', { to_index: index }, `回到第 #${index} 則結束時的狀態？後續對話將移除。`);
  }

  function handleReset() {
    return mutateCard('reset', {}, ensembleState ? '回到建立多角色副本時的狀態？之後對話將移除。' : '重置目前分支？將清空這條線的後續對話與記憶，其他分支不受影響。');
  }

  // ---------------------------------------------------------------------------
  // 模型切換器
  // ---------------------------------------------------------------------------

  async function initModelSelector() {
    const sel = document.getElementById('modelSelect');
    if (!sel) return;
    try {
      const res = await fetch('/api/config');
      const cfg = await res.json();
      if (!cfg.ok) return;

      populateChatModels(sel, cfg);
      let savedSelection = sel.value;
      sel.onchange = async () => {
        const [newProv, newModel] = sel.value.split('|');
        const update = { provider: newProv };
        update[newProv] = { model: newModel };
        try {
          const response = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(update),
          });
          const result = await response.json();
          if (!response.ok || !result.ok) throw new Error(result.error || '儲存失敗');
          savedSelection = sel.value;
        } catch (e) {
          sel.value = savedSelection;
          showToast('切換模型失敗：' + e.message);
        }
      };
    } catch (err) {
      console.error('initModelSelector error:', err);
    }
  }

  // ---------------------------------------------------------------------------
  // 事件監聽與綁定
  // ---------------------------------------------------------------------------

  if (userInput && sendBtn) bindChatComposer(userInput, sendBtn, handleSend);
  if (resetBtn) resetBtn.addEventListener('click', handleReset);
  if (drawerResetBtn) drawerResetBtn.addEventListener('click', handleReset);

  // 可供頁面操作使用的角色卡介面
  window.CardChat = {
    regenerateLast,
    rewindTo,
  };

  // 初始化載入
  function init() {
    initModelSelector();
    if (messageList && slug) {
      loadCardState();
      refreshTimer = setInterval(() => {
        if (!document.hidden) loadCardState();
      }, 2000);
      window.addEventListener('pagehide', () => clearInterval(refreshTimer));
      const addonBtn = document.getElementById('addon-panel-btn');
      const addonPanel = document.getElementById('addon-panel');
      const closeAddon = () => {
        addonPanel.classList.add('hidden');
        addonBtn.setAttribute('aria-expanded', 'false');
      };
      addonBtn.addEventListener('click', () => {
        const open = addonPanel.classList.toggle('hidden') === false;
        addonBtn.setAttribute('aria-expanded', String(open));
        if (open) initModelSelector();
      });
      document.addEventListener('click', e => {
        if (!addonPanel.contains(e.target) && !addonBtn.contains(e.target)) closeAddon();
      });
      document.addEventListener('keydown', e => { if (e.key === 'Escape') closeAddon(); });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
