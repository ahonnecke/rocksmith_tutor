# Reslice TODO

## Known Failure Modes

Discovered during reslice development and iterative testing against
Rocksmith 2014 on Mac.

### 1. XML ElementTree rewrite — CRASH on load
- **Symptom:** Song appears in list, disappears when selected
- **Cause:** Python's `xml.etree.ElementTree` rewrites the entire XML tree,
  which reorders attributes, strips the `<?xml?>` declaration, and changes
  whitespace. Rocksmith rejects the result.
- **Fix:** Use regex substitution to surgically replace only the `<phrases>`,
  `<phraseIterations>`, and `<sections>` blocks, preserving everything else
  byte-for-byte.
- **Validation:** Round-trip the XML through the rebuild function and diff
  against the original — only the three target blocks should differ.

### 2. XML CRLF line endings — silent no-op
- **Symptom:** Song loads, but Riff Repeater shows original (old) sections
- **Cause:** CDLC XML files use `\r\n` line endings. Regex patterns with `\n`
  don't match, so the substitution silently does nothing. The PSARC gets
  repacked with the original XML unchanged.
- **Fix:** Use `[\r\n]` in regex patterns and detect/preserve the original
  line ending style.
- **Validation:** After rebuild, count `<section` elements in the XML and
  compare to expected boundary count.

### 3. Manifest field structure mismatch — CRASH on load
- **Symptom:** Song appears in list, disappears when selected
- **Cause:** Manifest JSON sections need exact field names:
  - `UIName` (e.g. `$[0] Intro [1]`)
  - `StartPhraseIterationIndex` / `EndPhraseIterationIndex`
  - PhraseIterations use `PhraseIndex` (not `PhraseId`)
  - Phrases use `IterationCount` (not `phraseIterationLinks`)
  Using wrong field names or omitting required fields causes Rocksmith to
  reject the PSARC.
- **Fix:** Match the exact field structure from the original manifest.
- **Validation:** Parse rebuilt manifest, check all required keys present
  per entry type.

### 4. SNG-only modification — loads but old sections displayed
- **Symptom:** Song loads and plays, but Riff Repeater shows original sections
- **Cause:** Rocksmith reads section layout from the XML arrangement file,
  not the SNG binary. SNG alone is insufficient for Riff Repeater UI.
- **Note:** SNG rebuild IS required for correct gameplay (note-level phrase
  references, beat PI indices, iter counts). But XML must also be updated
  for the section list to change in the UI.
- **Validation:** Verify XML sections match SNG sections (count and times).

### 5. Mac vs PC SNG key paths
- **Symptom:** "No bass SNG found in PSARC"
- **Cause:** Mac PSARCs (`_m.psarc`) use `songs/bin/macos/`, PC PSARCs
  (`_p.psarc`) use `songs/bin/generic/`. Initial implementation only checked
  for `songs/bin/generic/`.
- **Fix:** Check both paths.
- **Validation:** Confirm at least one bass SNG key found.

### 6. Single-arrangement modification — loads but old sections displayed
- **Symptom:** Song loads, Riff Repeater shows original sections despite
  bass arrangement being correctly modified.
- **Cause:** Sections in Rocksmith are song-level, not per-arrangement.
  ALL arrangements (bass, lead, rhythm) must have consistent sections.
  Modifying only the bass XML/SNG/manifest leaves the other arrangements
  with the original section layout, and Rocksmith reads from all of them.
- **Fix:** Modify ALL arrangement SNGs, ALL manifests with sections, and
  ALL arrangement XMLs in the PSARC.
- **Validation:** Cross-arrangement consistency — all arrangement XMLs,
  SNGs, and manifests must agree on section count.

### 7. COUNT section hidden in Riff Repeater
- **Note:** Rocksmith always hides the COUNT section from Riff Repeater.
  Displayed blocks = total sections - 1. This is expected behavior, not
  a bug. The COUNT phrase iteration (time=0, phraseId=0) must still exist
  in the SNG and XML.

## Validator Tool

Build a `rocksmith-tutor validate` command that checks a PSARC for
all known failure modes before deploying to Rocksmith.

### Proposed Validation Checks

```
rocksmith-tutor validate path/to/file.psarc
```

1. **PSARC parse** — file opens and all entries decompress
2. **Bass SNG found** — at least one bass SNG key exists (mac or pc path)
3. **SNG parse** — Song.parse() succeeds on each arrangement SNG
4. **SNG internal consistency** (per arrangement):
   - Every note's `phraseIterationId` maps to a valid PI
   - Every note's `phraseId` matches its PI's `phraseId`
   - Beat PI indices are monotonically non-decreasing
   - Section `startPhraseIterationId`/`endPhraseIterationId` are valid
   - `notesInIterCount` sums equal total notes per level
5. **XML well-formed** — parses without error
6. **XML section count** — matches SNG section count
7. **XML phraseIteration count** — matches SNG PI count
8. **Manifest sections** — count matches, required fields present
   (`Name`, `UIName`, `Number`, `StartTime`, `EndTime`,
    `StartPhraseIterationIndex`, `EndPhraseIterationIndex`, `IsSolo`)
9. **Manifest PIs** — count matches, required fields present
   (`PhraseIndex`, `MaxDifficulty`, `Name`, `StartTime`, `EndTime`)
10. **Manifest phrases** — count matches, required fields present
    (`MaxDifficulty`, `Name`, `IterationCount`)
11. **Cross-layer consistency** — SNG, XML, and manifest all agree on
    section count, PI count, and phrase count
12. **Cross-arrangement consistency** — all arrangement SNGs, XMLs, and
    manifests agree on section count
