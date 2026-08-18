# Fixtures

Frozen snapshots of real responses from each source, trimmed down for repo
size but otherwise unmodified — real field names, real KB numbers/CVEs/build
numbers, real HTML structure quirks. The point (see the "parser tests"
backlog item this replaces) is that when Microsoft/the dotnet team
restructures a page or feed, the *live* fetcher starts failing while these
tests keep passing against the old-but-real shape — that mismatch is the
signal to come update both the fixture and the parser together.

All pulled on 2026-08-17.

- `windows_release_health/windows_server.html` — trimmed `role="main"` from
  the real windows-server-release-info page: the full "major versions by
  servicing option" summary table (4 rows) plus two `<details>` release
  history blocks (Server 2022, Server 2016), each cut to 6 `<tr>` (the
  header row plus 5 data rows — one of which is an "OOB" release). Kept
  as-is: the header `<tr>` living inside `<tbody>` with no `<thead>` — a real
  quirk of Microsoft's markup that `_parse_history_table`/`_parse_summary_table`
  must filter out (see the "not row.find_all('td')" guards).

- `windows_release_health/windows11.html` — same shape, but from
  windows11-release-information, kept separate because the summary section
  here has a schema windows_server.html never exercises: instead of one
  "Extended support end date" column, mainstream (SAC) versions get a *pair*
  of "End of updates: <editions>" columns (Home/Pro vs Enterprise/Education),
  and there's a second, separate LTSC summary table for the same version
  codes. Includes 4 real SAC rows (26H1/25H2/24H2/23H2, one of which —
  23H2 — has already-ended "End of updates" text with no date in one column
  but a real date in the other), the LTSC table's single "24H2" row (real
  footnote marker in the version cell: `24H2<sup>1</sup>`), and one history
  `<details>` block (24H2, 7 rows) whose servicing-option cells read the
  combined "LTSC • General Availability Channel" — Windows 11 24H2 is both
  channels on the same build train — plus a real "D" (preview) row, which
  the server fixture doesn't have an example of.

- `msrc/updates.json` — real `GET /cvrf/v2.0/updates` response, trimmed from
  191 months to the latest 12 (enough to exercise the `MONTHS_TO_SCAN = 6`
  cutoff).

- `msrc/cvrf_2026_aug.json` — real August 2026 CVRF document, trimmed hard:
  the full response is ~6MB/798 vulnerabilities covering every Microsoft
  product. Kept: `DocumentTitle`, the `.NET Framework`-only `ProductTree.
  FullProductName` entries (`ProductTree.Branch` is unused dead weight —
  the fetcher only ever reads the flat `FullProductName` list), and 3 real
  vulnerabilities referencing those product IDs — two of which share one KB
  (5120702), which is what exercises the `seen_in_month` de-dup.

- `dotnet/releases_index.json` — real `releases-index.json`, trimmed from 14
  channels to 3: `9.0` (in support, has a `releases.json`), `1.0` (long-EOL,
  *also* has a `releases.json` — this is what the "always fetch full
  history, don't gate on support-phase" fix in `dotnet.py` depends on), and
  `3.1` (used by the test to simulate that channel's `releases.json` fetch
  failing, to check the per-channel error path doesn't take the whole
  fetcher down).

- `dotnet/releases_9_0.json`, `dotnet/releases_1_0.json` — real
  `releases.json` for those two channels, each release entry trimmed to the
  four fields the fetcher reads (`release-date`, `release-version`,
  `security`, `release-notes`) — the untrimmed entries also carry
  runtime/sdk/cve-list download metadata the fetcher never touches, ~40x the
  size for no test value.
