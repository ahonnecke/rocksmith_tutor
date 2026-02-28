# CDLC Pipeline

End-to-end flow for Rocksmith CDLC files, from download to gameplay.

## Architecture

```
  Linux box                        NAS (nasty)                   Mac (rocksmithytoo)
  ─────────                        ──────────                    ───────────────────
  ~/Downloads/                     ~/nasty/music/Rocksmith_CDLC/
    *.psarc                          ├── live/                   ~/Library/.../Rocksmith2014/dlc/
      │                              ├── staging/                    ▲
      ▼                              └── quarantine/                 │
  wayward daemon                                                scp from Linux
    1. detect new .psarc
    2. sanitize filename
    3. pyrocksmith --convert
       (produces _m.psarc + _p.psarc)
    4. move both to staging/
```

## Components

### wayward (daemon on Linux box)

Watches `~/Downloads/` for new `.psarc` files. On detection:
1. Waits for the file to stop growing (download complete)
2. Sanitizes the filename (removes special chars)
3. Runs `pyrocksmith --convert` to produce both `_m.psarc` (Mac) and `_p.psarc` (PC)
4. Moves all `.psarc` files to NAS `staging/`

Source: `~/src/wayward/`

### NAS directory layout

| Directory      | Purpose                                              |
|----------------|------------------------------------------------------|
| `live/`        | Verified, game-ready. Deployed to Mac via scp.       |
| `staging/`     | New downloads. Not visible to game until promoted.    |
| `quarantine/`  | Files that caused crashes. Isolated for inspection.   |

NFS mount on Linux: `nasty:/volume1/music/Rocksmith_CDLC/` at `~/nasty/music/Rocksmith_CDLC/`

NAS IP: `10.0.1.100`

### rocksmithytoo (Mac)

- Rocksmith reads DLC from `~/Library/Application Support/Steam/steamapps/common/Rocksmith2014/dlc/`
- Files deployed via scp from Linux box
- SSH host `rocksmithytoo` configured in `~/.ssh/config`

## Workflow

### Normal flow

```bash
# 1. Download a .psarc to ~/Downloads on Linux
# 2. wayward auto-detects, converts, moves to staging/

# 3. Check what's in staging
wayward-promote --list

# 4. Promote to live on NAS
wayward-promote SomeArtist_SomeSong_v1_m.psarc
# or promote everything:
wayward-promote --all

# 5. Deploy to Mac
scp ~/nasty/music/Rocksmith_CDLC/live/SomeArtist_SomeSong_v1_m.psarc \
    'rocksmithytoo:"~/Library/Application Support/Steam/steamapps/common/Rocksmith2014/dlc/"'

# 6. Re-scan catalog to pick up new songs
rocksmith-tutor scan
```

### Quarantine flow

```bash
# After a song crashes Rocksmith:
wayward-quarantine BadSong_v1_m.psarc

# List quarantined files
wayward-quarantine --list

# Restore if you want to retry
wayward-quarantine --restore BadSong_v1_m.psarc
```

### rocksmith_tutor integration

```bash
# On Linux — scans NAS live/ and staging/
rocksmith-tutor scan

# On Mac — scans local Rocksmith DLC directory
rocksmith-tutor scan
```

## Repair flow

### Strategy: 100% difficulty, no DD

Bass is flattened to 100% difficulty — DD notes from all levels are composited into
a single level so every note is always visible. Use Riff Repeater speed control to
slow down hard sections instead of DD's note-hiding approach. Only the bass SNG is
modified; lead, rhythm, vocals, XML, and manifest are untouched.

### Repair → Reslice → Deploy

**Order matters: repair (flatten) before reslice.** Reslice rebuilds the SNG phrase
structure, which breaks DD level data. Flatten first to composite all notes into one
level, then reslice to get good phrase boundaries.

```bash
# 1. Repair (flatten bass DD to 100%)
rocksmith-tutor repair ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_m.psarc

# 2. Reslice (note-gap-driven phrase boundaries, 3-15s segments)
rocksmith-tutor reslice --file ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_repaired_m.psarc \
    -o ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_final_m.psarc

# 3. Deploy to Mac
scp ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_final_m.psarc \
    'rocksmithytoo:"/Users/ahonnecke/Library/Application Support/Steam/steamapps/common/Rocksmith2014/dlc/"'

# 4. Move to live on NAS once verified in-game
mv ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_final_m.psarc \
   ~/nasty/music/Rocksmith_CDLC/live/
```

### Repair only (skip reslice)

If the existing phrase boundaries are fine and you just want 100% difficulty:

```bash
rocksmith-tutor repair ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_m.psarc
```

### Batch

```bash
for f in ~/nasty/music/Rocksmith_CDLC/staging/*_m.psarc; do
  rocksmith-tutor repair "$f"
  rocksmith-tutor reslice --file "${f%_m.psarc}_repaired_m.psarc" \
      -o "${f%_m.psarc}_final_m.psarc"
done
```
