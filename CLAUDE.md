# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Topovert is a **greenfield project** — at the time of writing the repository contains only `README.md` and IDE config under `.idea/`. There is no source code, build system, dependency manifest, or tests yet, and no commits on the `master` branch. Expect to be establishing structure rather than fitting into it.

When you add the first real code, update this file with the actual build/test/run commands and architecture.

## What the project is meant to do

Topovert converts topographical maps into target formats via easily configurable conversions. The initial concrete goal:

- Take freely available [Swisstopo](https://www.swisstopo.admin.ch/de/digitale-karten) topographical data.
- Produce `.IMG` files installable on Garmin navigation devices (see the [OSM Map On Garmin / IMG File Format wiki](https://wiki.openstreetmap.org/wiki/OSM_Map_On_Garmin/IMG_File_Format)).

Design intentions from the README that should shape early decisions:

- **Base-map selection** should let the user pick area / geodata / scale / etc. based on what Swisstopo offers — prefer reusing Swisstopo's own selection tooling over reimplementing it.
- **Cross-platform** (Linux, macOS, Windows). A browser-based approach (with WASM where needed) is one idea under consideration, not a settled decision. Research is expected before committing.

## Language / toolchain not yet decided

The `.idea/` config is contradictory: `misc.xml` and `topovert.iml` describe a JDK 21 Java module, while `go.imports.xml` and an AWS Toolkit config are also present, and Go is installed in the environment. **The implementation language has not been settled.** Do not assume Java or Go — confirm the intended stack before scaffolding, and avoid leaning on the stale `.idea/` module type as evidence either way.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
