"""CLI; all analysis is offline and user code is never executed."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from urllib.parse import quote

from . import __version__
from .comparison import check_identity_context, compare
from .core import RULES, WorkflowError, analyze, load_workflow, rename


COMPARISON_FINGERPRINT_NAMESPACE = "n8n-refcheck/comparisonIdentity/v1"
SOURCE_ROOT_BASE_ID = "SRCROOT"


def _artifact_location(file, source_root=None):
    if source_root is None:
        return {"uri": Path(file).absolute().as_uri()}
    # Lexical checkout provenance: do not follow symlinks or probe inputs.
    path = Path(os.path.abspath(file))
    try:
        relative = path.relative_to(source_root)
    except ValueError:
        return {"uri": path.as_uri()}
    # POSIX surrogateescape names must round-trip the original filesystem bytes.
    uri_path = os.fsencode(relative.as_posix()) if os.name == "posix" else relative.as_posix()
    return {"uri": quote(uri_path, safe="/") if relative.parts else "./",
            "uriBaseId": SOURCE_ROOT_BASE_ID}


def _finding_properties(finding):
    return {"jsonPointer": finding["pointer"], "node": finding["node"],
            "reference": finding["reference"], "offset": finding["offset"]}


def _sarif_location(file, finding, source_root=None):
    return {"physicalLocation": {"artifactLocation": _artifact_location(file, source_root)},
            "logicalLocations": [{"fullyQualifiedName": finding["pointer"], "kind": "member"}]}


def _sarif_result(file, finding, source_root=None):
    return {
        "ruleId": finding["rule"], "ruleIndex": list(RULES).index(finding["rule"]),
        "level": finding["severity"],
        "message": {"text": finding["message"] + (f" (reference {finding['reference']!r})" if finding["reference"] is not None else "")},
        "locations": [_sarif_location(file, finding, source_root)],
        "properties": _finding_properties(finding),
    }


def _sarif_document(results, errors, automation_id=None, source_root=None):
    rules = [{"id": rule, "name": title.replace(" ", ""), "shortDescription": {"text": title},
              "defaultConfiguration": {"level": level}} for rule, (level, title) in RULES.items()]
    notifications = []
    for error in errors:
        prefix = (error.get("side", "") + " ").lstrip() if "side" in error else ""
        prefix += f"{error['file']}: " if "file" in error else ""
        notification = {"level": "error", "message": {"text": prefix + error["message"]}}
        if "file" in error:
            # A load failure identifies an input artifact, not a source region.
            notification["locations"] = [{"physicalLocation": {
                "artifactLocation": _artifact_location(error["file"], source_root)}}]
        if "file" in error and "category" in error:
            notification["properties"] = {"category": error["category"]}
        notifications.append(notification)
    document = {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0", "runs": [{
        "tool": {"driver": {"name": "n8n-refcheck", "version": __version__, "rules": rules}},
        "results": results,
        "invocations": [{"executionSuccessful": not errors, "toolExecutionNotifications": notifications}],
    }]}
    if automation_id is not None:
        document["runs"][0]["automationDetails"] = {"id": automation_id}
    if source_root is not None:
        uri = source_root.as_uri()
        document["runs"][0]["originalUriBaseIds"] = {
            SOURCE_ROOT_BASE_ID: {"uri": uri if uri.endswith("/") else uri + "/"}}
    return document


def sarif(reports, errors, eligibility=None, automation_id=None, source_root=None):
    results = []
    for report in reports:
        identities = report.get("identities")
        for index, finding in enumerate(report["findings"]):
            result = _sarif_result(report["file"], finding, source_root)
            if identities is not None:
                result["partialFingerprints"] = _partial_fingerprints(identities[index])
            results.append(result)
    document = _sarif_document(results, errors, automation_id, source_root)
    if eligibility is not None:
        document["runs"][0]["properties"] = {"fingerprintEligibility": {
            "namespace": COMPARISON_FINGERPRINT_NAMESPACE, "artifacts": eligibility,
        }}
    return document


def _partial_fingerprints(identity):
    # v1 hashes only comparison identity: never provenance, state, messages
    # or decoded-source offsets. Shared by complete check/compare results.
    encoded = json.dumps(identity, sort_keys=True, ensure_ascii=True,
                         separators=(",", ":")).encode("utf-8")
    return {COMPARISON_FINGERPRINT_NAMESPACE: hashlib.sha256(encoded).hexdigest()}


def comparison_sarif(baseline, candidate, comparison, errors, eligibility=None, automation_id=None, source_root=None):
    """Use candidate provenance for current results, baseline for absent ones.

    JSON Pointers and decoded-source offsets are not physical JSON regions.
    An incomplete scan cannot establish baselineState, including absence.
    """
    files = {"baseline": baseline, "candidate": candidate}
    results = []
    if comparison is not None:
        for status, state in (("new", "new"), ("resolved", "absent"), ("unchanged", "unchanged")):
            side = "baseline" if status == "resolved" else "candidate"
            for entry in comparison[status]:
                result = _sarif_result(files[side], entry[side], source_root)
                result["properties"]["comparisonSide"] = side
                result["properties"]["identity"] = entry["identity"]
                if comparison["complete"]:
                    result["baselineState"] = state
                    result["partialFingerprints"] = _partial_fingerprints(entry["identity"])
                if status == "unchanged":
                    old = _sarif_location(baseline, entry["baseline"], source_root)
                    old["message"] = {"text": "Baseline location"}
                    old["properties"] = _finding_properties(entry["baseline"])
                    result["relatedLocations"] = [old]
                results.append(result)
    document = _sarif_document(results, errors, automation_id, source_root)
    document["runs"][0]["properties"] = {"comparison": {
        **{side: _artifact_location(file, source_root) if source_root is not None else
           _artifact_location(file)["uri"] for side, file in files.items()},
        "complete": comparison is not None and comparison["complete"],
        "renames": comparison["renames"] if comparison is not None else [],
    }}
    if eligibility is not None:
        document["runs"][0]["properties"]["fingerprintEligibility"] = {
            "namespace": COMPARISON_FINGERPRINT_NAMESPACE,
            "eligible": comparison is not None and comparison["complete"] and
                        all(side["eligible"] for side in eligibility.values()),
            "sides": eligibility,
        }
    return document


def fingerprint(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise WorkflowError("in-place rename requires a regular file (no symlinks)")
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size, stat.S_IMODE(info.st_mode)


def atomic_write(path, text, expected=None):
    """Publish a complete file. New output uses link() for no-clobber semantics."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                         prefix=".n8n-refcheck-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if expected is not None:
            if fingerprint(path) != expected:
                raise WorkflowError("input changed during rename; no replacement written")
            os.chmod(temporary, expected[-1])
            os.replace(temporary, path)
        else:
            # Refuse existing outputs (including symlinks), atomically.
            os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parser():
    p = argparse.ArgumentParser(prog="flowwrench n8n-refs", description="Offline n8n data-reference analysis and safe node rename")
    p.add_argument("--version", action="version", version=__version__)
    commands = p.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="check one or more exported workflow objects")
    check.add_argument("files", nargs="+")
    check.add_argument("--format", choices=["text", "json", "sarif"], default="text")
    check.add_argument("--fail-on", choices=["error", "warning"], default="error")
    comparison = commands.add_parser("compare", help="compare baseline and candidate reference hazards")
    comparison.add_argument("baseline")
    comparison.add_argument("candidate")
    comparison.add_argument("--format", choices=["text", "json", "sarif"], default="text")
    comparison.add_argument("--fail-on-new", action="store_true", help="exit 1 only for new hazards at the severity threshold")
    comparison.add_argument("--fail-on", choices=["error", "warning"], default="error")
    for command in (check, comparison):
        command.add_argument("--sarif-automation-id", metavar="ID",
                             help="stable SARIF stream ID; 1-128 ASCII characters matching "
                                  "[A-Za-z0-9][A-Za-z0-9._/-]*; requires --format sarif")
        command.add_argument("--sarif-source-root", metavar="DIR",
                             help="existing lexical checkout root for SRCROOT-relative artifact URIs; "
                                  "requires --format sarif; symlinks are not resolved")
    refactor = commands.add_parser("rename", help="rename one node and literal references")
    refactor.add_argument("file")
    refactor.add_argument("--from", dest="old", required=True)
    refactor.add_argument("--to", dest="new", required=True)
    output = refactor.add_mutually_exclusive_group()
    output.add_argument("-o", "--output", type=Path)
    output.add_argument("--in-place", action="store_true")
    refactor.add_argument("--dry-run", action="store_true", help="emit proposed workflow to stdout; never write files")
    return p


