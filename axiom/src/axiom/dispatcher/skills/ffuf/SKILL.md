---
name: ffuf
description: 当需要在已明确授权的网络边界内使用 FFUF 对现有字典执行 HTTP 批量 Fuzz 时使用；覆盖 URL 路径、Query 参数名或值、Header、Cookie、Form、JSON Body 和原始 HTTP 请求。该 Skill 是 FFUF 命令说明书，并默认要求将预计较长的扫描交给附件脚本后台运行，返回运行 ID 后继续其它工作；不生成或改写字典，也不负责决定 Fuzz 方法论。
requirements:
  commands:
    - name: ffuf
      args: [-V]
      contains: ffuf version
---

# FFUF 使用说明

本文包含 FFUF 的核心命令和后台运行协议；后台进程、日志、结果和停止操作由本 Skill 的附件脚本统一处理。

完整的 Web-Fuzzing-Box 字典库已原样放在 `references/Web-Fuzzing-Box/`。使用字典前，必须先通过 `axiom skill path ffuf` 定位本 Skill，再用 `read` 阅读 `references/Web-Fuzzing-Box/README_CN.md`（英文版为同目录 `README.md`），根据 README 的目录说明选择原始字典；不要默认遍历、读取或全量执行整个弹药库。

## 1. 定位与边界

FFUF 会把字典中的每一项替换到 HTTP 请求中的关键字位置，批量发出请求，并按状态码、响应大小、字数、行数、正则或响应时间筛选差异。

使用前必须确认：

1. 目标属于当前任务的授权范围和有效 Scope；
2. 有一条已经验证可用的基础请求；
3. 已确定要替换的关键字位置；
4. 已先阅读 Web-Fuzzing-Box 的 README，并从对应目录选择了与当前测试位置匹配的原始字典。

本 Skill 只说明 FFUF 的真实使用方式：

- 不生成、扩充、下载或改写上游字典；选择前必须先阅读字典库 README；
- 不替代 Reason 决定测试对象、业务流程或 Fuzz 方法论；
- 不把状态码、长度、时间或正则差异直接认定为漏洞；
- 命中结果必须回到原始请求复现，并结合身份、权限、对象和业务影响验证。

密码或登录参数测试只能在明确授权、可控账号和可接受速率下进行；优先使用测试账号，设置短时长和低速率，避免账号锁定、验证码消耗和服务降级。不得通过持续重试绕过限速或防护。

## 2. AXIOM 默认运行协议：长任务交给附件脚本后台运行

FFUF 的短命令可以直接用 `bash` 前台执行，但以下情况**默认必须使用附件脚本后台运行**：

- 预计超过约 30 秒；
- 字典较大、递归扫描或 `clusterbomb`；
- 登录、密码或参数批量测试；
- 需要模型在扫描期间处理其它更重要的验证工作。

先动态定位本 Skill 的受控目录：

```bash
FFUF_SKILL_DIR="$(axiom skill path ffuf)"
FFUF_RUNNER="$FFUF_SKILL_DIR/scripts/ffuf_background.sh"
FFUF_DICTIONARY_ROOT="$FFUF_SKILL_DIR/references/Web-Fuzzing-Box"
```

### 启动：参数尽量保持原生 FFUF 写法

使用附件脚本的 `start` 子命令。`--` 后面的参数会按原样传给 `ffuf`，包括重复参数、Header、Cookie、JSON Body、原始请求文件和代理参数；不要把它们改写成另一套 AXIOM 参数格式。

```bash
bash "$FFUF_RUNNER" start --name api-params -- \
  -w 'evidence/dict/params.txt:PARAM' \
  -u 'https://authorized.example/api?PARAM=value' \
  -mc all -fc 404 \
  -t 5 -rate 10 -timeout 10 -maxtime 300
```

脚本自动负责：

- 使用当前 `AXIOM_RUN_DIR` 和任务 Workspace；
- 创建 `evidence/ffuf/<run_id>/`；
- 执行 `nohup` 和后台符号 `&`；
- 记录 PID、启动/结束时间和退出码；
- 把日志保存为 `run.log`；
- 把结构化结果固定保存为 `results.json`；
- 返回 `run_id`、PID、日志路径和结果路径。

启动输出中的 `run_id` 必须记录下来。不要自行拼接 `nohup`、`&`、重定向、PID 文件或 `AXIOM_*` 环境变量。

脚本统一管理 FFUF 的输出参数，因此 `--` 后**不要自行传入**：

```text
-o / --output
-of / --output-format
-od / --output-dir
-debug-log / --debug-log
```

其它原生 FFUF 参数可以正常使用。不要在命令参数中写入长期凭证；需要认证时优先从当前任务已披露的材料读取，并使用短期测试凭证。

### 查询运行状态、结果和停止进程

查询某个后台运行：

```bash
bash "$FFUF_RUNNER" status '<run_id>'
```

查询结果和最近日志：

```bash
bash "$FFUF_RUNNER" output '<run_id>' --lines 80
```

输出状态可能是：

```text
running
completed
failed
stopped
unknown
```

