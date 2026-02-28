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

All CDLC is flattened to a single max-difficulty level. Dynamic Difficulty (DD) is
stripped entirely — the game always shows all notes at 100%. Use Riff Repeater speed
control to slow down hard sections instead of DD's note-hiding approach.

This eliminates the entire class of DD-related corruption (`notesInIterCount` mismatch,
multi-level coverage bugs, stale `phraseId` references) that affects 54%+ of the CDLC
library.

### Validate → Repair → Deploy

```bash
# 1. Validate a file
rocksmith-tutor validate ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_m.psarc

# 2. Repair (flattens DD, recomputes derived fields)
#    Output goes next to the input as {stem}_repaired{suffix}
rocksmith-tutor repair ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_m.psarc

# 3. Deploy to Mac
scp ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_repaired_m.psarc \
    'rocksmithytoo:"~/Library/Application Support/Steam/steamapps/common/Rocksmith2014/dlc/"'

# 4. Move to live on NAS once verified in-game
mv ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_repaired_m.psarc \
   ~/nasty/music/Rocksmith_CDLC/live/

# Dry run — validate before/after without writing
rocksmith-tutor repair --dry-run ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_m.psarc
```

### Batch repair

```bash
# Validate everything in staging
for f in ~/nasty/music/Rocksmith_CDLC/staging/*_m.psarc; do
  rocksmith-tutor validate "$f"
done

# Repair all failing files
for f in ~/nasty/music/Rocksmith_CDLC/staging/*_m.psarc; do
  rocksmith-tutor repair "$f"
done
```

### Reslice (re-segment boundaries)

When a file needs completely new phrase/section structure (not just accounting fixes):

```bash
rocksmith-tutor reslice ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_m.psarc
```
