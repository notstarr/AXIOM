from __future__ import annotations

"""Redact a tool call before it leaves our host for the judge model.

The guard sends the tool call to a third-party completion API (SiliconFlow /
Qwen).  Explore routinely writes real credentials into its commands - a login
probe is ``curl -u admin:P@ssw0rd`` - and real personal data - a code-login
probe is ``-d "mobile=13800138000"``.  None of that is needed to answer the one
question the judge is asked ("will this call damage the target?"), and all of it
would otherwise sit in someone else's request log.

So the judged text is masked the way application logs are masked: *the shape
stays, the value goes*.  ``curl -u admin:P@ssw0rd https://t/login`` becomes
``curl -u admin:[密码] https://t/login``.  The verb, the flag, the key name, the
host, the path and the payload structure are untouched, so the verdict is formed
from the same information it was formed from before.

The placeholder carries a type (``[密码]`` vs ``[手机号]``) instead of a generic
``[已脱敏]``, because "this call moves a phone number" is itself a judging
signal and one blank marker would erase it.

Two rules for anything added here:

* Never mask something that is not a secret.  IPs, domains, ports, MAC
  addresses, paths, snowflake ids and route words like 管理/详情 are the evidence
  the judge reasons over; masking them degrades real decisions.  A false
  positive is worse than a false negative, which is why the id-card and
  bank-card rules are checksum-gated rather than shape-gated.
* Never fall back to the raw text on error.  The caller treats an empty result
  as "do not call the judge at all".

Deliberately not handled, because a regex cannot separate them from a payload:
a bare value with no key (``-d '谭子清'``), a nameless base64 blob, and a name
that only appears in free prose.  Those stay as they are - guessing would mean
masking the SQLi and deserialisation payloads the judge has to read.

The reverse case is accepted: a keyed value inside a SQL string literal, as in
``WHERE password='x'``, is masked too.  A regex cannot tell that literal from a
shell argument, and when the literal is a real credential masking it is exactly
what is wanted.  The keywords, the quoting and the payload shape all survive, so
only the compared value is lost.
"""

import re


PLACEHOLDER_PASSWORD = "[密码]"
PLACEHOLDER_TOKEN = "[令牌]"
PLACEHOLDER_KEY = "[密钥]"
PLACEHOLDER_COOKIE = "[Cookie]"
PLACEHOLDER_PRIVATE_KEY = "[私钥]"
PLACEHOLDER_ID_CARD = "[身份证]"
PLACEHOLDER_PHONE = "[手机号]"
PLACEHOLDER_LANDLINE = "[电话]"
PLACEHOLDER_EMAIL = "[邮箱]"
PLACEHOLDER_BANK_CARD = "[银行卡]"
PLACEHOLDER_NAME = "[姓名]"
PLACEHOLDER_ADDRESS = "[地址]"


# --------------------------------------------------------------------------
# value classifiers
# --------------------------------------------------------------------------

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CODES = "10X98765432"


def _is_id_card(value: str) -> bool:
    """18-digit mainland id card: mod-11-2 checksum over a real birth date.

    The checksum is what makes this safe to apply everywhere.  AXIOM stores
    snowflake ids like ``id=971493486794096642`` in almost every payload; those
    are 18 digits too, and masking them would hide which object a command acts
    on - the single most useful piece of context the judge gets.
    """

    if not re.fullmatch(r"\d{17}[\dXx]", value):
        return False
    year, month, day = int(value[6:10]), int(value[10:12]), int(value[12:14])
    if not (1900 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31):
        return False
    total = sum(int(char) * weight for char, weight in zip(value[:17], _ID_WEIGHTS))
    return _ID_CODES[total % 11] == value[17].upper()


