"""Generation engine: prepares inputs, runs providers, fits results back to the
exact original canvas, runs automatic checks and tracks attempts.

Every image source (test provider, manual import, API later) goes through the
same post-process and checks, so the rest of the app never cares where an
image came from.
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import numpy as np
from PIL import Image, ImageFilter

import scanner

WORK_DIR = os.path.join(scanner.DATA_DIR, "work")
INBOX_DIR = os.path.join(scanner.DATA_DIR, "inbox")
MAX_AUTO_ATTEMPTS = 3
ALPHA_ON = 24  # alpha above this counts as "painted"

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT, n INTEGER, source TEXT,
    prompt TEXT, file TEXT, raw_file TEXT, layout TEXT, checks TEXT,
    passed INTEGER, review TEXT DEFAULT '', review_reason TEXT DEFAULT '',
    created REAL
);
CREATE INDEX IF NOT EXISTS idx_attempts_guid ON attempts(guid);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS applied (
    guid TEXT PRIMARY KEY, path TEXT, applied_md5 TEXT, from_guid TEXT,
    attempt_n INTEGER, applied_at REAL
);
"""

DEFAULT_SETTINGS = {
    "style_prompt": "",
    "default_source": "manual",
    "openai_api_key": "",      # stored locally in data/, never sent back to the UI
    "openai_model": "gpt-image-1",
    "openai_quality": "medium",
    "agent_paths": "1",        # manual prompts name the input/output files for Codex
    "design_rules": "",        # game-design constraints that every prompt must respect
}
SECRET_KEYS = ("openai_api_key",)

CATEGORY_HINTS = {
    "UI": "This is a user-interface element from a game.",
    "Game": "This is an in-game sprite from a game.",
    "VFX": "This is a visual-effect texture from a game.",
}
ANIMATION_RULE = (
    "The existing skeleton animations must keep working with the new image without any "
    "re-rigging: every part must keep the exact same extent inside its slot (same bounding "
    "box, same pixel edges, nothing added or removed beyond the current shape), the same "
    "joint and attachment points where it connects to neighbouring parts (shoulder, hip, "
    "neck, wrist...), the same pivot and the same internal layout of features (eyes, "
    "fingers, folds stay in the same places, because meshes deform by those pixel "
    "positions). Eyes, mouths and brows exist only in their own eye/face parts: never paint "
    "a face onto a body or head part, the separate eye parts are drawn on top of it. "
    "Change only the look within those constraints: surface design, details, "
    "materials, colors, shading. Shadow parts (long flat ellipses, usually named 'shadow') "
    "are drawn as plain semi-transparent black ellipses: keep them exactly like that, in "
    "place, never drop them or recolor them. Small dark parts are eyes or brows, not shadows: "
    "keep them dark."
)
FRAME_RULE = (
    "If the parts are animation frames of the same sprite, use ONE new design and keep it "
    "identical across every frame (same colors, proportions and details), so the frames "
    "still play as a smooth animation.")
MODE_RULES = {
    "regenerate": "REDESIGN it: invent a new design for the same kind of object or character, "
                  "with different shapes, details, costume, materials and color accents, so the "
                  "result is clearly not a copy or a cleaned-up version of the input. Only keep "
                  "the same role, orientation, rough footprint (same position and about the same "
                  "size on the canvas) and the outline thickness of the input.",
    "part_sheet": "This image is a sheet of SEPARATE body parts used by an animation rig. "
                  "Redraw every part in exactly the same position, size and rotation. "
                  "Do not move, merge, add, remove or assemble parts. Keep the empty "
                  "gaps between parts empty. Inside its slot, give each part the new "
                  "design (new surface details, materials and color accents) while it "
                  "keeps the same role, orientation and outline thickness. " + FRAME_RULE,
    "recolor": "Keep the shape and outline pixel-for-pixel identical. Only restyle the "
               "surface: colors, shading and texture.",
}

state = {"current_manual": None, "worker": "idle", "inbox_log": []}
_wake = threading.Event()
_lock = threading.Lock()


def connect():
    db = scanner.connect()
    db.execute("PRAGMA busy_timeout=10000")
    db.executescript(SCHEMA)
    cols = {r["name"] for r in db.execute("PRAGMA table_info(assets)")}
    if "source" not in cols:
        db.execute("ALTER TABLE assets ADD COLUMN source TEXT DEFAULT ''")
    if "custom_prompt" not in cols:
        db.execute("ALTER TABLE assets ADD COLUMN custom_prompt TEXT DEFAULT ''")
    return db


SECRETS_PATH = os.path.join(scanner.DATA_DIR, "secrets.json")  # git-ignored, per PC


def _read_secrets():
    try:
        with open(SECRETS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_secrets(values):
    os.makedirs(scanner.DATA_DIR, exist_ok=True)
    with open(SECRETS_PATH, "w", encoding="utf-8") as f:
        json.dump(values, f)


def get_settings(db=None):
    """Shared settings live in the database (travels with git); secrets live
    in data/secrets.json, which stays on this PC."""
    own = db is None
    db = db or connect()
    out = dict(DEFAULT_SETTINGS)
    out.update({r["key"]: r["value"] for r in db.execute("SELECT * FROM settings")
                if r["key"] not in SECRET_KEYS})
    if own:
        db.close()
    secrets = _read_secrets()
    for k in SECRET_KEYS:
        out[k] = str(secrets.get(k, ""))
    return out


def public_settings():
    """Settings for the UI: secrets replaced by whether they are set."""
    out = get_settings()
    for k in SECRET_KEYS:
        out[k + "_set"] = bool(out.pop(k).strip())
    out["api_images"] = api_image_count()
    return out


def api_image_count():
    db = connect()
    n = db.execute("SELECT COUNT(*) FROM attempts WHERE source='api'").fetchone()[0]
    db.close()
    return n


def save_settings(values):
    db = connect()
    secrets = _read_secrets()
    for k, v in values.items():
        if k in SECRET_KEYS:
            if v is not None:  # UI sends null to mean "leave the key as it is"
                secrets[k] = str(v).strip()
            continue
        if k in DEFAULT_SETTINGS:
            db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) "
                       "DO UPDATE SET value=excluded.value", (k, str(v)))
    db.commit()
    db.close()
    _write_secrets(secrets)