def main(argv=None):
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    if args.command in ("check", "compare") and args.sarif_source_root is not None:
        if args.format != "sarif":
            argument_parser.error("--sarif-source-root requires --format sarif")
        try:
            if not args.sarif_source_root or "\x00" in args.sarif_source_root:
                raise ValueError("empty or NUL path")
            root = Path(os.path.abspath(args.sarif_source_root))
            if not root.is_dir():
                raise ValueError("not a directory")
        except (OSError, ValueError):
            argument_parser.error("--sarif-source-root must name an existing directory")
        args.sarif_source_root = root
    if args.command in ("check", "compare") and args.sarif_automation_id is not None:
        if args.format != "sarif":
            argument_parser.error("--sarif-automation-id requires --format sarif")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", args.sarif_automation_id) is None:
            argument_parser.error("--sarif-automation-id must be 1-128 ASCII characters matching "
                                  "[A-Za-z0-9][A-Za-z0-9._/-]*")
    if args.command == "compare":
        return compare_command(args)
    if args.command == "check":
        reports, errors, eligibility = [], [], []
        for file in args.files:
            reasons = ["load-error"]
            try:
                data = load_workflow(file)
                findings = analyze(data)
                report = {"file": file, "findings": [f.to_dict() for f in findings]}
                if args.format == "sarif":
                    report["identities"], reasons = check_identity_context(data, findings)
                reports.append(report)
            except (WorkflowError, OSError, RecursionError, ValueError) as exc:
                errors.append(_file_error(file, exc))
            if args.format == "sarif":
                eligibility.append({**_artifact_location(file, args.sarif_source_root),
                                    "eligible": not reasons, "reasonCodes": reasons})
        if args.format == "json":
            print(json.dumps({"version": __version__, "reports": reports, "errors": errors}, ensure_ascii=True, indent=2))
        elif args.format == "sarif":
            print(json.dumps(sarif(reports, errors, eligibility, args.sarif_automation_id,
                                   args.sarif_source_root), ensure_ascii=True, indent=2))
        else:
            for report in reports:
                for f in report["findings"]:
                    detail = f" (reference {f['reference']!r}, consumer {f['node']!r})" if f["reference"] is not None else ""
                    print(f"{report['file']}:{f['pointer']}: {f['rule']} {f['severity']}: {f['message']}{detail}")
                if not report["findings"]:
                    print(f"{report['file']}: OK")
            for error in errors:
                print(f"{error['file']}: {error['message']}", file=sys.stderr)
        if errors:
            return 2
        return int(any(f["severity"] == "error" or args.fail_on == "warning" for report in reports for f in report["findings"]))
    try:
        source = Path(args.file)
        before = fingerprint(source) if args.in_place and not args.dry_run else None
        data = load_workflow(source)
        if before is not None and fingerprint(source) != before:
            raise WorkflowError("input changed while reading")
        result = rename(data, args.old, args.new)
        rendered = json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False) + "\n"
        if args.dry_run or (not args.in_place and args.output is None):
            sys.stdout.write(rendered)
        elif args.in_place:
            atomic_write(source, rendered, before)
        else:
            atomic_write(args.output, rendered)
        return 0
    except (WorkflowError, OSError, RecursionError, ValueError) as exc:
        print("flowwrench n8n-refs: " + _safe_error(exc), file=sys.stderr)
        return 2