def _is_luhn(value: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(value)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


# Key names that hold a secret in a ``key=value`` or ``"key": "value"`` pair.
#
# The secret word is matched anywhere inside the key rather than as the whole
# key, because real payloads spell it ``loginPwd``, ``user_password`` and
# ``passWord`` as often as ``password``.  ``pass`` on its own is still excluded:
# ``bypass=1`` is not a password, and ``--pass`` is covered by the flag rule.
_SECRET_WORD = (
    r"password|passwd|passphrase|pwd|secret|token|bearer|credential|credentials|"
    r"api[-_]?key|apikey|access[-_]?key|secret[-_]?key|private[-_]?key|"
    r"access[-_]?token|auth[-_]?token|refresh[-_]?token|"
    r"session[-_]?id|sessionid|jsessionid|phpsessid|cookie|"
    r"client[-_]?secret|csrf[-_]?token|xsrf[-_]?token|"
    r"密码|口令|密钥|令牌|凭证|凭证号"
)
_SECRET_KEY = r"(?:[a-z0-9_]*?(?:%s)[a-z0-9_]*)" % _SECRET_WORD

# Flag names, where the leading ``--`` already proves it is a switch.  ``pass``
# is allowed here for the same reason it is not allowed above.
_FLAG_SECRET = (
    r"password|passwd|passphrase|pass|pwd|secret|token|bearer|cookie|auth|"
    r"api[-_]?key|apikey|access[-_]?key|secret[-_]?key|private[-_]?key|"
    r"access[-_]?token|auth[-_]?token|refresh[-_]?token|"
    r"credential|credentials|session[-_]?id|sessionid|jsessionid|"
    r"csrf[-_]?token|xsrf[-_]?token"
)


def _secret_placeholder(key: str) -> str:
    lowered = key.casefold()
    if "pass" in lowered or lowered.endswith("pwd") or "密码" in lowered or "口令" in lowered:
        return PLACEHOLDER_PASSWORD
    if "cookie" in lowered or "session" in lowered:
        return PLACEHOLDER_COOKIE
    if "token" in lowered or "bearer" in lowered or "令牌" in lowered:
        return PLACEHOLDER_TOKEN
    return PLACEHOLDER_KEY


# A value this module already replaced is never matched again: every value
# class below rejects ``[`` so ``password=[密码]`` cannot be re-typed by a later
# rule.  That is also what makes ``redact`` idempotent.
_VALUE = r'''(?:"([^"\n]*)"|'([^'\n]*)'|([^\s,;&|)}\)\]\["']+))'''


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------
#
# Order is the priority: each rule runs once over the text the previous ones
# produced.  Structural rules (private key, auth header, JWT, url userinfo) run
# before the generic key/value rule so the specific mask wins, and the PII rules
# run last so they see credentials already replaced.

_PRIVATE_KEY = re.compile(
    r"-----BEGIN[ A-Z]*PRIVATE KEY-----[\s\S]*?-----END[ A-Z]*PRIVATE KEY-----"
)

# The bare (unquoted) branch must stop at whitespace.  An earlier version used
# ``[^\n]*?`` with a lookahead, which swallowed the rest of the line: a
# ``-H 'Authorization: Bearer <jwt>' https://t/api/me`` lost the URL, and the
# judge then saw a command that did not resemble the one that ran.
_AUTH_HEADER = re.compile(
    r"""(?ix)
    (?<![\w-])
    (
        (?:(?:proxy-)?authorization|x-api-key|api-key|private-token|
           x-auth-token|x-csrf-token|x-xsrf-token|x-amz-security-token)
        # The closing quote of a JSON key sits between the name and the colon:
        # ``{"Authorization": "Bearer ..."}``.  Without it the header is missed
        # in every JSON body, which is most of what Explore sends.
        ["']?\s*[:=]\s*
    )
    (?:"([^"\n]*)"|'([^'\n]*)'|((?:(?i:bearer|basic|token|digest|bot|negotiate)\s+)?[^\s"';&|]+))
    """
)

# A cookie header carries a ``; ``-separated list, so its bare branch follows
# the list.  Everything else must stay a single token - ``Authorization: x; rm
# -rf /`` is a command injection, and masking it would hide the attack.
_COOKIE_HEADER = re.compile(
    r"""(?ix)
    (?<![\w-])
    ((?:set-)?cookie ["']?\s*[:=]\s*)
    (?:"([^"\n]*)"|'([^'\n]*)'|([^\s"';]+(?:\s*;\s*[^\s"';]+)*))
    """
)


def _is_reference(value: str) -> bool:
    """True when the value points at a secret rather than being one.

    ``Authorization: Bearer $AXIOM_SERVICE_TOKEN`` carries no secret - the
    variable's name is the whole content.  Masking it would hide which
    variable was used, which is the one fact connecting the header to the
    assignment the worker made earlier.
    """

    stripped = value.strip().strip("\"'")
    return bool(stripped) and stripped[0] in "${("


def _looks_like_filename(value: str) -> bool:
    """``wj.txt``, ``cookies.json`` - a path to a file, not a value in one."""

    stripped = value.strip().strip("\"'")
    if "/" in stripped:
        return True
    head, _, ext = stripped.rpartition(".")
    return bool(head) and ext.isalnum() and len(ext) <= 5


def _mask_header_value(value: str, placeholder: str) -> str:
    """Keep an auth scheme word, drop the credential behind it."""

    scheme = re.match(r"(?i)^(bearer|basic|token|digest|bot|negotiate)\s+", value)
    if scheme:
        rest = value[scheme.end():]
        if _is_reference(rest) or len(rest.strip()) < 6:
            # ``Bearer x`` is a placeholder the worker wrote while probing, not a
            # credential.  Masking it changes nothing a reader needs, and doing
            # it made the audit look like it had found a secret.
            return value
        return value[: scheme.end()] + placeholder
    if _is_reference(value) or len(value.strip()) < 6:
        return value
    return placeholder


def _header_replacement(match: re.Match[str], placeholder: str) -> str:
    prefix = match.group(1)
    for index, quote in ((2, '"'), (3, "'")):
        if match.group(index) is not None:
            inner = _mask_header_value(match.group(index), placeholder)
            return f"{prefix}{quote}{inner}{quote}"
    return prefix + _mask_header_value(match.group(4), placeholder)


_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")

# The password half of ``scheme://user:pass@host``.  The value class stops only
# at whitespace or ``/``, so an unencoded ``@`` inside the password does not cut
# the match short - the last ``@`` wins and the host survives.
_URL_USERINFO = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@]+:)([^\s/]+)(@)")


