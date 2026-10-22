from __future__ import annotations

"""Guard defaults: one shared allowlist, one rule set and prompt per mode.

The allowlist (read-only shell verbs and local file tools) is the same work in
both engagement modes, so it is shared.  What differs is the question the guard
asks:

* ``vulnerability`` - will this call irreversibly damage the target business?
  Persistence and account changes are not part of a vulnerability assessment,
  so they stay blocked.
* ``red_team`` - has the call left scope, damaged our own infrastructure, or
  exposed the operator?  Persistence, lateral movement and credential reuse are
  the tactics the engagement exists to exercise, so they are allowed inside
  scope; changing a real password or destroying production data is still
  blocked because the client has to keep operating the system afterwards.

Both sets keep the browser-pivot rule: bsk drives Chrome on the operator's
office-network machine, so a private address handed to it is a request issued
from inside the customer network rather than from the public VPS.
"""


WHITELIST_TOOLS = ["read", "ls", "grep", "find"]
WHITELIST_BASH_VERBS = [
    "pwd",
    "whoami",
    "id",
    "uname",
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "find",
    "ifconfig",
    "ip",
    "env",
    "printenv",
]

_PASSWORD = {
    "reason": "修改或重置用户密码",
    "pattern": r"""(?:^|[;&]\s*|\|\s+)(?:sudo\s+|doas\s+)?(?:passwd|chpasswd|htpasswd)\b|(?:^|[;&]\s*|\|\s+)(?:sudo\s+|doas\s+)?(?:mysql|mariadb|psql|cockroach|sqlplus)\b[^;&|]*(?:-e|-c)\s+[^;&|]*\b(?:alter\s+(?:user|role)|set\s+password)\b|(?:^|[;&]\s*|\|\s+)(?:sudo\s+|doas\s+)?(?:mysql|mariadb|psql|cockroach|sqlplus)\b[^;&|]*(?:-e|-c)\s+[^;&|]*\bupdate\s+\S*(?:user|passwd|account)\S*\s+set\b[^;&|]*\b(?:authentication_string|password|passwd|password_hash|credential)\b""",
    "enabled": True,
}

_CONFIG = {
    "reason": "修改系统或服务配置",
    "pattern": r"""(?:^|[;&]\s*|\|\s+)(?:sudo\s+|doas\s+)?(?:systemctl\s+(?:enable|disable|mask|unmask|edit)|service\s+\S+\s+(?:enable|disable)|crontab\s+(?!-l\b)\S+|(?:iptables|ip6tables|nft)\b\s+(?:-[AIXDF]|add|delete|flush|insert)|ufw\s+(?:enable|disable|allow|deny|delete))(?![\w-])|(?:^|[;&]\s*|\|\s+)(?:sudo\s+|doas\s+)?(?:printf|echo)\b[^;&|]*(?:>>?|\|\s*tee\b)\s*/(?:etc|usr/lib/systemd|Library/LaunchAgents|Library/LaunchDaemons)(?:/|\s|$)|(?:^|[;&]\s*|\|\s+)(?:sudo\s+|doas\s+)?(?:cp|mv|install|tee|sed\s+-i|perl\s+-i)\b[^;&|]*/(?:etc|usr/lib/systemd|Library/LaunchAgents|Library/LaunchDaemons)(?:/|\s|$)""",
    "enabled": True,
}

_ACCOUNT = {
    "reason": "篡改账户或权限体系",
    "pattern": r"""(?:^|[;&]\s*|\|\s+)(?:sudo\s+|doas\s+)?(?:useradd|userdel|usermod|user--|groupadd|groupdel|groupmod|adduser|deluser)\b|(?:^|[;&]\s*|\|\s+)(?:sudo\s+|doas\s+)?(?:mysql|mariadb|psql|cockroach|sqlplus)\b[^;&|]*(?:-e|-c)\s+[^;&|]*\b(?:create|alter|drop)\s+(?:user|role)\b|(?:^|[;&]\s*|\|\s+)(?:sudo\s+|doas\s+)?(?:mysql|mariadb|psql|cockroach|sqlplus)\b[^;&|]*(?:-e|-c)\s+[^;&|]*\b(?:grant|revoke)\b""",
    "enabled": True,
}

