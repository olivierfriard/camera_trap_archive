# CODEX.md

## Purpose

camera_trap_archive is a Flask/PostgreSQL application for managing data related to camera trap

## Working Rules

- Before changing files, first show the planned changes, then ask for confirmation before editing.
- Prefer small, localized changes.
- Do not rename form fields, POST keys, routes, or SQL columns unless explicitly required.
- Do not mix broad refactors with functional fixes unless requested.
- Preserve existing behavior unless the task explicitly requires changing it.
- docstring and comments in english

## HTML Templates

- Use the existing Bulma framework already present in the project.
- Prefer compact forms with related fields grouped on the same row.
- Do not break existing `name`, `id`, `value`, `onchange`, or `onclick` attributes.
- If template markup is repeated, prefer simple Jinja macros.
- Avoid unsupported Jinja patterns such as `**kwargs` in macro signatures.
- interface in italian

## Flask Backend

- Keep existing routes stable unless a route change is explicitly requested.
- If filtering can be added without risky SQL rewrites, prefer the least invasive approach.
- For new SQL queries, consider whether current indexes are sufficient.
- Do not remove legacy logic without first pointing it out.

## Database

- Before suggesting new indexes, check both the schema and the actual query patterns in the codebase.
- Call out duplicate or redundant indexes when found.
- Do not perform destructive database changes without explicit approval.

## Change Workflow

- First describe the intended modification.
- Then ask for confirmation before editing files.
- After editing, report only:
  - files changed
  - behavior changed
  - risks, constraints, or untested parts

## Verification

After each change, when practical, check:

- Jinja/template syntax correctness
- form field consistency
- route/template compatibility
- obvious regressions caused by the edit
