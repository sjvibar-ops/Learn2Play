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
    const dayMs = 24 * 60 * 60 * 1000;
    const phtOffsetMs = 8 * 60 * 60 * 1000;
    const confirmedStatuses = ["accepted", "scheduled"];
    const activeStatuses = ["pending", ...confirmedStatuses];

    function sessionDateRanges(now) {
        // Shift the clock for Philippine calendar arithmetic, then return UTC boundaries.
        const pht = new Date(now + phtOffsetMs);
        const today = Date.UTC(pht.getUTCFullYear(), pht.getUTCMonth(), pht.getUTCDate()) - phtOffsetMs;
        const weekStart = today - ((pht.getUTCDay() + 6) % 7) * dayMs;
        return { today, tomorrow: today + dayMs, weekStart, weekEnd: weekStart + 7 * dayMs };
    }

    const inRange = (start, from, until) => start >= from && start < until;

    function isUpcomingOrInProgress(status, start, duration, now) {
        if (confirmedStatuses.includes(status)) {
            return now < start + (Number(duration) || 60) * 60 * 1000;
        }
        return status === "pending" && start >= now;
    }

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

    function filterSessions(status = activeFilter, now = Date.now()) {
        activeFilter = status;
        const cards = sessionList ? [...sessionList.querySelectorAll(".scheduled-session")] : [];
        const query = (searchInput?.value || "").trim().toLocaleLowerCase();
        const { today, tomorrow, weekStart, weekEnd } = sessionDateRanges(now);
        let visibleCount = 0;

        cards.forEach(card => {
            const state = card.dataset.bookingStatus;
            const start = Date.parse(card.dataset.startTime);
            const isPast = state === "completed" || state === "rejected"
                || (activeStatuses.includes(state) && now >= start + (Number(card.dataset.duration) || 60) * 60 * 1000);
            const isCancelled = state === "cancelled";
            const isActive = activeStatuses.includes(state);
            const matchesFilter = status === "upcoming"
                ? isUpcomingOrInProgress(state, start, card.dataset.duration, now)
                : status === "today" ? isActive && inRange(start, today, tomorrow)
                : status === "this-week" ? isActive && inRange(start, weekStart, weekEnd)
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
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && !dropdown.classList.contains("hidden")) {
                dropdown.classList.add("hidden");
                avatar.setAttribute("aria-expanded", "false");
                avatar.focus();
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
            const now = Date.now();
            if (sessionList) {
                const mine = bookings.filter(b => b.student_id === window.L2UserId);
                const meaningful = mine.filter(b => ["pending", "accepted", "scheduled", "completed", "cancelled", "rejected"].includes(b.status));
                sessionList.innerHTML = meaningful.map(b => {
                    const statusClass = b.status === "accepted" ? "status-accepted" : b.status === "scheduled" ? "status-scheduled" : b.status === "pending" ? "status-pending" : b.status === "completed" ? "status-completed" : "status-cancelled";
                    const ended = Date.parse(b.start_time) + Number(b.duration || 60) * 60000 <= now;
                    const bookingEnded = ["cancelled", "rejected"].includes(b.status);
                    const joinBtn = b.payment_satisfied !== false && ["accepted", "scheduled"].includes(b.status) ? `<button class="session-action join-session" data-id="${b.id}">Join session</button>` : "";
                    const cancelBtn = ["pending", "accepted", "scheduled"].includes(b.status) ? `<button class="session-action cancel-booking" data-id="${b.id}">Cancel booking</button>` : "";
                    const confirmBtn = b.payment_satisfied !== false && ended && ["accepted", "scheduled"].includes(b.status) && !b.student_confirmed && !b.attendance_disputed && !b.attendance_closed
                        ? `<button class="session-action booking-action" data-id="${b.id}" data-action="confirm-attendance">Confirm attendance</button>` : "";
                    const disputeBtn = ended && ["accepted", "scheduled"].includes(b.status) && !b.attendance_disputed && !b.attendance_closed
                        ? `<button class="session-action booking-action" data-id="${b.id}" data-action="dispute-attendance">Report disagreement</button>` : "";
                    const confirmNote = bookingEnded ? "Booking ended. Attendance confirmation and XP are unavailable." : b.attendance_closed ? "Attendance closed by an administrator without XP." : b.attendance_disputed ? "Attendance disputed. XP is on hold; please contact support."
                        : b.status === "completed"
                        ? (b.xp_awarded ? "Attendance confirmed by both. 50 XP awarded." : "Completed before two-sided confirmation; no XP was awarded.")
                        : (b.student_confirmed ? "You confirmed attendance; waiting for your educator." : b.provider_confirmed ? "Your educator confirmed; please confirm attendance." : ended ? "Confirm attendance after your session." : "Both you and your educator confirm after the session.");
                    const paymentNote = bookingEnded
                        ? (b.reconciliation_state ? `Booking-end reconciliation: ${b.reconciliation_state}. No refund is implied by cancellation; see your payment history for the recorded outcome.` : "Booking ended. No further session access is available.")
                        : b.payment_satisfied === false ? "Payment pending verification; attendance confirmation and XP are blocked." : b.payment_status === "VERIFIED" ? "Payment verified." : "No payment requirement for this booking.";
                    const price = b.price === null || b.price === undefined ? "" : `<strong class="session-price">₱${Number(b.price).toLocaleString("en-PH", {minimumFractionDigits: 2, maximumFractionDigits: 2})}<small> session rate</small></strong>`;
                    return `<article class="scheduled-session student-session-card" data-booking-status="${escapeHtml(b.status)}" data-start-time="${escapeHtml(b.start_time)}" data-duration="${escapeHtml(b.duration)}">
                        <div class="student-session-top"><span class="session-status ${statusClass}">${escapeHtml(b.status)}</span><span class="session-datetime">${escapeHtml(b.formatted)}</span>${price}</div>
                        <div class="student-educator-row"><div class="student-educator-avatar" aria-hidden="true">${escapeHtml(String(b.mentor || "E").trim().slice(0, 1).toUpperCase())}</div><div class="student-educator-copy"><strong>${escapeHtml(b.mentor)}</strong><span>${escapeHtml(b.topic || "Learning session")}</span></div></div>
                        <div class="student-session-focus"><span>SESSION TOPIC</span><p>${escapeHtml(b.notes || b.topic || "No session notes were added.")}</p></div>
                        <p class="attendance-confirmation-note">${escapeHtml(confirmNote)}</p><p class="attendance-confirmation-note">${escapeHtml(paymentNote)}</p><div class="student-session-actions">${b.payment_url ? `<a class="session-action" href="${escapeHtml(b.payment_url)}">Payment: ${escapeHtml(b.payment_status)}</a>` : ""}${joinBtn}${confirmBtn}${disputeBtn}${cancelBtn}</div>
                    </article>`;
                }).join("");
                filterSessions(document.querySelector(".session-tab.active")?.dataset.status || "upcoming", now);
                const { today, tomorrow, weekStart, weekEnd } = sessionDateRanges(now);
                const activeStarts = meaningful.filter(b => activeStatuses.includes(b.status)).map(b => Date.parse(b.start_time));
                const upcoming = meaningful.filter(b =>
                    isUpcomingOrInProgress(b.status, Date.parse(b.start_time), b.duration, now)).length;
                const todayCount = activeStarts.filter(start => inRange(start, today, tomorrow)).length;
                const weekCount = activeStarts.filter(start => inRange(start, weekStart, weekEnd)).length;
                document.getElementById("upcomingCount")?.replaceChildren(String(upcoming));
                document.getElementById("thisWeekCount")?.replaceChildren(String(weekCount));
                document.getElementById("completedCount")?.replaceChildren(String(meaningful.filter(b => b.status === "completed").length));
                document.querySelector('[data-filter-count="upcoming"]')?.replaceChildren(String(upcoming));
                document.querySelector('[data-filter-count="today"]')?.replaceChildren(String(todayCount));
                document.querySelector('[data-filter-count="this-week"]')?.replaceChildren(String(weekCount));
            }
            const requests = bookings.filter(b => b.provider_id === window.L2UserId && b.status === "pending");
            const upcoming = bookings.filter(b => b.provider_id === window.L2UserId
                && confirmedStatuses.includes(b.status)
                && isUpcomingOrInProgress(b.status, Date.parse(b.start_time), b.duration, now));
            const awaitingProviderConfirmation = bookings.filter(b => b.provider_id === window.L2UserId
                && ["accepted", "scheduled"].includes(b.status)
                && Date.parse(b.start_time) + Number(b.duration || 60) * 60000 <= now);
            const requestList = document.getElementById("mentorRequestList");
            const upcomingList = document.getElementById("mentorUpcomingList");
            if (requestList) requestList.innerHTML = requests.length ? requests.map(b => `<article class="mentor-request-card"><div class="mentor-request-details"><h3>${escapeHtml(b.student)} · ${escapeHtml(b.topic)}</h3><p>${escapeHtml(b.formatted)}</p><span>${escapeHtml(b.notes || "No note provided")}</span></div><div class="mentor-request-actions"><button class="session-action booking-action" data-id="${b.id}" data-action="accept">Accept</button><button class="session-action cancel-session-action booking-action" data-id="${b.id}" data-action="reject">Decline</button></div></article>`).join("") : '<p class="sessions-empty">No pending booking requests.</p>';
            const providerSessions = [...new Map([...upcoming, ...awaitingProviderConfirmation].map(b => [b.id, b])).values()];
            if (upcomingList) upcomingList.innerHTML = providerSessions.length ? providerSessions.map(b => {
                const ended = Date.parse(b.start_time) + Number(b.duration || 60) * 60000 <= now;
                const confirm = b.attendance_closed ? `<span class="attendance-confirmation-note">Attendance closed without XP.</span>` : b.attendance_disputed
                    ? `<span class="attendance-confirmation-note">Attendance disputed; XP on hold. Contact support.</span>`
                    : b.payment_satisfied === false ? `<span class="attendance-confirmation-note">Payment pending verification; confirmation and XP are blocked.</span>`
                    : ended && !b.provider_confirmed
                    ? `<button class="session-action booking-action" data-id="${b.id}" data-action="complete">Confirm session</button>`
                    : ended ? `<span class="attendance-confirmation-note">You confirmed - ${b.student_confirmed ? "both confirmed; 50 XP awarded" : "waiting for learner"}</span>`
                    : `<button class="session-action join-session" data-id="${b.id}">Join Session</button>`;
                const dispute = ended && !b.attendance_disputed && !b.attendance_closed ? `<button class="session-action booking-action" data-id="${b.id}" data-action="dispute-attendance">Report disagreement</button>` : "";
                return `<article class="mentor-upcoming-item"><div><strong>${escapeHtml(b.student)} · ${escapeHtml(b.topic)}</strong><span>${escapeHtml(b.formatted)}</span></div>${confirm}${dispute}</article>`;
            }).join("") : '<p class="sessions-empty">No confirmed sessions scheduled.</p>';
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
            let body;
            const headers = {"X-CSRFToken":token};
            if (kind === "dispute-attendance") {
                const reason = window.prompt("Explain the attendance disagreement (5–1,000 characters). Your reason will be recorded for admin review.");
                if (reason === null) return;
                if (reason.trim().length < 5 || reason.trim().length > 1000) throw new Error("Enter a reason of 5–1,000 characters.");
                headers["Content-Type"] = "application/json";
                body = JSON.stringify({note:reason.trim()});
            }
            const result = await fetch(`/api/bookings/${action.dataset.id}/${kind}`, {method:"POST", headers, body}).then(r => r.json());
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
