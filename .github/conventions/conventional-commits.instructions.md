---
applyTo: "**"
---

Use the [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/#specification)
specification for every git commit message you write or suggest in this
workspace.

## Format

```
<type>[optional scope][!]: <description>

[optional body]

[optional footer(s)]
```

- `<type>` and `<description>` are mandatory; everything else is optional.
- A blank line MUST separate the header from the body, and the body from the
  footer block.
- Hard-wrap the body at 72 characters. The header (first line) SHOULD stay
  ≤ 72 characters and MUST stay ≤ 100.
- Write the description in the imperative mood, lower case, no trailing period
  (e.g. `add cron retry`, not `Added cron retry.`).

## Allowed types

| Type       | Use when…                                                          |
|------------|--------------------------------------------------------------------|
| `feat`     | A new feature is introduced (correlates with SemVer MINOR).        |
| `fix`      | A bug is fixed (correlates with SemVer PATCH).                     |
| `docs`     | Documentation-only changes.                                        |
| `style`    | Formatting/whitespace changes that do not affect behaviour.        |
| `refactor` | Code change that neither fixes a bug nor adds a feature.           |
| `perf`     | Code change that improves performance.                             |
| `test`     | Adding, fixing, or refactoring tests.                              |
| `build`    | Changes to build system, packaging, or external dependencies.      |
| `ci`       | Changes to CI configuration files and pipelines.                   |
| `chore`    | Other changes that don't modify src or test files.                 |
| `revert`   | Reverts a previous commit (body MUST start with `Reverts <hash>`). |

Use additional types only if the project explicitly defines them.

## Scope

- Optional. A noun in parentheses describing the area of code affected, e.g.
  `feat(parser):`, `fix(api): …`, `chore(deps): …`.
- Lower-case kebab-case. Reuse existing scopes in the repo when possible
  (search recent `git log` before inventing a new one).
- Omit the scope rather than guessing.

## Breaking changes

A commit introduces a breaking change when EITHER:

1. The header type/scope is followed by `!` before the colon, e.g.
   `feat(api)!: drop legacy /v0 routes`.
2. A footer line begins with `BREAKING CHANGE:` (or `BREAKING-CHANGE:`),
   followed by a description of the break.

Prefer using BOTH for visibility on truly breaking changes. A breaking change
correlates with a SemVer MAJOR bump regardless of the commit type.

## Body

- Explain *what* and *why*, not *how* — the diff already shows how.
- Use bullet points (`* `) for multiple discrete changes; keep paragraphs for
  prose.
- Reference user-visible behaviour, regressions fixed, or design decisions
  worth recording.
- Do not paste large code blocks; link to files/lines instead.

## Footers

Footers use `Token: value` (or `Token #value`) and follow the
[git trailer](https://git-scm.com/docs/git-interpret-trailers) convention.
Common tokens:

- `BREAKING CHANGE:` — required for any breaking change not flagged with `!`.
- `Refs:`, `Closes:`, `Fixes:` — link issues/PRs (e.g. `Closes: #123`).
- `Co-authored-by: Name <email>` — credit collaborators.
- `Signed-off-by: Name <email>` — DCO sign-off.

`BREAKING CHANGE` is the only token that allows whitespace inside it; all
others use a single hyphen or no space.

## Examples

Simple fix:

```
fix(parser): handle trailing comma in array literal
```

Feature with scope and body:

```
feat(api): add bearer token rotation endpoint

Allow clients to rotate their bearer token without re-authenticating
through the OAuth flow. The new endpoint accepts the current token and
returns a freshly signed JWT with the same scopes.

Refs: #482
```

Breaking change via `!` and footer:

```
feat(cli)!: rename --conf to --config

BREAKING CHANGE: the short-form `--conf` flag has been removed.
Update all scripts and docs to use `--config` instead.
```

Revert:

```
revert: feat(cron): persistent job store

Reverts 1f2e3d4c. The migration causes startup deadlocks on SQLite
backends; will reland after switching to WAL mode.
```

## Workflow rules

- Never bundle unrelated changes into a single commit. Split them into
  separate commits, each with its own conventional message.
- When the user says "commit", inspect `git status` and `git diff` first;
  surface anything that looks unrelated to the user's intent before
  committing.
- Prefer multi-line messages via `git commit -F <file>` (or `-m` repeated)
  rather than embedding `\n` in shell strings — quoting is fragile.
- Do NOT auto-push unless the user explicitly asks. Pushing is a separate
  step from committing.
- Do NOT use `--no-verify`, `--amend` on published commits, or
  `git push --force` without explicit user approval.