完整结果始终保存在 `result_file` 指向的 `results.json` 中；脚本为了避免撑爆模型上下文，最多直接输出约 1 MiB 的 JSON。结果较大时使用 `read` 分段读取文件，不要重复启动扫描。

停止某个后台运行：

```bash
bash "$FFUF_RUNNER" stop '<run_id>'
```

查看当前任务已有的 FFUF 后台运行：

```bash
bash "$FFUF_RUNNER" list
```

任务暂停、归档或删除前，先停止属于当前任务的后台运行。后台脚本不会自动唤醒模型；模型应在后续工作轮次主动调用 `status` 或 `output`。

### 结果判定

只有进程结束且 `results.json` 可读时，才把结果作为候选证据。FFUF 命中仍须用原始请求低频复现，并结合身份、权限、对象和业务影响验证；状态码、长度、时间或正则差异本身不是漏洞结论。

## 常见问题排查

- `ffuf` 不存在或 `-V` 失败：停止并记录，不要猜测安装路径。
- 附件脚本启动失败：先检查 `axiom skill path ffuf`、脚本路径和 `command -v ffuf`。
- 没有 `FUZZ` 或自定义关键字：检查 URL、Header、Cookie、Form、JSON 或原始请求文件。
- 全部响应相同：先用 `-mc all` 查看状态码、大小、字数和行数，再逐步添加 Filter。
- 出现大量 `403`、`429`、超时或连接错误：立即降低 `-rate`、`-t` 或停止，不能靠重试绕过限制。
- `status` 显示 `unknown`：进程可能被 Worker/Lease 强制清理，先检查 `run.log`，不要把它当作完成。
- 进程结束但没有 JSON：先看 `run.log`；没有命中不等于 FFUF 失败，确认输出路径和参数后再判断。

## 3. 安装与路径检查

```bash
command -v ffuf
ffuf -V
```

- 始终使用 `ffuf`，由 AXIOM Runner 通过 `PATH` 解析真实路径。
- 不硬编码 `/usr/local/bin`、Homebrew 或其它机器路径。
- 不使用 `find /` 搜索二进制或猜测安装位置。
- `ffuf -V` 不可用时，记录工具不可用并停止，不要改用不明来源的二进制。

## 4. 基本命令结构

最小形式：

```bash
ffuf -w <dictionary> -u '<url-containing-FUZZ>'
```

常用参数：

```text
-w <file>[:KEYWORD]    字典及其替换关键字
-u <url>               URL 模板
-request <file>        原始 HTTP 请求文件
-request-proto http|https  原始请求使用的协议
-X <method>            HTTP 方法
-H 'Name: VALUE'       Header 模板
-b 'name=VALUE'        Cookie 模板
-d 'body=VALUE'        Form 或原始 Body 模板
-mode clusterbomb|pitchfork|sniper  多关键字组合模式
-e .php,.json          为路径关键字追加扩展名
-enc 'KEY:urlencode'    对关键字做编码
-x <proxy>             全部请求经代理
-replay-proxy <proxy>  仅重放命中请求
-mc/-ms/-mw/-ml/-mr/-mt  Matcher
-fc/-fs/-fw/-fl/-fr/-ft  Filter
-t <n>                 并发线程数
-rate <n>              每秒请求数
-timeout <sec>         单请求超时
-maxtime <sec>         本次 FFUF 最长运行时间
-noninteractive        关闭交互控制台
-of json -o <file>     保存结构化 JSON 结果
```

先用小字典或少量样本验证基础请求和关键字，再扩大规模。不要为了掩盖请求量估算错误而提高并发。

## 5. FUZZ 关键字与字典

默认关键字是 `FUZZ`：

```bash
ffuf -w words.txt -u 'https://example.com/FUZZ'
```

自定义关键字适合多个注入位置：

```bash
ffuf -w values.txt:VALUE -u 'https://example.com/api/items?id=VALUE'
```

多个字典必须明确每个关键字：

```bash
ffuf -w names.txt:PARAM -w values.txt:VALUE \
  -u 'https://example.com/api?PARAM=VALUE' \
  -mode clusterbomb
```

关键字使用全大写且前后一致，例如 `PATH`、`PARAM`、`VALUE`、`HEADER`。包含 `&` 的 URL 必须加引号，避免被 Shell 当成后台操作符。

字典内容保持 Web-Fuzzing-Box 上游原样；根据 README 和当前测试位置选择文件，并在运行记录中保存字典路径和规模。

## 6. 注入位置

### URL 路径

```bash
ffuf -w paths.txt:PATH -u 'https://example.com/PATH' \
  -mc all -fc 404 -t 5 -rate 10 -timeout 10 \
  -maxtime 300 -noninteractive
```

递归探测只把关键字放在 URL 末尾，并设置有限递归深度：

```bash
ffuf -w paths.txt -u 'https://example.com/FUZZ' \
  -recursion -recursion-depth 2 -mc all -fc 404 \
  -t 5 -rate 10 -timeout 10 -maxtime 300 -noninteractive
```

### Query 参数名或值

