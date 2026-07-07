# Building LapScope from source (and verifying a release)

The published Windows exe is an unsigned-or-SignPath-signed PyInstaller
**onedir** build. This page covers building it yourself and verifying that a
downloaded release matches a build from source. If you just want to *use*
LapScope, grab the zip from the [Releases page](../../../releases) — you do not
need any of this.

## Prerequisites

- **Windows x64** (the release target; the exe is Windows-only).
- **Python 3.12.8** — the exact interpreter the release CI uses
  ([.github/workflows/release.yml](../.github/workflows/release.yml)). Matching
  the minor+patch keeps the build inputs reproducible.

## Build with the pinned inputs

The dependency set is frozen in [requirements-build.lock](../requirements-build.lock)
— every runtime and build package pinned to an exact version with SHA256
hashes. Installing with `--require-hashes` guarantees you get byte-identical
dependency wheels to the release:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install --require-hashes -r requirements-build.lock
pyinstaller LapScope.spec
```

Output: `dist\LapScope\LapScope.exe` plus its bundled runtime, static assets,
and `car_ordinals.json`.

Run it straight from the tree without packaging:

```powershell
python run_desktop.py
```

## Verifying a downloaded release

1. **Checksum.** Each release ships `checksums.txt` with the SHA256 of the zip:

   ```powershell
   (Get-FileHash LapScope-<version>-win64.zip -Algorithm SHA256).Hash
   ```

   It should match the line in `checksums.txt`.

2. **VirusTotal.** Upload the exe to <https://www.virustotal.com/> for an
   independent multi-engine scan (unsigned PyInstaller binaries draw occasional
   heuristic false positives — this is transparency against that).

3. **Rebuild + diff.** Build from source as above and compare the extracted
   `dist\LapScope` tree against the unzipped release. The Python bytecode,
   bundled assets, and `car_ordinals.json` will match.

### Caveat: the zip is not bit-for-bit reproducible

The goal here is **pinned, rebuildable inputs**, not a bit-identical archive.
Two things inject nondeterminism into the final zip's own hash even from
identical inputs:

- PyInstaller embeds build timestamps and absolute paths in a few bundle
  metadata files.
- `Compress-Archive` (used to make the release zip) stores file modification
  times.

So expect the *tree contents* to match on a rebuild, while the *zip's* SHA256
differs from the published one. Verify at the file/tree level, not the outer
zip, when reproducing.

### Regenerating the lock

If you bump a dependency, regenerate the lock **under the pinned interpreter**
so the resolution matches CI:

```powershell
pip install pip-tools
pip-compile --generate-hashes --allow-unsafe --output-file requirements-build.lock requirements.txt requirements-build.txt
```

`--allow-unsafe` is required so `setuptools` (pulled in by PyInstaller at
runtime) is pinned too, otherwise `--require-hashes` installs fail.

## Code signing (maintainers)

Releases are signed via **[SignPath Foundation](https://signpath.org/)** (free
certificates for OSS) when the signing credentials are configured; until then
they ship unsigned and rely on the checksum + VirusTotal path above. The signing
step in [.github/workflows/release.yml](../.github/workflows/release.yml) is
**inert by default** — it only runs when the `SIGNPATH_API_TOKEN` secret exists,
so forks and pre-enrollment tags still build a normal unsigned release.

To enable signing:

1. Apply to the SignPath Foundation OSS program and create a SignPath
   organization + project for LapScope.
2. In the repo, add:
   - secret `SIGNPATH_API_TOKEN`,
   - variable `SIGNPATH_ORGANIZATION_ID`,
   under **Settings -> Secrets and variables -> Actions**.
3. Adjust the `project-slug` / `signing-policy-slug` inputs on the
   *Submit signing request (SignPath)* step to match your SignPath project.

The workflow uploads the freshly built `dist/LapScope` as an artifact, submits
it to SignPath, downloads the signed tree back over the build, then zips and
checksums the signed result so `checksums.txt` always matches the published
asset.
