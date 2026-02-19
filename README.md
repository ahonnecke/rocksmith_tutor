# Rocksmith Bass Tutor

<img width="1459" height="833" alt="image" src="https://github.com/user-attachments/assets/b5732838-3777-417d-bf53-e9e81c8abeff" />

Bass learning curriculum generated from your Rocksmith CDLC library.

## On this machine (rocksmithytoo)

The venv is at `~/src/rocksmith_tutor/.venv/`. All commands:

```bash
~/src/rocksmith_tutor/.venv/bin/rocksmith-tutor <command>
```

### Browse lessons

```bash
# Show all modules and lessons
rocksmith-tutor lessons

# Show only one module
rocksmith-tutor lessons --module fundamentals_basics

# Show a single lesson in detail
rocksmith-tutor lesson fundamentals_basics sustain_basics
```

### Browse your catalog

```bash
# All 1130 bass songs
rocksmith-tutor catalog

# Filter by technique
rocksmith-tutor catalog --technique slapPop
rocksmith-tutor catalog --technique hopo
rocksmith-tutor catalog --technique tapping

# Filter by artist
rocksmith-tutor catalog --artist "red hot"

# Sort by difficulty (easiest first)
rocksmith-tutor catalog --sort difficulty
```

### Re-scan after adding new CDLC

```bash
rocksmith-tutor scan
```

Then tell the Linux box to pull the updated catalog and regenerate.

## Curriculum generation (Linux box only)

Generation uses the Anthropic API and runs on the Linux machine:

```bash
# Pull catalog from Mac
scp rocksmithytoo:~/.local/share/rocksmith_tutor/catalog.json \
    ~/.local/share/rocksmith_tutor/catalog.json

# Generate
rocksmith-tutor generate

# Push curriculum back to Mac
scp ~/.local/share/rocksmith_tutor/curriculum.yaml \
    rocksmithytoo:~/.local/share/rocksmith_tutor/curriculum.yaml
```

### Interactive Q&A (Linux only)

```bash
rocksmith-tutor ask "what should I practice for slap bass?"
rocksmith-tutor ask   # REPL mode
```

## Available techniques

slides, unpitchedSlides, hopo, slapPop, fretHandMutes, palmMutes,
harmonics, pinchHarmonics, tapping, vibrato, tremolo, bends, sustain,
syncopation, twoFingerPicking, bassPick, fingerPicking, fifthsAndOctaves,
doubleStops, openChords, pickDirection
