"""Scans the Unity project and builds the asset catalog (SQLite).

Textures are linked to their sprite slices and to the prefabs/scenes that use
them by GUID, never by file name. Manual edits made in the UI survive rescans.
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
import time

import numpy as np
from PIL import Image

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_json(name, default):
    try:
        with open(os.path.join(TOOL_DIR, name), encoding="utf-8") as f:
            return {**default, **json.load(f)}
    except (OSError, ValueError):
        return dict(default)


# project.json: which game this copy of the tool belongs to.
CONFIG = _load_json("project.json", {"name": "Game", "port": 8765, "project_root": "../..",
                                     "excluded_dirs": []})
PROJECT_ROOT = os.path.abspath(os.path.join(TOOL_DIR, CONFIG["project_root"]))
ASSETS_DIR = os.path.join(PROJECT_ROOT, "Assets")
DATA_DIR = os.path.join(TOOL_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "restyler.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backup")

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tga", ".psd")
# Third-party / editor-only folders (project.json): never part of the redesign.
EXCLUDED_DIRS = tuple(CONFIG["excluded_dirs"]) or ("Assets/Plugins/",)
SPINE_ATLAS_EXTS = (".atlas.txt", ".atlas")
USAGE_EXTS = (".prefab", ".unity", ".asset", ".mat", ".anim", ".controller",
              ".overrideController")

UI_IMAGE_SCRIPTS = {
    "fe87c0e1cc204ed48ad3b37840f39efc",  # UnityEngine.UI.Image
    "1344c3c82d62a2a41a3576d8abb8e3ea",  # UnityEngine.UI.RawImage
}

GUID_RE = re.compile(r"^guid: ([0-9a-f]{32})", re.M)
TOKEN_RE = re.compile(r"^--- !u!(\d+) |guid: ([0-9a-f]{32})", re.M)
SCRIPT_RE = re.compile(r"m_Script: \{fileID: \d+, guid: ([0-9a-f]{32})")

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    guid TEXT PRIMARY KEY, path TEXT, name TEXT,
    width INTEGER, height INTEGER, bytes INTEGER, md5 TEXT, dup_of TEXT,
    has_alpha INTEGER, grayscale INTEGER,
    slice_count INTEGER, has_border INTEGER,
    ui_refs INTEGER, world_refs INTEGER, vfx_refs INTEGER, script_refs INTEGER,
    data_refs INTEGER, mat_refs INTEGER, anim_refs INTEGER, total_refs INTEGER,
    category TEXT, subcategory TEXT, mode TEXT, manual INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, notes TEXT DEFAULT '',
    missing INTEGER DEFAULT 0, kind TEXT DEFAULT 'sprite'
);
CREATE TABLE IF NOT EXISTS slices (
    sprite_guid TEXT PRIMARY KEY, texture_guid TEXT, name TEXT,
    x REAL, y REAL, w REAL, h REAL, pivot_x REAL, pivot_y REAL,
    border_l REAL, border_b REAL, border_r REAL, border_t REAL
);
CREATE TABLE IF NOT EXISTS usages (texture_guid TEXT, kind TEXT, file TEXT);
CREATE INDEX IF NOT EXISTS idx_slices_tex ON slices(texture_guid);
CREATE INDEX IF NOT EXISTS idx_usages_tex ON usages(texture_guid);
"""


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    if "kind" not in {r["name"] for r in db.execute("PRAGMA table_info(assets)")}:
        db.execute("ALTER TABLE assets ADD COLUMN kind TEXT DEFAULT 'sprite'")
    return db


def original_file(guid, rel_path):
    """The untouched original: the backup once new art has been applied."""
    backup = os.path.join(BACKUP_DIR, guid, "original" + os.path.splitext(rel_path)[1])
    return backup if os.path.exists(backup) else os.path.join(PROJECT_ROOT, rel_path)


def rel(path):
    return os.path.relpath(path, PROJECT_ROOT).replace("\\", "/")


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def meta_guid(path):
    try:
        m = GUID_RE.search(read_text(path + ".meta"))
        return m.group(1) if m else None
    except OSError:
        return None


def walk(exts):
    for root, _dirs, files in os.walk(ASSETS_DIR):
        for name in files:
            if name.lower().endswith(exts):
                yield os.path.join(root, name)


