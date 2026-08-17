(() => {
  const script = document.currentScript;
  const endpoint = script?.dataset.newStateUrl;
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
