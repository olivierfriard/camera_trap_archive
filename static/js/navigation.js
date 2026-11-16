"use strict";

(() => {
    const burger = document.querySelector(".navbar-burger");
    const menu = document.getElementById(burger.getAttribute("aria-controls"));
    const dropdowns = document.querySelectorAll(".navbar .has-dropdown");

    function closeDropdowns() {
        dropdowns.forEach(dropdown => {
            dropdown.classList.remove("is-active");
            dropdown.querySelector(".navbar-link").setAttribute("aria-expanded", "false");
        });
    }

    burger.addEventListener("click", () => {
        const expanded = burger.getAttribute("aria-expanded") !== "true";
        burger.classList.toggle("is-active", expanded);
        menu.classList.toggle("is-active", expanded);
        burger.setAttribute("aria-expanded", String(expanded));
        if (!expanded) closeDropdowns();
    });

    dropdowns.forEach(dropdown => {
        const trigger = dropdown.querySelector(".navbar-link");
        trigger.addEventListener("click", () => {
            const expanded = trigger.getAttribute("aria-expanded") !== "true";
            closeDropdowns();
            dropdown.classList.toggle("is-active", expanded);
            trigger.setAttribute("aria-expanded", String(expanded));
        });
    });

    document.addEventListener("click", event => {
        if (!event.target.closest(".navbar .has-dropdown")) closeDropdowns();
    });
    document.addEventListener("keydown", event => {
        if (event.key !== "Escape") return;
        const expanded = document.querySelector('.navbar-link[aria-expanded="true"]');
        closeDropdowns();
        if (expanded) expanded.focus();
        else if (menu.classList.contains("is-active")) {
            burger.click();
            burger.focus();
        }
    });
})();
