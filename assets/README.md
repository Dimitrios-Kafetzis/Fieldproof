# assets

Vendored meshes, textures and other binary assets, and the lock file for assets fetched from
Gazebo Fuel. Rules ([04-engineering-standards.md](../docs/plan/04-engineering-standards.md)):

- Every vendored asset has a licence file under [`LICENSES/`](LICENSES/README.md) naming its
  source, author and licence (CC-BY and similar are fine; the repository itself is Apache-2.0).
- Fuel models are not vendored; `fuel.lock` pins their versions and a fetch script downloads them
  at build time (P1-T02).
- Files here are exempt from the formatters and linters (see the `exclude` lists in the tool
  configuration) and must stay below the 512 kB limit of the large-file hook, or go through Fuel.