def migrate_secrets():
    """Moves any secret still stored in the database out to secrets.json."""
    db = connect()
    rows = {r["key"]: r["value"] for r in db.execute("SELECT * FROM settings")
            if r["key"] in SECRET_KEYS}
    if rows:
        secrets = _read_secrets()
        for k, v in rows.items():
            if v and not secrets.get(k):
                secrets[k] = v
            db.execute("DELETE FROM settings WHERE key=?", (k,))
        db.commit()
        _write_secrets(secrets)
        db.execute("PRAGMA secure_delete=ON")
        db.execute("VACUUM")  # purge the deleted rows from the file itself
    db.close()


def work_dir(guid):
    path = os.path.join(WORK_DIR, guid)
    os.makedirs(path, exist_ok=True)
    return path


def get_asset(db, guid):
    row = db.execute("SELECT * FROM assets WHERE guid=?", (guid,)).fetchone()
    return dict(row) if row else None


def load_original(asset):
    return Image.open(original_path(asset)).convert("RGBA")


def original_path(asset):
    return scanner.original_file(asset["guid"], asset["path"])


def resize_rgba(im, size):
    """Lanczos resize with premultiplied alpha, so edges don't get dark fringes."""
    if im.size == tuple(size):
        return im
    return im.convert("RGBa").resize(size, Image.LANCZOS).convert("RGBA")


# ---------------------------------------------------------------- prepare

def prepare_input(asset):
    """Places the original on a generator-friendly canvas. Returns layout info."""
    folder = work_dir(asset["guid"])
    info_path = os.path.join(folder, "input.json")
    if os.path.exists(info_path) and os.path.exists(os.path.join(folder, "input.png")):
        with open(info_path) as f:
            return json.load(f)
    orig = load_original(asset)
    w, h = orig.size
    ratio = w / h
    canvas = (1536, 1024) if ratio > 1.25 else (1024, 1536) if ratio < 0.8 else (1024, 1024)
    margin = 0.92 if asset["has_alpha"] else 1.0
    scale = min(canvas[0] / w, canvas[1] / h) * margin
    sw, sh = max(1, round(w * scale)), max(1, round(h * scale))
    ox, oy = (canvas[0] - sw) // 2, (canvas[1] - sh) // 2
    sheet = Image.new("RGBA", canvas, (0, 0, 0, 0))
    sheet.paste(resize_rgba(orig, (sw, sh)), (ox, oy))
    sheet.save(os.path.join(folder, "input.png"))
    info = {"canvas": list(canvas), "box": [ox, oy, ox + sw, oy + sh]}
    with open(info_path, "w") as f:
        json.dump(info, f)
    return info


def agent_paths(asset):
    """Where an agent (Codex) reads the input and must save the result."""
    folder = work_dir(asset["guid"])
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", asset["name"])
    return (os.path.join(folder, "input.png"),
            os.path.join(INBOX_DIR, f"{safe}__{asset['guid'][:8]}.png"))


def asset_rules(asset):
    """Per-asset instructions shared by the single and batch prompts."""
    lines = [MODE_RULES.get(asset["mode"], MODE_RULES["regenerate"])]
    if asset.get("kind") == "spine":
        lines.append("Spine atlas page: parts are packed tightly and some are rotated 90 "
                     "degrees; keep every part exactly where it is, at the same rotation, "
                     "with fully transparent gaps. " + ANIMATION_RULE)
    if asset["has_alpha"]:
        lines.append("The background must stay fully transparent. Do not add a "
                     "background, floor, shadow or frame that is not in the original.")
    else:
        lines.append("Fill the whole canvas edge to edge, like the original.")
    if asset["grayscale"]:
        lines.append("Keep it grayscale (white/gray only): the game tints it with color at runtime.")
    return lines


def subject_name(asset):
    return re.sub(r"(?<=[a-z])(?=[A-Z0-9])|[_\-]+", " ", asset["name"]).strip()


