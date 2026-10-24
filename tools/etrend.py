#!/usr/bin/env python3
"""Étrendtervező segédszkript: receptkeresés, recept megnézése, terv kiértékelése.

  etrend.py search [--q SZÖVEG] [--cat KATEGÓRIA] [--min KCAL] [--max KCAL] [--limit N] [--random] [--include-previous]
  etrend.py show AZONOSÍTÓ_VAGY_NÉV
  etrend.py plan terv.json [--save]   (vagy: plan - és a JSON stdinről)
  etrend.py last                      (az előző hét menüje: kimenet/aktualis-het/terv.json)
  etrend.py exceptions                (az exceptions.js tartalma)

A kimenet/aktualis-het/ mindig az aktuális hetet tárolja; az újat csak akkor írjuk felül, ha kész.
Az exceptions.js: {ingredients: ["..."], recipes: ["..."]} – kizárt hozzávalók/receptek.

A recipes.json minden receptje 1 adagra van skálázva (servings = 1).
"""
import argparse
import html
import json
import math
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOALS = {"szandi": 1600, "csabi": 1800}
BUFFER = (100, 150)  # kcal/fő/nap mozgástér (zöldség, tej a kávéba stb.)
NAMES = {"szandi": "Szandi", "csabi": "Csabi"}
WEEK_DIR = ROOT / "kimenet" / "aktualis-het"
EXCEPTIONS = ROOT / "exceptions.js"


def load():
    recipes = json.loads((ROOT / "recipes.json").read_text("utf-8"))["recipes"]
    for r in recipes:
        r["cat_name"] = r.get("category") or "—"
    return recipes


def load_exceptions():
    """-> (kizárt hozzávalók, kizárt receptnevek), kisbetűsítve. Üres/hiányzó fájl = nincs kivétel."""
    if not EXCEPTIONS.exists():
        return [], []
    src = re.sub(r"//.*|/\*.*?\*/", "", EXCEPTIONS.read_text("utf-8"), flags=re.S)

    def strings(key):
        m = re.search(key + r"\s*:\s*\[(.*?)\]", src, re.S)
        return [x.lower() for x in re.findall(r"[\"'`](.+?)[\"'`]", m.group(1))] if m else []

    ings, recs = strings("ingredients"), strings("recipes")
    if not ings and not recs:  # kulcsok nélkül: minden idézett szöveg kizárt hozzávaló
        ings = [x.lower() for x in re.findall(r"[\"'`](.+?)[\"'`]", src)]
    return ings, recs


def hit_exception(line, exc_ings):
    low = line.lower()
    return next((e for e in exc_ings if e in low), None)


def previous_week():
    f = WEEK_DIR / "terv.json"
    return json.loads(f.read_text("utf-8")) if f.exists() else None


def previous_ids(recipes):
    prev = previous_week()
    if not prev:
        return set()
    return {find(recipes, m["recipe"])["id"] for m in prev.get("menu", [])}


def find(recipes, key):
    key = key.strip().lower()
    hits = [r for r in recipes if r["id"].lower().startswith(key)]
    if not hits:
        hits = [r for r in recipes if r["name"].lower() == key]
    if not hits:
        hits = [r for r in recipes if key in r["name"].lower()]
    if len(hits) != 1:
        names = "; ".join(f"{r['id'][:8]} {r['name']}" for r in hits[:8])
        sys.exit(f"Recept '{key}': {len(hits)} találat. {names}")
    return hits[0]


def ingredient_lines(r):
    ing = r.get("ingredients")
    if isinstance(ing, list):
        lines = [str(x) for x in ing]
    elif isinstance(ing, str):
        lines = ing.split("\n")
    else:
        lines = []
    out = []
    for l in lines:
        l = html.unescape(re.sub(r"<[^>]+>", "", l)).strip()
        if l and "kifli.hu" not in l and not l.lower().startswith("alapanyag bevásárlólistáért"):
            out.append(l)
    return out


