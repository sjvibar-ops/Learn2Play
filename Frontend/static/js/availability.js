"use strict";
(() => {
  const form = document.getElementById("availabilityForm");
  if (!form) return;
  const list = document.getElementById("availabilityList");
  const feedback = document.getElementById("availabilityFeedback");
  const unavailableForm = document.getElementById("unavailableForm");
  const unavailableList = document.getElementById("unavailableList");
  const unavailableDate = document.getElementById("unavailableDate");
  const days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const csrf = async () => (await fetch("/api/csrf-token")).json().then(data => data.csrf_token);
  // ----- 10-minute time dropdowns (12-hour labels, 24-hour HH:MM values) -----
  const startSelect = document.getElementById("availabilityStart");
  const endSelect = document.getElementById("availabilityEnd");
  const STEP = 10, SESSION = 60, LAST_END = 23 * 60 + 50, DEFAULT_START = 8 * 60;
  const pad = n => String(n).padStart(2, "0");
  const toValue = total => `${pad(Math.floor(total / 60))}:${pad(total % 60)}`;
  const toLabel = total => {
    const h = Math.floor(total / 60), m = total % 60;
    return `${h % 12 === 0 ? 12 : h % 12}:${pad(m)} ${h < 12 ? "AM" : "PM"}`;
  };
  const toMinutes = value => {
    const match = /^(\d{2}):(\d{2})$/.exec(value || "");
    return match ? Number(match[1]) * 60 + Number(match[2]) : null;
  };
  function fillSelect(select, from, to, selected) {
    select.replaceChildren();
    for (let t = from; t <= to; t += STEP) {
      const option = document.createElement("option");
      option.value = toValue(t); option.textContent = toLabel(t);
      select.appendChild(option);
    }
    select.value = toValue(selected);
  }
  // Until only offers times at least 60 minutes after From, on the same day.
  function refreshEnd() {
    const start = toMinutes(startSelect.value);
    if (start === null) return;
    const previous = toMinutes(endSelect.value);
    const earliest = start + SESSION;
    fillSelect(endSelect, earliest, LAST_END, previous !== null && previous >= earliest ? previous : earliest);
  }
  function initTimes() {
    // From must leave room for one full 60-minute session before midnight.
    fillSelect(startSelect, 0, LAST_END - SESSION, DEFAULT_START);
    endSelect.replaceChildren();
    refreshEnd();
  }
  startSelect.addEventListener("change", refreshEnd);
  form.addEventListener("reset", () => setTimeout(initTimes, 0));
  initTimes();
  async function load() {
    try {
      const data = await fetch("/api/availability").then(r => r.json());
      list.replaceChildren();
      if (!data.availability?.length) list.textContent = "No weekly availability yet. Add a window above.";
      (data.availability || []).forEach(row => {
        const item = document.createElement("div"); item.className = "availability-row";
        const label = document.createElement("span"); label.textContent = `${days[row.weekday]} \u00b7 ${row.start_time}\u2013${row.end_time}`;
        const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "Remove";
        remove.addEventListener("click", async () => {
          const token = await csrf();
          const result = await fetch("/api/availability", {method:"DELETE", headers:{"Content-Type":"application/json", "X-CSRFToken":token}, body:JSON.stringify({id:row.id})}).then(r => r.json());
          if (result.ok) load(); else feedback.textContent = result.message || "Could not remove availability.";
        });
        item.append(label, remove); list.appendChild(item);
      });
    } catch (_) { list.textContent = "Could not load availability."; }
    try {
      const data = await fetch("/api/unavailable-dates").then(r => r.json());
      unavailableList.replaceChildren();
      (data.dates || []).forEach(row => {
        const item = document.createElement("div"); item.className = "availability-row";
        const label = document.createElement("span"); label.textContent = row.date;
        const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "Restore availability";
        remove.addEventListener("click", async () => {
          const token = await csrf();
          const result = await fetch("/api/unavailable-dates", {method:"DELETE", headers:{"Content-Type":"application/json", "X-CSRFToken":token}, body:JSON.stringify({id:row.id})}).then(r => r.json());
          if (result.ok) load(); else feedback.textContent = result.message || "Could not restore date.";
        });
        item.append(label, remove); unavailableList.appendChild(item);
      });
      if (!data.dates?.length) { unavailableList.innerHTML = "<p style=\"color:var(--txt3,#8b8fa8);font-size:13px;margin-top:8px;\">No dates marked unavailable.</p>"; }
    } catch (_) { unavailableList.textContent = "Could not load unavailable dates."; }
  }
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const payload = {weekday:document.getElementById("availabilityDay").value,
      start_time:startSelect.value,
      end_time:endSelect.value};
    const startMinutes = toMinutes(payload.start_time), endMinutes = toMinutes(payload.end_time);
    if (startMinutes === null || endMinutes === null || startMinutes % STEP || endMinutes % STEP) {
      feedback.textContent = "Choose a start and end time from the lists."; return;
    }
    if (endMinutes - startMinutes < SESSION) {
      feedback.textContent = "Choose an end time at least 1 hour after the start time."; refreshEnd(); return;
    }
    const token = await csrf();
    const response = await fetch("/api/availability", {method:"POST", headers:{"Content-Type":"application/json", "X-CSRFToken":token}, body:JSON.stringify(payload)});
    const data = await response.json(); feedback.textContent = data.message || (data.ok ? "Availability added." : "Could not save availability.");
    if (data.ok) { form.reset(); load(); }
  });
  const tomorrowPht = new Date(Date.now() + 8 * 3600000);
  tomorrowPht.setUTCDate(tomorrowPht.getUTCDate() + 1);
  unavailableDate.min = tomorrowPht.toISOString().slice(0,10);
  unavailableForm.addEventListener("submit", async event => {
    event.preventDefault();
    const token = await csrf();
    const result = await fetch("/api/unavailable-dates", {method:"POST", headers:{"Content-Type":"application/json", "X-CSRFToken":token}, body:JSON.stringify({date:unavailableDate.value})}).then(r => r.json());
    feedback.textContent = result.message || (result.ok ? "Date blocked." : "Could not block date.");
    if (result.ok) { unavailableForm.reset(); load(); }
  });
  load();
})();

