#!/usr/bin/env python3
"""Identity and reorder links for genuine Bambu filament.

The printer tells us almost everything about an RFID spool:
  tray_sub_brands -> the product line, e.g. "PLA Basic"
  tray_info_idx   -> Bambu's SKU,     e.g. "GFA00"
  tray_id_name    -> product+colour code, e.g. "A00-W01"
  tray_color      -> the exact hex
...but never the marketing colour NAME ("Jade White"), which is the one thing
you need when reordering. That has to be looked up.

COLOR_NAMES is deliberately small: it contains only codes confirmed against real
RFID reports. An unknown code is shown as its raw code plus the hex swatch rather
than guessed at - a confidently wrong colour name is worse than none when the
point is to buy the right spool again. Extend it without touching this file via
`filament.color_names`, under Settings > Filament.

Nothing here is authoritative about Bambu's catalogue; the store link is a
*search*, not a product deep-link, so it keeps working when the catalogue changes.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote_plus

# tray_id_name -> Bambu's own colour name.
#   [invoice] = read off a real Bambu order PDF, where the SKU prefix IS the
#               tray_id_name the AMS reports - these are certain.
#   [assumed] = not yet seen on an invoice. Correct them in filament.color_names
#               if the store disagrees.
COLOR_NAMES = {
    "A00-W01": "Jade White",      # PLA Basic     [assumed]
    "A00-K00": "Black",           # PLA Basic     [assumed]
    "A19-K00": "Absolute Black",  # PLA Pure      [invoice]
    "G00-G01": "Pine Green",      # PETG Basic    [invoice]
    "A01-R4": "Dunkelrot",        # PLA Matte     [invoice]
    "A01-G0": "Apfelgrün",        # PLA Matte     [invoice]
    "A01-P3": "Sakura-Pink",      # PLA Matte     [invoice]
    "A01-B6": "Dunkelblau",       # PLA Matte     [invoice]
}

# Bambu runs one Shopify storefront per region; search paths are identical.
STORE_HOSTS = {
    "eu": "eu.store.bambulab.com",
    "us": "us.store.bambulab.com",
    "uk": "uk.store.bambulab.com",
    "jp": "jp.store.bambulab.com",
    "global": "store.bambulab.com",
}
DEFAULT_REGION = "eu"


def norm_color(c: str | None) -> str | None:
    """'#FFFFFFFF' / 'ffffff' -> 'FFFFFF'. The AMS sends '#RRGGBB', the cloud
    sends 'RRGGBB' (sometimes with alpha); both must key the same filament."""
    if not c:
        return None
    h = str(c).lstrip("#").strip().upper()
    return h[:6] if len(h) >= 6 else None


def key(filament_id: str | None, color: str | None, ftype: str | None = None) -> str:
    """Stable identity for one filament: SKU + colour.

    The only key the live AMS and the cloud's per-print detail both carry, which
    is what lets the history page work retroactively. Not per physical spool -
    two spools of the same PLA Basic Jade White are deliberately one entry.
    """
    return f"{(filament_id or ftype or '?').strip().upper()}|{norm_color(color) or '?'}"


def color_name(code: str | None, overrides: dict | None = None) -> str | None:
    """Marketing colour name for a tray_id_name code, or None if unknown.

    Both sides are compared in canonical form, so a name learned from an invoice
    written 'A00-W1' still names the spool the AMS calls 'A00-W01'. Overrides win,
    which is how an imported invoice's German name replaces a built-in English one.
    """
    if not code:
        return None
    n = norm_code(code)
    for table in (overrides or {}, COLOR_NAMES):
        for k in (code, n):
            if k and k in table:
                return table[k] or None
        # the table's own keys may be written either way round
        for k, v in table.items():
            if norm_code(k) == n:
                return v or None
    return None


def store_url(*terms: str | None, region: str | None = None, host: str | None = None) -> str:
    """Search URL on the regional Bambu store for the given words.

    A search rather than /products/<handle>: handles are not something this app
    can verify, and a 404 at the moment you want to reorder is worse than one
    extra click.
    """
    site = host or STORE_HOSTS.get((region or DEFAULT_REGION).lower(), STORE_HOSTS[DEFAULT_REGION])
    q = " ".join(str(t).strip() for t in terms if t and str(t).strip())
    return f"https://{site}/search?q={quote_plus(q)}"


def describe(tray: dict, overrides: dict | None = None, region: str | None = None,
             host: str | None = None) -> dict:
    """Extra display fields for one tray. Empty for anything that is not a
    genuine Bambu spool - a third-party spool reports a Bambu SKU whenever it was
    sliced with a Bambu profile, and linking that to the store would be wrong."""
    if not tray.get("is_bambu"):
        return {}
    name = color_name(tray.get("code"), overrides)
    # "PLA Basic" is the sellable product; fall back to the bare material so the
    # search still lands somewhere useful if the line is missing.
    line = tray.get("brand") or tray.get("type")
    return {
        "color_name": name,
        # the colour name narrows the search; without it the product page is
        # still the right destination and the colour is picked there
        "store_url": store_url(line, name, region=region, host=host),
    }


def norm_code(code: str | None) -> str | None:
    """Canonical form of a colour code: 'A00-W01' and 'A00-W1' are one colour.

    The AMS pads the colour part to two digits (`tray_id_name`), the store's SKU
    does not always - one real invoice carries both 'A19-K00' and 'A01-R4'. Only
    the numeric part is normalised, so W1 and W2 stay distinct.
    """
    c = (code or "").strip().upper()
    m = re.fullmatch(r"([A-Z]\d{2})-([A-Z]+)(\d+)", c)
    return f"{m.group(1)}-{m.group(2)}{int(m.group(3))}" if m else (c or None)


def sku_from_code(code: str | None) -> str | None:
    """'A19-K00' -> 'GFA19'.

    An invoice names a filament by colour code, the printer by SKU
    (`tray_info_idx`), and the two encode the same product line: GF + the code's
    letter + its two digits. Confirmed on three independent pairs - PLA Pure
    A19-K00/GFA19, PLA Basic A00-W01/GFA00, PETG Basic G00-G01/GFG00.

    This is what lets an imported order find filaments that were used up before
    the AMS was ever observed: their print rows carry the SKU but no colour code.
    """
    m = re.match(r"^([A-Z])(\d{2})\b", (code or "").strip().upper())
    return f"GF{m.group(1)}{m.group(2)}" if m else None


def match_key(product: str | None, color_name: str | None) -> str | None:
    """Loose join key between a purchase line and a filament identity.

    A store receipt has no SKU and no hex - only words - so purchases are matched
    on 'product line + colour name', case- and spacing-insensitive.
    """
    p = re.sub(r"\s+", " ", (product or "").strip().lower())
    c = re.sub(r"\s+", " ", (color_name or "").strip().lower())
    return f"{p}|{c}" if (p or c) else None


# ---------------------------------------------------------------------------
# Order-text parsing
#
# Best-effort ONLY. Bambu's confirmation-email layout is not something this file
# can verify, and it changes; every parsed line is therefore returned as a
# *suggestion* for the user to correct before anything is stored. The parser
# never invents a price or a quantity - a field it cannot read comes back None
# so the UI can flag it rather than silently guess.
# ---------------------------------------------------------------------------

# The material families Bambu sells, longest first so "PLA-CF" wins over "PLA".
_MATERIALS = ["PAHT", "PPA", "PPS", "PETG", "PLA", "ABS", "ASA", "TPU", "PVA",
              "PET", "PC", "PA"]
# Optional product-line words that follow the material, e.g. "PLA Basic".
_LINES = ["Basic", "Matte", "Silk", "Tough", "Pure", "Aero", "Glow", "Sparkle",
          "Marble", "Galaxy", "Wood", "Metal", "Translucent", "Support", "Flex",
          "Lite", "Plus", "Max", "HF", "CF", "GF", "Gradient", "Dual"]
_ACRONYMS = {"HF", "CF", "GF"}
_PRODUCT_RE = re.compile(
    r"\b((?:%s)(?:[-‑][A-Z]{2})?(?:\s+(?:%s))*)\b" % ("|".join(_MATERIALS), "|".join(_LINES)),
    re.I)
_QTY_RE = re.compile(r"(?:(?:^|\s)(?:x|×|qty|menge|anzahl)[:. ]*(\d{1,3})\b)"
                     r"|(?:\b(\d{1,3})\s*(?:x|×)(?:\s|$))", re.I)
# Two forms, tried in this order and never mixed: a trailing-symbol pattern would
# otherwise read "×1  € 27,99" as the number 1 followed by "€" and report a
# price of 1.00, swallowing the symbol that belongs to the real amount.
# (?!\w) rather than \b to close the group: "€" is not a word character, so a
# trailing \b never matches a line that ends in "49,98 €".
_PRICE_SYM_FIRST = re.compile(r"(€|EUR|\$|USD|£|GBP|¥|JPY)\s*(\d[\d.,]*)", re.I)
_PRICE_SYM_LAST = re.compile(r"(\d[\d.,]*)\s*(€|EUR|\$|USD|£|GBP|¥|JPY)(?!\w)", re.I)
_PRICE_ANY = re.compile(r"(?:€|EUR|\$|USD|£|GBP|¥|JPY)\s*\d[\d.,]*"
                        r"|\d[\d.,]*\s*(?:€|EUR|\$|USD|£|GBP|¥|JPY)(?!\w)", re.I)
_WEIGHT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|g)\b", re.I)
# The reference must sit on the SAME line as the keyword ([^\S\n], never \s) and
# must contain a digit - with re.I a bare [A-Z0-9] class happily matches the next
# line's ordinary words and would capture "Bestellung" itself.
_ORDER_RE = re.compile(
    r"(?:order\s*(?:no\.?|number)?|bestellung|bestellnummer)"
    r"[^\S\n]*[:#]?[^\S\n]*((?=[A-Za-z0-9\-]*\d)[A-Za-z0-9\-]{4,})", re.I)
_CURRENCY = {"EUR": "€", "€": "€", "USD": "$", "$": "$", "GBP": "£", "£": "£",
             "JPY": "¥", "¥": "¥"}
_DATE_FORMATS = ["%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y",
                 "%B %d, %Y", "%d %B %Y", "%b %d, %Y", "%d %b %Y"]
_NOISE_RE = re.compile(
    r"\b(filament|refill|spool|spule|with\s+spool|mit\s+spule|nachfüll\w*|"
    r"bambu\s*lab|bambulab|\d+\s*(?:kg|g)|1\.75\s*mm|1,75\s*mm)\b", re.I)


def _amount(txt: str) -> float | None:
    """'49,98' / '1.234,56' / '1,234.56' -> float. Decides which separator is
    the decimal one by whichever comes last."""
    t = txt.strip()
    if not t:
        return None
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") \
            else t.replace(",", "")
    elif "," in t:
        # a comma with exactly two digits after it is a decimal comma
        t = t.replace(",", ".") if re.search(r",\d{1,2}$", t) else t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


def _find_price(line: str) -> tuple:
    """(amount, currency) from one line, or (None, None).

    Symbol-before-number wins outright; only if the line has none of those is the
    number-before-symbol form considered. Within the chosen form the LAST match is
    taken, which on a typical receipt row is the line total rather than the unit
    price.
    """
    for rx, sym_first in ((_PRICE_SYM_FIRST, True), (_PRICE_SYM_LAST, False)):
        hits = list(rx.finditer(line))
        if not hits:
            continue
        m = hits[-1]
        sym, amt = (m.group(1), m.group(2)) if sym_first else (m.group(2), m.group(1))
        val = _amount(amt)
        if val is not None:
            return val, _CURRENCY.get((sym or "").upper(), sym)
    return None, None


def _find_date(text: str) -> float | None:
    for m in re.finditer(r"\b(\d{1,2}[./]\d{1,2}[./]\d{2,4}|\d{4}-\d{2}-\d{2}"
                         r"|[A-Za-zäöü]{3,9}\s+\d{1,2},\s*\d{4}"
                         r"|\d{1,2}\.?\s+[A-Za-zäöü]{3,9}\s+\d{4})\b", text):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(m.group(1).strip(), fmt).timestamp()
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# Bambu invoice PDF
#
# Their invoice is a fixed table, and it carries more than any pasted email can:
#
#   PLA Pure                                  <- product line
#   SKU: A19-K00-1.75-1000-SPL                <- A19-K00 IS the AMS tray_id_name
#   Variant: Absolute Black (17101) /         <- the colour name the printer
#   / Filament with spool / 1kg                  never reports
#   1 €27.99 €12.60 DE MwSt(19%) €2.46 €15.39 <- qty, list, discount, …, PAID
#
# So an imported invoice can be matched to a filament by code rather than by
# wording, and it teaches the app colour names it had no way to know.
# ---------------------------------------------------------------------------

# A19-K00-1.75-1000-SPL / A01-R4-1.75-1000-SPLFREE -> code, grams, packaging
_SKU_RE = re.compile(r"^([A-Z]\d{2}-[A-Z0-9]+)-(\d(?:\.\d+)?)-(\d{3,4})-(SPLFREE|SPL|\w+)$", re.I)
_SKU_LINE_RE = re.compile(r"^SKU:\s*(\S+)\s*$", re.I)
_VARIANT_RE = re.compile(r"^Variant:\s*(.+)$", re.I)
# the amounts row: quantity, then one or more money columns, the LAST of which
# is "Items SubTotal" - the amount actually charged for the line
_AMOUNTS_RE = re.compile(r"^\s*(\d{1,3})\s+[€$£¥]")
_MONEY_RE = re.compile(r"[€$£¥]\s*(\d[\d.,]*)")
_INVOICE_ORDER_RE = re.compile(r"^\s*Order\s*Number\s*:\s*(\S+)", re.I | re.M)
_INVOICE_DATE_RE = re.compile(r"^\s*(?:Invoice|Order)\s*Date\s*:\s*(\d{4}-\d{2}-\d{2})", re.I | re.M)


def _unwrap(lines: list) -> list:
    """Re-join the SKU that the PDF broke across two lines:
    'SKU: A01-R4-1.75-1000-' + 'SPLFREE'."""
    out = []
    for ln in lines:
        if out and out[-1].endswith("-") and re.fullmatch(r"[A-Z0-9]+", ln):
            out[-1] += ln
        else:
            out.append(ln)
    return out


def looks_like_invoice(text: str) -> bool:
    return bool(_SKU_LINE_RE.search(text or "")) or "Items SubTotal" in (text or "")


def parse_bambu_invoice(text: str) -> dict:
    """Parse the line items out of a Bambu Lab invoice PDF's text.

    Non-filament items (build plates, spools, tools) are skipped: their SKUs
    carry no '-1.75-' diameter field.
    """
    lines = _unwrap([ln.strip() for ln in (text or "").splitlines() if ln.strip()])
    om = _INVOICE_ORDER_RE.search(text or "")
    dm = _INVOICE_DATE_RE.search(text or "")
    ordered_at = None
    if dm:
        try:
            ordered_at = datetime.strptime(dm.group(1), "%Y-%m-%d").timestamp()
        except ValueError:
            ordered_at = None

    items, currency = [], None
    for i, ln in enumerate(lines):
        sm = _SKU_LINE_RE.match(ln)
        if not sm:
            continue
        km = _SKU_RE.match(sm.group(1))
        if not km:
            continue                      # accessory, not filament
        code, _dia, grams, pack = km.group(1).upper(), km.group(2), km.group(3), km.group(4).upper()
        product = lines[i - 1] if i else ""
        if not product or product.lower().startswith(("sku:", "variant:")):
            product = ""

        color = None
        amounts = None
        # the block runs from the SKU to its amounts row; cap the scan so a
        # malformed block can't swallow the address that follows the table
        for ln2 in lines[i + 1:i + 7]:
            if amounts is None and _AMOUNTS_RE.match(ln2):
                amounts = ln2
                break
            vm = _VARIANT_RE.match(ln2)
            if vm and color is None:
                # "Absolute Black (17101) /" / "Pine Green(30503) /" -> the name
                color = re.split(r"[(/]", vm.group(1))[0].strip(" /·-") or None
        qty = paid = listp = None
        if amounts:
            qty = int(_AMOUNTS_RE.match(amounts).group(1))
            money = _MONEY_RE.findall(amounts)
            if money:
                # first column is the unit LIST price, last is the line's
                # "Items SubTotal" - what was actually charged after discount
                listp = _amount(money[0])
                paid = _amount(money[-1])
            if currency is None:
                cm = re.search(r"[€$£¥]", amounts)
                currency = cm.group(0) if cm else None

        items.append({
            "product": product or None,
            "color_name": color,
            "code": code,
            "type": (product.split() or [""])[0].upper() or None,
            "spools": qty,
            "grams_each": float(grams),
            "total_price": paid,
            "list_price": listp,      # per unit, before any discount
            "currency": currency,
            "refill": pack == "SPLFREE",
        })

    return {"order_ref": om.group(1) if om else None, "ordered_at": ordered_at,
            "currency": currency, "lines": items, "source": "invoice"}


def parse_order(text: str) -> dict:
    """Pull candidate order lines out of a pasted confirmation.

    Returns {order_ref, ordered_at, currency, lines:[…]}. Each line carries what
    could be read and None for what could not - nothing is stored until the user
    confirms it.
    """
    text = text or ""
    if looks_like_invoice(text):     # the precise path, when the layout allows it
        return parse_bambu_invoice(text)
    om = _ORDER_RE.search(text)
    order_ref = om.group(1) if om else None
    ordered_at = _find_date(text)

    lines, seen = [], set()
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) < 4:
            continue
        pm = _PRODUCT_RE.search(line)
        if not pm:
            continue
        product = re.sub(r"\s+", " ", pm.group(1)).strip()
        # "pla basic" -> "PLA Basic", but the two-letter grades stay shouted: HF/CF/GF
        parts = product.split()
        product = " ".join([parts[0].upper()] +
                           [p.upper() if p.upper() in _ACRONYMS else p.capitalize()
                            for p in parts[1:]])

        price, currency = _find_price(line)
        qm = _QTY_RE.search(line)
        qty = int(qm.group(1) or qm.group(2)) if qm else None

        grams = None
        wm = _WEIGHT_RE.search(line)
        if wm:
            g = _amount(wm.group(1))
            if g:
                grams = g * 1000 if wm.group(2).lower() == "kg" else g

        # everything after the product phrase, minus prices/qty/units, is the colour
        tail = line[pm.end():]
        tail = _PRICE_ANY.sub(" ", tail)
        tail = _QTY_RE.sub(" ", tail)
        tail = _NOISE_RE.sub(" ", tail)
        tail = re.sub(r"[·|,;:\-–—()\[\]/]+", " ", tail)
        # whatever survives that is words; bare numbers are leftovers, not a colour
        tail = " ".join(w for w in tail.split() if not re.fullmatch(r"[\d.,]+", w))
        color = re.sub(r"\s+", " ", tail).strip(" .·-") or None
        if color and (len(color) > 32 or not re.search(r"[A-Za-zÄÖÜäöü]", color)):
            color = None

        key = (product, (color or "").lower())
        if key in seen:
            continue
        seen.add(key)
        lines.append({
            "product": product, "color_name": color,
            "type": parts[0].upper().split("-")[0],
            "spools": qty, "grams_each": grams,
            "total_price": price, "currency": currency,
        })
    return {"order_ref": order_ref, "ordered_at": ordered_at,
            "currency": next((l["currency"] for l in lines if l["currency"]), None),
            "lines": lines, "source": "text"}


if __name__ == "__main__":  # quick self-check
    t = {"is_bambu": True, "brand": "PLA Basic", "type": "PLA", "code": "A00-W01"}
    assert describe(t)["color_name"] == "Jade White"
    assert "PLA+Basic+Jade+White" in describe(t)["store_url"]
    assert describe(t, region="us")["store_url"].startswith("https://us.store.bambulab.com/")
    assert describe({"is_bambu": False, "code": "A00-W01"}) == {}
    assert describe({"is_bambu": True, "brand": "PLA CF", "code": "X99-Z99"})["color_name"] is None
    assert color_name("A00-W01", {"A00-W01": "Snow White"}) == "Snow White"
    # the AMS ('#FFFFFF') and the cloud ('FFFFFFFF') must key the same filament
    assert key("GFA00", "#FFFFFF") == key("GFA00", "FFFFFFFF") == "GFA00|FFFFFF"
    assert key(None, None, "PLA") == "PLA|?"
    assert match_key("PLA Basic", " Jade  White ") == "pla basic|jade white"
    # invoice colour code -> the SKU the printer reports (all three verified)
    assert sku_from_code("A19-K00") == "GFA19"
    assert sku_from_code("A00-W01") == "GFA00"
    assert sku_from_code("G00-G01") == "GFG00"
    assert sku_from_code("A01-R4") == "GFA01"      # short refill suffix
    assert sku_from_code(None) is None and sku_from_code("RSP001") is None
    # padded and unpadded colour suffixes are the same colour; W1 != W2
    assert norm_code("A00-W01") == norm_code("A00-W1") == "A00-W1"
    assert norm_code("A19-K00") == "A19-K0" and norm_code("A01-R4") == "A01-R4"
    assert norm_code("A00-W01") != norm_code("A00-W02")
    assert norm_code(None) is None and norm_code("RSP001") == "RSP001"
    # a name learned under the store's spelling must reach the AMS's spelling,
    # and must beat the built-in English guess
    assert color_name("A00-W01") == "Jade White"
    assert color_name("A00-W01", {"A00-W1": "Jade Weiß"}) == "Jade Weiß"
    assert color_name("A00-W1", {"A00-W01": "Jade Weiß"}) == "Jade Weiß"

    # order parsing - several plausible receipt layouts, none of them verified
    # against a real Bambu email, which is exactly why the UI asks for confirmation
    o = parse_order("""
        Vielen Dank fuer Ihre Bestellung!
        Bestellung #EU12345678 vom 04.06.2026
        Bambu PLA Basic Filament - Jade White (1 kg)   x 2      49,98 €
        PETG HF, Black, 1 kg  ×1  € 27,99
        Shipping                                                 4,99 €
    """)
    assert o["order_ref"] == "EU12345678", o["order_ref"]
    assert o["ordered_at"] and datetime.fromtimestamp(o["ordered_at"]).year == 2026
    got = [(l["product"], l["color_name"], l["spools"], l["total_price"],
            l["grams_each"]) for l in o["lines"]]
    assert got == [("PLA Basic", "Jade White", 2, 49.98, 1000.0),
                   ("PETG HF", "Black", 1, 27.99, 1000.0)], got
    assert o["currency"] == "€"
    # US layout, dot decimals, no explicit weight
    o2 = parse_order("Order # US-9911  June 4, 2026\nPLA Matte Charcoal  Qty: 3  $74.97")
    l2 = o2["lines"][0]
    assert (l2["product"], l2["color_name"], l2["spools"], l2["total_price"],
            l2["currency"]) == ("PLA Matte", "Charcoal", 3, 74.97, "$"), l2
    # a line with no filament in it must not become an order line
    assert parse_order("Subtotal 54,97 €\nVAT included")["lines"] == []

    # --- Bambu invoice PDF ---------------------------------------------------
    # Verbatim item rows as pypdf extracts them (personal details omitted on
    # purpose - the parser never looks at the address block anyway). Covers the
    # wrapped SKU, a missing product-discount column, and two accessories that
    # must NOT be imported as filament.
    INV = """Items Qty Price Product discount Tax Tax amount Items SubTotal
