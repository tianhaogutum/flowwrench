# Third-party sources and licenses

nrflowtest's original code and synthetic fixtures are licensed under Apache-2.0.
The upstream runtime is a dependency, not a fork. No upstream implementation code,
flow examples, private flows, or credentials are vendored here. npm installs each
dependency with its own license files; `npm-shrinkwrap.json` (the distributable npm lockfile) records resolved
versions and integrity hashes, including transitive dependencies.

| Component | Pinned version | License | Source / role |
| --- | --- | --- | --- |
| Node-RED | 5.0.9 | Apache-2.0 | [node-red/node-red](https://github.com/node-red/node-red), OpenJS Foundation and contributors; actual flow runtime and selected core nodes |
| node-red-node-test-helper | 0.3.6 | Apache-2.0 | [node-red/node-red-node-test-helper](https://github.com/node-red/node-red-node-test-helper), JS Foundation and contributors; in-memory runtime loader |
| yaml | 2.9.1 | ISC | [eemeli/yaml](https://github.com/eemeli/yaml), Eemeli Aro; declarative case parsing |
| glob | 14.0.5 | BlueOak-1.0.0 (package; individual source license notices also apply) | [isaacs/node-glob](https://github.com/isaacs/node-glob), Isaac Z. Schlueter and contributors; pattern expansion via API, never a shell |
| fast-xml-parser | 5.12.0 | MIT | [NaturalIntelligence/fast-xml-parser](https://github.com/NaturalIntelligence/fast-xml-parser), Amit Kumar Gupta; development-only independent JUnit parsing/validation |

Security patch overrides retain the main upstream runtime version:
`body-parser@1.20.9` ([Express](https://github.com/expressjs/body-parser), MIT),
`multer@2.5.0` ([Express](https://github.com/expressjs/multer), MIT), and
`npm@11.21.2` ([npm CLI](https://github.com/npm/cli), Artistic-2.0 and bundled
dependency licenses). npm is included by Node-RED's registry; nrflowtest does not
invoke it to install nodes or run workflows. The original upstream specifications
use older ranges for these packages. The overrides remove the high-severity
findings observed in the unpatched dependency tree under the documented
package-root installation (`npm ci`). npm ignores overrides declared by a
dependency, so installing this CLI as another project's dependency does not
guarantee those patches; see the recorded
[dependency audit](research/dependency-audit.json) for remaining moderate
findings in the bundled npm dependency chain. An audit is not a security proof.

The helper's runtime internals (`_NodePrototype`) and pinned Node-RED subflow
cloning/wiring behavior were inspected in the locally installed Apache-2.0
packages. nrflowtest supplies its own instrumentation and declarative assertion
engine. The helper's server API is never called.

## Research provenance

The supplied [RESEARCH.md](RESEARCH.md), [specs.json](specs.json), and
[research/evidence-index.md](research/evidence-index.md) remain in the repository.
They cover the wider OSS100 research batch and are not implementation claims or
license grants for those other tools. Existing ignored `research/raw/` artifacts
are not required at runtime and are not packaged.

The nrflowtest brief cited:

- [Node-RED issue #4956](https://github.com/node-red/node-red/issues/4956),
  about errors disappearing during flow testing.
- [node-red-flow-tester](https://github.com/node-red/node-red-flow-tester),
  the separate upstream editor plugin. nrflowtest does not use its source.

This implementation continues that supplied research; it does not claim to have
repeated or independently verified the entire batch's market research.
