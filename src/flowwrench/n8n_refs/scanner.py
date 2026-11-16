"""A bounded lexical scanner, never an expression evaluator.

Tokens retain literal spans so renames can change only node-name literals.
JavaScript templates recurse into ${...}; comments, strings and regexes stay inert.
"""
from dataclasses import dataclass
import json
import re


class ScanError(ValueError):
    pass


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    start: int
    end: int


@dataclass(frozen=True)
class Reference:
    name: str
    start: int
    end: int
    paired: bool
    syntax: str
    dot_start: int | None = None
    optional_dot: bool = False


IDENT = re.compile(r"[\w$]", re.UNICODE)
JS_STATIC_PROPERTY = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*", re.ASCII)
REGEX_PREFIX = {"(", "[", "{", "=", ":", ",", ";", "!", "?", "&", "|", "+", "-", "*", "return", "throw", "case", "=>", "else", "do", "yield", "await", "typeof", "void", "delete", "in", "instanceof"}


def decode_literal(raw):
    """Decode common JS/Python escapes without eval; reject ambiguous escapes."""
    body = raw[1:-1]
    out = []
    i = 0
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0", "\\": "\\", "'": "'", '"': '"', "`": "`", "$": "$", "/": "/"}
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i == len(body):
            raise ScanError("incomplete string escape")
        ch = body[i]
        if ch in "xu":
            width = 2 if ch == "x" else 4
            digits = body[i + 1:i + 1 + width]
            if len(digits) != width or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                raise ScanError("unsupported Unicode/string escape in node name")
            out.append(chr(int(digits, 16)))
            i += width + 1
        elif ch in escapes:
            if ch == "0" and i + 1 < len(body) and body[i + 1].isdigit():
                raise ScanError("octal escapes in node names are unsupported")
            out.append(escapes[ch])
            i += 1
        elif ch in "\r\n":
            if ch == "\r" and i + 1 < len(body) and body[i + 1] == "\n":
                i += 1
            i += 1
        else:
            raise ScanError("unsupported string escape in node name")
    value = "".join(out)
    try:
        return value.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError as exc:
        raise ScanError("unpaired Unicode surrogate in node name") from exc


