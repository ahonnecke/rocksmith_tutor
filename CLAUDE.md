# rocksmith_tutor

Rocksmith 2014 bass learning curriculum tool. No Jellyfin integration — never connect Rocksmith and Jellyfin.

## Deploy

rocksmithytoo is a Mac (SSH host configured in ~/.ssh/config). Rocksmith DLC directory:

```
~/Library/Application Support/Steam/steamapps/common/Rocksmith2014/dlc/
```

Deploy a resliced PSARC:
```bash
scp /tmp/my_resliced.psarc 'rocksmithytoo:"~/Library/Application Support/Steam/steamapps/common/Rocksmith2014/dlc/"'
```

## CDLC library (local)

```
~/nasty/music/Rocksmith_CDLC/verified/
~/nasty/music/Rocksmith_CDLC/unverified/
```
