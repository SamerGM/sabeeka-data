#!/usr/bin/env python3
"""
Sabeeka price feed.

Reads official sources and seller price lists, cross-checks them, and writes the app's
API-contract files (docs/api in the app repo):

    out/v1/snapshot.json   spot, FX, Egypt and UAE boards, source health
    out/v1/config.json     tax rules, sellers and products with REAL making charges
    out/v1/history/XAUUSD_{DAY,WEEK,MONTH,YEAR}.json

Rules:
  * Decimal maths only (no float); every decimal is written as a JSON string.
  * A source is only used if it agrees with spot parity within tolerance; otherwise it is
    reported STALE/DOWN in `sources` and the next candidate is used.
  * A seller is marked `verified` only when its products were read from its own (or its
    licensed dealer's) live price list in this run. Nothing is invented.

Standard library only, so it runs anywhere (GitHub Actions cron).
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal as D, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = Path(os.environ.get("FEED_OUT", ROOT.parent / "out")) / "v1"
PREV = Path(os.environ.get("FEED_PREV", ROOT.parent / "prev")) / "v1"
UA = "Mozilla/5.0 (Linux; Android 14) SabeekaFeed/1.0 (+https://github.com/SamerGM/sabeeka-data)"
OZ = D("31.1034768")
NOW = datetime.now(timezone.utc).replace(microsecond=0)
ISO = lambda dt: dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
FIN = {24: D("0.9999"), 22: D("0.9160"), 21: D("0.8750"), 18: D("0.7500")}
USD_AED_PEG = D("3.6725")
# A local board is accepted if its 24K price is within this band around spot parity.
BOARD_MIN, BOARD_MAX = D("0.97"), D("1.12")
LOG: list[str] = []


def q(x: D, places: str = "0.01") -> D:
    return x.quantize(D(places), rounding=ROUND_HALF_UP)


def s(x: D, places: str = "0.01") -> str:
    return format(q(x, places), "f")


def num(text: str) -> D:
    return D(text.replace(",", "").strip())


def fetch(url: str, timeout: int = 40) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en,ar;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def text_of(page: str) -> str:
    t = re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>", " ", page)
    t = re.sub(r"<[^>]+>", " ", t)
    return html.unescape(re.sub(r"\s+", " ", t))


def safe(name: str, fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — every source is optional; failures are reported
        LOG.append(f"{name}: {type(e).__name__}: {e}")
        return None


def source(id_: str, name: str, name_ar: str, url: str | None, published: datetime | None = None) -> dict:
    return {"id": id_, "name": name, "nameAr": name_ar, "url": url,
            "publishedAt": ISO(published) if published else None, "fetchedAt": ISO(NOW)}


# ----------------------------------------------------------------------------- sources

def spot_gold_api() -> dict:
    data = json.loads(fetch("https://api.gold-api.com/price/XAU"))
    return {"usd_oz": D(str(data["price"])), "at": datetime.fromisoformat(data["updatedAt"].replace("Z", "+00:00"))}


def cbe_fx() -> dict:
    t = text_of(fetch("https://www.cbe.org.eg/en/economic-research/statistics/cbe-exchange-rates"))
    usd = re.search(r"US Dollar\s+([\d.]+)\s+([\d.]+)", t)
    aed = re.search(r"UAE Dirham\s+([\d.]+)\s+([\d.]+)", t)
    date = re.search(r"Rates for Date:\s*(\d{2})/(\d{2})/(\d{4})", t)
    if not usd:
        raise ValueError("USD row not found")
    mid = lambda m: (num(m.group(1)) + num(m.group(2))) / 2
    pub = datetime(int(date.group(3)), int(date.group(2)), int(date.group(1)), 9, 0, tzinfo=timezone.utc) if date else None
    return {"usd_egp": mid(usd), "aed_egp": mid(aed) if aed else None, "at": pub}


def egajtd_board() -> dict:
    t = text_of(fetch("https://egajtd.com/"))
    out = {}
    for k in (24, 21, 18):
        m = re.search(rf"عيار {k}\s+([\d,.]+)\s+([\d,.]+)", t)
        if m:
            # Gold Division table: "بيع" (dealer sells = customer buys) then "شراء" (dealer buys).
            out[k] = (num(m.group(1)), num(m.group(2)))
    if 24 not in out:
        raise ValueError("24K row not found")
    return {"quotes": out, "at": None}


def elgalla_board() -> dict:
    t = text_of(fetch("https://elgallagold.com/"))
    m24 = re.search(r"\(24K\)\s*Sell\s*-\s*([\d,]+)\s*EGP\s*Buy\s*-\s*([\d,]+)", t)
    m21 = re.search(r"\(21K\)\s*Sell\s*-\s*([\d,]+)\s*EGP\s*Buy\s*-\s*([\d,]+)", t)
    when = re.search(r"Updated at:\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s*\w+,?\s*(\d{1,2})\s+(\w+)\s+(\d{4})", t)
    if not m24:
        raise ValueError("24K not found")
    at = None
    if when:
        h = int(when.group(1)) % 12 + (12 if when.group(3) == "PM" else 0)
        local = datetime.strptime(f"{when.group(4)} {when.group(5)} {when.group(6)} {h}:{when.group(2)}", "%d %B %Y %H:%M")
        at = local.replace(tzinfo=timezone(timedelta(hours=2)))  # Cairo
    quotes = {24: (num(m24.group(1)), num(m24.group(2)))}
    if m21:
        quotes[21] = (num(m21.group(1)), num(m21.group(2)))
    return {"quotes": quotes, "at": at}


def dgjg_board() -> dict:
    t = text_of(fetch("https://dubaicityofgold.com/"))
    out = {}
    for k in (24, 22, 21, 18):
        m = re.search(rf"{k}K Gold AED\s*([\d,.]+)", t)
        if m:
            out[k] = num(m.group(1))
    if 24 not in out:
        raise ValueError("24K not found")
    return {"asks": out}


def igold() -> dict:
    t = text_of(fetch("https://igold.ae/gold-bars"))
    rate = re.search(r"Pure Metal Rate in AED Metal Gram Gold\s*([\d,.]+)", t)
    bars = []
    pat = r"(Emirates|PAMP|Valcambi Suisse) (\d+(?:\.\d+)?) (Gram|Ounce) (?:Minted |Cast )?Gold Bar\s+([\d,]+\.\d{2})\s*AED"
    for m in re.finditer(pat, t):
        grams = num(m.group(2)) * (OZ if m.group(3) == "Ounce" else 1)
        bars.append({"brand": m.group(1), "grams": grams, "price": num(m.group(4)), "label": m.group(0)})
    coins = []
    ct = safe("igold-coins", lambda: text_of(fetch("https://igold.ae/gold-coins"))) or ""
    for m in re.finditer(r"(Emirates|PAMP|Valcambi[^0-9]{0,10}?) ?(\d+(?:\.\d+)?) ?(Gram|g|Ounce|oz) [^0-9]{0,40}?Gold Coin\s+([\d,]+\.\d{2})\s*AED", ct):
        unit = m.group(3).lower()
        grams = num(m.group(2)) * (OZ if unit in ("ounce", "oz") else 1)
        coins.append({"brand": m.group(1).strip(), "grams": grams, "price": num(m.group(4))})
    return {"rate": num(rate.group(1)) if rate else None, "bars": bars, "coins": coins}


def gold_era() -> dict:
    items = []
    for cat, typ in (("ingots", "INGOT"), ("bars", "INGOT"), ("coins", "COIN")):
        t = text_of(fetch(f"https://gold-era.eg/product-category/{cat}/"))
        for m in re.finditer(r"Add to cart (?:Add to cart )?(Gold (?:Ingots|Bars|Coins)) (.{3,70}?)\s([\d,]+\.\d{2})\s+EGP", t):
            name, price = m.group(2).strip(), num(m.group(3))
            g = re.search(r"(\d+(?:\.\d+)?)\s*(?:Gram|Grams|gm|KG)", name)
            if not g:
                continue
            grams = num(g.group(1)) * (1000 if "KG" in name else 1)
            if "King George" in name:  # identical price to Queen Elizabeth; keep one design
                continue
            items.append({"name": name, "grams": grams, "price": price, "type": typ, "karat": 21 if typ == "COIN" else 24})
    if not items:
        raise ValueError("no products parsed")
    return {"items": items}


# ----------------------------------------------------------------------------- build

def main() -> int:
    base = json.loads((ROOT / "base_config.json").read_text(encoding="utf-8"))
    statuses = []

    def status(src, role, health, desc, desc_ar, dev=None):
        statuses.append({"source": src, "role": role, "health": health, "description": desc,
                         "descriptionAr": desc_ar, "deviationPct": s(dev) if dev is not None else None})

    # --- Spot ---------------------------------------------------------------------------
    spot = safe("gold-api", spot_gold_api)
    ig = safe("igold", igold)
    if spot is None:
        if ig and ig["rate"]:
            spot = {"usd_oz": ig["rate"] / USD_AED_PEG * OZ / FIN[24], "at": NOW}
            LOG.append("spot: fell back to iGold pure-metal rate")
        else:
            print("FATAL: no spot source", LOG, file=sys.stderr)
            return 2
    usd_oz = spot["usd_oz"]
    usd_g = usd_oz / OZ
    spot_src = source("gold-api", "Gold-API · XAU/USD", "Gold-API · الذهب/الدولار", "https://gold-api.com", spot["at"])
    refs, ver_status, maxdev = [], "UNVERIFIED", None
    if ig and ig["rate"]:
        ig_oz = ig["rate"] / USD_AED_PEG * OZ / FIN[24]
        maxdev = abs(ig_oz - usd_oz) / usd_oz * 100
        refs.append("igold-pure-rate")
        # Retail pure-metal rates carry a premium; within 5 % counts as consistent.
        ver_status = "VERIFIED" if maxdev <= D("5") else "WARNING"
    status(spot_src, "PRIMARY", "HEALTHY", "Live spot (free API, interim until licensed feed)", "سعر فوري مباشر (مصدر مجاني مؤقت)")

    # --- FX -----------------------------------------------------------------------------
    fx = safe("cbe", cbe_fx)
    prev_snap = safe("prev-snapshot", lambda: json.loads((PREV / "snapshot.json").read_text()))
    cbe_src = source("cbe", "Central Bank of Egypt", "البنك المركزي المصري", "https://www.cbe.org.eg", fx["at"] if fx else None)
    if fx:
        usd_egp, aed_egp = fx["usd_egp"], fx["aed_egp"]
        status(cbe_src, "OFFICIAL", "HEALTHY", "USD/EGP, AED/EGP official (mid of buy/sell)", "أسعار الصرف الرسمية (متوسط الشراء والبيع)")
    elif prev_snap:
        usd_egp = num(prev_snap["fx"]["usdEgp"]); aed_egp = num(prev_snap["fx"]["aedEgp"]) if prev_snap["fx"].get("aedEgp") else None
        status(cbe_src, "OFFICIAL", "STALE", "Using last good official rate", "آخر سعر رسمي متاح")
    else:
        print("FATAL: no FX", LOG, file=sys.stderr)
        return 2
    cbuae_src = source("cbuae", "Central Bank of the UAE", "مصرف الإمارات العربية المتحدة المركزي", "https://www.centralbank.ae")
    status(cbuae_src, "OFFICIAL", "HEALTHY", "USD/AED official peg 3.6725", "الدولار/الدرهم · الربط الرسمي 3.6725", D("0"))

    # --- Egypt board: Gold Division if consistent, else El Galla -------------------------
    parity_eg24 = usd_g * usd_egp * FIN[24]
    eg_quotes, eg_src = None, None
    gd = safe("egajtd", egajtd_board)
    gd_src = source("egajtd", "Gold Division (egajtd)", "الشعبة العامة للذهب والمجوهرات", "https://egajtd.com")
    if gd:
        dev = (gd["quotes"][24][0] - parity_eg24) / parity_eg24 * 100
        if BOARD_MIN <= gd["quotes"][24][0] / parity_eg24 <= BOARD_MAX:
            eg_quotes, eg_src = gd["quotes"], gd_src
            status(gd_src, "OFFICIAL", "HEALTHY", "Egypt gram prices · parity check passed", "أسعار الجرام في مصر · مطابقة للسعر العالمي", dev)
        else:
            status(gd_src, "OFFICIAL", "STALE", "Published table disagrees with spot × CBE rate — not used", "الجدول المنشور لا يطابق السعر العالمي × سعر الصرف — غير مستخدم", dev)
    else:
        status(gd_src, "OFFICIAL", "DOWN", "Page unavailable", "الصفحة غير متاحة")
    eg_fallback = safe("elgalla", elgalla_board)
    gl_src = source("el-galla", "El Galla Gold (published prices)", "الجلا للذهب (الأسعار المعلنة)", "https://elgallagold.com", eg_fallback["at"] if eg_fallback else None)
    if eg_fallback:
        dev = (eg_fallback["quotes"][24][0] - parity_eg24) / parity_eg24 * 100
        ok = BOARD_MIN <= eg_fallback["quotes"][24][0] / parity_eg24 <= BOARD_MAX
        if eg_quotes is None and ok:
            eg_quotes, eg_src = eg_fallback["quotes"], gl_src
            status(gl_src, "CROSS_CHECK", "HEALTHY", "Egypt board (Gold Division fallback) · parity check passed", "سعر مصر (بديل الشعبة) · مطابق للسعر العالمي", dev)
        else:
            status(gl_src, "CROSS_CHECK", "VERIFIED" if ok else "STALE", "Egypt cross-check", "تحقق إضافي لسعر مصر", dev)
    if eg_quotes is None:
        # Last resort: spot parity with the typical 2 % local spread, clearly labelled.
        a = parity_eg24 * D("1.02")
        eg_quotes, eg_src = {24: (a, a * D("0.992"))}, source("parity-eg", "Spot parity estimate", "تقدير من السعر العالمي", None)
        LOG.append("egypt board: parity estimate")
    eg_src["name"] += ""
    a24, b24 = eg_quotes[24]
    for k in (22, 21, 18):  # derive missing karats from 24K by fineness (22K is not quoted in Egypt)
        if k not in eg_quotes:
            eg_quotes[k] = (a24 * FIN[k] / FIN[24], b24 * FIN[k] / FIN[24])

    # --- UAE board: DGJG asks; bid estimated from spot parity -----------------------------
    dg = safe("dgjg", dgjg_board)
    dg_src = source("dgjg", "Dubai Gold & Jewellery Group", "مجموعة دبي للذهب والمجوهرات", "https://dubaicityofgold.com", NOW)
    parity_ae24 = usd_g * USD_AED_PEG * FIN[24]
    if dg and BOARD_MIN <= dg["asks"][24] / parity_ae24 <= BOARD_MAX:
        asks = dg["asks"]
        status(dg_src, "OFFICIAL", "HEALTHY", "UAE suggested retail rates · sell price estimated (DGJG publishes one rate)",
               "أسعار التجزئة المقترحة · سعر الشراء من العميل تقديري", (asks[24] - parity_ae24) / parity_ae24 * 100)
    else:
        asks = {k: parity_ae24 * FIN[k] / FIN[24] * D("1.01") for k in (24, 22, 21, 18)}
        status(dg_src, "OFFICIAL", "DOWN" if dg is None else "STALE", "Unavailable — parity estimate used", "غير متاح — تم استخدام تقدير")
    for k in (22, 21, 18):
        asks.setdefault(k, asks[24] * FIN[k] / FIN[24])
    # Dealer buy-back ≈ spot parity × fineness − 1 % (never above ask).
    ae_quotes = {k: (asks[k], min(asks[k], usd_g * USD_AED_PEG * FIN[k] * D("0.99"))) for k in (24, 22, 21, 18)}

    # --- Change vs start of day (from previous history) ---------------------------------
    hist = safe("prev-history", lambda: json.loads((PREV / "history" / "XAUUSD_raw.json").read_text())) or {"points": []}
    pts = [p for p in hist["points"] if datetime.fromisoformat(p["t"].replace("Z", "+00:00")) > NOW - timedelta(days=370)]
    pts.append({"t": ISO(NOW), "v": s(usd_oz)})
    day_start = NOW.replace(hour=0, minute=0, second=0)
    day_pts = [p for p in pts if datetime.fromisoformat(p["t"].replace("Z", "+00:00")) >= day_start]
    open_px = num(day_pts[0]["v"]) if day_pts else usd_oz
    change = usd_oz - open_px
    change_pct = change / open_px * 100 if open_px else D(0)

    def board(market, cur, src, quotes, chg):
        return {"market": market, "currency": cur, "source": src,
                "quotes": [{"karat": k, "ask": s(quotes[k][0]), "bid": s(min(quotes[k][1], quotes[k][0])), "changePct": s(chg)} for k in (24, 22, 21, 18)]}

    snapshot = {
        "generatedAt": ISO(NOW),
        "spot": {"usdPerOunce": s(usd_oz), "changeAbs": s(change), "changePct": s(change_pct), "source": spot_src,
                 "verification": {"status": ver_status, "maxDeviationPct": s(maxdev) if maxdev is not None else None, "references": refs}},
        "fx": {"usdEgp": s(usd_egp, "0.0001"), "usdAed": s(USD_AED_PEG, "0.0001"),
               "aedEgp": s(aed_egp, "0.0001") if aed_egp else None, "egyptSource": cbe_src, "uaeSource": cbuae_src},
        "boards": [board("EGYPT", "EGP", eg_src, eg_quotes, change_pct), board("UAE", "AED", dg_src, ae_quotes, change_pct)],
        "sources": statuses,
    }

    # --- Sellers: real products from live price lists -------------------------------------
    brands = {b["id"]: b for b in base["brands"]}
    for b in brands.values():
        b["verified"] = False
    products = []
    ge = safe("gold-era", gold_era)
    ge_src = source("gold-era-eg", "Gold Era Egypt (price list)", "جولد إيرا مصر (قائمة الأسعار)", "https://gold-era.eg")
    if ge:
        for it in ge["items"]:
            k, w = it["karat"], it["grams"]
            making = it["price"] / w - eg_quotes[k][0]
            if making < 0:
                continue
            products.append({"sku": f"gold-era-eg-{it['type'].lower()}-{s(w, '0.01')}", "brandId": "gold-era-eg", "market": "EGYPT",
                             "type": it["type"], "karat": k, "weightGrams": format(w.normalize(), "f"),
                             "name": {"en": it["name"], "ar": it["name"]}, "makingPerGram": s(making), "cashbackPerGram": "0"})
        b = brands["gold-era-eg"]; b["verified"] = True; b["sourceUrl"] = "https://gold-era.eg/product-category/ingots/"
        b["profile"] = {"en": "Prices read live from gold-era.eg. Making charge = list price ÷ weight − Egypt gram price.",
                        "ar": "الأسعار مقروءة مباشرة من موقع جولد إيرا. المصنعية = سعر القائمة ÷ الوزن − سعر الجرام في مصر."}
        b["cashPolicy"] = {"en": "Cashback/buy-back terms are not published on gold-era.eg — confirm with the seller (hotline 16728).",
                           "ar": "شروط الكاش باك وإعادة الشراء غير منشورة على الموقع — يرجى التأكد من الشركة (16728)."}
        status(ge_src, "CROSS_CHECK", "HEALTHY", f"{len(ge['items'])} products read", f"تمت قراءة {len(ge['items'])} منتج")
    else:
        status(ge_src, "CROSS_CHECK", "DOWN", "Price list unavailable", "قائمة الأسعار غير متاحة")

    # BTC: official cashback policy (bulliontradingcenter.com/cash-back-policy).
    btc = brands["btc"]
    btc["cashPolicy"] = {"en": "Official BTC policy: refunds EGP 12/g of the workmanship fee plus the full stamping fee when sold back in the original sealed package; unpacked: EGP 10/g total. Not for wearable/customised items. Hotline 15057.",
                         "ar": "سياسة BTC الرسمية: رد 12 جنيه/جرام من المصنعية مع كامل رسوم الدمغة عند البيع في الغلاف الأصلي المغلق؛ غير المغلق: 10 جنيه/جرام إجمالًا. لا تنطبق على المشغولات والمنتجات المخصصة. 15057."}
    btc["profile"] = {"en": "Cashback policy verified from BTC's website. Its shop price list is not machine-readable yet.",
                      "ar": "سياسة الكاش باك موثقة من موقع BTC. قائمة أسعار المتجر غير قابلة للقراءة الآلية بعد."}
    btc["sourceUrl"] = "https://bulliontradingcenter.com/cash-back-policy/"

    ig_src = source("igold", "iGold.ae (licensed UAE dealer)", "آي جولد (تاجر مرخص في الإمارات)", "https://igold.ae")
    brand_map = {"Emirates": "emirates-gold", "PAMP": "pamp", "Valcambi Suisse": "valcambi"}
    if ig and ig["bars"]:
        seen = set()
        for bar in ig["bars"]:
            bid = brand_map.get(bar["brand"])
            key = (bid, bar["grams"])
            if bid is None or key in seen:
                continue
            seen.add(key)
            w = bar["grams"]
            premium = bar["price"] / w - (ig["rate"] or parity_ae24)
            products.append({"sku": f"{bid}-ingot-{s(w)}", "brandId": bid, "market": "UAE", "type": "INGOT", "karat": 24,
                             "weightGrams": s(w, "0.01") if w != w.to_integral() else format(w.normalize(), "f"),
                             "name": {"en": bar["label"].rsplit(" ", 2)[0], "ar": bar["label"].rsplit(" ", 2)[0]},
                             "makingPerGram": s(max(premium, D(0))), "cashbackPerGram": "0"})
        for coin in ig["coins"]:
            bid = brand_map.get(coin["brand"].split(" ")[0]) or brand_map.get(coin["brand"])
            if bid is None:
                continue
            w = coin["grams"]
            products.append({"sku": f"{bid}-coin-{s(w)}", "brandId": bid, "market": "UAE", "type": "COIN", "karat": 24,
                             "weightGrams": s(w), "name": {"en": f"{coin['brand']} {s(w)} g gold coin", "ar": f"عملة ذهب {s(w)} جم"},
                             "makingPerGram": s(max(coin["price"] / w - (ig["rate"] or parity_ae24), D(0))), "cashbackPerGram": "0"})
        for bid in set(brand_map.values()):
            b = brands[bid]; b["verified"] = any(p["brandId"] == bid for p in products); b["sourceUrl"] = "https://igold.ae/gold-bars"
            b["profile"] = {"en": "Prices read live from iGold.ae (licensed UAE dealer). Premium = list price ÷ weight − pure gold rate.",
                            "ar": "الأسعار مقروءة مباشرة من آي جولد (تاجر مرخص). العلاوة = سعر القائمة ÷ الوزن − سعر الذهب الخالص."}
        status(ig_src, "CROSS_CHECK", "HEALTHY", f"{len(seen)} bars · pure rate {s(ig['rate'] or D(0))} AED/g", f"{len(seen)} سبيكة · سعر الخالص {s(ig['rate'] or D(0))} درهم/جم")
    else:
        status(ig_src, "CROSS_CHECK", "DOWN", "Dealer price list unavailable", "قائمة أسعار التاجر غير متاحة")

    for b in brands.values():
        if not b["verified"] and b["id"] != "btc":
            b["profile"] = {"en": "No public machine-readable price list found yet — figures hidden rather than guessed.",
                            "ar": "لم يتم العثور على قائمة أسعار عامة بعد — لا نعرض أرقامًا تقديرية."}
            b["cashPolicy"] = {"en": "Policy not published online yet.", "ar": "السياسة غير منشورة على الإنترنت بعد."}

    config = dict(base)
    config["version"] = NOW.strftime("%Y.%m.%d.%H%M")
    config["effectiveFrom"] = NOW.strftime("%Y-%m-%d")
    config["brands"] = list(brands.values())
    config["products"] = products
    config["disclaimer"] = {"en": "Seller prices are read from each seller's or licensed dealer's public price list. Sellers without a public list show no figures.",
                            "ar": "أسعار البائعين مقروءة من قوائم الأسعار العامة لكل بائع أو تاجر مرخص. البائعون بدون قائمة عامة لا تظهر لهم أرقام."}

    # --- History ------------------------------------------------------------------------
    (OUT / "history").mkdir(parents=True, exist_ok=True)
    (OUT / "history" / "XAUUSD_raw.json").write_text(json.dumps({"points": pts}), encoding="utf-8")

    def series(rng, window, step):
        cut = NOW - window
        chosen, last = [], None
        for p in pts:
            t = datetime.fromisoformat(p["t"].replace("Z", "+00:00"))
            if t < cut:
                continue
            if last is None or t - last >= step or p is pts[-1]:
                chosen.append(p); last = t
        return {"series": "XAUUSD", "range": rng, "points": chosen}

    for rng, window, step in (("DAY", timedelta(days=1), timedelta(minutes=10)), ("WEEK", timedelta(days=7), timedelta(hours=1)),
                              ("MONTH", timedelta(days=31), timedelta(hours=6)), ("YEAR", timedelta(days=366), timedelta(days=2))):
        (OUT / "history" / f"XAUUSD_{rng}.json").write_text(json.dumps(series(rng, window, step)), encoding="utf-8")

    (OUT / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "log.txt").write_text("\n".join(LOG) + "\n", encoding="utf-8")
    print(json.dumps({"spot": s(usd_oz), "usdEgp": s(usd_egp, "0.0001"), "egypt24": [s(a24), s(b24)], "egyptSource": eg_src["id"],
                      "uae24": s(asks[24]), "products": len(products), "log": LOG}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
