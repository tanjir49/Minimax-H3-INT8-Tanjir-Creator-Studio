/* Per-model access matrix: each model owns its supported resolution policy. */
(() => {
  const categoryFor = model => {
    if (model.mode === 'image') return ['image', 'Image'];
    if (model.mode === 'video') return ['video', 'Video'];
    if (model.mode === 'edit') return ['edit', 'Video Edit'];
    if (model.mode === 'upscale') return ['upscale', 'Upscale'];
    return ['audio', 'Audio'];
  };

  loadUsers = async function () {
    const { users, models = [] } = await api('/api/admin/users');
    const list = $('#user-list');
    list.innerHTML = '';

    const update = async (user, payload, button) => {
      button.disabled = true;
      expandedUserAccessV195 = user.id;
      try {
        await api(`/api/admin/users/${user.id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        await loadUsers();
        await loadPresets();
      } catch (error) {
        alert(error.message);
        button.disabled = false;
      }
    };

    for (const user of users) {
      const card = document.createElement('details');
      card.className = 'team-user-card-v195';
      card.open = expandedUserAccessV195 === user.id;
      card.ontoggle = () => {
        if (card.open) expandedUserAccessV195 = user.id;
        else if (expandedUserAccessV195 === user.id) expandedUserAccessV195 = null;
      };

      const summary = document.createElement('summary');
      summary.innerHTML = '<span class="team-user-identity-v195"><b></b><small></small></span><span class="team-user-badges-v195"></span><i>⌄</i>';
      summary.querySelector('b').textContent = user.username;
      summary.querySelector('small').textContent = user.role === 'admin' ? 'Administrator' : 'Team member';
      summary.querySelector('.team-user-badges-v195').innerHTML = `<em class="${user.enabled ? 'active' : 'disabled'}">${user.enabled ? 'Active' : 'Disabled'}</em>${user.role === 'admin' ? '<em>Full access</em>' : `<em>${(user.model_access || []).length} models</em>`}`;

      const body = document.createElement('div');
      body.className = 'team-user-body-v195';
      const actions = document.createElement('div');
      actions.className = 'team-user-actions-v195';
      const toggle = document.createElement('button');
      toggle.textContent = user.enabled ? 'Disable' : 'Enable';
      toggle.disabled = user.id === state.user.id;
      toggle.onclick = () => update(user, { enabled: !user.enabled, logout_all: true }, toggle);
      const reset = document.createElement('button');
      reset.textContent = 'Reset password';
      reset.onclick = async () => {
        const password = prompt(`New password for ${user.username}`);
        if (!password) return;
        await update(user, { password, logout_all: true }, reset);
      };
      const remove = document.createElement('button');
      remove.className = 'danger';
      remove.textContent = 'Delete user';
      remove.disabled = user.id === state.user.id || user.role === 'admin';
      remove.onclick = async () => {
        if (!confirm(`Delete ${user.username} and all of this user's projects and media? This cannot be undone.`)) return;
        await api(`/api/admin/users/${user.id}`, { method: 'DELETE' });
        expandedUserAccessV195 = null;
        await loadUsers();
      };
      actions.append(toggle, reset, remove);
      body.append(actions);

      if (user.role === 'admin') {
        const full = document.createElement('div');
        full.className = 'admin-full-access-v195';
        full.innerHTML = '<b>✓ Full administrator access</b><small>Every model and every supported resolution is available.</small>';
        body.append(full);
      } else {
        const matrix = document.createElement('div');
        matrix.className = 'model-resolution-matrix-v197';
        const grouped = new Map();
        for (const model of models) {
          const [key, label] = categoryFor(model);
          if (!grouped.has(key)) grouped.set(key, { label, models: [] });
          grouped.get(key).models.push(model);
        }
        for (const [key, category] of grouped) {
          const section = document.createElement('details');
          section.className = 'model-category-v197';
          section.open = true;
          const heading = document.createElement('summary');
          const allowedCount = category.models.filter(model => (user.model_access || []).includes(model.id)).length;
          heading.innerHTML = `<b>${category.label}</b><small>${allowedCount}/${category.models.length} models</small>`;
          const rows = document.createElement('div');
          rows.className = 'model-policy-list-v197';

          for (const model of category.models) {
            const modelEnabled = (user.model_access || []).includes(model.id);
            const policy = document.createElement('article');
            policy.className = `model-policy-v197${modelEnabled ? ' enabled' : ' blocked'}`;
            const modelButton = document.createElement('button');
            modelButton.type = 'button';
            modelButton.className = 'model-policy-toggle-v197';
            modelButton.innerHTML = `<i>${modelEnabled ? '✓' : '＋'}</i><span><b></b><small>${modelEnabled ? 'Model allowed' : 'Model blocked'}</small></span>`;
            modelButton.querySelector('b').textContent = model.label;
            modelButton.onclick = () => update(user, { model_access: { [model.id]: !modelEnabled } }, modelButton);
            policy.append(modelButton);

            const resolutions = document.createElement('div');
            resolutions.className = 'model-resolution-chips-v197';
            const allowedResolutions = user.model_resolution_access?.[model.id] || [];
            if (!(model.resolutions || []).length) {
              resolutions.innerHTML = '<small>No resolution control</small>';
            } else {
              for (const resolution of model.resolutions) {
                const enabled = allowedResolutions.includes(resolution);
                const chip = document.createElement('button');
                chip.type = 'button';
                chip.className = enabled ? 'enabled' : '';
                chip.textContent = resolution === '2160p' ? '4K' : resolution;
                chip.title = `${enabled ? 'Allowed' : 'Blocked'} for ${model.label}`;
                chip.onclick = () => update(user, { model_resolution_access: { [model.id]: { [resolution]: !enabled } } }, chip);
                resolutions.append(chip);
              }
            }
            policy.append(resolutions);
            rows.append(policy);
          }
          section.append(heading, rows);
          matrix.append(section);
        }
        body.append(matrix);
      }
      card.append(summary, body);
      list.append(card);
    }
  };
})();
