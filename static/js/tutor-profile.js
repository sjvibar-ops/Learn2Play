"use strict";
(() => {
  const button = document.getElementById("shareProfile");
  const feedback = document.getElementById("shareFeedback");
  if (!button || !feedback) return;
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      feedback.textContent = "Profile link copied.";
    } catch (_) {
      feedback.textContent = "Copy this profile URL from your browser’s address bar.";
    }
    window.setTimeout(() => { feedback.textContent = ""; }, 3200);
  });
})();
