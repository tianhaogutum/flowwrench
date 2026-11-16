'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { loadFlows, loadTests } = require('./schema');
const { runSuite } = require('./runner');
const { spec, junit } = require('./reporters');
const { LoadError } = require('./errors');
const HELP = `nrflowtest 0.1.0 — offline headless Node-RED flow tests

Usage: nrflowtest run --flows flows.json --tests 'tests/*.yaml' [options]

  --tests PATTERN       YAML path/glob; repeat for multiple patterns
  --reporter TYPE       spec (default), json, junit (XML on stdout)
  --junit-file PATH     Also write XML beneath the current directory
  --strict-types        Reject unsupported types unless explicitly stubbed
  --allow-function      Execute trusted Function node JavaScript (not a sandbox)
  --dry-run             Validate and plan only; never start Node-RED
  --help, --version

Exit codes: 0 pass/valid dry-run, 1 test failure, 2 usage/load/report error.
SIGINT/SIGTERM: stop, reap active worker, report interruption; exit 130/143.
Inject nodes never start timers. External I/O nodes are never loaded.
`;
function parseArgs(argv) {
  if (argv.length === 1 && ['--help', '-h', '--version'].includes(argv[0])) return { info: argv[0] };
  if (argv[0] !== 'run') throw new LoadError('expected command "run"; use --help');
  const options = { tests: [], reporter: 'spec' }, seen = new Set();
  const valued = { '--flows': 'flows', '--tests': 'tests', '--reporter': 'reporter', '--junit-file': 'junitFile' };
  const flags = { '--strict-types': 'strictTypes', '--allow-function': 'allowFunction', '--dry-run': 'dryRun' };
  for (let i = 1; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--help') return { info: arg };
    if (seen.has(arg) && arg !== '--tests') throw new LoadError(`duplicate option ${arg}`);
    seen.add(arg);
    if (Object.hasOwn(flags, arg)) options[flags[arg]] = true;
    else if (Object.hasOwn(valued, arg)) {
      const value = argv[++i];
      if (!value || value.startsWith('--')) throw new LoadError(`${arg} needs a value`);
      if (arg === '--tests') options.tests.push(value); else options[valued[arg]] = value;
    } else throw new LoadError(`unknown option ${arg}`);
  }
  if (!options.flows || !options.tests.length) throw new LoadError('--flows and --tests are required');
  if (!['spec', 'junit', 'json'].includes(options.reporter)) throw new LoadError('--reporter must be spec, junit or json');
  if (options.dryRun && options.junitFile) throw new LoadError('--dry-run cannot write --junit-file');
  return options;
}
// Restrict report writes to the invocation directory; refuse traversal and symlinks.
function reportPath(relative) {
  if (path.isAbsolute(relative) || relative.split(/[\\/]/).includes('..')) throw new LoadError('--junit-file must be a relative path beneath the current directory');
  const root = fs.realpathSync(process.cwd()), target = path.resolve(relative);
  if (target === root || !target.startsWith(root + path.sep)) throw new LoadError('invalid report path');
  let current = root;
  for (const part of path.relative(root, target).split(path.sep)) {
    current = path.join(current, part);
    if (fs.existsSync(current) || (() => { try { return fs.lstatSync(current).isSymbolicLink(); } catch { return false; } })()) {
      const stat = fs.lstatSync(current);
      if (stat.isSymbolicLink()) throw new LoadError(`report path contains a symlink: ${current}`);
      if (current === target ? !stat.isFile() : !stat.isDirectory()) throw new LoadError(`invalid report path component: ${current}`);
    }
  }
  return target;
}
function writeReport(target, content) {
  fs.mkdirSync(path.dirname(target), { recursive: true });
  const temporary = path.join(path.dirname(target), `.nrflowtest-report-${randomUUID()}`);
  try { fs.writeFileSync(temporary, content, { flag: 'wx', mode: 0o600 }); fs.renameSync(temporary, target); }
  finally { fs.rmSync(temporary, { force: true }); }
}
async function main(argv, io = process) {
  const cancellation = new AbortController();
  const handlers = new Map(['SIGINT', 'SIGTERM'].map(signal => [signal, () => {
    // Keep both handlers installed until reports are written. Repeated signals
    // cannot exit the parent while it is waiting for worker reaping/cleanup.
    if (!cancellation.signal.aborted) cancellation.abort(signal);
  }]));
  const interruptionCode = () => cancellation.signal.reason === 'SIGINT' ? 130 : 143;
  try {
    const options = parseArgs(argv);
    if (options.info) { io.stdout.write(options.info === '--version' ? '0.1.0\n' : HELP); return 0; }
    const target = options.junitFile ? reportPath(options.junitFile) : null;
    const flows = loadFlows(options.flows);
    const cases = loadTests(options.tests, flows);
    if (target && [options.flows, ...cases.map(c => c.file)].some(f => fs.realpathSync(f) === target)) throw new LoadError('report path would overwrite an input file');
    for (const [signal, handler] of handlers) process.on(signal, handler);
    const report = await runSuite(flows, cases, { ...options, signal: cancellation.signal });
    if (report.interrupted) io.stderr.write(`nrflowtest: run interrupted by ${report.interrupted}; remaining cases were not run.\n`);
    for (const warning of report.warnings) io.stderr.write(`WARNING: ${warning}\n`);
    if (target) writeReport(target, junit(report));
    io.stdout.write(options.reporter === 'json' ? JSON.stringify(report, null, 2) + '\n' : options.reporter === 'junit' ? junit(report) : spec(report));
    return report.interrupted ? interruptionCode() : report.failed ? 1 : 0;
  } catch (err) {
    io.stderr.write(`nrflowtest: ${err.message}\n`);
    return cancellation.signal.aborted ? interruptionCode() : 2;
  } finally {
    for (const [signal, handler] of handlers) process.off(signal, handler);
  }
}
module.exports = { main, parseArgs, reportPath };
