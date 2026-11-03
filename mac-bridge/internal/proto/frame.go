// Package proto 定义 Mac Bridge 与 AXIOM 网关之间的线上帧。
//
// 一条 WebSocket 连接上传 JSON 文本帧，统一带一个信封：
//
//	{"v":1,"id":"...","type":"exec","ts":1700000000,"payload":{...}}
//
// 请求方填 id，设备侧回帧沿用同一个 id，这样一次请求只对应一个终帧
// （result 或 error），中途的内容走 chunk 帧。id 为空的帧是通知类
// （hello / ping / pong / bye / chunk），不期待答复。
package proto

import "encoding/json"

const Version = 1

// 硬限额。超过直接拒绝，不做静默截断——静默截断会让模型看到
// 看似完整其实缺了尾巴的结果。
const (
	MaxFrameBytes  = 1 << 20 // 1 MiB，单帧上限
	MaxChunkBytes  = 256 << 10 // 256 KiB，pull 分片
	MaxPullBytes   = 32 << 20 // 32 MiB，单次 pull 上限
	MaxOutBytes    = 2 << 20 // 2 MiB，单条命令单流的保留上限
	MaxQueueDepth  = 32      // 每设备待执行队列
)

// Envelope 是所有帧的外壳。
type Envelope struct {
	V       int             `json:"v"`
	ID      string          `json:"id,omitempty"`
	Type    string          `json:"type"`
	TS      int64           `json:"ts,omitempty"`
	Payload json.RawMessage `json:"payload,omitempty"`
}

// ---- 请求：服务器 → 设备 ----

// ExecRequest 跑一条命令。SessionID 留给将来的持久 PTY，v1 恒为空，
// 每条命令起独立进程。
type ExecRequest struct {
	Command        string `json:"command"`
	CWD            string `json:"cwd,omitempty"`
	TimeoutMS      int    `json:"timeout_ms,omitempty"`
	MaxOutputBytes int    `json:"max_output_bytes,omitempty"`
	SessionID      string `json:"session_id,omitempty"`
}

// PullRequest 拉一段文件内容，走 chunk 帧回传。
type PullRequest struct {
	Path     string `json:"path"`
	Offset   int64  `json:"offset,omitempty"`
	MaxBytes int64  `json:"max_bytes,omitempty"`
}

// CancelRequest 中止在途请求。
type CancelRequest struct {
	Reason string `json:"reason,omitempty"`
}

// Pong 答复设备心跳。
type Pong struct{}

// HelloOK 协商参数。
type HelloOK struct {
	HeartbeatSec int   `json:"heartbeat_sec"`
	MaxFrame     int   `json:"max_frame"`
	MaxPull      int64 `json:"max_pull"`
}

// ---- 请求：设备 → 服务器 ----

// Hello 必须是设备发出的第一帧。
type Hello struct {
	DeviceID   string `json:"device_id"`
	AppVersion string `json:"app_version"`
	OS         string `json:"os"`
	Arch       string `json:"arch"`
	Hostname   string `json:"hostname,omitempty"`
}

// Ping 心跳。
type Ping struct{}

// Bye 主动下线。reason=operator_off 时网关要禁止自动重连。
type Bye struct {
	Reason string `json:"reason"`
}

// ---- 答复：设备 → 服务器 ----

// Result 一次 exec 的终帧。非零退出码不算失败，exit_code 本身就是信息。
type Result struct {
	OK             bool   `json:"ok"`
	Stdout         string `json:"stdout"`
	Stderr         string `json:"stderr"`
	ExitCode       int    `json:"exit_code"`
	TruncatedBytes int64  `json:"truncated_bytes,omitempty"`
	DurationMS     int64  `json:"duration_ms,omitempty"`
	Rewritten      string `json:"rewritten,omitempty"` // 触发过 otool→llvm-objdump 改写时写明

	// pull 的结果（v1 一次性回传；流式走 chunk 帧，留给后续）
	ContentB64 string `json:"content_b64,omitempty"`
	Size       int64  `json:"size,omitempty"`
	SHA256     string `json:"sha256,omitempty"`
}

// ErrorFrame 一次请求的终帧，表示请求本身没跑起来。
type ErrorFrame struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

// Chunk pull 的分片。
type Chunk struct {
	Seq   int    `json:"seq"`
	Data  []byte `json:"data"` // base64
	Last  bool   `json:"last"`
	Total int64  `json:"total,omitempty"`
}

// PullDone pull 的元信息终帧。
type PullDone struct {
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256,omitempty"`
}

// ---- 错误码 ----

const (
	ErrDisabled        = "E_DISABLED"        // 开关关闭
	ErrCommandDenied   = "E_COMMAND_DENIED"   // 命中防手滑黑名单
	ErrPathDenied      = "E_PATH_DENIED"      // 路径被拒
	ErrPathNotAbsolute = "E_PATH_NOT_ABSOLUTE"
	ErrPathTraversal   = "E_PATH_TRAVERSAL"
	ErrPathNotExist    = "E_PATH_NOT_EXIST"
	ErrNotFile         = "E_PATH_NOT_FILE"
	ErrTimeout         = "E_TIMEOUT"
	ErrTooLarge        = "E_TOO_LARGE"
	ErrBadFrame        = "E_BAD_FRAME"
	ErrQueueFull       = "E_QUEUE_FULL"
	ErrInternal        = "E_INTERNAL"
)

// TypeName 返回给定 payload 的帧类型名，供日志/断言使用。
func TypeName(e Envelope) string { return e.Type }
