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

async function render({ entries = [], optedIn = false, progressRank, fetchError = false, progressError = false } = {}) {
  const ids = [
    "leaderboardOptIn", "leaderboardPreferenceStatus", "leaderboardStatus", "leaderboardWeek",
    "leaderboardEntries", "leaderboardPodiumSection", "leaderboardPodium", "leaderboardTableCard",
    "leaderboardTableScope", "leaderboardTableWrap", "leaderboardEmpty", "leaderboardEmptyTitle",
    "leaderboardProgressStatus", "leaderboardProgressStats", "leaderboardYourXp", "leaderboardYourRank",
    "leaderboardRankGoal", "leaderboardRankGoalLabel", "leaderboardRankGoalValue",
    "leaderboardBalancedStreak", "leaderboardBalancedStreakValue", "leaderboardStreakStatus",
    "leaderboardChallengeStatus", "leaderboardChallengeProgress", "leaderboardChallengeCount",
    "leaderboardChallengeTrack", "leaderboardChallengeFill",
  ];
  const elements = Object.fromEntries(ids.map(id => [id, new FakeElement()]));
  ["leaderboardPodiumSection", "leaderboardTableWrap", "leaderboardEmpty", "leaderboardProgressStats", "leaderboardRankGoal", "leaderboardBalancedStreak", "leaderboardChallengeProgress"]
    .forEach(id => { elements[id].hidden = true; });
  runInNewContext(source, {
    URLSearchParams,
    document: {
      getElementById: id => elements[id],
      createElement: tagName => new FakeElement(tagName),
      createElementNS: (_namespace, tagName) => new FakeElement(tagName),
      querySelector: () => ({ content: "test-csrf" }),
    },
    fetch: async url => {
      if (fetchError) throw new Error("Test network error");
      if (url === "/api/student/progress") {
        if (progressError) throw new Error("Test progress network error");
        return { ok: true, json: async () => ({
          ok: true,
          quests: { balanced_week: { count: 1, target: 2 } },
          balance_streak_weeks: 3,
        }) };
      }
      return { ok: true, json: async () => ({
        ok: true,
        opted_in: optedIn,
        your_progress: optedIn ? {
          weekly_xp: entries.find(entry => Number(entry.rank) === Number(progressRank ?? entries[0]?.rank))?.weekly_xp || 0,
          rank: progressRank ?? entries[0]?.rank ?? null,
        } : null,
        week_start: "2026-11-16",
        week_end: "2026-11-22",
        timezone: "Asia/Manila",
        entries,
      }) };
    },
  });
  await new Promise(resolve => setImmediate(resolve));
  return { elements };
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

  assert.match(stylesheet, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(stylesheet, /grid-template-columns: repeat\(3,minmax\(0,1fr\)\)/);
  assert.match(stylesheet, /leaderboard-podium-place--1, \.leaderboard-podium-place--2, \.leaderboard-podium-place--3 \{ grid-column: auto; grid-row: auto; \}/);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
