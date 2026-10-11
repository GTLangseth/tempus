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
2. **One branch, one PR.** The spec, its implementation plan and the code all land on the
   issue's Linear branch, in a single PR; merging it closes the issue. Name the spec
   `YYYY-MM-DD-<topic>-design.md` and the plan `YYYY-MM-DD-<topic>-plan.md`. The spec's
   header lists status and the Linear issue; the issue links back to the PR.
3. **Agree the design before coding.** Talk the design through (in chat or as an early
   draft commit on the branch) until it's settled, then write the plan and build. Large
   plans can fan out across parallel agents; no separate PRs or sub-issues per task group.
4. **Before merging,** apply the spec's "Docs to update" section and set its status to
   **Implemented**.
5. **Drift.** If implementation departs from the spec, add a short "Changes during
   implementation" note to the spec and a decision entry in `ARCHITECTURE.md`. Don't
   silently edit the original design.

## Statuses

`Draft` → `Implemented` (merged with its code), or `Superseded by <spec>`.