def _cookie_flag_replacement(match: re.Match[str]) -> str:
    for index, quote in ((2, '"'), (3, "'")):
        value = match.group(index)
        if value is None:
            continue
        if _is_reference(value) or _looks_like_filename(value):
            return match.group(0)
        return f"{match.group(1)}{quote}{PLACEHOLDER_COOKIE}{quote}"
    value = match.group(4)
    if _is_reference(value) or _looks_like_filename(value):
        return match.group(0)
    return f"{match.group(1)}{PLACEHOLDER_COOKIE}"


def _value_flag_replacement(placeholder: str, split_percent: bool = False):
    """Build a replacement that keeps the flag or prefix and masks the value."""

    def replace(match: re.Match[str]) -> str:
        prefix = match.group(1)
        for index, quote in ((2, '"'), (3, "'")):
            value = match.group(index)
            if value is None:
                continue
            if split_percent:
                if value.endswith("%"):
                    # Already masked by an earlier pass.
                    return match.group(0)
                if "%" in value:
                    head = value.split("%", 1)[0]
                    return f"{prefix}{quote}{head}%{placeholder}{quote}"
            return f"{prefix}{quote}{placeholder}{quote}"
        value = match.group(4)
        if split_percent:
            if value.endswith("%"):
                return match.group(0)
            if "%" in value:
                head = value.split("%", 1)[0]
                return f"{prefix}{head}%{placeholder}"
        return f"{prefix}{placeholder}"

    return replace


# ``curl -u user:pass`` in all four spellings curl accepts: ``-u user:pass``,
# ``-uuser:pass``, ``--user user:pass``, ``--user=user:pass``.  A ``-u`` with no
# colon is curl's prompt mode, not a credential, so it is left alone.
_SHORT_SECRET_FLAG = re.compile(
    r"""(?ix)
    (curl\b[^|;&]*?\s(?:-u|--user)(?:=|\s+)?)
    (?:"([^":\s]*):([^"\n]*)"|'([^':\s]*):([^'\n]*)'|([^\s:"']+):([^\s"']+))
    """
)


def _short_flag_replacement(match: re.Match[str]) -> str:
    prefix = match.group(1)
    for user_index, secret_index, quote in ((2, 3, '"'), (4, 5, "'")):
        if match.group(user_index) is not None:
            return f"{prefix}{quote}{match.group(user_index)}:{PLACEHOLDER_PASSWORD}{quote}"
    return f"{prefix}{match.group(6)}:{PLACEHOLDER_PASSWORD}"


# ``mysql -pXy9`` is a password, ``mysql -P 3306`` is a port and ``nmap -p`` is a
# range, so the flag stays case-sensitive and the client name is scoped.  This
# is the rule most likely to misfire if it were ever broadened.
_DB_PASSWORD_FLAG = re.compile(
    r"""(?x)
    (\b(?i:mysql|mysqldump|mysqladmin|mariadb|mariadb-dump|mongosh|mongo)\b
       [^|;&]*?\s(?:-p|--password)(?:\s+|=)?)
    """ + _VALUE
)

_REDIS_PASSWORD_FLAG = re.compile(
    r"""(?x)
    (\bredis-cli\b[^|;&]*?\s(?:-a|--pass)(?:\s+|=)?)
    """ + _VALUE
)