def inspect_image(path):
    """Returns (width, height, has_alpha, grayscale)."""
    try:
        with Image.open(path) as im:
            w, h = im.size
            rgba = im.convert("RGBA")
            rgba.thumbnail((64, 64))
            px = np.asarray(rgba, dtype=np.int16)
            rgb = px[px[..., 3] > 16][:, :3]
            has_alpha = len(rgb) < rgba.width * rgba.height
            gray = len(rgb) > 0 and int((rgb.max(1) - rgb.min(1)).max()) <= 12
            return w, h, int(has_alpha), int(gray)
    except Exception:
        return 0, 0, 0, 0


def scan_textures():
    textures = {}
    for path in walk(IMAGE_EXTS):
        r = rel(path)
        if r.startswith(EXCLUDED_DIRS):
            continue
        guid = meta_guid(path)
        if not guid:
            continue
        # Always describe the original art, even after new art was applied.
        source = original_file(guid, r)
        with open(source, "rb") as f:
            md5 = hashlib.md5(f.read()).hexdigest()
        w, h, has_alpha, gray = inspect_image(source)
        textures[guid] = dict(
            guid=guid, path=r, name=os.path.splitext(os.path.basename(r))[0],
            width=w, height=h, bytes=os.path.getsize(source), md5=md5,
            has_alpha=has_alpha, grayscale=gray, kind="sprite",
            meta_slices=meta_slices(path + ".meta", guid))
    return textures


ALIGN_PIVOT = {0: (0.5, 0.5), 1: (0, 1), 2: (0.5, 1), 3: (1, 1), 4: (0, 0.5),
               5: (1, 0.5), 6: (0, 0), 7: (0.5, 0), 8: (1, 0)}
META_SPRITE_RE = re.compile(
    r"name: (?P<name>.*?)\n.*?rect:\n\s+serializedVersion: \d+\n\s+x: (?P<x>\S+)\n\s+y: (?P<y>\S+)\n"
    r"\s+width: (?P<w>\S+)\n\s+height: (?P<h>\S+)\n\s+alignment: (?P<al>\d+)\n"
    r"\s+pivot: \{x: (?P<px>\S+), y: (?P<py>\S+)\}\n\s+border: \{x: (?P<bx>\S+), y: (?P<by>\S+), "
    r"z: (?P<bz>\S+), w: (?P<bw>\S+)\}", re.S)


def meta_slices(meta_path, texture_guid):
    """Slices stored inside the texture's .meta (regular Unity projects).
    Returns a list of slice dicts; empty for single sprites or ripped projects."""
    try:
        text = read_text(meta_path)
    except OSError:
        return []
    out = []
    section = re.search(r"\n    sprites:\n(.*?)\n    (?=[a-zA-Z])", text, re.S)
    if section:
        for m in META_SPRITE_RE.finditer(section.group(1)):
            al = int(m["al"])
            pivot = ALIGN_PIVOT.get(al, (float(m["px"]), float(m["py"])))
            out.append(dict(
                sprite_guid=f"{texture_guid}:{m['name'].strip()}", texture_guid=texture_guid,
                name=m["name"].strip(),
                rect=[float(m["x"]), float(m["y"]), float(m["w"]), float(m["h"])],
                pivot=list(pivot),
                border=[float(m["bx"]), float(m["by"]), float(m["bz"]), float(m["bw"])]))
    elif "spriteMode: 1" in text:
        b = re.search(r"spriteBorder: \{x: (\S+), y: (\S+), z: (\S+), w: (\S+)\}", text)
        if b and any(float(v) for v in b.groups()):
            out.append(dict(sprite_guid=texture_guid + ":self", texture_guid=texture_guid,
                            name="", rect=[0, 0, 0, 0], pivot=[0.5, 0.5],
                            border=[float(v) for v in b.groups()], self_only=True))
    return out


def parse_spine_atlas(text):
    """Parses a Spine atlas (3.x and 4.x text formats) into
    [(page_image_name, page_w, page_h, [region dicts])]."""
    pages, cur = [], None
    region = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            cur = None
            continue
        if cur is None:
            cur = {"image": line, "size": (0, 0), "regions": []}
            pages.append(cur)
            region = None
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip()
            nums = [v.strip() for v in val.split(",")]
            target = region if region is not None else cur
            if region is None and key == "size":
                cur["size"] = (int(nums[0]), int(nums[1]))
            elif region is not None:
                if key == "bounds":
                    region.update(x=int(nums[0]), y=int(nums[1]), w=int(nums[2]), h=int(nums[3]))
                elif key == "xy":
                    region.update(x=int(nums[0]), y=int(nums[1]))
                elif key == "size":
                    region.update(w=int(nums[0]), h=int(nums[1]))
                elif key == "rotate":
                    region["rotate"] = val not in ("false", "0")
            continue
        region = {"name": line, "rotate": False}
        cur["regions"].append(region)
    return [(p["image"], *p["size"], p["regions"]) for p in pages]