PLA Pure
SKU: A19-K00-1.75-1000-SPL
Variant: Absolute Black (17101)
/ Filament with spool / 1kg
1 €27.99 €12.60 DE MwSt(19%) €2.46 €15.39
PETG Basic
SKU: G00-G01-1.75-1000-SPL
Variant: Pine Green(30503) /
Filament with spool / 1kg
1 €25.99 €11.70 DE MwSt(19%) €2.28 €14.29
PLA Matte
SKU: A01-R4-1.75-1000-
SPLFREE
Variant: Dunkelrot (11202) /
Nachfüllung
1 €22.99 €10.35 DE MwSt(19%) €2.02 €12.64
Bambu 3D Effect Plate
SKU: FAP017-N
Variant: X1/P1/A1/P2/X2
1 €19.99 DE MwSt(19%) €3.19 €19.99
Bambu Reusable Spool
SKU: RSP001
Variant: Low Temp (≤70°C)
3 €13.99 €14.69 DE MwSt(19%) €4.36 €27.28
Order Number: EN750736547432341505
Invoice Date: 2026-07-07
"""
    inv = parse_order(INV)          # auto-detected, no separate entry point needed
    assert inv["source"] == "invoice"
    assert inv["order_ref"] == "EN750736547432341505", inv["order_ref"]
    assert datetime.fromtimestamp(inv["ordered_at"]).strftime("%Y-%m-%d") == "2026-07-07"
    got = [(l["product"], l["code"], l["color_name"], l["spools"],
            l["grams_each"], l["total_price"], l["refill"]) for l in inv["lines"]]
    assert got == [
        ("PLA Pure",   "A19-K00", "Absolute Black", 1, 1000.0, 15.39, False),
        ("PETG Basic", "G00-G01", "Pine Green",     1, 1000.0, 14.29, False),
        ("PLA Matte",  "A01-R4",  "Dunkelrot",      1, 1000.0, 12.64, True),
    ], got
    # list price (first money column) vs what was charged (last)
    assert [l["list_price"] for l in inv["lines"]] == [27.99, 25.99, 22.99]
    assert inv["currency"] == "€"
    # the invoice is the authority on colour names - and proves a guess wrong
    assert color_name("A19-K00") == "Absolute Black"
    print("filament_catalog: ok")
