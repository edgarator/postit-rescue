#!/usr/bin/env python3
"""Turn Post-it(R) App boards (.postit files) into an open, offline archive.

    python3 postit_export.py [SOURCE_DIR] [OUTPUT_DIR]

SOURCE_DIR defaults to the app's iCloud Drive folder, OUTPUT_DIR to "Post-it Archive"
next to this script. Standard library only. Re-running rebuilds the archive.

A .postit file is a ZIP archive (format versions 4.2-7.0 all share this layout):
  metadata                   JSON: format version + UUID lists for the files below
  sheet-<uuid>.json          the board: clusters (groups) -> notes, with layout and text
  note-<uuid>-enhanced.jpg   each note as the app shows it (ink and typed text baked in)
  edit-<uuid>.png            editable background layer of a note
  edit-drawing-<uuid>.png    transparent ink layer of a note
  capture-<uuid>.jpg/.json   original photo of the wall, plus detection data and location
  ocr-<uuid>.json            OCR request log (the recognised text lives in the sheet)
  sheet-preview.png          1024px render of the board, made by the app
  sheet-thumbnail.png        small render
  index.json                 file hashes (newer versions only)

Layout: board units are millimetres (a 3x3" note is 76.2). "grid" and "snap-to-grid"
clusters place notes in cells by layoutIndex (row-major, `width` columns, 90-unit pitch)
starting at (positionX, positionY). "free-form" clusters place each note at
(positionX + centerX, positionY + centerY), rotated by layoutRotation (radians).
"""
import colorsys, csv, datetime as dt, json, math, re, shutil, sys, unicodedata, zipfile
from pathlib import Path

if {"-h", "--help"} & set(sys.argv[1:]):
    print(__doc__)
    sys.exit(0)

HERE = Path(__file__).resolve().parent
SRC = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else \
    Path.home() / "Library/Mobile Documents/iCloud~com~mmm~post-it/Documents"
OUT = Path(sys.argv[2]).expanduser() if len(sys.argv) > 2 else HERE / "Post-it Archive"

NOTE = 76.2            # a 3x3" note, in board units
GAP = 13.8             # grid pitch = NOTE + GAP = 90, measured from the app's own renders
PAD, PAD_TOP = 22, 34  # group frame padding
MARGIN = 40
SCALE = 2              # px per board unit in the HTML viewer


def nfc(s):
    return unicodedata.normalize("NFC", s or "")


def safe(s, fallback):
    s = re.sub(r'[\x00-\x1f<>:"/\\|?*#%]+', "-", nfc(s)).strip(" .-")
    return s[:80] or fallback


def unique(name, used):
    out, k = name, 2
    while out.lower() in used:
        out, k = f"{name} ({k})", k + 1
    used.add(out.lower())
    return out


