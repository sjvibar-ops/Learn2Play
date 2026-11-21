const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
function element() {
  const classes = new Set();
  return { listeners: {}, attributes: {}, type: "password", value: "", textContent: "", style: {}, disabled: false,
    classList: { add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x),
      toggle(x, on) { if (on === undefined) on = !classes.has(x); on ? classes.add(x) : classes.delete(x); } },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    setAttribute(name, value) { this.attributes[name] = value; },
    removeAttribute(name) { delete this.attributes[name]; },
    focus() { this.focusCount = (this.focusCount || 0) + 1; },
    replaceChildren(){}, querySelector: () => null, contains: () => false };
}
const source = name => fs.readFileSync(path.join(__dirname, "../static/js", name), "utf8");
async function loginControls() {
  const ids = ["form", "email", "password", "submit", "btnLabel", "note", "eyeBtn", "eyeOpen", "eyeClosed", "remember", "forgot"];
  const els = Object.fromEntries(ids.map(id => [id, element()]));
  let resolveRequest, requests = 0;
  vm.runInNewContext(source("auth.js"), {
    document: { getElementById: id => els[id], querySelector: () => ({content:"csrf-test"}) },
    localStorage: {}, window: { location: { assign() { throw Error("Must not redirect on failed login"); } } },
    fetch: async (url, options) => {
      requests++;
      assert.equal(url, "/api/auth/login");
      assert.equal(options.headers["X-CSRFToken"], "csrf-test");
      return new Promise(resolve => { resolveRequest = resolve; });
    }
  });
  els.eyeBtn.listeners.click();
  assert.equal(els.password.type, "text");
  assert.equal(els.eyeBtn.attributes["aria-pressed"], "true");
  assert.equal(els.eyeBtn.attributes["aria-label"], "Hide password");
  els.eyeBtn.listeners.click();
  assert.equal(els.password.type, "password");
  assert.equal(els.eyeBtn.attributes["aria-pressed"], "false");
  els.email.value = "learner@example.test"; els.password.value = "Password1";
  const pending = els.form.listeners.submit({preventDefault(){}});
  await new Promise(setImmediate);
  assert.equal(els.submit.disabled, true);
  assert.equal(els.form.attributes["aria-busy"], "true");
  assert.equal(els.submit.attributes["aria-busy"], "true");
  assert.match(els.note.textContent, /Logging in/);
  await els.form.listeners.submit({preventDefault(){}});
  assert.equal(requests, 1);
  resolveRequest({ok:false, status:401, json:async () => ({message:"Incorrect email or password."})});
  await pending;
  assert.equal(els.submit.disabled, false);
  assert.equal(els.form.attributes["aria-busy"], undefined);
  assert.equal(els.submit.attributes["aria-busy"], undefined);
  assert.equal(els.note.textContent, "Incorrect email or password.");
}
async function dropdownEscape(filename, avgRating = null) {
  const els = Object.fromEntries(["profileMenu", "userAvatar", "profileDropdown"].map(id => [id, element()]));
  if (filename === "profile.js") els.statRating = element();
  const listeners = {};
  els.profileDropdown.classList.add("hidden");
  vm.runInNewContext(source(filename), {
    document: {
      body: {dataset:{}}, querySelector: () => null, querySelectorAll: () => [],
      getElementById: id => els[id] || null, addEventListener: (name, fn) => {listeners[name] = fn;}
    },
    window: {setInterval(){}, location:{search:""}},
    localStorage: {getItem: () => null}, URLSearchParams,
    fetch: async () => ({ok:true, json:async () => ({ok:true, bookings:[], user:{name:"Learner", roles:["student"], stats:{avg_rating:avgRating}}})}), console
  });
  await new Promise(setImmediate);
  if (filename === "profile.js") {
    const value = Number(avgRating);
    const valid = avgRating != null && Number.isFinite(value) && value > 0 && value <= 5;
    assert.equal(els.statRating.textContent, valid ? value.toFixed(1) : "—");
    assert.equal(els.statRating.title, valid ? "" : "No ratings yet");
  }
  listeners.keydown({key:"Escape"});
  assert.equal(els.userAvatar.focusCount, undefined);
  els.userAvatar.listeners.click({stopPropagation(){}});
  assert.equal(els.userAvatar.attributes["aria-expanded"], "true");
  listeners.keydown({key:"Escape"});
  assert.equal(els.profileDropdown.classList.contains("hidden"), true);
  assert.equal(els.userAvatar.attributes["aria-expanded"], "false");
  assert.equal(els.userAvatar.focusCount, 1);
  listeners.keydown({key:"Escape"});
  assert.equal(els.userAvatar.focusCount, 1);
}
(async () => {
  for (const filename of ["navbar.js", "profile.js", "sessions.js", "script.js"]) await dropdownEscape(filename);
  for (const rating of ["4.5", 0, -1, 6, Infinity, NaN]) await dropdownEscape("profile.js", rating);
  await loginControls();
  console.log("Login accessibility, profile ratings and all four dropdown entrypoints passed.");
})().catch(error => {console.error(error); process.exitCode = 1;});
