(() => {
  const form = document.querySelector('.clips-update-form');
  const button = form?.querySelector('.clips-update-button');
  const label = button?.querySelector('.clips-update-label');
  const statusUrl = button?.dataset.updateStatusUrl;
  if (!form || !button || !label || !statusUrl) return;

  let pollTimer = null;

  const setButtonState = (state, text) => {
    const running = state === 'checking' || state === 'processing';
    button.classList.toggle('is-updating', running);
    button.classList.toggle('is-complete', state === 'complete');
    button.classList.toggle('is-failed', state === 'failed');
    button.disabled = running;
    button.setAttribute('aria-busy', String(running));
    label.textContent = text;
  };

  const resetButtonSoon = () => {
    window.setTimeout(() => {
      if (!button.classList.contains('is-updating')) {
        button.classList.remove('is-complete', 'is-failed');
        button.disabled = false;
        label.textContent = 'Update';
      }
    }, 3200);
  };

  const isRecent = (value) => {
    const timestamp = Date.parse(value || '');
    return Number.isFinite(timestamp) && Date.now() - timestamp < 20000;
  };

  const applyStatus = (payload) => {
    const status = payload?.status || 'idle';

    if (status === 'checking') {
      setButtonState('checking', 'Checking…');
      return true;
    }

    if (status === 'processing') {
      setButtonState('processing', 'Updating…');
      return true;
    }

    if (status === 'complete' && isRecent(payload.updated_at)) {
      const newClips = Number(payload.new_clips || 0);
      setButtonState('complete', newClips > 0 ? `Updated +${newClips}` : 'Up to date');

      if (newClips > 0 && payload.updated_at) {
        const reloadKey = `clips-update-reloaded:${payload.updated_at}`;
        if (!sessionStorage.getItem(reloadKey)) {
          sessionStorage.setItem(reloadKey, '1');
          window.setTimeout(() => window.location.reload(), 700);
          return false;
        }
      }

      resetButtonSoon();
      return false;
    }

    if (status === 'failed' && isRecent(payload.updated_at)) {
      setButtonState('failed', 'Update failed');
      resetButtonSoon();
      return false;
    }

    setButtonState('idle', 'Update');
    button.disabled = false;
    return false;
  };

  const poll = async () => {
    try {
      const response = await fetch(statusUrl, { cache: 'no-store' });
      if (!response.ok) throw new Error('Update status failed');
      const payload = await response.json();
      const keepPolling = applyStatus(payload);
      if (keepPolling) pollTimer = window.setTimeout(poll, 1400);
    } catch {
      button.disabled = false;
      button.classList.remove('is-updating');
      label.textContent = 'Update';
    }
  };

  form.addEventListener('submit', () => {
    if (pollTimer) window.clearTimeout(pollTimer);
    setButtonState('checking', 'Checking…');
  });

  poll();
})();
