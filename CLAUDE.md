# rocksmith_tutor

Rocksmith 2014 bass learning curriculum tool. No Jellyfin integration — never connect Rocksmith and Jellyfin.

## CDLC library (NAS)

```
~/nasty/music/Rocksmith_CDLC/
├── live/           # Verified, game-ready
├── staging/        # New downloads awaiting play-test
└── quarantine/     # Files that crashed the game
```

## Deploy

rocksmithytoo is a Mac (SSH host configured in ~/.ssh/config). Rocksmith DLC directory:

```
~/Library/Application Support/Steam/steamapps/common/Rocksmith2014/dlc/
```

```bash
# 1. Repair (flatten bass DD to 100%)
rocksmith-tutor repair ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_m.psarc

# 2. Reslice (small, music-matching phrases — repair MUST come first)
rocksmith-tutor reslice --file ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_repaired_m.psarc \
    -o ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_final_m.psarc

# 3. Deploy to Mac
scp ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_final_m.psarc \
    'rocksmithytoo:"/Users/ahonnecke/Library/Application Support/Steam/steamapps/common/Rocksmith2014/dlc/"'

# 4. Move to live on NAS once verified
mv ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_final_m.psarc ~/nasty/music/Rocksmith_CDLC/live/
```

## Repair strategy: 100% difficulty, no DD

Bass is flattened to 100% — DD notes from all levels are composited into a single level. Only the bass SNG is modified; lead/rhythm/vocals/XML/manifest untouched. Order matters: **repair before reslice** (reslice breaks DD level data).

See [docs/cdlc-pipeline.md](docs/cdlc-pipeline.md) for the full pipeline.
