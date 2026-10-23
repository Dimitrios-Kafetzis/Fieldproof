# Changelog

## [0.15.1](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.15.0...v0.15.1) (2026-10-23)


### Bug fixes

* **engine:** judge a link's initial period from the start of the timeline ([#72](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/72)) ([b5e3510](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/b5e351041525eca2f76a2c54094404c8e6695d6d))

## [0.15.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.14.0...v0.15.0) (2026-10-22)


### Features

* **engine:** world events, fault injectors and the definition-of-done scenarios (P2-T03, session 2) ([#64](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/64)) ([ea84e55](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/ea84e5540a069c4db08f9f00b0623fafd00f5ccc)), closes [#65](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/65)


### Bug fixes

* **engine:** harden the load-sensitive checks the CI runner trips on ([#71](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/71)) ([4af9ccd](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/4af9ccd7a7356542aa11f415cf0ac341fb0c66d4))

## [0.14.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.13.0...v0.14.0) (2026-10-21)


### Features

* **engine:** timeline DSL, event plugins and the engine-side events (P2-T03, session 1) ([#60](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/60)) ([759c0ea](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/759c0eaa84fcd911cd35e4fc1bad7272f5855085))

## [0.13.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.12.1...v0.13.0) (2026-10-20)


### Features

* **engine:** emulate radio links between the vehicles and a ground station ([#57](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/57)) ([9b3e988](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/9b3e988320318475060adad744163b0d012b9479))

## [0.12.1](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.12.0...v0.12.1) (2026-10-20)


### Build and dependencies

* **deps:** bump the actions group across 1 directory with 2 updates ([#53](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/53)) ([2d38f13](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/2d38f137dad0ca042052f1aef250e57643f58636))

## [0.12.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.11.0...v0.12.0) (2026-10-18)


### Features

* **engine:** orchestrate fleets of vehicles with namespaces, isolation and budgets ([#49](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/49)) ([8dae9b3](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/8dae9b38d3769ee5bb93f6d0d233810436861790))

## [0.11.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.10.0...v0.11.0) (2026-10-18)


### Features

* **engine:** record a demo video of every run from the world's spectator camera ([#47](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/47)) ([54ed7ac](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/54ed7acb7ebf4bb59a70e550a27eba25e7b5fba8))

## [0.10.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.9.0...v0.10.0) (2026-10-18)


### Features

* **ci:** run the first scenario in CI and add the M1 demo ([#42](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/42)) ([e57f32a](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/e57f32a3888d6a4941fffedd0164521474dd7796))

## [0.9.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.8.0...v0.9.0) (2026-10-18)


### Features

* **engine:** add assertions v0, MCAP recording and run reports ([#39](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/39)) ([ed03651](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/ed03651fba81237c9a1c0802e3fa0edcb27d2b1b))

## [0.8.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.7.0...v0.8.0) (2026-10-17)


### Features

* **engine:** add scenario schema v0, the workspace configuration and the runner v0 ([#31](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/31)) ([92982bc](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/92982bc1ab68c4efb6183d8372f43c5987df79ce))


### Bug fixes

* **engine:** tune the example vehicle for a CPU-starved simulation host ([#37](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/37)) ([ccb6985](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/ccb69852a1ebffbc71404ca2022d9bad9d2f06dd))

## [0.7.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.6.0...v0.7.0) (2026-10-17)


### Features

* **engine:** add the vehicle readiness probe, the bridge image and the integration test ([#29](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/29)) ([95dea47](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/95dea47d1e10d6fa6b3472f164f08fc36010da2b))

## [0.6.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.5.0...v0.6.0) (2026-10-17)


### Features

* **engine:** add manifest schema v0, the vehicle image generator and the ugv_nav2 example vehicle ([#27](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/27)) ([d63cc32](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/d63cc32a6b3729eecd3a2debe2bbfb4c97b1379c))

## [0.5.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.4.0...v0.5.0) (2026-10-17)


### Features

* **bridge:** generate the ros_gz bridge bundle and contract tests from the vehicle manifest ([#25](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/25)) ([09f5f77](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/09f5f779b686f166aeb8cf5cb136563ffe92413b))

## [0.4.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.3.0...v0.4.0) (2026-10-17)


### Features

* **robots:** add the reference UGV model with sensors, manifest and spawn launch ([#21](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/21)) ([e07a071](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/e07a071f4fdc4345e7c191ffb2e9e2f27c103c9d))
* **sim:** add the depot reference world and the Fuel asset pipeline ([#23](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/23)) ([013ed84](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/013ed84517917256ae298a37c55fc4e3bfeda62c))

## [0.3.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.2.0...v0.3.0) (2026-10-16)


### Features

* **repo:** lay out the monorepo with a buildable, tested package per layer ([#18](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/18)) ([500bd89](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/500bd896b0424d28e7aefb374988b41087a4a0b1))


### Bug fixes

* **release:** read the engine version from version.txt so a release needs no re-lock ([#20](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/20)) ([197509e](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/197509eb768773a9b6f005a48bbc284c9261ab40))

## [0.2.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.1.0...v0.2.0) (2026-10-15)


### Features

* **docs:** publish the documentation site with MkDocs Material ([#15](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/15)) ([aa1b96a](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/aa1b96af61383ee90fe86d9052d59a2a5b17ece3))


### Bug fixes

* **ci:** name the SBOM artifacts with their extension so releases get them ([#14](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/14)) ([859c5b5](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/859c5b5fc56a0b31572eca4ea14b7e094ac8bd10))
* **ci:** replace the Dependabot docker updater with a weekly pins check ([#12](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/12)) ([9496fa7](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/9496fa72f5d987a9bdb312ff1c150f1ad77db388)), closes [#9](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/9)
* **docs:** retry the lychee image pull in the docs test and show its errors ([#17](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/17)) ([75d1ff6](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/75d1ff63cf9c2ccc11538b0d793c605f11e5c7ce))
* **release:** let a feat bump the minor version below 1.0 ([#16](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/16)) ([6320538](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/63205384c8cc6dbe76eecfe59b35a2c415e84e04))

## 0.1.0 (2026-10-15)


### Features

* **ci:** add the CI/CD pipeline with image publishing and releases ([#6](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/6)) ([4f74979](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/4f74979e9fc71e1b9929524817807c0dd498781b))


### Bug fixes

* **release:** start releases at 0.1.0 ([#11](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/11)) ([45a9c48](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/45a9c4865e343c4d786658f69616984b545ccd3d))


### Documentation

* **plan:** mark P0-T01 in review and record repository setup ([#1](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/1)) ([0fd5d63](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/0fd5d6313394f30dc385765a91620b934941493a))
