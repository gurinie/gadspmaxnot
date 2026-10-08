# Project agent instructions

<!-- BEGIN github-actions-cost-policy -->
## GitHub Actions cost controls

- Follow the [shared Actions policy](https://github.com/gurinie/github-actions-policy). Before adding the first workflow, install its pinned checker and declare the actual default branch and every hosted job in `.github/actions-minutes-budget.json`.
- Batch locally verified changes before pushing; inspect the first failing step before retrying. On an unchanged commit, rerun failed jobs only. Light draft checks never substitute for full ready-PR evidence; security findings remain blocking.
- Validation stays on hosted runners with bounded job/setup time, scoped superseded-run cancellation and no duplicate feature-branch push/PR triggers. Declare the cost of new jobs, runner changes and matrix copies; do not silently enlarge the budget.
- Before discretionary CI-triggering pushes or reruns, read fresh account billing and pending-run estimates. At 80% usage or when spending would breach the 500-minute release/incident reserve, require specific human approval for the commit and estimated new minutes. Unknown usage is unavailable, not zero.
- Preserve publishers, deployments, live operations and existing hooks. Never cancel or disable working execution or change account spending caps because usage is high. Do not publish or deploy an instruction-only change.
<!-- END github-actions-cost-policy -->