_SSHPASS_FLAG = re.compile(
    r"""(?x)
    (\bsshpass\b[^|;&]*?\s-p\s*)
    """ + _VALUE
)

# ``smbclient -U domain/user%password`` keeps the account, drops the secret.
_SMBCLIENT_FLAG = re.compile(
    r"""(?x)
    (\bsmbclient\b[^|;&]*?\s(?:-U|--user)(?:\s+|=)?)
    """ + _VALUE
)

# The name part before the secret word may be empty, because the common flag
# *is* the secret word: ``--password x`` starts with it.  Requiring a character
# in front quietly skipped every such flag, and only ``--db-password`` worked.
_LONG_SECRET_FLAG = re.compile(
    r"""(?ix)
    (--?[a-z0-9-]*?(?:%s)(?:=|\s+))
    """ % _FLAG_SECRET + _VALUE
)

# ``curl -b`` / ``curl --cookie`` hand the cookie over as a value rather than as
# a header, so the header rule never sees it.  But ``-b wj.txt`` names a cookie
# *jar* to read: a file name is not a credential, and masking it hid which file
# the worker was loading.
_CURL_COOKIE_FLAG = re.compile(
    r"""(?ix)
    (\bcurl\b[^|;&]*?\s(?:-b|--cookie)(?:=|\s+)?)
    """ + _VALUE
)

# A shell assignment is the shape that actually leaked.  Explore writes
# ``SID=3c80dc73-...; curl -H "Cookie: sessionId=$SID"``, and while the header
# reference was masked, the assignment above it kept the real session id in the
# clear.  ``TOK=de3de127...`` was the same story with an account token.
#
# The name decides, not the shape, because ``SID=`` has to be masked while
# ``BASE=``, ``PID=``, ``OUT=`` and ``U=`` in the same script must not.  Matching
# is per underscore-separated segment, so ``API_KEY`` and ``MYSQL_PWD`` are
# caught while ``monkey`` and ``bypass`` are not.
_SHELL_ASSIGN = re.compile(
    r"""(?x)
    (?<![A-Za-z0-9_])
    ((?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*)
    (?:"([^"\n]*)"|'([^'\n]*)'|([^\s;&|]+))
    """
)

# ``${SID:-3c80dc73-...}`` is the same leak with the value in the default.
_SHELL_DEFAULT_ASSIGN = re.compile(
    r"""(?x)
    (\$\{([A-Za-z_][A-Za-z0-9_]*):-)
    ([^}\n]+)
    (\})
    """
)

_SECRET_SEGMENTS = frozenset(
    "password passwd passphrase pwd secret token credential credentials cookie "
    "session sess sid tok auth ak sk cred priv key jwt csrf xsrf ticket bearer".split()
)
# Long unambiguous words also match as a name suffix, so camelCase holders like
# ``chenToken`` are caught.  The short ones must not: ``monkey`` ends in "key".
_SECRET_SUFFIXES = (
    "password", "passwd", "passphrase", "secret", "token", "credential",
    "credentials", "cookie", "session", "apikey", "ticket", "jwt",
)
_SHORT_SEGMENTS = frozenset("ak sk key priv cred auth tok sid sess".split())


def _is_secret_variable(name: str) -> bool:
    lowered = name.casefold()
    if lowered == "pwd":
        # The shell's working directory, not a password.  ``MYSQL_PWD`` still
        # matches, because the whole name is not "pwd".
        return False
    if any(part in _SECRET_SEGMENTS for part in lowered.split("_") if part):
        return True
    return any(lowered.endswith(suffix) for suffix in _SECRET_SUFFIXES)


# The name cannot carry the whole rule.  The recorded run also holds
# ``S='3c80dc73-...'`` and ``T=25fd91c4...`` and ``for s in <uuid> <uuid>``, where
# the variable is a single letter and the value is a live session id or account
# token.  So a second test looks at the shape of the value itself: a UUID, a long
# hex run, a JWT, or a long mixed-character blob is a credential whatever it is
# called.
#
# The cost is explicit: a UUID that is really an opaque object id - a graph node,
# a task instance - gets masked too when it sits in an assignment or a for-list.
# The judge still sees the structure (``for id in [令牌] [令牌]; do curl ...``),
# which is what the verdict turns on, and a real session id in the clear is the
# worse outcome.
_CREDENTIAL_SHAPE = re.compile(
    r"""(?x)
    ^(?:
        [0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}
      | [0-9a-fA-F]{32,}
      | eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}
      | (?=[A-Za-z0-9+/_=-]*[A-Za-z])(?=[A-Za-z0-9+/_=-]*[0-9])
        [A-Za-z0-9+/_=-]{28,}={0,2}
    )$
    """
)

