(() => {
  const feedback = document.getElementById("paymentFeedback");
  async function send(form, url, body, json = false) {
    const buttons = [...form.querySelectorAll("button")];
    const disabledBefore = buttons.map(b => b.disabled);
    buttons.forEach(b => b.disabled = true);
    try {
      const headers = {"X-CSRFToken": document.querySelector('meta[name="csrf-token"]').content};
      if (json) headers["Content-Type"] = "application/json";
      const response = await fetch(url, {method:"POST", credentials:"same-origin", headers, body});
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.message || "Request failed.");
      if (result.already_approved) {
        feedback.textContent = "This receipt revision was already approved. Reload to see the saved decision.";
        return;
      }
      window.location.reload();
    } catch (error) { feedback.textContent = error.message; buttons.forEach((b, i) => b.disabled = disabledBefore[i]); }
  }
  document.getElementById("paymentSubmission")?.addEventListener("submit", event => {
    event.preventDefault();
    const file = event.target.elements.receipt.files[0];
    if (file && file.size > 5 * 1024 * 1024) {
      feedback.textContent = "Receipt must be no larger than 5 MB."; return;
    }
    send(event.target, event.target.action, new FormData(event.target));
  });
  document.querySelectorAll(".payment-review").forEach(form => form.addEventListener("submit", event => {
    event.preventDefault(); const action = event.submitter.value;
    if (action === "approve" && !form.elements.confirmed.checked) {
      feedback.textContent = "Check the receiving account records before approving."; return;
    }
    const review = {reason: form.elements.reason.value, confirmed: form.elements.confirmed.checked};
    if (action === "approve") review.expected_revision = Number(form.dataset.revision);
    send(form, `/api/admin/payments/${form.dataset.id}/${action}`, JSON.stringify(review), true);
  }));
  document.querySelectorAll(".payment-reconciliation-form").forEach(form => form.addEventListener("submit", event => {
    event.preventDefault();
    const fields = form.elements;
    const data = {state: fields.state.value, expected_version: Number(form.dataset.version),
      reason: fields.reason.value, student_explanation: fields.student_explanation.value,
      confirmation_notes: fields.confirmation_notes.value, evidence_reference: fields.evidence_reference.value,
      authority_name: fields.authority_name.value, authority_capacity: fields.authority_capacity.value};
    for (const name of ["independent_check_confirmed", "funds_received_confirmed", "authorized_refund_confirmed", "account_holder_refund_confirmed", "authorized_retention_confirmed"]) {
      data[name] = fields[name].checked;
    }
    send(form, `/api/admin/payments/${form.dataset.id}/reconcile`, JSON.stringify(data), true);
  }));
  document.querySelectorAll(".admin-booking-end").forEach(form => form.addEventListener("submit", event => {
    event.preventDefault();
    if (!event.submitter) return;
    send(form, `/api/admin/bookings/${form.dataset.bookingId}/${event.submitter.value}`, JSON.stringify({reason: form.elements.reason.value}), true);
  }));
})();
