"""
market_vn.py — Vietnam market prices for Parker.

  - gold_price  : SJC gold prices from SJC's official JSON service
  - fuel_price  : Vietnam petrol/diesel prices (Petrolimex, national base price)

Free, no API key. Petrol prices are scraped from a public page, so that part is
best-effort and may break if the site changes.
"""

import json
import re
import urllib.request

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Parker/1.0"}


def _get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


# ── Gold (SJC official JSON) ─────────────────────────────────────────────────
def gold_price(parameters: dict = None, player=None, session_memory=None) -> str:
    """Current SJC gold buy/sell prices (thousand VND per lượng)."""
    try:
        data = json.loads(_get("https://sjc.com.vn/GoldPrice/Services/PriceService.ashx"))
    except Exception as e:
        return f"Sir, I couldn't fetch the gold price: {e}"

    rows = data.get("data", [])
    if not rows:
        return "Sir, no gold price data is available right now."
    when = data.get("latestDate", "")

    lines = [f"SJC gold prices ({when}) — thousand VND per lượng:"]
    # Show the main SJC bar first, then a couple of others.
    shown = 0
    for r in rows:
        name = r.get("TypeName", "")
        buy = r.get("Buy") or r.get("BuyValue")
        sell = r.get("Sell") or r.get("SellValue")
        if not (buy and sell):
            continue
        lines.append(f"  - {name}: buy {buy}, sell {sell}")
        shown += 1
        if shown >= 4:
            break
    msg = "\n".join(lines)
    if player:
        try:
            player.write_log(f"Parker: {msg}")
        except Exception:
            pass
    return msg


# ── Fuel (Petrolimex national price) ─────────────────────────────────────────
def fuel_price(parameters: dict = None, player=None, session_memory=None) -> str:
    """Current Vietnam petrol/diesel prices (Petrolimex, VND per litre).

    The homepage's fuel widget lists product names then their prices in the same
    order, so we pair them positionally.
    """
    try:
        html = _get("https://www.petrolimex.com.vn/")
    except Exception as e:
        return f"Sir, I couldn't fetch fuel prices: {e}"

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)

    # Product names as listed in the widget, in order.
    products = re.findall(
        r"(Xăng\s*E10\s*RON\s*95-V|Xăng\s*E10\s*RON\s*95-III|"
        r"Xăng\s*E5\s*RON\s*92-II|DO\s*0,001S-V|DO\s*0,05S-II|Dầu hỏa[^,\s]*)",
        text)
    # Price tokens like 27.327 (VND, tens of thousands) that follow the names.
    prices = re.findall(r"\b([12]\d\.\d{3})\b", text)

    # RON 95 in Vietnam is ~20,000–27,000 VND/l; pick the plausible fuel prices.
    fuel_prices = [p for p in dict.fromkeys(prices)
                   if 15.0 <= float(p.replace(".", "")) / 1000 <= 40]
    if fuel_prices:
        msg = ("Current Vietnam fuel prices (Petrolimex, VND/litre): "
               + ", ".join(fuel_prices[:4])
               + ". (These are the listed petrol/diesel prices; ask me to search "
               "for the exact product breakdown if you need it.)")
    else:
        return ("Sir, I couldn't read the current fuel prices from Petrolimex "
                "right now — I can web-search them instead.")

    if player:
        try:
            player.write_log(f"Parker: {msg}")
        except Exception:
            pass
    return msg