def build_prompt(asset, settings, feedback=None):
    style = settings["style_prompt"].strip() or "[ART STYLE NOT SET YET - add it in Settings]"
    subject = subject_name(asset)
    custom = (asset.get("custom_prompt") or "").strip()
    lines = [
        "Redraw the attached image in a new art style (same subject, same footprint on the canvas).",
        f"New art style: {style}",
    ]
    rules = settings.get("design_rules", "").strip()
    if rules:
        lines.append("Game design rules that must be respected even in the new style: " + rules)
    if custom:  # written per asset after looking at it; replaces the generic description
        lines.append(custom)
    else:
        lines += [
            f"{CATEGORY_HINTS.get(asset['category'], 'This is a 2D game asset.')} "
            f"It is named \"{subject}\" ({asset['subcategory']}).",
            MODE_RULES.get(asset["mode"], MODE_RULES["regenerate"]),
        ]
    if asset.get("kind") == "spine":
        lines.append("This is a Spine atlas page: the parts are packed tightly and some are "
                     "rotated 90 degrees. Keep every part exactly where it is, at the same "
                     "rotation, and keep the gaps between parts fully transparent.")
        lines.append(ANIMATION_RULE)
    if asset["has_alpha"]:
        lines.append("The background must stay fully transparent. Do not add a "
                     "background, floor, shadow or frame that is not in the original.")
    else:
        lines.append("Fill the whole canvas edge to edge, like the original.")
    if asset["grayscale"]:
        lines.append("Keep it grayscale (white/gray only): the game tints it with color at runtime.")
    lines.append("Do not add any new text, letters, watermark or new elements. "
                 "Output the same canvas size and aspect ratio as the attached image.")
    if feedback:
        lines.append("The previous attempt was rejected. Fix this: " + feedback)
    if settings.get("agent_paths") == "1":
        src, dst = agent_paths(asset)
        lines += ["", "Files (if you can access the disk):",
                  f"Input image: {src}",
                  f"Save the finished image as a PNG at exactly this path: {dst}",
                  "Save nothing else there, and do not change any other files."]
    return "\n".join(lines)


def batch_prompt(db, limit=None):
    """One prompt for an agent to work through the whole manual queue."""
    settings = get_settings(db)
    style = settings["style_prompt"].strip() or "[ART STYLE NOT SET YET - add it in Settings]"
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM assets WHERE missing=0 AND (status='awaiting_manual' OR "
        "(status='checks_failed' AND source='manual')) ORDER BY category, name")]
    if limit:
        rows = rows[:limit]
    if not rows:
        return "", 0
    out = [
        f"Redesign each of the {len(rows)} game images listed below in a new art style, "
        "one output image per input image. These are NEW designs for a new game: the input "
        "only tells you what kind of thing it is, where it sits on the canvas, how big it is "
        "and how thick its outline is. Do not copy or polish the input drawing.",
        f"New art style: {style}",
    ]
    rules = settings.get("design_rules", "").strip()
    if rules:
        out.append("Game design rules that must be respected even in the new style: " + rules)
    out += [
        "General rules for every image:",
        "- Keep the same kind of subject, the same orientation and the same footprint "
        "(position and size on the canvas - fill the same area, do not shrink it). "
        "Invent new shapes, details, costume, materials and color accents; the notes under "
        "each image describe the new design.",
        "- Outline: a uniform bold dark line with exactly the same thickness as the outline "
        "in the input image. Match that weight; no thin or tapering lines.",
        "- Output exactly the same pixel size as the input image.",
        "- Do not write any explanation, description, plan or summary and do not ask "
        "questions: produce no text at all, just generate the images and save them.",
        "- Do not add any new text, letters, watermark or new elements.",
        "- Save each result as a PNG at the exact output path given. Save nothing else "
        "there and do not change any other files. Skip an image if you cannot do it.",
        "",
    ]
    for i, a in enumerate(rows, 1):
        src, dst = agent_paths(a)
        prepare_input(a)
        rules = asset_rules(a)
        feedback = last_feedback(db, a["guid"])
        out.append(f"{i}. \"{subject_name(a)}\" - {a['category']} / {a['subcategory']}, "
                   f"{'transparent' if a['has_alpha'] else 'opaque'}"
                   f"{', sheet of ' + str(a['slice_count']) + ' parts' if a['slice_count'] > 1 else ''}")
        custom = (a.get("custom_prompt") or "").strip()
        if custom:
            out += [f"   {line}" for line in custom.splitlines() if line.strip()]
        out += [f"   {r}" for r in rules if r != MODE_RULES["regenerate"]]
        if feedback:
            out.append(f"   Previous attempt was rejected, fix this: {feedback}")
        out.append(f"   Input:  {src}")
        out.append(f"   Output: {dst}")
    return "\n".join(out), len(rows)


# ----------------------------------------------------------- post-process

