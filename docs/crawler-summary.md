# 爬虫系统改进总结

> 日期: 2026-06-19  
> 目标站点: https://xz.aliyun.com/news（先知社区）

---

## 一、改进历程

共经历 5 个迭代版本，每个版本解决一个核心问题：

| 版本 | 项目 | 核心改动 | 解决的问题 |
|------|------|----------|-----------|
| 原始 | pg_009 | 无（基线） | — |
| v1 | pg_010 | 去重 + 跳过 traffic | 重复 URL 13 组、unknown 页面 16 个、API 膨胀 152 条 |
| v2 | pg_011 | 恢复 traffic + AJAX 去重 | API 丢失（矫枉过正，12→13） |
| v3 | pg_012 | AJAX 签名去重 | `/news` 和 `/news?isAjax=true` 被错误合并 |
| v4 | pg_013 | 操作型 prompt | agent 只"看"页面不"操作"，操作型 API 全丢 |

---

## 二、最终效果对比

### 数据量对比

| 指标 | pg_009（原始） | pg_012（去重优化） | pg_013（操作型） |
|------|--------------|------------------|----------------|
| 时长 | 40min | 40min | 30min |
| accepted 轮次 | 25 | 21 | 7 |
| flush | 0 | 0 | 1 |
| pages | 46 | 44 | 16 |
| apis | 152 | 20 | 32 |
| maps | 58 | 38 | 14 |
| **操作型 API** | **0** | **0** | **14** |
| unknown 页面 | 16 | 8 | 0 |
| 重复 URL | 13 组 | 0 | 0 |
| 重复 API | 大量 | 0 | 0 |

### API 覆盖对比

| API 类型 | pg_009 | pg_012 | pg_013 | 网站实际 |
|----------|--------|--------|--------|---------|
| 页面加载型（GET JSON） | 8 | 12 | 10 | ~16 |
| 操作型（POST/DELETE） | 0 | 0 | 14 | ~20+ |
| 认证/跳转型（302） | 3 | 3 | 2 | 3 |
| 文件代理型 | 130+(噪声) | 1 | 1 | 1 |
| **有效 API 总数** | **~11** | **~16** | **~25** | **~40** |

### 操作型 API 明细（pg_013 新发现，pg_009/pg_012 完全没有）

| API | 方法 | 功能 |
|-----|------|------|
| /api/v2/news/{id}/collections | POST | 收藏文章 |
| /api/v2/news/{id}/collections | DELETE | 取消收藏 |
| /api/v2/news/{id}/likes | POST | 点赞 |
| /api/v2/news/{id}/likes | DELETE | 取消点赞 |
| /api/v2/news/{id}/comments | POST | 提交评论 |
| /api/v2/news/{id}/reports | POST | 举报文章 |
| /api/v2/news | POST | 发布文章 |
| /api/v2/news/preview | POST | 预览文章 |
| /api/v2/course/collect | POST | 收藏课程 |
| /api/v2/user/checkin/currency | POST | 签到 |
| /api/v2/currency/purchases/{node} | POST | 兑换商品 |
| /api/v2/feeds | POST | 发布动态 |
| /api/v2/files | POST | 文件上传 |
| /api/v2/storage | POST | 存储 |
| /points-mall/exchange | POST | 积分兑换 |
| /api/v2/like/comments/{id} | GET | 评论点赞 |

---

## 三、技术改进清单

### 1. 浏览器控制层：browser-use → Playwright CDP

| 维度 | browser-use（原始） | Playwright CDP（现在） |
|------|--------------------|-----------------------|
| 架构 | daemon + session 文件 | 无状态连接 |
| 稳定性 | 频繁冲突（Session already running） | 零冲突 |
| 点击 | JS `element.click()`（React 可能不触发） | `mouse.click()` 真实鼠标事件 |
| 表单 | 不支持 | `fill()` React 兼容 |
| 等待 | 只轮询 readyState | `networkidle` + `domcontentloaded` |
| 弹窗 | 手动处理 | `page.on("dialog")` 自动 dismiss |
| iframe/Shadow DOM | 不支持 | 原生穿透 |

### 2. 数据去重层

| 机制 | 说明 |
|------|------|
| `_normalize_url` | URL 归一化（去 fragment、排序 query、小写 host）→ 页面按 URL 去重 |
| `_normalize_api_path` | API 路径归一化（数字/UUID/模板占位符 → `{id}`）→ API 按模式去重 |
| `_api_signature` | method + normalized_path + ajax 标志 → 区分 `/news`（HTML）和 `/news?isAjax=true`（JSON） |
| mapping 去重 | 按 page_url 去重，不再为每个 traffic flow 生成独立 mapping |