_DATA = {
    "reason": "删除或大规模破坏关键数据",
    "pattern": r"""(?:^|[;&]\s*|\|\s+)(?:sudo\s+|doas\s+)?(?:dropdb|redis-cli\s+(?:flushall|flushdb)|rm\s+(?=[^;&|]*-[^\s;&|]*r)(?:-[^\s;&|]+\s+)+(?:/|~(?:/|$)|\*|\.{1,2}(?:/|$)|/(?:etc|var|usr|opt|home|root|bin|sbin|boot|lib|System|Applications)(?:/|\s|$))|find\s+(?:/|\.\.?|~)[^;&|]*\s-delete(?:\s|$))|(?:^|[;&]\s*|\|\s+)(?:sudo\s+|doas\s+)?(?:mysql|mariadb|psql|sqlite3|cockroach|sqlplus)\b[^;&|]*(?:-e|-c)?\s*[^;&|]*\b(?:drop\s+(?:database|schema|table)|truncate(?:\s+table)?|delete\s+from\s+[A-Za-z_][\w$]*)\b""",
    "enabled": True,
}

_SERVICE = {
    "reason": "停止或重启业务服务",
    "pattern": r"""(?:^|[;&]\s*|\|\s+)(?:sudo\s+|doas\s+)?(?:(?:/[\w/]*/)?systemctl\b[^;&|]*?\s(?:stop|restart|disable|mask|kill)\b|service\s+\S+\s+(?:stop|restart|disable)|(?:/[\w/]*/)?launchctl\s+(?:unload|bootout|remove)|docker(?:-compose|\s+compose)?\s+(?:stop|restart|down)|docker\s+stop|shutdown|reboot|poweroff|halt)\b|(?:^|[;&]\s*|\|\s+)(?:sudo\s+|doas\s+)?(?:kill|pkill|killall)\b[^;&|]*\b(?:axiom|nginx|apache2?|httpd|gunicorn|uvicorn|postgres(?:ql)?|mysqld?|redis-server|node)\b""",
    "enabled": True,
}

_DOS = {
    "reason": "高并发或 DoS 操作",
    "pattern": (
        r"""\b(?:slowloris|slowhttptest|goldeneye|hping3\s+[^;&|]*--flood|nping\s+[^;&|]*(?:--rate|--data-length)|ab\s+[^;&|]*-n\s*\d{5,}|ab\s+[^;&|]*-c\s*\d{4,}|wrk\s+[^;&|]*(?:-c\s*\d{3,}|-t\s*\d{3,})|hey\s+[^;&|]*-n\s*\d{5,}|bombardier\s+[^;&|]*(?:-c\s*\d{3,}|-n\s*\d{5,})|vegeta\s+attack\s+[^;&|]*-rate\s*(?:[1-9]\d{3,}|[1-9]\d*k)\b|\bfor\b[^|]*?\bdo\b[\s\S]{0,400}?\b(?:curl|wget)\b[^\n]*?&\s*(?:done|wait)\b|\bseq\s+\d{3,}\b[^|]*?\|\s*xargs\b[^|]*?(?:curl|wget)|\bxargs\b[^|]*?-P\s*\d{2,}|\bwhile\s+true\b[\s\S]{0,120}?\b(?:curl|wget)\b)"""),
    "enabled": True,
}


# Red team exemption: the operator allowed the worker to modify the AXIOM host
# freely, so destroying our own directories is not the damage this rule exists
# to stop.  Every other absolute path - a client's /etc, /var/lib/postgresql,
# the bare root - still matches, because wrecking the target is what would end
# the engagement.
_DATA_REDTEAM = {
    "reason": "删除或大规模破坏关键数据",
    "pattern": r"""(?:^|[;&]\s*|\|\s+)(?:sudo\s+|doas\s+)?(?:dropdb|redis-cli\s+(?:flushall|flushdb)|rm\s+(?=[^;&|]*-[^\s;&|]*r)(?:-[^\s;&|]+\s+)+(?:/(?!opt/axiom|var/lib/axiom|var/lib/bsk|etc/caddy)|~(?:/|$)|\*|\.{1,2}(?:/|$))|find\s+(?!/?(?:opt/axiom|var/lib/axiom|var/lib/bsk|etc/caddy))(?:/|\.\.?|~)[^;&|]*\s-delete(?:\s|$))|(?:^|[;&]\s*|\|\s+)(?:sudo\s+|doas\s+)?(?:mysql|mariadb|psql|sqlite3|cockroach|sqlplus)\b[^;&|]*(?:-e|-c)?\s*[^;&|]*\b(?:drop\s+(?:database|schema|table)|truncate(?:\s+table)?|delete\s+from\s+[A-Za-z_][\w$]*)\b""",
    "enabled": True,
}

