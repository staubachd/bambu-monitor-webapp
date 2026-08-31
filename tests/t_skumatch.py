"""Linking an invoice line to the filament it actually bought.

The two sides describe the same spool in different vocabularies. A print row
knows a SKU and a hex colour (`GFA01|E8AF00`) because that is what the printer
reports. An invoice knows a colour code and a product name (`A01-Y0`,
"PLA Basic Yellow") because that is what the shop prints. Neither carries the
other's key.

Four routes bridge that, most certain first, and the interesting rule is the
last one: every colour of one product line derives the SAME sku - A01-R4, A01-G0
and A01-B6 are all GFA01 - so matching on sku alone would attach a red invoice
to a blue spool. When it cannot tell, it must return nothing. A wrong link puts
somebody else's money on this filament.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import app, filament_catalog


def match(purchase, identities, colours_on_file=None):
    """Run one purchase against a set of known identities.

    identities: fkey -> the row the app would have (code, color_name, ...)
    """
    agg = {k: {} for k in identities}
    known = identities
    by_code, by_match = {}, {}
    for k, m in known.items():
        nc = filament_catalog.norm_code(m.get("code"))
        if nc:
            by_code[nc] = k
        mk = filament_catalog.match_key(m.get("product"), m.get("color_name"))
        if mk:
            by_match[mk] = k
    sku_colours = {}
    for code in (colours_on_file or [purchase.get("code")]):
        sku = filament_catalog.sku_from_code(code)
        nc = filament_catalog.norm_code(code)
        if sku and nc:
            sku_colours.setdefault(sku, set()).add(nc)
    return app._match_purchase(purchase, agg, known, by_code, by_match, sku_colours)


YELLOW = {"code": "A01-Y0", "product": "PLA Basic", "color_name": "Yellow"}
RED = {"code": "A01-R4", "product": "PLA Basic", "color_name": "Red"}

# --- the certain routes -----------------------------------------------------
got = match({"fkey": "GFA01|E8AF00", "code": "A01-Y0"}, {"GFA01|E8AF00": YELLOW})
assert got == ("GFA01|E8AF00", "fkey"), got
print("an invoice already carrying an fkey is taken as read")

got = match({"code": "A01-Y0"}, {"GFA01|E8AF00": YELLOW})
assert got == ("GFA01|E8AF00", "code"), got
print("a colour code matches the identity that carries it")

# and the padding difference the printer and the shop disagree about
got = match({"code": "A01-Y00"}, {"GFA01|E8AF00": YELLOW})
assert got == ("GFA01|E8AF00", "code"), (
    f"{got} - the AMS pads the colour to two digits and the shop does not; "
    f"norm_code exists so that is not two different colours")
print("'A01-Y00' from the shop finds 'A01-Y0' from the AMS")

got = match({"product": "PLA Basic", "color_name": "Yellow"}, {"GFA01|E8AF00": YELLOW})
assert got == ("GFA01|E8AF00", "name"), got
print("failing that, the product and colour name match")

# --- the sku bridge, which reaches spools used up long ago -----------------
# The identity knows nothing but its sku and hex: no code, no colour name.
anon = {"GFA01|E8AF00": {}}
got = match({"code": "A01-Y0", "color_name": "Yellow"}, anon)
assert got == ("GFA01|E8AF00", "sku"), (
    f"{got} - a spool used up before the AMS ever saw it has only a sku and a "
    f"hex, and sku_from_code is the only bridge to an invoice")
print("a nameless history-only identity is reached through its sku")

# --- and the refusals that make that route safe ----------------------------
# Identities named by hand with a colour only, never a code and never a product
# line - so the code route has nothing to match and the name route's key
# ("|yellow") does not equal the invoice's ("pla basic|yellow"). That is exactly
# where the sku route has to be careful.
named = {"GFA01|E8AF00": {"color_name": "Yellow"},
         "GFA01|C12E1F": {"color_name": "Red"}}
got = match({"code": "A01-Y0", "product": "PLA Basic", "color_name": "Yellow"}, named)
assert got == ("GFA01|E8AF00", "sku+colour"), got
print("two colours of one product: the colour name decides")

got = match({"code": "A01-R4", "product": "PLA Basic", "color_name": "Red"}, named)
assert got == ("GFA01|C12E1F", "sku+colour"), (
    f"{got} - a red invoice was matched against a yellow spool")
print("a red invoice does not land on the yellow spool")

# and where the identities DO carry codes, the surer route wins outright
two = {"GFA01|E8AF00": YELLOW, "GFA01|C12E1F": RED}
assert match({"code": "A01-R4", "color_name": "Red"}, two) == ("GFA01|C12E1F", "code")
print("with codes on file, the code route settles it before sku is consulted")

# two anonymous candidates and nothing to tell them apart
got = match({"code": "A01-Y0"}, {"GFA01|E8AF00": {}, "GFA01|C12E1F": {}})
assert got == (None, "ambiguous"), (
    f"{got} - two spools of the same product line, neither named: attaching the "
    f"invoice to one of them is a coin flip")
print("two nameless candidates -> ambiguous, not a guess")

# one anonymous candidate, but the purchase log holds several colours of the
# product: still a coin flip, because the other colours may not be seen yet
got = match({"code": "A01-Y0"}, {"GFA01|E8AF00": {}},
            colours_on_file=["A01-Y0", "A01-R4", "A01-B6"])
assert got == (None, "ambiguous"), (
    f"{got} - three colours were bought and only one spool is known; claiming it "
    f"for whichever invoice asked first is arbitrary")
print("one nameless candidate but three colours bought -> still ambiguous")

# the same, when only one colour was ever bought: no ambiguity to have
got = match({"code": "A01-Y0"}, {"GFA01|E8AF00": {}}, colours_on_file=["A01-Y0"])
assert got == ("GFA01|E8AF00", "sku"), got
print("...but with only one colour on file there is nothing to confuse it with")

# --- nothing at all ---------------------------------------------------------
assert match({"code": "Z99-X9"}, {"GFA01|E8AF00": YELLOW}) == (None, None)
assert match({}, {"GFA01|E8AF00": YELLOW}) == (None, None)
print("an unknown code, or no code, matches nothing")

# --- the bridge itself ------------------------------------------------------
for code, sku in [("A19-K00", "GFA19"), ("A00-W01", "GFA00"), ("G00-G01", "GFG00")]:
    assert filament_catalog.sku_from_code(code) == sku, code
assert filament_catalog.sku_from_code("RSP001") is None, \
    "a code that is not a Bambu colour code produced a sku anyway"
print("sku_from_code holds on the pairs it was derived from")
print("ok")
