"""Piece file rendering and safe, atomic output writing (port of ``src/generate.js``)."""

from __future__ import annotations

import os
import re
import shutil
import stat as _stat
import tempfile
from collections.abc import Mapping
from functools import lru_cache
from importlib import resources
from types import MappingProxyType
from typing import Any, NamedTuple

from ._js import js_stringify, js_truthy, to_utf8
from .model import build_model
from .spec import fail, property_expression

__all__ = ["VERSIONS", "DEPENDENCY_OVERRIDES", "RenderedPiece", "render_piece", "write_piece"]

VERSIONS: Mapping[str, str] = MappingProxyType(
    {"framework": "0.32.0", "common": "0.12.5", "typescript": "5.9.3", "axios": "1.20.0", "nodeTypes": "22.19.0"}
)
DEPENDENCY_OVERRIDES: Mapping[str, str] = MappingProxyType(
    {
        "axios": VERSIONS["axios"],
        "zod": "4.6.6",
        "nanoid": "3.3.18",
        "form-data": "4.0.6",
        "deepmerge-ts": "8.0.2",
        "ai": "6.0.214",
        "expr-eval": "npm:expr-eval-fork@3.0.3",
    }
)

_CUSTOM_FIELD_DESCRIPTION = (
    "Sensitive connection credential. This field is visible text in framework 0.32.0; use a private connection editor."
)
_CUSTOM_AUTH_DESCRIPTION = (
    "Fill the credentials required by your selected action and Authentication choice. "
    "OAuth2 fields take a manually obtained access token."
)
_BASE_URL_DESCRIPTION = "Absolute HTTP(S) API base URL, without credentials, query or fragment."
_REQUIREMENT_DESCRIPTION = (
    "Choose one complete security requirement. Protected choices require all their credentials; "
    "Anonymous sends none and needs no connection. There is no automatic fallback."
)
_SCHEME_DESCRIPTION = (
    "Choose one security scheme. Only its connection credentials are sent; there is no automatic fallback."
)


class RenderedPiece(NamedTuple):
    """``{ model, files }``: ``files`` maps relative paths to contents in generation order."""

    files: dict[str, str]
    model: dict[str, Any]


def _json(value: Any) -> str:
    return js_stringify(value, 2) + "\n"


def _literal(value: Any) -> str:
    """Input strings are always JS string literals, never interpolated source expressions."""
    return js_stringify(value).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


@lru_cache(maxsize=None)
def _template(name: str) -> str:
    # Read bytes: text mode would translate newlines, Node reads files verbatim.
    package = __name__.rpartition(".")[0]
    return resources.files(package).joinpath("templates").joinpath(name).read_bytes().decode("utf-8")


def _auth_source(auth: dict[str, Any]) -> str:
    kind = auth["kind"]
    s = auth["schemes"][0] if auth["schemes"] else None
    if kind == "none":
        expression = "PieceAuth.None()"
    elif kind in ("apiKey", "bearer"):
        assert s is not None
        expression = f"PieceAuth.SecretText({_literal({'displayName': s['name'], 'required': True})})"
    elif kind == "basic":
        options = {"required": True, "username": {"displayName": "Username"}, "password": {"displayName": "Password"}}
        expression = f"PieceAuth.BasicAuth({_literal(options)})"
    elif kind == "oauth2":
        assert s is not None
        options = {"required": True, "authUrl": s["authUrl"], "tokenUrl": s["tokenUrl"], "scope": s["scopes"]}
        expression = f"PieceAuth.OAuth2({_literal(options)})"
    else:  # custom
        props: list[str] = []
        for scheme in auth["schemes"]:
            fields = [scheme["usernameField"], scheme["passwordField"]] if scheme["kind"] == "basic" else [scheme["id"]]
            # 0.32.0's CustomAuth metadata schema excludes SecretText children despite
            # the public TS union accepting them. Use its supported input and report it.
            for field in fields:
                field_options = {"displayName": field, "required": False, "description": _CUSTOM_FIELD_DESCRIPTION}
                props.append(f"{_literal(field)}: Property.ShortText({_literal(field_options)})")
        expression = (
            f'PieceAuth.CustomAuth({{ required: true, description: "{_CUSTOM_AUTH_DESCRIPTION}", props: {{\n    '
            + ",\n    ".join(props)
            + "\n  } })"
        )
    return "import { PieceAuth, Property } from '@activepieces/pieces-framework';\n\nexport const pieceAuth = " + expression + ";\n"


