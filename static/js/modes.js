"use strict";

// Flask renders one button for each approved role. Storage remembers only the selected mode.
(() => {
  const modeSwitch = document.getElementById("modeSwitch");
  if (!modeSwitch) return;

  const buttons = [...modeSwitch.querySelectorAll(".mode-btn")];
  const availableModes = buttons.map(button => button.dataset.mode);
  const storageKey = "l2_activeMode_" + encodeURIComponent(modeSwitch.dataset.accountId);
  let savedMode;
  try { savedMode = localStorage.getItem(storageKey); } catch (e) { /* Storage is optional. */ }

  let activeMode = availableModes.includes(savedMode)
    ? savedMode
    : ["mentor", "coach", "student"].find(mode => availableModes.includes(mode));

  function applyMode() {
    document.querySelectorAll("[data-show-for]").forEach(element => {
      element.classList.toggle("hidden", !element.dataset.showFor.split(/\s+/).includes(activeMode));
    });
    modeSwitch.classList.toggle("hidden", buttons.length < 2);
    buttons.forEach(button => {
      const selected = button.dataset.mode === activeMode;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
  }

  modeSwitch.addEventListener("click", event => {
    const mode = event.target.closest(".mode-btn")?.dataset.mode;
    if (!availableModes.includes(mode) || mode === activeMode) return;
    activeMode = mode;
    try { localStorage.setItem(storageKey, activeMode); } catch (e) { /* Mode still works for this page. */ }
    applyMode();
  });

  applyMode();
})();
