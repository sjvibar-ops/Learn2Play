"use strict";
(() => {
  const page = document.getElementById("bookingPage");
  if (!page) return;

  const tutorId = Number(page.dataset.tutorId);
  const dateGrid = document.getElementById("dateGrid");
  const slotGrid = document.getElementById("slotGrid");
  const label = document.getElementById("calendarLabel");
  const topicInput = document.getElementById("bookingTopic");
  const goalInput = document.getElementById("learningGoal");
  const linkInput = document.getElementById("resourceLink");
  const feedback = document.getElementById("bookingFeedback");
  const submitButton = document.getElementById("submitBooking");
  const selected = { date: "", time: "" };
  let selectedRole = page.dataset.selectedRole || "mentor";

  const phtToday = () => {
    const parts = new Intl.DateTimeFormat("en-CA", {timeZone:"Asia/Manila", year:"numeric", month:"2-digit", day:"2-digit"}).formatToParts(new Date());
    const value = Object.fromEntries(parts.map(part => [part.type, part.value]));
    return new Date(Date.UTC(Number(value.year), Number(value.month) - 1, Number(value.day)));
  };
  const dateKey = date => date.toISOString().slice(0, 10);
  const dateFromKey = value => new Date(`${value}T00:00:00Z`);
  const today = phtToday();
  const lastDay = new Date(today); lastDay.setUTCDate(lastDay.getUTCDate() + 60);
  const firstMonday = new Date(today); firstMonday.setUTCDate(firstMonday.getUTCDate() - ((firstMonday.getUTCDay() + 6) % 7));
  let weekStart = new Date(firstMonday);
  let cachedSlots = [];

  function dateLabel(value, options = {weekday:"short", month:"short", day:"numeric"}) {
    return new Intl.DateTimeFormat("en", {...options, timeZone:"UTC"}).format(dateFromKey(value));
  }
  function timeLabel(value) {
    const [hour, minute] = value.split(":").map(Number);
    return new Intl.DateTimeFormat("en", {hour:"numeric", minute:"2-digit", timeZone:"UTC"}).format(new Date(Date.UTC(2020, 0, 1, hour, minute)));
  }

  function renderWeek() {
    dateGrid.replaceChildren();
    const end = new Date(weekStart); end.setUTCDate(end.getUTCDate() + 6);
    label.textContent = weekStart.getUTCMonth() === end.getUTCMonth()
      ? `${dateLabel(dateKey(weekStart), {month:"long"})} ${weekStart.getUTCFullYear()}`
      : `${dateLabel(dateKey(weekStart), {month:"short"})} – ${dateLabel(dateKey(end), {month:"short", year:"numeric"})}`;
    document.getElementById("prevWeek").disabled = weekStart <= firstMonday;
    document.getElementById("nextWeek").disabled = dateKey(weekStart) >= dateKey(lastDay);

    for (let offset = 0; offset < 7; offset++) {
      const day = new Date(weekStart); day.setUTCDate(day.getUTCDate() + offset);
      const key = dateKey(day);
      const button = document.createElement("button");
      button.type = "button";
      button.className = `date-option${key === selected.date ? " selected" : ""}${key < dateKey(today) || key > dateKey(lastDay) ? " unavailable" : ""}`;
      button.disabled = key < dateKey(today) || key > dateKey(lastDay);
      button.dataset.date = key;
      button.innerHTML = `<span>${dateLabel(key, {weekday:"short"})}</span><strong>${day.getUTCDate()}</strong><small>${key === dateKey(today) ? "Today" : "Check times"}</small>`;
      button.addEventListener("click", () => loadSlots(key));
      dateGrid.append(button);
    }
  }

  async function loadSlots(day) {
    selected.date = day; selected.time = ""; cachedSlots = [];
    renderWeek();
    document.getElementById("slotsTitle").textContent = `Available times · ${dateLabel(day, {weekday:"long", month:"short", day:"numeric"})}`;
    document.getElementById("summaryDate").textContent = `${dateLabel(day, {weekday:"short", month:"short", day:"numeric"})} · Choose a time`;
    slotGrid.innerHTML = '<p class="empty-copy">Loading available times…</p>';
    feedback.textContent = "";
    try {
      const response = await fetch(`/api/tutors/${tutorId}/availability?date=${encodeURIComponent(day)}`, {credentials:"same-origin"});
      const data = await response.json();
      if (selected.date !== day) return;
      if (!response.ok || !data.ok) throw new Error(data.message || "Could not load available times.");
      cachedSlots = data.slots || [];
      if (!cachedSlots.length) {
        slotGrid.innerHTML = '<p class="empty-copy slot-empty">No available times on this date. Try another day.</p>';
      } else {
        slotGrid.replaceChildren();
        cachedSlots.forEach(time => {
          const button = document.createElement("button");
          button.type = "button"; button.className = "time-option"; button.dataset.time = time;
          button.textContent = timeLabel(time);
          button.addEventListener("click", () => {
            selected.time = time;
            slotGrid.querySelectorAll(".time-option").forEach(item => item.classList.toggle("selected", item === button));
            document.getElementById("summaryDate").textContent = `${dateLabel(day, {weekday:"short", month:"short", day:"numeric"})} · ${timeLabel(time)} PHT`;
          });
          slotGrid.append(button);
        });
      }
    } catch (error) {
      if (selected.date !== day) return;
      slotGrid.innerHTML = '<p class="empty-copy slot-empty">Could not load availability. Please try again.</p>';
      feedback.textContent = error.message;
    }
  }

  document.getElementById("prevWeek").addEventListener("click", () => {
    weekStart.setUTCDate(weekStart.getUTCDate() - 7); renderWeek();
  });
  document.getElementById("nextWeek").addEventListener("click", () => {
    weekStart.setUTCDate(weekStart.getUTCDate() + 7); renderWeek();
  });
  document.querySelectorAll(".topic-chip").forEach(button => button.addEventListener("click", () => {
    topicInput.value = button.dataset.topic;
    document.querySelectorAll(".topic-chip").forEach(chip => chip.classList.toggle("selected", chip === button));
    document.getElementById("summaryTopic").textContent = button.dataset.topic;
  }));
  topicInput.addEventListener("input", () => {
    document.getElementById("summaryTopic").textContent = topicInput.value.trim() || "Choose a topic";
    document.querySelectorAll(".topic-chip").forEach(chip => chip.classList.toggle("selected", chip.dataset.topic === topicInput.value.trim()));
  });
  document.querySelectorAll(".track-option").forEach(button => button.addEventListener("click", () => {
    selectedRole = button.dataset.role;
    document.querySelectorAll(".track-option").forEach(option => option.classList.toggle("selected", option === button));
    const rawRate = page.dataset[`${selectedRole}Rate`];
    const rate = rawRate ? `₱${Number(rawRate).toLocaleString("en-PH", {minimumFractionDigits:2, maximumFractionDigits:2})}` : "Rate not set";
    document.getElementById("sessionRate").textContent = rate;
    document.getElementById("summaryRate").textContent = rate;
  }));
  goalInput.addEventListener("input", () => document.getElementById("goalCount").textContent = `${goalInput.value.length} / 850`);

  submitButton.addEventListener("click", async () => {
    const topic = topicInput.value.trim();
    const goal = goalInput.value.trim();
    const resource = linkInput.value.trim();
    if (!topic) { feedback.textContent = "Add a subject or session topic first."; topicInput.focus(); return; }
    if (!selected.date || !selected.time) { feedback.textContent = "Choose an available date and time first."; return; }
    if (resource) {
      try { const url = new URL(resource); if (!["http:", "https:"].includes(url.protocol)) throw new Error(); }
      catch (_) { feedback.textContent = "Enter a valid link beginning with https:// or http://."; linkInput.focus(); return; }
    }
    const message = [goal, resource ? `Study material link: ${resource}` : ""].filter(Boolean).join("\n\n");
    if (message.length > 1000) { feedback.textContent = "Your goals and link are too long. Shorten them and try again."; return; }
    submitButton.disabled = true; feedback.textContent = "Sending your request…";
    try {
      const token = document.querySelector('meta[name="csrf-token"]')?.content;
      const response = await fetch("/api/bookings", {
        method:"POST", credentials:"same-origin",
        headers:{"Content-Type":"application/json", "X-CSRFToken":token || ""},
        body:JSON.stringify({mentor_id:tutorId, role:selectedRole, date:selected.date, time:selected.time, topic, message})
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.message || "Could not send the booking request.");
      feedback.textContent = "Request sent. Redirecting to My Sessions…";
      window.setTimeout(() => { window.location.assign(data.payment_url || "/sessions"); }, 700);
    } catch (error) {
      feedback.textContent = error.message;
      submitButton.disabled = false;
    }
  });

  renderWeek();
  loadSlots(dateKey(today));
})();
