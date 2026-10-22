# Profile maintenance

Source for the public profile README at `benhammoush/benhammoush`. Edit `profile.json` for personal details. The original Matrix animation combines green character rain and the information panel into a GIF; a static PNG is saved in `assets/profile.png`.

## Counting rules

- **Repos:** public repositories owned by `benhammoush`, excluding forks. Includes empty, archived, and profile repositories.
- **Commits:** distinct commit SHAs authored by the GitHub account in eligible repositories' default-branch histories. The profile repository is excluded from commit and line statistics to prevent generated assets from inflating them.
- **PRs:** authored public pull requests, across all owners and all states, from GitHub issue search. Search indexing can lag.
- **Green (+N):** activity in the rolling 24 hours ending at the start of the last successful refresh, hidden when zero. PRs use creation time; commits use their Git committed timestamp, not push/merge time.
- **Lines:** lifetime additions minus deletions across counted commits. The green additions and red deletions in parentheses are **last 24 hours only**, using the same committed-time window as the recent commit counter. Both line deltas remain visible when zero, labeled `24h`. Historical net change is not current source-code size; generated files and merge diffs are not filtered.

Future-dated commits are excluded from recent activity. Commits only on unmerged branches, commits in other owners' repositories, private activity, and author emails GitHub cannot associate with the account are excluded. Squash merges count as their resulting commit. Identical SHAs are counted once across repositories, for both total and recent statistics. Each repository's branch tip is pinned during pagination; the overall refresh is not a cross-repository atomic snapshot. The inclusive UTC window is computed once per refresh. Daily snapshot counts are not live and can be almost a day old.

## Local refresh

Python 3.13 and Pillow 13.0.0. The generator uses Consolas on Windows or DejaVu Sans Mono on Linux. Ubuntu GitHub-hosted runners include DejaVu fonts; different system fonts can produce small typography differences.

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
$env:GITHUB_TOKEN = gh auth token
try { .venv/Scripts/python update_profile.py } finally { Remove-Item Env:GITHUB_TOKEN }
.venv/Scripts/python -m unittest discover -s tests -v
```

Only public repository endpoints/filters are used, even if the local token has private access. No token is stored in output. To regenerate artwork with existing saved stats without fetching GitHub data, run `.venv/Scripts/python generate_matrix.py`.

## Automated refresh

The workflow runs at approximately **05:23 UTC daily** or via **Run workflow**. It installs the pinned Pillow dependency, runs offline tests, fetches stats, and generates the 80-frame, 12-second GIF and static PNG. Its built-in `GITHUB_TOKEN` is sufficient; no personal access token is required. API failures, rate limits, incomplete searches, or render/validation errors fail before replacing existing output. Only changed `assets/profile.gif`, `assets/profile.png`, and `assets/stats.json` are committed.

GitHub may delay schedules, cache images, or disable scheduled workflows in inactive public repositories after 60 days. Branch protection may prevent the workflow from pushing; inspect Actions failures rather than weakening protections automatically. GIF animation cannot reliably respect reduced-motion settings in GitHub READMEs. The README intentionally shows only the animation, with empty alt text at the owner's request; profile information is not exposed to screen readers through that image. The static PNG and stats JSON are available directly in `assets`. Image text is rasterized and email inside the image is not clickable.