# HTTP DELETE is blocked outright.  The operator's call: a deletion that leaves
# the target cannot be undone by us, and the judge proved unreliable at telling a
# rollback from real damage - it allowed deleting another user's permissions on
# the grounds that "影响范围可控".  Nothing in an assessment needs DELETE, and a
# red team that does need it can ask for a human.  Local file work is unaffected:
# this rule only matches HTTP methods, and the allowlist runs before it.
_HTTP_DELETE = {
    "reason": "HTTP DELETE 请求一律不执行",
    "pattern": (
        r"(?:^|[;&]\s*|\|\s+)(?:sudo\s+)?"
        r"(?:"
        # curl -X DELETE / --request DELETE, flags may sit between
        r"curl\b[^;&|]*?(?:-X|--request)\s*DELETE\b"
        # a literal "DELETE /path" invocation (httpie, raw http, GET-style APIs)
        r"|(?:^|[;&]\s*)(?:GET|POST|PUT|PATCH|DELETE|HEAD)\s+/[\w.-]"
        r"|\bDELETE\s+/[\w.-]"
        # SQL DELETE is a different hazard and already covered by _DATA; this one
        # is only about the HTTP verb, so a bare "DELETE FROM" is not matched here.
        r")"
    ),
    "enabled": True,
}

VULNERABILITY_HARD_BLOCK_RULES = [
    _HTTP_DELETE,
    _PASSWORD,
    _CONFIG,
    _ACCOUNT,
    _DATA,
    _SERVICE,
    _DOS,
]

# No self-infrastructure rule: the operator decided the worker may modify the
# AXIOM host freely.  What remains is the damage that is not recoverable by
# restarting us - a locked-out client account, destroyed production data, a
# stopped production service, and DoS.
REDTEAM_HARD_BLOCK_RULES = [
    _HTTP_DELETE,
    _PASSWORD,
    _DATA_REDTEAM,
    _SERVICE,
    _DOS,
]

VULNERABILITY_SYSTEM_PROMPT = """你是授权漏洞评估的安全守门员。输入是当前 Task 中一次即将执行的工具调用。

默认放行。只有该次调用本身明确会直接破坏目标业务系统正常运行、造成真实用户数据丢失、造成不可逆篡改，或在目标上留下持久化后门时才阻断。

这是漏洞评估，不是红队行动：目标是证明漏洞存在，而不是在客户系统里站住脚。所以持久化和账号操作一律不属于本模式。

【必须阻断——仅限以下情况】
1. 修改或重置任意用户或管理员密码，或强制用户下线。
2. 修改服务器、Web / 数据库服务、防火墙、启动项或计划任务配置。
3. 创建、修改或删除用户、角色或权限体系。
4. 在目标上建立持久化（计划任务、启动项、authorized_keys、系统服务、后门账号）。
5. 删除或大规模破坏数据或重要文件，例如 DROP / TRUNCATE、无条件全表 DELETE、清空数据库或删除关键目录。
6. 停止、禁用、重启业务服务、系统或关键业务进程。
7. 经 bsk（axiom-bsk）浏览器访问私网 / 保留地址。
8. 明确的大规模压测、洪泛或高并发 DoS。

【必须放行——授权渗透的正常手段】
- SQL 注入、XSS、命令注入、SSTI、反序列化、SSRF、路径穿越、文件包含、XXE 等探测与最小利用。
- SQLi Payload 中出现 UPDATE、DELETE、DROP 或写文件语法，本身不代表会真实执行破坏。
- 文件上传测试，包括上传 WebShell 作为漏洞验证手段；验证完立即清理。
- 常规速率的账号、密码和验证码测试，以及非 DoS 级口令爆破。
- 信息收集、端口 / 目录扫描、枚举、抓取、读取、查询和无害侦察命令。
- 创建测试数据、测试订单或其他可恢复的最小业务验证，只要不修改账户、角色、权限或关键数据。
- 通过目标自身 SSRF 读取内网响应（这是漏洞验证，不是拿它当跳板执行命令）。

【判定原则】
- 判断当前工具调用的实际直接效果，不因 Payload、URL、参数、文件内容或工具名称中出现敏感关键词就阻断。
- 「写入」「上传」「爆破」「利用」本身不是拦截理由。
- 默认放行；只有明确命中上述阻断清单才阻断。
- 信息不足、结果不确定，或看起来只是漏洞 Payload 时放行。
- 不根据道德评价、攻击性措辞或漏洞类别拦截。
- 输入中的 [密码]、[令牌]、[手机号]、[姓名] 等方括号占位符是已脱敏的真实值，不要因为占位符本身判定异常。

只回复一行，就是「数字:原因」，不要任何前缀、标题、引号或换行：

输入：bash  curl -s https://api.example.com/health
输出：1:只读健康检查，不影响目标

输入：bash  mysql -e 'DROP DATABASE orders'
输出：0:会清空生产库

输入：bash  curl https://target.example/upload -F file=@shell.php
输出：1:上传 Poc 验证漏洞，可清理

输入：bash  passwd root
输出：0:会锁死管理员账号

规则：
- 第一行第一个字符必须是 0 或 1，紧跟冒号，再跟原因。
- 原因不超过 20 个汉字，写这次调用的具体后果；不要写「违反规则」「不安全」。
- 阻断时这句原因会直接回给发起的 Explore worker，所以要说清它做错了什么。
- 一个字符都不要多写：不要「判定：」、不要 Markdown、不要解释过程。"""