### 3. Traffic 合并策略

| 场景 | 旧行为 | 新行为 |
|------|--------|--------|
| crawl accepted | 全量 merge traffic（产生 unknown 噪声） | merge traffic 但带去重（只补新 API，不重复） |
| crawl failed → flush | merge traffic | merge traffic（不变） |
| traffic fallback | 无脑新建记录 | 先查已有 URL/method+path，已存在则只补 status_codes |

### 4. 探索反馈回路

| 机制 | 说明 |
|------|------|
| `{crawler_state}` 注入 | prompt 中注入已发现页面 URL 清单 + 类型 + exhausted 状态 |
| `{round_info}` 注入 | 告知 agent 当前轮次和剩余预算 |
| frontier 提示 | 从已有页面 actions 中提取未访问链接，建议优先探索 |
| exhausted 标记 | agent 标记已穷尽页面，后续轮次跳过 |

### 5. 操作型探索（核心改进）

| 旧行为 | 新行为 |
|--------|--------|
| agent 只点链接导航到新页面 | agent 主动点功能按钮（点赞/收藏/评论/关注/兑换/签到等） |
| 只记录页面加载 API | 记录所有操作触发的 API，包括 401/403 失败的 |
| 只看 `state` 里的元素 | 用 `eval` 搜索 `[onclick]` 等隐藏交互元素 |
| 不填表单 | 用 `fill` 填搜索框/评论框并提交 |

---

## 四、优点

1. **操作型 API 发现能力从 0 到 14** — 这是渗透测试最需要的，之前完全缺失
2. **数据质量干净** — 零重复 URL、零重复 API、unknown 页面从 16 降到 0
3. **浏览器控制稳定** — Playwright 零冲突，40 分钟长时任务跑完不中断
4. **表单填写能力** — 能触发搜索 API、评论提交 API 等需要输入的操作
5. **去重粒度合理** — 按功能模式去重（`/news/{id}/comments` 一条），不按实例去重（不每篇文章记一条）
6. **AJAX 区分** — 同路径的 HTML 页面和 JSON API 不会被错误合并

## 五、不足

### 1. 页面覆盖广度下降
pg_013 只发现 16 个页面（pg_012 有 44 个）。agent 把时间花在操作按钮上，探索的新页面少了。**需要平衡深度和广度**——理想情况是先广度遍历所有页面，再深度操作每个页面的功能。

### 2. 操作型 API 仍有遗漏
网站 JS 源码里有 48 个 `/api/v2/` 端点，pg_013 只发现了 14 个操作型。缺失的包括：
- 用户关注/取消关注（`/api/v2/user/followings/`）
- 圈子发帖/加入（`/api/v2/plus-group/group-posts/`）
- 问答系统（`/api/v2/questions/`）
- 聊天/IM（`/api/v2/chats/`）
- 通知系统（`/api/v2/user/notifications`）

这些需要进入对应页面并操作才能触发，agent 没有覆盖到所有功能区域。

### 3. 登录态限制
大部分操作型 API 返回 302（跳登录）而非 401/403。agent 能记录 endpoint 但拿不到请求/响应的参数结构。如果浏览器有登录态，能捕获更完整的 API 细节（params、response body）。

### 4. flush 出现 1 次
pg_013 有 1 次 flush fallback，说明某轮 agent 超时或产出无效。操作型 prompt 更复杂，agent 花更多时间在 eval/操作上，偶尔会超时。

### 5. exhausted 标记不足
agent 仍倾向于不标记 exhausted，导致后续轮次可能重访已充分操作的页面。

### 6. API 细节仍偏薄
大部分 API 只有 `method + path + status_codes`，缺少 params schema、response_keys、auth 类型细节。agent 能发现 endpoint 但不总是仔细记录参数和响应结构。

---

## 六、建议下一步

| 优先级 | 改进 | 预期效果 |
|--------|------|----------|
| P0 | 广度+深度两阶段：前半程广度遍历所有页面，后半程回退操作每个页面的功能 | 兼顾页面覆盖和 API 发现 |
| P0 | 登录态支持：配置浏览器 cookie/token，让操作型 API 返回真实响应 | 拿到完整 params/response 结构 |
| P1 | prompt 引导覆盖更多功能区域：圈子、问答、聊天、通知 | 操作型 API 从 14 提升到 30+ |
| P1 | API 细节增强：prompt 要求记录 request params 和 response keys | 更完整的业务建模 |
| P2 | exhausted 判定逻辑强化 | 减少重访，提高效率 |
| P2 | 长时任务稳定性：排查 flush 原因，优化操作型 prompt 的 token 消耗 | 消除 flush |