def compare_command(args):
    workflows, errors = {}, []
    result = None
    for side in ("baseline", "candidate"):
        file = getattr(args, side)
        try:
            workflows[side] = load_workflow(file)
        except (WorkflowError, OSError, RecursionError, ValueError) as exc:
            errors.append({"side": side, **_file_error(file, exc)})
    eligibility, findings = None, {}
    if args.format == "sarif":
        eligibility = {}
        # Qualify both sides independently, even if loading or identity on the
        # peer prevents comparison. Metadata must not add/replace diagnostics.
        for side in ("baseline", "candidate"):
            reasons = ["load-error"]
            if side in workflows:
                try:
                    analyzed = analyze(workflows[side])
                    _, reasons = check_identity_context(workflows[side], analyzed)
                    findings[side] = analyzed
                except (WorkflowError, OSError, RecursionError, ValueError):
                    pass
            eligibility[side] = {**_artifact_location(getattr(args, side), args.sarif_source_root),
                                 "eligible": not reasons, "reasonCodes": reasons}
    if not errors:
        try:
            result = compare(workflows["baseline"], workflows["candidate"],
                             _findings=findings if len(findings) == 2 else None)
            if not result["complete"]:
                errors.append({"message": "comparison incomplete: resolve R007 findings in both inputs first"})
        except (WorkflowError, RecursionError, ValueError) as exc:
            errors.append({"message": _safe_error(exc)})
    if args.format == "json":
        print(json.dumps({"version": __version__, "baseline": args.baseline, "candidate": args.candidate,
                          "comparison": result, "errors": errors}, ensure_ascii=True, indent=2))
    elif args.format == "sarif":
        print(json.dumps(comparison_sarif(args.baseline, args.candidate, result, errors, eligibility,
                                          args.sarif_automation_id, args.sarif_source_root), ensure_ascii=True, indent=2))
    else:
        if result is not None:
            for renamed in result["renames"]:
                print(f"rename: {renamed['from']!r} -> {renamed['to']!r} (id {renamed['id']!r})")
            for status in ("new", "resolved", "unchanged"):
                for entry in result[status]:
                    side = "baseline" if status == "resolved" else "candidate"
                    finding = entry[side]
                    print(f"{getattr(args, side)}:{finding['pointer']}: {status}: "
                          f"{finding['rule']} {finding['severity']}: {finding['message']} "
                          f"(reference {finding['reference']!r}, consumer {finding['node']!r})")
            print("comparison: " + ", ".join(f"{len(result[key])} {key}" for key in ("new", "resolved", "unchanged")))
        for error in errors:
            print("flowwrench n8n-refs: " + (f"{error['side']} {error['file']}: " if "side" in error else "") + error["message"], file=sys.stderr)
    if errors:
        return 2
    return int(args.fail_on_new and any(entry["candidate"]["severity"] == "error" or args.fail_on == "warning"
                                       for entry in result["new"]))


def _file_error(file, exc):
    error = {"file": file, "message": _safe_error(exc)}
    if isinstance(exc, WorkflowError) and exc.category is not None:
        error["category"] = exc.category
    return error


def _safe_error(exc):
    if isinstance(exc, OSError):
        return exc.strerror or "filesystem error"
    if isinstance(exc, RecursionError):
        return "excessive input nesting"
    return str(exc)
