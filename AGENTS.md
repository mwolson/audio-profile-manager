# Agent Instructions

## Project overview

aproman (audio-profile-manager) is a daemon that fixes HDMI audio after
suspend/resume on Linux systems running PipeWire + WirePlumber. It monitors
D-Bus for wake signals and cycles the card profile to force a node rebuild.

## Planning

Prefer to write plans in the `plans/` directory.

## Conventions

- Single-module Python 3 package (`aproman/`) containing the daemon.
- No external Python dependencies -- stdlib only.
- Keep code comments minimal.
- When making changes to data in existing code, try to keep things in
  alphabetical order when it's reasonable to do so.
- Prefer top-down control flow: caller first, then callee.
- When writing bash scripts: `#!/bin/bash`, 4-space indentation, fail-fast
  dependency checks.

## Key files

- `aproman/aproman.py` -- main script (daemon)
- `install.sh` -- legacy installer (use `aproman install-service` instead)
- `systemd/aproman.service` -- systemd user service definition (bundled in wheel
  via hatch `force-include`)
- `openrc-system/aproman` -- OpenRC system service init script (for OpenRC
  versions before 0.60)
- `openrc-user/aproman` -- OpenRC user service init script (for OpenRC 0.60+)

## Dev loop tools

### Running tests

Run unit tests with:

```sh
bun run test
```

This executes `python3 -m unittest discover -s tests -v`.

Run integration tests (requires Docker):

```sh
bun run test:integration
```

Run all tests:

```sh
bun run test:all
```

### Pre-commit hooks

Lefthook runs the following checks on commit (see `lefthook.yaml`):

- `md-format` -- Prettier formatting for Markdown files
- `ruff-check` -- linting via `uvx ruff check`
- `ty-check` -- type checking via `uvx ty check`
- `unit-tests` -- full unit test suite

Run checks against the working tree (no staging required):

```sh
bun run hooks:check
```

This runs the `pre-commit` hooks against all working tree files with
`--all-files --no-stage-fixed`, so there is no stashing and no auto-staging.
Prefer this for iterating on changes before committing.

## Releasing

### Pre-release steps

1. Check for uncommitted changes:

   ```sh
   git status
   ```

   If there are uncommitted changes, offer to commit them before proceeding.

2. Fetch latest tags to ensure we have the complete history:

   ```sh
   git fetch --tags
   ```

3. Run `bun run hooks:check` and confirm everything passes. CI (see
   `.github/workflows/publish.yml`) only runs on tag push, so this is the last
   opportunity to catch lint, type-check, and unit-test failures before publish.
   The pre-commit hook alone is not enough: its `glob` gate filters on
   `{staged_files}`, which can silently skip entire check groups when the staged
   set does not match.

4. Update the version in `pyproject.toml`, `package.json`, and the `VERSION`
   constant in `aproman/aproman.py`. Run `uv lock` to update `uv.lock`, then
   commit all four files together with message
   `chore: bump version to <version>`. This must be its own commit, not combined
   with other changes, unless the user explicitly agrees to that.

5. Push the version-bump commit:

   ```sh
   git push
   ```

   There is no branch-push CI to watch; the tag-push CI in the next step is what
   gates the PyPI publish.

6. Ask the user what tag name they want. Provide examples based on the current
   version:
   - If current version is `0.2.0`:
     - Minor update (new features): `0.3.0`
     - Bugfix update (patches): `0.2.1`

### Creating the release

When the user provides a version (or indicates major/minor/bugfix):

1. Create and push the tag:

   ```sh
   git tag v<version>
   git push origin v<version>
   ```

2. Wait for the tag-push CI to pass before drafting release notes. This run is
   what publishes to PyPI, so if it fails the release is incomplete:

   ```sh
   gh run list --limit 1         # grab the run id
   gh run watch <run-id> --exit-status
   ```

   If CI fails, fix the issue on `main`, delete the tag locally and remotely
   (`git push origin :refs/tags/v<version> && git tag -d v<version>`), re-tag,
   and push again.

3. Examine each commit since the last tag to understand the full context:

   ```sh
   git log <previous-tag>..HEAD --oneline
   ```

   For each commit, run `git show <commit>` to see the full commit message and
   diff. Commit messages may be terse or only show the first line in `--oneline`
   output, so examining the full commit is essential for accurate release notes.

4. Create a draft GitHub release:

   ```sh
   gh release create v<version> --draft --title "v<version>" --generate-notes
   ```

5. Enhance the release notes with more context:
   - Use insights from examining each commit in step 3
   - Group related changes under descriptive headings (e.g., "### Refactored X",
     "### Fixed Y")
   - Use bullet lists within each section to describe the changes
   - Include a brief summary of what changed and why it matters
   - Keep the "Full Changelog" link at the bottom
   - Update the release with `gh release edit v<version> --notes "..."`

   Ordering guidelines:
   - Put user-visible changes first (new features, bug fixes, breaking changes)
   - Put under-the-hood changes later (refactoring, internal improvements, docs)
   - Within each section, order by user impact (most impactful first)

6. Tell the user to review the draft release and provide a link:

   ```
   https://github.com/mwolson/aproman-py/releases
   ```
