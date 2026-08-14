# Bootstrap 子提示词：App

本文件只补充 Android／iOS App 工作流，所有通用边界、技能、产物与完成合同以主 `bootstrap.md` 为准。

## 唯一目标身份绑定

目标类型填写 `App`。优先用包名或 Bundle ID 锁定单一目标，并以名称、平台、版本、安装包哈希、来源或安装实例补充证据。设备上的其它 App、安装包、模拟器历史目录和无关联流量不得并入；身份不足时只记录阻塞，不选择最相似 App。

## App 工作流

按顺序执行。主合同要求的 **API 能力树** 对 RN / UniApp / Flutter 内嵌 H5 / 原生 HTTP 封装同样强制。

1. 从已确认 APK/IPA、安装实例和应用材料枚举 `.js`、`.jsbundle`、Hermes/React Native/UniApp 脚本、H5/WebView JS、动态 chunk、Source Map、配置和资源前缀，并沿 `import`、`require`、路由表和加载函数递归补全。业务 JS bundle 必须落盘再提取。
2. 存在登录后工作台、个人中心、订单、投诉、申诉、审批、管理或特定角色页面时，安全恢复当前包名/Bundle ID 的会话，收集认证后资源、WebView JS、H5 chunk 和接口配置；不能只分析安装包或未登录页面。
3. 从 JS、Source Map、路由和配置抽出能力树，而不是只抄字符串：
   - 前缀 / baseURL / 网关 Host（含多环境配置）。
   - 路径拼装函数及其全部调用点（详情、取消、分享、上传、推送回调）。
   - 原生侧若出现 Retrofit/OkHttp/Alamofire 接口表或 proto，按同样粒度展开，并与 JS 树对齐去重。
   - Deep Link、URL Scheme、Universal Link、App Link 视为**无登录 / 弱门禁入口**，记清参数槽。
   - 区分：界面可见、代码可达但当前角色看不到、Deep Link / 推送 / 分享令牌入口。
4. 参数槽写来源：Intent extras、路由 params、本地存储、**列表响应 id 链式读取**、剪贴板/分享口令。给出本用户基线 ID。
5. 当前阶段具备代理/MCP 能力时，使用包名、进程、安装实例、页面操作和会话把真实流量与 JS 模块关联；其它 App 或无身份关联的历史流量不得纳入。流量核对能力树，不能代替未完成的静态展开。
6. 鉴权矩阵至少三行：完整会话；去掉自定义业务头或 token；一个不属于自己的对象 id 或无登录 Deep Link / 预签名 URL。只读，禁止对他人对象写入或触发。
7. JS-first 后再整理权限、导出组件、Activity/Service/Receiver/Provider、Universal Link、WebView 配置、存储、证书和安全配置，不得用这些内容取代能力树。指纹、Debuggable、缺 Certificate Pinning 单独不成高危，最多进线索。

## 产物补充字段

- 资产清单补充平台、名称、包名/Bundle ID、版本、安装包/实例、包内与 WebView JS、动态模块、组件、权限、Deep Link、存储、证书、关联 Host、**API 能力树**（含拼装函数 / 子路径 / 参数来源）、本用户基线 ID、鉴权矩阵和认证前后覆盖。
- 初始线索补充：整树对象级鉴权缺口、列表回显密钥、Deep Link / 分享令牌无登录、导出组件。同一底座的读写触发保持一条线索。
- 已确认问题必须由当前 App 的运行时、真实请求、可访问组件、完整调用链或等价证据支撑。Bootstrap 默认不打写入型验证。
