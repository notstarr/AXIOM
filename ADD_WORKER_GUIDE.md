# 接入新 Worker 类型指南

以接入 OpenCode 为例，说明如何给 xrayAI 添加一个新的 Worker 驱动。

## 架构概览

```
dispatcher/workers/
├── base.py          ← WorkerDriver ABC（接口契约）
├── registry.py      ← DRIVERS 注册表
└── adapters/
    ├── pi.py        ← PiDriver（现有实现，参考模板）
    └── opencode.py  ← 新增：OpenCodeDriver
```

## 需要改动的文件（6 个）

### 1. config.py — 类型声明

```python
# 改 WorkerType 联合类型
WorkerType = Literal["pi", "opencode"]

# 加 WORKER_ENV_KEYS
WORKER_ENV_KEYS: dict[WorkerType, tuple[str, ...]] = {
    "pi": ("PI_MODEL", "PI_BASE_URL", "PI_API_KEY", "PI_PROVIDER_API"),
    "opencode": ("OC_MODEL", "OC_BASE_URL", "OC_API_KEY"),
}
```

### 2. registry.py — 注册驱动

```python
from cairn.dispatcher.workers.adapters.opencode import OpenCodeDriver

DRIVERS: dict[str, WorkerDriver] = {
    "pi": PiDriver(),
    "opencode": OpenCodeDriver(),  # 新增
}
```

### 3. adapters/opencode.py — 驱动实现

继承 `WorkerDriver`，实现 4 个抽象方法：

```python
class OpenCodeDriver(WorkerDriver):
    type_name = "opencode"

    def build_healthcheck(self, worker, phase="") -> list[str]:
        # 返回 ["opencode", "healthcheck"] 或类似命令
        ...
    
    def build_execute(self, worker, prompt, session, phase="") -> DriverResult:
        # 返回 DriverResult(argv=[...], session=...)
        ...
    
    def build_conclude(self, worker, prompt, session, phase="") -> list[str]:
        # 用 --resume <session> 继续已有会话
        ...
    
    # 可选覆盖：
    def extract_response_text(self, stdout, stderr) -> str:
        # 从 stdout 提取 LLM 的最终文本输出
        ...
    
    def extract_session(self, session, stdout, stderr) -> str | None:
        # 从 stdout 提取 session ID
        ...
    
    def supports_conclude(self) -> bool:
        # OpenCode 是否支持 session resume
        return True
```

### 4. models.py — 前端表单模型

```python
class DispatchWorkerSettings(BaseModel):
    # 已有字段不变，前端按 worker_type 动态显示对应的 env 表单
    ...
    pi_model: str = ""          # pi 的
    pi_base_url: str = ""
    pi_api_key: str = ""
    oc_model: str = ""          # opencode 的新增
    oc_base_url: str = ""
    oc_api_key: str = ""
```

### 5. settings.py — 保存配置

```python
# resolved_workers 构建中，按 type 分支：
if worker.get("type") == "opencode":
    env = {
        "OC_MODEL": oc_model,
        "OC_BASE_URL": oc_base_url,
        "OC_API_KEY": oc_key,
    }
```

### 6. index.html — 前端配置 UI

在 dispatch 设置页的 worker 配置卡片中，按 `worker_type` 切换显示 pi/opencode 的表单字段。

## OpenCode 与 pi 的关键差异

| 维度 | pi | OpenCode（假设） |
|------|-----|------------------|
| CLI 启动 | `pi -p "prompt" --session xxx` | `opencode --prompt "..." --resume xxx` |
| 输出格式 | NDJSON 事件流到 stdout | 取决于实现（可能是 JSON 或纯文本） |
| Session | `--session-dir` / `--session` | 取决于实现 |
| 工具配置 | `--tools` 白名单 | 取决于实现 |
| Provider 配置 | `$PI_CODING_AGENT_DIR/models.json` | 可能用环境变量或 config 文件 |

**核心原则**：只要新工具有 CLI 接口、能接受 prompt、能返回文本、能管理 session，就可以接入。不需要它支持 MCP 或 skill 或任何高级特性——Driver 只负责拼命令行和解析输出。

## 实现步骤

### Step 1: 研究 OpenCode CLI

```bash
opencode --help
opencode --prompt "say hi" --model gpt-4o
```

搞清楚：
- 怎么传 prompt？(`--prompt`? `-p`? 还是 stdin?)
- 怎么传 model？(`--model`?)
- 怎么传 API key？（环境变量 `OPENAI_API_KEY`? 还是 `--api-key`?)
- 怎么管理 session？（有没有 `--session` / `--resume`?)
- 输出格式？（NDJSON? JSON? 纯文本?)
- 怎么关掉交互模式？（pi 用 `-p`，opencode 可能用 `--non-interactive`)

### Step 2: 实现 OpenCodeDriver

参照 [pi.py](cairn/src/cairn/dispatcher/workers/adapters/pi.py)，核心要实现的：

```
build_healthcheck  → 返回 opencode CLI 命令（最简单的 prompt）
build_execute      → 返回完整命令行（model/prompt/session）
build_conclude     → 返回 --resume <session> + conclude prompt
extract_response_text → 从 stdout 提取文本（可能很简单，也可能需要解析）
extract_session    → 从 stdout 或 session 文件提取 ID
```

### Step 3: 注册 & 测试

```bash
# 验证导入
python -c "from cairn.dispatcher.workers.registry import get_driver; print(get_driver('opencode'))"

# 手动测试命令行
opencode --prompt "Reply exactly: pong" ...
```

### Step 4: 前端配置

在 dispatch 设置页的 worker 卡片中，按 worker type 显示对应的配置项（model/base_url/api_key），跟 pi 的配置模式一致。

## PiDriver 实现的参考要点

以下是 pi.py 中值得参考的关键模式：

### 模型配置注入（`_wrap_with_models`）

pi 通过 shell wrapper 把 models.json 写入临时目录。OpenCode 如果支持环境变量或 config 文件，可以直接用 env 注入，不需要 shell wrapper。

```python
def build_execute(self, worker, prompt, session, phase=""):
    env = worker.env
    return DriverResult(
        argv=[
            "opencode",
            "--model", env["OC_MODEL"],
            "--prompt", prompt,
            "--session", session,
        ],
        session=session,
    )
```

### 输出提取（`extract_response_text`）

pi 的输出是 NDJSON 事件流，需要解析 event → 找 assistant text。OpenCode 可能更简单——直接输出纯文本或 JSON。

```python
def extract_response_text(self, stdout, stderr):
    # 最简单的情况：stdout 就是模型输出
    return stdout.strip()
```

### Session 恢复（`build_conclude`）

超时后拿 session ID，启动新进程继续：

```python
def build_conclude(self, worker, prompt, session, phase=""):
    return [
        "opencode",
        "--model", worker.env["OC_MODEL"],
        "--resume", session,
        "--prompt", prompt,
    ]
```

## 验证清单

- [ ] `get_driver("opencode")` 不报错
- [ ] healthcheck 能跑通（`opencode --prompt "pong"` 返回正常）
- [ ] bootstrap 项目能跑通（模型收到 prompt，返回 JSON）
- [ ] 超时后 conclude 能跑通（session 恢复成功）
- [ ] 前端能配置 opencode worker（表单字段正常）
- [ ] dispatch 日志没有报错
