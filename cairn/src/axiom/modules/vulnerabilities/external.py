from __future__ import annotations

from typing import Any

from axiom.modules.vulnerabilities.schemas import ReportVulnerability


MOZZIE_TYPES = {
    "未授权访问",
    "越权访问",
    "认证绕过",
    "SQL注入",
    "XSS",
    "SSRF",
    "命令注入",
    "CSRF",
    "任意文件上传",
    "开放重定向",
    "逻辑漏洞",
    "信息泄漏",
    "密钥硬编码",
    "其他",
}


class MozzieAdapter:
    def build_payload(
        self,
        vulnerability: ReportVulnerability,
        *,
        resolved_project: str,
    ) -> dict[str, Any]:
        return {
            "vuln_name": vulnerability.title,
            "vuln_type": vulnerability.type if vulnerability.type in MOZZIE_TYPES else "其他",
            "project": resolved_project or "others",
            "severity": self._severity(vulnerability.severity),
            "source": "axiom",
            "curl_command": vulnerability.curl_command,
            "description": {
                "danger": vulnerability.impact,
                "premise": vulnerability.prerequisites,
                "reproduce": "\n".join(vulnerability.reproduction_steps),
                "fix": vulnerability.remediation,
            },
        }

    @staticmethod
    def _severity(value: str) -> str:
        if value in {"critical", "high"}:
            return "高危"
        if value == "medium":
            return "中危"
        return "低危"
