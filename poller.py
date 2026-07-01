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
import random
import hashlib
import tempfile
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
RATINGS_PATH = os.path.join(HERE, "ratings.json")

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Safari/605.1.15 NYLON-WATCH/2.0")
PER_PAGE = 250
MAX_PAGES = 8
REQUEST_TIMEOUT = 25
IMG_TIMEOUT = 18
SLEEP_BETWEEN = 1.2
IMG_SLEEP = 0.25
MAX_IMG_BYTES = 4_000_000
# Statuses worth retrying: Shopify's bot-mitigation intermittently 503/430/429s
# GitHub's datacenter IPs, and a different shop tends to fail each run. These are
# transient, so a polite retry with jitter keeps one blip from dropping a shop's
# whole catalog. Hard statuses (403/404/401) are NOT here — they fail fast.
RETRY_STATUS = {429, 430, 500, 502, 503, 504}
FETCH_RETRIES = 3
IMG_CACHE_TTL_DAYS = 45  # keep a scored image this long after we last saw it,
                         # so a shop failing to fetch for a run doesn't discard
                         # (and force costly re-scoring of) all its gloss cache.

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


def _backoff_sleep(attempt, retry_after=None):
    """Exponential backoff (1s, 2s, 4s...) capped at 20s, plus up to 1.5s of
    random jitter so many shops retrying at once don't sync into a thundering
    herd. Honors a server Retry-After header when it gives a plain number."""
    if retry_after and str(retry_after).strip().isdigit():
        base = min(float(retry_after), 20.0)
    else:
        base = min(2.0 ** attempt, 20.0)
    time.sleep(base + random.uniform(0.0, 1.5))


def http_get_json(url, retries=FETCH_RETRIES):
    """GET JSON, retrying transient failures with jittered backoff.

    Retries on: connection timeouts/resets AND HTTP statuses in RETRY_STATUS
    (503/430/429/5xx — Shopify's intermittent bot-mitigation). A hard status
    like 403/404 is a real block and propagates immediately without waiting."""
    last = None
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in RETRY_STATUS and attempt < retries:
                ra = e.headers.get("Retry-After") if e.headers else None
                log(f"  transient HTTP {e.code} on attempt {attempt + 1}, backing off")
                _backoff_sleep(attempt, ra)
                continue
            raise
        except Exception as e:
            last = e
            if attempt < retries:
                log(f"  transient error on attempt {attempt + 1} ({str(e)[:60]}), backing off")
                _backoff_sleep(attempt)
                continue
            raise
    raise last


