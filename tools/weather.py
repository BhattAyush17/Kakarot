"""Weather forecast tool backed by the free Open-Meteo API.

Open-Meteo needs no API key and no signup: a place name is resolved to
latitude/longitude through its geocoding endpoint (cached per process), then the
forecast endpoint returns current conditions plus a few days ahead. The LLM
phrases the spoken answer in the user's language from the structured result, so
this module only fetches and normalizes the data.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from core.config_store import get_config

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT_SECONDS = 15.0
FORECAST_DAYS = 3

# WMO weather interpretation codes -> short English gloss. The model rewrites
# these in the user's language; keeping them here means one stable vocabulary.
WEATHER_CODES: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snowfall",
    73: "moderate snowfall",
    75: "heavy snowfall",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}

# Geocoding rarely changes for a given query, so cache resolved coordinates for
# the lifetime of the process to avoid a lookup on every forecast request.
_GEOCODE_CACHE: dict[str, dict[str, Any] | None] = {}


def _describe_code(code: Any) -> str:
    try:
        return WEATHER_CODES.get(int(code), "unknown conditions")
    except (TypeError, ValueError):
        return "unknown conditions"


def _configured_location(config: dict[str, Any] | None) -> str:
    config = config if isinstance(config, dict) else get_config()
    return str(config.get("tools", {}).get("weather_location") or "").strip()


async def _get_json(url: str, params: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


def _split_location(location: str) -> tuple[str, str | None]:
    """Split "City, BR" into ("City", "BR"); a trailing 2-letter token is read
    as an ISO country code. Anything else is returned as the name unchanged."""
    parts = [part.strip() for part in location.split(",") if part.strip()]
    if len(parts) >= 2 and len(parts[-1]) == 2 and parts[-1].isalpha():
        return ", ".join(parts[:-1]), parts[-1].upper()
    return parts[0] if parts else location, None


async def _geocode_query(name: str, country_code: str | None) -> dict[str, Any] | None:
    params: dict[str, Any] = {"name": name, "count": 1, "language": "pt", "format": "json"}
    if country_code:
        params["countryCode"] = country_code
    data = await _get_json(GEOCODE_URL, params)
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or not results:
        return None
    top = results[0]
    return {
        "name": str(top.get("name") or name),
        "country": str(top.get("country") or ""),
        "admin1": str(top.get("admin1") or ""),
        "latitude": top.get("latitude"),
        "longitude": top.get("longitude"),
        "timezone": str(top.get("timezone") or "auto"),
    }


async def _geocode(location: str) -> dict[str, Any] | None:
    """Resolve a place name to a coordinates record, or None if not found.

    Open-Meteo's geocoder matches a bare place name, so "São Paulo, BR" finds
    nothing as one string. Try the whole string first, then fall back to the
    city name with the trailing country code passed separately.
    """
    key = location.strip().lower()
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]
    record = await _geocode_query(location.strip(), None)
    if record is None:
        name, country_code = _split_location(location)
        if name != location.strip() or country_code:
            record = await _geocode_query(name, country_code)
    _GEOCODE_CACHE[key] = record
    return record


async def _fetch_forecast(latitude: Any, longitude: Any) -> dict[str, Any]:
    return await _get_json(
        FORECAST_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
            "precipitation,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max",
            "timezone": "auto",
            "forecast_days": FORECAST_DAYS,
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
        },
    )


def _place_label(geo: dict[str, Any]) -> str:
    parts = [geo.get("name"), geo.get("admin1"), geo.get("country")]
    seen: list[str] = []
    for part in parts:
        text = str(part or "").strip()
        if text and text not in seen:
            seen.append(text)
    return ", ".join(seen[:2]) if seen else "the location"


def _round(value: Any) -> Any:
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return value


def _build_result(geo: dict[str, Any], forecast: dict[str, Any]) -> dict[str, Any]:
    current = forecast.get("current") or {}
    daily = forecast.get("daily") or {}
    place = _place_label(geo)

    current_out = {
        "temperature_c": current.get("temperature_2m"),
        "feels_like_c": current.get("apparent_temperature"),
        "humidity_pct": current.get("relative_humidity_2m"),
        "wind_kmh": current.get("wind_speed_10m"),
        "precipitation_mm": current.get("precipitation"),
        "weather_code": current.get("weather_code"),
        "conditions": _describe_code(current.get("weather_code")),
    }

    def _at(field: str, index: int) -> Any:
        values = daily.get(field) or []
        return values[index] if index < len(values) else None

    days_out: list[dict[str, Any]] = []
    for index, day in enumerate(daily.get("time") or []):
        days_out.append({
            "date": day,
            "conditions": _describe_code(_at("weather_code", index)),
            "min_c": _at("temperature_2m_min", index),
            "max_c": _at("temperature_2m_max", index),
            "precipitation_probability_pct": _at("precipitation_probability_max", index),
        })

    answer = (
        f"Weather in {place}: {current_out['conditions']}, {_round(current_out['temperature_c'])}°C"
        f" (feels like {_round(current_out['feels_like_c'])}°C), wind {_round(current_out['wind_kmh'])} km/h,"
        f" humidity {_round(current_out['humidity_pct'])}%."
    )
    if days_out:
        today = days_out[0]
        answer += (
            f" Today: {today['conditions']}, {_round(today['min_c'])}–{_round(today['max_c'])}°C,"
            f" {_round(today['precipitation_probability_pct'])}% chance of precipitation."
        )
    if len(days_out) > 1:
        tomorrow = days_out[1]
        answer += (
            f" Tomorrow: {tomorrow['conditions']}, {_round(tomorrow['min_c'])}–{_round(tomorrow['max_c'])}°C,"
            f" {_round(tomorrow['precipitation_probability_pct'])}% chance of precipitation."
        )

    return {
        "ok": True,
        "location": place,
        "current": current_out,
        "daily": days_out,
        "answer": answer,
    }


async def get_weather(location: str | None = None, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return current conditions + a short forecast for ``location``.

    Falls back to the location configured in the BitMon config when no explicit
    location is given.
    """
    started = time.perf_counter()
    requested = str(location or "").strip() or _configured_location(config)
    if not requested:
        message = (
            "No location is set. Add a city in the BitMon config, Capabilities tab, "
            "or tell me which city you want the weather for."
        )
        return {"ok": False, "error": message, "answer": message}

    try:
        geo = await _geocode(requested)
    except Exception as exc:  # noqa: BLE001 - network/parse error surfaced to caller
        message = f"Could not reach the weather service: {exc}"
        return {"ok": False, "error": message, "answer": message}

    if not geo or geo.get("latitude") is None or geo.get("longitude") is None:
        message = f"I could not find a place called {requested!r}."
        return {"ok": False, "error": message, "answer": message}

    try:
        forecast = await _fetch_forecast(geo["latitude"], geo["longitude"])
    except Exception as exc:  # noqa: BLE001 - network/parse error surfaced to caller
        message = f"Could not reach the weather service: {exc}"
        return {"ok": False, "error": message, "answer": message}

    result = _build_result(geo, forecast)
    result["elapsed_seconds"] = round(time.perf_counter() - started, 2)
    return result