def _pick(source: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Destructure-and-rebuild like ``({ a, b }) => ({ a, b })``: absent keys stay undefined (omitted)."""
    return {key: source[key] for key in keys if key in source}


def _action_source(action: dict[str, Any], auth: dict[str, Any]) -> str:
    base_url_options: dict[str, Any] = {"displayName": "Base URL", "description": _BASE_URL_DESCRIPTION, "required": True}
    if js_truthy(action["baseUrl"]):
        base_url_options["defaultValue"] = action["baseUrl"]
    props = [f"base_url: Property.ShortText({_literal(base_url_options)})"]
    requirements = action.get("securityRequirements")
    if requirements:
        options = [
            {
                "label": " + ".join(js_stringify(n) for n in g["names"]) if g["names"] else "Anonymous",
                "value": g["value"],
            }
            for g in requirements
        ]
        dropdown = {
            "displayName": "Authentication requirement",
            "required": True,
            "description": _REQUIREMENT_DESCRIPTION,
            "options": {"options": options},
        }
        props.append(f"auth_requirement: Property.StaticDropdown({_literal(dropdown)})")
    elif action.get("securityChoice"):
        dropdown = {
            "displayName": "Authentication",
            "required": True,
            "description": _SCHEME_DESCRIPTION,
            "options": {"options": [{"label": s["name"], "value": s["name"]} for s in action["security"]]},
        }
        props.append(f"auth_scheme: Property.StaticDropdown({_literal(dropdown)})")
    for p in action["props"]:
        expression = property_expression(p["schema"], display_name=p["name"], description=p["description"], required=p["required"])
        props.append(f"{_literal(p['id'])}: {expression}")
    operation: dict[str, Any] = {
        "path": action["path"],
        "method": action["method"],
        "baseUrl": action["baseUrl"],
        "props": [_pick(p, ("id", "name", "in", "required", "schema")) for p in action["props"]],
        "body": {"mediaType": action["body"]["mediaType"]} if action["body"] else None,
        "security": [
            _pick(s, ("id", "name", "kind", "in", "key", "usernameField", "passwordField")) for s in action["security"]
        ],
    }
    if action.get("securityChoice"):
        operation["securityChoice"] = True
    if requirements:
        operation["securityRequirements"] = requirements
    require_auth = len(action["security"]) > 0 and not any(len(g["names"]) == 0 for g in (requirements or []))
    return (
        "import { createAction, Property } from '@activepieces/pieces-framework';\n"
        "import { pieceAuth } from '../auth';\n"
        "import { execute, Operation } from '../runtime';\n"
        "\n"
        f"const operation: Operation = {_literal(operation)};\n"
        "\n"
        f"export const action_{action['id']} = createAction({{\n"
        f"  name: {_literal(action['id'])},\n"
        "  auth: pieceAuth,\n"
        f"  requireAuth: {'true' if require_auth else 'false'},\n"
        f"  displayName: {_literal(action['displayName'])},\n"
        f"  description: {_literal(action['description'])},\n"
        "  props: {\n"
        "    " + ",\n    ".join(props) + "\n"
        "  },\n"
        "  async run(context) {\n"
        f"    return execute(operation, {_literal(auth['kind'])}, context.propsValue, context.auth);\n"
        "  },\n"
        "});\n"
    )


def _index_source(model: dict[str, Any]) -> str:
    imports = "\n".join(f"import {{ action_{a['id']} }} from './lib/actions/{a['id']}';" for a in model["actions"])
    actions = ", ".join(f"action_{a['id']}" for a in model["actions"])
    title = model["title"]
    return (
        "import { createPiece } from '@activepieces/pieces-framework';\n"
        "import { pieceAuth } from './lib/auth';\n"
        f"{imports}\n"
        "\n"
        f"export const piece_{model['name'].replace('-', '_')} = createPiece({{\n"
        f"  displayName: {_literal(title)},\n"
        f"  description: {_literal(f'OpenAPI-generated connector for {title}')},\n"
        "  auth: pieceAuth,\n"
        "  logoUrl: '',\n"
        "  authors: [],\n"
        f"  actions: [{actions}],\n"
        "  triggers: [],\n"
        "});\n"
    )


_README_PLACEHOLDER_RE = re.compile(r"\{\{(name|title|framework|auth_kind|actions)\}\}")


def _readme_source(model: dict[str, Any]) -> str:
    actions = "\n".join(f"- `{a['id']}`: {a['method']} `{a['path'].replace('`', '')}`" for a in model["actions"])
    values = {
        "name": model["name"],
        "title": _literal(model["title"]),
        "framework": VERSIONS["framework"],
        "auth_kind": model["auth"]["kind"],
        "actions": actions,
    }
    # One pass: substituted (user-controlled) text is never rescanned for placeholders.
    return _README_PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], _template("README.md.in"))


def render_piece(
    spec: dict[str, Any],
    *,
    name: Any,
    include_tags: list[str] | tuple[str, ...] | None = None,
    include_ops: list[str] | tuple[str, ...] | None = None,
    max_actions: int | None = None,
) -> RenderedPiece:
    """Render every generated file in memory; nothing is written."""
    model = build_model(spec, name=name, include_tags=include_tags, include_ops=include_ops, max_actions=max_actions)
    title = model["title"]
    files: dict[str, str] = {}
    files["package.json"] = _json(
        {
            "name": f"@openapi2piece/piece-{model['name']}",
            "version": "0.1.0",
            "private": True,
            "description": f"Generated Activepieces connector for {title}",
            "type": "commonjs",
            "main": "dist/index.js",
            "license": "MIT",
            "engines": {"node": ">=20"},
            "scripts": {"build": "tsc", "typecheck": "tsc --noEmit"},
            "dependencies": {
                "@activepieces/pieces-framework": VERSIONS["framework"],
                "@activepieces/pieces-common": VERSIONS["common"],
                "axios": VERSIONS["axios"],
            },
            "devDependencies": {"typescript": VERSIONS["typescript"], "@types/node": VERSIONS["nodeTypes"]},
            "overrides": dict(DEPENDENCY_OVERRIDES),
        }
    )
    files["tsconfig.json"] = _json(
        {
            "compilerOptions": {
                "target": "ES2022",
                "module": "CommonJS",
                "moduleResolution": "Node",
                "strict": True,
                "esModuleInterop": True,
                "skipLibCheck": True,
                "declaration": True,
                "rootDir": "src",
                "outDir": "dist",
                "types": ["node"],
            },
            "include": ["src/**/*.ts"],
        }
    )
    files["src/lib/auth.ts"] = _auth_source(model["auth"])
    files["src/lib/runtime.ts"] = _template("runtime.ts")
    for action in model["actions"]:
        files[f"src/lib/actions/{action['id']}.ts"] = _action_source(action, model["auth"])
    files["src/index.ts"] = _index_source(model)
    files["report.json"] = _json(model["report"])
    files["README.md"] = _readme_source(model)
    files["LICENSE"] = _template("LICENSE")
    files["THIRD_PARTY.md"] = _template("THIRD_PARTY.md")
    return RenderedPiece(files=files, model=model)


def _lstat(path: str) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _resolve(path: str | os.PathLike[str]) -> str:
    """``path.resolve``: absolute and lexically normalized, without following symlinks."""
    target = os.path.abspath(os.fspath(path))
    if os.sep == "/" and target.startswith("//"):
        target = "/" + target.lstrip("/")
    return target


def _validate_destination(path: str | os.PathLike[str]) -> str:
    """Refuse preexisting output and symlink ancestors, even during dry-run. No user-derived filenames."""
    target = _resolve(path)
    if _lstat(target) is not None:
        fail("Output already exists; choose a new output directory")
    parent = os.path.dirname(target)
    while True:
        info = _lstat(parent)
        if info is not None and (_stat.S_ISLNK(info.st_mode) or not _stat.S_ISDIR(info.st_mode)):
            fail("Output ancestors must be real directories, not symlinks/files")
        if parent == os.path.dirname(parent):
            break
        parent = os.path.dirname(parent)
    return target


def write_piece(files: Mapping[str, str], out: str | os.PathLike[str], *, dry_run: bool = False) -> None:
    """Validate the destination and write all files atomically into a new directory."""
    target = _validate_destination(out)
    for path in files:
        joined = _resolve(os.path.join(target, path))
        if path.startswith("/") or ".." in re.split(r"[\\/]", path) or joined[: len(target) + 1] != target + os.sep:
            fail("Unsafe generated file path")
    parent = os.path.dirname(target)
    # Require an existing parent to avoid leaving partial ancestor directories on failure.
    info = _lstat(parent)
    if info is None or not _stat.S_ISDIR(info.st_mode):
        fail("Output parent must already exist (create it first)")
    if dry_run:
        return
    stage = tempfile.mkdtemp(prefix=".openapi2piece-", dir=parent)
    try:
        for path, contents in files.items():
            full = os.path.join(stage, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            descriptor = os.open(full, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(to_utf8(contents))
        # Claim the destination before rename: never replace a concurrently created directory.
        os.mkdir(target)
        try:
            os.rename(stage, target)
        except BaseException:
            try:
                os.rmdir(target)
            except OSError:
                pass
            raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
