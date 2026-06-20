#!/usr/bin/env python3
"""
NYLON WATCH poller v2 — material-aware + visual gloss.

Pipeline per run:
  1. Pull each shop's Shopify products.json (no key).
  2. Tag every product with which MATERIALS its text matches (taxonomy.json),
     producing a fit_score. Body description is included, not just the title.
  3. VISUAL STAGE: for candidate products, download the primary image and compute
     a 0..100 gloss_score (gloss.py, classical CV, no API). Cached by image URL
     across runs so each image is analyzed once. Capped per run.
  4. Keep a product if it matches an enabled material OR looks glossy enough.
  5. Diff vs state.json: flag NEW products and RESTOCK variants, push via ntfy.
  6. Write state.json (app reads it) + imgcache.json (persistent image cache).

Two scores kept SEPARATE end to end:
  - fit_score   : keyword/material confidence (what it's probably made of)
  - gloss_score : how shiny the photo looks (what it looks like)
A product with no keyword match but high gloss is kept and flagged GLOSSY —
the unlabelled-shiny catch.
"""

import json
import os
import time
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timezone

try:
    from gloss import score_image_bytes
    GLOSS_AVAILABLE = True
    _GLOSS_IMPORT_ERR = ""
except Exception as _e:
    GLOSS_AVAILABLE = False
    _GLOSS_IMPORT_ERR = str(_e)

HERE = os.path.dirname(os.path.abspath(__file__))
SHOPS_PATH = os.path.join(HERE, "shops.json")
STATE_PATH = os.path.join(HERE, "state.json")
TAX_PATH = os.path.join(HERE, "taxonomy.json")
IMGCACHE_PATH = os.path.join(HERE, "imgcache.json")
EXCLUDES_PATH = os.path.join(HERE, "excludes.json")

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Safari/605.1.15 NYLON-WATCH/2.0")
PER_PAGE = 250
MAX_PAGES = 8
REQUEST_TIMEOUT = 25
IMG_TIMEOUT = 18
SLEEP_BETWEEN = 1.2
IMG_SLEEP = 0.25
MAX_IMG_BYTES = 4_000_000

NTFY_BASE = os.environ.get("NTFY_BASE", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "").strip()


def log(*a):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}]", *a, flush=True)


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"WARN parse {path}: {e}")
    return default


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def http_get_bytes(url, timeout, cap):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(cap)


def fetch_shop_products(shop):
    base = shop["products_url"].rstrip("?")
    sep = "&" if "?" in base else "?"
    out = []
    for page in range(1, MAX_PAGES + 1):
        data = http_get_json(f"{base}{sep}limit={PER_PAGE}&page={page}")
        prods = data.get("products", [])
        if not prods:
            break
        out.extend(prods)
        if len(prods) < PER_PAGE:
            break
        time.sleep(SLEEP_BETWEEN)
    return out


def tag_materials(text, tax):
    """Return (fit_score, matched_materials, hit_terms) across all materials,
    minus global_avoid penalties."""
    t = (text or "").lower()
    fit = 0
    matched = []
    hits = []
    for mat, cfg in tax.get("materials", {}).items():
        w = cfg.get("weight", 1)
        local = 0
        for term in cfg.get("terms", []):
            if term.lower() in t:
                local += w
                hits.append(term)
        for term in cfg.get("avoid", []):
            if term.lower() in t:
                local -= w
        if local > 0:
            matched.append(mat)
            fit += local
    gw = tax.get("global_avoid_weight", 3)
    for term in tax.get("global_avoid", []):
        if term.lower() in t:
            fit -= gw
    return fit, matched, hits


def normalize(shop, p, tax):
    handle = p.get("handle", "")
    title = p.get("title", "")
    vendor = p.get("vendor", "")
    ptype = p.get("product_type", "")
    tags = p.get("tags", "")
    if isinstance(tags, list):
        tags = " ".join(tags)
    body = p.get("body_html", "") or ""
    blob = " ".join([title, vendor, ptype, tags, body[:600]])
    fit, mats, hits = tag_materials(blob, tax)

    image = ""
    imgs = p.get("images") or []
    if imgs and isinstance(imgs[0], dict):
        image = imgs[0].get("src", "")
    if not image and isinstance(p.get("featured_image"), str):
        image = p["featured_image"]

    price = None
    variants = []
    for v in p.get("variants", []) or []:
        vp = v.get("price")
        if vp is not None:
            try:
                price = float(vp) if price is None else min(price, float(vp))
            except (TypeError, ValueError):
                pass
        variants.append({"id": str(v.get("id")), "title": v.get("title", ""),
                         "available": bool(v.get("available")), "price": vp})

    return {
        "shop": shop["id"], "shop_name": shop["name"], "pid": str(p.get("id")),
        "title": title, "vendor": vendor, "type": ptype,
        "url": f"{shop['site'].rstrip('/')}/products/{handle}",
        "image": image, "price": price,
        "fit_score": fit, "materials": mats, "hits": list(dict.fromkeys(hits))[:8],
        "gloss_score": None, "gloss_ok": None,
        "variants": variants,
        "any_available": any(v["available"] for v in variants),
        "published_at": p.get("published_at") or p.get("created_at") or "",
    }