# ``for s in <uuid> <uuid> <uuid>; do`` - a list of session ids, taken verbatim
# from the recorded run.  Only the credential-shaped members are replaced; the
# words around them stay readable.
_FOR_LIST = re.compile(
    r"""(?x)
    (\bfor[ \t]+[A-Za-z_][A-Za-z0-9_]*[ \t]+in[ \t]+)
    ([^;\n|&]+)
    """
)


def _looks_like_credential(value: str) -> bool:
    """True for a value whose shape alone marks it as a secret."""

    candidate = value.strip().strip("\"'")
    if len(candidate) < 20:
        return False
    # A path, a URL or a repository/API route is infrastructure.  Without this
    # the generic branch below masked
    # ``projects/3/process-instance/list-paging`` - an API route being iterated
    # over - because it is long and mixes letters with digits.
    if "://" in candidate or "/" in candidate or ":" in candidate:
        return False
    if candidate[0] in "/~.-":
        return False
    # A file name is an asset, not a token: ``OriginzationService-CGXyz123.js``
    # matched the long mixed-character branch and was masked as a credential.
    if "." in candidate and candidate.rsplit(".", 1)[-1].isalnum():
        return False
    return bool(_CREDENTIAL_SHAPE.match(candidate))


def _mask_shell_assignment(name: str, value: str) -> str:
    """Return the value to send, or ``value`` itself when it is not a secret."""

    stripped = value.strip()
    if not stripped:
        return value
    # A reference is not a value: ``$SID``, ``$(cat k)``, ``(a b c)``.  Masking
    # one deletes the only readable part of the command.
    if stripped[0] in "$({`":
        return value
    if _looks_like_credential(stripped):
        return PLACEHOLDER_TOKEN
    if not _is_secret_variable(name):
        return value
    lowered = name.casefold()
    if len(stripped) < 8 and any(
        part in _SHORT_SEGMENTS for part in lowered.split("_") if part
    ):
        # ``AK=1`` is a loop counter; ``AK=AKIDabc123456`` is a key.
        return value
    return _secret_placeholder(name)


# A quoted credential with no key in front of it.  The recorded run has
# ``SID=sys.argv[1] if len(sys.argv)>1 else "96dbf03b-..."``: the assignment rule
# masked the left branch, but a Python conditional put a live session id inside a
# plain string literal, where no key/value rule could see it.
#
# Only shapes that cannot be anything else qualify (see _looks_like_credential),
# and paths, URLs and image names are excluded there, so the API routes and
# bundle filenames the workers iterate over stay readable.
_QUOTED_CREDENTIAL = re.compile(r"""(?<![\w"'=])(["'])([^"'\n]+)\1""")


# A bare word on the command line.  The recorded run ends with
# ``python3 q.py 96dbf03b-... "select ..."``: the session id is a positional
# argument, so it is neither an assignment, nor a for-list, nor a quoted string,
# and every earlier rule walked past it.
#
# Measured over 447 recorded commands this fires twice - once on the session id
# above and once on a run of ``A`` characters used as base64 padding.  The
# padding is excluded here because a single repeated character is not a secret.
_BARE_CREDENTIAL = re.compile(
    r"(?<![\w/.:-])"
    r"([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}|[0-9a-fA-F]{32,})"
    r"(?![\w/.:-])"
)


def _bare_credential_replacement(match: re.Match[str]) -> str:
    token = match.group(1)
    if len(set(token.lower())) < 3:
        return match.group(0)
    return PLACEHOLDER_TOKEN


def _quoted_credential_replacement(match: re.Match[str]) -> str:
    if not _looks_like_credential(match.group(2)):
        return match.group(0)
    return f"{match.group(1)}{PLACEHOLDER_TOKEN}{match.group(1)}"


def _for_list_replacement(match: re.Match[str]) -> str:
    values = match.group(2).split()
    changed = False
    masked = []
    for token in values:
        if _looks_like_credential(token):
            quote = '"' if token.startswith('"') else ("'" if token.startswith("'") else "")
            masked.append(f"{quote}{PLACEHOLDER_TOKEN}{quote}")
            changed = True
        else:
            masked.append(token)
    if not changed:
        return match.group(0)
    return f"{match.group(1)}{' '.join(masked)}"