def atomic_write_json(path, obj):
    """Write JSON to a temp file in the same directory, fsync, then os.replace
    onto the target. os.replace is atomic, so a crash mid-write can never leave
    a truncated/half-written file behind — readers always see either the old
    complete file or the new complete file. This is what stops a crashed run
    from corrupting state.json (a partial state was the notification-storm root
    cause: a truncated file read as 'no products' and reset first_run)."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def matched_disqualifier(text, tax):
    """Return the first silhouette/hardware disqualifier term found in the text,
    or "". These are the fetish/hardware/expose-not-cover cues (locking collar,
    harness, crotch zip, lace-up, corset...) that gloss.py can't see because it
    scores sheen, not shape. Text is a coarse proxy, but it's the only shape
    signal available. Shipped and editable in taxonomy.json -> disqualifiers."""
    t = (text or "").lower()
    for term in tax.get("disqualifiers", {}).get("terms", []):
        if term.lower() in t:
            return term
    return ""


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
    disq = matched_disqualifier(blob, tax)

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
        "disqualified": disq,
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
    def apply_tone(prod, d):
        # tone signals ride alongside gloss; missing on legacy cache entries,
        # which the ranker then treats as neutral (no bonus/penalty).
        prod["litsat"] = d.get("litsat")
        prod["subjval"] = d.get("subjval")
        prod["specden"] = d.get("specden")

    analyzed = 0
    for p in candidates:
        url = p.get("image")
        if not url:
            continue
        cached = imgcache.get(url)
        if cached is not None:
            p["gloss_score"] = cached.get("gloss")
            p["gloss_ok"] = cached.get("ok")
            apply_tone(p, cached)
            continue
        if analyzed >= cap:
            continue
        try:
            raw = http_get_bytes(url, IMG_TIMEOUT, MAX_IMG_BYTES)
            res = score_image_bytes(raw)
            p["gloss_score"] = res.get("gloss", 0)
            p["gloss_ok"] = res.get("ok", False)
            entry = {"gloss": p["gloss_score"], "ok": p["gloss_ok"],
                     "litsat": res.get("lit_sat_raw"), "subjval": res.get("subj_val"),
                     "specden": res.get("spec_density"),
                     "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            imgcache[url] = entry
            apply_tone(p, entry)
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
    mats = tax.get("materials", {})
    floor = tax.get("default_floor", 0)
    gfloor = tax.get("visual", {}).get("gloss_floor_for_unkeyworded", 62)
    # Only SHINE materials (satin, wet-look, patent, latex, pvc) keep an item on
    # keywords. Matte-prone fabric labels (nylon, rainwear) carry keyword_keep:false
    # — they tag for display but must be proven glossy by the image. This is what
    # stops the feed filling with matte technical nylon.
    matched_enabled = any(m in p["materials"]
                          and mats.get(m, {}).get("enabled", True)
                          and mats.get(m, {}).get("keyword_keep", True)
                          for m in p["materials"])
    keyword_keep = matched_enabled and p["fit_score"] >= floor
    gloss_keep = (p.get("gloss_score") or 0) >= gfloor
    if keyword_keep and gloss_keep:
        return True, "BOTH"
    if keyword_keep:
        return True, "KW"
    if gloss_keep:
        return True, "GLOSSY"
    return False, ""


# Default tone-ranking config; overridable in taxonomy.json -> visual.tone_ranking.
TONE_DEFAULTS = {
    "enabled": True,
    "min_gloss": 45,      # below this, a photo isn't glossy enough to tier — neutral
    "sat_colored": 0.28,  # lit-band saturation above this = a COLORED gloss
    "dark_val": 0.30,     # subject brightness below this = a DARK/black garment
    "bright_val": 0.55,   # brightness above this + low saturation = METALLIC/silver
    "sharp_spec": 0.02,   # highlight tightness above this = wet-look/latex (vs soft satin)
    "bonus": {"colored": 30, "metallic": 18, "wetlook_black": 0,
              "black_satin": -25, "neutral": 0},
}


def tone_and_desire(p, tax):
    """Classify a product's gloss TONE from image signals and return
    (tone, desire). desire = gloss_score + a tone bonus/penalty encoding the
    aesthetic priority order: colored > metallic > black wet-look, with black
    SATIN penalized (it reads flat/cheap). Items with no tone data (legacy
    cache, or too matte to tier) are 'neutral' — no bonus, no penalty."""
    cfg = {**TONE_DEFAULTS, **tax.get("visual", {}).get("tone_ranking", {})}
    bonus = {**TONE_DEFAULTS["bonus"], **cfg.get("bonus", {})}
    g = p.get("gloss_score") or 0
    litsat, subjval, specden = p.get("litsat"), p.get("subjval"), p.get("specden")
    if (not cfg.get("enabled", True) or g < cfg["min_gloss"]
            or litsat is None or subjval is None):
        tone = "neutral"
    elif litsat >= cfg["sat_colored"]:
        tone = "colored"
    elif subjval <= cfg["dark_val"]:
        # dark + colorless: sharp highlights = wet-look (good), soft = satin (cheap)
        tone = "wetlook_black" if (specden or 0) >= cfg["sharp_spec"] else "black_satin"
    elif subjval >= cfg["bright_val"]:
        tone = "metallic"
    else:
        tone = "neutral"
    return tone, g + bonus.get(tone, 0)


def main():
    shops = [s for s in load_json(SHOPS_PATH, []) if s.get("enabled", True)]
    tax = load_json(TAX_PATH, {})
    state = load_json(STATE_PATH, {})
    imgcache = load_json(IMGCACHE_PATH, {})
    excludes = load_json(EXCLUDES_PATH, {})
    ratings = load_json(RATINGS_PATH, {})
    # your shininess verdicts: 'matte' items are hidden like excludes; 'shiny'
    # items are always kept, pinned, and flagged PICK — your call beats the score.
    matte_set = set(ratings.get("matte", []))
    shiny_set = set(ratings.get("shiny", []))

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

    # silhouette/hardware veto: drop fetish/expose-not-cover cuts before render,
    # regardless of how glossy the photo is. A hard "no", also pre-imaging so we
    # don't spend the image budget on a piece the aesthetic rejects on shape.
    n_before = len(raw_normed)
    raw_normed = [p for p in raw_normed if not p.get("disqualified")]
    n_disqualified = n_before - len(raw_normed)
    if n_disqualified:
        log(f"disqualified {n_disqualified} products via taxonomy disqualifiers")

    # your "not shiny" verdicts hide those items everywhere (feed + any push)
    n_before = len(raw_normed)
    raw_normed = [p for p in raw_normed if (p["shop"] + "|" + p["pid"]) not in matte_set]
    n_matte = n_before - len(raw_normed)
    if n_matte:
        log(f"hid {n_matte} products you rated 'not shiny' (ratings.json)")

    candidates = [p for p in raw_normed if p["fit_score"] > 0 or
                  (not p["materials"] and p["fit_score"] >= 0)]
    analyzed = run_visual(candidates, tax, imgcache)
    log(f"visual: analyzed {analyzed} new images, cache {len(imgcache)}")

    current = []
    for p in raw_normed:
        keep, why = keep_decision(p, tax)
        is_pick = (p["shop"] + "|" + p["pid"]) in shiny_set
        if keep or is_pick:                 # a 'shiny' pick is kept even if the
            p["keep_reason"] = why or "PICK"  # score/keywords would have dropped it
            p["tone"], p["desire"] = tone_and_desire(p, tax)
            if is_pick:
                p["pick"] = True
                p["desire"] += 1000         # your picks rank above everything
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
    # Prune the image cache by AGE, not strict presence: refresh last-seen for
    # every image observed this run, and keep anything seen within the TTL so a
    # transient shop outage doesn't purge (and force re-scoring of) its images.
    now_ts = time.time()
    ttl = IMG_CACHE_TTL_DAYS * 86400
    seen_imgs = {p["image"] for p in raw_normed if p.get("image")}
    pruned = {}
    for k, v in imgcache.items():
        if not isinstance(v, dict):
            continue
        if k in seen_imgs:
            v["seen"] = now_ts
            pruned[k] = v
        else:
            last = v.get("seen", now_ts)  # legacy entries get a one-run grace
            if now_ts - last < ttl:
                v["seen"] = last
                pruned[k] = v
    imgcache = pruned

    def rank(p):
        badge = 2 if p.get("flag") == "RESTOCK" else 1 if p.get("flag") == "NEW" else 0
        # desire = gloss + tone bonus/penalty (colored/metallic up, black satin down)
        desire = p.get("desire")
        if desire is None:
            desire = (p.get("gloss_score") or 0) + p["fit_score"]
        return (-badge, -desire, p["title"])

    out = {
        "schema": 2,
        "generated_at": now_iso,
        "shop_status": shop_status,
        "materials_enabled": {m: c.get("enabled", True) for m, c in tax.get("materials", {}).items()},
        "visual_on": tax.get("visual", {}).get("enabled", True) and GLOSS_AVAILABLE,
        "gloss_floor": tax.get("visual", {}).get("gloss_floor_for_unkeyworded", 62),
        "excluded_count": n_excluded,
        "disqualified_count": n_disqualified,
        "rated_matte_count": n_matte,
        "rated_shiny_count": sum(1 for p in current if p.get("pick")),
        "excludes_active": bool(excludes.get("items") or excludes.get("brands") or excludes.get("keywords")),
        "product_count": len(current),
        "event_count": len(events),
        "first_run": first_run,
        "products": sorted(current, key=rank),
        "alerted_ids": alerted_list,
    }
    # Don't overwrite a good state with an empty one: if EVERY shop failed to
    # fetch this run (a bad-luck cron where bot-mitigation blocked all of them)
    # and we already have a populated state, keep the last good state untouched.
    any_ok = any(st.get("ok") for st in shop_status.values())
    if not any_ok and prev:
        log("ALL shops failed this run — preserving previous state.json, skipping write.")
        return
    atomic_write_json(STATE_PATH, out)
    atomic_write_json(IMGCACHE_PATH, imgcache)

    log(f"DONE kept={len(current)} events={len(events)} first_run={first_run}")
    if first_run:
        log("First run: baseline stored, no alerts. Future runs alert on change.")


if __name__ == "__main__":
    main()
