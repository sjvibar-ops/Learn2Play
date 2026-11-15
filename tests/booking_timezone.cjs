const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { runInNewContext } = require("node:vm");

function loadDashboard(script, now, bookings, elementIds, tabs = []) {
  const elements = new Map(elementIds.map(id => [id, { textContent: "" }]));
  for (const element of elements.values()) {
    element.addEventListener = () => {};
    element.replaceChildren = text => { element.textContent = text; };
  }
  const document = {
    body: { dataset: {} },
    getElementById: id => elements.get(id) || null,
    querySelector: () => null,
    querySelectorAll: selector => selector === ".session-tab" ? tabs : [],
    addEventListener() {},
  };
  class FixedDate extends Date {
    static now() { return Date.parse(now); }
  }
  runInNewContext(readFileSync(join(__dirname, "../static/js", script), "utf8"), {
    document,
    window: { L2UserId: 42, setInterval() {} },
    Date: FixedDate,
    fetch: async url => ({
      ok: true,
      json: async () => ({
        "/api/bookings": { ok: true, bookings },
        "/api/me": { user: { roles: ["student"] } },
        "/api/availability": { ok: true, availability: [] },
      })[url],
    }),
    console,
  });
  return elements;
}

function booking(id, start_time, status = "accepted") {
  return { id, start_time, status, student_id: 42, provider_id: 42, topic: id, formatted: "PHT display" };
}

async function checkSessionFilters() {
  const fixtures = [
    booking("previous week", "2026-11-15T15:59:59Z"),
    booking("Monday midnight", "2026-11-15T16:00:00+00:00"),
    booking("before UTC midnight", "2026-11-15T23:59:59Z"),
    booking("after UTC midnight", "2026-11-16T00:00:00Z"),
    booking("last instant today", "2026-11-16T15:59:59Z"),
    booking("Tuesday midnight", "2026-11-16T16:00:00Z"),
    booking("last instant this week", "2026-11-22T15:59:59Z"),
    booking("next Monday midnight", "2026-11-22T16:00:00Z"),
    booking("cancelled today", "2026-11-16T01:00:00Z", "cancelled"),
  ];
  const cards = fixtures.map(b => ({
    dataset: { bookingStatus: b.status, startTime: b.start_time },
    textContent: b.topic,
  }));
  const tabs = ["upcoming", "today", "this-week"].map(status => ({
    dataset: { status },
    addEventListener(_, handler) { this.click = handler; },
    classList: { toggle() {} },
    setAttribute() {},
  }));
  const elements = loadDashboard("sessions.js", "2026-11-15T23:59:59Z", fixtures,
    ["sessionList", "upcomingCount", "thisWeekCount"], tabs);
  elements.get("sessionList").querySelectorAll = () => cards;
  await new Promise(resolve => setImmediate(resolve));

  const visible = () => cards.filter(card => !card.hidden).map(card => card.textContent);
  assert.deepEqual(visible(), [
    "before UTC midnight", "after UTC midnight", "last instant today", "Tuesday midnight",
    "last instant this week", "next Monday midnight",
  ]);
  assert.equal(elements.get("upcomingCount").textContent, "6");
  tabs[1].click();
  assert.deepEqual(visible(), [
    "Monday midnight", "before UTC midnight", "after UTC midnight", "last instant today",
  ]);
  tabs[2].click();
  assert.deepEqual(visible(), [
    "Monday midnight", "before UTC midnight", "after UTC midnight", "last instant today",
    "Tuesday midnight", "last instant this week",
  ]);
  assert.equal(elements.get("thisWeekCount").textContent, "6");
}

async function checkEducatorNextSession() {
  const ids = ["mentorHome", "mentorDashRequestList", "mentorDashNextSession", "mentorDashAvailability",
    "mentorDashPendingCount", "mentorDashUpcomingCount", "mentorDashCompletedCount"];
  const past = booking("Past session", "2026-11-15T15:59:58Z");
  const near = booking("Near session", "2026-11-15T16:00:00+00:00");
  const later = booking("Later session", "2026-11-16T00:00:00Z", "scheduled");
  const upcoming = loadDashboard("mentor-dashboard.js", "2026-11-15T15:59:59Z", [past, near, later], ids);
  const atStart = loadDashboard("mentor-dashboard.js", "2026-11-15T16:00:00Z", [near, later], ids);
  const afterStart = loadDashboard("mentor-dashboard.js", "2026-11-15T16:00:01Z", [near, later], ids);
  const pastOnly = loadDashboard("mentor-dashboard.js", "2026-11-15T15:59:59Z", [past], ids);
  await new Promise(resolve => setImmediate(resolve));

  const next = upcoming.get("mentorDashNextSession").innerHTML;
  assert.match(next, /Near session/);
  assert.match(next, /PHT display/);
  assert.doesNotMatch(next, /Past session|Later session/);
  assert.equal(upcoming.get("mentorDashUpcomingCount").textContent, 2);
  for (const page of [atStart, afterStart]) {
    const card = page.get("mentorDashNextSession").innerHTML;
    assert.match(card, /Later session/);
    assert.doesNotMatch(card, /Near session/);
  }
  assert.match(pastOnly.get("mentorDashNextSession").innerHTML, /No upcoming sessions/);
  assert.equal(pastOnly.get("mentorDashUpcomingCount").textContent, 0);
}

async function checkJoinVisibilityAroundStartAndEnd() {
  const session = {
    ...booking(91, "2026-11-15T16:00:00+00:00", "scheduled"),
    duration: 30,
    meeting_room: true,
  };
  const ids = ["sessionList", "upcomingCount", "mentorUpcomingList", "mentorUpcomingCount"];
  for (const [now, visible] of [
    ["2026-11-15T15:59:59Z", true],
    ["2026-11-15T16:00:01Z", true],
    ["2026-11-15T16:29:59Z", true],
    ["2026-11-15T16:30:00Z", false],
  ]) {
    const page = loadDashboard("sessions.js", now, [session], ids);
    const learnerList = page.get("sessionList");
    const cards = [];
    learnerList.querySelectorAll = () => cards;
    Object.defineProperty(learnerList, "innerHTML", {
      set(html) {
        for (const match of html.matchAll(/<article[^>]*data-booking-status="([^"]+)" data-start-time="([^"]+)" data-duration="([^"]*)"[^>]*>([\s\S]*?)<\/article>/g)) {
          const [, bookingStatus, startTime, duration, contents] = match;
          cards.push({ dataset: { bookingStatus, startTime, duration }, contents });
        }
      },
    });
    await new Promise(resolve => setImmediate(resolve));

    assert.equal(cards.length, 1);
    assert.equal(cards[0].hidden, !visible, now);
    assert.match(cards[0].contents, /join-session" data-id="91"/);
    assert.equal(page.get("upcomingCount").textContent, visible ? "1" : "0", now);
    const educatorList = page.get("mentorUpcomingList").innerHTML;
    assert.equal(/join-session" data-id="91"/.test(educatorList), visible, now);
    assert.equal(page.get("mentorUpcomingCount").textContent, visible ? "1" : "0", now);
  }
}

(async () => {
  await checkSessionFilters();
  await checkJoinVisibilityAroundStartAndEnd();
  await checkEducatorNextSession();
})().catch(error => { console.error(error); process.exitCode = 1; });
