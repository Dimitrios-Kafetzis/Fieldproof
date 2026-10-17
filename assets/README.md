# assets

Vendored meshes, textures and other binary assets, and the lock file for the models fetched
from Gazebo Fuel. Rules ([04-engineering-standards.md](../docs/plan/04-engineering-standards.md),
[docs/reference/worlds.md](../docs/reference/worlds.md#the-asset-pipeline)):

- **Vendored assets** live under `assets/<set>/` with a licence file
  `assets/LICENSES/<file>.LICENSE.txt` or `<set>.LICENSE.txt` naming the source, the author,
  the licence (CC-BY and similar are fine; the repository itself is Apache-2.0) and the date of
  the copy. `tests/assets/test_assets.sh` fails on a tracked file without one. Files must stay
  below the 512 kB limit of the large-file hook, or go through Fuel. Assets that belong to one
  package (the UGV mesh, the world package's primitive models) stay in that package with their
  own licence file.
- **Fuel models are not vendored.** `fuel.lock` pins each one (owner, name, version, directory
  name, SPDX licence, archive size and SHA-256, the worlds using it) and
  `tools/assets/fetch_fuel.py` downloads, verifies and extracts them into `fuel/` (ignored by
  git): `make assets`, or the `fieldproof_worlds` build. Only permissive licences (CC0, CC-BY,
  Apache, MIT) are accepted, no NC or ND terms. Pin a new model with
  `tools/assets/fetch_fuel.py add OWNER/NAME --used-by <world>`.
- Files here are exempt from the formatters and linters (see the `exclude` lists in the tool
  configuration), so keep the lock tidy by hand or through the script.

```text
assets/
├── README.md
├── fuel.lock             pinned Fuel models
├── LICENSES/             one licence file per vendored asset or set
└── fuel/                 ignored: archives/<dir>-v<version>.zip, models/<dir>/ (fetched)
```
