"use strict";

// Provider applications happen after account creation.
(() => {
  const $ = (id) => document.getElementById(id);

  const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

  const form = $("regForm");

  // ---------- errors ----------
  function setError(field, message) {
    const slot = form.querySelector(`.field-error[data-for="${field}"]`);
    if (slot) {
      slot.textContent = message || "";
      const wrap = slot.closest(".r-field");
      if (wrap) wrap.classList.toggle("invalid", Boolean(message));
    }
  }
  function clearErrors() {
    form.querySelectorAll(".field-error").forEach((e) => (e.textContent = ""));
    form.querySelectorAll(".invalid").forEach((e) => e.classList.remove("invalid"));
    $("note").textContent = "";
  }
  function showNote(message) {
    $("note").textContent = message;
    $("note").style.color = "var(--danger)";
  }

  function setupPasswordToggle(inputId, buttonId, label) {
    const input = $(inputId);
    const button = $(buttonId);

    button.addEventListener("click", () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      button.querySelector(".r-eye-open").classList.toggle("hidden-svg", show);
      button.querySelector(".r-eye-closed").classList.toggle("hidden-svg", !show);
      button.setAttribute("aria-label", `${show ? "Hide" : "Show"} ${label}`);
      button.setAttribute("aria-pressed", String(show));
    });
  }

  setupPasswordToggle("password", "passwordToggle", "password");
  setupPasswordToggle("confirmPassword", "confirmPasswordToggle", "password confirmation");

  // ---------- password strength ----------
  function strength(pw) {
    let s = 0;
    if (pw.length >= 8) s++;
    if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) s++;
    if (/\d/.test(pw)) s++;
    if (/[^A-Za-z0-9]/.test(pw) || pw.length >= 12) s++;
    return s;
  }
  $("password").addEventListener("input", (e) => {
    const lvl = e.target.value ? strength(e.target.value) : 0;
    form.querySelector(".strength").dataset.level = lvl;
    $("strengthText").textContent =
      ["Use upper and lower case letters and a number.", "Weak", "Fair", "Good", "Strong"][lvl];
  });

  // ---------- validation ----------
  function validate() {
    const errs = {};
    const name = $("name").value.trim();
    const pw = $("password").value;

    if (name.length < 2 || name.length > 80) errs.name = "Enter your full name (2 to 80 characters).";
    if (!EMAIL_RE.test($("email").value.trim())) errs.email = "Enter a valid email address.";
    if (pw.length < 8 || !/[a-z]/.test(pw) || !/[A-Z]/.test(pw) || !/\d/.test(pw))
      errs.password = "Use at least 8 characters, with upper and lower case letters and a number.";
    if (pw !== $("confirmPassword").value) errs.confirmPassword = "Passwords don't match.";
    if (!$("ack").checked) errs.ack = "Confirm that you understand before you continue.";
    return errs;
  }

  // ---------- submit ----------
  
  async function csrf() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta?.content) return meta.content;
    const r = await fetch("/api/csrf-token", { credentials: "same-origin", headers: { Accept: "application/json" } });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.csrf_token) throw new Error("Couldn't start a secure request. Reload the page and try again.");
    return d.csrf_token;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if ($("submit").disabled) return;
    clearErrors();

    const errors = validate();
    if (Object.keys(errors).length > 0) {
      Object.entries(errors).forEach(([field, message]) => setError(field, message));
      return;
    }

    const body = new FormData();
    body.append("name", $("name").value.trim());
    body.append("email", $("email").value.trim());
    body.append("password", $("password").value);
    body.append("confirm_password", $("confirmPassword").value);
    body.append("registration_intent", $("registrationIntent").value);
    if ($("ack").checked) body.append("acknowledged", "true");

    $("submit").disabled = true;
    form.setAttribute("aria-busy", "true");
    $("btnLabel").textContent = "Creating account…";

    try {
      const token = await csrf();
      const res = await fetch("/api/auth/register", {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json", "X-CSRFToken": token }, // no Content-Type: browser sets the boundary
        body,
      });
      const data = await res.json().catch(() => ({}));

      if (!res.ok || !data.ok) {
        if (data.field && form.querySelector(`.field-error[data-for="${data.field}"]`)) {
          setError(data.field, data.message);
        } else {
          showNote(data.message || `Registration failed (${res.status}). Please try again.`);
        }
        return;
      }
      showDone(data);
      // The new account is already signed in; go straight to the dashboard.
      window.location.assign(data.redirect || "/dashboard");
      return;
    } catch (err) {
      showNote(err.message || "Something went wrong. Please try again.");
    } finally {
      $("submit").disabled = false;
      form.removeAttribute("aria-busy");
      $("btnLabel").textContent = "Create account";
    }
  });

  function showDone(data) {
    if (data.user) {
      try {
        const participation = Object.fromEntries(["academic", "gaming"].map(path => {
          const enabled = Boolean($(path + "Path")?.checked);
          return [path, {enabled, roles: enabled ? ["learn"] : []}];
        }));
        const email = String(data.user.email || $("email").value).trim().toLowerCase();
        localStorage.setItem(`l2_participation_${encodeURIComponent(email)}`, JSON.stringify({participation, locked:true}));
        localStorage.setItem("l2_user", JSON.stringify({...data.user, participation, participation_locked:true}));
      } catch (e) { /* The authenticated Flask session still works without localStorage. */ }
    }
    form.classList.add("hidden");
    const done = $("done");
    done.classList.remove("hidden");
    $("doneTitle").textContent = "You're all set";
    $("doneText").textContent = data.message;
    const link = $("doneLink");
    link.href = data.redirect || "/dashboard";
    link.textContent = "Go to dashboard";
    done.focus();
  }
})();
