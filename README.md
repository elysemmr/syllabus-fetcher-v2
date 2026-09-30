# syllabus-fetcher

Downloads course syllabi from MyFire/Brightspace (D2L) for a batch of course
codes, always as PDF. Login goes through a real, visible browser window so
you do your school's SSO (and any MFA) by hand; the script automates the
repetitive part: searching each course's content tree for something named
"syllabus" and saving it into one flat folder, named by course code.

**Run this on your own machine, not in a headless/remote environment** — the
login step needs a real window you can click through.

## Setup (one time)

**Windows:** double-click `setup.bat` (or run it from a terminal). It creates
the virtual environment and installs everything for you.

**Manual (any OS):**

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

The script drives your actual installed Google Chrome (via Playwright's
`channel="chrome"`), not a separate downloaded browser, so make sure Chrome
is installed and there's nothing else to fetch for it.

PDF conversion drives Microsoft Word itself (via the `docx2pdf` package
already in requirements.txt), so no extra software to install — just make
sure Word is installed and not blocked by a dialog box when the script runs.
Windows and macOS only. If Word isn't available, the script still downloads
the file but leaves it in its original format and warns you.

The first time you run the script, if `config.json` doesn't exist yet or has
no login URL saved, a small popup asks for your MyFire/Brightspace login URL
(the page you'd normally open in a browser to log in) and saves it to
`config.json` automatically — no manual editing needed, and you won't be
asked again on later runs. `config.json` is gitignored, since it holds your
personal login URL.

To set it (or change it) without waiting for the popup, or to adjust the
other settings, edit `config.json` directly (copy `config.example.json` to
`config.json` first if it doesn't exist):

- `base_url` — the URL that starts your MyFire/Brightspace SSO login.
- `home_url_fragment` — a substring of the URL Brightspace lands you on once
  login succeeds (default `/d2l/home`). Used to detect that login finished.
- `output_dir` — optional; defaults to `~/Desktop/Syllabi` if left `null`.

## Usage

```bash
python fetch_syllabi.py --courses CSE201,MATH150,ENGL101
# or
python fetch_syllabi.py --courses-file courses.txt
```

A Chrome window opens. Log in via SSO (the script waits up to 5 minutes).
After that, for each course code the script first tries Brightspace's own
REST API (the same one its web UI is built on) to look up the course and
find the syllabus as structured data -- no clicking at all when this works.
If the API isn't reachable or doesn't turn anything up, it falls back to
driving the browser UI itself:

1. Find and open the course automatically via Brightspace's course
   selector/search.
2. Open its Content page and expand all modules.
3. Find an item with "syllabus" in the name and download it.

Every downloaded file is saved flat into the output folder, named by course
code and converted to PDF if it wasn't one already (e.g. `Syllabi/CSE201.pdf`,
`Syllabi/MATH150.pdf`) — see the Word/docx2pdf note above.

### Bare course codes (no `--requester`)

An entry that is just a department and number, optionally with a trailing
course title (`psyc 4063`, `BIOL-1334`, `BBUS 2123 Macroeconomics`) names no
term or section. The script tries, in order -- entirely via the API, no
browser clicking -- its master course `PSYC_4063_ON_MC`, else
`PSYC_4063_TR_MC`, else its most recent section (the highest-numbered dated
offering, ignoring "do not use" copies): each candidate is checked for an
actual syllabus, not just picked by name, so if one has no content the
script moves on to the next rather than getting stuck. If none of them have
a syllabus, that entry is reported as failed in the summary and the script
moves on to the next course -- there's no manual-fallback prompt for this
path, since picking among several possible courses isn't something clicking
through the UI can help with. The file is named by what you typed
(`psyc_4063.pdf`). Entries that name a specific course (a year, section, or
full course code) are searched for as before, with the usual manual
fallback if automation can't find it.

### Manual fallback

Brightspace themes vary by school, so if a step can't be automated (course
not found, no Content link), the script pauses and asks you to do that one
step by hand in the open browser window, then press Enter in the terminal to
let it continue.

