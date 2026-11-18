"use strict";

let currentSettingsAccount = null;

// ---------- Helper: Show Toast Notification ----------
function showSettingsToast(message, isError = false) {
    const toast = document.getElementById("settingsToast");
    if (!toast) return;
    toast.textContent = message;
    toast.style.borderColor = isError ? "var(--danger)" : "var(--purple)";
    toast.classList.add("show");
    setTimeout(() => {
        toast.classList.remove("show");
    }, 3500);
}

// ---------- 1. Tab Switching System ----------
(function initSettingsTabs() {
    const tabButtons = document.querySelectorAll(".settings-tab-btn");
    const tabPanels = document.querySelectorAll(".settings-tab-panel");

    function activateTab(tabId) {
        tabButtons.forEach(btn => {
            const isActive = btn.dataset.tab === tabId;
            btn.classList.toggle("active", isActive);
            btn.setAttribute("aria-selected", isActive ? "true" : "false");
        });

        tabPanels.forEach(panel => {
            const isTarget = panel.id === `tab-${tabId}`;
            panel.classList.toggle("active", isTarget);
        });

        // Update URL hash without forcing a jump
        if (history.replaceState) {
            history.replaceState(null, "", `#${tabId}`);
        }
    }

    tabButtons.forEach(button => {
        button.addEventListener("click", () => {
            activateTab(button.dataset.tab);
        });
    });

    // Check URL hash on page load (e.g. settings.html#security)
    const initialHash = window.location.hash.replace("#", "");
    if (initialHash && document.getElementById(`tab-${initialHash}`)) {
        activateTab(initialHash);
    }
})();

// ---------- 2. Load Account Details (/api/me) ----------
(async function loadAccountSettings() {
    try {
        const response = await fetch("/api/me", {
            credentials: "same-origin",
            headers: { Accept: "application/json" }
        });
        if (response.status === 401) {
            window.location.href = "/auth";
            return;
        }
        const data = await response.json();
        if (!response.ok || !data.ok || !data.user) {
            throw new Error("Could not load account details.");
        }

        const user = data.user;
        currentSettingsAccount = user;
        const name = user.name || "User";

        // Update profile card text & initials
        const nameEl = document.getElementById("settingsName");
        const emailEl = document.getElementById("settingsEmail");
        const avatarEl = document.getElementById("settingsAvatar");
        const topbarAvatarEl = document.getElementById("topbarAvatar");

        if (nameEl) nameEl.textContent = name;
        if (emailEl) emailEl.textContent = user.email || "";

        // ✅ WITH THIS (Renders picture or falls back to initials):
const initials = name.trim().split(/\s+/).filter(Boolean).map(part => part[0]).join("").slice(0, 2).toUpperCase() || "U";

function renderAvatar(el, pic, text) {
    if (!el) return;
    el.replaceChildren();
    if (pic) {
        const img = document.createElement("img");
        img.src = pic;
        img.alt = "Avatar";
        img.className = "avatar-img";
        img.onerror = () => { el.textContent = text; };
        el.appendChild(img);
    } else {
        el.textContent = text;
    }
}

renderAvatar(avatarEl, user.profile_picture, initials);
renderAvatar(topbarAvatarEl, user.profile_picture, initials.slice(0, 1));

        // Pre-fill form inputs
        const nameParts = name.trim().split(/\s+/).filter(Boolean);
        const firstNameInput = document.getElementById("settingsFirstName");
        const lastNameInput = document.getElementById("settingsLastName");
        const emailInput = document.getElementById("settingsProfileEmail");

        if (firstNameInput) firstNameInput.value = nameParts[0] || "";
        if (lastNameInput) lastNameInput.value = nameParts.slice(1).join(" ");
        if (emailInput) emailInput.value = user.email || "";

        // Populate Roles cleanly (avoids duplication)
        const rolesContainer = document.getElementById("settingsRoles");
        if (rolesContainer) {
            rolesContainer.innerHTML = "";
            const labels = { student: "Student", mentor: "Teacher", coach: "Gaming Coach" };
            (user.roles || []).forEach(role => {
                const pill = document.createElement("span");
                pill.className = "pill " + (role === "coach" ? "g" : "a");
                pill.textContent = labels[role] || role.charAt(0).toUpperCase() + role.slice(1);
                rolesContainer.appendChild(pill);
            });
        }
    } catch (error) {
        console.warn("Notice: /api/me endpoint response:", error.message);
    }
})();

