"use strict";

(function setupNavbarDropdownAndLogout() {
    const menu = document.getElementById("profileMenu");
    const avatar = document.getElementById("userAvatar");
    const dropdown = document.getElementById("profileDropdown");

    if (!menu || !avatar || !dropdown) return;

    function closeMenu(returnFocus = false) {
        dropdown.classList.add("hidden");
        avatar.setAttribute("aria-expanded", "false");
        if (returnFocus) avatar.focus();
    }

    avatar.addEventListener("click", (event) => {
        event.stopPropagation();
        const isOpen = !dropdown.classList.contains("hidden");
        dropdown.classList.toggle("hidden", isOpen);
        avatar.setAttribute("aria-expanded", String(!isOpen));
    });

    document.addEventListener("click", (event) => {
        if (!menu.contains(event.target)) closeMenu();
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") closeMenu(true);
    });

    const logoutButton = document.getElementById("logoutBtn");
    if (!logoutButton) return;

    logoutButton.addEventListener("click", async (event) => {
        event.preventDefault();
        try {
            const meta = document.querySelector('meta[name="csrf-token"]');
            let csrfToken = meta && meta.content ? meta.content : null;

            if (!csrfToken) {
                const tokenResponse = await fetch("/api/csrf-token", { credentials: "same-origin" });
                const tokenData = await tokenResponse.json();
                if (!tokenResponse.ok || !tokenData.csrf_token) {
                    throw new Error("Could not obtain the logout security token.");
                }
                csrfToken = tokenData.csrf_token;
            }

            const logoutResponse = await fetch("/api/auth/logout", {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    Accept: "application/json",
                    "X-CSRFToken": csrfToken
                }
            });
            if (!logoutResponse.ok) {
                const data = await logoutResponse.json().catch(() => ({}));
                throw new Error(data.message || "Logout request was rejected.");
            }
            window.location.href = "/auth";
        } catch (error) {
            window.alert(error.message || "Logout could not be completed. Please try again.");
        }
    });
})();