If it still can't auto-detect or auto-click the actual syllabus link/file
(e.g. it's buried inside another page in a way the script doesn't recognize),
it falls back to just waiting for **you** to click it yourself in the browser
-- it'll capture whatever results, whether that's a real download or a file
that opens directly in a new tab (like Chrome's built-in PDF viewer), and
still handles the saving, naming, and PDF conversion automatically. This
means you never have to manually "Save As" into the right folder with the
right name yourself.

### Fetching syllabi on someone else's behalf

If you're pulling syllabi for someone who sent you a list of ~20 course
codes/names that don't exactly match Brightspace's internal naming, use
`--requester`:

```bash
python fetch_syllabi.py --requester theirusername --courses-file their_list.txt
```

This looks up `theirusername` in Brightspace, pulls their real enrollment
log, and fuzzy-matches each entry in your list against it (tolerating
different separators/casing/extra words, e.g. "PSYC 4063" will match
`2024_US_PSYC_4063_70_ON_ONLN`), then downloads each matched course's
syllabus -- entirely via the API, no clicking. If an entry matches more than
one of their courses, it'll list the options and ask you which one they
meant. With no terminal to ask (e.g. a background run) it won't guess -- the
enrollment data doesn't say which section they took -- so that entry fails and
lists the candidates; put the year in the entry (`2024 PSYC 3063`) to pick one.
`--guess-newest` opts in to taking the newest, flagged as a guess in the
summary. A real student's log normally holds only their own section(s), so
this mostly comes up with staff/admin accounts, who are enrolled everywhere.

If an entry matches nothing in their log at all, it falls back to the course's
master course: the bare course code from the entry plus `ON MC` (`PSYC 4063`
-> `PSYC_4063_ON_MC`), or, if there's no `ON` master, `TR MC`
(`CSEC_4003_TR_MC`). Exact match only, searched in *your* account's
enrollments since a student's log won't hold master courses. The summary flags
such results as coming from the master course, and the first lookup scans your
whole enrollment list (about a minute), once per run.

A syllabus that is a link to a Google Doc is exported as it is *now*: the
document may have been edited since the student's term (or be shared by several
terms' courses), so those results are flagged `[live Google Doc, may differ
from that term]`.

When no syllabus is found, the summary says why: no matching course or master
course (naming the codes it looked for), or the course it did pick had no
syllabus link in its module descriptions or among its N searchable content
topics (noting any topic types the search doesn't read), or its syllabus topic
couldn't be downloaded.

It only ever looks in the one course it picked, because students expect the
syllabus from their own exact course: if that course has no syllabus, the
entry is reported as failed. Pass `--allow-other-sections` to instead fall
back to the other matching courses (newest first); the summary then flags
that the file came from a different course than the best match.

The log names the topic and module the syllabus came from (e.g. "Syllabus is
the topic 'CSEC 4003 Syllabus' (in 'Reference Information')"), so you can
check it against what the student sees.

**Requires an admin-level Brightspace account** (able to look up other
users and their enrollments) -- this mode has no click-based fallback, so if
the API can't resolve the username or find a syllabus for a matched course,
that entry is just reported as failed in the summary rather than falling
back to manual clicking.

### Session reuse

Your logged-in session is kept in `.auth/` (gitignored) so you don't have to
SSO in again on every run, as long as the session hasn't expired.

## Tuning selectors

If automated course search or "Expand All" never succeeds for your school's
theme, open `fetch_syllabi.py` and adjust the `*_CANDIDATES` selector lists
near the top of the file — the rest of the script doesn't need to change.

## Claude Code permissions

To scope Claude Code's write access to this project directory and the
default `~/Desktop/Syllabi` output folder (so it doesn't prompt for approval
on every action here), add a `.claude/settings.json` with:

```json
{
  "permissions": {
    "allow": [
      "Write(**)",
      "Edit(**)",
      "Write(~/Desktop/Syllabi/**)",
      "Edit(~/Desktop/Syllabi/**)",
      "Bash(mkdir -p ~/Desktop/Syllabi*)"
    ]
  }
}
```