// ---------- 3. Save Personal Information (/api/profile) ----------
const profileForm = document.getElementById("profileInformationForm");
if (profileForm) {
    profileForm.addEventListener("submit", async event => {
        event.preventDefault();
        const message = document.getElementById("profileInformationMessage");
        const button = document.getElementById("profileInformationSave");
        const firstName = document.getElementById("settingsFirstName").value.trim();
        const lastName = document.getElementById("settingsLastName").value.trim();

        if (!firstName || firstName.length < 2) {
            if (message) message.textContent = "First name must contain at least 2 characters.";
            return;
        }

        button.disabled = true;
        const originalText = button.textContent;
        button.textContent = "Saving…";
        if (message) message.textContent = "";

        try {
            const csrfResponse = await fetch("/api/csrf-token", { credentials: "same-origin" });
            const csrfData = await csrfResponse.json();
            if (!csrfResponse.ok || !csrfData.csrf_token) {
                throw new Error("Could not prepare profile update session.");
            }

            const fullName = [firstName, lastName].filter(Boolean).join(" ");
            const response = await fetch("/api/profile", {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    Accept: "application/json",
                    "X-CSRFToken": csrfData.csrf_token
                },
                body: JSON.stringify({
                    name: fullName,
                    bio: currentSettingsAccount?.bio || "",
                    profile_picture: currentSettingsAccount?.profile_picture || ""
                })
            });

            const data = await response.json();
            if (!response.ok || !data.ok) throw new Error(data.message || "Could not save profile.");

            currentSettingsAccount = { ...currentSettingsAccount, ...data.user };
            document.getElementById("settingsName").textContent = fullName;
            showSettingsToast("Profile information updated successfully.");
            if (message) message.textContent = "Changes saved!";
        } catch (error) {
            if (message) message.textContent = error.message || "Failed to update profile.";
            showSettingsToast(error.message || "Failed to update profile", true);
        } finally {
            button.disabled = false;
            button.textContent = originalText;
        }
    });
}

// ---------- 4. Password Strength & Rule Checklist ----------
const newPasswordInput = document.getElementById("newPassword");
if (newPasswordInput) {
    newPasswordInput.addEventListener("input", () => {
        const val = newPasswordInput.value;
        const meter = document.getElementById("passwordStrengthMeter");

        const hasLength = val.length >= 8;
        const hasUpper = /[A-Z]/.test(val);
        const hasLower = /[a-z]/.test(val);
        const hasNumber = /[0-9!@#$%^&*()_+\-=\[\]{};':"\\|,.<>\/?]/.test(val);

        document.getElementById("rule-length")?.classList.toggle("valid", hasLength);
        document.getElementById("rule-upper")?.classList.toggle("valid", hasUpper);
        document.getElementById("rule-lower")?.classList.toggle("valid", hasLower);
        document.getElementById("rule-number")?.classList.toggle("valid", hasNumber);

        let score = 0;
        if (hasLength) score++;
        if (hasUpper) score++;
        if (hasLower) score++;
        if (hasNumber) score++;

        if (meter) {
            meter.dataset.level = val.length === 0 ? "0" : score.toString();
        }
    });
}

// ---------- 5. Password Show/Hide Toggle Button ----------
document.querySelectorAll(".password-toggle-btn").forEach(btn => {
    btn.addEventListener("click", () => {
        const input = btn.closest(".input-wrap")?.querySelector("input");
        if (!input) return;
        const isPassword = input.type === "password";
        input.type = isPassword ? "text" : "password";
        btn.classList.toggle("active", isPassword);
    });
});

// ---------- 6. Update Password (/api/account/password) ----------
const passwordForm = document.getElementById("passwordForm");
if (passwordForm) {
    passwordForm.addEventListener("submit", async event => {
        event.preventDefault();
        const message = document.getElementById("passwordMessage");
        const button = document.getElementById("passwordSave");
        const currentPassword = document.getElementById("currentPassword").value;
        const newPassword = document.getElementById("newPassword").value;
        const confirmPassword = document.getElementById("confirmPassword").value;

        if (newPassword.length < 8) {
            if (message) message.textContent = "New password must be at least 8 characters.";
            return;
        }
        if (newPassword !== confirmPassword) {
            if (message) message.textContent = "The new passwords do not match.";
            return;
        }

        button.disabled = true;
        const originalText = button.textContent;
        button.textContent = "Updating…";
        if (message) message.textContent = "";

        try {
            const csrfResponse = await fetch("/api/csrf-token", { credentials: "same-origin" });
            const csrfData = await csrfResponse.json();
            if (!csrfResponse.ok || !csrfData.csrf_token) {
                throw new Error("Could not prepare password update.");
            }

            const response = await fetch("/api/account/password", {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    Accept: "application/json",
                    "X-CSRFToken": csrfData.csrf_token
                },
                body: JSON.stringify({ current_password: currentPassword, new_password: newPassword })
            });

            const data = await response.json();
            if (!response.ok || !data.ok) throw new Error(data.message || "Could not update password.");

            showSettingsToast("Password changed successfully!");
            if (message) message.textContent = data.message || "Password updated.";
            passwordForm.reset();
            const meter = document.getElementById("passwordStrengthMeter");
            if (meter) meter.dataset.level = "0";
            document.querySelectorAll(".rules-list li").forEach(li => li.classList.remove("valid"));
        } catch (error) {
            if (message) message.textContent = error.message || "Could not update password. Please verify current password.";
            showSettingsToast(error.message || "Password update failed.", true);
        } finally {
            button.disabled = false;
            button.textContent = originalText;
        }
    });
}