def _bbox(mask):
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def postprocess(raw, asset, info):
    """Fits a generated image back onto the exact original canvas."""
    raw = raw.convert("RGBA")
    w, h = asset["width"], asset["height"]
    rw, rh = raw.size
    cw, ch = info["canvas"]

    def close(a, b):
        return abs(a - b) / b < 0.03

    if close(rw / rh, cw / ch):
        kx, ky = rw / cw, rh / ch
        x0, y0, x1, y1 = info["box"]
        out = resize_rgba(raw.crop((round(x0 * kx), round(y0 * ky),
                                    round(x1 * kx), round(y1 * ky))), (w, h))
        layout = "exact"
    elif close(rw / rh, w / h):
        out, layout = resize_rgba(raw, (w, h)), "direct"
    else:
        layout = "fitted"
        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        orig_box = _bbox(np.asarray(load_original(asset))[..., 3] > ALPHA_ON)
        raw_box = _bbox(np.asarray(raw)[..., 3] > ALPHA_ON)
        if asset["has_alpha"] and orig_box and raw_box and raw_box != (0, 0, rw, rh):
            part = raw.crop(raw_box)
            bw, bh = orig_box[2] - orig_box[0], orig_box[3] - orig_box[1]
            k = min(bw / part.width, bh / part.height)
            part = resize_rgba(part, (max(1, round(part.width * k)),
                                      max(1, round(part.height * k))))
            out.paste(part, (orig_box[0] + (bw - part.width) // 2,
                             orig_box[1] + (bh - part.height) // 2))
        else:
            k = max(w / rw, h / rh)
            big = resize_rgba(raw, (max(w, round(rw * k)), max(h, round(rh * k))))
            left, top = (big.width - w) // 2, (big.height - h) // 2
            out = big.crop((left, top, left + w, top + h))
    px = np.array(out)
    fill = float((px[..., 3] > ALPHA_ON).mean())
    if asset["has_alpha"]:
        px[px[..., 3] < 8] = 0
    else:
        px[..., 3] = 255
    return Image.fromarray(px, "RGBA"), layout, fill


# ------------------------------------------------------------------ checks

def _iou(a, b):
    union = (a | b).sum()
    return float((a & b).sum() / union) if union else 1.0


FIXED_PART = re.compile(r"shadow", re.I)


def restore_fixed_parts(orig, result, slices, asset):
    """Spine shadow parts are plain semi-transparent black ellipses: generators drop or
    shrink them, and they never need a redesign. Copy them back from the original."""
    if asset.get("kind") != "spine" or result.size != orig.size:
        return result
    h = orig.height
    out = None
    for s in slices:
        if not FIXED_PART.search(s["name"]):
            continue
        x0, x1 = max(0, int(s["x"])), min(orig.width, int(s["x"] + s["w"]))
        y0, y1 = max(0, int(h - s["y"] - s["h"])), min(h, int(h - s["y"]))
        if x1 <= x0 or y1 <= y0:
            continue
        out = out or result.copy()
        out.paste(orig.crop((x0, y0, x1, y1)), (x0, y0))
    return out or result


def clip_parts_to_original(orig, result, slices, asset, grow=3):
    """Spine parts must keep their edges (the rig deforms exactly these pixels). Painted
    versions tend to thicken thin strokes (eyelashes, blink lines, staffs) or bleed a few
    pixels past the edge, so inside each part's slot the result is clipped to the original
    part's silhouette grown by `grow` px, with a soft 1 px edge. Parts that shrank are
    left alone (nothing to clip) and show up in the checks instead."""
    if asset.get("kind") != "spine" or result.size != orig.size or not slices:
        return result
    oa = np.asarray(orig)[..., 3]
    r = np.array(result)  # copy
    h, w = oa.shape
    for s in slices:
        x0, x1 = max(0, int(s["x"])), min(w, int(s["x"] + s["w"]))
        y0, y1 = max(0, int(h - s["y"] - s["h"])), min(h, int(h - s["y"]))
        if x1 <= x0 or y1 <= y0:
            continue
        m = Image.fromarray(((oa[y0:y1, x0:x1] > ALPHA_ON) * 255).astype(np.uint8))
        if not m.getbbox():
            continue
        m = m.filter(ImageFilter.MaxFilter(grow * 2 + 1)).filter(ImageFilter.GaussianBlur(1))
        k = np.asarray(m).astype(np.float32) / 255.0
        a = r[y0:y1, x0:x1, 3].astype(np.float32) * k
        r[y0:y1, x0:x1, 3] = a.round().astype(np.uint8)
    return Image.fromarray(r, "RGBA")


def distinctness(orig, result):
    """How far the result moved from the original: mean color difference on the shared
    opaque area (0..1) and edge-structure difference (1 - cosine similarity of edge maps).
    Calibrated on this project: genuine redesigns score color >= 0.24 / edge >= 0.85 for
    free sprites; Spine pages (edges locked) land lower, painted copies at ~0.06 / <0.4."""
    o = orig.resize((256, 256)); r = result.resize((256, 256))
    ao = np.asarray(o).astype(np.float32); ar = np.asarray(r).astype(np.float32)
    both = (ao[..., 3] > ALPHA_ON) & (ar[..., 3] > ALPHA_ON)
    if both.sum() < 50:
        return 1.0, 1.0
    color = float(np.abs(ao[..., :3] - ar[..., :3]).mean(2)[both].mean() / 255)
    go = np.asarray(o.convert("L").filter(ImageFilter.FIND_EDGES)).astype(np.float32)
    gr = np.asarray(r.convert("L").filter(ImageFilter.FIND_EDGES)).astype(np.float32)
    edge = 1 - float((go * gr).sum() / max(np.sqrt((go * go).sum() * (gr * gr).sum()), 1e-6))
    return color, edge


def run_checks(asset, orig, result, slices, layout, fill=1.0):
    """Returns (passed, [check dicts]). Fails block; warnings only inform."""
    checks = []

    def add(name, ok, detail, warn=False):
        checks.append({"name": name, "ok": bool(ok), "warn": warn, "detail": detail})

    w, h = asset["width"], asset["height"]
    add("Canvas size", result.size == (w, h), f"{result.width}x{result.height} (needs {w}x{h})")
    if layout == "fitted":
        add("Layout", True, "Aspect ratio didn't match, so the art was auto-fitted into "
            "the original bounds. Double-check the position.", warn=True)
    o = np.asarray(orig)
    r = np.asarray(result)
    om, rm = o[..., 3] > ALPHA_ON, r[..., 3] > ALPHA_ON
    mode = asset["mode"]

    if asset["has_alpha"]:
        opaque = rm.mean() > 0.98 and om.mean() < 0.9
        add("Transparent background", not opaque,
            "Result has a solid background" if opaque else "Background is transparent")
        if not opaque:
            need = {"recolor": 0.90, "part_sheet": 0.70}.get(mode, 0.40)  # redesigns move details around
            iou = _iou(om, rm)
            add("Silhouette match", iou >= need,
                f"{iou:.0%} overlap with the original shape (needs {need:.0%})")
            radius = max(2, round(0.02 * max(w, h)))
            grown = np.asarray(Image.fromarray((om * 255).astype(np.uint8))
                               .filter(ImageFilter.MaxFilter(radius * 2 + 1))) > 0
            spill = float((rm & ~grown).sum() / max(rm.sum(), 1))
            limit = 0.04 if mode in ("part_sheet", "recolor") else 0.10
            add("Stays inside outline", spill <= limit,
                f"{spill:.1%} of the art is outside the original outline (max {limit:.0%}); "
                "art outside it can get clipped in-game")
            if len(slices) > 1:
                bad = []
                for s in slices:
                    x0, x1 = int(s["x"]), int(s["x"] + s["w"])
                    y0, y1 = int(h - s["y"] - s["h"]), int(h - s["y"])
                    so, sr = om[y0:y1, x0:x1], rm[y0:y1, x0:x1]
                    if so.sum() == 0:
                        continue
                    ratio = sr.sum() / so.sum()
                    if not 0.6 <= ratio <= 1.5 or _iou(so, sr) < 0.5:
                        bad.append(s["name"])
                # Inside a part the dark features (eyes, mouth, markings) anchor mesh
                # deformation: if their center drifts, the rig animates the wrong pixels
                # even though the part's edges are intact. Warn, so a reviewer looks.
                drift = []
                lo, lr = np.asarray(orig.convert("L")), np.asarray(result.convert("L"))
                for s in slices:
                    x0, x1 = int(s["x"]), int(s["x"] + s["w"])
                    y0, y1 = int(h - s["y"] - s["h"]), int(h - s["y"])
                    if x1 - x0 < 24 or y1 - y0 < 24:
                        continue
                    do = (lo[y0:y1, x0:x1] < 90) & om[y0:y1, x0:x1]
                    dr = (lr[y0:y1, x0:x1] < 90) & rm[y0:y1, x0:x1]
                    if do.sum() < 30 or dr.sum() < 30:
                        continue
                    co, cr = np.argwhere(do).mean(0), np.argwhere(dr).mean(0)
                    if np.abs(co - cr).max() > 0.08 * max(x1 - x0, y1 - y0):
                        drift.append(s["name"])
                if drift:
                    add("Features anchored", True,  # a warning: ok stays True
                        f"dark features (eyes/markings) moved inside {len(drift)} part(s): "
                        + ", ".join(drift[:6]) + " - check that the rig still animates right",
                        warn=True)
                add("Parts in place", not bad,
                    f"All {len(slices)} parts are in their slots" if not bad else
                    f"{len(bad)} of {len(slices)} parts moved, changed size or went missing: "
                    + ", ".join(bad[:8]))
    else:
        add("Fills the canvas", fill >= 0.97,
            f"{fill:.0%} of the canvas is painted; this image has no transparency, "
            "so empty areas would show as black")
    if mode in ("regenerate", "part_sheet") and not asset["grayscale"]:
        color, edge = distinctness(orig, result)
        spine = asset.get("kind") == "spine"
        # Spine pages have locked part edges, so they cannot move as far: fail them only
        # when both numbers are low. Free sprites must clear one of the two bars.
        c_min, e_min = (0.10, 0.40) if spine else (0.20, 0.80)
        add("Distinct from original", color >= c_min or edge >= e_min,
            f"color change {color:.2f}, structure change {edge:.2f} (needs {c_min:.2f} or "
            f"{e_min:.2f}); increase the visual change for the requested restyle")
    if asset["grayscale"] and rm.any():
        rgb = r[rm][:, :3].astype(np.int16)
        chroma = float((rgb.max(1) - rgb.min(1)).mean())
        add("Stays grayscale", chroma <= 24,
            f"Average color tint {chroma:.0f} (max 24); this sprite is tinted by the game")
    passed = all(c["ok"] for c in checks)
    return passed, checks


# --------------------------------------------------------------- providers

def provider_test(asset, info, prompt, variant="ok"):
    """Stand-in generator: recolors the input so the full loop can be tested."""
    im = Image.open(os.path.join(work_dir(asset["guid"]), "input.png")).convert("RGBA")
    alpha = im.getchannel("A")
    hsv = np.array(im.convert("RGB").convert("HSV"))
    hsv[..., 0] = (hsv[..., 0].astype(np.int16) + 96) % 256
    out = Image.fromarray(hsv, "HSV").convert("RGB").filter(ImageFilter.SMOOTH)
    out.putalpha(alpha)
    if variant == "broken":  # deliberately shifted, to exercise failed checks
        moved = Image.new("RGBA", out.size, (0, 0, 0, 0))
        moved.paste(out.resize((out.width * 3 // 5, out.height * 3 // 5)),
                    (out.width // 3, out.height // 3))
        out = moved
    return out


class ProviderError(Exception):
    """A generation failure that should stop retries for this asset."""


def _openai_request(key, url, data=None, content_type=None, timeout=180):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=data, headers={"Authorization": f"Bearer {key}"})
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8"))["error"]["message"]
        except Exception:
            msg = f"HTTP {e.code}"
        raise ProviderError(f"OpenAI: {msg}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(f"Could not reach OpenAI: {getattr(e, 'reason', e)}") from None


def check_openai_key(key=None):
    """Verifies the key with a cheap, free request. Returns the image models seen."""
    key = (key or get_settings()["openai_api_key"]).strip()
    if not key:
        raise ProviderError("No API key saved")
    data = _openai_request(key, "https://api.openai.com/v1/models", timeout=20)
    models = sorted(m["id"] for m in data.get("data", []) if "image" in m["id"])
    return {"ok": True, "image_models": models}


def _multipart(fields, files):
    import uuid
    boundary = "----ArtRestyler" + uuid.uuid4().hex
    body = io.BytesIO()
    for name, value in fields.items():
        body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                   f"{value}\r\n".encode("utf-8"))
    for name, (filename, data, mime) in files.items():
        body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                   f"filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n".encode("utf-8"))
        body.write(data)
        body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode("utf-8"))
    return body.getvalue(), f"multipart/form-data; boundary={boundary}"


def provider_openai(asset, info, prompt):
    """Image edit via OpenAI's Images API: the prepared input + prompt in,
    a same-size image out. Retries rate limits, gives up on real errors."""
    import base64
    s = get_settings()
    key = s["openai_api_key"].strip()
    if not key:
        raise ProviderError("No OpenAI API key set (Settings)")
    with open(os.path.join(work_dir(asset["guid"]), "input.png"), "rb") as f:
        png = f.read()
    cw, ch = info["canvas"]
    fields = {"model": s["openai_model"], "prompt": prompt, "n": "1",
              "size": f"{cw}x{ch}", "quality": s["openai_quality"]}
    if asset["has_alpha"]:
        fields["background"] = "transparent"
    body, ctype = _multipart(fields, {"image": ("input.png", png, "image/png")})
    for wait in (0, 15, 45):
        if wait:
            time.sleep(wait)
        try:
            data = _openai_request(key, "https://api.openai.com/v1/images/edits", body, ctype)
            break
        except ProviderError as e:
            if "rate limit" not in str(e).lower() or wait == 45:
                raise
    item = data["data"][0]
    if "b64_json" in item:
        raw = base64.b64decode(item["b64_json"])
    else:
        import urllib.request
        with urllib.request.urlopen(item["url"], timeout=120) as r:
            raw = r.read()
    img = Image.open(io.BytesIO(raw))
    img.load()
    return img


PROVIDERS = {"test": provider_test, "api": provider_openai}


def available_sources():
    out = ["manual", "test"]
    if get_settings()["openai_api_key"].strip():
        out.append("api")
    return out


# ---------------------------------------------------------------- attempts

def record_attempt(db, asset, raw, source, prompt):
    """Post-processes + checks one generated image and stores it as an attempt."""
    guid = asset["guid"]
    info = prepare_input(asset)
    slices = [dict(r) for r in db.execute(
        "SELECT * FROM slices WHERE texture_guid=?", (guid,))]
    orig = load_original(asset)
    result, layout, fill = postprocess(raw, asset, info)
    result = clip_parts_to_original(orig, result, slices, asset)
    result = restore_fixed_parts(orig, result, slices, asset)
    passed, checks = run_checks(asset, orig, result, slices, layout, fill)
    n = (db.execute("SELECT MAX(n) FROM attempts WHERE guid=?", (guid,)).fetchone()[0] or 0) + 1
    folder = work_dir(guid)
    raw.save(os.path.join(folder, f"attempt_{n}_raw.png"))
    result.save(os.path.join(folder, f"attempt_{n}.png"))
    db.execute(
        "INSERT INTO attempts (guid, n, source, prompt, file, raw_file, layout, checks, "
        "passed, created) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (guid, n, source, prompt, f"attempt_{n}.png", f"attempt_{n}_raw.png", layout,
         json.dumps(checks), int(passed), time.time()))
    db.execute("UPDATE assets SET attempts=? WHERE guid=?", (n, guid))
    return passed, checks, n


def last_feedback(db, guid):
    row = db.execute("SELECT * FROM attempts WHERE guid=? ORDER BY n DESC LIMIT 1",
                     (guid,)).fetchone()
    if not row:
        return None
    if row["review"] == "rejected" and row["review_reason"]:
        return row["review_reason"]
    if not row["passed"]:
        return "; ".join(c["detail"] for c in json.loads(row["checks"]) if not c["ok"])
    return None


def current_prompt(db, asset):
    return build_prompt(asset, get_settings(db), last_feedback(db, asset["guid"]))


def set_status(db, guid, status):
    db.execute("UPDATE assets SET status=? WHERE guid=?", (status, guid))


def queue(guids, source):
    """Sends assets to a source. Duplicates and skipped assets are left out."""
    if source not in available_sources():
        raise ValueError("Add your OpenAI API key in Settings first" if source == "api"
                         else "Unknown source")
    db = connect()
    status = "awaiting_manual" if source == "manual" else "queued"
    n = 0
    for guid in guids:
        a = get_asset(db, guid)
        if not a or a["mode"] == "skip" or a["dup_of"] or a["status"] == "applied":
            continue
        db.execute("UPDATE assets SET status=?, source=? WHERE guid=?",
                   (status, source, guid))
        n += 1
    db.commit()
    db.close()
    _wake.set()
    return n


def import_manual(guid, data):
    """Takes an image the user made by hand (paste, drop or inbox)."""
    raw = Image.open(io.BytesIO(data))
    raw.load()
    with _lock:
        db = connect()
        asset = get_asset(db, guid)
        if not asset:
            db.close()
            raise ValueError("Unknown asset")
        prompt = current_prompt(db, asset)
        passed, checks, n = record_attempt(db, asset, raw, "manual", prompt)
        db.execute("UPDATE assets SET status=?, source='manual' WHERE guid=?",
                   ("generated" if passed else "checks_failed", guid))
        db.commit()
        db.close()
    return {"passed": passed, "checks": checks, "n": n}


def review(guid, verdict, reason=""):
    """approve / reject (re-queues) / override (accept despite failed checks) / reset."""
    db = connect()
    asset = get_asset(db, guid)
    last = db.execute("SELECT id FROM attempts WHERE guid=? ORDER BY n DESC LIMIT 1",
                      (guid,)).fetchone()
    if not asset:
        db.close()
        raise ValueError("Unknown asset")
    if asset["status"] == "applied":
        db.close()
        raise ValueError("This art is in the game. Roll it back first.")
    if verdict == "reset":
        set_status(db, guid, "pending")
    elif not last:
        db.close()
        raise ValueError("Nothing generated yet")
    elif verdict == "approve":
        db.execute("UPDATE attempts SET review='approved' WHERE id=?", (last["id"],))
        set_status(db, guid, "approved")
    elif verdict == "accept":  # the user's final sign-off
        db.execute("UPDATE attempts SET review='accepted' WHERE id=?", (last["id"],))
        set_status(db, guid, "accepted")
    elif verdict == "override":
        db.execute("UPDATE attempts SET review='override' WHERE id=?", (last["id"],))
        set_status(db, guid, "generated")
    elif verdict == "reject":
        db.execute("UPDATE attempts SET review='rejected', review_reason=? WHERE id=?",
                   (reason, last["id"]))
        set_status(db, guid, "awaiting_manual" if asset["source"] == "manual" else "queued")
    else:
        db.close()
        raise ValueError("Unknown verdict")
    db.commit()
    db.close()
    _wake.set()


# ------------------------------------------------------------------ worker

def process_auto(guid):
    """Runs an automatic provider with retries until checks pass."""
    with _lock:
        db = connect()
        asset = get_asset(db, guid)
        provider = PROVIDERS.get(asset["source"])
        if not provider:
            set_status(db, guid, "error")
            db.commit()
            db.close()
            return
        info = prepare_input(asset)
        passed = False
        for _ in range(MAX_AUTO_ATTEMPTS):
            prompt = current_prompt(db, asset)
            try:
                raw = provider(asset, info, prompt)
            except ProviderError as e:
                db.execute("UPDATE assets SET status='error', notes=? WHERE guid=?",
                           (str(e), guid))
                db.commit()
                db.close()
                return
            passed, _checks, _n = record_attempt(db, asset, raw, asset["source"], prompt)
            db.commit()
            if passed:
                break
        set_status(db, guid, "generated" if passed else "needs_human")
        db.commit()
        db.close()


def worker_loop():
    while True:
        db = connect()
        row = db.execute("SELECT guid, name FROM assets WHERE status='queued' "
                         "ORDER BY rowid LIMIT 1").fetchone()
        db.close()
        if not row:
            state["worker"] = "idle"
            _wake.wait(timeout=5)
            _wake.clear()
            continue
        state["worker"] = f"Generating {row['name']}"
        try:
            process_auto(row["guid"])
        except Exception as e:
            db = connect()
            db.execute("UPDATE assets SET status='error', notes=? WHERE guid=?",
                       (f"Generation error: {e}", row["guid"]))
            db.commit()
            db.close()


def inbox_target(name):
    """A file named <anything>__<guid8>.png or <guid>.png goes to that asset;
    anything else goes to the asset open in the manual workbench."""
    m = re.search(r"__([0-9a-f]{8})\.|^([0-9a-f]{32})\.", name, re.I)
    if m:
        prefix = (m.group(1) or m.group(2)).lower()
        db = connect()
        row = db.execute("SELECT guid FROM assets WHERE guid LIKE ? AND missing=0",
                         (prefix + "%",)).fetchone()
        db.close()
        if row:
            return row["guid"]
    return state["current_manual"]


def import_in_subprocess(guid, path):
    """Imports a file in a child process: the checks and clipping on a 2048x2048 page
    take seconds of CPU and, inside the server process, stalled every HTTP request
    (the UI looked dead while Codex deliveries were being imported)."""
    code = ("import sys, json, engine; "
            "print(json.dumps(engine.import_manual(sys.argv[1], open(sys.argv[2], 'rb').read())))")
    proc = subprocess.run([sys.executable, "-c", code, guid, path], cwd=scanner.TOOL_DIR,
                          capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(((proc.stderr or "").strip().splitlines() or ["import failed"])[-1])
    return json.loads(proc.stdout.strip().splitlines()[-1])


def inbox_loop():
    """Files saved into data/inbox are imported, matched by name or by the
    asset open in the manual workbench."""
    os.makedirs(INBOX_DIR, exist_ok=True)
    done = os.path.join(INBOX_DIR, "imported")
    locked = {}  # file name -> ticks it has been locked by its writer
    while True:
        time.sleep(2)
        try:
            names = sorted(os.listdir(INBOX_DIR))
        except OSError:
            continue
        for name in names:
            path = os.path.join(INBOX_DIR, name)
            if not name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            try:
                if time.time() - os.path.getmtime(path) < 3:
                    continue  # still being written
            except OSError:
                continue  # vanished between listdir and stat
            guid = inbox_target(name)
            if not guid:
                continue  # nothing to match it to yet; leave it for later
            # Move it out of the inbox BEFORE importing. On Windows the move fails
            # while the writer (Codex, a browser download) still holds the file, so a
            # locked file is simply retried on the next tick and nothing is imported
            # twice. A failure here must never escape: it would kill this thread.
            os.makedirs(done, exist_ok=True)
            dest = os.path.join(done, f"{int(time.time())}_{name}")
            try:
                os.replace(path, dest)
            except OSError as e:
                locked[name] = locked.get(name, 0) + 1
                if locked[name] == 5:  # ~10 s: tell the UI, keep retrying
                    state["inbox_log"].append(
                        {"guid": guid, "file": name,
                         "error": f"waiting, file still in use by another program: {e}"})
                continue
            locked.pop(name, None)
            try:
                print(f"[inbox] importing {name}", flush=True)
                t0 = time.time()
                result = import_in_subprocess(guid, dest)
                print(f"[inbox] done {name} in {time.time() - t0:.1f}s passed={result.get('passed')}",
                      flush=True)
                state["inbox_log"].append({"guid": guid, "file": name, **result})
            except Exception as e:  # bad image, unknown asset...: keep the file, log it
                state["inbox_log"].append({"guid": guid, "file": name, "error": str(e)})
                try:
                    os.replace(dest, os.path.join(INBOX_DIR, name + ".failed"))
                except OSError:
                    pass
            break


def start_background():
    connect().close()
    migrate_secrets()
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=inbox_loop, daemon=True).start()


def progress():
    db = connect()
    counts = {r[0]: r[1] for r in db.execute(
        "SELECT status, COUNT(*) FROM assets WHERE missing=0 GROUP BY status")}
    db.close()
    return {"counts": counts, "worker": state["worker"]}


# ---------------------------------------------------------- apply/rollback

def file_md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _write_image(result, asset, dest):
    """Writes new art in the original file's format, via a temp file."""
    ext = os.path.splitext(dest)[1].lower()
    tmp = dest + ".restyler_tmp"
    if ext == ".png":
        (result if asset["has_alpha"] else result.convert("RGB")).save(tmp, "PNG")
    elif ext in (".jpg", ".jpeg"):
        result.convert("RGB").save(tmp, "JPEG", quality=95)
    else:
        raise ValueError(f"{ext} files can't be written yet")
    os.replace(tmp, dest)


def apply(guids=None):
    """Puts accepted art into the Unity project. Only image bytes change:
    .meta files, GUIDs and sprite slices are never touched."""
    with _lock:
        db = connect()
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM assets WHERE status='accepted' AND missing=0")]
        if guids is not None:
            rows = [a for a in rows if a["guid"] in set(guids)]
        applied, skipped = 0, []
        for a in rows:
            att = db.execute("SELECT * FROM attempts WHERE guid=? ORDER BY n DESC LIMIT 1",
                             (a["guid"],)).fetchone()
            src = os.path.join(WORK_DIR, a["guid"], att["file"]) if att else ""
            if not os.path.isfile(src):
                skipped.append({"name": a["name"], "why": "generated image is missing"})
                continue
            result = Image.open(src).convert("RGBA")
            targets = [a] + [dict(r) for r in db.execute(
                "SELECT * FROM assets WHERE dup_of=? AND missing=0", (a["guid"],))]
            for t in targets:
                dest = os.path.join(scanner.PROJECT_ROOT, t["path"])
                prev = db.execute("SELECT applied_md5 FROM applied WHERE guid=?",
                                  (t["guid"],)).fetchone()
                try:
                    current = file_md5(dest)
                    if current != t["md5"] and not (prev and current == prev[0]):
                        raise ValueError("file was changed outside this tool; rescan first")
                    if result.size != (t["width"], t["height"]):
                        raise ValueError("size no longer matches the original")
                    backup = os.path.join(scanner.BACKUP_DIR, t["guid"],
                                          "original" + os.path.splitext(dest)[1])
                    if not os.path.exists(backup):
                        os.makedirs(os.path.dirname(backup), exist_ok=True)
                        shutil.copyfile(dest, backup)
                        if file_md5(backup) != t["md5"]:
                            os.remove(backup)
                            raise ValueError("backup could not be verified")
                    _write_image(result, t, dest)
                except (OSError, ValueError) as e:
                    skipped.append({"name": t["name"], "why": str(e)})
                    continue
                db.execute(
                    "INSERT INTO applied VALUES (?,?,?,?,?,?) ON CONFLICT(guid) DO UPDATE SET "
                    "applied_md5=excluded.applied_md5, attempt_n=excluded.attempt_n, "
                    "applied_at=excluded.applied_at",
                    (t["guid"], t["path"], file_md5(dest), a["guid"], att["n"], time.time()))
                set_status(db, t["guid"], "applied")
                db.commit()
                applied += 1
        db.close()
    return {"applied": applied, "skipped": skipped}


def rollback(guids=None):
    """Restores original art from the backups. None = everything applied."""
    with _lock:
        db = connect()
        rows = [dict(r) for r in db.execute("SELECT * FROM applied")]
        if guids is not None:
            wanted = set(guids)
            rows = [r for r in rows if r["guid"] in wanted or r["from_guid"] in wanted]
        restored, skipped = 0, []
        for r in rows:
            asset = get_asset(db, r["guid"])
            dest = os.path.join(scanner.PROJECT_ROOT, r["path"])
            backup = os.path.join(scanner.BACKUP_DIR, r["guid"],
                                  "original" + os.path.splitext(dest)[1])
            try:
                if not os.path.isfile(backup):
                    raise ValueError("backup file is missing")
                if file_md5(dest) != r["applied_md5"]:
                    raise ValueError("file was changed outside this tool; restore manually from the backup")
                tmp = dest + ".restyler_tmp"
                shutil.copyfile(backup, tmp)
                os.replace(tmp, dest)
                if asset and file_md5(dest) != asset["md5"]:
                    raise ValueError("restored file doesn't match the original")
            except (OSError, ValueError) as e:
                skipped.append({"name": asset["name"] if asset else r["path"], "why": str(e)})
                continue
            db.execute("DELETE FROM applied WHERE guid=?", (r["guid"],))
            set_status(db, r["guid"], "accepted" if r["guid"] == r["from_guid"] else "pending")
            db.commit()
            restored += 1
        db.close()
    return {"restored": restored, "skipped": skipped}
