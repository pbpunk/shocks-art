(() => {
  const tabLinks = [...document.querySelectorAll('nav a[data-app-tab]')];
  if (tabLinks.length < 2) return;

  const normalizePath = (value) => {
    try {
      const path = new URL(value, window.location.origin).pathname;
      return path.length > 1 ? path.replace(/\/+$/, '') : path;
    } catch {
      return value;
    }
  };

  const currentPath = normalizePath(window.location.pathname);
  const tabPaths = tabLinks.map((link) => normalizePath(link.href));
  let currentIndex = tabPaths.indexOf(currentPath);

  // Clips can be reached through either the prefixed root or a trailing slash.
  if (currentIndex < 0) {
    currentIndex = tabPaths.findIndex((path) => currentPath === `${path}/` || `${currentPath}/` === path);
  }
  if (currentIndex < 0) return;

  tabLinks.forEach((link, index) => {
    const isActive = index === currentIndex;
    link.classList.toggle('active', isActive);
    if (isActive) {
      link.setAttribute('aria-current', 'page');
    } else {
      link.removeAttribute('aria-current');
    }
  });

  if (!('ontouchstart' in window)) return;

  const interactiveSelector = 'a, button, input, select, textarea, video, iframe, [contenteditable="true"], [role="button"]';
  const minDistance = 70;
  const maxDuration = 800;
  const directionBias = 1.35;
  const edgeGuard = 24;

  let startX = 0;
  let startY = 0;
  let startTime = 0;
  let tracking = false;

  document.addEventListener('touchstart', (event) => {
    if (event.touches.length !== 1) {
      tracking = false;
      return;
    }

    const touch = event.touches[0];
    const target = event.target instanceof Element ? event.target : null;
    const nearBrowserEdge = touch.clientX <= edgeGuard || touch.clientX >= window.innerWidth - edgeGuard;
    if (nearBrowserEdge || target?.closest(interactiveSelector)) {
      tracking = false;
      return;
    }

    startX = touch.clientX;
    startY = touch.clientY;
    startTime = performance.now();
    tracking = true;
  }, { passive: true });

  document.addEventListener('touchend', (event) => {
    if (!tracking || event.changedTouches.length !== 1) {
      tracking = false;
      return;
    }

    const touch = event.changedTouches[0];
    const deltaX = touch.clientX - startX;
    const deltaY = touch.clientY - startY;
    const elapsed = performance.now() - startTime;
    tracking = false;

    const horizontalEnough = Math.abs(deltaX) >= minDistance && Math.abs(deltaX) > Math.abs(deltaY) * directionBias;
    if (!horizontalEnough || elapsed > maxDuration) return;

    const nextIndex = deltaX < 0 ? currentIndex + 1 : currentIndex - 1;
    if (nextIndex < 0 || nextIndex >= tabLinks.length) return;

    window.location.assign(tabLinks[nextIndex].href);
  }, { passive: true });

  document.addEventListener('touchcancel', () => {
    tracking = false;
  }, { passive: true });
})();
