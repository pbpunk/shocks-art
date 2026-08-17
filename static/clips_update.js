(() => {
  const form = document.querySelector('.clips-update-form');
  const button = form?.querySelector('.clips-update-button');
  const label = button?.querySelector('.clips-update-label');
  const statusUrl = button?.dataset.updateStatusUrl;
  if (!form || !button || !label || !statusUrl) return;

  let pollTimer = null;

  const runningStatus = (status) => status === 'checking' || status === 'processing';

  const stateAgeMs = (payload) => {
    const timestamp = Date.parse(payload?.updated_at || '');
    return Number.isFinite(timestamp) ? Date.now() - timestamp : Number.POSITIVE_INFINITY;
  };

  const setProgress = (payload) => {
    const total = Number(payload?.total_streams || 0);
    const completed = Number(payload?.completed_streams || 0) + Number(payload?.failed_streams || 0);
    const percent = total > 0 ? Math.max(0, Math.min(100, (completed / total) * 100)) : 0;
    button.style.setProperty('--update-progress', `${percent}%`);
  };

  const setButtonState = (state, text, payload = {}) => {
    const running = runningStatus(state);
    button.classList.toggle('is-updating', running);
    button.classList.toggle('is-complete', state === 'complete');
    button.classList.toggle('is-failed', state === 'failed');
    button.disabled = running;
    button.setAttribute('aria-busy', String(running));
    button.setAttribute('title', payload.message || text);
    button.setAttribute('aria-label', payload.message || text);
    label.textContent = text;
    setProgress(payload);
  };

  const resetButtonSoon = () => {
    window.setTimeout(() => {
      if (!button.classList.contains('is-updating')) {
        button.classList.remove('is-complete', 'is-failed');
        button.disabled = false;
        button.style.setProperty('--update-progress', '0%');
        button.removeAttribute('title');
        button.setAttribute('aria-label', 'Update clips from new streams');
        label.textContent = 'Update';
      }
    }, 3200);
  };

  const applyStatus = (payload) => {
    const status = payload?.status || 'idle';

    if (runningStatus(status) && stateAgeMs(payload) > 6 * 60 * 60 * 1000) {
      setButtonState('idle', 'Update');
      button.disabled = false;
      return false;
    }

    if (status === 'checking') {
      setButtonState('checking', 'Checking…', payload);
      return true;
    }

    if (status === 'processing') {
      const total = Number(payload.total_streams || 0);
      const current = Number(payload.current_index || 0);
      const text = total > 0 && current > 0 ? `${current}/${total}` : 'Updating…';
      setButtonState('processing', text, payload);
      return true;
    }

    if (status === 'complete' && stateAgeMs(payload) < 5 * 60 * 1000) {
      const newClips = Number(payload.new_clips || 0);
      const failed = Number(payload.failed_streams || 0);
      const text = newClips > 0 ? `Updated +${newClips}` : (failed > 0 ? 'Finished' : 'Up to date');
      setButtonState('complete', text, payload);
      button.style.setProperty('--update-progress', '100%');

      if (newClips > 0 && payload.updated_at) {
        const reloadKey = `clips-update-reloaded:${payload.updated_at}`;
        if (!sessionStorage.getItem(reloadKey)) {
          sessionStorage.setItem(reloadKey, '1');
          window.setTimeout(() => window.location.reload(), 900);
          return false;
        }
      }

      resetButtonSoon();
      return false;
    }

    if (status === 'failed' && stateAgeMs(payload) < 5 * 60 * 1000) {
      setButtonState('failed', 'Update failed', payload);
      resetButtonSoon();
      return false;
    }

    setButtonState('idle', 'Update');
    button.disabled = false;
    return false;
  };

  const schedulePoll = () => {
    if (pollTimer) window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(poll, 1200);
  };

  const poll = async () => {
    try {
      const response = await fetch(statusUrl, { cache: 'no-store' });
      if (!response.ok) throw new Error('Update status failed');
      const payload = await response.json();
      if (applyStatus(payload)) schedulePoll();
    } catch {
      button.disabled = false;
      button.classList.remove('is-updating');
      label.textContent = 'Update';
    }
  };

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (button.disabled) return;

    if (pollTimer) window.clearTimeout(pollTimer);
    setButtonState('checking', 'Checking…', { message: 'Checking for new streams…' });

    try {
      const response = await fetch(form.action, {
        method: 'POST',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!response.ok) throw new Error('Clip update could not start');
      const payload = await response.json();
      if (applyStatus(payload)) schedulePoll();
    } catch (error) {
      setButtonState('failed', 'Update failed', { message: error?.message || 'Clip update could not start' });
      resetButtonSoon();
    }
  });

  poll();
})();
