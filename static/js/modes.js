"use strict";

// Apply registration-time participation choices to the shared account-mode switch.
(() => {
  const ROLE_MODES = ["student", "mentor", "coach"];
  const modeSwitch = document.getElementById("modeSwitch");
  let currentUser = readCachedUser();
  let availableModes = getAvailableModes(currentUser);
  let activeMode = readSavedMode(currentUser) || defaultMode(availableModes);

  function readJson(value) {
    try { return value ? JSON.parse(value) : null; } catch (e) { return null; }
  }

  function readCachedUser() {
    let cached = null;
    try { cached = readJson(localStorage.getItem("l2_user")); } catch (e) { /* storage may be disabled */ }
    return attachLocalParticipation(cached || { roles: ["student"] });
  }

  function attachLocalParticipation(account) {
    const user = { ...account, roles: Array.isArray(account.roles) ? account.roles : [] };
    const email = String(user.email || "").trim().toLowerCase();
    if (!email) return user;
    try {
      const saved = readJson(localStorage.getItem(`l2_participation_${encodeURIComponent(email)}`));
      if (saved && saved.locked && saved.participation) {
        user.participation = saved.participation;
        user.participation_locked = true;
      }
    } catch (e) { /* keep the account data available if local storage is disabled */ }
    return user;
  }

  function getAvailableModes(user) {
    const choices = user?.participation;
    if (user?.participation_locked && choices && typeof choices === "object") {
      const selected = new Set();
      for (const path of ["academic", "gaming"]) {
        const choice = choices[path];
        if (!choice?.enabled || !Array.isArray(choice.roles)) continue;
        if (choice.roles.includes("learn")) selected.add("student");
        if (choice.roles.includes("teach")) selected.add(path === "academic" ? "mentor" : "coach");
      }
      const modes = ROLE_MODES.filter((role) => selected.has(role));
      if (modes.length) return modes;
    }

    const roles = (user?.roles || []).filter((role) => ROLE_MODES.includes(role));
    return roles.length ? ROLE_MODES.filter((role) => roles.includes(role)) : ["student"];
  }

  function modeStorageKey(user) {
    const identity = user?.id || String(user?.email || "local").trim().toLowerCase();
    return `l2_activeMode_${encodeURIComponent(String(identity))}`;
  }

  function readSavedMode(user) {
    try {
      const saved = localStorage.getItem(modeStorageKey(user));
      return availableModes.includes(saved) ? saved : null;
    } catch (e) { return null; }
  }

  function defaultMode(modes) {
    // Teaching is the default for provider accounts; learning is the student default.
    if (modes.includes("mentor")) return "mentor";
    if (modes.includes("coach")) return "coach";
    return modes[0] || "student";
  }

  function applyMode() {
    document.querySelectorAll("[data-show-for]").forEach((element) => {
      const modes = element.dataset.showFor.split(/\s+/);
      element.classList.toggle("hidden", !modes.includes(activeMode));
    });
    if (!modeSwitch) return;
    modeSwitch.classList.toggle("hidden", availableModes.length < 2);
    modeSwitch.querySelectorAll(".mode-btn").forEach((button) => {
      const mode = button.dataset.mode;
      const selected = mode === activeMode;
      button.classList.toggle("hidden", !availableModes.includes(mode));
      button.classList.toggle("active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
  }

  function saveActiveMode() {
    try { localStorage.setItem(modeStorageKey(currentUser), activeMode); } catch (e) { /* mode still works for this page */ }
  }

  function setMode(mode) {
    if (!availableModes.includes(mode) || mode === activeMode) return;
    activeMode = mode;
    saveActiveMode();
    applyMode();
    document.dispatchEvent(new CustomEvent("l2:modechange", { detail: { mode } }));
  }

  modeSwitch?.addEventListener("click", (event) => {
    const button = event.target.closest(".mode-btn");
    if (button) setMode(button.dataset.mode);
  });

  document.addEventListener("l2:participationchange", () => {
    currentUser = attachLocalParticipation(currentUser);
    availableModes = getAvailableModes(currentUser);
    if (!availableModes.includes(activeMode)) activeMode = defaultMode(availableModes);
    saveActiveMode();
    applyMode();
    window.L2.user = currentUser;
  });

  applyMode();
  window.L2 = { user: currentUser, getMode: () => activeMode, setMode };

  fetch("/api/me", {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  }).then((response) => {
    if (!response.ok) return null;
    return response.json();
  }).then((data) => {
    if (!data?.ok || !data.user) return;
    currentUser = attachLocalParticipation({ ...currentUser, ...data.user });
    availableModes = getAvailableModes(currentUser);
    activeMode = readSavedMode(currentUser) || defaultMode(availableModes);
    saveActiveMode();
    applyMode();
    window.L2.user = currentUser;
  }).catch(() => { /* keep cached modes if the account request is temporarily unavailable */ });
})();
