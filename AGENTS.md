# Project agent instructions

<!-- BEGIN github-actions-cost-policy -->
## GitHub Actions cost controls

- Enroll new owned repositories before their first workflow with `python3 /Users/ivangurin/github-actions-policy/bootstrap_project.py --root <absolute-project-path> --repository gurinie/<repository>`. Use the actual default branch; add no compliance workflow to a project without CI.
- Follow the [shared Actions policy](https://github.com/gurinie/github-actions-policy), available locally at `/Users/ivangurin/github-actions-policy`. Declare workflows, hosted jobs, runners, limits and matrix bounds in `.github/actions-minutes-budget.json`; keep the checker content-pinned. Forecasts are advisory and workflow changes receive normal PR review.
- Task authorization covers normal commits, pushes, PR validation and necessary CI fixes. The 3,000 included minutes are a savings target and the 500-minute release/incident reserve is a planning warning. At 80%, after allowance exhaustion or during billing outages, do not ask for per-action cost approval. Preserve existing GitHub spending caps; cap increases, paid infrastructure changes and protective-limit exceptions require separate approval.
- Keep the push hook offline and preserve existing hooks. Billing and pending-run estimates belong to the read-only monitor, never push admission. Missing/unfamiliar evidence is unavailable, never zero. Notify once per meaningful finding and retain unresolved findings through partial outages.
- Batch locally verified fixes. Drafts run inexpensive checks; ready PRs run complete selected coverage. Dependency/security findings remain blocking before expensive tests. Diagnose failures first; allow one justified failed-job-only rerun on an unchanged commit, then investigate any identical failure instead of repeating it.
- Validation uses hosted runners, a default 20-minute job limit (maximum 30), package setup capped at five minutes, bounded network waits, same-workflow PR/ref cancellation and no duplicate feature-branch push/PR validation. Update declarations with new jobs, runners or fan-out; do not bypass policy failures.
- Preserve publishers, deployments, client operations and existing dollar caps. Production deployment approval remains separate. Instruction-only changes must not deploy. Never stop existing operations to control minutes. Monitoring stays local and read-only, with hourly billing/jobs and daily workflow inventory.
<!-- END github-actions-cost-policy -->
