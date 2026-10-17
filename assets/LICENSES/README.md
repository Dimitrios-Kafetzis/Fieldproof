# Asset licences

One file per vendored asset or asset set, named `<file>.LICENSE.txt` (for a single file) or
`<set>.LICENSE.txt` (for a directory under `assets/`), containing the licence text or its SPDX
identifier and URL, the source URL, the author and the date of the copy. Fuel models are not
vendored; their licences are recorded per model in `../fuel.lock` and must be permissive.
`tests/assets/test_assets.sh` checks both.