def run_visual(candidates, tax, imgcache):
    vis = tax.get("visual", {})
    if not vis.get("enabled", True) or not GLOSS_AVAILABLE:
        if not GLOSS_AVAILABLE:
            log(f"visual stage skipped: gloss import failed ({_GLOSS_IMPORT_ERR})")
        return 0
    cap = vis.get("max_images_per_run", 200)
    # spend image budget on keyword hits first, then newest
    candidates.sort(key=lambda p: (p["fit_score"] > 0, p.get("published_at", "")), reverse=True)
    analyzed = 0
    for p in candidates:
        url = p.get("image")
        if not url:
            continue
        cached = imgcache.get(url)
        if cached is not None:
            p["gloss_score"] = cached.get("gloss")
            p["gloss_ok"] = cached.get("ok")
            continue
        if analyzed >= cap:
            continue
        try:
            raw = http_get_bytes(url, IMG_TIMEOUT, MAX_IMG_BYTES)
            res = score_image_bytes(raw)
            p["gloss_score"] = res.get("gloss", 0)
            p["gloss_ok"] = res.get("ok", False)
            imgcache[url] = {"gloss": p["gloss_score"], "ok": p["gloss_ok"],
                             "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            analyzed += 1
            time.sleep(IMG_SLEEP)
        except Exception as e:
            p["gloss_ok"] = False
            imgcache[url] = {"gloss": 0, "ok": False, "err": str(e)[:80]}
    return analyzed


def variant_avail_map(prod):
    return {v["id"]: v["available"] for v in prod.get("variants", [])}


def ntfy_push(title, message, url, tags, priority="default"):
    if not NTFY_TOPIC:
        log("ntfy not configured; skip push:", title)
        return
    headers = {"Title": title.encode("utf-8"), "Tags": tags, "Priority": priority,
               "Click": url, "User-Agent": USER_AGENT}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    req = urllib.request.Request(f"{NTFY_BASE}/{NTFY_TOPIC}", data=message.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            r.read()
    except Exception as e:
        log(f"WARN ntfy fail {title!r}: {e}")


def is_excluded(p, excludes):
    """True if a product matches any exclude rule. Mirrors the app's logic exactly
    so feed and alerts behave identically.
      - items:    'shop|pid' exact match
      - brands:   case-insensitive vendor match (exact, trimmed)
      - keywords: case-insensitive substring anywhere in the title
    """
    if not excludes:
        return False
    key = p["shop"] + "|" + p["pid"]
    if key in set(excludes.get("items", [])):
        return True
    vend = (p.get("vendor") or "").strip().lower()
    if vend and vend in {b.strip().lower() for b in excludes.get("brands", [])}:
        return True
    title = (p.get("title") or "").lower()
    for kw in excludes.get("keywords", []):
        kw = kw.strip().lower()
        if kw and kw in title:
            return True
    return False


def keep_decision(p, tax):
    enabled = {m for m, c in tax.get("materials", {}).items() if c.get("enabled", True)}
    floor = tax.get("default_floor", 0)
    gfloor = tax.get("visual", {}).get("gloss_floor_for_unkeyworded", 62)
    matched_enabled = any(m in enabled for m in p["materials"])
    keyword_keep = matched_enabled and p["fit_score"] >= floor
    gloss_keep = (p.get("gloss_score") or 0) >= gfloor
    if keyword_keep and gloss_keep:
        return True, "BOTH"
    if keyword_keep:
        return True, "KW"
    if gloss_keep:
        return True, "GLOSSY"
    return False, ""


def main():
    shops = [s for s in load_json(SHOPS_PATH, []) if s.get("enabled", True)]
    tax = load_json(TAX_PATH, {})
    state = load_json(STATE_PATH, {})
    imgcache = load_json(IMGCACHE_PATH, {})
    excludes = load_json(EXCLUDES_PATH, {})

    prev = {p["shop"] + "|" + p["pid"]: p for p in state.get("products", [])}
    alerted = set(state.get("alerted_ids", []))
    first_run = not state.get("products")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    raw_normed = []
    shop_status = {}
    for shop in shops:
        try:
            prods = fetch_shop_products(shop)
            raw_normed.extend(normalize(shop, p, tax) for p in prods)
            shop_status[shop["id"]] = {"ok": True, "raw": len(prods), "at": now_iso}
            log(f"OK   {shop['id']}: {len(prods)} products")
        except urllib.error.HTTPError as e:
            shop_status[shop["id"]] = {"ok": False, "error": f"HTTP {e.code}", "at": now_iso}
            log(f"FAIL {shop['id']}: HTTP {e.code}")
        except Exception as e:
            shop_status[shop["id"]] = {"ok": False, "error": str(e)[:160], "at": now_iso}
            log(f"FAIL {shop['id']}: {e}")
        time.sleep(SLEEP_BETWEEN)

    # apply exclusions BEFORE imaging/diffing: excluded items never render,
    # never get a gloss image spent on them, and never fire a push.
    n_before = len(raw_normed)
    raw_normed = [p for p in raw_normed if not is_excluded(p, excludes)]
    n_excluded = n_before - len(raw_normed)
    if n_excluded:
        log(f"excluded {n_excluded} products via excludes.json")

    candidates = [p for p in raw_normed if p["fit_score"] > 0 or
                  (not p["materials"] and p["fit_score"] >= 0)]
    analyzed = run_visual(candidates, tax, imgcache)
    log(f"visual: analyzed {analyzed} new images, cache {len(imgcache)}")

    current = []
    for p in raw_normed:
        keep, why = keep_decision(p, tax)
        if keep:
            p["keep_reason"] = why
            current.append(p)

    counts = {}
    for p in current:
        counts[p["shop"]] = counts.get(p["shop"], 0) + 1
    for sid, st in shop_status.items():
        if st.get("ok"):
            st["count"] = counts.get(sid, 0)

    events = []
    for prod in current:
        key = prod["shop"] + "|" + prod["pid"]
        pv = prev.get(key)
        if pv is None:
            if prod["any_available"]:
                ev = "new:" + key
                prod["flag"] = "NEW"; prod["flagged_at"] = now_iso
                if ev not in alerted and not first_run:
                    events.append(("NEW", prod, ev))
                alerted.add(ev)
            else:
                prod["flag"] = "NEW_SOLDOUT"; prod["flagged_at"] = now_iso
            continue
        prod["flag"] = pv.get("flag", "")
        prod["flagged_at"] = pv.get("flagged_at", "")
        prev_v = variant_avail_map(pv)
        restocked = [v["title"] or "OS" for v in prod["variants"]
                     if prev_v.get(v["id"]) is False and v["available"]]
        came_back = (not pv.get("any_available")) and prod["any_available"]
        if restocked or came_back:
            ev = "restock:" + key + ":" + hashlib.sha1(
                (",".join(sorted(restocked)) + now_iso[:10]).encode()).hexdigest()[:8]
            prod["flag"] = "RESTOCK"; prod["flagged_at"] = now_iso
            prod["restocked_sizes"] = restocked
            if ev not in alerted and not first_run:
                events.append(("RESTOCK", prod, ev))
            alerted.add(ev)

    events.sort(key=lambda e: (e[1].get("gloss_score") or 0) + e[1]["fit_score"])
    for kind, prod, ev in events:
        price = f"${prod['price']:.0f}" if prod.get("price") else ""
        mats = "/".join(prod["materials"][:2]) or ("glossy" if (prod.get("gloss_score") or 0) >= 62 else "")
        g = prod.get("gloss_score")
        gtxt = f" · gloss {g}" if g else ""
        if kind == "NEW":
            ntfy_push(f"NEW · {prod['vendor'] or prod['shop_name']}",
                      f"{prod['title']} {price}\n{mats}{gtxt} · {prod['shop_name']}",
                      prod["url"], "sparkles", "default")
        else:
            sizes = ", ".join(prod.get("restocked_sizes", [])) or "back"
            ntfy_push(f"RESTOCK · {prod['vendor'] or prod['shop_name']}",
                      f"{prod['title']} — {sizes} {price}\n{mats}{gtxt} · {prod['shop_name']}",
                      prod["url"], "arrows_counterclockwise", "high")
        log(f"PUSH {kind} g={prod.get('gloss_score')} {prod['vendor']} — {prod['title'][:42]}")

    cutoff = time.time() - 7 * 86400
    for prod in current:
        fa = prod.get("flagged_at", "")
        if prod.get("flag") in ("NEW", "RESTOCK") and fa:
            try:
                if datetime.fromisoformat(fa).timestamp() < cutoff:
                    prod["flag"] = ""
            except ValueError:
                pass

    alerted_list = list(alerted)[-6000:]
    seen_imgs = {p["image"] for p in raw_normed if p.get("image")}
    imgcache = {k: v for k, v in imgcache.items() if k in seen_imgs}

    def rank(p):
        badge = 2 if p.get("flag") == "RESTOCK" else 1 if p.get("flag") == "NEW" else 0
        return (-badge, -((p.get("gloss_score") or 0) + p["fit_score"]), p["title"])

    out = {
        "schema": 2,
        "generated_at": now_iso,
        "shop_status": shop_status,
        "materials_enabled": {m: c.get("enabled", True) for m, c in tax.get("materials", {}).items()},
        "visual_on": tax.get("visual", {}).get("enabled", True) and GLOSS_AVAILABLE,
        "gloss_floor": tax.get("visual", {}).get("gloss_floor_for_unkeyworded", 62),
        "excluded_count": n_excluded,
        "excludes_active": bool(excludes.get("items") or excludes.get("brands") or excludes.get("keywords")),
        "product_count": len(current),
        "event_count": len(events),
        "first_run": first_run,
        "products": sorted(current, key=rank),
        "alerted_ids": alerted_list,
    }
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    with open(IMGCACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(imgcache, f, separators=(",", ":"))

    log(f"DONE kept={len(current)} events={len(events)} first_run={first_run}")
    if first_run:
        log("First run: baseline stored, no alerts. Future runs alert on change.")


if __name__ == "__main__":
    main()
