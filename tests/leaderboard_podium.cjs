const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { runInNewContext } = require("node:vm");

const source = readFileSync(join(__dirname, "../static/js/leaderboard.js"), "utf8");
const stylesheet = readFileSync(join(__dirname, "../static/css/leaderboard.css"), "utf8");

class FakeElement {
  constructor(tagName = "div") {
    this.tagName = tagName;
    this.children = [];
    this.attributes = {};
    this.style = {};
    this.listeners = new Map();
    this.classList = { add: name => { this.className = `${this.className || ""} ${name}`.trim(); } };
    this.textContent = "";
    this.hidden = false;
  }

  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
}

async function render({ entries = [], optedIn = false, progressRank, fetchError = false, progressError = false, boardErrors = [], saveError = false, yourProgress } = {}) {
  const ids = [
    "leaderboardOptIn", "leaderboardPreferenceStatus", "leaderboardStatus", "leaderboardWeek",
    "leaderboardEntries", "leaderboardPodiumSection", "leaderboardPodium", "leaderboardTableCard",
    "leaderboardTableScope", "leaderboardTableWrap", "leaderboardEmpty", "leaderboardEmptyTitle",
    "leaderboardProgressStatus", "leaderboardProgressStats", "leaderboardYourXp", "leaderboardYourRank",
    "leaderboardRankGoal", "leaderboardRankGoalLabel", "leaderboardRankGoalValue",
    "leaderboardBalancedStreak", "leaderboardBalancedStreakValue", "leaderboardStreakStatus",
    "leaderboardChallengeStatus", "leaderboardChallengeProgress", "leaderboardChallengeCount",
    "leaderboardChallengeTrack", "leaderboardChallengeFill", "leaderboardEmptyDescription", "leaderboardRetry",
  ];
  const elements = Object.fromEntries(ids.map(id => [id, new FakeElement()]));
  ["leaderboardPodiumSection", "leaderboardTableWrap", "leaderboardEmpty", "leaderboardProgressStats", "leaderboardRankGoal", "leaderboardBalancedStreak", "leaderboardChallengeProgress"]
    .forEach(id => { elements[id].hidden = true; });
  let boardCalls = 0;
  let serverOptedIn = optedIn;
  const requests = [];
  runInNewContext(source, {
    URLSearchParams,
    document: {
      getElementById: id => elements[id],
      createElement: tagName => new FakeElement(tagName),
      createElementNS: (_namespace, tagName) => new FakeElement(tagName),
      querySelector: () => ({ content: "test-csrf" }),
    },
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (url === "/api/student/leaderboard-preference") {
        if (saveError) return { ok: false, json: async () => ({ ok: false, message: "Save unavailable" }) };
        serverOptedIn = JSON.parse(options.body).opted_in;
        return { ok: true, json: async () => ({ ok: true, opted_in: serverOptedIn }) };
      }
      if (fetchError) throw new Error("Test network error");
      if (url === "/api/student/progress") {
        if (progressError) throw new Error("Test progress network error");
        return { ok: true, json: async () => ({
          ok: true,
          quests: { balanced_week: { count: 1, target: 2 } },
          balance_streak_weeks: 3,
        }) };
      }
      if (boardErrors[boardCalls++]) throw new Error("Refresh unavailable");
      return { ok: true, json: async () => ({
        ok: true,
        opted_in: serverOptedIn,
        your_progress: serverOptedIn ? (yourProgress || {
          weekly_xp: entries.find(entry => Number(entry.rank) === Number(progressRank ?? entries[0]?.rank))?.weekly_xp || 0,
          rank: progressRank ?? entries[0]?.rank ?? null,
        }) : null,
        week_start: "2026-11-16",
        week_end: "2026-11-22",
        timezone: "Asia/Manila",
        entries,
      }) };
    },
  });
  await new Promise(resolve => setImmediate(resolve));
  return { elements, requests };
}

