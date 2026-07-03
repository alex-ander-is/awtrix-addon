# AWTRIX App Release Rules

For `awtrix-addon`, a user request to finish, commit, push, publish, or release
the current change set authorizes this complete delivery sequence unless they
explicitly exclude a step:

1. Run the relevant local regression/smoke tests and `git diff --check`; fix
   any failures before proceeding.
2. Bump `awtrix-addon/config.yaml` to the next App version, align
   `awtrix-addon/pyproject.toml` to the same version, and add a matching new
   section to `awtrix-addon/CHANGELOG.md`. Released changelog sections are
   immutable.
3. Commit the change set with a behavior-focused subject and useful body.
4. Create the matching annotated release tag `v<version>` on that commit. Do
   not reuse or move an existing tag without explicit user approval.
5. Push `main` and the new tag together to `origin`; verify the branch and tag
   point at the intended commit.

Do not require separate user prompts for tests, version/changelog updates, tag,
or push once this delivery sequence is authorized. Do not perform the sequence
for work explicitly marked as local-only, no-commit, no-tag, or no-push.
