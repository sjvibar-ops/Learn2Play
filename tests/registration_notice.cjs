const assert = require("node:assert/strict");
const { File } = require("node:buffer");
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
        focus() {},
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
    fetch: async (url, options) => {
      assert.equal(url, "/api/auth/register");
      requests.push(options.body.getAll("roles"));
      return { ok: true, json: async () => ({ ok: true, message: "Submitted" }) };
    },
  });

  const providerApplication = expectedRoles.some(role => role !== "student");
  assert.equal(classes.has("hidden"), !providerApplication);
  for (const path of ["academic", "gaming"]) {
    assert.equal(getElementById(path + "Learn").disabled, !choices.includes(path + "Path"));
    assert.equal(getElementById(path + "Teach").disabled, !choices.includes(path + "Path"));
  }

  if (providerApplication) {
    const proof = new File(["%PDF-1.4"], "certificate.pdf");
    getElementById("proofs").files = [proof];
    getElementById("proofs").listeners.get("change")();
  }
  await getElementById("regForm").listeners.get("submit")({ preventDefault() {} });
  assert.equal(roleError.textContent, expectedError);
  assert.deepEqual(requests, expectedError ? [] : [expectedRoles]);
  if (!expectedError) {
    const { participation } = JSON.parse(saved.get("l2_participation_learner%40example.test"));
    for (const path of ["academic", "gaming"]) {
      if (!choices.includes(path + "Path")) assert.deepEqual(participation[path], { enabled: false, roles: [] });
    }
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
