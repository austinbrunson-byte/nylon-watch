#!/usr/bin/env python3
"""
gloss.py — score how 'shiny' a garment photo looks, with classical computer
vision only. No ML model, no API, no key, no per-image cost. Runs in a GitHub
Action with just Pillow + numpy.

The physical idea: glossy / wet-look / latex / PVC / patent surfaces reflect
light specularly, producing:
  1. SPECULAR HIGHLIGHTS — small clusters of near-white, near-blown-out pixels.
  2. HIGH DYNAMIC RANGE — those hotspots sit next to rapid dark falloff, so the
     bright-vs-dark spread within the garment is large.
  3. SATURATED LIT REGIONS — on colored latex/PVC the lit areas stay vivid
     rather than washing to grey (helps separate true gloss from a plain
     white matte garment on a bright background).
Matte cotton/wool/fleece diffuses light: even mid-tones, few hot pixels, low
local contrast. So a weighted blend of those three signals separates them.

score_image_bytes(raw) -> dict with components and a 0..100 'gloss' score.

Caveats (documented, not hidden):
  - A white matte item on a white seamless can score mid; a black latex piece
    in flat studio light can score low. That's why the poller keeps gloss as a
    SEPARATE axis from keyword fit, never the sole gate.
  - Operates on the listing's primary image, so it judges the photo, not the
    cloth. Good enough to surface candidates; you make the call.
"""

import io
import numpy as np
from PIL import Image, ImageFilter


def _prep(raw, max_side=320):
    """Decode, downscale, return RGB float array in 0..1 and an HSV array."""
    im = Image.open(io.BytesIO(raw))
    im = im.convert("RGB")
    w, h = im.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR)
    rgb = np.asarray(im, dtype=np.float32) / 255.0
    hsv = np.asarray(im.convert("HSV"), dtype=np.float32) / 255.0
    return im, rgb, hsv


def _center_mask(shape, frac=0.86):
    """Soft mask favoring the center, to discount bright seamless backgrounds
    that bleed in at the edges. Not a true subject cutout — just edge damping."""
    h, w = shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    cy, cx = (h - 1) / 2.0, (w - 1) / 2.0
    ry, rx = h * frac / 2.0, w * frac / 2.0
    d = ((yy - cy) / ry) ** 2 + ((xx - cx) / rx) ** 2
    m = np.clip(1.2 - d, 0.0, 1.0)
    return m


def score_image_bytes(raw):
    try:
        im, rgb, hsv = _prep(raw)
    except Exception as e:
        return {"gloss": 0, "ok": False, "error": str(e)[:120]}

    lum = 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
    sat = hsv[..., 1]
    mask = _center_mask(lum.shape)
    mw = mask.sum() + 1e-6

    # --- 1. specular highlight density -------------------------------------
    # Fraction of (center-weighted) pixels that are very bright AND locally
    # peaked. We approximate "locally peaked" by comparing each pixel to a
    # blurred version: a true specular hotspot is much brighter than its
    # neighborhood, unlike a broad bright background.
    lum_img = Image.fromarray((lum * 255).astype(np.uint8))
    blur = np.asarray(lum_img.filter(ImageFilter.GaussianBlur(radius=6)), dtype=np.float32) / 255.0
    peak = np.clip(lum - blur, 0, 1)                      # local brightness excess
    hot = ((lum > 0.82) & (peak > 0.06)).astype(np.float32)
    spec_density = float((hot * mask).sum() / mw)         # 0..~0.2 typically
    # Reward presence but saturate: a little specular goes a long way.
    spec_score = 1.0 - np.exp(-spec_density * 60.0)       # 0..1

    # --- 2. dynamic range within the garment -------------------------------
    # Spread between bright and dark center pixels. Specular surfaces have
    # blown highlights next to deep falloff -> wide spread.
    vals = lum[mask > 0.5]
    if vals.size < 50:
        vals = lum.reshape(-1)
    p5, p95 = np.percentile(vals, 5), np.percentile(vals, 95)
    dr = float(np.clip(p95 - p5, 0, 1))
    # Also reward a heavy bright tail (the highlight) specifically.
    bright_tail = float(np.clip(np.percentile(vals, 99) - np.percentile(vals, 70), 0, 1))
    dr_score = float(np.clip(0.55 * dr + 0.9 * bright_tail, 0, 1))

    # --- 3. saturation in lit regions --------------------------------------
    # On colored gloss, the lit band stays saturated. Wash-to-grey => matte or
    # plain white. Measure mean saturation of the brighter half of the subject.
    bright_sel = (lum > np.percentile(vals, 60)) & (mask > 0.5)
    lit_sat = float(sat[bright_sel].mean()) if bright_sel.any() else 0.0
    # Black latex has ~0 saturation but huge spec+dr, so this is a bonus, not a gate.
    sat_score = float(np.clip(lit_sat * 1.4, 0, 1))

    # --- combine ------------------------------------------------------------
    # Specular density is the strongest single cue; dynamic range second;
    # saturation a supporting bonus. Tuned on synthetic + sanity images.
    gloss01 = 0.50 * spec_score + 0.34 * dr_score + 0.16 * sat_score
    gloss = int(round(float(np.clip(gloss01, 0, 1)) * 100))

    return {
        "gloss": gloss,
        "ok": True,
        "spec": round(spec_score, 3),
        "spec_density": round(spec_density, 4),
        "dynrange": round(dr_score, 3),
        "litsat": round(sat_score, 3),
    }


if __name__ == "__main__":
    # quick manual check against generated swatches
    import sys
    if len(sys.argv) > 1:
        with open(sys.argv[1], "rb") as f:
            print(score_image_bytes(f.read()))
