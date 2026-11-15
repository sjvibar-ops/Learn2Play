const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { runInNewContext } = require("node:vm");

const source = name => readFileSync(join(__dirname, "../static/js", name), "utf8");

function element(dataset = {}) {
  const classes = new Set();
  const listeners = new Map();
  return {
    dataset, classes, listeners, value: "", textContent: "", attributes: {},
    classList: {
      add: name => classes.add(name),
      remove: name => classes.delete(name),
      toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
    },
    addEventListener: (name, listener) => listeners.set(name, listener),
    setAttribute(name, value) { this.attributes[name] = value; },
    querySelectorAll: () => [],
  };
}

function openModes(roles, storage) {
  const modeSwitch = element({ accountId: "42" });
  const student = element({ showFor: "student" });
  const provider = element({ showFor: "mentor coach" });
  const buttons = roles.map(mode => element({ mode }));
  modeSwitch.querySelectorAll = () => buttons;
  runInNewContext(source("modes.js"), {
    document: {
      getElementById: () => modeSwitch,
      querySelectorAll: () => [student, provider],
    },
    localStorage: {
      getItem(key) { assert.equal(key, "l2_activeMode_42"); return storage.get(key); },
      setItem(key, value) { storage.set(key, value); },
    },
  });
  return {
    active: () => buttons.find(button => button.attributes["aria-pressed"] === "true")?.dataset.mode,
    choose: mode => modeSwitch.listeners.get("click")({ target: { closest: () => ({ dataset: { mode } }) } }),
    student, provider, modeSwitch,
  };
}

function checkModes() {
  const storage = new Map([
    ["l2_activeMode_42", "coach"],
    ["l2_user", JSON.stringify({ roles: ["student", "mentor", "coach"] })],
  ]);
  for (const role of ["student", "mentor", "coach"]) {
    const page = openModes([role], storage);
    assert.equal(page.active(), role);
    assert.equal(page.student.classes.has("hidden"), role !== "student");
    assert.equal(page.provider.classes.has("hidden"), role === "student");
    assert(page.modeSwitch.classes.has("hidden"));
    page.choose(role === "coach" ? "mentor" : "coach");
    assert.equal(page.active(), role);
  }
  const roles = ["student", "mentor", "coach"];
  const page = openModes(roles, storage);
  assert.equal(page.active(), "coach");
  assert(!page.modeSwitch.classes.has("hidden"));
  page.choose("student");
  assert.equal(page.active(), "student");
  assert(page.provider.classes.has("hidden"));
  assert.equal(openModes(roles, storage).active(), "student");
  assert.equal(openModes(["mentor", "coach"], storage).active(), "mentor");
}

async function checkDirectory() {
  const ids = ["tutorGrid", "searchInput", "price-range", "price-display", "sort-select",
    "results-count", "reset-filters"];
  const elements = Object.fromEntries(ids.map(id => [id, element()]));
  const [all, academic] = ["all", "academic"].map(cat => element({ cat }));
  const slider = elements["price-range"];
  slider.value = slider.defaultValue = slider.max = "50000";
  const sort = elements["sort-select"];
  sort.value = "recommended";
  const search = elements.searchInput;
  const price = elements["price-display"];
  const count = elements["results-count"];
  const tutors = [
    { id: 1, name: "Alyssa", category: "academic", price: 500 },
    { id: 2, name: "Ben", category: "gaming", price: 2000 },
    { id: 3, name: "Clara", category: "academic", price: 60000 },
  ];
  runInNewContext(source("script.js"), {
    document: {
      body: { dataset: {} },
      getElementById: id => elements[id] || null,
      querySelector: () => null,
      querySelectorAll: selector => selector === "[data-cat]" ? [all, academic] : [],
    },
    window: { location: { search: "?view=browse" } },
    URLSearchParams,
    fetch: async url => {
      assert.equal(url, "/api/tutors");
      return { ok: true, json: async () => ({ ok: true, tutors }) };
    },
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(count.textContent, "3 educators shown");
  slider.value = "1000";
  slider.listeners.get("input")();
  assert.equal(count.textContent, "1 educator shown");
  assert.match(price.textContent, /1,000/);
  academic.listeners.get("click")();
  search.value = "Alyssa";
  search.listeners.get("input")();
  sort.value = "name";
  sort.listeners.get("change")();

  elements["reset-filters"].listeners.get("click")();
  assert.equal(slider.value, slider.defaultValue);
  assert.equal(search.value, "");
  assert.equal(sort.value, "recommended");
  assert(all.classes.has("active"));
  assert(!academic.classes.has("active"));
  assert.equal(price.textContent, "Any rate");
  assert.equal(count.textContent, "3 educators shown");
  assert.match(elements.tutorGrid.innerHTML, /Ben/);
  assert.match(elements.tutorGrid.innerHTML, /Clara/);
}

checkModes();
checkDirectory().catch(error => { console.error(error); process.exitCode = 1; });
