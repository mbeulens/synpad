---
name: Git project rules
description: Universal rules for Git projects — branch layout, per-edit patch bumps, minor/major release workflow
type: feedback
originSessionId: c0a7a286-a012-4f9e-b45c-05cf32451f23
---
Git/GitHub project workflow that applies to every project the user works on with Claude.

**Why:** User wants consistent, predictable git behavior across all projects. Every edit produces a versioned commit; releases are explicit events that update docs and merge to the stable branch.

**How to apply:**

1. **Session start / context:** State which GitHub repo and branch you are currently working on. If no repo is selected, ask whether to create one.

2. **Branch layout:** New repositories always start with `master` and `dev` branches. You always work in `dev`. Honor existing conventions for pre-existing repos (e.g., SynPad uses `main` instead of `master`).

3. **Per-edit commits — patch bump:** Each time you modify a source file, bump the patch version (`0.1.0` → `0.1.1` → `0.1.2`), commit to `dev`, push. Patch bumps do not touch CHANGELOG.md or README.md. Memory/docs-only changes do not require a version bump.

4. **"Bump minor":** When the user says this, first create/update `CHANGELOG.md` and update `README.md` with the new version info, commit to `dev`, then merge `dev` → `master` (or `main` for existing repos using that name) and push both branches. Version goes `0.1.x` → `0.2.0`.

5. **"Bump major":** Same sequence as bump minor — update `CHANGELOG.md` + `README.md`, commit to `dev`, merge `dev` → `master`, push both. Version goes `0.1.x` → `1.0.0`.

6. **Commit message format:** Prefix with the new version, e.g. `v0.1.1: short description`. Include co-author trailer `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.

7. **Tags:** Tag minor and major releases on the stable branch (`master`/`main`). Patch releases on `dev` are not tagged.