// ---------- 7. Preferences Storage Sync ----------
(function setupPreferences() {
    const storageKey = "l2_account_preferences";
    const defaults = {
        emailNotifications: true,
        bookingReminders: true,
        marketingOffers: false,
        soundEffects: true,
        twoFactor: false
    };

    let saved = {};
    try {
        saved = JSON.parse(localStorage.getItem(storageKey) || "{}");
    } catch (e) {
        saved = {};
    }

    document.querySelectorAll(".settings-toggle-input").forEach(input => {
        const preference = input.dataset.preference;
        if (!preference) return;

        input.checked = typeof saved[preference] === "boolean"
            ? saved[preference]
            : (defaults[preference] ?? false);

        input.addEventListener("change", () => {
            saved[preference] = input.checked;
            try {
                localStorage.setItem(storageKey, JSON.stringify(saved));
                showSettingsToast(`Preference updated.`);
            } catch (err) {
                console.error("Local storage error:", err);
            }
        });
    });
})();

// ---------- 8. Export & Danger Zone Handlers ----------
document.getElementById("exportDataBtn")?.addEventListener("click", () => {
    showSettingsToast("Preparing your archive... download will begin shortly.");
});

document.getElementById("deactivateBtn")?.addEventListener("click", () => {
    if (confirm("Are you sure you want to deactivate your Learn2Play account? You can reactivate by signing back in.")) {
        showSettingsToast("Deactivation request submitted.");
    }
});

document.getElementById("deleteAccountBtn")?.addEventListener("click", () => {
    const input = prompt("Type 'DELETE' to confirm permanent deletion of your account and all associated data:");
    if (input === "DELETE") {
        showSettingsToast("Account deletion in progress...", true);
    }
});

