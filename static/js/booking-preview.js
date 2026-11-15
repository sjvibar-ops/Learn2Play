"use strict";
(() => {
  const grid = document.getElementById("dateGrid");
  const slots = document.getElementById("slotGrid");
  const topicInput = document.getElementById("bookingTopic");
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const monday = new Date(today); monday.setDate(monday.getDate() - ((monday.getDay() + 6) % 7));
  let week = new Date(monday);
  let selectedDate = "";
  const times = ["10:00", "13:00", "14:00", "16:00", "19:00"];
  const dateKey = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  const dateText = date => new Intl.DateTimeFormat("en", {weekday:"short", month:"short", day:"numeric"}).format(date);
  const timeText = value => { const [h, m] = value.split(":").map(Number); return new Intl.DateTimeFormat("en", {hour:"numeric", minute:"2-digit"}).format(new Date(2020, 0, 1, h, m)); };
  const priceText = rate => `₱${Number(rate).toLocaleString("en-PH", {minimumFractionDigits:2, maximumFractionDigits:2})}`;

  function renderWeek() {
    grid.replaceChildren();
    const end = new Date(week); end.setDate(end.getDate() + 6);
    document.getElementById("calendarLabel").textContent = week.getMonth() === end.getMonth()
      ? `${new Intl.DateTimeFormat("en", {month:"long"}).format(week)} ${week.getFullYear()}`
      : `${new Intl.DateTimeFormat("en", {month:"short"}).format(week)} – ${new Intl.DateTimeFormat("en", {month:"short", year:"numeric"}).format(end)}`;
    document.getElementById("prevWeek").disabled = week <= monday;
    for (let i = 0; i < 7; i++) {
      const day = new Date(week); day.setDate(day.getDate() + i);
      const key = dateKey(day);
      const button = document.createElement("button"); button.type = "button";
      button.className = `date-option${key === selectedDate ? " selected" : ""}${day < today ? " unavailable" : ""}`;
      button.disabled = day < today; button.dataset.date = key;
      button.innerHTML = `<span>${new Intl.DateTimeFormat("en", {weekday:"short"}).format(day)}</span><strong>${day.getDate()}</strong><small>${day < today ? "Past" : "5 times"}</small>`;
      button.addEventListener("click", () => selectDate(day));
      grid.append(button);
    }
  }

  function selectDate(day) {
    selectedDate = dateKey(day); renderWeek();
    document.getElementById("slotsTitle").textContent = `Available times · ${dateText(day)}`;
    document.getElementById("summaryDate").textContent = `${dateText(day)} · Choose a time`;
    slots.replaceChildren();
    times.forEach(time => {
      const button = document.createElement("button"); button.type = "button"; button.className = "time-option";
      button.textContent = timeText(time);
      button.addEventListener("click", () => {
        slots.querySelectorAll(".time-option").forEach(item => item.classList.toggle("selected", item === button));
        document.getElementById("summaryDate").textContent = `${dateText(day)} · ${timeText(time)} PHT`;
      });
      slots.append(button);
    });
  }

  document.getElementById("prevWeek").addEventListener("click", () => { week.setDate(week.getDate() - 7); renderWeek(); });
  document.getElementById("nextWeek").addEventListener("click", () => { week.setDate(week.getDate() + 7); renderWeek(); });
  document.querySelectorAll(".topic-chip").forEach(button => button.addEventListener("click", () => {
    topicInput.value = button.dataset.topic;
    document.querySelectorAll(".topic-chip").forEach(chip => chip.classList.toggle("selected", chip === button));
    document.getElementById("summaryTopic").textContent = button.dataset.topic;
  }));
  topicInput.addEventListener("input", () => { document.getElementById("summaryTopic").textContent = topicInput.value.trim() || "Choose a topic"; });
  document.getElementById("learningGoal").addEventListener("input", event => { document.getElementById("goalCount").textContent = `${event.target.value.length} / 850`; });
  document.querySelectorAll(".track-option").forEach(button => button.addEventListener("click", () => {
    document.querySelectorAll(".track-option").forEach(option => option.classList.toggle("selected", option === button));
    const rate = priceText(button.dataset.rate);
    document.getElementById("previewRate").textContent = rate;
    document.getElementById("summaryRate").textContent = rate;
    document.getElementById("summaryFormat").textContent = `${button.dataset.role === "mentor" ? "Academic" : "Gaming coaching"} · Online · 60 minutes`;
  }));
  document.getElementById("previewSubmit").addEventListener("click", () => {
    document.getElementById("previewResult").textContent = "Preview only — no booking was submitted.";
  });

  renderWeek();
  selectDate(today);
})();
