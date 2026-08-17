(() => {
  const script = document.currentScript;
  const endpoint = script?.dataset.newStateUrl;

  const favoriteFilter = document.querySelector('.favorite-filter[data-filter="favorites"]');
  const nonFavoriteFilter = document.querySelector('.favorite-filter[data-filter="not-favorites"]');
  const heartPath = "M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.08C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35Z";

  function installHeartIcon(button, outlined) {
    if (!button) return;
    button.replaceChildren();
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    svg.classList.add("filter-heart-icon");

    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", heartPath);
    path.classList.add(outlined ? "is-outline" : "is-filled");
    svg.appendChild(path);
    button.appendChild(svg);
  }

  if (favoriteFilter) {
    installHeartIcon(favoriteFilter, false);
    favoriteFilter.setAttribute("aria-label", "Show favorites");
    favoriteFilter.setAttribute("title", "Favorites");
  }

  if (nonFavoriteFilter) {
    installHeartIcon(nonFavoriteFilter, true);
    nonFavoriteFilter.setAttribute("aria-label", "Show clips that are not favorited");
    nonFavoriteFilter.setAttribute("title", "Not favorite");
  }

  if (!endpoint) return;

  fetch(endpoint)
    .then((response) => {
      if (!response.ok) throw new Error("NEW clip state request failed");
      return response.json();
    })
    .then((payload) => {
      const newIds = new Set(payload.candidate_window_ids || []);
      if (!newIds.size) return;

      document.querySelectorAll(".public-clip[data-candidate-id]").forEach((card) => {
        if (!newIds.has(card.dataset.candidateId)) return;
        const heading = card.querySelector(".public-title-row h2");
        if (!heading || heading.querySelector(".new-chip")) return;
        const chip = document.createElement("span");
        chip.className = "new-chip";
        chip.textContent = "NEW";
        chip.setAttribute("aria-label", "New clip from latest productive stream refresh");
        heading.appendChild(chip);
      });
    })
    .catch(() => {
      // NEW chips are supplemental UI; a state read failure should not affect Clips.
    });
})();
