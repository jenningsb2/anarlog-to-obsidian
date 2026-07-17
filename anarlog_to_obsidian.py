#!/usr/bin/env python3
"""Incrementally export Anarlog meetings into an Obsidian vault as Markdown.

For each new meeting it writes a note to ``<vault>/<subdir>/YYYY-MM/`` with YAML
frontmatter (title, date, anarlog-id, source, attendees, audio), normalises
Anarlog's heading levels, and — optionally — copies the recording into the
vault's attachments folder (transcoded to a small mono mp3) so the audio link
resolves on every device the vault syncs to.

Design goals:

* **Idempotent** — exported meeting ids are tracked in a state file and skipped
  on later runs. Safe to run from launchd/cron/systemd or by hand, repeatedly.
* **Non-destructive** — it never overwrites or deletes an existing vault file
  (name collisions get a numeric suffix), and it only reads Anarlog through its
  read-only CLI (never the SQLite database directly).

Requirements: the ``anarlog`` CLI on PATH, Python 3.8+, and (optional, for audio
transcoding) ``ffmpeg``. No third-party Python packages.

Configuration is via environment variables or flags — see ``--help`` and README.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from shutil import which


# ---- configuration ----------------------------------------------------------
# Every setting has an env-var override so the script stays generic; flags on the
# command line win over env vars, which win over these defaults.

def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _env_bool(name, default):
    v = os.environ.get(name)
    if v in (None, ""):
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# Where notes live inside the vault: <vault>/<SUBDIR>/YYYY-MM/<note>.md
SUBDIR = _env("ANARLOG_SUBDIR", "Meetings")
# Where audio copies live inside the vault: <vault>/<ATTACH_SUBDIR>/
ATTACH_SUBDIR = _env("ANARLOG_ATTACH_SUBDIR", "Attachments")
# State file of already-exported meeting ids (keeps runs idempotent).
STATE = Path(_env("ANARLOG_STATE", str(Path.home() / ".anarlog_obsidian_state.json")))
# Anarlog's per-meeting recording folder: <SESSIONS>/<meeting-id>/audio.*
# Default is the macOS location; override on other platforms.
SESSIONS = Path(_env(
    "ANARLOG_SESSIONS",
    str(Path.home() / "Library/Application Support/anarlog/sessions"),
))
AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus", ".flac")

# Audio handling. Meeting audio is mono speech, so transcoding to a low-bitrate
# mono mp3 typically cuts ~75% of the size with no perceptible loss. mp3 is used
# for universal playback (incl. iOS). Set ANARLOG_TRANSCODE=0 to copy verbatim.
COPY_AUDIO = _env_bool("ANARLOG_COPY_AUDIO", True)
TRANSCODE = _env_bool("ANARLOG_TRANSCODE", True)
AUDIO_BITRATE = _env("ANARLOG_AUDIO_BITRATE", "32k")
AUDIO_CHANNELS = _env("ANARLOG_AUDIO_CHANNELS", "1")

PAGE_SIZE = 200  # anarlog `meetings list` caps --limit at 200; we paginate.

# Set from CLI in main().
VAULT = None
DRY_RUN = False


def ffmpeg_bin():
    """Locate ffmpeg. Schedulers (launchd/cron) run with a minimal PATH, so we
    also probe the usual install locations across macOS and Linux."""
    return which("ffmpeg") or next(
        (p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg",
                     "/usr/bin/ffmpeg", "/snap/bin/ffmpeg") if Path(p).exists()),
        None,
    )


# ---- anarlog CLI ------------------------------------------------------------

def run_json(args):
    """Run `anarlog --json <args>` and return the parsed JSON payload."""
    try:
        r = subprocess.run(["anarlog", "--json", *args], capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("anarlog CLI not found on PATH. Install it first: https://docs.anarlog.so")
    if r.returncode != 0:
        sys.exit(f"anarlog {' '.join(args)} failed (exit {r.returncode}): {r.stderr.strip()}")
    return json.loads(r.stdout)


def list_all_meetings(limit=None):
    """Page through `anarlog meetings list` and return meetings (newest first).

    Response shape (schema_version 1):
      {"schema_version": "1", "command": "meetings.list",
       "data": [ {id, title, started_at, ...}, ... ],
       "pagination": {"offset", "limit", "returned", "total", "next_offset"}}
    """
    meetings = []
    offset = 0
    while True:
        resp = run_json(["meetings", "list", "--limit", str(PAGE_SIZE), "--offset", str(offset)])
        page = resp.get("data", []) or []
        meetings.extend(page)
        if limit is not None and len(meetings) >= limit:
            return meetings[:limit]
        nxt = (resp.get("pagination") or {}).get("next_offset")
        if nxt is None:
            if len(page) < PAGE_SIZE:  # fallback if next_offset is ever absent
                break
            offset += PAGE_SIZE
        else:
            offset = nxt
    return meetings


def export_meeting(mid):
    """Export one meeting to Markdown via the CLI, returning the text.

    Writes to a throwaway temp file (not the vault) so we can post-process before
    anything lands in the vault.
    """
    with tempfile.NamedTemporaryFile("r", suffix=".md", delete=False) as tf:
        tmp = Path(tf.name)
    try:
        r = subprocess.run(
            ["anarlog", "meetings", "export", mid, "--format", "markdown",
             "--output", str(tmp), "--force"],  # --force targets the temp file only
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or f"exit {r.returncode}")
        return tmp.read_text()
    finally:
        tmp.unlink(missing_ok=True)


# ---- note building ----------------------------------------------------------

def sanitize(title):
    """Make a title safe for a filename on macOS/Windows/Linux."""
    title = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", title or "Untitled")
    return re.sub(r"\s+", " ", title).strip()[:120] or "Untitled"


def meeting_date(m):
    """Best-effort local datetime for a meeting from its timestamp fields."""
    for key in ("started_at", "created_at", "date", "start_time"):
        v = m.get(key)
        if v:
            try:
                return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone()
            except ValueError:
                pass
    return datetime.now().astimezone()


def has_frontmatter(text):
    return text.lstrip().startswith("---")


def _yaml_scalar(v):
    # Plain scalar for simple values (emails, names); quote anything else so the
    # YAML stays valid (e.g. names with apostrophes or colons).
    return v if re.match(r"^[A-Za-z0-9][\w .@+-]*$", v) else json.dumps(v)


def transform_body(raw):
    """Normalise Anarlog's exported Markdown and pull out the participants.

    Anarlog emits the meeting title as an H1 (repeated inside sections), an
    ID/Date/Participants bullet block, `## Notes|Summary|Transcript` section
    headers, and content sub-headings as H1. We extract participants, drop the
    metadata block, drop the redundant title H1s, and demote content H1s to H3
    so the outline is `## Section` > `### Sub-heading`.

    Returns (cleaned_markdown, attendees_list).
    """
    lines = raw.splitlines()
    title = next((m.group(1).strip() for m in
                  (re.match(r"^#\s+(.*\S)\s*$", ln) for ln in lines) if m), None)

    attendees, out = [], []
    for ln in lines:
        mp = re.match(r"^- Participants:\s*(.*)$", ln)
        if mp:
            for p in (x.strip() for x in mp.group(1).split(",")):
                if p and p not in attendees:
                    attendees.append(p)
            continue
        if re.match(r"^- (ID|Date):", ln):
            continue
        h1 = re.match(r"^#\s+(.*\S)\s*$", ln)  # single-# only; ## / ### untouched
        if h1:
            txt = h1.group(1).strip()
            if txt == title:
                continue                       # drop leading + repeated title
            out.append(f"### {txt}")           # demote real sub-heading to H3
            continue
        out.append(ln)

    # collapse blank runs, strip leading/trailing blanks
    cleaned = []
    for ln in out:
        if ln.strip() == "" and (not cleaned or cleaned[-1].strip() == ""):
            continue
        cleaned.append(ln)
    while cleaned and cleaned[-1].strip() == "":
        cleaned.pop()
    return "\n".join(cleaned) + "\n", attendees


def build_frontmatter(m, dt, audio_name=None, attendees=None):
    """YAML frontmatter block: title (quoted), local ISO-minute date, ids, tags,
    attendees list, and a vault-relative audio path."""
    lines = [
        "---",
        f"title: {json.dumps(m.get('title') or 'Untitled')}",
        f"date: {dt.strftime('%Y-%m-%dT%H:%M')}",
        f"anarlog-id: {m.get('id')}",
        "source: anarlog",
    ]
    if attendees:
        lines.append("attendees:")
        lines += [f"  - {_yaml_scalar(a)}" for a in attendees]
    if audio_name is not None:
        lines.append(f"audio: {json.dumps(f'{ATTACH_SUBDIR}/{audio_name}')}")
    lines += ["---", "", ""]
    return "\n".join(lines)


# ---- audio ------------------------------------------------------------------

def find_audio(mid):
    """Return the Path to a session's audio file, or None if absent."""
    sess = SESSIONS / mid
    if not sess.is_dir():
        return None
    audio = sess / "audio.mp3"
    if audio.is_file():
        return audio
    for p in sorted(sess.iterdir()):
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
            return p
    return None


