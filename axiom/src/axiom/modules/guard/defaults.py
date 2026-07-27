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
HARD_BLOCK_RULES = [
    {
        "reason": "修改或重置用户密码",
        "pattern": r"\b(passwd|chpasswd|reset[_-]?password)\b",
        "enabled": True,
    },
    {
        "reason": "删除或清空关键数据",
        "pattern": (
            r"\b(drop\s+table|truncate\s+table|redis-cli\s+flush(all|db))\b"
            r"|rm\s+-rf\s+(/|/etc|/var|/usr|/opt|/home|\*)"
        ),
        "enabled": True,
    },
    {
        "reason": "停止或重启业务服务",
        "pattern": (
            r"\b(systemctl\s+(stop|restart)|shutdown|reboot|"
            r"service\s+\S+\s+(stop|restart))\b"
        ),
        "enabled": True,
    },
    {
        "reason": "篡改账户或权限体系",
        "pattern": (
            r"\b(useradd|userdel|usermod|groupadd|groupdel|"
            r"grant\s+all|revoke\s+all)\b"
        ),
        "enabled": True,
    },
    {
        "reason": "高并发或 DoS 操作",
        "pattern": r"\b(slowloris|hping3|ab\s+-n\s+\d{5,}|wrk\s+.*-c\s+\d{3,})\b",
        "enabled": True,
    },
]
SYSTEM_PROMPT = """你是授权渗透测试的安全守门员。默认放行正常的漏洞探测与最小化验证；只有操作会直接破坏业务可用性、不可逆修改真实数据、触达真实用户权益、造成高并发拒绝服务或明显越过授权边界时才阻断。只回复 ALLOW 或 BLOCK:<不超过30字的原因>。"""