def scan_spine(textures):
    """Spine atlas pages become part sheets: one slice per packed region."""
    by_path = {t["path"]: t for t in textures.values()}
    by_name = {}  # fallback for ripped projects, where pages sit in another folder
    for t in textures.values():
        by_name.setdefault(os.path.basename(t["path"]).lower(), []).append(t)
    slices, pages = {}, 0
    for path in walk(SPINE_ATLAS_EXTS):
        folder = os.path.dirname(rel(path))
        for image, pw, ph, regions in parse_spine_atlas(read_text(path)):
            tex = by_path.get(f"{folder}/{image}")
            if not tex and len(by_name.get(image.lower(), [])) == 1:
                tex = by_name[image.lower()][0]
            if not tex:
                continue
            tex["kind"] = "spine"
            pages += 1
            W, H = tex["width"], tex["height"]
            # Region coordinates are in the page size the atlas declares; the PNG in the
            # project can be smaller (ripped/downscaled textures), and Spine samples it
            # by UV, so scale the regions to the real pixel size.
            sx = W / pw if pw else 1.0
            sy = H / ph if ph else 1.0
            for r in regions:
                if "w" not in r:
                    continue
                w, h = (r["h"], r["w"]) if r["rotate"] else (r["w"], r["h"])
                x, y, w, h = r["x"] * sx, r["y"] * sy, w * sx, h * sy
                slices[f"{tex['guid']}:{r['name']}"] = dict(
                    sprite_guid=f"{tex['guid']}:{r['name']}", texture_guid=tex["guid"],
                    name=r["name"] + (" (rotated)" if r["rotate"] else ""),
                    rect=[round(x), round(H - y - h), round(w), round(h)],
                    pivot=[0.5, 0.5], border=[0, 0, 0, 0])
    return slices, pages


def _vec(text, key, names):
    m = re.search(key + r": \{([^}]*)\}", text)
    if not m:
        return [0.0] * len(names)
    vals = dict(p.split(": ") for p in m.group(1).split(", ") if ": " in p)
    return [float(vals.get(n, 0)) for n in names]


def scan_sprites():
    """Standalone Sprite assets (AssetRipper layout): one .asset per slice."""
    sprites = {}
    for path in walk((".asset",)):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
        if "--- !u!213 " not in head:
            continue
        guid = meta_guid(path)
        tex = re.search(r"texture: \{fileID: \d+, guid: ([0-9a-f]{32})", head)
        if not tex:
            tex = re.search(r"texture: \{fileID: \d+, guid: ([0-9a-f]{32})",
                            read_text(path))
        rect = re.search(
            r"m_Rect:\s+serializedVersion: \d+\s+x: (\S+)\s+y: (\S+)\s+"
            r"width: (\S+)\s+height: (\S+)", head)
        name = re.search(r"m_Name: (.*)", head)
        if not (guid and tex and rect):
            continue
        sprites[guid] = dict(
            sprite_guid=guid, texture_guid=tex.group(1),
            name=name.group(1).strip() if name else "",
            rect=[float(v) for v in rect.groups()],
            pivot=_vec(head, "m_Pivot", "xy"),
            border=_vec(head, "m_Border", "xyzw"))
    return sprites


def classify_ref(ext, class_id, script_guid):
    if ext == ".mat":
        return "mat"
    if ext == ".anim":
        return "anim"
    if class_id == "212":
        return "world"
    if class_id in ("198", "199"):
        return "vfx"
    if class_id == "114":
        if script_guid in UI_IMAGE_SCRIPTS:
            return "ui"
        return "data" if ext == ".asset" else "script"
    return "script"