def lex(source, language="js", start=0, terminator=None, depth=0):
    """Return (tokens, end offset), stopping outside strings at a terminator."""
    if depth > 40:
        raise ScanError("template nesting exceeds 40")
    tokens = []
    i = start
    braces = 0
    while i < len(source):
        if len(tokens) > 100000:
            raise ScanError("source exceeds 100000 lexical tokens")
        if terminator and braces == 0 and source.startswith(terminator, i):
            return tokens, i + len(terminator)
        ch = source[i]
        if ch.isspace():
            i += 1
            continue
        if (language == "python" and ch == "#") or (language == "js" and source.startswith("//", i)):
            line_endings = "\r\n" if language == "python" else "\r\n\u2028\u2029"
            while i < len(source) and source[i] not in line_endings:
                i += 1
            continue
        if language == "js" and source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end == -1:
                raise ScanError("unterminated block comment")
            i = end + 2
            continue
        if ch in "\"'" or (ch == "`" and language == "js"):
            begin = i
            triple = language == "python" and source.startswith(ch * 3, i)
            delimiter = ch * (3 if triple else 1)
            i += len(delimiter)
            template_tokens = []
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                elif ch == "`" and source.startswith("${", i):
                    inner, i = lex(source, language, i + 2, "}", depth + 1)
                    template_tokens.extend([Token("boundary", "", i, i), *inner, Token("boundary", "", i, i)])
                elif source.startswith(delimiter, i):
                    i += len(delimiter)
                    break
                elif not triple and ch != "`" and source[i] in "\n\r":
                    raise ScanError("newline in quoted string")
                else:
                    i += 1
            else:
                raise ScanError("unterminated string or template")
            if template_tokens:
                tokens.append(Token("dynamic", "template", begin, i))
                tokens.extend(template_tokens)
            else:
                kind = "opaque" if triple else "string"
                tokens.append(Token(kind, source[begin:i], begin, i))
            continue
        # Regex detection is lexical, not a JS parser: statement-position '/' is
        # rejected rather than risk silently treating division as a regex.
        if language == "js" and ch == "/" and tokens and tokens[-1].value in {")", "}"}:
            raise ScanError("ambiguous slash after ')' or '}'; use an intermediate variable")
        if language == "js" and ch == "/" and (not tokens or tokens[-1].value in REGEX_PREFIX):
            begin = i
            i += 1
            in_class = False
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == "[":
                    in_class = True
                elif source[i] == "]":
                    in_class = False
                elif source[i] == "/" and not in_class:
                    i += 1
                    while i < len(source) and source[i].isalpha():
                        i += 1
                    break
                elif source[i] in "\n\r":
                    raise ScanError("unsupported or unterminated regex literal")
                i += 1
            else:
                raise ScanError("unterminated regex literal")
            tokens.append(Token("opaque", "regex", begin, i))
            continue
        if IDENT.fullmatch(ch):
            begin = i
            while i < len(source) and IDENT.fullmatch(source[i]):
                i += 1
            word = source[begin:i]
            if language == "python" and i < len(source) and source[i] in "\"'" and word.lower() in {"f", "fr", "rf"}:
                raise ScanError("Python f-strings are unsupported; use plain code expressions")
            tokens.append(Token("identifier", word, begin, i))
            continue
        if language == "js" and source.startswith("=>", i):
            tokens.append(Token("punct", "=>", i, i + 2))
            i += 2
            continue
        if ch == "{":
            braces += 1
        elif ch == "}":
            if braces == 0:
                raise ScanError("unmatched closing brace")
            braces -= 1
        tokens.append(Token("punct", ch, i, i + 1))
        i += 1
    if len(tokens) > 100000:
        raise ScanError("source exceeds 100000 lexical tokens")
    if terminator:
        raise ScanError("unterminated expression/template interpolation")
    if braces:
        raise ScanError("unclosed brace")
    return tokens, i


