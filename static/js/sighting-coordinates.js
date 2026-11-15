/* Editable sighting coordinates; camera defaults never update the camera trap. */
(function () {
    "use strict";

    const editor = document.getElementById("sightingCoordinates");
    if (!editor) return;
    const form = document.getElementById("uploadForm");
    const select = document.querySelector('select[name="camtrap_id"]');
    const details = document.getElementById("details");
    const latitude = document.getElementById("sightingLatitude");
    const longitude = document.getElementById("sightingLongitude");
    const zone = document.getElementById("sightingUtmZone");
    const east = document.getElementById("sightingUtmEast");
    const north = document.getElementById("sightingUtmNorth");
    const source = document.getElementById("sightingCoordinateSource");
    const status = document.getElementById("sightingCoordinateStatus");
    const toggle = document.getElementById("sightingMapToggle");
    const panel = document.getElementById("sightingMapPanel");
    const utmFields = [zone, east, north];
    const allFields = [latitude, longitude, ...utmFields];
    const utmAvailable = typeof window.proj4 === "function" && !!window.CameraTrapUtm;
    let camera = null;
    let loading = false;
    let activeRequest = null;
    let map = null;
    let marker = null;

    if (!utmAvailable) utmFields.forEach(field => { field.disabled = true; });
    if (!window.L) {
        toggle.disabled = true;
        toggle.textContent = "Mappa non disponibile";
    }

    function readCamera() {
        const data = details.querySelector("[data-camera-details]");
        return data ? {id: data.dataset.cameraId, lat: data.dataset.latitude, lon: data.dataset.longitude} : null;
    }

    function geographic() {
        if (!latitude.value.trim() || !longitude.value.trim()) return null;
        const lat = Number(latitude.value);
        const lon = Number(longitude.value);
        return Number.isFinite(lat) && Number.isFinite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180
            ? {lat, lon} : null;
    }

    function updateSource() {
        const same = (current, original) => !current.trim() || !original.trim()
            ? current.trim() === original.trim()
            : Number.isFinite(Number(current)) && Math.abs(Number(current) - Number(original)) < 0.0000001;
        const fromCamera = camera && (camera.lat.trim() || camera.lon.trim())
            && same(latitude.value, camera.lat) && same(longitude.value, camera.lon);
        source.classList.toggle("is-hidden", !fromCamera);
    }

    function updateMap() {
        if (!map) return;
        const point = geographic();
        if (!point) {
            if (marker) marker.remove();
            marker = null;
            return;
        }
        if (!marker) {
            marker = window.L.marker([point.lat, point.lon], {draggable: true}).addTo(map);
            marker.on("dragend", event => {
                const moved = event.target.getLatLng();
                setPoint(moved.lat, moved.lng);
            });
        } else marker.setLatLng([point.lat, point.lon]);
        map.panTo([point.lat, point.lon]);
    }

    function fromGeographic() {
        allFields.forEach(field => field.setCustomValidity(""));
        utmFields.forEach(field => { field.value = ""; });
        status.textContent = "";
        const point = geographic();
        if (!point && (latitude.value.trim() || longitude.value.trim())) {
            status.textContent = "Completa entrambe le coordinate: latitudine da -90 a 90, longitudine da -180 a 180.";
            latitude.setCustomValidity(status.textContent);
        } else if (point) {
            if (!utmAvailable) status.textContent = "Conversione UTM non disponibile. Puoi modificare latitudine e longitudine.";
            else {
                try {
                    const utm = window.CameraTrapUtm.fromLatLon(point.lat, point.lon);
                    zone.value = utm.zone;
                    east.value = utm.easting.toFixed(0);
                    north.value = utm.northing.toFixed(0);
                } catch (error) {
                    status.textContent = "Coordinate UTM disponibili tra 80° S e 84° N.";
                }
            }
        }
        updateSource();
        updateMap();
    }

    function fromUtm() {
        allFields.forEach(field => field.setCustomValidity(""));
        latitude.value = longitude.value = "";
        status.textContent = "";
        if (utmFields.every(field => field.value.trim())) {
            try {
                const point = window.CameraTrapUtm.toLatLon(zone.value, Number(east.value), Number(north.value));
                latitude.value = point.latitude.toFixed(8);
                longitude.value = point.longitude.toFixed(8);
                zone.value = point.zone;
            } catch (error) {
                status.textContent = "Coordinate UTM non valide. Verifica zona, banda, Est e Nord.";
            }
        } else if (utmFields.some(field => field.value.trim())) {
            status.textContent = "Completa zona, Est e Nord UTM.";
        }
        zone.setCustomValidity(status.textContent);
        updateSource();
        updateMap();
    }

    function setPoint(lat, lon) {
        latitude.value = lat.toFixed(8);
        longitude.value = lon.toFixed(8);
        fromGeographic();
    }

    latitude.addEventListener("input", fromGeographic);
    longitude.addEventListener("input", fromGeographic);
    utmFields.forEach(field => field.addEventListener("input", fromUtm));
    toggle.addEventListener("click", () => {
        panel.hidden = !panel.hidden;
        toggle.setAttribute("aria-expanded", String(!panel.hidden));
        toggle.textContent = panel.hidden ? "Mostra mappa" : "Nascondi mappa";
        if (panel.hidden || !window.L) return;
        if (!map) {
            const point = geographic();
            map = window.L.map("sightingMap").setView(point ? [point.lat, point.lon] : [45, 7.6], point ? 14 : 6);
            window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
                maxZoom: 19, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>',
            }).addTo(map);
            map.on("click", event => setPoint(event.latlng.lat, event.latlng.lng));
        }
        updateMap();
        setTimeout(() => map.invalidateSize(), 0);
    });

    select.addEventListener("change", () => {
        loading = true;
        activeRequest = null;
        camera = null;
        details.replaceChildren();
        latitude.value = longitude.value = "";
        fromGeographic();
        status.textContent = "Caricamento delle coordinate della fototrappola…";
    }, true);
    document.body.addEventListener("htmx:beforeRequest", event => {
        if (event.detail.elt === select) activeRequest = event.detail.xhr;
    });
    document.body.addEventListener("htmx:afterSwap", event => {
        if (event.detail.target !== details) return;
        const selected = readCamera();
        if (selected && selected.id !== select.value) return;
        camera = selected;
        loading = false;
        activeRequest = null;
        latitude.value = camera ? camera.lat : "";
        longitude.value = camera ? camera.lon : "";
        fromGeographic();
    });
    document.body.addEventListener("htmx:afterRequest", event => {
        if (event.detail.elt !== select || event.detail.xhr !== activeRequest) return;
        loading = false;
        activeRequest = null;
        if (!event.detail.successful) status.textContent = "Impossibile caricare la fototrappola. Puoi inserire le coordinate manualmente.";
    });
    form.addEventListener("submit", event => {
        if (loading) {
            event.preventDefault();
            status.textContent = "Attendi il caricamento della fototrappola prima di salvare.";
        }
    });

    camera = readCamera();
    if (editor.dataset.preserveCoordinates !== "true" && camera) {
        latitude.value = camera.lat;
        longitude.value = camera.lon;
    }
    fromGeographic();
})();
