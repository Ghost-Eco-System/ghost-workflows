# ghost-workflows

Reusable GitHub Actions workflows of the Ghost ecosystem: CI and signed releases of Ghost firmware (GOS RFC-0008, in the Ghost Extended Standard).

This repository is public because a reusable workflow can only be called from another organisation when it is public (GitHub Free). It holds no firmware code, no image and no secret.

## Workflows

| Workflow | Called on | Does |
|---|---|---|
| `firmware-ci.yml` | push, pull request | host tests (`pio test -e native`), build of each environment |
| `firmware-release.yml` | tag `vX.Y.Z` or `vX.Y.Z-rcN` | build, checks, manifests completed, Sigstore keyless signature, GitHub release (pre-release for `-rcN`) |

### In a firmware repo

`.github/workflows/ci.yml`:

```yaml
name: CI
on: [push, pull_request]
jobs:
  ci:
    uses: Ghost-Eco-System/ghost-workflows/.github/workflows/firmware-ci.yml@v1
    with:
      envs: nano
      libraries: ghost-field-core,ghost-field-arduino
      app-id: ${{ vars.GHOST_CI_APP_ID }}
    secrets:
      GHOST_CI_PRIVATE_KEY: ${{ secrets.GHOST_CI_PRIVATE_KEY }}
```

`.github/workflows/release.yml`:

```yaml
name: Release
on:
  push:
    tags: ['v*']
jobs:
  release:
    permissions:
      contents: write
      id-token: write
    uses: Ghost-Eco-System/ghost-workflows/.github/workflows/firmware-release.yml@v1
    with:
      envs: nano
      libraries: ghost-field-core,ghost-field-arduino
      app-id: ${{ vars.GHOST_CI_APP_ID }}
    secrets:
      GHOST_CI_PRIVATE_KEY: ${{ secrets.GHOST_CI_PRIVATE_KEY }}
```

`secrets: inherit` does not cross organisations: pass the secret by name, as above. The repo needs the variable `GHOST_CI_APP_ID` and the secret `GHOST_CI_PRIVATE_KEY` of the **Ghost CI** GitHub App (read-only access to the private libraries), and `scripts/firmware_dist.py` as a PlatformIO post script (it writes `firmware/`). ESP32 products declare `custom_fw_nvs_offset` in `platformio.ini`. When host tests use the local Ghost tree (`symlink://../../../core/ghost-eco-system/...`), set `local-core-tree: true`: the libraries of `platformio.ini` are cloned there at their pinned tags.

## Checks before a release (`scripts/ghost_release.py`)

- the tag equals the version of every manifest;
- no dirty build, and the commit of the tag;
- every part unchanged (size, SHA-256);
- AVR: the image ends below the bootloader;
- ESP32: the NVS partition, where the identity lives, is where the product declares it.

Then each manifest gets `source` (repo, tag, full commit) and, on ESP32, `nvs_offset`.

Tests: `python -m unittest discover -s tests`.

## Signature

Each manifest is signed with Sigstore keyless signing: the certificate names this workflow (`…/firmware-release.yml@refs/tags/v1`) and the source repository and tag. The bundle `<manifest>.sigstore.json` is attached to the release. The Ghost provisioning tool verifies it before any flash. The public Sigstore log records the repository name, tag, commit and time of each release; never the code or the images.

## Versions

Callers use the moving tag `v1`. Changes that keep the inputs move `v1`; a breaking change makes `v2`.
