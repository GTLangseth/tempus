# Specs

Design specs for architectural work: new subsystems, or changes to how components fit
together or to interfaces others depend on. Small, well-scoped changes don't need one;
the Linear issue's description is the design.

## Where things live

| Where | Holds | Lifespan |
|---|---|---|
| Linear (`SPA-` issues) | What and when: backlog, priority, status, milestone | Until the issue is done |
| `docs/specs/` | How and why for one piece of work | Kept as a dated record; not rewritten after it's implemented |
| `docs/ARCHITECTURE.md` | What the system is now, plus the decision log | Always current |
| `docs/STACK.md` | Tool choices, open decisions, inspirations | Always current |

## Flow

1. **Issue first.** Work starts from a Linear issue.
2. **Spec on a branch.** Name the branch `spec/<topic>`, *without* the issue ID, and write
   `Refs SPA-25` in the PR body. Linear closes an issue when a PR whose branch carries the
   issue ID merges, and approving a spec shouldn't close the work. Name the file
   `YYYY-MM-DD-<topic>-design.md`. Its header lists status and the Linear issue; the issue
   links back to the spec.
3. **Review as a PR.** Merging the spec PR means the spec is approved.
4. **Plan, then sub-issues.** The implementation plan sits next to the spec
   (`YYYY-MM-DD-<topic>-plan.md`). Each group of plan tasks becomes a Linear sub-issue.
5. **Implementation PRs** use the sub-issue's Linear branch name, so merging closes that
   sub-issue. The last one applies the spec's
   "Docs to update" section and sets the spec's status to **Implemented**.
6. **Drift.** If implementation departs from the spec, add a short "Changes during
   implementation" note to the spec and a decision entry in `ARCHITECTURE.md`. Don't
   silently edit the original design.

## Statuses

`Draft` → `Approved` (merged) → `Implemented`, or `Superseded by <spec>`.
