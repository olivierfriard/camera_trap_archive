"use strict";

(() => {
    const form = document.getElementById("uploadForm");
    const date = document.getElementById("sightingDate");
    if (!form || !date || !form.hasAttribute("data-sighting-user-code")) return;
    const code = form.elements.namedItem("code");
    if (!code) return;

    function updateCode() {
        const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date.value.trim());
        if (!parts) return;
        const year = Number(parts[1]);
        const month = Number(parts[2]);
        const day = Number(parts[3]);
        const calendar = new Date(0);
        calendar.setUTCFullYear(year, month - 1, day);
        if (year < 1 || calendar.getUTCFullYear() !== year ||
            calendar.getUTCMonth() !== month - 1 || calendar.getUTCDate() !== day) return;
        code.value = `A${parts[1].slice(-2)}${parts[2]}${parts[3]}${form.dataset.sightingUserCode}`;
    }

    date.addEventListener("input", updateCode);
    date.addEventListener("change", updateCode);
})();
