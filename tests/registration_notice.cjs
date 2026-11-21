const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { runInNewContext } = require("node:vm");

const source = readFileSync(join(__dirname, "../static/js/register.js"), "utf8");

async function checkSignup(intent, choices) {
  const elements = new Map();
  const requests = [];
  const saved = new Map();
  const navigations = [];
  const redirect = intent === "student" ? "/dashboard" : `/dashboard?onboarding=${intent}`;

  function getElementById(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        value: "", textContent: "", style: {}, listeners: new Map(),
        checked: choices.includes(id),
        classList: { add() {}, remove() {}, toggle() {} },
        addEventListener(event, listener) { this.listeners.set(event, listener); },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        setAttribute() {}, removeAttribute() {}, focus() {},
        append() {},
      });
    }
    return elements.get(id);
  }

  for (const [id, value] of Object.entries({
    name: "Test Learner", email: "learner@example.test", password: "StrongPassword1",
    confirmPassword: "StrongPassword1", registrationIntent: intent,
  })) getElementById(id).value = value;
  getElementById("ack").checked = true;
  const document = {
    getElementById,
    querySelector: () => ({ content: "test-csrf" }),
    createElement: () => ({ append() {}, addEventListener() {}, setAttribute() {} }),
  };
  runInNewContext(source, {
    document, FormData,
    localStorage: { setItem: (key, value) => saved.set(key, value) },
    window: { location: { assign: path => navigations.push(path) } },
    fetch: async (url, options) => {
      if (url === "/api/csrf-token") return {ok:true, json:async () => ({csrf_token:"test-csrf"})};
      assert.equal(url, "/api/auth/register");
      requests.push(options.body);
      return { ok: true, json: async () => ({
        ok: true, redirect, message: "Submitted",
        user: {email:"learner@example.test", roles:["student"]},
      }) };
    },
  });

  await getElementById("regForm").listeners.get("submit")({ preventDefault() {} });
  assert.equal(requests.length, 1);
  assert.equal(requests[0].get("registration_intent"), intent);
  assert.deepEqual(requests[0].getAll("roles"), []);
  assert.deepEqual(navigations, [redirect]);
  assert.equal(getElementById("submit").disabled, false);
  const cached = JSON.parse(saved.get("l2_user"));
  assert.deepEqual(cached.roles, ["student"]);
  const preference = JSON.parse(saved.get("l2_participation_learner%40example.test"));
  for (const path of ["academic", "gaming"]) {
    assert.equal(preference.participation[path].enabled, choices.includes(path + "Path"));
    assert.deepEqual(preference.participation[path].roles, choices.includes(path + "Path") ? ["learn"] : []);
  }

}

async function main() {
  await checkSignup("student", []);
  await checkSignup("student", ["academicPath", "gamingPath"]);
  await checkSignup("mentor", ["academicPath"]);
  await checkSignup("coach", ["gamingPath"]);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