```bash
# 参数值
ffuf -w values.txt:VALUE -u 'https://example.com/api/items?id=VALUE' \
  -mc all -fc 404 -t 5 -rate 10 -timeout 10 \
  -maxtime 300 -noninteractive

# 参数名
ffuf -w names.txt:PARAM -u 'https://example.com/api/items?PARAM=test' \
  -mc all -t 5 -rate 10 -timeout 10 -maxtime 300 -noninteractive
```

### Header、Cookie、Form 和 JSON Body

```bash
# Header
ffuf -w values.txt:VALUE -u 'https://example.com/api/profile' \
  -H 'X-Test-Value: VALUE' -mc all -t 5 -rate 10 \
  -timeout 10 -maxtime 300 -noninteractive

# Cookie
ffuf -w values.txt:VALUE -u 'https://example.com/account' \
  -b 'session=VALUE' -mc all -t 5 -rate 5 \
  -timeout 10 -maxtime 300 -noninteractive

# Form
ffuf -w values.txt:VALUE -u 'https://example.com/login' -X POST \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'username=test&password=VALUE' -mc all -t 2 -rate 2 \
  -timeout 10 -maxtime 180 -noninteractive

# JSON
ffuf -w values.txt:VALUE -u 'https://example.com/api/items' -X POST \
  -H 'Content-Type: application/json' -d '{"id":"VALUE"}' \
  -mc all -t 5 -rate 10 -timeout 10 -maxtime 300 -noninteractive
```

登录或密码参数测试必须使用授权的测试账号、低速率和短时长，并提前确认锁定、验证码和告警策略；不要把登录页统一响应直接当作成功。

### 原始 HTTP 请求

```bash
ffuf -w values.txt:VALUE -request request.txt \
  -request-proto https -mc all -t 5 -rate 10 \
  -timeout 10 -maxtime 300 -noninteractive
```

请求文件应包含正确的请求行、`Host`、Headers、空行和 Body；不要把 curl 命令当作原始请求文件。

## 7. 多关键字、编码和扩展名

`clusterbomb` 是字典长度的乘积，必须先估算请求总量；`pitchfork` 按行配对，要求字典顺序和长度已对齐；`sniper` 逐个替换关键字：

```bash
ffuf -w names.txt:PARAM -w values.txt:VALUE \
  -u 'https://example.com/api?PARAM=VALUE' \
  -mode clusterbomb -mc all -maxtime 300 -noninteractive
```

只有目标协议明确要求时才编码：

```bash
ffuf -w values.txt:VALUE -u 'https://example.com/search?q=VALUE' \
  -enc 'VALUE:urlencode' -mc all -maxtime 300 -noninteractive
```

路径扩展名可用 `-e .php,.json`，但只在目标技术栈和授权范围有依据时使用。

## 8. Matcher、Filter 与自动校准

Matcher 决定保留哪些响应：

```text
-mc 状态码  -ms 字节数  -mw 单词数  -ml 行数  -mr 正则  -mt 首字节时间
```

Filter 决定排除哪些响应：

```text
-fc 状态码  -fs 字节数  -fw 单词数  -fl 行数  -fr 正则  -ft 首字节时间
```

先用 `-mc all` 观察基线分布，再逐步添加 Filter：

```bash
ffuf -w paths.txt -u 'https://example.com/FUZZ' \
  -mc all -fc 404 -fs 1234 -t 5 -rate 10 \
  -timeout 10 -maxtime 300 -noninteractive
```

`-ac` 会自动校准过滤条件，但动态页面、统一错误页和鉴权网关可能造成误过滤；必须检查校准请求和结果分布。

## 9. 代理、输出与结果解释

全部请求经代理：

```bash
ffuf -w values.txt:VALUE -u 'https://example.com/api?id=VALUE' \
  -x 'http://127.0.0.1:8080' -mc all -maxtime 300 -noninteractive
```

只重放命中请求：

```bash
ffuf -w values.txt:VALUE -u 'https://example.com/api?id=VALUE' \
  -replay-proxy 'http://127.0.0.1:8080' -mc all \
  -maxtime 300 -noninteractive
```

推荐保存 JSON：

```bash
ffuf -w values.txt:VALUE -u 'https://example.com/api?id=VALUE' \
  -mc all -fc 404 -of json -o results.json \
  -t 5 -rate 10 -timeout 10 -maxtime 300 -noninteractive
```

`-json` 将结果逐行输出；`-or` 在无命中时不创建结果文件；`-od` 保存命中响应。结果必须与基础请求、关键字、字典路径、Matcher/Filter、速率和执行时间一并保存。FFUF 命中只代表响应差异，不代表漏洞；必须复现并验证业务影响。

## 10. 完成前检查

- 运行目录、原生 FFUF 参数、PID、日志、状态和 JSON 结果都已由附件脚本保存；
- 若仍在运行，已明确记录 `running/pending`，没有伪造完成结果；
- 已检查 429、403、超时、连接错误和认证状态；
- 已对候选命中做原始请求复现；
- 没有把未验证差异写入漏洞结论；
- 下一轮可以根据运行目录恢复检查，而不会重复启动同一个扫描。
