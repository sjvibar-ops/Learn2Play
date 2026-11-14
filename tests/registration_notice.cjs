const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { runInNewContext } = require("node:vm");

const elements = new Map();
let selectedRoles = ["student"];

function getElementById(id) {
  if (!elements.has(id)) {
    const classes = new Set(["hidden"]);
    const listeners = new Map();
    elements.set(id, {
      classes,
      listeners,
      classList: {
        toggle(name, enabled) {
          if (enabled) classes.add(name);
          else classes.delete(name);
        },
      },
      addEventListener(event, listener) {
        listeners.set(event, listener);
      },
      querySelector: () => null,
      querySelectorAll: () => selectedRoles.map((value) => ({ value })),
    });
  }
  return elements.get(id);
}

const source = readFileSync(join(__dirname, "../static/js/register.js"), "utf8");
runInNewContext(source, { document: { getElementById } });
const notice = getElementById("verificationNotice");
assert.equal(notice.classes.has("hidden"), true);

const changeRole = getElementById("rolesField").listeners.get("change");
for (const roles of [["mentor"], ["coach"], ["student", "coach"], ["student"], []]) {
  selectedRoles = roles;
  changeRole();
  assert.equal(notice.classes.has("hidden"), !roles.some((role) => role !== "student"));
}