def hexcolor(c):
    if not isinstance(c, dict):
        return ""
    r, g, b = (max(0.0, min(1.0, float(c.get(k) or 0))) for k in ("red", "green", "blue"))
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def colorname(hx):
    if not hx:
        return ""
    r, g, b = (int(hx[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    d = h * 360
    if s < 0.12:
        return "white" if v > 0.8 else "grey"
    if d < 15 or d >= 340:
        return "pink" if s < 0.5 else "red"
    for limit, name in ((42, "orange"), (70, "yellow"), (170, "green"), (255, "blue"), (300, "purple")):
        if d < limit:
            return name
    return "pink"


def note_text(n):
    typed = (n.get("noteEditInfo") or {}).get("text")
    if isinstance(typed, dict):
        typed = typed.get("contents")
    for val, src in ((n.get("userProvidedText"), "edited"), (typed, "typed"),
                     ((n.get("ocr") or {}).get("text"), "ocr")):
        if isinstance(val, str) and val.strip():
            return val.strip(), src
    return "", ""


def layout(clusters):
    """Place every note in board units. Returns groups with a frame box and placed notes."""
    groups = []
    for c in clusters:
        kind = c.get("layoutType") or "grid"
        x0, y0 = float(c.get("positionX") or 0), float(c.get("positionY") or 0)
        notes = sorted(c.get("notes") or [], key=lambda n: (n.get("layoutIndex", 0), n.get("index", 0)))
        sizes = [(float(n.get("width") or NOTE), float(n.get("height") or NOTE)) for n in notes]
        placed = []
        if kind == "free-form":
            for n, (w, h) in zip(notes, sizes):
                cx, cy = x0 + float(n.get("centerX") or 0), y0 + float(n.get("centerY") or 0)
                placed.append((n, cx - w / 2, cy - h / 2, w, h, math.degrees(float(n.get("layoutRotation") or 0))))
            box = [x0, y0, x0 + float(c.get("width") or 0), y0 + float(c.get("height") or 0)]
        else:
            cols = max(1, int(round(float(c.get("width") or 1))))
            rows = max(1, int(round(float(c.get("height") or 1))))
            pw = max([w for w, _ in sizes] + [NOTE]) + GAP
            ph = max([h for _, h in sizes] + [NOTE]) + GAP
            for i, (n, (w, h)) in enumerate(zip(notes, sizes)):
                cell = int(n.get("layoutIndex", i)) if kind == "snap-to-grid" else i
                col, row = cell % cols, cell // cols
                rows = max(rows, row + 1)
                placed.append((n, x0 + col * pw, y0 + row * ph, w, h, 0.0))
            box = [x0, y0, x0 + cols * pw - GAP, y0 + rows * ph - GAP]
        for _, x, y, w, h, _ in placed:  # the frame must hold every note
            box = [min(box[0], x), min(box[1], y), max(box[2], x + w), max(box[3], y + h)]
        frame = [box[0] - PAD, box[1] - PAD_TOP, box[2] + PAD, box[3] + PAD]
        groups.append({"name": nfc(c.get("name")).strip() or "Untitled group", "layout": kind,
                       "frame": frame, "placed": placed})
    return groups


def board_date(z, sheet):
    stamps = []
    for i in z.infolist():
        if i.filename.startswith(("note-", "capture-")):
            try:
                stamps.append(dt.datetime(*i.date_time))
            except ValueError:
                pass
    if stamps:
        return min(stamps)
    try:
        return dt.datetime.strptime(sheet.get("creationDate", ""), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def js(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def export_board(path, used_dirs):
    with zipfile.ZipFile(path) as z:
        return _export_board(z, path, used_dirs)


def _export_board(z, path, used_dirs):
    names = set(z.namelist())
    meta = json.loads(z.read("metadata"))
    sheet = json.loads(z.read(next(n for n in z.namelist() if n.startswith("sheet-") and n.endswith(".json"))))
    title = nfc(path.stem)
    folder = unique(safe(title, "Board"), used_dirs)
    bdir = OUT / "boards" / folder
    if bdir.exists():
        shutil.rmtree(bdir)
    (bdir / "notes").mkdir(parents=True)

    if "sheet-preview.png" in names:
        (bdir / "preview.png").write_bytes(z.read("sheet-preview.png"))

    infos = {i.filename: i for i in z.infolist()}
    captures = sorted((u for u in meta.get("captureUUIDs") or [] if f"capture-{u}.jpg" in names),
                      key=lambda u: infos[f"capture-{u}.jpg"].date_time)
    photos = {}
    if captures:
        (bdir / "photos").mkdir()
    for i, u in enumerate(captures, 1):
        photos[u] = f"photos/photo-{i:02d}.jpg"
        (bdir / photos[u]).write_bytes(z.read(f"capture-{u}.jpg"))

    groups = layout(sheet.get("clusters") or [])
    frames = [g["frame"] for g in groups]
    ox = (min(f[0] for f in frames) if frames else 0) - MARGIN
    oy = (min(f[1] for f in frames) if frames else 0) - MARGIN
    width = (max(f[2] for f in frames) - ox + MARGIN) if frames else 400
    height = (max(f[3] for f in frames) - oy + MARGIN) if frames else 300

    used_groups, rows, view_groups, json_groups = set(), [], [], []
    for gi, g in enumerate(groups, 1):
        gfolder = f"notes/{gi:02d} {unique(safe(g['name'], 'Group'), used_groups)}"
        (bdir / gfolder).mkdir()
        fx0, fy0, fx1, fy1 = g["frame"]
        vg = {"name": g["name"], "x": (fx0 - ox) * SCALE, "y": (fy0 - oy) * SCALE,
              "w": (fx1 - fx0) * SCALE, "h": (fy1 - fy0) * SCALE, "notes": []}
        jg = {"name": g["name"], "layout": g["layout"], "notes": []}
        for ni, (n, x, y, w, h, rot) in enumerate(g["placed"], 1):
            img = ""
            member = f"note-{n.get('contentUUID')}-enhanced.jpg"
            if member in names:
                img = f"{gfolder}/{ni:03d}.jpg"
                (bdir / img).write_bytes(z.read(member))
            text, source = note_text(n)
            hx = hexcolor(n.get("backgroundColor") or (n.get("noteEditInfo") or {}).get("backgroundColor"))
            kind = "digital" if n.get("isDigitalNote") else "photo"
            conf = (n.get("ocr") or {}).get("confidence") if source == "ocr" else None
            photo = photos.get(n.get("board"), "")
            size = n.get("sizeClass") or "3x3"
            vg["notes"].append({"img": img, "x": round((x - ox) * SCALE, 1), "y": round((y - oy) * SCALE, 1),
                                "w": round(w * SCALE, 1), "h": round(h * SCALE, 1), "r": round(rot, 2),
                                "z": int(n.get("layoutZOrder") or 0), "text": text, "src": source,
                                "conf": None if conf is None else round(conf, 2), "color": hx,
                                "cname": colorname(hx), "kind": kind, "photo": photo})
            jg["notes"].append({"n": ni, "text": text, "text_source": source, "ocr_confidence": conf,
                                "color": colorname(hx), "color_hex": hx, "kind": kind, "size": size,
                                "image": img, "photo": photo, "x": round(x - ox, 2), "y": round(y - oy, 2),
                                "w": w, "h": h, "rotation_deg": round(rot, 3),
                                "uuid": n.get("UUID"), "content_uuid": n.get("contentUUID")})
            rows.append([title, g["name"], ni, text, source, "" if conf is None else round(conf, 2),
                         colorname(hx), hx, kind, size, img, photo])
        view_groups.append(vg)
        json_groups.append(jg)

    date = board_date(z, sheet)
    version = "%s.%s" % ((meta.get("version") or {}).get("major"), (meta.get("version") or {}).get("minor"))
    # The app sometimes saved its preview before note images loaded (flat grey squares, a few KB).
    # Those, and missing previews, get a mosaic of the board's first notes on the index instead.
    preview = bdir / "preview.png"
    stale = rows and (not preview.exists() or preview.stat().st_size < 200 * len(rows))
    summary = {"dir": folder, "name": title, "date": date.strftime("%Y-%m-%d") if date else "",
               "notes": len(rows), "groups": len(groups), "photos": len(photos),
               "thumb": "preview.png" if rows and preview.exists() and not stale else "",
               "mosaic": [r[10] for r in rows if r[10]][:9] if stale else []}

    (bdir / "board.json").write_text(json.dumps(
        {"board": title, "name_inside_file": sheet.get("name"), "date": summary["date"],
         "source_file": path.name, "app_format_version": version, "units": "mm (a 3x3in note is 76.2)",
         "groups": json_groups, "photos": sorted(photos.values())}, ensure_ascii=False, indent=1), "utf-8")
    with open(bdir / "notes.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        w.writerows(rows)
    view = {"name": title, "date": summary["date"], "w": width * SCALE, "h": height * SCALE,
            "groups": view_groups, "photos": len(photos), "preview": summary["thumb"]}
    (bdir / "board.html").write_text(BOARD_HTML.replace("__TITLE__", _esc(title)).replace("__DATA__", js(view)), "utf-8")
    return summary, rows, view_groups


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


CSV_HEADER = ["board", "group", "note", "text", "text_source", "ocr_confidence", "color", "color_hex",
              "kind", "size", "image", "photo"]


def copy_originals():
    dst = OUT / "originals"
    dst.mkdir(parents=True, exist_ok=True)
    files = sorted(SRC.glob("*.postit")) if SRC.is_dir() else []
    if not files and not any(dst.glob("*.postit")):
        sys.exit(f"No .postit files found in {SRC}\n"
                 f"Pass the folder that holds them:  python3 postit_export.py /path/to/folder")
    if not files:
        print(f"  {SRC} not available; rebuilding from the copies in {dst}")
    for f in files:
        target = dst / f.name
        if not (target.exists() and target.stat().st_size == f.stat().st_size
                and int(target.stat().st_mtime) == int(f.stat().st_mtime)):
            shutil.copy2(f, target)
        with zipfile.ZipFile(target) as z:  # fails loudly on a truncated copy
            json.loads(z.read("metadata"))
    return sorted(dst.glob("*.postit"), key=lambda p: nfc(p.stem).lower())


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"Copying originals from {SRC} ...")
    originals = copy_originals()
    print(f"  {len(originals)} boards copied and verified")
    used, boards, all_rows, search = set(), [], [], []
    for i, f in enumerate(originals, 1):
        summary, rows, vgroups = export_board(f, used)
        boards.append(summary)
        all_rows += rows
        for gi, g in enumerate(vgroups):
            for ni, n in enumerate(g["notes"]):
                search.append([len(boards) - 1, g["name"], n["text"],
                               f"boards/{summary['dir']}/{n['img']}" if n["img"] else "", f"g{gi}-{ni}"])
        print(f"  [{i:3}/{len(originals)}] {summary['name']}: {summary['notes']} notes, "
              f"{summary['groups']} groups, {summary['photos']} photos")
    with open(OUT / "all-notes.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        w.writerows([r[:10] + [f"boards/{safe_dir(boards, r[0])}/{r[10]}" if r[10] else "",
                               f"boards/{safe_dir(boards, r[0])}/{r[11]}" if r[11] else ""] for r in all_rows])
    stats = {"boards": len(boards), "notes": len(all_rows), "photos": sum(b["photos"] for b in boards),
             "with_text": sum(1 for r in all_rows if r[3]), "exported": f"{dt.date.today():%d %b %Y}".lstrip("0")}
    (OUT / "index.html").write_text(INDEX_HTML.replace("__DATA__", js(
        {"stats": stats, "boards": boards, "notes": search})), "utf-8")
    (OUT / "README.md").write_text(README.format(**stats), "utf-8")
    print(f"\nDone: {stats['boards']} boards, {stats['notes']} notes ({stats['with_text']} with text), "
          f"{stats['photos']} photos -> {OUT}")


def safe_dir(boards, title):
    for b in boards:
        if b["name"] == title:
            return b["dir"]
    return ""


README = """# Post-it Archive

Everything from the Post-it App, exported {exported}: {boards} boards, {notes} notes
({with_text} with recognised or typed text), and {photos} original wall photos.

- **index.html** – open this. Browse every board and search the text of every note.
- **boards/<board>/board.html** – the board laid out as it was in the app. Drag to pan,
  pinch or Cmd+scroll to zoom, click a note to enlarge it.
- **boards/<board>/notes/** – every note as a 1024px JPEG, one folder per group, in board order.
- **boards/<board>/photos/** – the original photos you captured the notes from.
- **boards/<board>/preview.png** – the app's own render of the board.
- **boards/<board>/notes.csv** and **all-notes.csv** – the text of every note, with group,
  colour and image path. Opens in Excel or Numbers.
- **boards/<board>/board.json** – the same data, with positions in millimetres, for importing elsewhere.
- **originals/** – untouched copies of the .postit files. Each one is a plain ZIP archive.

Note text comes from the app's own handwriting recognition, so it is only as good as the
app's OCR was; the note images are the source of truth.

This folder was made with postit_export.py from the postit-rescue project. To rebuild it
from the copies in originals/, run:

    python3 postit_export.py "Post-it Archive/originals" "Post-it Archive"
"""

BOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f5f1;--canvas:#efeee9;--dot:#dcdad2;--ink:#1c1c1a;--muted:#6d6c66;--line:#d6d4cc;--panel:#fffffe;
--frame:#a3a198;--accent:#0b62d6;--hit:#ff5a1f;--scrim:rgba(20,20,18,.72)}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--canvas:#1b1b1a;--dot:#2b2b29;--ink:#ecebe5;--muted:#9c9b94;
--line:#34342f;--panel:#1f1f1d;--frame:#5f5e58;--accent:#6aa8ff;--hit:#ff7a45;--scrim:rgba(0,0,0,.8)}}
*{box-sizing:border-box}
html,body{margin:0;height:100%;overflow:hidden;background:var(--bg);color:var(--ink);
font:14px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}
header{position:fixed;inset:0 0 auto;height:56px;display:flex;align-items:center;gap:10px;padding:0 14px;
background:var(--panel);border-bottom:1px solid var(--line);z-index:10}
header .back{white-space:nowrap;font-weight:500}
header h1{font-size:15px;margin:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:0 1 auto;min-width:48px}
header .meta{color:var(--muted);white-space:nowrap;font-size:13px}
header .sp{flex:1}
input[type=search]{width:220px;max-width:36vw;height:32px;border:1px solid var(--line);border-radius:8px;padding:0 10px;
background:var(--bg);color:var(--ink);font:inherit}
button,.btn{height:32px;min-width:32px;padding:0 10px;border:1px solid var(--line);border-radius:8px;background:var(--panel);
color:var(--ink);font:inherit;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;white-space:nowrap}
button[aria-pressed=true]{background:var(--ink);color:var(--panel);border-color:var(--ink)}
#count{color:var(--muted);font-size:13px;min-width:0;white-space:nowrap}
#vp{position:fixed;inset:56px 0 0;overflow:hidden;cursor:grab;touch-action:none;
background-color:var(--canvas);background-image:radial-gradient(var(--dot) 1px,transparent 1.2px);background-size:22px 22px}
#vp.drag{cursor:grabbing}
#world{position:absolute;left:0;top:0;transform-origin:0 0}
.group{position:absolute;border:1.5px solid var(--frame);border-radius:14px;pointer-events:none}
.group .label{position:absolute;left:50%;top:0;transform:translate(-50%,-55%);max-width:92%;padding:0 12px;
background:var(--canvas);color:var(--muted);font-size:28px;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.note{position:absolute;display:block;box-shadow:0 1px 2px rgba(0,0,0,.22),0 3px 10px rgba(0,0,0,.10);cursor:pointer;
user-select:none;-webkit-user-drag:none;object-fit:cover;transition:opacity .15s}
.note.blank{display:flex;align-items:center;justify-content:center;text-align:center;padding:10px;font-size:18px;
white-space:pre-wrap;overflow:hidden;color:#222}
.searching .note{opacity:.18}
.searching .note.hit{opacity:1;outline:6px solid var(--hit);outline-offset:3px}
.note.pulse{animation:pulse 1.2s ease-out 1}
@keyframes pulse{0%{outline:10px solid var(--hit);outline-offset:14px}100%{outline:6px solid var(--hit);outline-offset:3px}}
#empty{position:fixed;inset:56px 0 0;display:none;align-items:center;justify-content:center;color:var(--muted)}
#list{position:fixed;inset:56px 0 0;overflow:auto;padding:24px clamp(16px,4vw,48px) 60px;background:var(--bg);display:none}
body.listing #list{display:block}
body.listing #vp{display:none}
#list h2{font-size:15px;margin:28px 0 12px;display:flex;gap:8px;align-items:baseline}
#list h2 span{color:var(--muted);font-weight:400;font-size:13px}
#list .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px}
#list .card{cursor:pointer;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
#list .card img{display:block;width:100%;aspect-ratio:1;object-fit:cover}
#list .card p{margin:0;padding:8px 10px;font-size:12.5px;white-space:pre-wrap;color:var(--ink);max-height:7.5em;overflow:hidden}
#list .card p:empty::before{content:"No text";color:var(--muted)}
#list .card.hide{display:none}
#modal{position:fixed;inset:0;background:var(--scrim);display:none;z-index:20;align-items:center;justify-content:center;padding:24px}
#modal.open{display:flex}
#modal .sheet{position:relative;display:flex;gap:24px;align-items:flex-start;max-width:min(1180px,96vw);max-height:92vh;
background:var(--panel);border-radius:16px;padding:20px;box-shadow:0 20px 60px rgba(0,0,0,.35)}
#m-img{display:block;max-width:min(78vh,62vw);max-height:78vh;border-radius:6px}
#modal .info{width:300px;max-height:78vh;overflow:auto;display:flex;flex-direction:column;gap:12px}
#m-crumbs{color:var(--muted);font-size:12.5px}
#m-text{font-size:16px;white-space:pre-wrap;margin:0}
#m-text.none{color:var(--muted);font-style:italic;font-size:14px}
#m-meta{color:var(--muted);font-size:12.5px;display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center}
.sw{display:inline-block;width:11px;height:11px;border-radius:3px;vertical-align:-1px;border:1px solid rgba(0,0,0,.2);margin-right:4px}
#modal .actions{display:flex;flex-wrap:wrap;gap:8px}
#modal .nav{position:absolute;top:50%;transform:translateY(-50%);width:40px;height:56px;font-size:22px}
#m-prev{left:-52px}#m-next{right:-52px}
#m-close{position:absolute;top:-14px;right:-14px;border-radius:50%;width:32px;padding:0}
body.shot header{display:none}
body.shot #vp{inset:0;background-image:none}
@media (max-width:760px){
 header{gap:6px;padding:0 10px}header .meta,#count,#zo,#zi,#prev,.back .lbl{display:none}input[type=search]{width:110px;flex:0 1 110px;min-width:70px}
 #modal .sheet{flex-direction:column;padding:14px}#m-img{max-width:84vw;max-height:52vh}#modal .info{width:auto;max-height:30vh}
 #m-prev{left:4px}#m-next{right:4px}}
</style>
</head>
<body>
<header>
 <a class="back" href="../../index.html" aria-label="All boards">&larr;<span class="lbl"> All boards</span></a>
 <h1 id="title"></h1><span class="meta" id="meta"></span>
 <span class="sp"></span>
 <input type="search" id="q" placeholder="Search notes" aria-label="Search notes"><span id="count"></span>
 <button id="view" aria-pressed="false" title="Show as a list">List</button>
 <button id="zo" title="Zoom out (-)">&minus;</button><button id="zi" title="Zoom in (+)">+</button>
 <button id="fit" title="Fit board (0)">Fit</button>
 <a class="btn" id="prev" target="_blank" title="The app's own render of this board">App render</a>
</header>
<div id="vp"><div id="world"></div></div>
<div id="empty">This board is empty.</div>
<section id="list"></section>
<div id="modal" role="dialog" aria-modal="true">
 <div class="sheet">
  <img id="m-img" alt="">
  <div class="info">
   <div id="m-crumbs"></div><p id="m-text"></p><div id="m-meta"></div>
   <div class="actions"><a class="btn" id="m-open" target="_blank">Open image</a><a class="btn" id="m-photo" target="_blank">Original photo</a></div>
  </div>
  <button class="nav" id="m-prev" aria-label="Previous note">&lsaquo;</button>
  <button class="nav" id="m-next" aria-label="Next note">&rsaquo;</button>
  <button id="m-close" aria-label="Close">&times;</button>
 </div>
</div>
<script>
const B = __DATA__;
const $ = id => document.getElementById(id);
const vp = $('vp'), world = $('world'), url = p => encodeURI(p);
const fold = s => (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
const pl = (n, w) => n + ' ' + w + (n === 1 ? '' : 's');
const SHOT = location.search.includes('shot');
if (SHOT) document.body.classList.add('shot');
$('title').textContent = B.name;
document.title = B.name + ' · Post-it Archive';
const total = B.groups.reduce((a, g) => a + g.notes.length, 0);
$('meta').textContent = [B.date && new Date(B.date + 'T12:00').toLocaleDateString(undefined, {day: 'numeric', month: 'short', year: 'numeric'}),
  pl(total, 'note'), pl(B.groups.length, 'group'), B.photos ? pl(B.photos, 'photo') : ''].filter(Boolean).join(' · ');
if (B.preview) $('prev').href = url(B.preview); else $('prev').style.display = 'none';

// ---- build board + list
const notes = [], els = [], cards = [];
world.style.width = B.w + 'px'; world.style.height = B.h + 'px';
B.groups.forEach((g, gi) => {
  const f = document.createElement('div'); f.className = 'group';
  f.style.cssText = `left:${g.x}px;top:${g.y}px;width:${g.w}px;height:${g.h}px`;
  const lab = document.createElement('div'); lab.className = 'label'; lab.textContent = g.name; f.append(lab); world.append(f);
  const h2 = document.createElement('h2'); h2.textContent = g.name;
  const n2 = document.createElement('span'); n2.textContent = pl(g.notes.length, 'note'); h2.append(n2);
  const grid = document.createElement('div'); grid.className = 'cards'; $('list').append(h2, grid);
  g.notes.forEach((n, ni) => {
    const i = notes.length; notes.push({...n, g: g.name, gi, ni, of: g.notes.length});
    let e;
    if (n.img) { e = new Image(); e.src = url(n.img); e.alt = n.text || `${g.name}, note ${ni + 1}`; e.draggable = false; }
    else { e = document.createElement('div'); e.textContent = n.text; e.classList.add('blank'); e.style.background = n.color || '#fbe36b'; }
    e.classList.add('note'); e.dataset.i = i;
    e.style.cssText += `left:${n.x}px;top:${n.y}px;width:${n.w}px;height:${n.h}px;z-index:${n.z}` + (n.r ? `;transform:rotate(${n.r}deg)` : '');
    world.append(e); els.push(e);
    const c = document.createElement('div'); c.className = 'card'; c.dataset.i = i;
    if (n.img) { const im = new Image(); im.loading = 'lazy'; im.src = url(n.img); im.alt = ''; c.append(im); }
    const p = document.createElement('p'); p.textContent = n.text; c.append(p); grid.append(c); cards.push(c);
  });
});
if (!total && !B.groups.length) { vp.style.display = 'none'; $('empty').style.display = 'flex'; }

// ---- pan & zoom
let k = 1, tx = 0, ty = 0;
const apply = () => { world.style.transform = `translate(${tx}px,${ty}px) scale(${k})`; };
function fit() {
  const r = vp.getBoundingClientRect();
  k = Math.min(r.width / B.w, r.height / B.h) * (SHOT ? 0.98 : 0.96); if (!SHOT) k = Math.min(k, 1.5);
  tx = (r.width - B.w * k) / 2; ty = (r.height - B.h * k) / 2; apply();
}
function zoomAt(f, cx, cy) {
  const nk = Math.max(0.02, Math.min(8, k * f)); f = nk / k; k = nk;
  tx = cx - (cx - tx) * f; ty = cy - (cy - ty) * f; apply();
}
function zoomCenter(f) { const r = vp.getBoundingClientRect(); zoomAt(f, r.width / 2, r.height / 2); }
function focusNote(i) {
  const n = notes[i], r = vp.getBoundingClientRect();
  k = Math.max(k, Math.min(1.2, r.height / (n.h * 3)));
  tx = r.width / 2 - (n.x + n.w / 2) * k; ty = r.height / 2 - (n.y + n.h / 2) * k; apply();
  els[i].classList.remove('pulse'); void els[i].offsetWidth; els[i].classList.add('pulse');
}
vp.addEventListener('wheel', e => {
  e.preventDefault();
  const r = vp.getBoundingClientRect();
  if (e.ctrlKey || e.metaKey) zoomAt(Math.exp(-e.deltaY * (e.ctrlKey ? 0.012 : 0.002)), e.clientX - r.left, e.clientY - r.top);
  else { tx -= e.deltaX; ty -= e.deltaY; apply(); }
}, {passive: false});
let down = null;
vp.addEventListener('pointerdown', e => { down = {x: e.clientX, y: e.clientY, tx, ty, t: e.target, moved: false}; vp.setPointerCapture(e.pointerId); });
vp.addEventListener('pointermove', e => {
  if (!down) return;
  const dx = e.clientX - down.x, dy = e.clientY - down.y;
  if (!down.moved && Math.hypot(dx, dy) < 4) return;
  down.moved = true; vp.classList.add('drag'); tx = down.tx + dx; ty = down.ty + dy; apply();
});
vp.addEventListener('pointerup', () => {
  if (down && !down.moved && down.t.dataset && down.t.dataset.i) openNote(+down.t.dataset.i);
  down = null; vp.classList.remove('drag');
});
$('zi').onclick = () => zoomCenter(1.4); $('zo').onclick = () => zoomCenter(1 / 1.4); $('fit').onclick = fit;
$('list').addEventListener('click', e => { const c = e.target.closest('.card'); if (c) openNote(+c.dataset.i); });
$('view').onclick = () => {
  const on = document.body.classList.toggle('listing'); $('view').setAttribute('aria-pressed', on);
  $('view').textContent = on ? 'Board' : 'List'; if (!on) fit();
};

// ---- search
let hits = [], hitAt = -1;
$('q').addEventListener('input', () => {
  const q = fold($('q').value.trim());
  hits = []; hitAt = -1;
  world.classList.toggle('searching', !!q);
  notes.forEach((n, i) => {
    const hit = !!q && fold(n.text + ' ' + n.g).includes(q);
    els[i].classList.toggle('hit', hit); cards[i].classList.toggle('hide', !!q && !hit);
    if (hit) hits.push(i);
  });
  $('count').textContent = q ? `${hits.length} match${hits.length === 1 ? '' : 'es'}` : '';
});
$('q').addEventListener('keydown', e => {
  if (e.key === 'Enter' && hits.length) { hitAt = (hitAt + (e.shiftKey ? -1 : 1) + hits.length) % hits.length; focusNote(hits[hitAt]); }
  if (e.key === 'Escape') { $('q').value = ''; $('q').dispatchEvent(new Event('input')); $('q').blur(); }
});

// ---- note viewer
let cur = -1;
function openNote(i) {
  cur = i; const n = notes[i];
  $('m-img').src = n.img ? url(n.img) : ''; $('m-img').style.display = n.img ? '' : 'none'; $('m-img').alt = n.text;
  $('m-crumbs').textContent = `${n.g} · note ${n.ni + 1} of ${n.of}`;
  $('m-text').textContent = n.text || 'No text recognised for this note.'; $('m-text').classList.toggle('none', !n.text);
  const src = {ocr: 'Recognised by the app' + (n.conf != null ? ` (${Math.round(n.conf * 100)}% confident)` : ''), typed: 'Typed', edited: 'Edited text'}[n.src] || '';
  $('m-meta').innerHTML = '';
  const bits = [n.color && `<span><span class="sw" style="background:${n.color}"></span>${n.cname || n.color}</span>`,
    `<span>${n.kind === 'photo' ? 'Captured from a photo' : 'Digital note'}</span>`, src && `<span>${src}</span>`].filter(Boolean);
  $('m-meta').innerHTML = bits.join('<span>·</span>');
  $('m-open').href = n.img ? url(n.img) : '#'; $('m-open').style.display = n.img ? '' : 'none';
  $('m-photo').href = n.photo ? url(n.photo) : '#'; $('m-photo').style.display = n.photo ? '' : 'none';
  $('modal').classList.add('open'); history.replaceState(null, '', '#g' + n.gi + '-' + n.ni);
}
function closeNote() { $('modal').classList.remove('open'); cur = -1; history.replaceState(null, '', location.pathname); }
const step = d => { if (cur >= 0) openNote((cur + d + notes.length) % notes.length); };
$('m-prev').onclick = () => step(-1); $('m-next').onclick = () => step(1); $('m-close').onclick = closeNote;
$('modal').addEventListener('click', e => { if (e.target === $('modal')) closeNote(); });
document.addEventListener('keydown', e => {
  if (e.target === $('q')) return;
  if ($('modal').classList.contains('open')) {
    if (e.key === 'Escape') closeNote(); else if (e.key === 'ArrowLeft') step(-1); else if (e.key === 'ArrowRight') step(1);
    return;
  }
  if (e.key === '/') { e.preventDefault(); $('q').focus(); }
  else if (e.key === '0') fit(); else if (e.key === '+' || e.key === '=') zoomCenter(1.4); else if (e.key === '-') zoomCenter(1 / 1.4);
});
window.addEventListener('resize', () => { if (!document.body.classList.contains('listing')) fit(); });
fit();
const m = location.hash.match(/^#g(\d+)-(\d+)$/);
if (m) { const i = notes.findIndex(n => n.gi === +m[1] && n.ni === +m[2]); if (i >= 0) { focusNote(i); openNote(i); } }
</script>
</body>
</html>
"""

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Post-it Archive</title>
<style>
:root{--bg:#f6f5f1;--ink:#1c1c1a;--muted:#6d6c66;--line:#d6d4cc;--panel:#fffffe;--accent:#0b62d6;--mark:#ffe08a}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--ink:#ecebe5;--muted:#9c9b94;--line:#34342f;--panel:#1f1f1d;--accent:#6aa8ff;--mark:#6b5210}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Helvetica,Arial,sans-serif}
a{color:inherit;text-decoration:none}
.wrap{max-width:1320px;margin:0 auto;padding:0 clamp(16px,4vw,40px)}
header{padding:40px 0 20px}
h1{font-size:30px;letter-spacing:-.01em;margin:0 0 4px}
.sub{color:var(--muted);margin:0}
.bar{position:sticky;top:0;z-index:5;background:var(--bg);padding:12px 0;display:flex;gap:10px;align-items:center;border-bottom:1px solid var(--line)}
input[type=search]{flex:1;min-width:0;height:40px;border:1px solid var(--line);border-radius:10px;padding:0 14px;font:inherit;font-size:15px;background:var(--panel);color:var(--ink)}
select{height:40px;border:1px solid var(--line);border-radius:10px;padding:0 10px;font:inherit;background:var(--panel);color:var(--ink)}
#status{color:var(--muted);font-size:13px;margin:14px 0 0}
#boards{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:18px;padding:18px 0 60px}
.card{display:block;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;transition:transform .12s,box-shadow .12s}
.card:hover{transform:translateY(-2px);box-shadow:0 8px 24px rgba(0,0,0,.10)}
.card .th{aspect-ratio:1;background:#fff;display:block;width:100%;object-fit:contain;border-bottom:1px solid var(--line)}
.card .mosaic{display:grid;grid-template-columns:repeat(3,1fr);align-content:center;gap:6px;padding:14px}
.card .mosaic img{width:100%;aspect-ratio:1;object-fit:cover;box-shadow:0 1px 2px rgba(0,0,0,.2)}
.card .empty{display:flex;align-items:center;justify-content:center;color:#9c9b94;font-size:13px}
.card .t{padding:10px 12px 2px;font-weight:600;font-size:14px;overflow-wrap:anywhere}
.card .m{padding:0 12px 12px;color:var(--muted);font-size:12.5px}
#results{padding:10px 0 60px;display:none}
.searching #results{display:block}.searching #boards{display:none}
.rb{margin:22px 0 10px;display:flex;gap:8px;align-items:baseline}
.rb a{font-weight:600;font-size:15px;color:var(--accent)}.rb span{color:var(--muted);font-size:13px}
.hits{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px}
.hit{display:flex;flex-direction:column;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.hit img{width:100%;aspect-ratio:1;object-fit:cover;display:block}
.hit p{margin:0;padding:8px 10px;font-size:12.5px;white-space:pre-wrap;max-height:7.5em;overflow:hidden}
.hit small{display:block;padding:0 10px 8px;color:var(--muted);font-size:11.5px}
mark{background:var(--mark);color:inherit;border-radius:2px}
</style>
</head>
<body>
<div class="wrap">
<header><h1>Post-it Archive</h1><p class="sub" id="sub"></p></header>
<div class="bar"><input type="search" id="q" placeholder="Search every note and board" aria-label="Search every note and board">
<select id="sort" aria-label="Sort boards"><option value="new">Newest first</option><option value="old">Oldest first</option><option value="name">Name</option><option value="size">Most notes</option></select></div>
<p id="status"></p>
<main id="boards"></main>
<section id="results"></section>
</div>
<script>
const A = __DATA__;
const $ = id => document.getElementById(id), url = p => encodeURI(p);
const fold = s => (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
const esc = s => s.replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const pl = (n, w) => n.toLocaleString() + ' ' + w + (n === 1 ? '' : 's');
const fmt = d => d ? new Date(d + 'T12:00').toLocaleDateString(undefined, {day: 'numeric', month: 'short', year: 'numeric'}) : '';
const S = A.stats;
$('sub').textContent = `${S.boards} boards · ${S.notes.toLocaleString()} notes · ${S.photos} original photos · exported from the Post-it App on ${S.exported}`;
function renderBoards() {
  const s = $('sort').value, b = A.boards.map((x, i) => ({...x, i}));
  b.sort(s === 'name' ? (a, c) => a.name.localeCompare(c.name) : s === 'size' ? (a, c) => c.notes - a.notes :
    s === 'old' ? (a, c) => a.date.localeCompare(c.date) : (a, c) => c.date.localeCompare(a.date));
  $('boards').innerHTML = b.map(x => `<a class="card" href="${url('boards/' + x.dir + '/board.html')}">` +
    (x.thumb ? `<img class="th" loading="lazy" alt="" src="${url('boards/' + x.dir + '/' + x.thumb)}">` :
     x.mosaic.length ? `<div class="th mosaic">${x.mosaic.map(m => `<img loading="lazy" alt="" src="${url('boards/' + x.dir + '/' + m)}">`).join('')}</div>` :
     '<div class="th empty">Empty board</div>') +
    `<div class="t">${esc(x.name)}</div><div class="m">${[fmt(x.date), pl(x.notes, 'note'), pl(x.groups, 'group')].filter(Boolean).join(' · ')}</div></a>`).join('');
}
function hl(text, q) {
  const f = fold(text), i = f.indexOf(q);
  return i < 0 ? esc(text) : esc(text.slice(0, i)) + '<mark>' + esc(text.slice(i, i + q.length)) + '</mark>' + esc(text.slice(i + q.length));
}
function search() {
  const q = fold($('q').value.trim());
  document.body.classList.toggle('searching', q.length > 1);
  if (q.length < 2) { $('status').textContent = ''; return; }
  const byBoard = new Map(); let n = 0;
  A.boards.forEach((b, i) => { if (fold(b.name).includes(q)) byBoard.set(i, []); });
  A.notes.forEach(([b, g, text, img, id]) => {
    if (!fold(text + ' ' + g).includes(q)) return;
    n++; if (!byBoard.has(b)) byBoard.set(b, []); byBoard.get(b).push({g, text, img, id});
  });
  $('status').textContent = `${n} note${n === 1 ? '' : 's'} in ${byBoard.size} board${byBoard.size === 1 ? '' : 's'}`;
  let shown = 0, out = '';
  for (const [bi, hs] of byBoard) {
    const b = A.boards[bi], base = 'boards/' + b.dir + '/board.html';
    out += `<div class="rb"><a href="${url(base)}">${hl(b.name, q)}</a><span>${hs.length} match${hs.length === 1 ? '' : 'es'}</span></div><div class="hits">`;
    for (const h of hs.slice(0, 60)) {
      out += `<a class="hit" href="${url(base) + '#' + h.id}">${h.img ? `<img loading="lazy" alt="" src="${url(h.img)}">` : ''}<p>${hl(h.text, q)}</p><small>${esc(h.g)}</small></a>`;
      if (++shown > 600) break;
    }
    out += '</div>'; if (shown > 600) { out += '<p class="sub">Showing the first 600 matches. Narrow the search to see more.</p>'; break; }
  }
  $('results').innerHTML = out;
}
$('sort').onchange = renderBoards; $('q').addEventListener('input', search);
renderBoards();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
