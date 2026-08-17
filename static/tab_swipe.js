(() => {
  const allTabLinks = [...document.querySelectorAll('a[data-app-tab]')];
  if (allTabLinks.length < 2) return;

  const linksByDest = new Map();
  const primaryOrder = [];
  allTabLinks.forEach((link) => {
    const dest = link.dataset.appTab;
    if (!dest) return;
    if (!linksByDest.has(dest)) {
      linksByDest.set(dest, []);
      primaryOrder.push(dest);
    }
    linksByDest.get(dest).push(link);
  });
  if (primaryOrder.length < 2) return;

  const normalizePath = (value) => {
    try {
      const path = new URL(value, window.location.origin).pathname;
      return path.length > 1 ? path.replace(/\/+$/, '') : path;
    } catch {
      return value;
    }
  };

  const currentPath = normalizePath(window.location.pathname);
  const tabPaths = primaryOrder.map((dest) => normalizePath(linksByDest.get(dest)[0].href));
  let currentIndex = tabPaths.indexOf(currentPath);

  if (currentIndex < 0) {
    currentIndex = tabPaths.findIndex((path) => currentPath === `${path}/` || `${currentPath}/` === path);
  }
  if (currentIndex < 0) return;

  const activeDest = primaryOrder[currentIndex];
  allTabLinks.forEach((link) => {
    const isActive = link.dataset.appTab === activeDest;
    link.classList.toggle('active', isActive);
    if (isActive) {
      link.setAttribute('aria-current', 'page');
    } else {
      link.removeAttribute('aria-current');
    }
  });

  if (!('ontouchstart' in window)) return;

  const swipeSurface = document.querySelector('#app-main');
  if (!swipeSurface) return;

  const interactiveSelector = [
    'a',
    'button',
    'input',
    'select',
    'textarea',
    'video',
    'audio',
    'iframe',
    'summary',
    '[contenteditable="true"]',
    '[role="button"]',
    '[role="tab"]',
    '[draggable="true"]',
    '[data-no-swipe]',
    '.secondary-nav',
    '.sub-nav'
  ].join(', ');
  const minDistance = 70;
  const maxDuration = 800;
  const directionBias = 1.35;
  const edgeGuard = 24;

  let startX = 0;
  let startY = 0;
  let startTime = 0;
  let tracking = false;

  swipeSurface.addEventListener('touchstart', (event) => {
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

  swipeSurface.addEventListener('touchend', (event) => {
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
    if (nextIndex < 0 || nextIndex >= primaryOrder.length) return;

    const nextDest = primaryOrder[nextIndex];
    const nextLink = linksByDest.get(nextDest)?.[0];
    if (nextLink) window.location.assign(nextLink.href);
  }, { passive: true });

  swipeSurface.addEventListener('touchcancel', () => {
    tracking = false;
  }, { passive: true });
})();
