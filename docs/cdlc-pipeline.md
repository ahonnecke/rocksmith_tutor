# CDLC Pipeline

End-to-end flow for Rocksmith CDLC files, from download to gameplay.

## Architecture

```
  Linux box                        NAS (nasty)                   Mac (rocksmithytoo)
  ─────────                        ──────────                    ───────────────────
  ~/Downloads/                     ~/nasty/music/Rocksmith_CDLC/
    *.psarc                          ├── live/       ◄────── NFS mount ──── ~/dlc
      │                              ├── staging/
      ▼                              └── quarantine/
  wayward daemon
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
| `live/`        | Verified, game-ready. Mounted by rocksmithytoo.      |
| `staging/`     | New downloads. Not visible to game until promoted.    |
| `quarantine/`  | Files that caused crashes. Isolated for inspection.   |

NFS mount on Linux: `nasty:/volume1/music/Rocksmith_CDLC/` at `~/nasty/music/Rocksmith_CDLC/`

NAS IP: `10.0.1.100`

### rocksmithytoo (Mac)

- NFS mount: `10.0.1.100:/volume1/music/Rocksmith_CDLC/live` at `~/mnt/nasty_cdlc_live`
- Symlink: `~/dlc` -> `~/mnt/nasty_cdlc_live`
- Rocksmith reads DLC from `~/Library/Application Support/Steam/steamapps/common/Rocksmith2014/dlc/`
- Mount persisted via launchd: `/Library/LaunchDaemons/com.nasty.cdlc.mount.plist`

## Workflow

### Normal flow

```bash
# 1. Download a .psarc to ~/Downloads on Linux
# 2. wayward auto-detects, converts, moves to staging/

# 3. Check what's in staging
wayward-promote --list

# 4. Promote to live (immediately visible to Rocksmith on Mac)
wayward-promote SomeArtist_SomeSong_v1_m.psarc
# or promote everything:
wayward-promote --all

# 5. Re-scan catalog to pick up new songs
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

The tutor scans both `live/` and `staging/` on Linux:

```bash
# Scan NAS for catalog updates
rocksmith-tutor scan

# On Mac, the catalog is local
rocksmith-tutor scan   # scans ~/dlc which points to NAS live/
```

## Repair flow

When a `.psarc` file is corrupt or has issues, `rocksmith-tutor` can reslice it:

```bash
# Validate files
rocksmith-tutor validate ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_m.psarc

# Reslice to fix
rocksmith-tutor reslice ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_m.psarc

# Deploy repaired file to the game directory on Mac
scp /tmp/resliced.psarc 'rocksmithytoo:"~/Library/Application Support/Steam/steamapps/common/Rocksmith2014/dlc/"'
```
