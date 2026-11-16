# Third-party sources and attribution

flowwrench is licensed under Apache-2.0 (see [LICENSE](LICENSE) and [NOTICE](NOTICE)).
Its commands were previously separate projects (some MIT, some Apache-2.0) written
by the same authors and are relicensed here under Apache-2.0. Unless a section
below says otherwise, all code, synthetic fixtures, tests and documentation are
original work; they contain no production exports, customer data, credentials,
execution payloads or copied issue attachments.

flowwrench is an independent interoperability project. It is not affiliated with
or endorsed by n8n GmbH, the OpenJS Foundation or Node-RED, Activepieces Inc.,
Windmill Labs or the Huginn project. Product names only identify interoperable
file formats. This file is attribution; it does not relicense any dependency or
upstream material.

The standalone projects kept their original research notes (RESEARCH.md,
specs.json, research/) for provenance. Those notes are not part of this
repository and are not needed to build, test or run flowwrench.

## Dependencies (installed separately, never vendored)

Dependencies are installed by your package manager under their own licenses and
keep their own notices in their distributions. The flowwrench wheel contains only
the `flowwrench` package and these project notices.

| Package | License | Used by | Kind |
| --- | --- | --- | --- |
| [PyYAML](https://github.com/yaml/pyyaml) | MIT, Copyright (c) 2017-2021 Ingy döt Net; (c) 2006-2016 Kirill Simonov | `huginn` (YAML kits, manifests and variable files) | runtime |
| [pytest](https://github.com/pytest-dev/pytest) | MIT, pytest developers | test runner | test |
| [Hatchling](https://github.com/pypa/hatch) | MIT, Ofek Lev and contributors | build backend | build |
| [hatch-fancy-pypi-readme](https://github.com/hynek/hatch-fancy-pypi-readme) | MIT, Hynek Schlawack | PyPI README link rewriting | build |

Python and its standard library (including `sqlite3`, `zoneinfo` and `fcntl`) are
provided by the user's Python installation under the
[PSF License Agreement and bundled notices](https://docs.python.org/3/license.html).
SQLite is [public domain](https://www.sqlite.org/copyright.html); no SQLite source
is vendored. `zoneinfo` reads the host's IANA timezone database; no timezone
database is vendored.

PyYAML is used under the following MIT notice:

> Copyright (c) 2017-2021 Ingy döt Net
> Copyright (c) 2006-2016 Kirill Simonov
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of
> this software and associated documentation files (the "Software"), to deal in
> the Software without restriction, including without limitation the rights to
> use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
> of the Software, and to permit persons to whom the Software is furnished to do
> so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

## `flowwrench n8n-diet` (formerly n8n-exec-diet)

No n8n implementation, application assets, customer data, or execution payloads
are copied or bundled. Table/column names and their relationships are interface
facts used to read a database belonging to the operator. All fixtures are
synthetic and authored for this project.

| Source | License / terms | Use |
| --- | --- | --- |
| [n8n](https://github.com/n8n-io/n8n) | [Sustainable Use License v1.0](https://github.com/n8n-io/n8n/blob/master/LICENSE.md); source-available, **not OSI open source**; enterprise files have separate terms | Public SQLite schema interface facts only; no code copied, imported or executed |
| [SQLite](https://www.sqlite.org/) | [Public domain](https://www.sqlite.org/copyright.html) | Python's existing `sqlite3` runtime; no SQLite source vendored |
| [Python](https://www.python.org/) | [PSF License Agreement and bundled notices](https://docs.python.org/3/license.html) | Standard library, including `sqlite3`; no interpreter vendored |

### Interface references

Narrow schema verification on 2026-11-11 used these upstream documents:

- [execution_entity table](https://github.com/n8n-io/n8n/blob/master/docs/generated/sqlite-schema/execution_entity.md)
- [execution_data table](https://github.com/n8n-io/n8n/blob/master/docs/generated/sqlite-schema/execution_data.md)
- [workflow_entity table](https://github.com/n8n-io/n8n/blob/master/docs/generated/sqlite-schema/workflow_entity.md)
- [Split execution data migration](https://github.com/n8n-io/n8n/blob/master/packages/%40n8n/db/src/migrations/sqlite/1690000000010-SeparateExecutionData.ts) — used to identify format boundaries, not copied as implementation.
- [SQLite VACUUM INTO](https://www.sqlite.org/lang_vacuum.html#vacuuminto)
- [SQLite locking protocol](https://www.sqlite.org/lockingv3.html)
- [SQLite WAL format and lock bytes](https://www.sqlite.org/walformat.html)

The supported structural subset is recorded in `tests/n8n_diet/fixtures/split-schema.sql`;
this is not a promise of compatibility with every n8n release.

### Demand evidence

- [n8n #37926](https://github.com/n8n-io/n8n/issues/37926): SQLite file size after execution pruning.
- [n8n #29555](https://github.com/n8n-io/n8n/issues/29555): execution data retained in the database.
- [n8n #39928](https://github.com/n8n-io/n8n/issues/39928): external binary cleanup context (outside this command's scope).

Issue content and upstream source are not relicensed by this project's license.

## `flowwrench n8n-refs` (formerly n8n-refcheck)

The implementation, tests and synthetic fixtures are original work. No n8n or
third-party linter implementation is copied, vendored, imported or invoked by the
offline command; its runtime uses the Python standard library only. The opt-in
`tests/n8n_refs/integration/n8n` recipe installs a pinned n8n package and invokes
its Server CLI for synthetic local acceptance; its dependency lock records upstream
package licenses/integrities, and no n8n source or binaries are distributed here.
Those optional dependencies retain their own licenses.

| Source | License / relationship | Use here |
| --- | --- | --- |
| [n8n](https://github.com/n8n-io/n8n), [license](https://github.com/n8n-io/n8n/blob/master/LICENSE.md) | Sustainable Use License v1.0; enterprise files have separate terms. This is source-available, **not an OSI open-source license**. | Interoperates with user-owned exported workflow JSON; node type identifiers and JSON field names are format facts. No n8n source is distributed. |
| [n8n issue 15981](https://github.com/n8n-io/n8n/issues/15981) | Public issue, demand evidence | Motivation: paired lookup across Merge inputs. No issue text or user workflow is copied into fixtures. |
| [n8n issue 18290](https://github.com/n8n-io/n8n/issues/18290) | Public issue, demand evidence | Motivation: references to unexecuted nodes. |
| [Payloadhq/n8n-workflow-linter](https://github.com/Payloadhq/n8n-workflow-linter), [Redsf/n8n-workflow-linter](https://github.com/Redsf/n8n-workflow-linter) | MIT, per supplied research | Scope comparison only: this command concentrates on data references. No dependency or copied code. |
| [n8n expression documentation](https://docs.n8n.io/build/work-with-data/expressions-versus-data-nodes.md), [node output reference](https://docs.n8n.io/build/work-with-data/transform-data/expression-reference/nodeoutputdata.md), [Code node](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.code.md) | Upstream documentation; consulted as interface reference on 2026-11-11 | Moustache expression blocks, selector properties and Code modes. No documentation content vendored. |

New fixtures contain invented nodes/data, no credentials or production exports.
The Slack-shaped nodes are unconfigured examples used only for offline analysis,
not runnable integrations.

## `flowwrench ap-promote` (formerly ap-flow-promote)

ap-promote is not an Activepieces product and has no third-party runtime
dependencies.

### Activepieces format reference

- Repository: https://github.com/activepieces/activepieces
- Consulted revision: `bf145a07489f5bc32a83e35e68564b861389aaed` (2026-11-11).
- License: MIT for the consulted core/web files. The repository has separately
  licensed enterprise directories; no enterprise implementation was copied or
  used. The upstream license notice is reproduced below.
- Copyright notice: Copyright (c) 2020-2024 Activepieces Inc.
- Usage: JSON format interoperability only. The parser, source-edit algorithm,
  CLI and tests were written for this tool. Fixtures were authored here with
  synthetic data and contain no copied customer exports, accounts or secrets.

Pinned source links, checked on 2026-11-11:

- [Action/branch structure](https://github.com/activepieces/activepieces/blob/bf145a07489f5bc32a83e35e68564b861389aaed/packages/core/execution/src/lib/flows/actions/action.ts)
- [Trigger structure](https://github.com/activepieces/activepieces/blob/bf145a07489f5bc32a83e35e68564b861389aaed/packages/core/execution/src/lib/flows/triggers/trigger.ts)
- [Flow version](https://github.com/activepieces/activepieces/blob/bf145a07489f5bc32a83e35e68564b861389aaed/packages/core/execution/src/lib/flows/flow-version.ts)
- [Template envelopes](https://github.com/activepieces/activepieces/blob/bf145a07489f5bc32a83e35e68564b861389aaed/packages/core/shared/src/lib/management/template/template.ts)
- [Web import envelope handling](https://github.com/activepieces/activepieces/blob/bf145a07489f5bc32a83e35e68564b861389aaed/packages/web/src/features/flows/utils/template-parser.ts)
- [Agent piece tool metadata](https://github.com/activepieces/activepieces/blob/bf145a07489f5bc32a83e35e68564b861389aaed/packages/core/piece-types/src/lib/agents.ts)
- [Connection format helper](https://github.com/activepieces/activepieces/blob/bf145a07489f5bc32a83e35e68564b861389aaed/packages/core/utils/src/lib/connection-template.ts)
- [Connection metadata/status](https://github.com/activepieces/activepieces/blob/bf145a07489f5bc32a83e35e68564b861389aaed/packages/core/shared/src/lib/automation/app-connection/app-connection.ts)
- [Sample-data settings](https://github.com/activepieces/activepieces/blob/bf145a07489f5bc32a83e35e68564b861389aaed/packages/core/execution/src/lib/flows/sample-data/index.ts)
- [License](https://github.com/activepieces/activepieces/blob/bf145a07489f5bc32a83e35e68564b861389aaed/LICENSE)

Motivation links: [#14866](https://github.com/activepieces/activepieces/issues/14866),
[#16113](https://github.com/activepieces/activepieces/issues/16113), and
[#16107](https://github.com/activepieces/activepieces/issues/16107).

**Research correction:** #14866's body, rechecked on 2026-11-11, documents
upstream improvements for flow-scoped replacement and embedded agent-tool
connections. Its historical limitations must not be described as unchanged
current behavior. This command adds a local, reviewable, file-based workflow.

The Activepieces license notice (formerly `licenses/Activepieces-LICENSE.txt`):

> Copyright (c) 2020-2024 Activepieces Inc.
>
> Portions of this software are licensed as follows:
>
> * All content that resides under the "packages/ee/" and "packages/server/api/src/app/ee" directory of this repository, if that directory exists, is licensed under the license defined in packages/ee/LICENSE
> * All third party components incorporated into the Activepieces Inc Software are licensed under the original license provided by the owner of the applicable component.
> * Content outside of the above mentioned directories or restrictions above is available under the "MIT Expat" license as defined below.
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

## `flowwrench huginn` (formerly huginn-scenario-kit)

This is not an official Huginn project. No Huginn implementation code or scenario
content has been copied. The implementation and all fixtures were written for
this project; fixtures use synthetic identifiers, data and `.invalid` endpoints.

### Huginn — format reference, MIT

- Project: https://github.com/huginn/huginn
- Copyright (c) 2013 Andrew Cantino (Iteration Labs, LLC).
- License: https://github.com/huginn/huginn/blob/master/LICENSE
- Inspected on 2026-11-11; upstream HEAD resolved by `git ls-remote` to
  `639c050b2bf4e14ef9983012af99dfc1dc68b334`.
- Export format: [lib/agents_exporter.rb](https://github.com/huginn/huginn/blob/639c050b2bf4e14ef9983012af99dfc1dc68b334/lib/agents_exporter.rb).
  Schema version 1; index-based source/receiver and controller/control_target;
  schedule is omitted for nonscheduled agents. Only contained links are exported.
- Import interpretation: [app/importers/scenario_import.rb](https://github.com/huginn/huginn/blob/639c050b2bf4e14ef9983012af99dfc1dc68b334/app/importers/scenario_import.rb).
  The importer consumes these same zero-based indices; schema version 1 retention
  values are seconds. It does not require exported_at for import.
- Schedule vocabulary: [app/models/agent.rb](https://github.com/huginn/huginn/blob/639c050b2bf4e14ef9983012af99dfc1dc68b334/app/models/agent.rb).
  The schedule names are interface facts, independently encoded here.
- [data/default_scenario.json](https://github.com/huginn/huginn/blob/639c050b2bf4e14ef9983012af99dfc1dc68b334/data/default_scenario.json)
  was inspected to check optional fields and Liquid-bearing option strings;
  it is not included in this repository.
- Motivation: [#813](https://github.com/huginn/huginn/issues/813),
  [#1644](https://github.com/huginn/huginn/issues/1644),
  [#2478](https://github.com/huginn/huginn/issues/2478).

### PyYAML — runtime dependency, MIT

- Project: https://github.com/yaml/pyyaml
- Distribution: https://pypi.org/project/PyYAML/ (the standalone tool pinned 6.0.3;
  flowwrench requires PyYAML >= 6).
- Copyright (c) 2017-2021 Ingy döt Net; (c) 2006-2016 Kirill Simonov.
- Installed as a dependency, not vendored. The full MIT notice (formerly
  `licenses/PyYAML-MIT.txt`) is reproduced in the Dependencies section above.

## `flowwrench schedules` (formerly cronherd)

No upstream application code is bundled, linked, executed, or required. Platform
names identify interoperable export formats; this project is not affiliated with
or endorsed by those projects.

All files in `tests/schedules/fixtures/` and `tests/schedules/examples/` are
original, synthetic, redistributable examples written for this project, not
exports of private workflows. They illustrate format shapes only; they are not
intended to be deployed as live automations.

| Project / authors | Upstream license | Use here |
| --- | --- | --- |
| [n8n / n8n GmbH and contributors](https://github.com/n8n-io/n8n) | [Sustainable Use License](https://github.com/n8n-io/n8n/blob/master/LICENSE.md); source-available, not OSI open source | Export field names and schedule semantics for interoperability only. No n8n code copied. |
| [Node-RED / OpenJS Foundation and contributors](https://github.com/node-red/node-red) | [Apache-2.0](https://github.com/node-red/node-red/blob/main/LICENSE) | Inject/flow metadata format; no code copied. |
| [Huginn contributors](https://github.com/huginn/huginn) | [MIT](https://github.com/huginn/huginn/blob/master/LICENSE) | Scenario agent enum meanings; independently expressed table and parser. No scheduler code copied. |
| [Windmill Labs and contributors](https://github.com/windmill-labs/windmill) | [Mixed upstream license](https://github.com/windmill-labs/windmill/blob/main/LICENSE): server/CLI generally AGPL-3.0, OpenAPI/spec/client exceptions Apache-2.0, enterprise exceptions proprietary | Schedule export/spec fields only. CLI metadata was inspected to confirm format. No AGPL or proprietary source is included or executed. |
| [Activepieces contributors](https://github.com/activepieces/activepieces) | [MIT outside enterprise directories](https://github.com/activepieces/activepieces/blob/main/LICENSE) | Public schedule piece trigger/input field meanings only; no enterprise files used, no code copied. |
| [Kelektiv node-cron contributors](https://github.com/kelektiv/node-cron) | [MIT](https://github.com/kelektiv/node-cron/blob/main/LICENSE) | n8n cron day matching behavior cross-check; no code copied or dependency. |
| [Jake Clarke / CronosJS contributors](https://github.com/jaclarke/cronosjs) | [MIT](https://github.com/jaclarke/cronosjs/blob/master/LICENSE) | Node-RED cron day matching documentation cross-check; no code copied or dependency. |

The implementation does not assert that historical issue reports are still open.
Predictions follow the host's installed tzdb version, so governments' rule changes
and differing host tzdb revisions can change future results.

## `flowwrench nodered-test` (Node.js companion nrflowtest, `packages/nrflowtest/`)

nrflowtest's original code and synthetic fixtures are licensed under Apache-2.0
(see [packages/nrflowtest/LICENSE](packages/nrflowtest/LICENSE),
[NOTICE](packages/nrflowtest/NOTICE) and
[THIRD_PARTY.md](packages/nrflowtest/THIRD_PARTY.md)). It is not part of the
Python wheel. The upstream runtime is a dependency, not a fork. No upstream
implementation code, flow examples, private flows, or credentials are vendored.
npm installs each dependency with its own license files; `npm-shrinkwrap.json`
records resolved versions and integrity hashes, including transitive dependencies.

| Component | Pinned version | License | Source / role |
| --- | --- | --- | --- |
| Node-RED | 5.0.9 | Apache-2.0 | [node-red/node-red](https://github.com/node-red/node-red), Copyright OpenJS Foundation and other contributors; actual flow runtime and selected core nodes |
| node-red-node-test-helper | 0.3.6 | Apache-2.0 | [node-red/node-red-node-test-helper](https://github.com/node-red/node-red-node-test-helper), Copyright JS Foundation and other contributors; in-memory runtime loader |
| yaml | 2.9.1 | ISC | [eemeli/yaml](https://github.com/eemeli/yaml), Eemeli Aro; declarative case parsing |
| glob | 14.0.5 | BlueOak-1.0.0 (package; individual source license notices also apply) | [isaacs/node-glob](https://github.com/isaacs/node-glob), Isaac Z. Schlueter and contributors; pattern expansion via API, never a shell |
| fast-xml-parser | 5.12.0 | MIT | [NaturalIntelligence/fast-xml-parser](https://github.com/NaturalIntelligence/fast-xml-parser), Amit Kumar Gupta; development-only independent JUnit parsing/validation |

Security patch overrides retain the main upstream runtime version:
`body-parser@1.20.9` ([Express](https://github.com/expressjs/body-parser), MIT),
`multer@2.5.0` ([Express](https://github.com/expressjs/multer), MIT), and
`npm@11.21.2` ([npm CLI](https://github.com/npm/cli), Artistic-2.0 and bundled
dependency licenses). npm is included by Node-RED's registry; nrflowtest does not
invoke it to install nodes or run workflows. npm ignores overrides declared by a
dependency, so installing nrflowtest as another project's dependency does not
guarantee those patches. An audit is not a security proof.

The helper's runtime internals (`_NodePrototype`) and pinned Node-RED subflow
cloning/wiring behavior were inspected in the locally installed Apache-2.0
packages. nrflowtest supplies its own instrumentation and declarative assertion
engine. The helper's server API is never called.

The nrflowtest brief cited [Node-RED issue #4956](https://github.com/node-red/node-red/issues/4956),
about errors disappearing during flow testing, and
[node-red-flow-tester](https://github.com/node-red/node-red-flow-tester), the
separate upstream editor plugin; nrflowtest does not use its source.

Node-RED is a trademark of the OpenJS Foundation. This project is not an OpenJS
Foundation or Node-RED project and is not endorsed by them.