async function main() {
  const empty = await render();
  assert.equal(empty.elements.leaderboardPodiumSection.hidden, true);
  assert.equal(empty.elements.leaderboardTableCard.hidden, false);
  assert.equal(empty.elements.leaderboardTableWrap.hidden, true);
  assert.equal(empty.elements.leaderboardEmpty.hidden, false);

  for (const entries of [
    [{ rank: 1, display_name: "First Learner", weekly_xp: 500 }],
    [
      { rank: 1, display_name: "First Learner", weekly_xp: 500 },
      { rank: 2, display_name: "Second Learner", weekly_xp: 400 },
    ],
    [
      { rank: 1, display_name: "First Learner", weekly_xp: 500 },
      { rank: 2, display_name: "Second Learner", weekly_xp: 400 },
      { rank: 3, display_name: "Third Learner", weekly_xp: 300 },
    ],
  ]) {
    const result = await render({ entries, optedIn: true });
    assert.equal(result.elements.leaderboardPodium.children.length, entries.length);
    assert.deepEqual(
      result.elements.leaderboardPodium.children.map(item => item.className.match(/place--(\d)/)[1]),
      entries.map(entry => String(entry.rank)),
    );
    assert.equal(result.elements.leaderboardTableCard.hidden, true);
    assert.equal(result.elements.leaderboardTableWrap.hidden, true);
    assert.equal(result.elements.leaderboardEmpty.hidden, true);
    assert.equal(result.elements.leaderboardRankGoal.hidden, false);
    assert.equal(result.elements.leaderboardRankGoalValue.textContent, "Leading this week");
    assert.equal(result.elements.leaderboardChallengeCount.textContent, "1 of 2");
    assert.equal(result.elements.leaderboardBalancedStreakValue.textContent, "3 weeks");
  }

  const fourEntries = [1, 2, 3, 4].map(rank => ({
    rank, display_name: `Learner ${rank}`, weekly_xp: 1000 - rank * 100,
  }));
  const four = await render({ entries: fourEntries, optedIn: true, progressRank: 4 });
  assert.equal(four.elements.leaderboardPodium.children.length, 3);
  assert.equal(four.elements.leaderboardTableCard.hidden, false);
  assert.equal(four.elements.leaderboardEntries.children.length, 1);
  assert.equal(four.elements.leaderboardEntries.children[0].children[0].children[0].textContent, "#4");
  assert.equal(four.elements.leaderboardEntries.children[0].children[2].children[0].textContent, "600 XP");
  assert.match(four.elements.leaderboardEntries.children[0].className, /leaderboard-row-current/);
  assert.equal(four.elements.leaderboardEntries.children[0].children[1].children[0].children[2].textContent, "YOU");
  assert.equal(four.elements.leaderboardRankGoalLabel.textContent, "To pass #3");
  assert.equal(four.elements.leaderboardRankGoalValue.textContent, "+150 XP");

  const failed = await render({ fetchError: true });
  assert.equal(failed.elements.leaderboardPodiumSection.hidden, true);
  assert.equal(failed.elements.leaderboardTableCard.hidden, false);
  assert.equal(failed.elements.leaderboardEmpty.hidden, false);
  assert.equal(failed.elements.leaderboardEmptyTitle.textContent, "Leaderboard unavailable");
  assert.equal(failed.elements.leaderboardChallengeProgress.hidden, true);
  assert.equal(failed.elements.leaderboardBalancedStreak.hidden, true);

  const optionalProgressFailed = await render({ entries: fourEntries, optedIn: true, progressRank: 4, progressError: true });
  assert.equal(optionalProgressFailed.elements.leaderboardPodium.children.length, 3);
  assert.equal(optionalProgressFailed.elements.leaderboardEntries.children.length, 1);
  assert.equal(optionalProgressFailed.elements.leaderboardChallengeProgress.hidden, true);
  assert.equal(optionalProgressFailed.elements.leaderboardBalancedStreak.hidden, true);

  const maliciousName = '<img src=x onerror="alert(1)">';
  const escaped = await render({ entries: [{ rank: 1, display_name: maliciousName, weekly_xp: 50 }] });
  assert.equal(escaped.elements.leaderboardPodium.children[0].children[0].children[3].textContent, maliciousName);

  const outsideTop50 = await render({ entries: fourEntries, optedIn: true, yourProgress: { weekly_xp: 50, rank: 51 } });
  assert.equal(outsideTop50.elements.leaderboardYourRank.textContent, "#51");
  assert.equal(outsideTop50.elements.leaderboardYourXp.textContent, "50 XP");
  assert.equal(outsideTop50.elements.leaderboardRankGoal.hidden, true);

  assert.equal(failed.elements.leaderboardOptIn.disabled, true);
  assert.equal(failed.elements.leaderboardRetry.hidden, false);
  assert.equal(failed.elements.leaderboardProgressStats.hidden, true);

  const retry = await render({ entries: fourEntries, boardErrors: [true, false] });
  retry.elements.leaderboardRetry.listeners.get("click")();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(retry.elements.leaderboardPodiumSection.hidden, false);
  assert.equal(retry.elements.leaderboardRetry.hidden, true);
  assert.equal(retry.elements.leaderboardOptIn.disabled, false);

  const saveFailed = await render({ entries: fourEntries, saveError: true });
  saveFailed.elements.leaderboardOptIn.checked = true;
  await saveFailed.elements.leaderboardOptIn.listeners.get("change")();
  assert.equal(saveFailed.elements.leaderboardOptIn.checked, false);
  assert.equal(saveFailed.elements.leaderboardPreferenceStatus.textContent, "Save unavailable");

  const refreshFailed = await render({ entries: fourEntries, optedIn: true, boardErrors: [false, true] });
  refreshFailed.elements.leaderboardOptIn.checked = false;
  await refreshFailed.elements.leaderboardOptIn.listeners.get("change")();
  assert.equal(refreshFailed.elements.leaderboardOptIn.checked, false);
  assert.match(refreshFailed.elements.leaderboardPreferenceStatus.textContent, /opted out/);
  assert.equal(refreshFailed.elements.leaderboardEntries.children.length, 0);
  assert.equal(refreshFailed.elements.leaderboardPodium.children.length, 0);
  assert.equal(refreshFailed.elements.leaderboardProgressStats.hidden, true);
  assert.equal(refreshFailed.elements.leaderboardRankGoal.hidden, true);
  const saveRequest = refreshFailed.requests.find(request => request.url.endsWith("leaderboard-preference"));
  assert.equal(saveRequest.options.headers["X-CSRFToken"], "test-csrf");
  assert.equal(saveRequest.options.credentials, "same-origin");
  assert.deepEqual(JSON.parse(saveRequest.options.body), { opted_in: false });

  assert.match(stylesheet, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(stylesheet, /grid-template-columns: repeat\(3,minmax\(0,1fr\)\)/);
  assert.match(stylesheet, /leaderboard-podium-place--1, \.leaderboard-podium-place--2, \.leaderboard-podium-place--3 \{ grid-column: auto; grid-row: auto; \}/);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
