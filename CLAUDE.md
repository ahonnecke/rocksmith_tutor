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
# Repair a PSARC (outputs next to input as {stem}_repaired{suffix})
rocksmith-tutor repair ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_m.psarc

# Deploy to Mac
scp ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_repaired_m.psarc \
    'rocksmithytoo:"~/Library/Application Support/Steam/steamapps/common/Rocksmith2014/dlc/"'

# Move to live on NAS once verified
mv ~/nasty/music/Rocksmith_CDLC/staging/SomeFile_repaired_m.psarc ~/nasty/music/Rocksmith_CDLC/live/
```

## Repair strategy: 100% difficulty, no DD

All CDLC is flattened to a single max-difficulty level. Dynamic Difficulty (DD) is stripped — the game always shows all notes at 100%. Use Riff Repeater speed control to slow down hard sections instead of DD's note-hiding approach. This eliminates the entire class of DD-related corruption (broken `notesInIterCount`, multi-level coverage bugs) that affects 54% of the CDLC library.

See [docs/cdlc-pipeline.md](docs/cdlc-pipeline.md) for the full pipeline.