// ---------- 9. Roles & Access: Mentor / Coach applications (Batch 2.2A-1 status + 2.2A-2 submission) ----------
(function initRoleApplications() {
    const list = document.getElementById("roleApplicationsList");
    const messageEl = document.getElementById("roleApplicationsMessage");
    const retryBtn = document.getElementById("roleApplicationsRetry");
    const rolesTabBtn = document.querySelector('.settings-tab-btn[data-tab="roles"]');
    if (!list || !rolesTabBtn) return;

    const ROLE_META = {
        mentor: { title: "Academic Teacher (Mentor)", iconClass: "role-mentor-icon" },
        coach: { title: "Gaming Coach", iconClass: "role-coach-icon" }
    };
    const STATUS_META = {
        not_submitted: { label: "Not submitted", cls: "inactive", hint: "You have not applied for this role yet." },
        pending: { label: "Pending review", cls: "pending", hint: "Your application is being reviewed by our team." },
        approved: { label: "Approved", cls: "active", hint: "This role is active on your account." },
        rejected: { label: "Rejected", cls: "rejected", hint: "Your application was not approved." },
        revision_required: { label: "Revision required", cls: "revision", hint: "Please update your application based on the review notes." }
    };
    const NOTE_STATUSES = ["rejected", "revision_required"];
    // Used only until the first GET response supplies the real limits.
    const FALLBACK_LIMITS = {
        experience_min_length: 50, experience_max_length: 1500, max_links: 5,
        max_files: 5, max_file_bytes: 5 * 1024 * 1024, allowed_file_types: ["pdf", "jpg", "jpeg", "png"]
    };
    const LINK_PATTERN = /^https?:\/\/\S+\.\S+$/i;
    const MAX_LINK_LENGTH = 300; // mirrors the server; the server remains authoritative

    let requestSeq = 0;
    let limits = Object.assign({}, FALLBACK_LIMITS);
    const apps = {};          // latest application data per role
    const openForms = {};     // role -> { form, submitting }
    const notices = {};       // role -> { text, isError } shown inside the card

    function setMessage(text, isError) {
        if (!messageEl) return;
        messageEl.textContent = text || "";
        messageEl.classList.toggle("error", Boolean(text) && Boolean(isError));
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function formatDate(iso) {
        if (!iso) return "";
        const d = new Date(iso);
        return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    }

    // Filenames are user-supplied: always rendered through textContent, never innerHTML.
    function safeFilename(name) {
        const text = typeof name === "string" ? name.replace(/[\u0000-\u001f\u007f]/g, "").trim() : "";
        return text || "Unnamed file";
    }

    function formatBytes(bytes) {
        if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(bytes % (1024 * 1024) === 0 ? 0 : 1) + " MiB";
        if (bytes >= 1024) return Math.round(bytes / 1024) + " KiB";
        return bytes + " B";
    }

    function allowedExtensions() {
        const types = Array.isArray(limits.allowed_file_types) && limits.allowed_file_types.length
            ? limits.allowed_file_types : FALLBACK_LIMITS.allowed_file_types;
        return types.map(t => String(t).replace(/^\./, "").toLowerCase());
    }

    function fileExtension(name) {
        const idx = name.lastIndexOf(".");
        return idx === -1 ? "" : name.slice(idx + 1).toLowerCase();
    }

    // ----- Application form (one per role, independent) -----
    function buildForm(role, app) {
        const status = app ? app.status : "not_submitted";
        const existingCount = app && Array.isArray(app.credentials) ? app.credentials.length : 0;
        const remainingFiles = Math.max(limits.max_files - existingCount, 0);
        const isResubmit = NOTE_STATUSES.includes(status);

        const form = el("form", "role-app-form");
        form.noValidate = true;
        form.setAttribute("aria-label", (isResubmit ? "Resubmit " : "Apply as ") + ROLE_META[role].title);

        // Experience description (required)
        const descId = "roleAppDesc-" + role;
        const descField = el("div", "role-app-field");
        const descLabel = el("label", null, "Experience description (required)");
        descLabel.htmlFor = descId;
        const desc = el("textarea", "role-app-textarea");
        desc.id = descId;
        desc.name = "experience_description";
        desc.rows = 6;
        desc.maxLength = limits.experience_max_length;
        desc.value = app && typeof app.experience_description === "string" ? app.experience_description : "";
        const descCount = el("div", "role-app-help");
        const descErr = el("div", "role-app-field-error");
        descErr.setAttribute("role", "alert");
        function updateCount() {
            descCount.textContent = desc.value.trim().length + " / " + limits.experience_max_length +
                " characters (minimum " + limits.experience_min_length + ")";
        }
        desc.addEventListener("input", updateCount);
        updateCount();
        descField.append(descLabel, desc, descCount, descErr);

        // Supporting links (optional)
        const linksId = "roleAppLinks-" + role;
        const linksField = el("div", "role-app-field");
        const linksLabel = el("label", null, "Supporting links (optional)");
        linksLabel.htmlFor = linksId;
        const links = el("textarea", "role-app-textarea");
        links.id = linksId;
        links.name = "links";
        links.rows = 3;
        links.placeholder = "https://example.com/profile";
        links.value = app && Array.isArray(app.links) ? app.links.join("\n") : "";
        const linksHelp = el("div", "role-app-help",
            "One link per line, starting with http:// or https:// (up to " + limits.max_links + ").");
        const linksErr = el("div", "role-app-field-error");
        linksErr.setAttribute("role", "alert");
        linksField.append(linksLabel, links, linksHelp, linksErr);

        // Evidence files (optional)
        const filesId = "roleAppFiles-" + role;
        const filesField = el("div", "role-app-field");
        const filesLabel = el("label", null, "Supporting evidence files (optional)");
        filesLabel.htmlFor = filesId;
        const files = el("input", "role-app-file");
        files.type = "file";
        files.id = filesId;
        files.name = "proofs";
        files.multiple = true;
        files.accept = allowedExtensions().map(e => "." + e).join(",");
        files.disabled = remainingFiles === 0;
        const filesHelp = el("div", "role-app-help",
            allowedExtensions().map(e => e.toUpperCase()).join(", ") + " only, up to " + formatBytes(limits.max_file_bytes) +
            " each. " + (existingCount
                ? "This application already has " + existingCount + " file(s) (your earlier files are kept and are not re-uploaded); you can add " + remainingFiles + " more."
                : "You can add up to " + remainingFiles + " file(s)."));
        const fileList = el("ul", "role-app-selected-files");
        const filesErr = el("div", "role-app-field-error");
        filesErr.setAttribute("role", "alert");
        files.addEventListener("change", () => {
            fileList.replaceChildren();
            Array.from(files.files).forEach(f => {
                fileList.appendChild(el("li", null, safeFilename(f.name) + " (" + formatBytes(f.size) + ")"));
            });
            filesErr.textContent = "";
        });
        filesField.append(filesLabel, files, filesHelp, fileList, filesErr);

        // Form-level status + buttons
        const formMsg = el("div", "role-app-form-message");
        formMsg.setAttribute("role", "status");
        formMsg.setAttribute("aria-live", "polite");
        const actions = el("div", "role-app-form-actions");
        const submitBtn = el("button", "view-btn role-app-submit", isResubmit ? "Resubmit application" : "Submit application");
        submitBtn.type = "submit";
        const cancelBtn = el("button", "view-btn role-app-cancel", "Cancel");
        cancelBtn.type = "button";
        actions.append(submitBtn, cancelBtn);

        form.append(descField, linksField, filesField, formMsg, actions);

        const ctx = { role, form, desc, links, files, descErr, linksErr, filesErr, formMsg, submitBtn, cancelBtn, remainingFiles, submitting: false };

        cancelBtn.addEventListener("click", () => {
            if (ctx.submitting) return;
            closeForm(role); // nothing is sent
        });
        form.addEventListener("submit", event => {
            event.preventDefault();
            submitApplication(ctx);
        });
        return ctx;
    }

    function setSubmitting(ctx, on) {
        ctx.submitting = on;
        ctx.submitBtn.disabled = on;
        ctx.cancelBtn.disabled = on;
        ctx.desc.disabled = on;
        ctx.links.disabled = on;
        ctx.files.disabled = on || ctx.remainingFiles === 0;
        ctx.form.setAttribute("aria-busy", on ? "true" : "false");
        ctx.submitBtn.textContent = on ? "Submitting…" : ctx.submitBtn.dataset.label;
    }

    function clearFieldErrors(ctx) {
        ctx.descErr.textContent = "";
        ctx.linksErr.textContent = "";
        ctx.filesErr.textContent = "";
        ctx.formMsg.textContent = "";
        ctx.formMsg.classList.remove("error", "success");
    }

    function showFormError(ctx, message, field) {
        const target = { experience_description: ctx.descErr, links: ctx.linksErr, proofs: ctx.filesErr }[field];
        if (target) {
            target.textContent = message;
        } else {
            ctx.formMsg.textContent = message;
            ctx.formMsg.classList.add("error");
        }
    }

    // Returns { description, linkList, fileList } or null after showing the first problem.
    function validateForm(ctx) {
        clearFieldErrors(ctx);
        let ok = true;
        const description = ctx.desc.value.trim();
        if (description.length < limits.experience_min_length || description.length > limits.experience_max_length) {
            ctx.descErr.textContent = "Describe your experience in " + limits.experience_min_length + " to " +
                limits.experience_max_length + " characters (currently " + description.length + ").";
            ok = false;
        }

        const linkList = ctx.links.value.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
        if (linkList.length > limits.max_links) {
            ctx.linksErr.textContent = "You can add up to " + limits.max_links + " links.";
            ok = false;
        } else if (linkList.some(l => !LINK_PATTERN.test(l) || l.length > MAX_LINK_LENGTH)) {
            ctx.linksErr.textContent = "Each link must start with http:// or https:// and be at most " + MAX_LINK_LENGTH + " characters.";
            ok = false;
        }

        const fileList = Array.from(ctx.files.files);
        const exts = allowedExtensions();
        let fileProblem = "";
        if (fileList.length > ctx.remainingFiles) {
            fileProblem = "You can add " + ctx.remainingFiles + " more file(s) to this application.";
        } else {
            const bad = fileList.find(f => !exts.includes(fileExtension(f.name)));
            const big = fileList.find(f => f.size > limits.max_file_bytes);
            const empty = fileList.find(f => f.size === 0);
            if (bad) fileProblem = safeFilename(bad.name) + ": only " + exts.map(e => e.toUpperCase()).join(", ") + " files are allowed.";
            else if (big) fileProblem = safeFilename(big.name) + " is larger than " + formatBytes(limits.max_file_bytes) + ".";
            else if (empty) fileProblem = safeFilename(empty.name) + " is empty.";
        }
        if (fileProblem) {
            ctx.filesErr.textContent = fileProblem;
            ok = false;
        }
        return ok ? { description, linkList, fileList } : null;
    }

    async function fetchCsrfToken() {
        const response = await fetch("/api/csrf-token", { credentials: "same-origin", headers: { "Accept": "application/json" } });
        const data = await response.json();
        if (!response.ok || !data || !data.csrf_token) throw new Error("csrf");
        return data.csrf_token;
    }

    async function submitApplication(ctx) {
        if (ctx.submitting) return;
        const values = validateForm(ctx);
        if (!values) return;

        setSubmitting(ctx, true);
        const { role } = ctx;
        try {
            let token;
            try {
                token = await fetchCsrfToken();
            } catch (err) {
                showFormError(ctx, "Could not verify your session. Please refresh the page and try again.");
                setSubmitting(ctx, false);
                return;
            }

            const body = new FormData();
            body.append("experience_description", values.description);
            values.linkList.forEach(link => body.append("links", link));
            values.fileList.forEach(file => body.append("proofs", file, file.name));

            // Content-Type is intentionally not set: the browser adds the multipart boundary.
            const response = await fetch("/api/me/applications/" + encodeURIComponent(role), {
                method: "POST",
                credentials: "same-origin",
                headers: { "X-CSRFToken": token, "Accept": "application/json" },
                body
            });

            let data = null;
            try { data = await response.json(); } catch (err) { data = null; }
            const serverMessage = data && typeof data.message === "string" ? data.message : "";

            if (response.ok && data && data.ok === true) {
                notices[role] = { text: serverMessage || "Your application was submitted for review.", isError: false };
                closeForm(role, true);
                showSettingsToast(notices[role].text);
                await loadApplications({ force: true, keepNotices: true });
                return;
            }

            if (response.status === 409) {
                // State changed elsewhere (already pending/approved): show it and reload the real state.
                notices[role] = { text: serverMessage || "This application can no longer be changed. Its status has been refreshed.", isError: true };
                closeForm(role, true);
                await loadApplications({ force: true, keepNotices: true });
                return;
            }

            showFormError(ctx, serverMessage || "We couldn't submit your application. Please try again.", data && data.field);
            setSubmitting(ctx, false);
        } catch (err) {
            showFormError(ctx, "Network problem: your application was not confirmed. Please check your connection and try again.");
            setSubmitting(ctx, false);
        }
    }

    function openForm(role) {
        const card = document.getElementById("roleApp-" + role);
        const app = apps[role];
        if (!card || !app || app.can_submit !== true) return;
        if (openForms[role]) {
            openForms[role].desc.focus();
            return;
        }
        delete notices[role];
        const ctx = buildForm(role, app);
        ctx.submitBtn.dataset.label = ctx.submitBtn.textContent;
        openForms[role] = ctx;
        renderCard(card, role, app);
        ctx.desc.focus();
    }

    function closeForm(role, skipRender) {
        delete openForms[role];
        if (skipRender) return;
        const card = document.getElementById("roleApp-" + role);
        if (card && apps[role]) renderCard(card, role, apps[role]);
    }

    // ----- Cards -----
    function renderCard(card, role, app) {
        const meta = ROLE_META[role];
        const known = app && Object.prototype.hasOwnProperty.call(STATUS_META, app.status);
        const status = known ? app.status : "not_submitted";
        const sMeta = STATUS_META[status];
        const formCtx = openForms[role];
        card.replaceChildren();

        const icon = el("div", "role-icon-avatar " + meta.iconClass);
        icon.setAttribute("aria-hidden", "true");
        icon.textContent = role === "mentor" ? "T" : "C";

        const info = el("div", "role-info");
        info.appendChild(el("h3", null, meta.title));
        info.appendChild(el("p", null, sMeta.hint));

        const submittedAt = app ? formatDate(app.submitted_at) : "";
        const reviewedAt = app ? formatDate(app.reviewed_at) : "";
        if (submittedAt || reviewedAt) {
            const dates = [];
            if (submittedAt) dates.push("Submitted " + submittedAt);
            if (reviewedAt) dates.push("Reviewed " + reviewedAt);
            info.appendChild(el("p", "role-app-dates", dates.join(" · ")));
        }

        const notice = notices[role];
        if (notice) {
            const n = el("div", "role-app-notice " + (notice.isError ? "error" : "success"), notice.text);
            n.setAttribute("role", notice.isError ? "alert" : "status");
            info.appendChild(n);
        }

        if (app && NOTE_STATUSES.includes(status) && app.review_note) {
            const note = el("div", "role-app-note");
            note.appendChild(el("strong", null, "Reviewer notes"));
            note.appendChild(el("p", null, app.review_note));
            info.appendChild(note);
        }

        const creds = app && Array.isArray(app.credentials) ? app.credentials : [];
        if (creds.length) {
            const filesBox = el("div", "role-app-files");
            filesBox.appendChild(el("strong", null, "Submitted evidence"));
            const ul = el("ul");
            creds.forEach(c => ul.appendChild(el("li", null, safeFilename(c && c.name))));
            filesBox.appendChild(ul);
            info.appendChild(filesBox);
        }

        const side = el("div", "role-app-side");
        side.appendChild(el("span", "role-badge " + sMeta.cls, sMeta.label));

        if (app && app.can_submit === true) {
            const verb = NOTE_STATUSES.includes(status) ? "Resubmit" : "Apply";
            const btn = el("button", "view-btn role-app-action", verb);
            btn.type = "button";
            btn.setAttribute("aria-expanded", formCtx ? "true" : "false");
            btn.setAttribute("aria-controls", "roleAppForm-" + role);
            btn.disabled = Boolean(formCtx);
            btn.addEventListener("click", () => openForm(role));
            side.appendChild(btn);
        }

        card.appendChild(icon);
        card.appendChild(info);
        card.appendChild(side);
        if (formCtx) {
            formCtx.form.id = "roleAppForm-" + role;
            card.appendChild(formCtx.form);
        }
    }

    function renderLoading() {
        list.setAttribute("aria-busy", "true");
        list.querySelectorAll(".role-app-card").forEach(card => {
            const role = card.dataset.appRole;
            if (openForms[role]) return; // never wipe a form the user is filling in
            card.replaceChildren(el("p", "role-app-loading", "Loading " + ROLE_META[role].title + " status…"));
        });
    }

    function renderError() {
        list.setAttribute("aria-busy", "false");
        list.querySelectorAll(".role-app-card").forEach(card => {
            if (!openForms[card.dataset.appRole]) card.replaceChildren();
        });
        setMessage("Could not load your application status. Please try again.", true);
        if (retryBtn) retryBtn.hidden = false;
    }

    // options.force: also rebuild cards that have an open form (used after submit / 409).
    // options.keepNotices: keep inline success/error notices (otherwise cleared on a fresh tab open).
    async function loadApplications(options) {
        const opts = options || {};
        const seq = ++requestSeq;
        setMessage("");
        if (!opts.keepNotices) Object.keys(notices).forEach(k => delete notices[k]);
        if (retryBtn) retryBtn.hidden = true;
        renderLoading();
        try {
            const response = await fetch("/api/me/applications", {
                credentials: "same-origin",
                headers: { "Accept": "application/json" }
            });
            if (!response.ok) throw new Error("HTTP " + response.status);
            const data = await response.json();
            if (seq !== requestSeq) return; // a newer request superseded this one
            if (!data || data.ok !== true || !data.applications) throw new Error("Unexpected response");
            if (data.limits && typeof data.limits === "object") {
                limits = Object.assign({}, FALLBACK_LIMITS, data.limits);
            }
            list.querySelectorAll(".role-app-card").forEach(card => {
                const role = card.dataset.appRole;
                apps[role] = data.applications[role];
                const stillSubmittable = apps[role] && apps[role].can_submit === true;
                if (openForms[role]) {
                    // A plain refresh keeps a form the user is filling in; force or a lost right to submit closes it.
                    if (opts.force || !stillSubmittable) delete openForms[role];
                    else return;
                }
                renderCard(card, role, apps[role]);
            });
            list.setAttribute("aria-busy", "false");
        } catch (err) {
            if (seq !== requestSeq) return;
            renderError();
        }
    }

    rolesTabBtn.addEventListener("click", () => loadApplications());
    retryBtn?.addEventListener("click", () => loadApplications());

    // Roles tab already active on page load (e.g. settings#roles)
    if (document.getElementById("tab-roles")?.classList.contains("active")) {
        loadApplications();
    }
})();

// ---------- 10. Connected Accounts: Discord (Batch 3.1B-2B) ----------
// Module-level copy of the CSRF helper (the original lives inside initRoleApplications' closure).
async function fetchCsrfToken() {
    const response = await fetch("/api/csrf-token", { credentials: "same-origin", headers: { "Accept": "application/json" } });
    const data = await response.json();
    if (!response.ok || !data || !data.csrf_token) throw new Error("csrf");
    return data.csrf_token;
}

(function initDiscordConnectedAccounts() {
    const panel = document.getElementById("discordSettingsPanel");
    const securityTabBtn = document.querySelector('.settings-tab-btn[data-tab="security"]');
    if (!panel || !securityTabBtn) return;

    const el = id => document.getElementById(id);
    const body = el("discordBody");
    const views = {
        loading: el("discordLoadingState"),
        error: el("discordErrorState"),
        disconnected: el("discordDisconnectedState"),
        connected: el("discordConnectedState")
    };
    const errorMessage = el("discordErrorMessage");
    const retryBtn = el("discordRetryBtn");
    const connectLink = el("discordConnectLink");
    const usernameEl = el("discordUsername");
    const linkedAtEl = el("discordLinkedAt");
    const guildBadge = el("discordGuildBadge");
    const guildMessage = el("discordGuildMessage");
    const disconnectBtn = el("discordDisconnectBtn");
    const feedback = el("discordFeedback");

    const RELOAD_AFTER_MS = 30000;
    const DISCONNECT_LABEL = "Disconnect Discord";

    // Fixed client-side copy: server messages are never shown, so nothing unexpected can leak into the UI.
    const GUILD_META = {
        VERIFIED_MEMBER: { label: "Server Member", cls: "pill discord-pill g", text: "You are a verified member of the Learn2Play Discord server." },
        NOT_IN_GUILD: { label: "Not in Server", cls: "pill discord-pill discord-pill-warn", text: "Your Discord account is not in the Learn2Play Discord server yet. Join the Learn2Play Discord server with this account to access community features." },
        CHECK_UNAVAILABLE: { label: "Check Unavailable", cls: "pill discord-pill", text: "We couldn't check your server membership right now. This doesn't mean you're not a member. Please try again later." },
        BOT_CONFIG_ERROR: { label: "Verification Unavailable", cls: "pill discord-pill", text: "Server membership verification is temporarily unavailable. Please try again later." }
    };

    let statusSeq = 0;      // newest account-status request wins
    let guildSeq = 0;       // newest guild-status request wins
    let statusInFlight = false;
    let disconnecting = false;
    let lastLoadedAt = 0;
    let lastLoadOk = false;

    function showView(name) {
        Object.keys(views).forEach(key => { views[key].hidden = key !== name; });
        body.setAttribute("aria-busy", name === "loading" ? "true" : "false");
    }

    function setFeedback(text) { feedback.textContent = text || ""; }

    function setGuildBadge(label, cls) {
        guildBadge.textContent = label;
        guildBadge.className = cls;
    }

    function renderLoading() { setFeedback(""); showView("loading"); }

    function renderError(text) {
        errorMessage.textContent = text || "We couldn't check your Discord connection right now. Please try again.";
        showView("error");
    }

    function renderDisconnected() {
        // Clear every linked-account detail so nothing stale can reappear.
        usernameEl.textContent = "";
        linkedAtEl.textContent = "";
        linkedAtEl.hidden = true;
        guildMessage.textContent = "";
        setGuildBadge("Checking server...", "pill discord-pill");
        showView("disconnected");
    }

    function parseLinkedDate(value) {
        if (typeof value !== "string" || !value.trim()) return "";
        let iso = value.trim();
        // The API emits naive UTC timestamps; mark them as UTC so the local date is right.
        if (/^\d{4}-\d{2}-\d{2}T[\d:.]+$/.test(iso)) iso += "Z";
        const date = new Date(iso);
        if (Number.isNaN(date.getTime())) return "";
        return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    }

    function renderConnected(data) {
        const name = typeof data.username === "string" ? data.username.trim() : "";
        usernameEl.textContent = name || "Linked Discord account";   // textContent only: never HTML
        const dateText = parseLinkedDate(data.linked_at);
        linkedAtEl.textContent = dateText ? "Linked on " + dateText : "";
        linkedAtEl.hidden = !dateText;
        setGuildBadge("Checking server...", "pill discord-pill");
        guildMessage.textContent = "Checking your Discord server membership...";
        showView("connected");
    }

    function renderGuild(status) {
        // Anything unrecognised is treated as "unavailable", never as non-membership.
        const meta = GUILD_META[status] || GUILD_META.CHECK_UNAVAILABLE;
        setGuildBadge(meta.label, meta.cls);
        guildMessage.textContent = meta.text;
    }

    // Returns parsed JSON, or throws an Error whose .kind is "auth" | "http" | "format" | "network".
    async function getJson(url) {
        let response;
        try {
            response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
        } catch (err) {
            const e = new Error("network"); e.kind = "network"; throw e;
        }
        let redirectedToLogin = false;
        if (response.redirected) {
            try { redirectedToLogin = /^\/(auth|login)(\/|$)/.test(new URL(response.url, window.location.href).pathname); } catch (err) { redirectedToLogin = false; }
        }
        if (response.status === 401 || redirectedToLogin) {
            const e = new Error("auth"); e.kind = "auth"; throw e;
        }
        let data = null;
        try { data = await response.json(); } catch (err) { data = null; }
        if (!response.ok) { const e = new Error("http"); e.kind = "http"; e.status = response.status; throw e; }
        if (!data || typeof data !== "object") { const e = new Error("format"); e.kind = "format"; throw e; }
        return data;
    }

    function goToLogin() { window.location.href = "/auth"; }

    async function loadGuildStatus() {
        const seq = ++guildSeq;
        let status = "CHECK_UNAVAILABLE";
        try {
            const data = await getJson("/api/me/discord/guild-status");
            if (seq !== guildSeq) return;
            if (data.ok === true && typeof data.status === "string") status = data.status;
        } catch (err) {
            if (seq !== guildSeq) return;
            if (err.kind === "auth") { goToLogin(); return; }
        }
        if (status === "NOT_LINKED") {
            // Server says there is no link: reconcile the card with that.
            renderDisconnected();
            setFeedback("Your Discord account is no longer linked.");
            return;
        }
        renderGuild(status);
    }

    async function loadStatus(opts) {
        const force = !!(opts && opts.force);
        if (disconnecting) return;
        if (statusInFlight && !force) return;
        const seq = ++statusSeq;
        guildSeq++;                      // invalidate any older guild lookup
        statusInFlight = true;
        renderLoading();
        try {
            const data = await getJson("/api/me/discord");
            if (seq !== statusSeq) return;
            if (data.ok !== true || typeof data.is_linked !== "boolean") {
                const e = new Error("format"); e.kind = "format"; throw e;
            }
            lastLoadOk = true;
            lastLoadedAt = Date.now();
            if (!data.is_linked) {
                renderDisconnected();    // guild endpoint is deliberately not called
                return;
            }
            renderConnected(data);
            statusInFlight = false;
            await loadGuildStatus();
        } catch (err) {
            if (seq !== statusSeq) return;
            lastLoadOk = false;
            if (err.kind === "auth") { goToLogin(); return; }
            renderError(err.kind === "network"
                ? "We couldn't reach Learn2Play to check your Discord connection. Check your connection and try again."
                : "We couldn't check your Discord connection right now. Please try again.");
        } finally {
            if (seq === statusSeq) statusInFlight = false;
        }
    }

    async function disconnect() {
        if (disconnecting) return;
        if (!window.confirm("Disconnect your Discord account from Learn2Play?")) return;

        disconnecting = true;
        disconnectBtn.disabled = true;
        disconnectBtn.setAttribute("aria-busy", "true");
        disconnectBtn.textContent = "Disconnecting...";
        setFeedback("");

        function fail(text) {
            setFeedback(text);
            showSettingsToast(text, true);
        }

        try {
            let token;
            try {
                token = await fetchCsrfToken();
            } catch (err) {
                fail("Could not verify your session. Please refresh the page and try again.");
                return;
            }

            let response;
            try {
                response = await fetch("/api/me/discord/disconnect", {
                    method: "POST",
                    credentials: "same-origin",
                    headers: { "X-CSRFToken": token, "Accept": "application/json" }
                });
            } catch (err) {
                fail("We couldn't reach Learn2Play to disconnect Discord. Your account is still connected.");
                return;
            }

            if (response.status === 401) { goToLogin(); return; }

            let data = null;
            try { data = await response.json(); } catch (err) { data = null; }

            if (response.ok && data && data.ok === true) {
                statusSeq++; guildSeq++;          // discard any in-flight status/guild responses
                lastLoadOk = true;
                lastLoadedAt = Date.now();
                renderDisconnected();
                showSettingsToast("Discord account disconnected.");
                setFeedback("Your Discord account has been disconnected.");
                connectLink.focus();              // the button that had focus is now hidden
                return;
            }

            if (response.status === 400) {
                fail("Your session has expired. Please refresh the page and try again. Your account is still connected.");
            } else if (response.status === 403) {
                fail("This action isn't available for your account right now. Your account is still connected.");
            } else {
                fail("We couldn't disconnect Discord right now. Your account is still connected. Please try again.");
            }
        } finally {
            disconnecting = false;
            disconnectBtn.disabled = false;
            disconnectBtn.removeAttribute("aria-busy");
            disconnectBtn.textContent = DISCONNECT_LABEL;
        }
    }

    function maybeLoadOnOpen() {
        const stale = !lastLoadOk || (Date.now() - lastLoadedAt) > RELOAD_AFTER_MS;
        if (stale) loadStatus();
    }

    disconnectBtn.addEventListener("click", disconnect);
    retryBtn.addEventListener("click", () => loadStatus({ force: true }));
    securityTabBtn.addEventListener("click", maybeLoadOnOpen);

    // Security tab already active on page load (e.g. settings#security, or returning from Discord OAuth2)
    if (document.getElementById("tab-security")?.classList.contains("active")) {
        loadStatus();
    }
})();
