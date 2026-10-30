"""Turn a ``curl`` command into the HTTP packet it is equivalent to.

Workers paste curl.  That is what they type at the shell, it is what every vuln write-up
on the internet shows, and asking them to hand-expand ``-H`` flags into a request block
is friction that produces mistakes.  But the packet is the only form we can replay,
render, or trust: a curl line hides which headers were actually sent whenever it uses
``-b``, ``-A``, ``--compressed``, or a config file.

So accept the curl, and translate it — visibly.  The translation is deterministic and
lossless for the flags below; anything unsupported raises with the flag named rather
than being dropped, because silently discarding ``--cert`` would make the packet look
like an unauthenticated request and change what the replay proves.

Scope of the translation:

* methods, ``-X``, ``--data*``, ``-F``, ``-H``, ``-b``/``--cookie``, ``-A``, ``-e``,
  ``-u``, ``-I``, ``--json``, ``-G``, and the URL
* quoted arguments in both ``'...'`` and ``"..."`` (with backslash escapes in the latter)
* line continuations (``\\`` at end of line) since multi-line curl is the common paste

Deliberately *not* supported, and refused by name: ``--cert``/``--key`` (client certs),
``--proxy`` (a replay through a proxy is a different network path than the one being
proven), ``--upload-file``, and anything else that changes what is on the wire.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass


class CurlParseError(ValueError):
    """The command cannot be translated faithfully. The message names the reason."""


# Request body flags, grouped by what they imply about Content-Type.
_DATA_FLAGS = {"-d", "--data", "--data-raw", "--data-ascii"}
_DATA_BINARY_FLAGS = {"--data-binary"}
_DATA_URLENCODE_FLAGS = {"--data-urlencode"}
_FORM_FLAGS = {"-F", "--form"}
_JSON_FLAGS = {"--json"}

# Flags that take a value we must read, even though we may then reject them.
_VALUE_FLAGS = {
    "-X", "--request",
    "-H", "--header",
    "-b", "--cookie",
    "-A", "--user-agent",
    "-e", "--referer",
    "-u", "--user",
    "-o", "--output",
    "-m", "--max-time",
    "-w", "--write-out",
    "--url",
    "--connect-timeout",
    "--retry",
    "-T", "--upload-file",
    "--cert", "--key",
    "-x", "--proxy",
    "-c", "--cookie-jar",
    "--resolve",
    "--interface",
    "--cacert",
    "--capath",
    "--limit-rate",
    "--max-redirs",
}

# Flags with no value that only shape client-side behaviour, so the packet is unchanged.
_IGNORED_FLAGS = {
    "-s", "--silent", "-sS",
    "-k", "--insecure",
    "-L", "--location",
    "-i", "--include",
    "-v", "--verbose",
    "-f", "--fail",
    "-#", "--progress-bar",
    "-N", "--no-buffer",
    "--compressed",
    "--no-keepalive",
    "--http1.1", "--http2",
    "--tlsv1.2", "--tlsv1.3",
    "-g", "--globoff",
}

# Flags that would change what actually goes on the wire, so they cannot be folded into
# a packet without lying about it.
_REFUSED_FLAGS = {
    "--cert": "客户端证书（--cert）不在报文里，转成报文会看起来像匿名请求",
    "--key": "客户端私钥（--key）不在报文里",
    "-T": "-T/--upload-file 是流式上传，无法还原成一次请求体",
    "--upload-file": "--upload-file 是流式上传，无法还原成一次请求体",
    "-x": "--proxy 会改变实际网络路径，重放时无法复现",
    "--proxy": "--proxy 会改变实际网络路径，重放时无法复现",
    "--resolve": "--resolve 强制解析结果，报文里没有这个信息",
    "--interface": "--interface 绑定出口网卡，报文里没有这个信息",
}


@dataclass
class CurlRequest:
    method: str
    url: str
    headers: list[tuple[str, str]]
    body: str
    # Where the URL came from, so the caller can warn if it disagrees with later flags.
    form_encoded: bool = False

    def to_packet(self, *, http_version: str = "1.1") -> str:
        """Render as a request packet. ``Host`` always comes from the URL."""
        from urllib.parse import urlsplit

        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"}:
            raise CurlParseError(f"只支持 http/https，当前是 `{parsed.scheme or '无协议'}`")
        if not parsed.hostname:
            raise CurlParseError(f"URL 里没有主机名：`{self.url}`")

        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"

        lines = [f"{self.method} {target} HTTP/{http_version}"]
        # Host first: it is what a reader looks at, and the replay path derives the
        # target from it.  ``curl -H 'Host: ...'`` overrides the URL's authority.
        authority = _header_value(self.headers, "host")
        if not authority:
            authority = parsed.netloc
            lines.append(f"Host: {authority}")
        else:
            lines.append(f"Host: {authority}")

        for name, value in self.headers:
            if name.lower() == "host":
                continue
            lines.append(f"{name}: {value}")

        head = "\n".join(lines)
        return f"{head}\n\n{self.body}" if self.body else f"{head}\n\n"


def _header_value(headers: list[tuple[str, str]], wanted: str) -> str:
    for name, value in headers:
        if name.lower() == wanted:
            return value
    return ""


def is_curl_command(text: str) -> bool:
    """True when the first token is ``curl`` (after prompts and ``sudo``)."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    # A pasted transcript often keeps the shell prompt; it is not part of the command.
    while stripped.startswith("$"):
        stripped = stripped[1:].lstrip()
    if not stripped:
        return False
    first = stripped.split(None, 1)[0]
    if first == "curl":
        return True
    if first in {"sudo", "env"}:
        rest = stripped.split(None, 1)[1] if len(stripped.split(None, 1)) > 1 else ""
        return is_curl_command(rest)
    return False


