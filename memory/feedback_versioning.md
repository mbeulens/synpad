---
name: SynPad versioning workflow
description: How to handle version bumps, commits, branches for the SynPad project
type: feedback
originSessionId: 755c183c-86ca-4b4e-b566-7d088fcb2c9d
---
Git branching: `dev` for active work, `main` for stable releases.

**Why:** User wants stable main branch, all development on dev, merge to main on minor/major releases.

**How to apply:**
- Work on `dev` branch (git checkout dev)
- Every change to synpad.py: bump patch in APP_VERSION, commit to dev, push origin dev
- When user says "release" or wants minor/major bump:
  1. git checkout main
  2. git merge dev
  3. Bump minor/major version
  4. git commit, git tag, git push origin main --tags
  5. git checkout dev
- Commit message format: `v1.8.3: short description` with co-author line
- Desktop launcher points to repo dir (follows whichever branch is checked out)