def _shell_assign_replacement(match: re.Match[str]) -> str:
    prefix, name = match.group(1), match.group(2)
    for index, quote in ((3, '"'), (4, "'")):
        original = match.group(index)
        if original is not None:
            value = _mask_shell_assignment(name, original)
            if value == original:
                return match.group(0)
            return f"{prefix}{quote}{value}{quote}"
    original = match.group(5)
    value = _mask_shell_assignment(name, original)
    if value == original:
        return match.group(0)
    return f"{prefix}{value}"


def _shell_default_replacement(match: re.Match[str]) -> str:
    prefix, name, original = match.group(1), match.group(2), match.group(3)
    value = _mask_shell_assignment(name, original)
    if value == original:
        return match.group(0)
    return f"{prefix}{value}{match.group(4)}"


# A JSON body nested inside a shell command arrives with its quotes escaped:
# ``--data-binary '{\"username\":\"Terp\",\"passwd\":\"Sin308\"}'``.  The generic
# key/value rule needs a literal quote, so it walked straight past this and the
# password stayed in the clear - one of only two real leaks a 447-command sweep
# found.  This rule matches the escaped spelling.
_ESCAPED_JSON_KV = re.compile(
    r'((?:\\)?"(?:' + _SECRET_KEY + r')(?:\\)?"\s*:\s*(?:\\)?")'
    r'([^"\\\n]+)'
    r'((?:\\)?")',
    re.I,
)


def _escaped_json_replacement(match: re.Match[str]) -> str:
    key = match.group(1)
    if _is_reference(match.group(2)):
        return match.group(0)
    placeholder = _secret_placeholder(key)
    return f"{key}{placeholder}{match.group(3)}"


_ENV_SECRET = re.compile(
    r"""(?x)
    \b([A-Z0-9_]*(?:PASSWORD|PASSWD|PWD|SECRET|TOKEN|APIKEY|API_KEY|
                   ACCESS_KEY|PRIVATE_KEY|CREDENTIAL|AUTH_KEY)[A-Z0-9_]*)
    (\s*=\s*)
    """ + _VALUE
)

# The generic ``key=value`` / ``"key": "value"`` fallback.
_KEY_VALUE = re.compile(
    r"""(?ix)
    (["']?(?:%s)["']?\s*[:=]\s*)
    """ % _SECRET_KEY + _VALUE
)


def _long_flag_replacement(match: re.Match[str]) -> str:
    prefix = match.group(1)
    return _value_flag_replacement(_secret_placeholder(prefix), False)(match)


def _env_replacement(match: re.Match[str]) -> str:
    key = match.group(1)
    if key == "PWD":
        # The shell's working directory, not a password.
        return match.group(0)
    placeholder = _secret_placeholder(key)
    for index, quote in ((3, '"'), (4, "'")):
        if match.group(index) is not None:
            return f"{key}{match.group(2)}{quote}{placeholder}{quote}"
    return f"{key}{match.group(2)}{placeholder}"


def _key_value_replacement(match: re.Match[str]) -> str:
    key = match.group(1).strip("\"'=: \t")
    if key == "PWD":
        # Only the all-caps shell variable; a lowercase ``pwd=x`` is a password.
        return match.group(0)
    for index in (2, 3, 4):
        value = match.group(index)
        if value is not None:
            if _is_reference(value):
                return match.group(0)
            break
    placeholder = _secret_placeholder(key)
    for index, quote in ((2, '"'), (3, "'")):
        if match.group(index) is not None:
            return f"{match.group(1)}{quote}{placeholder}{quote}"
    return f"{match.group(1)}{placeholder}"


_ID_CARD = re.compile(r"(?<![\dA-Za-z])(\d{17}[\dXx])(?![\dA-Za-z])")

# The neighbours are ASCII alphanumerics, not only digits: a phone-shaped run
# inside a hex digest or a base64 blob is a false positive, and masking it would
# hide evidence for no privacy gain.
_PHONE = re.compile(r"(?<![\dA-Za-z+])(?:\+?86[- ]?)?(1[3-9]\d{9})(?![\dA-Za-z])")
_LANDLINE = re.compile(r"(?<![\dA-Za-z])(0\d{2,3}-?\d{7,8})(?![\dA-Za-z])")

# ``https://alice@github.com/...`` is url userinfo, not an address book entry:
# the local part before the ``/`` is an account name on the host that follows.
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+/])"
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}\b"
)

# A 16-19 digit run is as likely to be an order number as a card, so the card
# rule only fires where the key says card and the Luhn check agrees.
_BANK_CARD = re.compile(
    r"""(?ix)
    (["']?(?:card[-_]?no|card[-_]?number|bank[-_]?card|bank[-_]?account|
            acct[-_]?no|account[-_]?no|pan|卡号|银行卡|账号)["']?\s*[:=]\s*)
    (?:"(\d{13,19})"|'(\d{13,19})'|(\d{13,19}))
    """
)

