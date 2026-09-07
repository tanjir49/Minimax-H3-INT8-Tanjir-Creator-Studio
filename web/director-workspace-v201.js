/* Project scope selector and persistent Director conversation history. */
(() => {
  const projectSelect = $('#director-project-select');
  const historyPanel = $('#director-history-panel');
  const historyList = $('#director-history-list');
  const chat = $('#director-chat');
  if (!projectSelect || !historyPanel || !historyList || !chat) return;

  window.directorScopeProjectIdV201 = '';
  const welcome = () => {
    chat.innerHTML = '<div class="director-welcome"><b>✦ Your production director is ready</b><span>Create standalone media, or choose a project above for its characters, assets and continuity.</span><small>General requests are saved under Quick Creations.</small></div>';
    $('#director-token-status').textContent = 'Tokens: ready / 8,192';
  };
  const syncScopeLabel = () => {
    window.directorScopeProjectIdV201 = projectSelect.value;
    const option = projectSelect.selectedOptions[0];
    $('#director-project-name').textContent = projectSelect.value ? `Project workspace: ${option.textContent}` : 'General workspace · Quick Creations';
  };
  const populateProjects = () => {
    const selected = window.directorScopeProjectIdV201 || '';
    projectSelect.innerHTML = '<option value="">General · Quick Creations</option>';
    const seen = new Set();
    for (const project of state.projects || []) {
      if (project.name === 'Quick Creations' || seen.has(project.id)) continue;
      seen.add(project.id);
      const option = document.createElement('option');
      option.value = project.id;
      option.textContent = state.user?.role === 'admin' && project.owner_username ? `${project.name} · ${project.owner_username}` : project.name;
      projectSelect.append(option);
    }
    projectSelect.value = [...projectSelect.options].some(option => option.value === selected) ? selected : '';
    syncScopeLabel();
  };
  const newChat = () => {
    directorConversationV186 = null;
    localStorage.removeItem('director-conversation-global');
    welcome();
    historyPanel.hidden = true;
    $('#director-prompt').focus();
  };
  const loadHistory = async () => {
    historyList.innerHTML = '<div class="empty">Loading chats…</div>';
    const { conversations = [] } = await api('/api/director/conversations');
    historyList.innerHTML = '';
    if (!conversations.length) historyList.innerHTML = '<div class="empty">No saved chats yet.</div>';
    for (const conversation of conversations) {
      const button = document.createElement('button');
      const date = new Date(conversation.updated_at);
      button.innerHTML = '<b></b><small></small>';
      button.querySelector('b').textContent = conversation.title || 'Untitled chat';
      button.querySelector('small').textContent = Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
      button.onclick = async () => {
        const result = await api(`/api/director/conversations/${encodeURIComponent(conversation.id)}`);
        directorConversationV186 = conversation.id;
        localStorage.setItem('director-conversation-global', conversation.id);
        chat.innerHTML = '';
        for (const message of result.messages || []) directorMessageV186(message.role === 'user' ? 'user' : 'assistant', message.content);
        const project = (state.projects || []).find(item => item.id === result.conversation.project_id && item.name !== 'Quick Creations');
        projectSelect.value = project ? project.id : '';
        syncScopeLabel();
        historyPanel.hidden = true;
        chat.scrollTop = chat.scrollHeight;
      };
      historyList.append(button);
    }
  };

  projectSelect.onchange = () => { syncScopeLabel(); newChat(); };
  $('#director-new-chat').onclick = newChat;
  $('#director-history-toggle').onclick = async () => {
    historyPanel.hidden = !historyPanel.hidden;
    if (!historyPanel.hidden) {
      try { await loadHistory(); } catch (error) { historyList.innerHTML = `<div class="empty">${error.message}</div>`; }
    }
  };
  $('#director-history-close').onclick = () => { historyPanel.hidden = true; };

  const openDirector = $('#director').onclick;
  $('#director').onclick = event => {
    populateProjects();
    openDirector.call($('#director'), event);
    const saved = localStorage.getItem('director-conversation-global');
    directorConversationV186 = saved || null;
    syncScopeLabel();
  };
})();
