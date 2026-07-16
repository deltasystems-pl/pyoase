# Releasing `pyoase`

`pyoase` publishes to PyPI automatically: publishing a GitHub **release** triggers
`.github/workflows/ci.yml`'s `publish` job, which builds the sdist + wheel and uploads
them with the `PYPI_API_TOKEN` repository secret (`skip-existing` makes it idempotent).

## Cutting a release

1. Bump `version` in `pyproject.toml`, commit, push.
2. Tag it: `git tag -a vX.Y.Z -m "pyoase X.Y.Z" && git push origin vX.Y.Z`.
   (The tag version must match `pyproject.toml`.)
3. Create & **publish** the GitHub release for that tag — the `publish` job uploads to PyPI.
4. Confirm at <https://pypi.org/project/pyoase/>.

## Credentials

- The workflow uses the **`PYPI_API_TOKEN`** repository secret
  (Settings → Secrets and variables → Actions). Rotate it on PyPI as needed.
- Prefer **trusted publishing (OIDC)** instead of a stored token when convenient: add a
  GitHub trusted publisher on PyPI (project `pyoase`, owner `deltasystems-pl`, repo
  `pyoase`, workflow `ci.yml`), then drop the `password:` line and give the job
  `permissions: id-token: write`.

> The Home Assistant integration [`ha-oase`](https://github.com/deltasystems-pl/ha-oase)
> depends on `pyoase` from PyPI (`requirements` in its `manifest.json`). A fresh HACS
> install only succeeds once the matching `pyoase` version is on PyPI, so **publish
> `pyoase` first**, then release `ha-oase`.
