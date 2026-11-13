/* WGS84 / UTM conversions using Proj4js. Zone letters are latitude bands. */
(function (global) {
    "use strict";

    const bands = "CDEFGHJKLMNPQRSTUVWX";

    function projection(zone, southern) {
        return `+proj=utm +zone=${zone} ${southern ? "+south" : ""} +datum=WGS84 +units=m +no_defs`;
    }

    function fromLatLon(latitude, longitude) {
        if (!Number.isFinite(latitude) || !Number.isFinite(longitude)
            || latitude < -80 || latitude > 84 || longitude < -180 || longitude > 180) {
            throw new RangeError("Coordinates outside UTM coverage");
        }
        let zone = Math.min(60, Math.floor((longitude + 180) / 6) + 1);
        // Apply the standard Norway and Svalbard grid-zone exceptions.
        if (latitude >= 56 && latitude < 64 && longitude >= 3 && longitude < 12) zone = 32;
        if (latitude >= 72 && longitude >= 0 && longitude < 42) {
            zone = longitude < 9 ? 31 : longitude < 21 ? 33 : longitude < 33 ? 35 : 37;
        }
        const band = bands[Math.min(19, Math.floor((latitude + 80) / 8))];
        const [easting, northing] = global.proj4(
            "EPSG:4326", projection(zone, latitude < 0), [longitude, latitude]
        );
        if (!Number.isFinite(easting) || !Number.isFinite(northing)) {
            throw new RangeError("Invalid projected coordinates");
        }
        return {zone: `${zone}${band}`, easting, northing};
    }

    function toLatLon(zoneText, easting, northing) {
        const match = /^([1-9]|[1-5][0-9]|60)([C-HJ-NP-X])$/.exec(zoneText.trim().toUpperCase());
        if (!match || !Number.isFinite(easting) || !Number.isFinite(northing)
            || easting < 100000 || easting >= 1000000 || northing < 0 || northing > 10000000) {
            throw new RangeError("Invalid UTM coordinates");
        }
        const zone = Number(match[1]);
        const band = match[2];
        const [longitude, latitude] = global.proj4(
            projection(zone, band < "N"), "EPSG:4326", [easting, northing]
        );
        const lower = -80 + bands.indexOf(band) * 8;
        const upper = band === "X" ? 84 : lower + 8;
        // Allow sub-metre rounding at band boundaries, without accepting a wrong band.
        const tolerance = 0.000001;
        if (!Number.isFinite(latitude) || !Number.isFinite(longitude)
            || latitude < lower - tolerance || latitude > upper + tolerance
            || longitude < -180 - tolerance || longitude > 180 + tolerance) {
            throw new RangeError("Coordinates do not match the UTM latitude band");
        }
        return {
            latitude: Math.max(-80, Math.min(84, latitude)),
            longitude: Math.max(-180, Math.min(180, longitude)),
            zone: `${zone}${band}`,
        };
    }

    global.CameraTrapUtm = {fromLatLon, toLatLon};
})(window);
