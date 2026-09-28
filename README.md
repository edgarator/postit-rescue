# postit-rescue

Get your boards out of the Post-it® App.

3M retires the app on 29 September 2026 at 11:59 pm CDT. After that it won't open your
boards, and 3M says the `.postit` files it leaves behind are no longer useful. This script
reads those files and turns every board into an ordinary folder you can keep: a browsable
copy of each board, every note as an image, and all the text in a spreadsheet.

It works after the shutdown too, as long as you still have the `.postit` files.

## What you get

A `Post-it Archive` folder containing:

- `index.html`: every board, with a search across the text of every note. Opens in any
  browser and works offline.
- `boards/<board>/board.html`: the board in its original layout. Drag to pan, pinch or
  ⌘-scroll to zoom, click a note to enlarge it.
- `boards/<board>/notes/`: every note as a JPEG, one folder per group, in board order.
- `boards/<board>/photos/`: the original photos your notes were captured from.
- `boards/<board>/notes.csv` and `all-notes.csv`: note text, group, colour and image
  path. Opens in Excel or Numbers.
- `boards/<board>/board.json`: the same data with positions, for importing into other tools.
- `originals/`: untouched copies of your `.postit` files.

## Use it on a Mac

If your boards are in iCloud Drive, which is where the app keeps them on iPhone, iPad and
Mac, open Terminal and run:

```bash
curl -LO https://raw.githubusercontent.com/edgarator/postit-rescue/main/postit_export.py
python3 postit_export.py
open "Post-it Archive/index.html"
```

The first time you run `python3`, macOS may offer to install its command line developer
tools. Accept, then run the command again.

The script looks for boards in `~/Library/Mobile Documents/iCloud~com~mmm~post-it/Documents`,
which is iCloud Drive → Post-it in Finder. If your boards are somewhere else, for example
`.postit` files you exported from the app, pass the folder:

```bash
python3 postit_export.py ~/Downloads/my-boards
```

Any computer with Python 3.8 or newer works the same way. Only the default folder is
Mac-specific. Run `python3 postit_export.py --help` for details.

## Good to know

- Everything runs on your computer. Nothing is uploaded.
- Notes only have text where the app ran its handwriting recognition, and that text is only
  as good as the app's OCR was. In testing, under a third of notes had any. The note images
  are the source of truth.
- `.postit` files record where each photo was taken, down to the street address. The HTML
  and CSV exports leave that out, but the copies in `originals/` keep it, so think twice
  before sharing that folder.
- Tested on a library of 142 boards and 9,531 notes, covering file format versions 4.2 to 7.0.

The `.postit` format is documented in [FORMAT.md](FORMAT.md).

Not affiliated with or endorsed by 3M. Post-it® is a registered trademark of 3M.
