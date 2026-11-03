# Job Calendar backup (backup_supabase.py)

Written by Claude on 2026-11-04. Plain instructions, no installs.

Supabase's free plan has no automatic backups, so this makes your own copy of the Job Calendar database
(schedule, clients, reports, service records, Job Book files) on the Mac. It only reads from Supabase. It never
changes or deletes anything there.

## Run it

    python3 ~/lighthouse-inspection/tools/backup_supabase.py

Takes about 5 seconds. It signs in with the same login the Inspection Inbox uses (the password is in the Mac
Keychain, nothing is stored in the script). No setup needed if the Inbox is already working.

## Where the backups go

`~/Backups/lpc-supabase/<date_time>/` for example `2026-11-04_0230`. The folder is private to you (700) and the
files inside are 600. It holds customer names, phones and emails, so the script refuses to run if the destination is
inside any git folder (it will not write under `~/lighthouse-inspection` or `~/business-context`).

Each folder has:

- one `<table>.json` per table (15 of them, one row per line, all rows)
- `storage_job-photos.json` and `storage_inspection-photos.json`: lists of the photo files (names, sizes, dates)
- `manifest.json`: row counts and a fingerprint (sha256) of every file
- `REPORT.txt`: the plain summary. Open this one first.

`~/Backups/lpc-supabase/LAST_BACKUP.json` is updated only when a run finishes with no problems. The Dashboard can read
it later for "Last backup".

It keeps the newest 14 folders and removes older ones, but only folders it made itself (name like
`2026-11-04_0230` and holding its own `manifest.json`). It does not tidy up after a run that had problems.

## Reading the result

- Last line `OK` and exit code 0: all good.
- `PARTIAL` and exit code 2: it finished but could not read something (for example a table the login isn't allowed to
  read). `REPORT.txt` lists exactly what. `LAST_BACKUP.json` is not updated and old folders are kept.
- `BACKUP FAILED` and exit code 1: nothing was saved (no connection, bad login, or a blocked destination).
- A table that isn't in the database yet is shown as "not in the database" in the report. That is a note, not a failure.
  The table list is fixed inside the script (from the SQL files in `~/lighthouse-supabase`), so a table made some other
  way would not be backed up.

When it runs from the nightly timer (no Terminal open) a failure also pops up a Mac notification.

## Photos

By default only the list of photo files is saved (photos are big, one report is about 14 MB).

    python3 ~/lighthouse-inspection/tools/backup_supabase.py --photos              all photo files
    python3 ~/lighthouse-inspection/tools/backup_supabase.py --photos --since 3    only photos from the last 3 days

A photo that hasn't changed since the last backup is linked to the earlier copy, not downloaded again, so repeat
runs cost very little extra disk. (On a drive that can't do that, such as exFAT, it downloads them again.)

## Put it on the external drive

    python3 ~/lighthouse-inspection/tools/backup_supabase.py --dest /Volumes/YOUR_DRIVE/lpc-backups

Same rules apply. `LAST_BACKUP.json` is written in whatever folder you pass to `--dest`.

## Check a backup

    python3 ~/lighthouse-inspection/tools/backup_supabase.py --verify
    python3 ~/lighthouse-inspection/tools/backup_supabase.py --verify ~/Backups/lpc-supabase/2026-11-04_0230

Re-fingerprints every file against `manifest.json` and checks the row counts. Prints `OK` or lists what is missing,
changed or extra. Add `--dest` if the backups are on the drive and you don't give the full folder path.

    python3 ~/lighthouse-inspection/tools/backup_supabase.py --restore-check

Checks every JSON file opens, and that the links between tables line up inside the backup: jobs to clients, jobs to
staff, photos to jobs, inspections to jobs, plus the smaller ones. It prints any "orphans" (a row pointing at
something that isn't in the backup). It also lists a couple of information-only links that the database doesn't
enforce. It talks to nothing; it only reads the folder. Both checks work on the newest folder if you don't name one.

Other switches: `--dry-run` (show what it would do, change nothing, no sign-in), `--prune-only` (just the clean-up,
add `--dry-run` to preview), `--keep 30` (keep more folders).

## Run it every night (not set up yet)

1. Copy `com.lighthouse.backup-supabase.plist.example` (same folder as this file) to
   `~/Library/LaunchAgents/com.lighthouse.backup-supabase.plist`
2. Open the copy and change each `YOUR_USERNAME` to your Mac username.
3. `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.lighthouse.backup-supabase.plist`
4. Try it now: `launchctl kickstart -k gui/$(id -u)/com.lighthouse.backup-supabase`, then look at
   `~/Library/Logs/lpc-backup.log`.
5. Switch off: `launchctl bootout gui/$(id -u)/com.lighthouse.backup-supabase`

It runs at 2:30 am. If the Mac is asleep it runs when it wakes. The first time it writes to an external drive from the
timer, the Mac may ask to let Python onto removable volumes: click Allow.

## Restoring (how it would go)

There is no automatic restore. The script is read-only on purpose, and a restore script hasn't been written or tested.
If Supabase ever lost the project, the steps would be:

1. Make a new Supabase project and run the SQL files in `~/lighthouse-supabase` in number order (the table and
   security setup first, 01 and then the others that add tables or columns). Skip the data imports; the backup replaces them.
2. Re-create the logins (Mitch and Andrew). The logins are NOT in this backup. A login gets a new id, and the `staff`
   rows, `jobs.staff_id`, `jobs.created_by`, `service_records.staff_id` and `inspections.staff_id` all hold the old
   login ids, so those ids need swapping to the new ones before loading.
3. Load the JSON files in this order so the links work: staff, clients, service_types, client_properties,
   service_prices, jobs, job_prices, job_photos, service_records, inspections, jobbook_state, jobbook_files,
   plan_blocks, day_notes, staff_prefs.
4. Photos: only if you ran with `--photos`. Upload them back into the two buckets using the same paths as in
   `storage_<bucket>.json`.

Ask Claude to write the loader when it's needed, and run `--restore-check` on the backup first.