def scan(source, language="js", expression=False):
    if len(source) > 1024 * 1024:
        raise ScanError("source field exceeds 1 MiB character limit")
    groups = []
    if expression:
        if not source.startswith("="):
            return []
        if "{{" in source:
            i = 1
            while True:
                begin = source.find("{{", i)
                if begin == -1:
                    break
                tokens, i = lex(source, "js", begin + 2, "}}")
                groups.append(tokens)
    else:
        groups.append(lex(source, language)[0])
    refs = []
    selectors = {"$", "$node", "$items"}
    if language == "python" and not expression:
        selectors |= {"_", "_node", "_items"}
    for tokens in groups:
        parentheses, stack = {}, []
        for index, tok in enumerate(tokens):
            if tok.value == "(":
                stack.append(index)
            elif tok.value == ")" and stack:
                parentheses[stack.pop()] = index
        for i, token in enumerate(tokens):
            if token.kind != "identifier" or token.value not in selectors:
                continue
            if i and tokens[i - 1].value == ".":
                continue  # object.$(...) is not the n8n global
            bracket = token.value in {"$node", "_node"}
            opener = "[" if bracket else "("
            closer = "]" if bracket else ")"
            dot = None
            js_selector = language == "js" or expression
            js_node = js_selector and token.value == "$node"
            lookup = i + 1
            optional = (js_selector and [t.value for t in tokens[lookup:lookup + 2]] == ["?", "."]
                        and tokens[lookup].end == tokens[lookup + 1].start)
            dot_index = lookup + 1 if optional else lookup
            if (js_node and dot_index < len(tokens) and tokens[dot_index].value == "."
                    and (not optional or dot_index + 1 >= len(tokens) or tokens[dot_index + 1].value != "[")):
                dot = tokens[dot_index]
                if dot_index + 1 >= len(tokens) or tokens[dot_index + 1].kind != "identifier":
                    raise ScanError("dot node access requires a plain ASCII identifier")
                literal = tokens[dot_index + 1]
                # Do not accept a prefix of an escaped/Unicode identifier. The
                # broader lexer is not a complete ECMAScript identifier parser.
                following = source[literal.end:literal.end + 1]
                if (not JS_STATIC_PROPERTY.fullmatch(literal.value) or following == "\\"
                        or following and ord(following) > 127 and not following.isspace()):
                    raise ScanError("dot node access requires a plain ASCII identifier; use a quoted bracket literal")
                name = literal.value
                after = dot_index + 2
            else:
                if optional:
                    # Only the contiguous JS ?. operator is optional access/call.
                    # Keep the original literal span and the usual static-key
                    # checks; optional access does not remove pairing hazards.
                    lookup += 2
                if lookup >= len(tokens) or tokens[lookup].value != opener:
                    raise ScanError("aliased/indirect reference accessor is unsupported")
                if lookup + 2 >= len(tokens) or tokens[lookup + 1].kind != "string":
                    raise ScanError("dynamic node name is unsupported; use a quoted literal")
                literal = tokens[lookup + 1]
                name = decode_literal(literal.value)
                tail = tokens[lookup + 2].value
                if tail != closer and not (token.value in {"$items", "_items"} and tail == ","):
                    raise ScanError("computed node name or selector arguments are unsupported")
                after = lookup + 3
            is_items = token.value in {"$items", "_items"}
            paired = bracket
            if not is_items:
                before = i - 1
                # Follow only grouping parentheses, never a containing call.
                while before >= 0 and tokens[before].value == "(" and parentheses.get(before) == after:
                    if before and (tokens[before - 1].kind in {"identifier", "string", "opaque", "dynamic"}
                                   and tokens[before - 1].value not in {"return", "throw", "yield"}
                                   or tokens[before - 1].value in {")", "]"}):
                        break
                    after += 1
                    before -= 1
                rest = tokens[after:after + 5]
                values = [t.value for t in rest]
                # Optional chaining and literal ['item'] are also paired access.
                if js_selector and values[:2] == ["?", "."] and rest[0].end != rest[1].start:
                    raise ScanError("split optional selector property operator is unsupported")
                if values[:3] == ["?", ".", "["]:
                    rest = rest[2:]
                    values = values[2:]
                elif values[:1] == ["?"]:
                    rest = rest[1:]
                    values = values[1:]
                if len(values) >= 2 and values[0] == "." and values[1] in {"item", "itemMatching"}:
                    paired = True
                if values[:1] == ["["]:
                    if len(rest) < 3 or rest[1].kind != "string" or values[2] != "]":
                        raise ScanError("computed selector property is unsupported")
                    if decode_literal(values[1]) in {"item", "itemMatching"}:
                        paired = True
            refs.append(Reference(name, literal.start, literal.end, paired, token.value,
                                  dot.start if dot is not None else None, optional and dot is not None))
    return refs


def replace_references(source, references, old, new):
    for ref in sorted(references, key=lambda ref: ref.start, reverse=True):
        if ref.name == old:
            # JSON quoted strings are valid JS and plain Python strings.
            quoted = json.dumps(new, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
            if ref.optional_dot:
                # Keep ?. contiguous and all surrounding trivia untouched;
                # optional dot names become an optional quoted bracket key.
                source = source[:ref.start] + "[" + quoted + "]" + source[ref.end:]
            elif ref.dot_start is not None:
                # Two disjoint edits preserve comments/whitespace around the dot
                # and identifier, including when the new name needs brackets.
                source = source[:ref.start] + quoted + "]" + source[ref.end:]
                source = source[:ref.dot_start] + "[" + source[ref.dot_start + 1:]
            else:
                source = source[:ref.start] + quoted + source[ref.end:]
    return source
