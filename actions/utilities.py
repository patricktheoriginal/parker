"""
utilities.py — everyday utility tools for Parker.

All use free services (no API key) or local computation:
  - currency_convert : exchange rates (exchangerate-api)
  - air_quality      : AQI / PM2.5 for a Vietnamese city (Open-Meteo)
  - wiki_lookup      : Wikipedia summary
  - crypto_price     : crypto prices in USD/VND (CoinGecko)
  - unit_convert     : length/weight/temperature/etc. conversions (local)
  - calculate        : safe arithmetic expression evaluation (local)
  - lunar_date       : Vietnamese lunar calendar date (local)
"""

import json
import re
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

_UA = "Parker-Assistant/1.0"


def _get(url: str, timeout: int = 12) -> dict:
    req = Request(url, headers={"User-Agent": _UA})
    with urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ── Currency ─────────────────────────────────────────────────────────────────
def currency_convert(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    amount = p.get("amount", 1)
    frm = (p.get("from") or "USD").upper().strip()
    to = (p.get("to") or "VND").upper().strip()
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        amount = 1.0
    try:
        data = _get(f"https://api.exchangerate-api.com/v4/latest/{quote(frm)}")
        rate = data["rates"].get(to)
        if rate is None:
            return f"Sir, I don't have a rate for {frm} to {to}."
        converted = amount * rate
        return f"{amount:,.2f} {frm} = {converted:,.2f} {to} (rate {rate:,.4f})."
    except Exception as e:
        return f"Sir, I couldn't get the exchange rate: {e}"


# ── Air quality ──────────────────────────────────────────────────────────────
def air_quality(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    place = (p.get("city") or p.get("place") or "").strip()
    try:
        from actions.weather_report import _geocode_vn, current_location
        if place:
            geo = _geocode_vn(place)
            if not geo:
                return f"Sir, I couldn't find '{place}'."
            lat, lon, label = geo
        else:
            loc = current_location()
            if not loc:
                return "Sir, tell me a city for the air quality."
            lat, lon, label = loc["lat"], loc["lon"], loc["label"]
        d = _get("https://air-quality-api.open-meteo.com/v1/air-quality?"
                 + urlencode({"latitude": lat, "longitude": lon,
                              "current": "pm2_5,pm10,us_aqi"}))
        cur = d["current"]
        aqi = cur.get("us_aqi")
        pm25 = cur.get("pm2_5")
        if aqi is None:
            return f"Sir, air-quality data isn't available for {label}."
        if aqi <= 50:      level = "good"
        elif aqi <= 100:   level = "moderate"
        elif aqi <= 150:   level = "unhealthy for sensitive groups"
        elif aqi <= 200:   level = "unhealthy"
        elif aqi <= 300:   level = "very unhealthy"
        else:              level = "hazardous"
        advice = "" if aqi <= 100 else " Consider a mask outdoors."
        return (f"Air quality in {label}: AQI {aqi} ({level}), PM2.5 {pm25} µg/m³."
                + advice)
    except Exception as e:
        return f"Sir, I couldn't get the air quality: {e}"


# ── Wikipedia ────────────────────────────────────────────────────────────────
def wiki_lookup(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    topic = (p.get("topic") or p.get("query") or "").strip()
    if not topic:
        return "Sir, what should I look up?"
    try:
        d = _get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(topic)}")
        extract = d.get("extract", "")
        if not extract:
            return f"Sir, I couldn't find a Wikipedia article on '{topic}'."
        return extract[:600]
    except Exception:
        return f"Sir, I couldn't find a Wikipedia article on '{topic}'."


# ── Crypto ───────────────────────────────────────────────────────────────────
_CRYPTO_IDS = {
    "btc": "bitcoin", "bitcoin": "bitcoin", "eth": "ethereum", "ethereum": "ethereum",
    "bnb": "binancecoin", "sol": "solana", "solana": "solana", "xrp": "ripple",
    "doge": "dogecoin", "ada": "cardano", "usdt": "tether",
}


def crypto_price(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    coin = (p.get("coin") or p.get("symbol") or "bitcoin").lower().strip()
    cid = _CRYPTO_IDS.get(coin, coin)
    try:
        d = _get("https://api.coingecko.com/api/v3/simple/price?"
                 + urlencode({"ids": cid, "vs_currencies": "usd,vnd"}))
        if cid not in d:
            return f"Sir, I don't recognize the coin '{coin}'."
        usd = d[cid].get("usd")
        vnd = d[cid].get("vnd")
        return f"{cid.title()}: ${usd:,.2f} USD" + (f" (~{vnd:,.0f} VND)." if vnd else ".")
    except Exception as e:
        return f"Sir, I couldn't get the crypto price: {e}"


# ── Unit conversion (local) ──────────────────────────────────────────────────
_LENGTH = {"m": 1, "km": 1000, "cm": 0.01, "mm": 0.001, "mile": 1609.34,
           "miles": 1609.34, "ft": 0.3048, "feet": 0.3048, "inch": 0.0254,
           "inches": 0.0254, "yard": 0.9144}
_WEIGHT = {"kg": 1, "g": 0.001, "mg": 1e-6, "ton": 1000, "lb": 0.453592,
           "lbs": 0.453592, "pound": 0.453592, "oz": 0.0283495}


def unit_convert(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    try:
        value = float(p.get("value", 0))
    except (TypeError, ValueError):
        return "Sir, give me a number to convert."
    frm = (p.get("from") or "").lower().strip()
    to = (p.get("to") or "").lower().strip()

    # Temperature (special formulas)
    temp = {"c", "celsius", "f", "fahrenheit", "k", "kelvin"}
    if frm in temp and to in temp:
        c = value
        if frm in ("f", "fahrenheit"): c = (value - 32) * 5 / 9
        elif frm in ("k", "kelvin"):   c = value - 273.15
        if to in ("f", "fahrenheit"):  out = c * 9 / 5 + 32
        elif to in ("k", "kelvin"):    out = c + 273.15
        else:                          out = c
        return f"{value}° {frm} = {out:.2f}° {to}."

    for table in (_LENGTH, _WEIGHT):
        if frm in table and to in table:
            out = value * table[frm] / table[to]
            return f"{value} {frm} = {out:,.4g} {to}."
    return f"Sir, I can't convert '{frm}' to '{to}'."


# ── Calculator (local, safe) ─────────────────────────────────────────────────
def calculate(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    expr = (p.get("expression") or p.get("query") or "").strip()
    if not expr:
        return "Sir, what should I calculate?"
    # Allow only digits, operators, parentheses, decimal points, spaces.
    if not re.fullmatch(r"[0-9+\-*/().%\s]+", expr):
        return "Sir, I can only compute basic arithmetic (+ - * / % and parentheses)."
    try:
        result = eval(expr, {"__builtins__": {}}, {})  # sandboxed: no names/builtins
        return f"{expr} = {result:,}"
    except Exception:
        return f"Sir, I couldn't evaluate: {expr}"


# ── Vietnamese lunar date (local) ────────────────────────────────────────────
def lunar_date(parameters: dict, player=None, session_memory=None) -> str:
    from datetime import date, datetime
    p = parameters or {}
    d_str = (p.get("date") or "").strip()
    try:
        d = datetime.strptime(d_str, "%Y-%m-%d").date() if d_str else date.today()
    except ValueError:
        d = date.today()
    try:
        from lunardate import LunarDate
        lu = LunarDate.fromSolarDate(d.year, d.month, d.day)
        return (f"Solar {d.isoformat()} is lunar {lu.day}/{lu.month}"
                f"{' (leap month)' if getattr(lu, 'isLeapMonth', False) else ''}, "
                f"year {lu.year} in the Vietnamese lunar calendar.")
    except Exception:
        return ("Sir, the lunar-calendar library isn't installed. "
                "Run: pip install lunardate")


# ── Day briefing (creative: combines weather + AQI + lunar + FX in one line) ──
def day_briefing(parameters: dict, player=None, session_memory=None) -> str:
    """A personal 'start of day' summary: date + lunar date, weather, air
    quality, and USD/VND — all for the user's current location, in one go."""
    from datetime import datetime
    bits = []
    now = datetime.now()
    bits.append(f"It's {now.strftime('%A, %B %d')}, {now.strftime('%H:%M')}.")

    # Lunar date
    try:
        from lunardate import LunarDate
        lu = LunarDate.fromSolarDate(now.year, now.month, now.day)
        bits.append(f"Lunar {lu.day}/{lu.month}.")
    except Exception:
        pass

    # Location + weather + AQI
    try:
        from actions.weather_report import current_location, _http_json as _wj, _VN_TZ
        loc = current_location()
        if loc:
            lat, lon, label = loc["lat"], loc["lon"], loc["label"]
            place = label.split(",")[0]
            try:
                w = _wj("https://api.open-meteo.com/v1/forecast?"
                        + urlencode({"latitude": lat, "longitude": lon,
                                     "current": "temperature_2m,precipitation",
                                     "daily": "precipitation_probability_max",
                                     "timezone": _VN_TZ, "forecast_days": 1}))
                t = w["current"]["temperature_2m"]
                rain_p = (w.get("daily", {}).get("precipitation_probability_max") or [0])[0]
                rain = f", {rain_p}% chance of rain" if rain_p else ""
                bits.append(f"In {place}: {round(t)}°C{rain}.")
            except Exception:
                pass
            try:
                a = _get("https://air-quality-api.open-meteo.com/v1/air-quality?"
                         + urlencode({"latitude": lat, "longitude": lon, "current": "us_aqi"}))
                aqi = a["current"].get("us_aqi")
                if aqi is not None:
                    q = ("good" if aqi <= 50 else "moderate" if aqi <= 100
                         else "unhealthy")
                    bits.append(f"Air quality {q} (AQI {aqi}).")
            except Exception:
                pass
    except Exception:
        pass

    # USD/VND
    try:
        fx = _get("https://api.exchangerate-api.com/v4/latest/USD")
        vnd = fx["rates"].get("VND")
        if vnd:
            bits.append(f"1 USD ≈ {vnd:,.0f} VND.")
    except Exception:
        pass

    msg = " ".join(bits)
    if player:
        try:
            player.write_log(f"Parker: {msg}")
        except Exception:
            pass
    return msg
