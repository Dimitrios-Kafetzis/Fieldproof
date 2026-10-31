# Changelog

## [0.20.2](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.20.1...v0.20.2) (2026-10-31)


### Documentation

* **docs:** the M2 demo video, two depot cameras and the M2 sign-off ([#111](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/111)) ([2489768](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/2489768b9dd1caea6933ae9e030e00b59d0ab077))

## [0.20.1](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.20.0...v0.20.1) (2026-10-30)


### Bug fixes

* **engine:** retry an aborted Nav2 bringup within seconds, start the replay's data first ([#109](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/109)) ([920a93e](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/920a93e87180a60d581b5b497966506ce640b7e8))
* **repo:** the M2 demo's matrix row, and the demo run recorded in STATUS ([#106](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/106)) ([207f229](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/207f2293d2bbfab8a47b035dcb981a311c95cb89))

## [0.20.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.19.0...v0.20.0) (2026-10-29)


### Features

* **engine:** vehicle onboarding SDK, example vehicle repository and the M2 demo ([#101](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/101)) ([7ab52fa](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/7ab52fa42127e3f9179c0082ae430be64c4c96f4))

## [0.19.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.18.0...v0.19.0) (2026-10-28)


### Features

* **engine:** field log replay source, localisation assertion and synthetic UGV log ([#98](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/98)) ([b1c2e60](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/b1c2e60270a72c532eebc6b8b8a10e3930cd19b0))

## [0.18.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.17.1...v0.18.0) (2026-10-28)


### Features

* **engine:** run matrix, run database, scenario overrides and HTML reports ([#95](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/95)) ([b821825](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/b82182549c6ccdbc4f998f92a8e735ebacea365d))


### Build and dependencies

* **deps:** bump mkdocs-material in /tools/docs in the docs group ([#93](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/93)) ([e08df62](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/e08df628de037b953ae7d8cfda2f9cf502318a45))

## [0.17.1](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.17.0...v0.17.1) (2026-10-27)


### Build and dependencies

* **deps:** bump certifi to 2026.10.23 in both uv lockfiles ([#91](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/91)) ([cda8ea7](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/cda8ea76a5c6c670619115d247e65c6866306220))
* **deps:** bump mcap from 1.5.0 to 1.6.0 in /engine in the python group across 1 directory ([#86](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/86)) ([3a1f781](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/3a1f7812d837c434d6a3024a135b1ef30089b93b))
* **deps:** bump the node group across 1 directory with 5 updates ([#85](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/85)) ([6c247f6](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/6c247f6c5691a49a2c1a64936110eafc18d8d987))

## [0.17.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.16.0...v0.17.0) (2026-10-27)


### Features

* **engine:** assertion library v1, KPI collectors and the assertion plugin API ([#83](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/83)) ([5ba8558](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/5ba8558429a045e15eba54a663aae211cf59de69))


### Bug fixes

* **engine:** harden the image job: clock timeout, Nav2 bringup retry, kept-alive announce ([#84](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/84)) ([3cb5f80](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/3cb5f80856eca45ba0e4fda139c2cf903c272d32))


### Build and dependencies

* **deps:** bump the actions group with 3 updates ([#87](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/87)) ([e7dfa3f](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/e7dfa3f481fb9f29d4aa6cfe248e18ed2161f68a))

## [0.16.0](https://github.com/Dimitrios-Kafetzis/Fieldproof/compare/v0.15.1...v0.16.0) (2026-10-23)


### Features

* **engine:** clock management and determinism ([#76](https://github.com/Dimitrios-Kafetzis/Fieldproof/issues/76)) ([c6ec1d0](https://github.com/Dimitrios-Kafetzis/Fieldproof/commit/c6ec1d0b795b8919ccc4e893cd8e2ca334b62c17))

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