def _tokenize(command: str) -> list[str]:
    """Split a shell command, tolerating the multi-line pastes people actually make."""
    # Join continuations first: shlex would otherwise see a lone backslash.
    flattened = command.replace("\\\r\n", " ").replace("\\\n", " ")
    try:
        return shlex.split(flattened, posix=True)
    except ValueError as exc:
        raise CurlParseError(
            f"命令无法解析（引号可能没闭合）：{exc}。"
            "把命令放在单引号 heredoc 里，或检查是否有落单的引号"
        ) from exc


def parse_curl(command: str) -> CurlRequest:
    """Translate ``curl ...`` into a request. Raises :class:`CurlParseError`."""
    # Mirror ``is_curl_command``: a pasted transcript keeps the prompt, and the two
    # entry points must agree on what counts as a command or one accepts what the
    # other refuses.
    stripped = (command or "").strip()
    while stripped.startswith("$"):
        stripped = stripped[1:].lstrip()
    tokens = _tokenize(stripped)
    if not tokens:
        raise CurlParseError("命令为空")
    if tokens[0] in {"sudo", "env"}:
        tokens = tokens[1:]
    if not tokens or tokens[0] != "curl":
        raise CurlParseError("不是 curl 命令")

    method = ""
    headers: list[tuple[str, str]] = []
    body_parts: list[str] = []
    form_parts: list[str] = []
    url = ""
    head_only = False
    form_encoded = False
    saw_json = False
    data_started = False

    index = 1
    while index < len(tokens):
        token = tokens[index]

        if token in _REFUSED_FLAGS:
            raise CurlParseError(_REFUSED_FLAGS[token])

        if token in _IGNORED_FLAGS:
            index += 1
            continue

        # --flag=value form, e.g. --data=page=1
        inline_value: str | None = None
        if token.startswith("--") and "=" in token:
            name, _, inline_value = token.partition("=")
            if name in _DATA_FLAGS | _DATA_BINARY_FLAGS | _DATA_URLENCODE_FLAGS | _JSON_FLAGS | _FORM_FLAGS | _VALUE_FLAGS:
                token = name

        def take_value() -> str:
            nonlocal index
            if inline_value is not None:
                return inline_value
            index += 1
            if index >= len(tokens):
                raise CurlParseError(f"`{token}` 后面缺少参数")
            return tokens[index]

        if token in {"-X", "--request"}:
            method = take_value().upper()
        elif token in {"-H", "--header"}:
            raw = take_value()
            name, separator, value = raw.partition(":")
            if not separator:
                raise CurlParseError(f"请求头缺少冒号：`{raw}`")
            headers.append((name.strip(), value.strip()))
        elif token in {"-b", "--cookie"}:
            headers.append(("Cookie", take_value()))
        elif token in {"-A", "--user-agent"}:
            headers.append(("User-Agent", take_value()))
        elif token in {"-e", "--referer"}:
            headers.append(("Referer", take_value()))
        elif token in {"-u", "--user"}:
            credentials = take_value()
            import base64

            encoded = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
            headers.append(("Authorization", f"Basic {encoded}"))
        elif token in _DATA_FLAGS:
            data_started = True
            body_parts.append(take_value())
        elif token in _DATA_BINARY_FLAGS:
            data_started = True
            body_parts.append(take_value())
        elif token in _DATA_URLENCODE_FLAGS:
            data_started = True
            raw = take_value()
            # ``--data-urlencode name=value`` means the name is sent as-is and the value
            # is percent-encoded.  ``name=`` alone encodes the value only.
            from urllib.parse import quote

            name, separator, value = raw.partition("=")
            if separator:
                body_parts.append(f"{name}={quote(value, safe='')}")
            else:
                body_parts.append(quote(raw, safe="="))
            form_encoded = True
        elif token in _FORM_FLAGS:
            data_started = True
            form_parts.append(take_value())
        elif token in _JSON_FLAGS:
            data_started = True
            saw_json = True
            body_parts.append(take_value())
        elif token in {"-I", "--head"}:
            head_only = True
        elif token in {"-G", "--get"}:
            form_encoded = True
        elif token in _VALUE_FLAGS:
            take_value()
        elif token.startswith("-") and token != "-":
            raise CurlParseError(
                f"不支持的 curl 参数 `{token}`。"
                "为了不复述错的报文，未知参数一律拒绝；请手动写成 HTTP 报文"
            )
        else:
            if url:
                raise CurlParseError(
                    f"命令里有多个 URL（`{url}` 与 `{token}`）；一次报文只能有一个目标"
                )
            url = token
        index += 1

    if not url:
        raise CurlParseError("命令里没有 URL")

    if form_parts:
        if body_parts:
            raise CurlParseError("-F 与 -d/--json 不能混用")
        body, content_type = _multipart_body(form_parts)
        headers.append(("Content-Type", content_type))
        body_text = body
    else:
        body_text = "&".join(body_parts) if form_encoded else "".join(body_parts)
        if saw_json and not _header_value(headers, "content-type"):
            headers.append(("Content-Type", "application/json"))
        elif body_text and not _header_value(headers, "content-type"):
            headers.append(("Content-Type", "application/x-www-form-urlencoded"))

    if not method:
        if head_only:
            method = "HEAD"
        elif data_started:
            method = "POST"
        else:
            method = "GET"

    if head_only and method == "GET":
        method = "HEAD"

    return CurlRequest(
        method=method,
        url=url,
        headers=headers,
        body=body_text,
        form_encoded=form_encoded,
    )


def _multipart_body(form_parts: list[str]) -> tuple[str, str]:
    """Render ``-F`` fields as one multipart body with a stable boundary.

    ``-F 'file=@/tmp/x.png'`` cannot be expanded here (the file is on the worker's disk,
    not the server's), so the body carries a placeholder and the boundary is fixed.  A
    reviewer replaying this gets the same shape; the actual bytes have to be supplied.
    """
    boundary = "----axiom-curl-boundary"
    chunks: list[str] = []
    for part in form_parts:
        name, separator, value = part.partition("=")
        if not separator:
            raise CurlParseError(f"-F 参数缺少等号：`{part}`")
        if value.startswith("@"):
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{value[1:].rsplit("/", 1)[-1]}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
                f"[文件内容：{value[1:]}]"
            )
        else:
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}'
            )
    body = "\r\n".join([*chunks, f"--{boundary}--", ""])
    return body, f"multipart/form-data; boundary={boundary}"


def curl_to_packet(command: str) -> str:
    """One-shot translation used by the schema validator."""
    return parse_curl(command).to_packet()


__all__ = [
    "CurlParseError",
    "CurlRequest",
    "curl_to_packet",
    "is_curl_command",
    "parse_curl",
]
