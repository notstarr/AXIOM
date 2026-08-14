from __future__ import annotations


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

# These expressions intentionally require a command/operator context for destructive
# operations.  A security payload such as ``curl -d 'DROP TABLE ...'`` or an upload
# body containing ``reset_password`` must reach the judge instead of being blocked by
# a keyword appearing in a request payload.
HARD_BLOCK_RULES = [
    {
        "reason": "修改或重置用户密码",
        "pattern": (
            r"(?:^|[;&|]\s*)(?:sudo\s+|doas\s+)?"
            r"(?:passwd|chpasswd|htpasswd)\b"
            r"|(?:^|[;&|]\s*)(?:sudo\s+|doas\s+)?"
            r"(?:mysql|mariadb|psql|cockroach|sqlplus)\b[^;&|]*"
            r"(?:-e|-c)\s+[^;&|]*\b(?:alter\s+(?:user|role)|set\s+password)\b"
        ),
        "enabled": True,
    },
    {
        "reason": "修改系统或服务配置",
        "pattern": (
            r"(?:^|[;&|]\s*)(?:sudo\s+|doas\s+)?"
            r"(?:systemctl\s+(?:enable|disable|mask|unmask|edit)|"
            r"service\s+\S+\s+(?:enable|disable)|"
            r"crontab\s+(?!-l\b)\S+|"
            r"(?:iptables|ip6tables|nft)\b\s+"
            r"(?:-[AIXDF]|add|delete|flush|insert)|"
            r"ufw\s+(?:enable|disable|allow|deny|delete))\b"
            r"|(?:^|[;&|]\s*)(?:sudo\s+|doas\s+)?"
            r"(?:printf|echo)\b[^;&|]*(?:>>?|\|\s*tee\b)\s*"
            r"/(?:etc|usr/lib/systemd|Library/LaunchAgents|Library/LaunchDaemons)"
            r"(?:/|\s|$)"
            r"|(?:^|[;&|]\s*)(?:sudo\s+|doas\s+)?"
            r"(?:cp|mv|install|tee|sed\s+-i|perl\s+-i)\b[^;&|]*"
            r"/(?:etc|usr/lib/systemd|Library/LaunchAgents|Library/LaunchDaemons)"
            r"(?:/|\s|$)"
        ),
        "enabled": True,
    },
    {
        "reason": "篡改账户或权限体系",
        "pattern": (
            r"(?:^|[;&|]\s*)(?:sudo\s+|doas\s+)?"
            r"(?:useradd|userdel|usermod|user--|groupadd|groupdel|groupmod|"
            r"adduser|deluser)\b"
            r"|(?:^|[;&|]\s*)(?:sudo\s+|doas\s+)?"
            r"(?:mysql|mariadb|psql|cockroach|sqlplus)\b[^;&|]*"
            r"(?:-e|-c)\s+[^;&|]*\b(?:create|alter|drop)\s+(?:user|role)\b"
            r"|(?:^|[;&|]\s*)(?:sudo\s+|doas\s+)?"
            r"(?:mysql|mariadb|psql|cockroach|sqlplus)\b[^;&|]*"
            r"(?:-e|-c)\s+[^;&|]*\b(?:grant|revoke)\b"
        ),
        "enabled": True,
    },
    {
        "reason": "删除或大规模破坏关键数据",
        "pattern": (
            r"(?:^|[;&|]\s*)(?:sudo\s+|doas\s+)?"
            r"(?:dropdb|redis-cli\s+(?:flushall|flushdb)|"
            r"rm\s+(?=[^;&|]*-[^\s;&|]*r)(?:-[^\s;&|]+\s+)+"
            r"(?:/|~(?:/|$)|\*|\.{1,2}(?:/|$)|"
            r"/(?:etc|var|usr|opt|home|root|bin|sbin|boot|lib|System|Applications)(?:/|\s|$))"
            r"|find\s+(?:/|\.\.?|~)[^;&|]*\s-delete(?:\s|$))"
            r"|(?:^|[;&|]\s*)(?:sudo\s+|doas\s+)?"
            r"(?:mysql|mariadb|psql|sqlite3|cockroach|sqlplus)\b[^;&|]*"
            r"(?:-e|-c)?\s*[^;&|]*\b(?:drop\s+(?:database|schema|table)|"
            r"truncate(?:\s+table)?|delete\s+from\s+[A-Za-z_][\w$]*)\b"
        ),
        "enabled": True,
    },
    {
        "reason": "停止或重启业务服务",
        "pattern": (
            r"(?:^|[;&|]\s*)(?:sudo\s+|doas\s+)?"
            r"(?:systemctl\s+(?:stop|restart|disable)|service\s+\S+\s+(?:stop|restart)|"
            r"launchctl\s+(?:unload|bootout|remove)|"
            r"docker(?:-compose|\s+compose)?\s+(?:stop|restart|down)|"
            r"docker\s+stop|shutdown|reboot|poweroff|halt)\b"
            r"|(?:^|[;&|]\s*)(?:sudo\s+|doas\s+)?"
            r"(?:kill|pkill|killall)\b[^;&|]*\b"
            r"(?:axiom|nginx|apache2?|httpd|gunicorn|uvicorn|postgres(?:ql)?|"
            r"mysqld?|redis-server|node)\b"
        ),
        "enabled": True,
    },
    {
        "reason": "高并发或 DoS 操作",
        "pattern": (
            r"\b(?:slowloris|slowhttptest|goldeneye|hping3\s+[^;&|]*--flood|"
            r"nping\s+[^;&|]*(?:--rate|--data-length)|"
            r"ab\s+[^;&|]*-n\s*\d{5,}|ab\s+[^;&|]*-c\s*\d{4,}|"
            r"wrk\s+[^;&|]*(?:-c\s*\d{3,}|-t\s*\d{3,})|"
            r"hey\s+[^;&|]*-n\s*\d{5,}|"
            r"bombardier\s+[^;&|]*(?:-c\s*\d{3,}|-n\s*\d{5,})|"
            r"vegeta\s+attack\s+[^;&|]*-rate\s*(?:[1-9]\d{3,}|[1-9]\d*k)\b)"
        ),
        "enabled": True,
    },
]