def copy_audio_into_vault(mid, note_base):
    """Place the session audio into <vault>/<ATTACH_SUBDIR>/, transcoded to a
    small mono mp3 when ffmpeg is available (else copied verbatim).

    Returns the attachment filename (for the embed) or None if there is no audio.
    Never overwrites: the destination is keyed by the note's collision-free base
    name, so an existing file is ours from a prior run and is reused as-is.
    """
    src = find_audio(mid)
    if src is None:
        return None
    attach_dir = VAULT / ATTACH_SUBDIR
    attach_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg = ffmpeg_bin() if TRANSCODE else None
    if ffmpeg:
        dest = attach_dir / f"{note_base}.mp3"
        if dest.exists():
            return dest.name
        # Encode to a temp file first, then move into place, so an interrupted
        # run can't leave a truncated mp3 behind.
        tmp = attach_dir / f".{note_base}.partial.mp3"
        r = subprocess.run(
            [ffmpeg, "-y", "-i", str(src), "-ac", str(AUDIO_CHANNELS),
             "-b:a", AUDIO_BITRATE, "-map_metadata", "-1", str(tmp)],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and tmp.exists():
            tmp.replace(dest)
            return dest.name
        tmp.unlink(missing_ok=True)
        print(f"ffmpeg failed for {mid}, copying raw: {r.stderr.strip()[:200]}", file=sys.stderr)

    # No transcode (disabled, no ffmpeg, or it failed): copy the original.
    dest = attach_dir / f"{note_base}{src.suffix.lower()}"
    if not dest.exists():
        shutil.copy2(src, dest)
    return dest.name


# ---- main -------------------------------------------------------------------

def process(m, exported):
    """Export a single meeting. Returns the vault-relative note path, or None if
    skipped. Honors DRY_RUN (no writes)."""
    mid = m.get("id") or m.get("meeting_id")
    if not mid or mid in exported:
        return None

    try:
        body = export_meeting(mid)
    except RuntimeError as e:
        print(f"skip {mid}: {e}", file=sys.stderr)
        return None  # not marked exported; retried next run

    dt = meeting_date(m)
    out_dir = VAULT / SUBDIR / dt.strftime("%Y-%m")
    base = f"{dt.strftime('%Y-%m-%d')} {sanitize(m.get('title'))}"
    out = out_dir / f"{base}.md"
    n = 2
    while out.exists():  # never overwrite an existing vault file
        out = out_dir / f"{base} {n}.md"
        n += 1

    if DRY_RUN:
        print(f"would export: {out.relative_to(VAULT)}")
        exported.add(mid)
        return out

    out_dir.mkdir(parents=True, exist_ok=True)
    audio_name = copy_audio_into_vault(mid, out.stem) if COPY_AUDIO else None
    if has_frontmatter(body):
        content = body
    else:
        clean_body, attendees = transform_body(body)
        embed = f"![[{audio_name}]]\n\n" if audio_name else ""
        content = build_frontmatter(m, dt, audio_name, attendees) + embed + clean_body
    out.write_text(content)

    exported.add(mid)
    print(f"exported: {out.relative_to(VAULT)}")
    return out


def main():
    global VAULT, DRY_RUN
    parser = argparse.ArgumentParser(
        description="Export Anarlog meetings into an Obsidian vault as Markdown.")
    parser.add_argument("--vault", default=_env("ANARLOG_VAULT"),
                        help="Obsidian vault root (or set ANARLOG_VAULT).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be exported without writing anything.")
    parser.add_argument("--no-audio", action="store_true",
                        help="Do not copy/transcode meeting audio into the vault.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only consider the N most recent meetings.")
    args = parser.parse_args()

    if not args.vault:
        sys.exit("No vault set. Pass --vault /path/to/vault or set ANARLOG_VAULT.")
    VAULT = Path(args.vault).expanduser()
    if not VAULT.is_dir():
        sys.exit(f"Vault not found: {VAULT}")
    DRY_RUN = args.dry_run
    if args.no_audio:
        global COPY_AUDIO
        COPY_AUDIO = False

    exported = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    before = len(exported)

    new = 0
    for m in list_all_meetings(limit=args.limit):
        if process(m, exported) is not None and not DRY_RUN:
            new += 1

    if not DRY_RUN:
        STATE.write_text(json.dumps(sorted(exported)))
    print(f"done — {new} new, {len(exported)} total tracked"
          + (" (dry-run, nothing written)" if DRY_RUN else "")
          + (f", {len(exported) - before} newly seen" if DRY_RUN else ""))


if __name__ == "__main__":
    main()
