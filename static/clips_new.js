(() => {
  const script = document.currentScript;
  const endpoint = script?.dataset.newStateUrl;

  const favoriteFilter = document.querySelector('.favorite-filter[data-filter="favorites"]');
  const nonFavoriteFilter = document.querySelector('.favorite-filter[data-filter="not-favorites"]');

  if (favoriteFilter) {
    favoriteFilter.textContent = "♥";
    favoriteFilter.setAttribute("aria-label", "Show favorites");
    favoriteFilter.setAttribute("title", "Favorites");
  }

  if (nonFavoriteFilter) {
    nonFavoriteFilter.textContent = "♡";
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
