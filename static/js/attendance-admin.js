(() => {
  const feedback = document.getElementById("attendanceFeedback");
  document.querySelectorAll(".attendance-resolution").forEach(form => form.addEventListener("submit", async event => {
    event.preventDefault();
    const outcome = event.submitter.value;
    const buttons = [...form.querySelectorAll("button")];
    buttons.forEach(button => button.disabled = true);
    try {
      const response = await fetch(`/api/admin/attendance/${form.dataset.id}/resolve`, {
        method: "POST", credentials: "same-origin",
        headers: {"Content-Type":"application/json", "X-CSRFToken":document.querySelector('meta[name="csrf-token"]').content},
        body: JSON.stringify({outcome, reason:form.elements.reason.value})
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.message || "Could not record the decision.");
      window.location.reload();
    } catch (error) {feedback.textContent = error.message; buttons.forEach(button => button.disabled = false);}
  }));
})();