def scan_usages(textures, sprites):
    """Finds every file that references a texture, directly or via a slice."""
    target = {g: g for g in textures}
    target.update({g: s["texture_guid"] for g, s in sprites.items()})
    mat_textures = {}  # material guid -> texture guids
    usages = set()
    files = [p for p in walk(USAGE_EXTS)]
    for path in files:
        ext = os.path.splitext(path)[1]
        text = read_text(path)
        if ext == ".asset" and "--- !u!213 " in text[:4096]:
            continue  # sprite assets reference their own texture
        r = rel(path)
        class_id, doc_start, script = None, 0, None
        for m in TOKEN_RE.finditer(text):
            if m.group(1):
                class_id, doc_start, script = m.group(1), m.start(), None
                continue
            tex = target.get(m.group(2))
            if not tex or tex not in textures:
                continue
            if class_id == "114" and script is None:
                s = SCRIPT_RE.search(text, doc_start, m.start())
                script = s.group(1) if s else ""
            usages.add((tex, classify_ref(ext, class_id, script), r))
            if ext == ".mat":
                mat_textures.setdefault(meta_guid(path), set()).add(tex)
    # Textures used through a material on a particle system count as VFX.
    if mat_textures:
        for path in files:
            if not path.endswith((".prefab", ".unity")):
                continue
            text = read_text(path)
            class_id = None
            for m in TOKEN_RE.finditer(text):
                if m.group(1):
                    class_id = m.group(1)
                elif class_id in ("198", "199") and m.group(2) in mat_textures:
                    for tex in mat_textures[m.group(2)]:
                        usages.add((tex, "vfx", rel(path)))
    return usages


# Categorization rules live in rules.json so each game can have its own.
RULES = _load_json("rules.json", {
    "skip_name": r"^(sactx-|resize|unity|default|font|lut|noise|mask|white$|black$|pixel$|"
                 r"square$|circle$|gradient)|( atlas|@2x)$",
    "vfx_name": r"(particle|glow|flare|smoke|spark|trail|fume|shine|flash|fx_|_fx|explosion|beam|dust|blast)",
    "ui_name": r"(icon|background|portrait|badge|button|popup|banner|logo|arrow|check)",
    "data_hints": [], "ui_sub": [], "game_sub": [], "spine_sub": [], "path_rules": [],
    "resources_folder_as": ["Game", "other"], "unused_as_skip": True,
})
SKIP_NAME = re.compile(RULES["skip_name"], re.I)
VFX_NAME = re.compile(RULES["vfx_name"], re.I)
UI_NAME = re.compile(RULES["ui_name"], re.I)
DATA_HINTS = [tuple(p) for p in RULES["data_hints"]]
UI_SUB = [tuple(p) for p in RULES["ui_sub"]]
GAME_SUB = [tuple(p) for p in RULES["game_sub"]]
SPINE_SUB = [tuple(p) for p in RULES["spine_sub"]]
PATH_RULES = [tuple(p) for p in RULES["path_rules"]]


def _sub(name, table, default):
    for label, pattern in table:
        if re.search(pattern, name, re.I):
            return label
    return default


def categorize(a):
    """Returns (category, subcategory, handling mode) for one asset."""
    name = a["name"]
    ui = a["ui_refs"]
    world = a["world_refs"]
    vfx = a["vfx_refs"]
    if SKIP_NAME.search(name):
        return "Skip", "utility", "skip"
    if a.get("kind") == "spine":
        return "Game", "spine " + _sub(name, SPINE_SUB, "characters"), "part_sheet"
    for prefix, pcat, psub in PATH_RULES:
        if a["path"].startswith(prefix):
            category, sub = pcat, psub
            break
    else:
        category, sub = _categorize_by_usage(a)
    if category in ("Unsorted", "Skip", "VFX"):
        return category, sub, "skip"
    if a["grayscale"] or a["has_border"]:
        mode = "recolor"
    elif a["slice_count"] > 1 and category == "Game":
        mode = "part_sheet"
    else:
        mode = "regenerate"
    return category, sub, mode


def _categorize_by_usage(a):
    name = a["name"]
    ui = a["ui_refs"]
    world = a["world_refs"]
    vfx = a["vfx_refs"]
    if vfx > max(ui, world) or (VFX_NAME.search(name) and vfx >= 0 and ui + world == 0):
        return "VFX", "particles"
    if ui > world:
        default = "icons" if max(a["width"], a["height"]) <= 256 else "other"
        category, sub = "UI", _sub(name, UI_SUB, default)
    elif world > 0:
        default = "characters" if a["slice_count"] > 1 else "other"
        category, sub = "Game", _sub(name, GAME_SUB, default)
    elif a["path"].startswith("Assets/Resources/"):
        category, sub = RULES["resources_folder_as"]
    elif a["total_refs"] == 0 and RULES["unused_as_skip"]:
        return "Unsorted", "unused"
    elif UI_NAME.search(name):
        default = "icons" if max(a["width"], a["height"]) <= 256 else "other"
        category, sub = "UI", _sub(name, UI_SUB, default)
    else:
        category, sub = "Game", a.get("hint") or _sub(name, GAME_SUB, "other")
    return category, sub