# Bare ``name`` is deliberately absent.  ``{"name": "获取数据"}`` is a task name
# in Explore's own API calls, and masking it as a person removed the one word
# that said which object the command was creating.  Only keys that actually
# claim to hold a person's name count.
_NAME_KEYS = (
    r"real[-_]?name|full[-_]?name|true[-_]?name|nick[-_]?name|customer[-_]?name|"
    r"contact[-_]?name|user[-_]?name|username|display[-_]?name|person[-_]?name|"
    r"真实姓名|姓名|联系人|联系人姓名|用户名|昵称|客户名|客户姓名|收件人"
)
_NAME_KEYED = re.compile(
    r"""(?x)
    ((?:\\)?["']?(?:%s)(?:\\)?["']?\s*[:=]\s*(?:\\)?["']?)
    ([\u4e00-\u9fa5]{2,8}(?:·[\u4e00-\u9fa5]{2,8})?)
    ((?:\\)?["']?)
    """ % _NAME_KEYS
)

# A Chinese name where an id normally sits in a REST path:
# ``GET /user/谭子清/permissions``.  Only those segments are touched, and never
# a word a route would also use as a noun.
_NAME_SEGMENTS = (
    r"user|users|member|members|employee|employees|staff|customer|customers|"
    r"person|persons|account|accounts|contact|contacts|"
    r"用户|会员|员工|客户|人员|联系人"
)
_NOT_A_NAME = frozenset(
    "管理 列表 详情 新增 删除 修改 查询 导出 导入 信息 编辑 创建 批量 统计 "
    "首页 登录 注销 权限 角色 设置 搜索 个人 中心 操作 审核 状态 数据".split()
)
_NAME_IN_PATH = re.compile(
    r"""(?x)
    (/(?:%s)/)
    ([\u4e00-\u9fa5]{2,8})
    (?=[/?"'&\s]|$)
    """ % _NAME_SEGMENTS
)

# A bare ``name`` key is too common to mask on its own: ``{"name": "获取数据"}``
# is a task name in Explore's own API calls, and hiding it removed the one word
# that said what was being created.  But ``name=谭子清`` is a real person, and
# dropping that rule would let every form-posted name through.
#
# The surname is what separates them.  A two-to-four character Chinese value
# beginning with a common surname is a person; ``获取数据`` begins with a verb.
# The list is the usual hundred, which covers essentially every real name.
_SURNAMES = frozenset(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘"
    "于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江"
    "尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤欧阳司马诸葛上官"
)
_NAME_BARE_KEYED = re.compile(
    r"""(?x)
    ((?:\\)?["']?(?:name|nick)(?:\\)?["']?\s*[:=]\s*(?:\\)?["']?)
    ([\u4e00-\u9fa5]{2,8})
    ((?:\\)?["']?)
    """,
    re.I,
)


def _bare_name_replacement(match: re.Match[str]) -> str:
    value = match.group(2)
    if value[:2] not in _SURNAMES and value[:1] not in _SURNAMES:
        return match.group(0)
    return f"{match.group(1)}{PLACEHOLDER_NAME}{match.group(3)}"

# ``macAddress`` contains ``address``, and a MAC is the most useful thing in an
# access-point payload - it identifies the device being changed.  Masking it as a
# postal address cost 12 replacements in a 447-command sweep, all of them
# evidence.  So the key is checked, not just the suffix.
_ADDRESS_KEYED = re.compile(
    r"""(?ix)
    (["']?(?:(?<!mac)(?<!mac_)(?<!mac-)(?:address|addr|street)|住址|地址|收货地址|家庭住址)["']?\s*[:=]\s*)
    """ + _VALUE
)


def _bank_card_replacement(match: re.Match[str]) -> str:
    for index, quote in ((2, '"'), (3, "'")):
        if match.group(index) is not None:
            if _is_luhn(match.group(index)):
                return f"{match.group(1)}{quote}{PLACEHOLDER_BANK_CARD}{quote}"
            return match.group(0)
    if _is_luhn(match.group(4)):
        return f"{match.group(1)}{PLACEHOLDER_BANK_CARD}"
    return match.group(0)


