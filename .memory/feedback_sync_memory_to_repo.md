---
name: Sync memory to repo
description: Every time Claude project memory changes for Synpad, copy updated files to repo/.memory/, commit, and push
type: feedback
originSessionId: 51f70466-38d3-4a1a-9d2b-93e13f09c7eb
---
When any memory file for this project is created or updated, also copy it to `/home/beuner/Development/Local/Synpad/repo/.memory/`, then commit and push to git.

**Why:** User wants memory files version-controlled in the repo so they're accessible regardless of which working directory Claude is started from.

**How to apply:** After every memory write/update, sync the changed file(s) to `repo/.memory/`, `git add`, commit, and `git push`.
