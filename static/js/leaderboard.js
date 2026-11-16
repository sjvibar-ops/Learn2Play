(() => {
  const checkbox = document.getElementById("leaderboardOptIn");
  const preferenceStatus = document.getElementById("leaderboardPreferenceStatus");
  const listStatus = document.getElementById("leaderboardStatus");
  const week = document.getElementById("leaderboardWeek");
  const entries = document.getElementById("leaderboardEntries");
  const tableWrap = document.getElementById("leaderboardTableWrap");
  const emptyState = document.getElementById("leaderboardEmpty");
  const emptyTitle = document.getElementById("leaderboardEmptyTitle");

  async function loadBoard() {
    try {
      const response = await fetch("/api/leaderboard/weekly", { credentials: "same-origin", headers: { Accept: "application/json" } });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.message || "Could not load the leaderboard.");
      checkbox.checked = data.opted_in;
      preferenceStatus.textContent = data.opted_in
        ? "You opted in. Your display name and this week’s confirmed XP are visible."
        : "You are opted out. Opt in to appear in the rankings.";
      week.textContent = `${data.week_start} to ${data.week_end} (${data.timezone})`;
      entries.replaceChildren();
      if (!data.entries.length) {
        tableWrap.hidden = true;
        emptyState.hidden = false;
        emptyTitle.textContent = "No entries yet this week";
        listStatus.textContent = "No opted-in students have confirmed XP this week.";
        return;
      }
      emptyState.hidden = true;
      tableWrap.hidden = false;
      data.entries.forEach((entry) => {
        const row = document.createElement("tr");
        [entry.rank, entry.display_name, `${entry.weekly_xp} XP`].forEach((value) => {
          const cell = document.createElement("td");
          cell.textContent = String(value);
          row.appendChild(cell);
        });
        entries.appendChild(row);
      });
      listStatus.textContent = `${data.entries.length} opted-in students ranked this week.`;
    } catch (error) {
      tableWrap.hidden = true;
      emptyState.hidden = false;
      emptyTitle.textContent = "Leaderboard unavailable";
      listStatus.textContent = error.message || "Could not load the leaderboard.";
      preferenceStatus.textContent = "Your participation preference could not be loaded.";
    }
  }

  checkbox.addEventListener("change", async () => {
    checkbox.disabled = true;
    preferenceStatus.textContent = "Saving your choice…";
    try {
      const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
      const response = await fetch("/api/student/leaderboard-preference", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf, Accept: "application/json" },
        body: JSON.stringify({ opted_in: checkbox.checked }),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.message || "Could not save your choice.");
      preferenceStatus.textContent = data.opted_in ? "You opted in to the weekly leaderboard." : "You opted out and your name was removed from rankings.";
      await loadBoard();
    } catch (error) {
      checkbox.checked = !checkbox.checked;
      preferenceStatus.textContent = error.message || "Could not save your choice.";
    } finally {
      checkbox.disabled = false;
    }
  });

  loadBoard();
})();
