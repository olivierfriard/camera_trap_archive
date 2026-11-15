/* Display camera-trap UTM coordinates using the same converter as the form. */
(function () {
    "use strict";

    document.querySelectorAll("[data-fototrappola-coordinates]").forEach(block => {
        const latitudeText = block.dataset.latitude.trim();
        const longitudeText = block.dataset.longitude.trim();
        if (!latitudeText || !longitudeText) return;
        const latitude = Number(latitudeText);
        const longitude = Number(longitudeText);
        const zone = block.querySelector("[data-utm-zone]");
        const east = block.querySelector("[data-utm-east]");
        const north = block.querySelector("[data-utm-north]");
        const message = block.querySelector("[data-utm-message]");
        try {
            if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) {
                throw new RangeError("Invalid geographic coordinates");
            }
            const utm = window.CameraTrapUtm.fromLatLon(latitude, longitude);
            zone.textContent = utm.zone;
            east.textContent = utm.easting.toFixed(0);
            north.textContent = utm.northing.toFixed(0);
        } catch (error) {
            zone.textContent = east.textContent = north.textContent = "Non disponibile";
            message.textContent = latitude < -80 || latitude > 84
                ? "Coordinate UTM disponibili tra 80° S e 84° N."
                : "Conversione UTM temporaneamente non disponibile.";
        }
    });
})();
