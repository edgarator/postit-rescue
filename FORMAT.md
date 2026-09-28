# The .postit file format

Notes from reverse engineering files written by the Post-it® App, format versions 4.2,
5.0, 6.0 to 6.3 and 7.0. Field names are the app's own. Nothing here comes from 3M.

A `.postit` file is a ZIP archive:

| Member | Contents |
|---|---|
| `metadata` | JSON: `version` `{major, minor}` and UUID lists `sheetUUIDs`, `noteContentUUIDs`, `noteEditImageUUIDs`, `noteEditDrawingImageUUIDs`, `captureUUIDs`, `ocrRequestUUIDs`, `titleNoteContentUUIDs` |
| `sheet-<uuid>.json` | The board. Exactly one per file in every file we have seen. |
| `note-<contentUUID>-enhanced.jpg` | A note as the app displays it, with ink and typed text baked in. 1024 px in recent versions, smaller in old ones. |
| `edit-<uuid>.png` | A note's editable background layer |
| `edit-drawing-<uuid>.png` | A note's ink layer, transparent |
| `capture-<uuid>.jpg` | An original photo that notes were captured from |
| `capture-<uuid>.json` | Detection data for that photo (`piboard`) and where it was taken (`placemark`: coordinates and street address) |
| `ocr-<uuid>.json` | An OCR request log. The recognised text itself is stored in the sheet. |
| `sheet-preview.png`, `sheet-thumbnail.png` | Renders of the board. Occasionally saved before the note images loaded. |
| `index.json` | File hashes, newer versions only |

## Sheet

```json
{ "name": "…", "UUID": "…", "creationDate": "…", "digitalNoteColor": {…}, "clusters": [ … ] }
```

`name` is often stale, such as a UUID or "Board 46". The app shows the file name instead.
`creationDate` is rewritten whenever the app re-saves the board, so the timestamps of the
note images inside the ZIP are a better guide to when a board was made.

Each cluster is a group of notes:

```json
{ "name": "…", "layoutType": "grid", "positionX": 0, "positionY": 0, "width": 3, "height": 2, "notes": [ … ] }
```

Each note:

| Field | Meaning |
|---|---|
| `contentUUID` | Its image: `note-<contentUUID>-enhanced.jpg` |
| `layoutIndex` | Position in grid layouts |
| `centerX`, `centerY` | Position in free-form layouts, relative to the cluster |
| `layoutRotation` | Rotation in radians, free-form only |
| `layoutZOrder` | Stacking order |
| `width`, `height`, `sizeClass` | Size in millimetres; `"3x3"` is 76.2 × 76.2. Old files omit them: assume 76.2. |
| `isDigitalNote` | `false` for notes captured from a photo |
| `board` | For captured notes, the UUID of the source photo (`capture-<uuid>.jpg`) |
| `corners` | For captured notes, the note's corners in that photo, normalised to 0–1 |
| `backgroundColor` | Detected colour, `{red, green, blue, alpha}` from 0 to 1 |
| `noteEditInfo` | For edited and digital notes: `imageUUID`, `drawingImageUUID`, `backgroundColor`, and `text`, which is either a string or an object with `contents`, `typeface`, `textSize` and placement |
| `ocr` | `{text, confidence, language}` |
| `userProvidedText` | Text the user typed in place of the OCR result |

The best available text for a note is `userProvidedText`, then `noteEditInfo.text`, then `ocr.text`.

## Layout

Board units are millimetres.

- `grid`: notes fill cells in `layoutIndex` order, row by row, `width` columns wide,
  starting at (`positionX`, `positionY`). Cells are the note size plus about 13.8 apart,
  so 90 for 3×3 notes, measured from the app's own renders.
- `snap-to-grid`: the same cells, but `layoutIndex` is the cell number, so cells can be
  empty. The app ignores `centerX` and `centerY` when drawing these.
- `free-form`: each note is centred on (`positionX + centerX`, `positionY + centerY`) and
  rotated by `layoutRotation`. Here the cluster's `width` and `height` are its size in
  millimetres, not a cell count.

In grid layouts `height` can be smaller than the number of rows the notes actually use.
