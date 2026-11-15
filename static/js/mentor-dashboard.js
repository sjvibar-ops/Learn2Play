"use strict";
(() => {
  const root = document.getElementById("mentorHome");
  if (!root || document.body.dataset.dashboardView === "browse") return;
  const userId = Number(window.L2UserId);
  const feedback = document.getElementById("mentorDashFeedback");
  const requestList = document.getElementById("mentorDashRequestList");
  const upcomingHost = document.getElementById("mentorDashNextSession");
  const availabilityHost = document.getElementById("mentorDashAvailability");
  const weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const escape = value => String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));

  function requestCard(b) {
    const price = b.price == null ? "Rate not listed" : `₱${Number(b.price).toLocaleString("en-PH", {minimumFractionDigits:2, maximumFractionDigits:2})}`;
    const kind = b.role === "coach" ? "Gaming coaching" : "Academic teaching";
    const color = b.role === "coach" ? "gaming" : "academic";
    return `<article class="mentor-home-request"><div class="mentor-home-request-top"><span class="mentor-request-kind ${color}">${kind}</span><span class="mentor-request-status">Pending</span></div><div class="mentor-home-learner"><span class="mentor-home-initial">${escape((b.student || "L").trim().slice(0,1).toUpperCase())}</span><div><strong>${escape(b.student || "Learner")}</strong><small>${escape(b.topic || "Session topic not specified")}</small></div></div><div class="mentor-home-request-meta"><span>${escape(b.formatted || "Time not available")}</span><strong>${price}</strong></div>${b.notes ? `<p class="mentor-home-request-note">${escape(b.notes)}</p>` : ""}<div class="mentor-home-request-actions"><button type="button" data-booking-action="accept" data-id="${Number(b.id)}">Accept request</button><button type="button" class="decline" data-booking-action="reject" data-id="${Number(b.id)}">Decline</button></div></article>`;
  }

  async function loadBookings() {
    try {
      const response = await fetch("/api/bookings", {credentials:"same-origin", headers:{Accept:"application/json"}});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error();
      const mine = (data.bookings || []).filter(b => b.provider_id === userId);
      const pending = mine.filter(b => b.status === "pending");
      const now = Date.now();
      const upcoming = mine.filter(b => ["accepted", "scheduled"].includes(b.status) && Date.parse(b.start_time) > now);
      document.getElementById("mentorDashPendingCount").textContent = pending.length;
      document.getElementById("mentorDashUpcomingCount").textContent = upcoming.length;
      document.getElementById("mentorDashCompletedCount").textContent = mine.filter(b => b.status === "completed").length;
      requestList.innerHTML = pending.length ? pending.map(requestCard).join("") : '<div class="mentor-home-empty"><span aria-hidden="true">✓</span><strong>No pending requests</strong><small>New learner requests will appear here.</small></div>';
      // The booking API orders sessions by start time.
      const next = upcoming[0];
      upcomingHost.innerHTML = next ? `<article class="mentor-home-next"><span class="mentor-home-next-date">${escape(next.formatted || "Scheduled")}</span><h3>${escape(next.student || "Learner")}</h3><p>${escape(next.topic || "Session")}</p><div><span>${escape(next.role === "coach" ? "Gaming coaching" : "Academic teaching")}</span>${next.price == null ? "" : `<strong>₱${Number(next.price).toLocaleString("en-PH", {minimumFractionDigits:2, maximumFractionDigits:2})}</strong>`}</div></article>` : '<div class="mentor-home-empty compact"><span aria-hidden="true">◷</span><strong>No upcoming sessions</strong><small>Your next confirmed session will appear here.</small></div>';
    } catch (_) {
      requestList.innerHTML = '<p class="mentor-home-error">Booking details could not be loaded.</p>';
      upcomingHost.innerHTML = '<p class="mentor-home-error">Schedule details could not be loaded.</p>';
    }
  }

  async function loadAvailability() {
    try {
      const response = await fetch("/api/availability", {credentials:"same-origin", headers:{Accept:"application/json"}});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error();
      const rows = data.availability || [];
      availabilityHost.innerHTML = rows.length ? rows.slice(0,4).map(r => `<div class="mentor-home-hours"><span>${weekdays[r.weekday] || "Day"}</span><strong>${escape(r.start_time)}–${escape(r.end_time)}</strong></div>`).join("") : '<p class="mentor-home-empty-inline">No weekly hours added yet.</p>';
    } catch (_) { availabilityHost.innerHTML = '<p class="mentor-home-error">Availability could not be loaded.</p>'; }
  }

  async function updateRequest(button) {
    button.disabled = true;
    try {
      const meta = document.querySelector('meta[name="csrf-token"]');
      let token = meta?.content;
      if (!token) token = (await fetch("/api/csrf-token", {credentials:"same-origin"}).then(r => r.json())).csrf_token;
      const response = await fetch(`/api/bookings/${Number(button.dataset.id)}/${button.dataset.bookingAction}`, {method:"POST", credentials:"same-origin", headers:{"X-CSRFToken":token, Accept:"application/json"}});
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.message || "Could not update this request.");
      feedback.textContent = button.dataset.bookingAction === "accept" ? "Request accepted." : "Request declined.";
      await loadBookings();
    } catch (error) { feedback.textContent = error.message || "Could not update this request."; button.disabled = false; }
  }

  requestList.addEventListener("click", event => {
    const button = event.target.closest("[data-booking-action]");
    if (button) updateRequest(button);
  });
  loadBookings();
  loadAvailability();
  window.setInterval(loadBookings, 15000);
})();
