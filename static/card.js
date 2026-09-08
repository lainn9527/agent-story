/**
 * Card Mode (角色卡模式) 獨立前端腳本
 * 基底全面承襲主神空間 RPG 的互動流與視圖結構
 */

(function () {
  'use strict';

  const scriptTag = document.currentScript;
  const slug = (scriptTag && scriptTag.dataset.slug) || window.location.pathname.split('/').pop();

  const TIERS = ['敵對', '戒備', '陌生', '相識', '友好', '信任', '摯友'];

  // DOM 元素
  const messageList = document.getElementById('message-list');
  const messagesScroll = document.getElementById('messages');
  const userInput = document.getElementById('user-input');
  const sendBtn = document.getElementById('send-btn');
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
  const navCharRole = document.getElementById('navCharRole');
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
  let currentMessages = [];

  // ---------------------------------------------------------------------------
  // 抽屜 (Drawer) 開關控制
  // ---------------------------------------------------------------------------

  function openDrawer() {
    if (drawer) drawer.classList.remove('closed');
    if (drawerOverlay) drawerOverlay.classList.remove('hidden');
  }

  function closeDrawer() {
    if (drawer) drawer.classList.add('closed');
    if (drawerOverlay) drawerOverlay.classList.add('hidden');
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
  // Markdown 渲染 (與主神空間 app.js 規格對齊)
  // ---------------------------------------------------------------------------

  function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  function markdownToHtml(text) {
    if (!text) return '';
    let html = escapeHtml(text);

    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
    html = html.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
    html = html.replace(/【(.+?)】/g, '<span class="god-hint">【$1】</span>');
    html = html.replace(/^---$/gm, '<hr class="scene-break">');
    html = html.replace(/（(.+?)）/g, '<span class="aside">（$1）</span>');
    html = html.replace(/^[•·\-]\s+(.+)$/gm, '<li>$1</li>');
    html = html.replace(/<\/li>\n+<li>/g, '</li>\n<li>');
    html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul>$1</ul>');
    html = html.replace(/^\d+[.、]\s*(.+)$/gm, '<li>$1</li>');
    html = html.replace(/<\/li>\n+<li>/g, '</li>\n<li>');
    html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, '<ol>$1</ol>');
    html = html.replace(/\n{2,}/g, '</p><p>');
    html = html.replace(/\n/g, '<br>');
    html = '<p>' + html + '</p>';
    html = html.replace(/<p>\s*<\/p>/g, '');
    html = html.replace(/(<\/h[1234]>)(<br>|<\/p><p>)/g, '$1');
    html = html.replace(/(<\/[uo]l>)(<br>|<\/p><p>)/g, '$1');
    html = html.replace(/(<[uo]l>)(<br>|<\/p><p>)/g, '$1');
    html = html.replace(/(<hr class="scene-break">)(<br>|<\/p><p>)/g, '$1');

    return html;
  }

  // ---------------------------------------------------------------------------
  // 狀態載入與渲染
  // ---------------------------------------------------------------------------

  async function loadCardState() {
    try {
      const res = await fetch(`/api/card/${slug}/state`);
      const data = await res.json();
      if (!data.ok) {
        alert('載入失敗：' + (data.error || '未知錯誤'));
        return;
      }

      // 更新側欄資訊
      updateSidebar(data);

      // 更新訊息列表
      currentMessages = data.messages || [];
      renderMessages(currentMessages);
      scrollToBottom();
    } catch (err) {
      console.error('loadCardState error:', err);
    }
  }

  function updateSidebar(data) {
    const card = data.card || {};
    const persona = card.persona || {};
    const charName = card.display_name || card.name || '未知角色';
    const charRole = (persona && persona.identity) || card.role || '';

    if (navCharName) navCharName.textContent = charName;
    if (navCharRole) navCharRole.textContent = charRole || '專屬人設 · 深度互動';
    if (sidebarName) sidebarName.textContent = `${charName} 角色檔案`;
    if (sidebarRole) sidebarRole.textContent = charRole;
    if (sidebarPitch) sidebarPitch.textContent = card.pitch || '';

    // 角色外觀與特徵
    if (sidebarTraits) {
      const traits = [];
      if (persona.appearance) {
        traits.push(`<strong>外觀：</strong>${escapeHtml(persona.appearance)}`);
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

    // 時鐘環境更新
    const clock = data.clock || (data.state && data.state.world_clock) || {};
    const timeStr = clock.time_str || clock.time || '--:--';
    const locStr = (card.setting && card.setting.place) || clock.location || '--';
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
          <span class="inv-chip" title="${escapeHtml(inv[k])}">${escapeHtml(k)}: ${escapeHtml(inv[k])}</span>
        `).join('');
      }
    }
  }

  // ---------------------------------------------------------------------------
  // 訊息列表渲染
  // ---------------------------------------------------------------------------

  function renderMessages(messages) {
    if (!messageList) return;
    messageList.innerHTML = '';

    // 找出最後一條 GM 訊息以賦予「重新生成」按鈕
    let lastGmIdx = -1;
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'gm' || messages[i].role === 'assistant') {
        lastGmIdx = i;
        break;
      }
    }

    messages.forEach((msg, idx) => {
      const el = createMessageElement(msg, idx, idx === lastGmIdx);
      messageList.appendChild(el);
    });
  }

  function createMessageElement(msg, index, isLatestGm) {
    const el = document.createElement('div');
    const isUser = msg.role === 'user';
    el.className = `message ${isUser ? 'user' : 'gm'}`;
    el.dataset.index = index;

    if (isUser) {
      el.innerHTML = `
        <div class="role-tag">玩家</div>
        <div class="content"><p>${escapeHtml(msg.content).replace(/\n/g, '<br>')}</p></div>
      `;
      return el;
    }

    // GM 訊息解析標頭 [時間 地點 天氣]
    let contentText = msg.content || '';
    let headerChipHtml = '';
    const match = contentText.match(/^\[(.*?)\]\s*\n?/);
    if (match) {
      headerChipHtml = `<div class="msg-turn-chip">⏱️ ${escapeHtml(match[1])}</div>`;
      contentText = contentText.substring(match[0].length);
    }

    let actionsHtml = '';
    if (isLatestGm) {
      actionsHtml = `
        <div class="msg-actions">
          <button class="msg-action-btn" onclick="CardChat.regenerateLast()" title="重新生成此回合">↺ 重新生成</button>
          <button class="msg-action-btn" onclick="CardChat.rewindTo(${index})" title="回退到此回合">⏪ 回捲至此</button>
        </div>
      `;
    } else {
      actionsHtml = `
        <div class="msg-actions">
          <button class="msg-action-btn" onclick="CardChat.rewindTo(${index})" title="回退到此回合">⏪ 回捲至此</button>
        </div>
      `;
    }

    el.innerHTML = `
      <div class="role-tag">GM</div>
      ${headerChipHtml}
      <div class="content">${markdownToHtml(contentText)}</div>
      ${actionsHtml}
    `;

    return el;
  }

  function scrollToBottom() {
    if (messagesScroll) {
      messagesScroll.scrollTop = messagesScroll.scrollHeight;
    }
  }

  // ---------------------------------------------------------------------------
  // 發送訊息 (SSE 串流處理)
  // ---------------------------------------------------------------------------

  async function handleSend(e) {
    if (e) e.preventDefault();
    if (isGenerating) return;

    const text = (userInput && userInput.value || '').trim();
    if (!text) return;

    isGenerating = true;
    userInput.value = '';
    userInput.style.height = 'auto';
    if (sendBtn) sendBtn.disabled = true;

    // 立即將使用者訊息加入 DOM
    const userMsg = { role: 'user', content: text };
    currentMessages.push(userMsg);
    const userEl = createMessageElement(userMsg, currentMessages.length - 1, false);
    messageList.appendChild(userEl);

    // 建立 GM 回應佔位元素
    const gmMsg = { role: 'gm', content: '' };
    const gmIndex = currentMessages.length;
    currentMessages.push(gmMsg);
    const gmEl = createMessageElement(gmMsg, gmIndex, true);
    messageList.appendChild(gmEl);

    const gmContentDiv = gmEl.querySelector('.content');

    if (loadingIndicator) {
      if (loadingText) loadingText.textContent = '角色回應中…';
      loadingIndicator.style.display = 'flex';
    }
    scrollToBottom();

    let accumulatedContent = '';

    try {
      const response = await fetch(`/api/card/${slug}/send/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text }),
      });

      if (!response.ok) {
        const errJson = await response.json().catch(() => ({}));
        throw new Error(errJson.error || `HTTP ${response.status}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop(); // 留下未結尾的片段

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const dataStr = line.slice(6).trim();
            if (!dataStr) continue;

            try {
              const payload = JSON.parse(dataStr);
              if (payload.type === 'chunk') {
                if (loadingIndicator) loadingIndicator.style.display = 'none';
                accumulatedContent += payload.text;
                gmMsg.content = accumulatedContent;

                // 即時渲染 Markdown
                let displayText = accumulatedContent;
                const m = displayText.match(/^\[(.*?)\]\s*\n?/);
                if (m) {
                  displayText = displayText.substring(m[0].length);
                }
                gmContentDiv.innerHTML = markdownToHtml(displayText);
                scrollToBottom();
              } else if (payload.type === 'done') {
                // PASS 2 已於背景派發，等待並拉取最新 state
                setTimeout(loadCardState, 800);
              } else if (payload.type === 'error') {
                console.error('SSE error:', payload.message);
                gmContentDiv.innerHTML += `<p style="color: var(--accent);">[生成發生錯誤: ${escapeHtml(payload.message)}]</p>`;
              }
            } catch (pErr) {
              // Ignore non-json lines
            }
          }
        }
      }
    } catch (err) {
      console.error('Send error:', err);
      gmContentDiv.innerHTML = `<p style="color: var(--accent);">[發送失敗: ${escapeHtml(err.message)}]</p>`;
    } finally {
      if (loadingIndicator) loadingIndicator.style.display = 'none';
      isGenerating = false;
      if (sendBtn) sendBtn.disabled = false;
      renderMessages(currentMessages);
      scrollToBottom();
      loadCardState();
    }
  }

  // ---------------------------------------------------------------------------
  // 重生 (Regenerate) 與回捲 (Rewind)
  // ---------------------------------------------------------------------------

  async function regenerateLast() {
    if (isGenerating) return;
    if (!confirm('確定要重新生成這一輪回應嗎？（將撤銷本回合記錄並重試）')) return;

    isGenerating = true;
    if (loadingIndicator) {
      if (loadingText) loadingText.textContent = '正在重新思考…';
      loadingIndicator.style.display = 'flex';
    }

    try {
      const res = await fetch(`/api/card/${slug}/regenerate`, { method: 'POST' });
      const data = await res.json();
      if (!data.ok) {
        alert('重新生成失敗：' + (data.error || '未知錯誤'));
        return;
      }
      await loadCardState();
    } catch (err) {
      alert('網路錯誤：' + err.message);
    } finally {
      if (loadingIndicator) loadingIndicator.style.display = 'none';
      isGenerating = false;
    }
  }

  async function rewindTo(msgIndex) {
    if (isGenerating) return;
    if (!confirm(`確定要回退到第 #${msgIndex} 條訊息之前的狀態嗎？`)) return;

    try {
      const res = await fetch(`/api/card/${slug}/rewind`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ to_index: msgIndex, message_index: msgIndex }),
      });
      const data = await res.json();
      if (!data.ok) {
        alert('回捲失敗：' + (data.error || '未知錯誤'));
        return;
      }
      await loadCardState();
    } catch (err) {
      alert('網路錯誤：' + err.message);
    }
  }

  async function handleReset() {
    if (isGenerating) return;
    if (!confirm('確定要重置對話嗎？將清空所有對話歷史與記憶，回到初次見面的開場。')) return;

    try {
      const res = await fetch(`/api/card/${slug}/reset`, { method: 'POST' });
      const data = await res.json();
      if (!data.ok) {
        alert('重置失敗：' + (data.error || '未知錯誤'));
        return;
      }
      closeDrawer();
      await loadCardState();
    } catch (err) {
      alert('重置失敗：' + err.message);
    }
  }

  // ---------------------------------------------------------------------------
  // 模型切換器
  // ---------------------------------------------------------------------------

  const PROVIDER_LABELS = { vertex: 'Vertex', gemini: 'Gemini', claude_cli: 'Claude', codex_agent: 'Codex' };
  const PROVIDER_MODELS = {
    vertex: ['gemini-3.8-flash', 'gemini-3.5-flash', 'gemini-3.1-flash-lite', 'gemini-2.5-flash'],
    gemini: ['gemini-3.8-flash', 'gemini-3.5-flash', 'gemini-3.1-flash-lite'],
    claude_cli: ['claude-sonnet-4-6', 'claude-opus-4-6'],
    codex_agent: ['gpt-5.4', 'gpt-5.4-mini'],
  };

  async function initModelSelector() {
    const sel = document.getElementById('modelSelect');
    if (!sel) return;
    try {
      const res = await fetch('/api/config');
      const cfg = await res.json();
      if (!cfg.ok) return;

      sel.innerHTML = '';
      const curProv = cfg.provider;
      const curModel = (cfg[curProv] && cfg[curProv].model) || '';

      for (const [prov, models] of Object.entries(PROVIDER_MODELS)) {
        const group = document.createElement('optgroup');
        group.label = PROVIDER_LABELS[prov] || prov;
        for (const m of models) {
          const opt = document.createElement('option');
          opt.value = `${prov}|${m}`;
          opt.textContent = `${PROVIDER_LABELS[prov] || prov}: ${m}`;
          if (prov === curProv && m === curModel) {
            opt.selected = true;
          }
          group.appendChild(opt);
        }
        sel.appendChild(group);
      }

      sel.onchange = async () => {
        const [newProv, newModel] = sel.value.split('|');
        const update = { provider: newProv };
        update[newProv] = { model: newModel };
        try {
          await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(update),
          });
        } catch (e) {
          console.error('Failed to update model:', e);
        }
      };
    } catch (err) {
      console.error('initModelSelector error:', err);
    }
  }

  // ---------------------------------------------------------------------------
  // 事件監聽與綁定
  // ---------------------------------------------------------------------------

  if (userInput) {
    userInput.addEventListener('input', () => {
      userInput.style.height = 'auto';
      userInput.style.height = Math.min(userInput.scrollHeight, 180) + 'px';
    });

    userInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    });
  }

  if (sendBtn) sendBtn.addEventListener('click', handleSend);
  if (resetBtn) resetBtn.addEventListener('click', handleReset);
  if (drawerResetBtn) drawerResetBtn.addEventListener('click', handleReset);

  // 全域暴露給 inline onclick
  window.CardChat = {
    regenerateLast,
    rewindTo,
  };

  // 初始化載入
  function init() {
    initModelSelector();
    if (messageList && slug) {
      loadCardState();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