SYSTEM_PROMPT = """你是授权渗透测试的安全守门员。输入是当前 Task 中一次即将执行的工具调用。

绝大多数授权渗透测试操作都应放行，包括攻击 Payload、可恢复的最小化验证和带有“写”字样的测试。只有该次调用本身明确会直接破坏目标业务系统正常运行、造成真实用户数据丢失或不可逆篡改时才阻断。

【必须 BLOCK——仅限以下情况】
1. 修改或重置任意用户或管理员密码，或强制用户下线。
2. 修改服务器、Web/数据库服务、防火墙、启动项或计划任务配置。
3. 创建、修改或删除用户、角色或权限体系。
4. 删除或大规模破坏数据或重要文件，例如 DROP/TRUNCATE、无条件全表 DELETE、清空数据库或删除关键目录。
5. 停止、禁用、重启业务服务、系统或关键业务进程。
6. 明确的大规模压测、洪泛或高并发 DoS。

【必须 ALLOW——授权渗透的正常手段】
- SQL 注入、XSS、命令注入、SSTI、反序列化、SSRF、路径遍历、文件包含、XXE 等探测与最小利用。
- SQLi Payload 中出现 UPDATE、DELETE、DROP 或写文件语法，本身不代表会真实执行破坏；除非当前调用明确在直接破坏真实数据。
- 文件上传测试，包括上传 WebShell 作为漏洞验证手段。
- 常规速率的账号、密码和验证码测试，以及非 DoS 级口令爆破。
- 信息收集、端口/目录扫描、枚举、抓取、读取、查询和无害侦察命令（whoami/id/uname/ls/cat/ifconfig）。
- 创建测试数据、测试订单或其他可恢复的最小业务验证，只要不修改账户、角色、权限或关键数据。

【判定原则】
- 判断当前工具调用的实际直接效果，不因 Payload、URL、参数、文件内容或工具名称中出现敏感关键词就 BLOCK。
- “写入”“上传”“爆破”“利用”本身不是拦截理由。
- 默认倾向 ALLOW；只有明确命中上述 BLOCK 清单才 BLOCK。
- 信息不足、结果不确定，或看起来只是漏洞 Payload 时必须 ALLOW。
- 不根据道德评价、攻击性措辞或漏洞类别拦截。

只回复一行，不要任何额外内容：
ALLOW
或
BLOCK:<不超过30字，说明命中了哪条 BLOCK 清单>"""
