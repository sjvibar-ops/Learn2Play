(() => {
  const checkbox = document.getElementById("leaderboardOptIn");
  const preferenceStatus = document.getElementById("leaderboardPreferenceStatus");
  const listStatus = document.getElementById("leaderboardStatus");
  const week = document.getElementById("leaderboardWeek");
  const entries = document.getElementById("leaderboardEntries");
  const podiumSection = document.getElementById("leaderboardPodiumSection");
  const podium = document.getElementById("leaderboardPodium");
  const tableCard = document.getElementById("leaderboardTableCard");
  const tableScope = document.getElementById("leaderboardTableScope");
  const tableWrap = document.getElementById("leaderboardTableWrap");
  const emptyState = document.getElementById("leaderboardEmpty");
  const emptyTitle = document.getElementById("leaderboardEmptyTitle");
  const progressStatus = document.getElementById("leaderboardProgressStatus");
  const progressStats = document.getElementById("leaderboardProgressStats");
  const yourXp = document.getElementById("leaderboardYourXp");
  const yourRank = document.getElementById("leaderboardYourRank");
  const rankGoal = document.getElementById("leaderboardRankGoal");
  const rankGoalLabel = document.getElementById("leaderboardRankGoalLabel");
  const rankGoalValue = document.getElementById("leaderboardRankGoalValue");
  const balancedStreak = document.getElementById("leaderboardBalancedStreak");
  const balancedStreakValue = document.getElementById("leaderboardBalancedStreakValue");
  const streakStatus = document.getElementById("leaderboardStreakStatus");
  const challengeStatus = document.getElementById("leaderboardChallengeStatus");
  const challengeProgress = document.getElementById("leaderboardChallengeProgress");
  const challengeCount = document.getElementById("leaderboardChallengeCount");
  const challengeTrack = document.getElementById("leaderboardChallengeTrack");
  const challengeFill = document.getElementById("leaderboardChallengeFill");
  let currentRank = null;

  function updateProgress(data, boardEntries = []) {
    currentRank = data.opted_in && data.your_progress?.rank ? Number(data.your_progress.rank) : null;
    progressStats.hidden = true;
    rankGoal.hidden = true;
    if (!data.opted_in) {
      progressStatus.textContent = "Opt in to see your weekly XP and rank here.";
      return;
    }
    if (!data.your_progress) {
      progressStatus.textContent = "Your weekly progress is unavailable right now.";
      return;
    }
    yourXp.textContent = `${data.your_progress.weekly_xp} XP`;
    yourRank.textContent = data.your_progress.rank ? `#${data.your_progress.rank}` : "-";
    progressStats.hidden = false;
    progressStatus.textContent = data.your_progress.rank
      ? "Your confirmed XP and rank for this week."
      : "No confirmed sessions yet this week. Complete one to appear in the rankings.";

    if (currentRank === 1) {
      rankGoalLabel.textContent = "Current standing";
      rankGoalValue.textContent = "Leading this week";
      rankGoal.hidden = false;
    } else if (currentRank > 1) {
      const ownEntry = boardEntries.find(entry => Number(entry.rank) === currentRank);
      const nextEntry = boardEntries.find(entry => Number(entry.rank) === currentRank - 1);
      if (ownEntry && nextEntry) {
        const xpGap = Math.max(1, Number(nextEntry.weekly_xp) - Number(ownEntry.weekly_xp) + 1);
        const xpNeeded = Math.ceil(xpGap / 50) * 50;
        rankGoalLabel.textContent = `To pass #${currentRank - 1}`;
        rankGoalValue.textContent = `+${xpNeeded.toLocaleString()} XP`;
        rankGoal.hidden = false;
      }
    }
  }

  async function loadLearningProgress() {
    try {
      const response = await fetch("/api/student/progress", { credentials: "same-origin", headers: { Accept: "application/json" } });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.message || "Could not load learning progress.");

      const challenge = data.quests?.balanced_week;
      const count = Number(challenge?.count);
      const target = Number(challenge?.target);
      if (Number.isFinite(count) && Number.isFinite(target) && target > 0) {
        const completed = Math.max(0, Math.min(target, count));
        challengeCount.textContent = `${completed} of ${target}`;
        challengeFill.style.width = `${completed / target * 100}%`;
        challengeTrack.setAttribute("aria-valuemax", String(target));
        challengeTrack.setAttribute("aria-valuenow", String(completed));
        challengeProgress.hidden = false;
        challengeStatus.textContent = completed >= target
          ? "Both learning paths are complete for this week."
          : "Complete tutoring and coaching sessions to finish this challenge.";
      } else {
        challengeProgress.hidden = true;
        challengeStatus.textContent = "Challenge progress is unavailable right now.";
      }

      const streak = Number(data.balance_streak_weeks);
      if (Number.isFinite(streak) && streak >= 0) {
        balancedStreakValue.textContent = `${streak} ${streak === 1 ? "week" : "weeks"}`;
        balancedStreak.hidden = false;
        streakStatus.textContent = "Consecutive weeks with both tutoring and coaching activity.";
      } else {
        balancedStreak.hidden = true;
        streakStatus.textContent = "Weekly streak information is unavailable.";
      }
    } catch (_error) {
      challengeProgress.hidden = true;
      challengeStatus.textContent = "Challenge progress is unavailable right now.";
      balancedStreak.hidden = true;
      streakStatus.textContent = "Weekly streak information is unavailable right now.";
    }
  }

  function getInitials(displayName) {
    return String(displayName || "?")
      .trim()
      .split(/\s+/)
      .slice(0, 2)
      .map((part) => part.charAt(0))
      .join("")
      .toUpperCase();
  }

  function createPodiumCrown() {
    const svgNamespace = "http://www.w3.org/2000/svg";
    const crown = document.createElementNS(svgNamespace, "svg");
    crown.setAttribute("class", "leaderboard-podium-crown");
    crown.setAttribute("viewBox", "0 0 32 28");
    crown.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(svgNamespace, "path");
    path.setAttribute("d", "M3 21 1.5 7.5l7 5L16 2l7.5 10.5 7-5L29 21H3Zm1 3h24v2H4z");
    crown.appendChild(path);
    return crown;
  }

  function renderPodium(boardEntries) {
    const topThree = boardEntries
      .filter((entry) => Number(entry.rank) >= 1 && Number(entry.rank) <= 3)
      .sort((a, b) => Number(a.rank) - Number(b.rank));
    podium.replaceChildren();

    topThree.forEach((entry) => {
      const rank = Number(entry.rank);
      const place = document.createElement("li");
      place.className = `leaderboard-podium-place leaderboard-podium-place--${rank}`;

      const card = document.createElement("div");
      card.className = "leaderboard-podium-person";
      const symbol = rank === 1 ? createPodiumCrown() : document.createElement("span");
      if (rank !== 1) {
        symbol.className = "leaderboard-podium-medal";
        symbol.textContent = String(rank);
        symbol.setAttribute("aria-hidden", "true");
      }
      card.appendChild(symbol);

      const avatar = document.createElement("span");
      avatar.className = "leaderboard-podium-avatar";
      avatar.textContent = getInitials(entry.display_name);
      avatar.setAttribute("aria-hidden", "true");
      card.appendChild(avatar);

      const rankLine = document.createElement("div");
      rankLine.className = "leaderboard-podium-rank-line";
      const rankNumber = document.createElement("span");
      rankNumber.className = "leaderboard-podium-number";
      rankNumber.textContent = `#${rank}`;
      const rankLabel = document.createElement("span");
      rankLabel.className = "leaderboard-podium-rank";
      rankLabel.textContent = rank === 1 ? "Weekly Champion" : `${rank === 2 ? "2nd" : "3rd"} Place`;
      rankLine.append(rankNumber, rankLabel);
      if (currentRank === rank) {
        place.classList.add("leaderboard-podium-place--current");
        const you = document.createElement("span");
        you.className = "leaderboard-you-badge";
        you.textContent = "YOU";
        rankLine.appendChild(you);
      }
      card.appendChild(rankLine);

      const name = document.createElement("h3");
      name.className = "leaderboard-podium-name";
      name.textContent = String(entry.display_name || "Student");
      card.appendChild(name);

      const xp = document.createElement("p");
      xp.className = "leaderboard-podium-xp";
      xp.textContent = `${(Number(entry.weekly_xp) || 0).toLocaleString()} XP`;
      card.appendChild(xp);

      const adjacentRank = rank === 1 ? 2 : rank - 1;
      const adjacent = topThree.find(candidate => Number(candidate.rank) === adjacentRank);
      if (adjacent) {
        const gap = document.createElement("p");
        gap.className = "leaderboard-podium-gap";
        const gapXp = Math.abs((Number(entry.weekly_xp) || 0) - (Number(adjacent.weekly_xp) || 0));
        gap.textContent = gapXp === 0
          ? `Tied on XP with #${adjacentRank}`
          : `${gapXp.toLocaleString()} XP ${rank === 1 ? "lead over" : "behind"} #${adjacentRank}`;
        card.appendChild(gap);
      }

      const base = document.createElement("div");
      base.className = "leaderboard-podium-base";
      base.textContent = String(rank);
      base.setAttribute("aria-hidden", "true");
      place.append(card, base);
      podium.appendChild(place);
    });
    podiumSection.hidden = topThree.length === 0;
  }

  function renderEntries(boardEntries) {
    renderPodium(boardEntries);
    entries.replaceChildren();
    const remainingEntries = boardEntries.filter((entry) => Number(entry.rank) > 3);
    const leadingXp = Math.max(...boardEntries.map((entry) => Number(entry.weekly_xp) || 0), 1);
    remainingEntries.forEach((entry) => {
      const row = document.createElement("tr");
      const isCurrentStudent = currentRank === Number(entry.rank);
      if (isCurrentStudent) row.classList.add("leaderboard-row-current");
      const rankCell = document.createElement("td");
      const rankBadge = document.createElement("span");
      rankBadge.className = "leaderboard-rank-badge";
      rankBadge.textContent = `#${entry.rank}`;
      rankCell.appendChild(rankBadge);

      const studentCell = document.createElement("td");
      const student = document.createElement("div");
      student.className = "leaderboard-student";
      const avatar = document.createElement("span");
      avatar.className = "leaderboard-student-avatar";
      avatar.setAttribute("aria-hidden", "true");
      avatar.textContent = getInitials(entry.display_name);
      const name = document.createElement("span");
      name.className = "leaderboard-student-name";
      name.textContent = String(entry.display_name || "Student");
      student.append(avatar, name);
      if (isCurrentStudent) {
        const you = document.createElement("span");
        you.className = "leaderboard-you-badge";
        you.textContent = "YOU";
        student.appendChild(you);
      }
      studentCell.appendChild(student);

      const xpCell = document.createElement("td");
      xpCell.className = "leaderboard-xp-cell";
      const xpLabel = document.createElement("strong");
      xpLabel.className = "leaderboard-xp-label";
      xpLabel.textContent = `${(Number(entry.weekly_xp) || 0).toLocaleString()} XP`;
      const xpTrack = document.createElement("span");
      xpTrack.className = "leaderboard-xp-track";
      xpTrack.setAttribute("aria-hidden", "true");
      const xpFill = document.createElement("span");
      xpFill.style.width = `${Math.max(6, Math.min(100, (Number(entry.weekly_xp) || 0) / leadingXp * 100))}%`;
      xpTrack.appendChild(xpFill);
      xpCell.append(xpLabel, xpTrack);

      row.append(rankCell, studentCell, xpCell);
      entries.appendChild(row);
    });
    tableWrap.hidden = remainingEntries.length === 0;
    emptyState.hidden = boardEntries.length > 0;
    tableCard.hidden = boardEntries.length > 0 && remainingEntries.length === 0;
    tableScope.textContent = boardEntries.length ? "Rank #4 onward" : "Top 50 participants";
  }

  async function loadBoard() {
    try {
      const response = await fetch("/api/leaderboard/weekly", { credentials: "same-origin", headers: { Accept: "application/json" } });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.message || "Could not load the leaderboard.");
      checkbox.checked = data.opted_in;
      updateProgress(data, data.entries || []);
      preferenceStatus.textContent = data.opted_in
        ? "You opted in. Your display name and this week’s confirmed XP are visible."
        : "You are opted out. Opt in to appear in the rankings.";
      week.textContent = `${data.week_start} to ${data.week_end} (${data.timezone})`;
      if (!data.entries.length) {
        emptyTitle.textContent = "No entries yet this week";
        listStatus.textContent = "No opted-in students have confirmed XP this week.";
        renderEntries([]);
        return;
      }
      renderEntries(data.entries);
      listStatus.textContent = `${data.entries.length} opted-in students ranked this week.`;
    } catch (error) {
      tableWrap.hidden = true;
      podiumSection.hidden = true;
      tableCard.hidden = false;
      emptyState.hidden = false;
      emptyTitle.textContent = "Leaderboard unavailable";
      week.textContent = "Leaderboard data could not be loaded.";
      listStatus.textContent = error.message || "Could not load the leaderboard.";
      preferenceStatus.textContent = "Your participation preference could not be loaded.";
      progressStatus.textContent = "Your weekly progress could not be loaded.";
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
  loadLearningProgress();
})();