def steps_text(r):
    """Az elkészítés szövege az adatbázisból szó szerint (a kalória/makró blokkot kivéve).
    A számozott listák (<ol>) 1., 2., ... sorok, a pontozottak (<ul>) kötőjeles sorok lesznek."""
    t = r.get("instructions") or ""

    def number(m):
        k = 0

        def li(_):
            nonlocal k
            k += 1
            return f"\n{k}. "

        return re.sub(r"<li[^>]*>", li, m.group(0))

    t = re.sub(r"<ol[^>]*>.*?</ol>", number, t, flags=re.S | re.I)
    t = re.sub(r"<li[^>]*>", "\n- ", t)
    t = re.sub(r"</li>|</p>|</div>|</pre>|<br\s*/?>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    lines = [l.strip() for l in t.split("\n") if l.strip()]
    cut = next((i for i, l in enumerate(lines) if l.lower().startswith("kalória és makrók")), len(lines))
    lines = [re.sub(r"^Elkészítés:\s*", "", l) for l in lines[:cut]]
    return "\n".join(l for l in lines if l)


# ---------- hozzávaló-értelmezés ----------
UNITS = {"g": ("g", 1), "dkg": ("g", 10), "kg": ("g", 1000), "ml": ("ml", 1), "cl": ("ml", 10),
         "dl": ("ml", 100), "l": ("ml", 1000), "db": ("db", 1), "darab": ("db", 1), "ek": ("ek", 1),
         "evőkanál": ("ek", 1), "tk": ("tk", 1), "tsk": ("tk", 1), "teáskanál": ("tk", 1),
         "gerezd": ("gerezd", 1), "szelet": ("szelet", 1), "csipet": ("csipet", 1), "marék": ("marék", 1)}
QTY = re.compile(r"^\s*(?:kb\.?\s*|~\s*|egy\s+)?(\d+(?:[.,]\d+)?(?:\s*/\s*\d+)?)(?:\s*-\s*\d+(?:[.,]\d+)?)?\s*([^\W\d_]+)?\s*(.*)$", re.I)


def num(s):
    s = s.replace(" ", "").replace(",", ".")
    if "/" in s:
        a, b = s.split("/")
        return float(a) / float(b)
    return float(s)


def parse_ing(line):
    """-> (mennyiség, egység, név) vagy None, ha nem értelmezhető."""
    if line.lstrip().startswith("+"):
        return None
    m = QTY.match(line)
    if not m:
        return None
    qty, unit, rest = m.groups()
    u = UNITS.get((unit or "").lower())
    if u:
        name = rest
    else:  # "2 tojás" -> darab, a "unit" a név része
        u, name = ("db", 1), ((unit or "") + " " + rest).strip()
    name = re.sub(r"\(.*?\)", "", name)
    name = re.sub(r"\d+(?:[.,]\d+)?\s*%\s*-?(?:os|os)?\s*(?:zsírtartalmú\s*)?", "", name)
    name = re.split(r"[,;–-]\s", name)[0]
    name = re.sub(r"\s+", " ", name).strip(" .:").lower()
    if not name:
        return None
    return num(qty) * u[1], u[0], name


GROUPS = [
    ("Húsok, halak, felvágottak", r"csirke|pulyka|sertés|marha|darált|bacon|sonka|kolbász|sovány felvágott|hal\b|tonhal|lazac|garnéla|szalonna|virsli|húsgolyó|comb|mell"),
    ("Tejtermék, tojás", r"tej|joghurt|skyr|túró|sajt|feta|mozzarella|parmez|tejföl|tejszín|vaj\b|krémsajt|kefir|tojás|mascarpone|ricotta|cottage"),
    ("Pékáru, tészta, gabona", r"kenyér|tortilla|wrap|zsemle|kifli|bagett|pita|tészta|spagetti|penne|rizs|zab|liszt|bulgur|kuszkusz|müzli|pehely|keksz|burger|bun|galuska|kukoricadara|rizspapír|piskóta"),
    ("Zöldség és gyümölcs", r"hagyma|paprika|paradicsom|uborka|saláta|cukkini|répa|krumpli|burgonya|brokkoli|karfiol|spenót|gomba|padlizsán|káposzta|fokhagyma|alma|banán|eper|bogyó|áfonya|málna|citrom|lime|narancs|avokádó|sütőtök|zeller|kukorica|póréhagyma|bébispenót|rukkola|gyömbér|petrezselyem|koriander|bazsalikom|snidling|zöldborsó|zöldbab|retek|édesburgonya|sárgarépa|körte|szőlő|mangó|kivi|őszibarack|szilva|gyümölcs"),
    ("Fűszerek, szószok, ízesítők", r"^só\b|bors|kömény|pirospaprika|fűszer|oregánó|fahéj|vanília|szósz|ketchup|mustár|ecet|szójaszósz|majonéz|curry|kurkuma|chili|olaj|olíva|aroma|sütőpor|szódabikarbóna|cukor|édesítő|méz|eritrit|mák|kakaó|zselatin|keményítő|leveskocka|alaplé"),
    ("Konzerv, szárazáru, egyéb", r"konzerv|bab\b|lencse|csicseriborsó|kukorica|tonhal|fehérjepor|protein|nutella|mogyoróvaj|dió|mandula|mogyoró|mag\b|magok|csokoládé|kókusz"),
]


def group_of(name):
    for g, rx in GROUPS:
        if re.search(rx, name):
            return g
    return "Egyéb / ellenőrizendő"


# ---------- parancsok ----------
def cmd_search(a):
    rs = load()
    q = (a.q or "").lower()
    _, exc_recs = load_exceptions()
    prev = set() if a.include_previous else previous_ids(rs)
    out = []
    for r in rs:
        if r["id"] in prev or any(e in r["name"].lower() for e in exc_recs):
            continue
        if a.cat and a.cat.lower() not in r["cat_name"].lower():
            continue
        if a.min is not None and r["calories_per_serving"] < a.min:
            continue
        if a.max is not None and r["calories_per_serving"] > a.max:
            continue
        if q and q not in (r["name"] + " " + " ".join(ingredient_lines(r))).lower():
            continue
        out.append(r)
    if a.random:
        random.shuffle(out)
    else:
        out.sort(key=lambda r: (r["cat_name"], r["calories_per_serving"]))
    for r in out[: a.limit]:
        print(f"{r['id'][:8]} | {r['calories_per_serving']:>4} kcal | {r['cat_name']} | {r['name']}")
    print(f"-- {len(out)} találat" + (f", az első {a.limit} látszik" if len(out) > a.limit else ""))


def cmd_show(a):
    r = find(load(), a.key)
    print(f"{r['name']}  [{r['id'][:8]}]  {r['calories_per_serving']} kcal/adag  | {r['cat_name']}")
    print(f"Makrók: {r.get('label') or '—'}\n\nHozzávalók (1 adag):")
    exc_ings, _ = load_exceptions()
    for l in ingredient_lines(r):
        hit = hit_exception(l, exc_ings)
        print(" -", l + (f"   <-- KIVÉTEL ({hit}), helyettesítendő" if hit else ""))
    print("\nElkészítés:\n" + steps_text(r))


def fmt(x):
    return f"{x:.0f}" if abs(x - round(x)) < 0.05 else f"{x:.1f}".replace(".", ",")


def _ceil_to(x, step):
    return math.ceil(x / step - 0.02) * step


def buy(x, unit):
    """Bevásárláshoz felfelé kerekített, ésszerű mennyiség."""
    if unit in ("g", "ml"):
        return _ceil_to(x, 5 if x < 100 else 10)
    if unit in ("db", "gerezd", "szelet", "marék"):
        return _ceil_to(x, 1)
    return _ceil_to(x, 0.5)  # ek, tk, csipet


def cook(x, unit):
    """Főzéshez kerekített mennyiség a recept-táblázatba: (érték, előjel)."""
    if unit in ("g", "ml"):
        v = round(x) if x < 20 else round(x / 5) * 5
    elif unit in ("db", "ek", "tk", "szelet", "gerezd", "marék", "csipet"):
        v = round(x * 2) / 2
        if v == 0 and x > 0:
            return "kevés", ""
    else:
        v = x
    return fmt(v), unit


def cell(x, unit):
    v, u = cook(x, unit)
    return f"{v} {u}".strip()


def cmd_plan(a):
    """Mealprep-terv kiértékelése. A terv JSON-t fájlból vagy stdinről ('-') olvassa, semmit nem ment.

    {"menu": [{"slot": "reggeli", "recipe": "azonosító/név", "szandi": 1, "csabi": 1.5}, ...],
     "days": {"szandi": 3, "csabi": 5},
     "skip": [{"person": "szandi", "slot": "ebéd", "days": 2}]}

    Minden menü-étel minden napon szerepel (mealprep); a szorzó az egy napi adag személyenként
    (0 = az illető ezt sosem eszi). A "skip" azt adja meg, hány napon hagyja ki az illető az adott étkezést.
    """
    recipes = load()
    exc_ings, _ = load_exceptions()
    flagged = []
    plan = json.loads(sys.stdin.read() if a.plan == "-" else Path(a.plan).read_text("utf-8"))
    persons = list(GOALS)
    days = {p: int(plan["days"].get(p, 0)) for p in persons}
    skips = defaultdict(int)
    for sk in plan.get("skip", []):
        skips[(sk["person"].lower(), sk["slot"].lower())] += int(sk["days"])

    shop = defaultdict(float)
    unparsed = defaultdict(float)
    kc = dict.fromkeys(persons, 0.0)
    batches = []
    warnings = []
    for m in plan["menu"]:
        r = find(recipes, m["recipe"])
        slot = m.get("slot", "")
        port = {}
        for p in persons:
            f = float(m.get(p, 1))
            n = max(days[p] - skips[(p, slot.lower())], 0)
            port[p] = (f, f * n)  # (napi adag, összes adag az egész időszakra)
            kc[p] += r["calories_per_serving"] * f
        total = {p: port[p][1] for p in persons}
        batches.append((slot, r, port, total))
        for line in ingredient_lines(r):
            hit = hit_exception(line, exc_ings)
            if hit:
                flagged.append(f"KIVÉTEL ({hit}): {r['name']}: {line} (x {fmt(sum(total.values()))} adag) – keress helyettesítőt")
                continue
            pi = parse_ing(line)
            for p in persons:
                if pi:
                    shop[(pi[2], pi[1])] += pi[0] * total[p]
                else:
                    unparsed[line] += total[p]

    out = []
    out.append("NAPI KALÓRIA (mealprep, minden nap ugyanaz)")
    for p in persons:
        lo, hi = GOALS[p] - BUFFER[1], GOALS[p] - BUFFER[0]
        ok = lo <= kc[p] <= hi
        out.append(f"- {NAMES[p]}: {kc[p]:.0f} kcal/nap (cél a receptekre: {lo}-{hi}) {'OK' if ok else 'ELTÉRÉS'}; {days[p]} napig eszik")
        if not ok and days[p]:
            warnings.append(f"{NAMES[p]}: {kc[p]:.0f} kcal, a célsáv {lo}-{hi}")
    diffs = sum(1 for m in plan["menu"] if float(m.get("szandi", 1)) != float(m.get("csabi", 1)))
    out.append(f"- Étel, amiben a két adag eltér: {diffs} (cél: legfeljebb 2)")
    if diffs > 2:
        warnings.append(f"{diffs} étel adagja tér el a két személynél, legfeljebb 2 lenne ideális")
    out.append("")

    groups = defaultdict(list)
    for (name, unit), q in shop.items():
        groups[group_of(name)].append((name, unit, q))
    out.append("BEVÁSÁRLÓLISTA")
    for g in [x[0] for x in GROUPS] + ["Egyéb / ellenőrizendő"]:
        if g not in groups:
            continue
        out.append("")
        out.append(g.upper())
        for name, unit, q in sorted(groups[g]):
            out.append(f"- {name}: {fmt(buy(q, unit))} {unit}")
    if unparsed:
        out.append("")
        out.append("NEM ÉRTELMEZETT SOROK (kézzel ellenőrizendő)")
        for l, f in sorted(unparsed.items()):
            out.append(f"- {l} (x {fmt(f)} adag összesen)")
    out.append("")
    out.append("RECEPTEK (a teljes időszakra, két főre)")
    for slot, r, port, total in batches:
        out.append("")
        out.append(f"{r['name'].upper()} ({slot})")
        out.append(f"Napi adag: Szandi {fmt(port['szandi'][0])}x ({r['calories_per_serving'] * port['szandi'][0]:.0f} kcal), "
                   f"Csabi {fmt(port['csabi'][0])}x ({r['calories_per_serving'] * port['csabi'][0]:.0f} kcal)")
        out.append(f"Összesen főzendő: {fmt(total['szandi'] + total['csabi'])} adag (Szandi {fmt(total['szandi'])}, Csabi {fmt(total['csabi'])})")

        def ing_block(title, mult, check=False):
            out.append("")
            out.append(title)
            for line in ingredient_lines(r):
                if hit_exception(line, exc_ings):
                    out.append(f"- {line}  <-- KIVÉTEL, helyettesítendő")
                    continue
                pi = parse_ing(line)
                if pi:
                    q, unit, name = pi
                    out.append(f"- {name}: {cell(q * mult, unit)}")
                    if check and unit == "db" and abs(q * mult * 2 - round(q * mult * 2)) > 0.1:
                        warnings.append(f"{r['name']}: {name} {fmt(q * mult)} db, nem kerekíthető jól; válassz más adagszámot")
                else:
                    out.append(f"- {line} (x {fmt(mult)})")

        differ = port["szandi"][0] != port["csabi"][0] and port["szandi"][0] and port["csabi"][0]
        both = total["szandi"] + total["csabi"]
        ing_block("Hozzávalók összesen (az egész időszakra):", both, check=True)
        if differ:
            ing_block(f"Ebből Szandi adagjába ({fmt(total['szandi'])} adag):", total["szandi"])
            ing_block(f"Ebből Csabi adagjába ({fmt(total['csabi'])} adag):", total["csabi"])
        daily = {p: port[p][0] for p in persons if port[p][0]}
        if differ:
            ing_block(f"Egy napi adag, Szandi ({fmt(daily['szandi'])} adag):", daily["szandi"])
            ing_block(f"Egy napi adag, Csabi ({fmt(daily['csabi'])} adag):", daily["csabi"])
        else:
            ing_block(f"Egy napi adag (mindketten, {fmt(sum(daily.values()))} adag együtt):", sum(daily.values()))
        out.append("")
        out.append("Elkészítés:")
        out.append(steps_text(r))
    warnings = flagged + warnings
    prev = previous_ids(recipes)
    for m in plan["menu"]:
        if find(recipes, m["recipe"])["id"] in prev:
            warnings.append(f"{find(recipes, m['recipe'])['name']}: szerepelt az előző héten, válassz másikat")
    if warnings:
        out.append("")
        out.append("FIGYELMEZTETÉSEK")
        out += [f"- {w}" for w in warnings]
    print("\n".join(out))
    if a.save:
        WEEK_DIR.mkdir(parents=True, exist_ok=True)
        (WEEK_DIR / "terv.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), "utf-8")
        print(f"\n[terv elmentve: {WEEK_DIR / 'terv.json'}]")


def cmd_last(a):
    prev = previous_week()
    if not prev:
        print("Nincs előző hét (kimenet/aktualis-het/terv.json hiányzik).")
        return
    recipes = load()
    print("ELŐZŐ HÉT MENÜJE")
    for m in prev["menu"]:
        r = find(recipes, m["recipe"])
        print(f"- {m.get('slot', '')}: {r['name']} [{r['id'][:8]}] Szandi {m.get('szandi', 1)}x, Csabi {m.get('csabi', 1)}x")
    print(f"Napok: {prev.get('days')}")


def cmd_exceptions(a):
    ings, recs = load_exceptions()
    print("Kizárt hozzávalók:", ", ".join(ings) or "—")
    print("Kizárt receptek:", ", ".join(recs) or "—")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("--q"); s.add_argument("--cat")
    s.add_argument("--min", type=float); s.add_argument("--max", type=float)
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--random", action="store_true", help="véletlen sorrend (ne válogass értékítélet alapján)")
    s.add_argument("--include-previous", action="store_true", help="az előző heti recepteket is mutassa")
    s.set_defaults(fn=cmd_search)
    s = sub.add_parser("show"); s.add_argument("key"); s.set_defaults(fn=cmd_show)
    s = sub.add_parser("plan"); s.add_argument("plan", help="JSON fájl, vagy - az stdinhez"); s.add_argument("--save", action="store_true", help="a tervet elmenti kimenet/aktualis-het/terv.json-be (felülír)"); s.set_defaults(fn=cmd_plan)
    sub.add_parser("last").set_defaults(fn=cmd_last)
    sub.add_parser("exceptions").set_defaults(fn=cmd_exceptions)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
