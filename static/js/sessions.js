"use strict";

(function setupSessionManager() {
    const studentView = document.getElementById("studentSessionsView");
    const mentorView = document.getElementById("mentorSessionsView");
    const sessionList = document.getElementById("sessionList");
    const emptyBox = document.getElementById("sessionsEmpty");
    const tabs = document.querySelectorAll(".session-tab");

    // 1. Role-based view switching (Student vs Mentor/Coach workspace)
    function setRoleView(mode) {
        const isMentor = mode === "mentor" || mode === "coach";
        if (studentView) studentView.classList.toggle("hidden", isMentor);
        if (mentorView) mentorView.classList.toggle("hidden", !isMentor);
    }

    const initialMode = window.L2?.getMode?.() || "student";
    setRoleView(initialMode);

    fetch("/api/me", { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(res => res.ok ? res.json() : null)
        .then(data => {
            if (!data?.user) return;
            const roles = data.user.roles || [];
            const mode = roles.includes(initialMode)
                ? initialMode
                : (roles.includes("mentor") ? "mentor" : (roles.includes("coach") ? "coach" : "student"));
            setRoleView(mode);
        })
        .catch(() => setRoleView(initialMode));

    // 2. Filter and search learner bookings.
    let activeFilter = "upcoming";
    const searchInput = document.getElementById("sessionSearch");
    const startOfDay = value => { const d = new Date(value); return new Date(d.getFullYear(), d.getMonth(), d.getDate()); };
    const weekStart = value => { const d = startOfDay(value); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return d; };

    tabs.forEach(tab => {
        tab.addEventListener("click", () => {
            tabs.forEach(item => {
                const selected = item === tab;
                item.classList.toggle("active", selected);
                item.setAttribute("aria-selected", String(selected));
            });
            filterSessions(tab.dataset.status);
        });
    });

    searchInput?.addEventListener("input", () => filterSessions(activeFilter));

    function filterSessions(status = activeFilter) {
        activeFilter = status;
        const cards = sessionList ? [...sessionList.querySelectorAll(".scheduled-session")] : [];
        const query = (searchInput?.value || "").trim().toLocaleLowerCase();
        const now = new Date();
        const today = startOfDay(now);
        const nextWeek = new Date(weekStart(now)); nextWeek.setDate(nextWeek.getDate() + 7);
        let visibleCount = 0;

        cards.forEach(card => {
            const state = card.dataset.bookingStatus;
            const start = new Date(card.dataset.startTime);
            const isPast = state === "completed" || state === "rejected";
            const isCancelled = state === "cancelled";
            const isActive = !isPast && !isCancelled;
            const matchesFilter = status === "upcoming" ? isActive
                : status === "today" ? isActive && start >= today && start < new Date(today.getTime() + 86400000)
                : status === "this-week" ? isActive && start >= today && start < nextWeek
                : status === "past" ? isPast
                : status === "cancelled" ? isCancelled : true;
            const matchesSearch = !query || card.textContent.toLocaleLowerCase().includes(query);
            const visible = matchesFilter && matchesSearch;
            card.hidden = !visible;
            if (visible) visibleCount++;
        });

        const noBookings = cards.length === 0;
        if (emptyBox) {
            emptyBox.hidden = visibleCount > 0;
            emptyBox.classList.toggle("sessions-filter-empty", !noBookings);
            const title = emptyBox.querySelector("h3");
            const message = emptyBox.querySelector("p");
            const browseLink = emptyBox.querySelector("a");
            if (title) title.textContent = noBookings ? "No sessions scheduled yet" : "No sessions match this view";
            if (message) message.textContent = noBookings
                ? "Your bookings will appear here. Browse educators to schedule your first lesson."
                : "Try another filter or search term to find a session.";
            if (browseLink) browseLink.hidden = !noBookings;
        }
    }

    // 3. Dropdown Menu & Logout
    const menu = document.getElementById("profileMenu");
    const avatar = document.getElementById("userAvatar");
    const dropdown = document.getElementById("profileDropdown");
    const logoutBtn = document.getElementById("logoutBtn");

    if (avatar && dropdown) {
        avatar.addEventListener("click", (e) => {
            e.stopPropagation();
            const isOpen = !dropdown.classList.contains("hidden");
            dropdown.classList.toggle("hidden", isOpen);
            avatar.setAttribute("aria-expanded", String(!isOpen));
        });

        document.addEventListener("click", (e) => {
            if (menu && !menu.contains(e.target)) {
                dropdown.classList.add("hidden");
                avatar.setAttribute("aria-expanded", "false");
            }
        });
    }

    if (logoutBtn) {
        logoutBtn.addEventListener("click", async (e) => {
            e.preventDefault();
            try {
                const meta = document.querySelector('meta[name="csrf-token"]');
                let csrfToken = meta && meta.content ? meta.content : null;

                if (!csrfToken) {
                    const tokenResp = await fetch("/api/csrf-token", { credentials: "same-origin" });
                    const tokenData = await tokenResp.json();
                    csrfToken = tokenData.csrf_token || "";
                }

                const logoutResponse = await fetch("/api/auth/logout", {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {
                        "X-CSRFToken": csrfToken,
                        Accept: "application/json"
                    }
                });
                if (!logoutResponse.ok) {
                    const data = await logoutResponse.json().catch(() => ({}));
                    throw new Error(data.message || "Logout request was rejected.");
                }
                window.location.href = "/auth";
            } catch (err) {
                window.alert(err.message || "Logout could not be completed. Please try again.");
            }
        });
    }

    const escapeHtml = value => String(value || "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
    async function loadBookings() {
        try {
            const data = await fetch("/api/bookings", {credentials:"same-origin"}).then(r => r.json());
            if (!data.ok) throw new Error(data.message || "Could not load bookings.");
            const bookings = data.bookings || [];
            if (sessionList) {
                const mine = bookings.filter(b => b.student_id === window.L2UserId);
                const meaningful = mine.filter(b => ["pending", "accepted", "scheduled", "completed", "cancelled", "rejected"].includes(b.status));
                sessionList.innerHTML = meaningful.map(b => {
                    const statusClass = b.status === "accepted" ? "status-accepted" : b.status === "scheduled" ? "status-scheduled" : b.status === "pending" ? "status-pending" : b.status === "completed" ? "status-completed" : "status-cancelled";
                    const joinBtn = (b.status === "accepted" || b.status === "scheduled") ? `<button class="session-action join-session" data-id="${b.id}">Join session</button>` : "";
                    const cancelBtn = ["pending", "accepted", "scheduled"].includes(b.status) ? `<button class="session-action cancel-booking" data-id="${b.id}">Cancel booking</button>` : "";
                    const price = b.price === null || b.price === undefined ? "" : `<strong class="session-price">₱${Number(b.price).toLocaleString("en-PH", {minimumFractionDigits: 2, maximumFractionDigits: 2})}<small> session rate</small></strong>`;
                    return `<article class="scheduled-session student-session-card" data-booking-status="${escapeHtml(b.status)}" data-start-time="${escapeHtml(b.start_time)}">
                        <div class="student-session-top"><span class="session-status ${statusClass}">${escapeHtml(b.status)}</span><span class="session-datetime">${escapeHtml(b.formatted)}</span>${price}</div>
                        <div class="student-educator-row"><div class="student-educator-avatar" aria-hidden="true">${escapeHtml(String(b.mentor || "E").trim().slice(0, 1).toUpperCase())}</div><div class="student-educator-copy"><strong>${escapeHtml(b.mentor)}</strong><span>${escapeHtml(b.topic || "Learning session")}</span></div></div>
                        <div class="student-session-focus"><span>SESSION TOPIC</span><p>${escapeHtml(b.notes || b.topic || "No session notes were added.")}</p></div>
                        <div class="student-session-actions">${joinBtn}${cancelBtn}</div>
                    </article>`;
                }).join("");
                filterSessions(document.querySelector(".session-tab.active")?.dataset.status || "upcoming");
                const today = startOfDay(new Date());
                const weekStartDate = weekStart(today); const weekEnd = new Date(weekStartDate); weekEnd.setDate(weekEnd.getDate() + 7);
                const upcoming = meaningful.filter(b => ["pending", "accepted", "scheduled"].includes(b.status));
                const todayCount = upcoming.filter(b => { const d = new Date(b.start_time); return d >= today && d < new Date(today.getTime() + 86400000); }).length;
                const weekCount = upcoming.filter(b => { const d = new Date(b.start_time); return d >= today && d < weekEnd; }).length;
                document.getElementById("upcomingCount")?.replaceChildren(String(upcoming.length));
                document.getElementById("thisWeekCount")?.replaceChildren(String(weekCount));
                document.getElementById("completedCount")?.replaceChildren(String(meaningful.filter(b => b.status === "completed").length));
                document.querySelector('[data-filter-count="upcoming"]')?.replaceChildren(String(upcoming.length));
                document.querySelector('[data-filter-count="today"]')?.replaceChildren(String(todayCount));
                document.querySelector('[data-filter-count="this-week"]')?.replaceChildren(String(weekCount));
            }
            const requests = bookings.filter(b => b.provider_id === window.L2UserId && b.status === "pending");
            const upcoming = bookings.filter(b => b.provider_id === window.L2UserId && ["accepted","scheduled"].includes(b.status));
            const requestList = document.getElementById("mentorRequestList");
            const upcomingList = document.getElementById("mentorUpcomingList");
            if (requestList) requestList.innerHTML = requests.length ? requests.map(b => `<article class="mentor-request-card"><div class="mentor-request-details"><h3>${escapeHtml(b.student)} · ${escapeHtml(b.topic)}</h3><p>${escapeHtml(b.formatted)}</p><span>${escapeHtml(b.notes || "No note provided")}</span></div><div class="mentor-request-actions"><button class="session-action booking-action" data-id="${b.id}" data-action="accept">Accept</button><button class="session-action cancel-session-action booking-action" data-id="${b.id}" data-action="reject">Decline</button></div></article>`).join("") : '<p class="sessions-empty">No pending booking requests.</p>';
            if (upcomingList) upcomingList.innerHTML = upcoming.length ? upcoming.map(b => `<article class="mentor-upcoming-item"><div><strong>${escapeHtml(b.student)} · ${escapeHtml(b.topic)}</strong><span>${escapeHtml(b.formatted)}</span></div><button class="session-action join-session" data-id="${b.id}">Join Session</button></article>`).join("") : '<p class="sessions-empty">No confirmed sessions scheduled.</p>';
            document.getElementById("mentorPendingCount")?.replaceChildren(String(requests.length));
            document.getElementById("mentorUpcomingCount")?.replaceChildren(String(upcoming.length));
            document.getElementById("mentorRequestCount")?.replaceChildren(`${requests.length} new`);
        } catch (error) { console.error("Could not load bookings", error); }
    }
    document.addEventListener("click", async event => {
        const action = event.target.closest(".booking-action, .cancel-booking, .join-session");
        if (!action) return;
        action.disabled = true;
        action.setAttribute("aria-busy", "true");
        try {
            if (action.classList.contains("join-session")) {
                const data = await fetch(`/api/sessions/${action.dataset.id}/join`).then(r => r.json());
                if (!data.ok) throw new Error(data.message);
                window.location.assign(data.url); return;
            }
            const meta = document.querySelector('meta[name="csrf-token"]');
            const token = meta && meta.content ? meta.content : await fetch("/api/csrf-token").then(r => r.json()).then(d => d.csrf_token);
            const kind = action.classList.contains("cancel-booking") ? "cancel" : action.dataset.action;
            const result = await fetch(`/api/bookings/${action.dataset.id}/${kind}`, {method:"POST", headers:{"X-CSRFToken":token}}).then(r => r.json());
            if (!result.ok) throw new Error(result.message || "Could not update booking.");
            loadBookings();
        } catch (error) { window.alert(error.message || "The requested action could not be completed."); }
        finally {
            action.disabled = false;
            action.removeAttribute("aria-busy");
        }
    });
    loadBookings();
    window.setInterval(loadBookings, 10000);

    // Availability button
    document.querySelectorAll(".mentor-availability-btn").forEach(btn => {
        btn.addEventListener("click", () => { window.location.href = "/schedule#availabilityForm"; });
    });
})();
