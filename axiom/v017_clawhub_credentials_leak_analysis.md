# 漏洞深度分析报告：v017 - clawhub-ops 内部运维技能泄露 5 个 ClawHub API Token、GitHub PAT 及代理凭证

---

## 一、漏洞基本信息

| 属性 | 详情 |
| :--- | :--- |
| **漏洞编号** | `v017` |
| **漏洞标题** | clawhub-ops 内部运维技能泄露 5 个 ClawHub API Token、GitHub PAT 及代理凭证 |
| **风险等级** | **High / Critical（高危/严重）** |
| **漏洞类型** | 软件供应链核心密钥泄露（Supply Chain Credential Disclosure） |
| **影响组件** | 内部运维技能 `clawhub-ops`（文件路径：`references/accounts.md`） |

---

## 二、到底泄露了什么信息？

通过沙箱越权读取未上架的 `clawhub-ops` 运维技能中的账号配置，提取到了 **3 大类核心特权资产**：

### 1. 5 个 ClawHub 官方/核心维护者账号的完整 API Token
* **账号与 Token 列表**：
  1. `nemovideonemo` : `clh_9qw68rqb2ndCMmsz5OMLJpCizOvq5vZuneP0B8BlOFI`
  2. `imo14reifey` : `clh_VcH3CLusIVf1YPqGF9Lxf6T9vYAE3RF7X-Vg3DS9xlY`
  3. `udnerc` : `clh_tvsdw49PleWLjFuqZpN2x3rvzvXzUbCUtHtAHO1xFWo`
  4. `siddylcon` : `clh_CuI52y2NCV5qWBCdksJ4_qt_8uhHIj-UAj6ta1VEXAk`
  5. `peandrover` : `clh_drweX0o_ncYQFOQISEsqOFstZDEHAzBqI7gnxd532Jk`

### 2. 1 个 GitHub 个人访问令牌（PAT / Personal Access Token）
* **Token 内容**：`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`
* **所属开发者**：`imo14reifey`（ClawHub 核心底层开发者）

### 3. 5 组代理通道（Proxy）鉴权账密
* **代理凭据**：`nkwegceg:xl7g87qhmywm`（共享代理集群鉴权密码）

---

## 三、泄露的这些东西是干嘛的？（通俗解释）

1. **ClawHub API Token (`clh_*`) 是干嘛的？**
   * **背景**：`ClawHub` 是类似 npm（Node.js 包仓库）或 PyPI（Python 包仓库）的**技能与扩展包官方注册表**。Skillhub 平台的技能市场底层直接从 ClawHub 拉取包和版本。
   * **作用**：这是维护者在命令行发布技能（`clawhub publish`）、修改版本代码和管理包权限的“身份护照”。有了它，无需账号密码和短信验证，直接拥有对应账号的全部发布和覆盖权限。

2. **GitHub PAT (`ghp_*`) 是干嘛的？**
   * **作用**：GitHub 的个人访问令牌相当于拥有者在 GitHub 上的 API 钥匙。可以绕过网页端登录和 2FA，直接克隆私有仓库、推送恶意 Commit 代码、创建 Release 发布物等。

3. **代理凭证是干嘛的？**
   * **作用**：运维系统用于避免多账号操作被目标平台限速或风控的专用代理通道。

---

## 四、核心实战危害分析（攻击者能拿它做什么？）

```
                          ┌── ① 劫持 ClawHub 官方账号发布恶意版本/后门技能
                          │        ↓
[持有 v017 泄露凭据] ─────┼── ② 全网用户与企业加载该技能，沙箱/终端自动执行恶意代码（全盘沦陷）
                          │        ↓
                          └── ③ 控制 GitHub 源码仓库，篡改 openclaw-skills 底层核心逻辑
```

### 1. 致命危害：全平台“软件供应链投毒”与后门下发（Supply Chain Attack）
* **攻击场景**：
  攻击者使用这 5 个官方维护者的 Token，可以直接执行命令行更新热门或官方基础技能包（如常用工具、网络库、基础环境组件），并在其 `SKILL.md` 或脚本代码中**植入反弹 Shell、内存马或窃密指令**。
* **连锁反应**：
  因为这些账号是官方认可的高信任维护者，恶意技能会瞬间同步到全平台的技能库。所有平台用户、企业租户在沙箱中正常使用这些技能时，**会在毫秒级别内将整个沙箱环境的控制权拱手让给攻击者**。

### 2. 开发者 GitHub 私有仓库控制与核心源码窃取
* **攻击场景**：
  攻击者利用 `imo14reifey` 的 GitHub PAT，可直接访问其名下的所有私有 GitHub 仓库（如 `openclaw-skills` 等底层框架）：
  * **代码审计与 0day 挖掘**：获取未公开的核心源代码，挖掘更多未知的架构缺陷；
  * **源代码污染**：向源码主分支直接注入隐蔽后门逻辑，随下一次版本发布直接分发到生产环境。

### 3. 利用特权代理池进行隐蔽渗透与风控绕过
* **攻击场景**：
  攻击者可使用随包泄露的代理集群作为流量中继，隐藏真实攻击来源，绕过平台与云厂商的安全风控（WAF / IP 限速）。

---

## 五、修复与整改建议

1. **紧急撤销与吊销所有 Token**：
   * 立即在 ClawHub 注册表服务端吊销上述 5 个 `clh_*` API Token；
   * 立即在 GitHub 账号设置中 Revoke 掉该 `ghp_*` 令牌；
   * 修改代理服务器的访问账密。
2. **供应链完整性排查**：
   * 立即回溯并核对这 5 个维护者账号最近发布的所有版本与提交记录，确认是否已有未授权的异常发布物。
3. **禁止敏感配置入库与分发**：
   * 严禁将含有内部账户密钥的 `accounts.md` 或配置文件打包进技能资源中。