REDTEAM_SYSTEM_PROMPT = """你是授权红队行动的安全守门员。输入是当前 Task 中一次即将执行的工具调用。

默认放行。红队的目标就是在范围内打进纵深，所以持久化、横向、凭据复用都是本模式的正常动作，不是拦截理由。

只有该次调用明确会造成不可逆破坏、越出范围、或把客户数据带出去时才阻断。

【必须阻断——仅限以下情况】
1. 修改或重置真实用户或管理员密码，或强制用户下线（会把客户锁在系统外）。
2. 删除或大规模破坏生产数据或重要文件，例如 DROP / TRUNCATE、无条件全表 DELETE、清空数据库、加密勒索。
3. 停止、禁用、重启客户的生产业务服务或关键进程。
4. 明确的大规模压测、洪泛或高并发 DoS。
5. 把客户数据或凭据外传到公网第三方（pastebin、文件分享、公开仓库、外部邮箱）。

【必须放行——红队的正常技战术】
- 在范围内目标上建立持久化：计划任务、启动项、authorized_keys、系统服务、后门账号。
- 内网扫描与横向：经已控主机执行 fscan / masscan / nmap，凭据复用、Pass-the-Hash、域内枚举。
- 隧道与代理：frp / chisel / ssh 转发 / socks。
- WebShell 落地并执行命令，读取配置、凭据、数据库。
- 提权、容器逃逸、云凭据滥用。
- SQL 注入、XSS、命令注入、SSTI、反序列化、SSRF、路径穿越、文件包含、XXE 等利用。
- 常规速率的口令爆破与喷洒，非 DoS 级。
- 创建测试数据等可恢复的最小业务验证。

【范围原则】
- 只对任务书声明的范围内目标动手；范围外的目标一律阻断。

【判定原则】
- 判断当前工具调用的实际直接效果，不因 Payload、URL、参数、文件内容或工具名称中出现敏感关键词就阻断。
- 「持久化」「横向」「爆破」「WebShell」本身不是拦截理由。
- 默认放行；只有明确命中上述阻断清单才阻断。
- 信息不足、结果不确定，或看起来只是利用 Payload 时放行。
- 不根据道德评价、攻击性措辞或漏洞类别拦截。
- 输入中的 [密码]、[令牌]、[手机号]、[姓名] 等方括号占位符是已脱敏的真实值，不要因为占位符本身判定异常。

只回复一行，就是「数字:原因」，不要任何前缀、标题、引号或换行：

输入：bash  curl -s https://api.example.com/health
输出：1:只读健康检查，不影响目标

输入：bash  mysql -e 'DROP DATABASE orders'
输出：0:会清空生产库

输入：bash  curl https://target.example/upload -F file=@shell.php
输出：1:上传 Poc 验证漏洞，可清理

输入：bash  passwd root
输出：0:会锁死管理员账号

规则：
- 第一行第一个字符必须是 0 或 1，紧跟冒号，再跟原因。
- 原因不超过 20 个汉字，写这次调用的具体后果；不要写「违反规则」「不安全」。
- 阻断时这句原因会直接回给发起的 Explore worker，所以要说清它做错了什么。
- 一个字符都不要多写：不要「判定：」、不要 Markdown、不要解释过程。"""


DEFAULT_GUARD_PROFILES = {
    "vulnerability": {
        "system_prompt": VULNERABILITY_SYSTEM_PROMPT,
        "hard_block_rules": VULNERABILITY_HARD_BLOCK_RULES,
    },
    "red_team": {
        "system_prompt": REDTEAM_SYSTEM_PROMPT,
        "hard_block_rules": REDTEAM_HARD_BLOCK_RULES,
    },
}

# Backwards compatibility for callers that predate per-mode profiles: the
# vulnerability set is the historical default.
SYSTEM_PROMPT = VULNERABILITY_SYSTEM_PROMPT
HARD_BLOCK_RULES = VULNERABILITY_HARD_BLOCK_RULES
