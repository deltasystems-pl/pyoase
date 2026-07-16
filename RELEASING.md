# Releasing `pyoase`

`pyoase` publishes to PyPI via **trusted publishing (OIDC)** — no API tokens. A GitHub
release triggers `.github/workflows/ci.yml`'s `publish` job, which builds and uploads.

## One-time PyPI setup (per project)

Do this once, before the first release, at <https://pypi.org>:

1. Sign in to PyPI → **Your projects** → **Publishing** (or **Account → Publishing** for a
   *pending* publisher if the project does not exist yet).
2. Add a **GitHub** trusted publisher with exactly:
   - **PyPI project name:** `pyoase`
   - **Owner:** `deltasystems-pl`
   - **Repository:** `pyoase`
   - **Workflow name:** `ci.yml`
   - **Environment:** `pypi`
3. In the GitHub repo, create an **Environment** named `pypi`
   (Settings → Environments → New environment). No secrets are needed.

## Cutting a release

1. Bump `version` in `pyproject.toml`, commit, push.
2. Tag it: `git tag -a vX.Y.Z -m "pyoase X.Y.Z" && git push origin vX.Y.Z`.
   (The tag version must match `pyproject.toml`.)
3. Create the GitHub release for that tag (a draft for `v0.1.0` already exists — just
   **Publish** it). Publishing fires the `publish` job → PyPI.
4. Confirm at <https://pypi.org/project/pyoase/>.

> The Home Assistant integration [`ha-oase`](https://github.com/deltasystems-pl/ha-oase)
> depends on `pyoase` from PyPI (`requirements` in its `manifest.json`). A fresh HACS
> install only succeeds once the matching `pyoase` version is on PyPI, so **publish
> `pyoase` first**, then release `ha-oase`.
