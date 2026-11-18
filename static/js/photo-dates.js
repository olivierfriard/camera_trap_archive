"use strict";
const photoDateSource = document.getElementById("photoDateSource");
if (photoDateSource) {
    ["sightingDate", "sightingTime"].forEach(id => {
        document.getElementById(id).addEventListener("input", () => { photoDateSource.textContent = "Inserimento manuale"; });
    });
}
document.querySelectorAll(".photo-date-choice").forEach(button => {
    button.addEventListener("click", () => {
        document.getElementById("sightingDate").value = button.dataset.date;
        document.getElementById("sightingTime").value = button.dataset.time;
        if (photoDateSource) photoDateSource.textContent = "Fonte: " + button.dataset.sourceLabel;
    });
});
