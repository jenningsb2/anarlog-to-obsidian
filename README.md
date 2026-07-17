# anarlog-to-obsidian

Automatically export your [Anarlog](https://docs.anarlog.so) meeting recordings
into an [Obsidian](https://obsidian.md) vault as clean Markdown notes — with
YAML frontmatter, normalised headings, an attendees list, and (optionally) the
audio recording copied into the vault so it plays on every device.

Run it on a schedule (launchd / cron / systemd) and new meetings show up in your
vault automatically. It's a single dependency-free Python file.

## What each note looks like

```markdown
---
title: "Weekly Product Sync"
date: 2026-07-16T13:00
anarlog-id: f09a7087-ae05-4df5-af5a-62c2405d5b0e
source: anarlog
attendees:
  - alice@example.com
  - bob@example.com
audio: "Attachments/2026-07-16 Weekly Product Sync.mp3"
---

![[2026-07-16 Weekly Product Sync.mp3]]

## Notes

- ...

## Summary

### Some Topic

- ...

## Transcript

<full meeting transcript, as provided by anarlog's export>
```

> Note: anarlog's Markdown export already includes the transcript (plain text).
> It does not carry per-person speaker names — words are only tagged by audio
> channel (mic vs. system) — so no speaker labels are added.

## How it works

- Lists meetings with `anarlog --json meetings list` (paginated for a full backfill).
- Exports each new one with `anarlog meetings export <id> --format markdown`.
- Rewrites the output: pulls participants into an `attendees:` frontmatter list,
  drops Anarlog's duplicated title/metadata block, and demotes content
  sub-headings so the outline is `## Section` → `### Sub-heading`.
- Optionally copies the recording from Anarlog's session folder into the vault's
  attachments folder, transcoded to a small mono mp3, and embeds it in the note.
- Tracks exported meeting ids in a state file so re-runs are **idempotent**.

**It is read-only toward Anarlog** (only the CLI, never the database) and
**never overwrites or deletes** an existing vault file — filename collisions get
a numeric suffix. Your original recordings in Anarlog are left untouched, so the
in-vault copy can safely be a compressed derivative.

## Requirements

- The [`anarlog` CLI](https://docs.anarlog.so) on your `PATH`.
- Python 3.8+ (standard library only — no `pip install`).
- Optional: [`ffmpeg`](https://ffmpeg.org/) for audio transcoding. Without it,
  audio is copied verbatim (or skip audio with `--no-audio`).

## Quick start

```sh
git clone https://github.com/<you>/anarlog-to-obsidian.git
cd anarlog-to-obsidian

# Point it at your vault and preview (writes nothing):
export ANARLOG_VAULT="/path/to/your/Obsidian/Vault"
python3 anarlog_to_obsidian.py --dry-run

# Do the export:
python3 anarlog_to_obsidian.py
```

## Configuration

Set via environment variables (or the few flags below). Flags win over env vars.

| Variable | Default | Meaning |
|---|---|---|
| `ANARLOG_VAULT` | *(required)* | Obsidian vault root. |
| `ANARLOG_SUBDIR` | `Meetings` | Folder in the vault for notes (`<subdir>/YYYY-MM/`). |
| `ANARLOG_ATTACH_SUBDIR` | `Attachments` | Folder in the vault for audio copies. |
| `ANARLOG_STATE` | `~/.anarlog_obsidian_state.json` | Where exported ids are tracked. |
| `ANARLOG_SESSIONS` | `~/Library/Application Support/anarlog/sessions` | Anarlog's per-meeting recording folders. Override off macOS. |
| `ANARLOG_COPY_AUDIO` | `1` | Copy audio into the vault (`0` to disable). |
| `ANARLOG_TRANSCODE` | `1` | Transcode audio with ffmpeg (`0` copies verbatim). |
| `ANARLOG_AUDIO_BITRATE` | `32k` | Target audio bitrate. |
| `ANARLOG_AUDIO_CHANNELS` | `1` | Audio channels (`1` = mono; ideal for speech). |

Flags: `--vault PATH`, `--dry-run`, `--no-audio`, `--limit N` (only the N most
recent meetings). See `--help`.

### A note on audio size

Meeting audio is mono speech, so the default mono/32 kbps mp3 is typically
~75% smaller than the source with no meaningful quality loss. If your vault syncs
via a quota-limited service (iCloud, Dropbox), remember every meeting's audio is
copied there permanently — set `ANARLOG_COPY_AUDIO=0` if you'd rather not, or
raise `ANARLOG_AUDIO_BITRATE` for higher fidelity.

## Scheduling

### macOS (launchd)

Edit `com.example.anarlog-obsidian.plist` (replace the placeholders), then:

```sh
cp com.example.anarlog-obsidian.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.example.anarlog-obsidian.plist
launchctl list | grep anarlog          # verify
launchctl start com.example.anarlog-obsidian   # run once now
```

launchd runs the job at the next opportunity if the Mac was asleep at the
scheduled time (cron would skip it). It runs while you're logged in.

### Linux / macOS (cron)

```cron
# Daily at 6pm. Set ANARLOG_VAULT for cron's environment.
0 18 * * * ANARLOG_VAULT="/path/to/vault" /usr/bin/python3 /path/to/anarlog_to_obsidian.py >> /tmp/anarlog-obsidian.log 2>&1
```

### Linux (systemd timer)

Create a `--user` service that runs the script and a timer with
`OnCalendar=*-*-* 18:00:00`. (See the systemd docs for the two unit files.)

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

Not affiliated with Anarlog or Obsidian. Community tool provided as-is.
