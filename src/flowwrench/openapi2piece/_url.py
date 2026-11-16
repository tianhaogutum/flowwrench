"""A focused WHATWG URL parser subset replacing Node's ``new URL(input)``.

Only absolute URLs without a base are supported, which is all the generator
needs.  Special schemes with hosts (http, https, ws, wss, ftp) are parsed and
serialized like the WHATWG URL Standard: host lowercasing, IDNA, IPv4/IPv6
normalization, default-port removal, dot-segment removal and percent-encoding.
Other schemes are reported as successfully parsed with their scheme only; the
generator rejects them regardless.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from encodings import idna as _idna

__all__ = ["URLError", "ParsedURL", "parse_url"]


class URLError(ValueError):
    """``TypeError: Invalid URL``."""


@dataclass(frozen=True)
class ParsedURL:
    protocol: str  # Including the trailing ':'.
    username: str
    password: str
    search: str  # '' or '?…' (an empty query also gives '').
    hash: str  # '' or '#…'.
    href: str


_SPECIAL_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443, "ftp": 21}
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")
_FORBIDDEN_HOST = set("\x00\t\n\r #/:<>?@[\\]^|")
_FORBIDDEN_DOMAIN = _FORBIDDEN_HOST | {chr(c) for c in range(0x20)} | {"%", "\x7f"}

_C0_CONTROL = {chr(c) for c in range(0x20)}
_FRAGMENT_SET = _C0_CONTROL | set(' "<>`')
_QUERY_SET = _C0_CONTROL | set(' "#<>')
_SPECIAL_QUERY_SET = _QUERY_SET | {"'"}
# Node 20/22 (ada) predates the 2024 spec change that added '^' to the path set.
_PATH_SET = _QUERY_SET | set("?`{}")
_USERINFO_SET = _PATH_SET | set("/:;=@[\\]^|")


def _percent_encode(text: str, encode_set: set[str]) -> str:
    out = []
    for char in text:
        if char in encode_set or ord(char) > 0x7E:
            out.append("".join("%%%02X" % b for b in char.encode("utf-8", "surrogatepass")))
        else:
            out.append(char)
    return "".join(out)


def _percent_decode(text: str) -> bytes:
    data = text.encode("utf-8", "surrogatepass")
    out = bytearray()
    i = 0
    while i < len(data):
        byte = data[i]
        if byte == 0x25 and i + 2 < len(data) and re.fullmatch(rb"[0-9A-Fa-f]{2}", data[i + 1 : i + 3]):
            out.append(int(data[i + 1 : i + 3], 16))
            i += 3
        else:
            out.append(byte)
            i += 1
    return bytes(out)


def _parse_ipv4_number(part: str) -> int | None:
    if part == "":
        return None
    radix = 10
    if len(part) >= 2 and part[:2] in ("0x", "0X"):
        radix, part = 16, part[2:]
    elif len(part) >= 2 and part[0] == "0":
        radix, part = 8, part[1:]
    if part == "":
        return 0
    digits = {10: "0123456789", 8: "01234567", 16: "0123456789abcdefABCDEF"}[radix]
    if any(c not in digits for c in part):
        return None
    return int(part, radix)


def _ends_in_number(domain: str) -> bool:
    parts = domain.split(".")
    if parts[-1] == "":
        if len(parts) == 1:
            return False
        parts.pop()
    last = parts[-1]
    if last != "" and all(c in "0123456789" for c in last):
        return True
    return _parse_ipv4_number(last) is not None and last[:2] in ("0x", "0X")


def _parse_ipv4(domain: str) -> str:
    parts = domain.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts.pop()
    if len(parts) > 4:
        raise URLError("Invalid URL")
    numbers = []
    for part in parts:
        number = _parse_ipv4_number(part)
        if number is None:
            raise URLError("Invalid URL")
        numbers.append(number)
    if any(n > 255 for n in numbers[:-1]):
        raise URLError("Invalid URL")
    if numbers[-1] >= 256 ** (5 - len(numbers)):
        raise URLError("Invalid URL")
    address = numbers[-1]
    for index, number in enumerate(numbers[:-1]):
        address += number * 256 ** (3 - index)
    return ".".join(str((address >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def _parse_ipv6(text: str) -> list[int]:
    address = [0] * 8
    piece = 0
    compress: int | None = None
    i = 0
    n = len(text)
    hexdigits = "0123456789abcdefABCDEF"
    if i < n and text[i] == ":":
        if not text.startswith("::"):
            raise URLError("Invalid URL")
        i += 2
        piece += 1
        compress = piece
    while i < n:
        if piece == 8:
            raise URLError("Invalid URL")
        if text[i] == ":":
            if compress is not None:
                raise URLError("Invalid URL")
            i += 1
            piece += 1
            compress = piece
            continue
        value = 0
        length = 0
        while length < 4 and i < n and text[i] in hexdigits:
            value = value * 16 + int(text[i], 16)
            i += 1
            length += 1
        if i < n and text[i] == ".":
            if length == 0:
                raise URLError("Invalid URL")
            i -= length
            if piece > 6:
                raise URLError("Invalid URL")
            numbers_seen = 0
            while i < n:
                ipv4_piece: int | None = None
                if numbers_seen > 0:
                    if text[i] == "." and numbers_seen < 4:
                        i += 1
                    else:
                        raise URLError("Invalid URL")
                if i >= n or text[i] not in "0123456789":
                    raise URLError("Invalid URL")
                while i < n and text[i] in "0123456789":
                    number = int(text[i])
                    if ipv4_piece is None:
                        ipv4_piece = number
                    elif ipv4_piece == 0:
                        raise URLError("Invalid URL")
                    else:
                        ipv4_piece = ipv4_piece * 10 + number
                    if ipv4_piece > 255:
                        raise URLError("Invalid URL")
                    i += 1
                address[piece] = address[piece] * 0x100 + (ipv4_piece or 0)
                numbers_seen += 1
                if numbers_seen in (2, 4):
                    piece += 1
            if numbers_seen != 4:
                raise URLError("Invalid URL")
            break
        if i < n and text[i] == ":":
            i += 1
            if i >= n:
                raise URLError("Invalid URL")
        elif i < n:
            raise URLError("Invalid URL")
        address[piece] = value
        piece += 1
    if compress is not None:
        swaps = piece - compress
        piece = 7
        while piece != 0 and swaps > 0:
            address[piece], address[compress + swaps - 1] = address[compress + swaps - 1], address[piece]
            piece -= 1
            swaps -= 1
    elif piece != 8:
        raise URLError("Invalid URL")
    return address


def _serialize_ipv6(address: list[int]) -> str:
    best_start, best_len = -1, 1
    i = 0
    while i < 8:
        if address[i] == 0:
            j = i
            while j < 8 and address[j] == 0:
                j += 1
            if j - i > best_len:
                best_start, best_len = i, j - i
            i = j
        else:
            i += 1
    out = ""
    ignore0 = False
    for index, piece in enumerate(address):
        if ignore0 and piece == 0:
            continue
        ignore0 = False
        if index == best_start:
            out += "::" if index == 0 else ":"
            ignore0 = True
            continue
        out += format(piece, "x")
        if index != 7:
            out += ":"
    return out


def _domain_to_ascii(domain: str) -> str:
    domain = re.sub("[。．｡]", ".", domain)
    if domain.isascii():
        result = domain.lower()
    else:
        labels = []
        for label in domain.split("."):
            if label.isascii():
                labels.append(label.lower())
                continue
            try:
                labels.append(_idna.ToASCII(label).decode("ascii").lower())
            except UnicodeError:
                raise URLError("Invalid URL") from None
        result = ".".join(labels)
    if result == "":
        raise URLError("Invalid URL")
    return result


def _parse_host(text: str) -> str:
    if text.startswith("["):
        if not text.endswith("]"):
            raise URLError("Invalid URL")
        return "[" + _serialize_ipv6(_parse_ipv6(text[1:-1])) + "]"
    domain = _percent_decode(text).decode("utf-8", "replace")
    ascii_domain = _domain_to_ascii(domain)
    if any(char in _FORBIDDEN_DOMAIN for char in ascii_domain):
        raise URLError("Invalid URL")
    if _ends_in_number(ascii_domain):
        return _parse_ipv4(ascii_domain)
    return ascii_domain


_SINGLE_DOT = {".", "%2e"}
_DOUBLE_DOT = {"..", ".%2e", "%2e.", "%2e%2e"}


def _parse_path(text: str) -> list[str]:
    segments: list[str] = []
    if text == "":
        return [""]
    if text[0] in "/\\":
        text = text[1:]
    raw_segments = re.split(r"[/\\]", text)
    for index, raw in enumerate(raw_segments):
        last = index == len(raw_segments) - 1
        buffer = _percent_encode(raw, _PATH_SET)
        lowered = buffer.lower()
        if lowered in _DOUBLE_DOT:
            if segments:
                segments.pop()
            if last:
                segments.append("")
        elif lowered in _SINGLE_DOT:
            if last:
                segments.append("")
        else:
            segments.append(buffer)
    return segments


def parse_url(value: str) -> ParsedURL:
    """Parse an absolute URL like ``new URL(value)``; raise :class:`URLError` on failure."""
    text = value.strip("".join(chr(c) for c in range(0x21)))
    text = re.sub("[\t\n\r]", "", text)
    match = _SCHEME_RE.match(text)
    if not match:
        raise URLError("Invalid URL")
    scheme = match.group()[:-1].lower()
    rest = text[match.end() :]
    if scheme not in _SPECIAL_PORTS:
        return _non_special(scheme, rest)
    rest = rest.lstrip("/\\")
    authority_end = len(rest)
    for index, char in enumerate(rest):
        if char in "/\\?#":
            authority_end = index
            break
    authority, remainder = rest[:authority_end], rest[authority_end:]
    username = password = ""
    if "@" in authority:
        credentials, _, authority = authority.rpartition("@")
        user, _, secret = credentials.partition(":")
        username = _percent_encode(user, _USERINFO_SET)
        password = _percent_encode(secret, _USERINFO_SET)
    host_text, port_text = authority, ""
    inside_brackets = False
    for index, char in enumerate(authority):
        if char == "[":
            inside_brackets = True
        elif char == "]":
            inside_brackets = False
        elif char == ":" and not inside_brackets:
            host_text, port_text = authority[:index], authority[index + 1 :]
            break
    if host_text == "":
        raise URLError("Invalid URL")
    host = _parse_host(host_text)
    port: int | None = None
    if port_text != "":
        if not all(c in "0123456789" for c in port_text):
            raise URLError("Invalid URL")
        port = int(port_text)
        if port > 65535:
            raise URLError("Invalid URL")
        if port == _SPECIAL_PORTS[scheme]:
            port = None
    fragment: str | None = None
    if "#" in remainder:
        remainder, _, fragment_text = remainder.partition("#")
        fragment = _percent_encode(fragment_text, _FRAGMENT_SET)
    query: str | None = None
    if "?" in remainder:
        remainder, _, query_text = remainder.partition("?")
        query = _percent_encode(query_text, _SPECIAL_QUERY_SET)
    segments = _parse_path(remainder)
    href = scheme + "://"
    if username or password:
        href += username + (":" + password if password else "") + "@"
    href += host + (f":{port}" if port is not None else "")
    href += "".join("/" + segment for segment in segments)
    if query is not None:
        href += "?" + query
    if fragment is not None:
        href += "#" + fragment
    return ParsedURL(
        protocol=scheme + ":",
        username=username,
        password=password,
        search="?" + query if query else "",
        hash="#" + fragment if fragment else "",
        href=href,
    )


def _non_special(scheme: str, rest: str) -> ParsedURL:
    # Approximation: the generator rejects every non-HTTP(S) scheme, so only
    # the protocol (and the presence of a query/fragment) matters here.
    fragment = ""
    if "#" in rest:
        rest, _, fragment_text = rest.partition("#")
        fragment = "#" + fragment_text if fragment_text else ""
    search = ""
    if "?" in rest:
        rest, _, query_text = rest.partition("?")
        search = "?" + query_text if query_text else ""
    return ParsedURL(protocol=scheme + ":", username="", password="", search=search, hash=fragment, href=scheme + ":" + rest)
