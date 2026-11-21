const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { runInNewContext } = require("node:vm");

const source = readFileSync(join(__dirname, "../static/js/register.js"), "utf8");

async function checkSignup(choices, expectedRoles, expectedError = "") {
  const elements = new Map();
  const roleInputs = ["student", "mentor", "coach"].map(value => ({ value, checked: false }));
  const classes = new Set();
  const roleError = { textContent: "", closest: () => null };
  const requests = [];
  const saved = new Map();

  function getElementById(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        value: "", textContent: "", style: {}, listeners: new Map(),
        checked: choices.includes(id),
        classList: {
          add(name) { if (id === "verificationNotice") classes.add(name); },
          remove(name) { if (id === "verificationNotice") classes.delete(name); },
          toggle(name, on) { if (on) this.add(name); else this.remove(name); },
        },
        addEventListener(event, listener) { this.listeners.set(event, listener); },
        querySelector(selector) { return selector.includes('data-for="roles"') ? roleError : null; },
        querySelectorAll(selector) {
          if (selector === 'input[name="roles"]') return roleInputs;
          if (selector === 'input[name="roles"]:checked') return roleInputs.filter(input => input.checked);
          return [];
        },
        setAttribute() {}, removeAttribute() {}, focus() {},
        append() {},
      });
    }
    return elements.get(id);
  }

  for (const [id, value] of Object.entries({
    name: "Test Learner", email: "learner@example.test", password: "StrongPassword1",
    confirmPassword: "StrongPassword1", bio: "I have several years of experience teaching this topic.",
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
    window: { location: { assign: () => {} } },
    fetch: async (url, options) => {
      if (url === "/api/csrf-token") return {ok:true, json:async () => ({csrf_token:"test-csrf"})};
      assert.equal(url, "/api/auth/register");
      requests.push(options.body.getAll("roles"));
      return { ok: true, json: async () => ({ ok: true, message: "Submitted", user: {email:"learner@example.test",roles:["student"]} }) };
    },
  });

  await getElementById("regForm").listeners.get("submit")({ preventDefault() {} });
  assert.deepEqual(requests, [[]]);
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
  await checkSignup(["academicPath", "academicLearn"], ["student"]);
  await checkSignup(["academicPath", "academicTeach"], ["mentor"]);
  await checkSignup(["gamingPath", "gamingTeach"], ["coach"]);
  await checkSignup(["academicPath", "academicLearn", "gamingPath", "gamingTeach"], ["student", "coach"]);
  await checkSignup(["academicPath", "academicTeach", "gamingPath", "gamingTeach"], ["mentor", "coach"]);
  await checkSignup(["academicPath", "academicLearn", "gamingTeach"], ["student"]);
  await checkSignup([], [], "Choose at least one path to activate now.");
  await checkSignup(["academicPath", "academicLearn", "gamingPath"], [],
    "Choose Learn, Teach, or both for each enabled path.");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
