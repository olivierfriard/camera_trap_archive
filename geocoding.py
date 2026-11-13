"""Nominatim reverse geocoding with a shared on-disk cache and request limit.

All workers on one application host must share the same cache file. The endpoint
can be changed using nominatim_url in .env; nominatim_user_agent can include an
operator contact. No upstream request is made on a cache hit.
"""

import json
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class GeocodingError(Exception):
    """Expose a localized error and optional retry delay to the form."""

    def __init__(self, message, status=503, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def reverse_geocode(latitude, longitude, *, cache_path, service_url, user_agent):
    """Resolve administrative names while serializing upstream calls across workers."""
    key = json.dumps([service_url, f"{latitude:.6f}", f"{longitude:.6f}"])
    try:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(cache_path, timeout=0.1)) as cache:
            cache.execute(
                "CREATE TABLE IF NOT EXISTS addresses "
                "(key TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL)"
            )
            cache.execute(
                "CREATE TABLE IF NOT EXISTS request_limit "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), next_request REAL NOT NULL)"
            )
            # The write lock is held until the HTTP call finishes, including failures.
            cache.execute("BEGIN IMMEDIATE")
            now = time.time()
            cached = cache.execute(
                "SELECT payload FROM addresses WHERE key = ? AND expires > ?", (key, now)
            ).fetchone()
            if cached:
                return json.loads(cached[0])
            limit = cache.execute("SELECT next_request FROM request_limit WHERE id = 1").fetchone()
            if limit and limit[0] > now:
                raise GeocodingError(
                    "Servizio occupato. Riprova tra qualche secondo.",
                    status=429, retry_after=math.ceil(limit[0] - now),
                )
            delay = 1.1
            try:
                params = urlencode({
                    "lat": latitude, "lon": longitude, "format": "geocodejson",
                    "addressdetails": 1, "accept-language": "it", "zoom": 10,
                })
                request = Request(
                    f"{service_url}?{params}",
                    headers={"User-Agent": user_agent, "Accept": "application/json"},
                )
                with urlopen(request, timeout=5) as response:
                    data = json.load(response)
                features = data.get("features", [])
                if not features:
                    raise GeocodingError("Nessun indirizzo trovato. Compila i campi manualmente.", status=404)
                address = features[0]["properties"]["geocoding"]
                admin = address.get("admin", {})
                result = {
                    "comune": address.get("city") or admin.get("level8", ""),
                    "provincia": address.get("county") or admin.get("level6", ""),
                    "regione": address.get("state") or admin.get("level4", ""),
                    "country": address.get("country", ""),
                }
                if not all(isinstance(value, str) for value in result.values()):
                    raise ValueError("Invalid address fields")
                if not any(result.values()):
                    raise GeocodingError("Nessun indirizzo trovato. Compila i campi manualmente.", status=404)
                cache.execute("DELETE FROM addresses WHERE expires <= ?", (now,))
                cache.execute(
                    "INSERT OR REPLACE INTO addresses (key, payload, expires) VALUES (?, ?, ?)",
                    (key, json.dumps(result), now + 30 * 86400),
                )
                return result
            except HTTPError as error:
                delay = 60 if error.code in (403, 429, 503) else 1.1
                raise GeocodingError("Servizio non disponibile. Compila i campi manualmente.") from error
            except (URLError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                raise GeocodingError("Servizio non disponibile. Compila i campi manualmente.") from error
            finally:
                # Persist the cooldown even when the service times out or rejects a call.
                cache.execute(
                    "INSERT OR REPLACE INTO request_limit (id, next_request) VALUES (1, ?)",
                    (time.time() + delay,),
                )
                cache.commit()
    except sqlite3.OperationalError as error:
        if "locked" in str(error):
            raise GeocodingError("Servizio occupato. Riprova tra qualche secondo.", 429, 2) from error
        raise GeocodingError("Servizio non disponibile. Compila i campi manualmente.") from error
    except (sqlite3.Error, OSError) as error:
        raise GeocodingError("Servizio non disponibile. Compila i campi manualmente.") from error