def validate_project():
    if not os.path.isfile(os.path.join(TOOL_DIR, "project.json")):
        raise ValueError('Run python init_project.py --project "PATH/TO/UNITY/PROJECT" first.')
    if not os.path.isdir(ASSETS_DIR) or not os.path.isdir(os.path.join(PROJECT_ROOT, "ProjectSettings")):
        raise ValueError("Configured Unity project must contain Assets/ and ProjectSettings/.")


def scan(log=print):
    validate_project()
    t0 = time.time()
    log("Scanning textures...")
    textures = scan_textures()
    log(f"  {len(textures)} textures")
    log("Scanning sprite slices...")
    sprites = scan_sprites()
    for t in textures.values():  # slices stored inside .meta files
        for s in t.pop("meta_slices"):
            if s.get("self_only"):
                t["meta_border"] = 1
            else:
                sprites.setdefault(s["sprite_guid"], s)
    spine, pages = scan_spine(textures)
    sprites.update(spine)
    log(f"  {len(sprites)} sprite slices, {pages} Spine atlas pages")
    log("Scanning prefabs, scenes and data for references...")
    usages = scan_usages(textures, sprites)
    log(f"  {len(usages)} references")

    slices_by_tex = {}
    for s in sprites.values():
        slices_by_tex.setdefault(s["texture_guid"], []).append(s)
    first_by_md5 = {}
    for a in sorted(textures.values(), key=lambda t: t["path"]):
        a["dup_of"] = first_by_md5.setdefault(a["md5"], a["guid"])
        if a["dup_of"] == a["guid"]:
            a["dup_of"] = None
        sl = slices_by_tex.get(a["guid"], [])
        a["slice_count"] = len(sl)
        a["has_border"] = int(any(any(s["border"]) for s in sl) or a.get("meta_border", 0))
        for kind in ("ui", "world", "vfx", "script", "data", "mat", "anim"):
            a[kind + "_refs"] = 0
    for tex, kind, file in usages:
        textures[tex][kind + "_refs"] += 1
        if kind == "data" and "hint" not in textures[tex]:
            hint = _sub(os.path.basename(file), DATA_HINTS, None)
            if hint:
                textures[tex]["hint"] = hint
    for a in textures.values():
        a["total_refs"] = sum(a[k + "_refs"] for k in
                              ("ui", "world", "vfx", "script", "data", "mat", "anim"))
        a["category"], a["subcategory"], a["mode"] = categorize(a)

    db = connect()
    manual = {r["guid"] for r in db.execute("SELECT guid FROM assets WHERE manual=1")}
    auto_cols = ["path", "name", "width", "height", "bytes", "md5", "dup_of", "kind",
                 "has_alpha", "grayscale", "slice_count", "has_border", "ui_refs",
                 "world_refs", "vfx_refs", "script_refs", "data_refs", "mat_refs",
                 "anim_refs", "total_refs"]
    class_cols = ["category", "subcategory", "mode"]
    db.execute("UPDATE assets SET missing=1")
    for a in textures.values():
        cols = auto_cols + ([] if a["guid"] in manual else class_cols)
        db.execute(
            f"INSERT INTO assets (guid, {', '.join(auto_cols + class_cols)}) "
            f"VALUES (:guid, {', '.join(':' + c for c in auto_cols + class_cols)}) "
            f"ON CONFLICT(guid) DO UPDATE SET missing=0, "
            + ", ".join(f"{c}=excluded.{c}" for c in cols), a)
    db.execute("DELETE FROM slices")
    db.executemany(
        "INSERT INTO slices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(s["sprite_guid"], s["texture_guid"], s["name"], *s["rect"], *s["pivot"],
          *s["border"]) for s in sprites.values()])
    db.execute("DELETE FROM usages")
    db.executemany("INSERT INTO usages VALUES (?,?,?)", sorted(usages))
    db.commit()
    db.close()
    log(f"Done in {time.time() - t0:.1f}s")
    return len(textures)


if __name__ == "__main__":
    scan()