def _path_name_replacement(match: re.Match[str]) -> str:
    if match.group(2) in _NOT_A_NAME:
        return match.group(0)
    return match.group(1) + PLACEHOLDER_NAME


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def redact(text: str) -> tuple[str, int]:
    """Return ``(masked_text, replacements)``.

    Pure regex over one string: no network, no state, nothing worth measuring on
    the guard's latency budget.  Calling it twice returns the same text, because
    every placeholder is shaped so the rule that produced it cannot match it.
    """

    if not text:
        return text, 0

    count = 0
    steps = (
        (_PRIVATE_KEY, PLACEHOLDER_PRIVATE_KEY),
        (_COOKIE_HEADER, lambda match: _header_replacement(match, PLACEHOLDER_COOKIE)),
        (_AUTH_HEADER, lambda match: _header_replacement(match, PLACEHOLDER_TOKEN)),
        (_JWT, PLACEHOLDER_TOKEN),
        (
            _URL_USERINFO,
            lambda match: f"{match.group(1)}{PLACEHOLDER_PASSWORD}{match.group(3)}",
        ),
        (_SHORT_SECRET_FLAG, _short_flag_replacement),
        (_DB_PASSWORD_FLAG, _value_flag_replacement(PLACEHOLDER_PASSWORD)),
        (_REDIS_PASSWORD_FLAG, _value_flag_replacement(PLACEHOLDER_PASSWORD)),
        (_SSHPASS_FLAG, _value_flag_replacement(PLACEHOLDER_PASSWORD)),
        (_SMBCLIENT_FLAG, _value_flag_replacement(PLACEHOLDER_PASSWORD, split_percent=True)),
        (_LONG_SECRET_FLAG, _long_flag_replacement),
        (_CURL_COOKIE_FLAG, _cookie_flag_replacement),
        (_SHELL_DEFAULT_ASSIGN, _shell_default_replacement),
        (_SHELL_ASSIGN, _shell_assign_replacement),
        (_FOR_LIST, _for_list_replacement),
        (_QUOTED_CREDENTIAL, _quoted_credential_replacement),
        (_BARE_CREDENTIAL, _bare_credential_replacement),
        (_ENV_SECRET, _env_replacement),
        (_ESCAPED_JSON_KV, _escaped_json_replacement),
        (_KEY_VALUE, _key_value_replacement),
        (
            _ID_CARD,
            lambda match: PLACEHOLDER_ID_CARD
            if _is_id_card(match.group(1))
            else match.group(0),
        ),
        (
            _PHONE,
            lambda match: match.group(0)[: -len(match.group(1))] + PLACEHOLDER_PHONE,
        ),
        (_LANDLINE, PLACEHOLDER_LANDLINE),
        (_EMAIL, PLACEHOLDER_EMAIL),
        (_BANK_CARD, _bank_card_replacement),
        (
            _NAME_KEYED,
            # Group 2 is the name; the escaped-quote form has no separate
            # opening-quote group, so the key prefix and the trailing quote are
            # kept verbatim around the placeholder.
            lambda match: f"{match.group(1)}{PLACEHOLDER_NAME}{match.group(3)}",
        ),
        (_NAME_BARE_KEYED, _bare_name_replacement),
        (_NAME_IN_PATH, _path_name_replacement),
        (_ADDRESS_KEYED, _value_flag_replacement(PLACEHOLDER_ADDRESS)),
    )
    for pattern, replacement in steps:
        text, hits = _apply(pattern, replacement, text)
        count += hits
    return text, count


def _apply(pattern: re.Pattern[str], replacement, text: str) -> tuple[str, int]:
    """Replace every match whose replacement actually differs.

    ``subn`` counts matches, not changes, and a rule that inspects the value
    (the id-card checksum, the Luhn gate) deliberately leaves most of its
    matches alone.  Counting those as redactions would overstate what left the
    host, so the count is taken from the replacements that changed something.
    """

    pieces: list[str] = []
    cursor = 0
    hits = 0
    for match in pattern.finditer(text):
        new = replacement(match) if callable(replacement) else replacement
        if new == match.group(0):
            continue
        pieces.append(text[cursor : match.start()])
        pieces.append(new)
        cursor = match.end()
        hits += 1
    if not hits:
        return text, 0
    pieces.append(text[cursor:])
    return "".join(pieces), hits


def _main(argv: list[str]) -> int:
    """``python -m axiom.modules.guard.redact '<command>'`` dry-run."""

    import sys

    if not argv:
        print("usage: python -m axiom.modules.guard.redact '<command>'", file=sys.stderr)
        return 2
    text, count = redact(" ".join(argv))
    print(text)
    print(f"[{count} 处替换]", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - manual dry-run only
    import sys

    raise SystemExit(_main(sys.argv[1:]))
