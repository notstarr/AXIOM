// Package link 负责与 AXIOM 网关之间的 WebSocket 链路。
//
// 这里只管"连上、把帧收发对上号、断线重连"，不理解命令内容。理解内容的是
// core 那一层。
//
// 连接方向刻意是**设备主动拨出**：网关不向这台 Mac 开任何端口，所以 NAT、
// 公司网、切换 Wi-Fi 都不影响。
package link

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"

	"axiom/mac-bridge/internal/proto"
)

// Handler 处理服务器下发的请求。实现方自己决定怎么执行、怎么回。
type Handler interface {
	// Exec 跑一条命令，回一个 result 帧的 payload。
	Exec(ctx context.Context, req proto.ExecRequest) (proto.Result, error)
	// Pull 拉一段文件内容，一次性回传（有上限）。
	Pull(ctx context.Context, req proto.PullRequest) (proto.Result, error)
	// Cancel 中止在途请求。
	Cancel(id string, reason string)
	// Enabled 开关状态。false 时不执行任何请求，直接回 E_DISABLED。
	Enabled() bool
	// DeviceID / AppVersion / OS / Arch 用于 hello。
	Identity() (deviceID, appVersion, os, arch, hostname string)
}

const (
	// 心跳间隔与失联判定。跟服务端约定一致：10s 一次，30s 无消息即失联。
	heartbeatInterval = 10 * time.Second
	// 断线重连退避上限
	maxBackoff = 30 * time.Second
	// 单次拨号超时
	dialTimeout = 15 * time.Second
)

// Client 一条 WebSocket 连接的封装。
type Client struct {
	serverURL string
	token     string
	insecure  bool
	handler   Handler

	conn     *websocket.Conn
	writeMu  sync.Mutex
	backoff  time.Duration
	lastPong time.Time
}

func New(serverURL, token string, insecure bool, handler Handler) *Client {
	return &Client{
		serverURL: serverURL,
		token:     token,
		insecure:  insecure,
		handler:   handler,
		backoff:   time.Second,
	}
}

// Run 阻塞运行：连上 → 收发 → 掉线 → 退避重连，直到 ctx 取消。
//
// 关键语义：**开关关掉时不重连**。那不是网络故障，是操作员的决定，
// 自动重连等于无视这个决定。
func (c *Client) Run(ctx context.Context) error {
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		if !c.handler.Enabled() {
			// 开关关着：安静等待，不要打日志刷屏，也不要拨号
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Second):
			}
			continue
		}

		err := c.session(ctx)
		if err == nil || errors.Is(err, errOperatorOff) {
			// 正常下线（bye{operator_off}）：退回等待，不重连
			c.backoff = time.Second
			continue
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		log.Printf("link: session ended: %v; retry in %s", err, c.backoff)
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(c.backoff):
		}
		c.backoff *= 2
		if c.backoff > maxBackoff {
			c.backoff = maxBackoff
		}
	}
}

var errOperatorOff = errors.New("operator turned the switch off")

func (c *Client) session(ctx context.Context) error {
	dialCtx, cancel := context.WithTimeout(ctx, dialTimeout)
	defer cancel()

	opts := &websocket.DialOptions{
		HTTPClient: &http.Client{
			Timeout: dialTimeout,
			Transport: &http.Transport{
				TLSClientConfig: &tls.Config{
					// 只在连自签证书的 wss:// 时用。生产环境应该换成指纹固定，
					// 但当前是单人自用 + 公司内网，这里先留个明确的开关。
					InsecureSkipVerify: c.insecure, //nolint:gosec // 见上
				},
			},
		},
	}
	conn, _, err := websocket.Dial(dialCtx, c.dialURL(), opts)
	if err != nil {
		return fmt.Errorf("dial: %w", err)
	}
	c.conn = conn
	defer func() {
		_ = conn.CloseNow()
		c.conn = nil
	}()

	// 服务端接收的单帧上限要跟协议一致，否则大包会被直接切断
	conn.SetReadLimit(proto.MaxFrameBytes)

	if err := c.handshake(ctx); err != nil {
		return err
	}
	c.backoff = time.Second
	c.lastPong = time.Now()

	// 读循环 + 心跳 + 失联巡检三件事并行
	readErr := make(chan error, 1)
	go func() { readErr <- c.readLoop(ctx) }()

	heartbeat := time.NewTicker(heartbeatInterval)
	defer heartbeat.Stop()
	watchdog := time.NewTicker(5 * time.Second)
	defer watchdog.Stop()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case err := <-readErr:
			return err
		case <-heartbeat.C:
			if err := c.sendFrame(map[string]any{"v": 1, "type": "ping", "ts": time.Now().Unix()}); err != nil {
				return fmt.Errorf("heartbeat: %w", err)
			}
		case <-watchdog.C:
			// macOS 合盖休眠时 socket 还是 ESTABLISHED，但进程不跑代码。
			// 不判活的话请求会一直等到自己的超时，模型以为在慢慢跑。
			if time.Since(c.lastPong) > 3*heartbeatInterval {
				return errors.New("link unresponsive: no frame for 30s")
			}
		}
	}
}

func (c *Client) dialURL() string {
	u := strings.TrimSpace(c.serverURL)
	if !strings.Contains(u, "?") {
		u += "?token=" + c.token
	} else if !strings.Contains(u, "token=") {
		u += "&token=" + c.token
	}
	return u
}

func (c *Client) handshake(ctx context.Context) error {
	id, version, goos, arch, hostname := c.handler.Identity()
	hello := map[string]any{
		"v": 1, "type": "hello", "ts": time.Now().Unix(),
		"payload": map[string]any{
			"device_id":   id,
			"app_version": version,
			"os":          goos,
			"arch":        arch,
			"hostname":    hostname,
		},
	}
	// hello 按信封发，但 payload 字段就是 Hello 结构，服务端按 type 分派
	if err := c.sendFrame(hello); err != nil {
		return fmt.Errorf("hello: %w", err)
	}
	// 等 hello_ok
	readCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	_, data, err := c.conn.Read(readCtx)
	if err != nil {
		return fmt.Errorf("waiting hello_ok: %w", err)
	}
	var env proto.Envelope
	if err := json.Unmarshal(data, &env); err != nil {
		return fmt.Errorf("hello_ok parse: %w", err)
	}
	if env.Type != "hello_ok" {
		return fmt.Errorf("expected hello_ok, got %q", env.Type)
	}
	return nil
}

func (c *Client) readLoop(ctx context.Context) error {
	for {
		_, data, err := c.conn.Read(ctx)
		if err != nil {
			return err
		}
		c.lastPong = time.Now()

		var env proto.Envelope
		if err := json.Unmarshal(data, &env); err != nil {
			log.Printf("link: undecodable frame: %v", err)
			continue
		}

		switch env.Type {
		case "pong":
			continue
		case "exec":
			var req proto.ExecRequest
			if err := json.Unmarshal(env.Payload, &req); err != nil {
				c.sendError(env.ID, proto.ErrBadFrame, err.Error())
				continue
			}
			if !c.handler.Enabled() {
				c.sendError(env.ID, proto.ErrDisabled, "switch is off")
				continue
			}
			go c.handleExec(ctx, env.ID, req)
		case "pull":
			var req proto.PullRequest
			if err := json.Unmarshal(env.Payload, &req); err != nil {
				c.sendError(env.ID, proto.ErrBadFrame, err.Error())
				continue
			}
			if !c.handler.Enabled() {
				c.sendError(env.ID, proto.ErrDisabled, "switch is off")
				continue
			}
			go c.handlePull(ctx, env.ID, req)
		case "cancel":
			c.handler.Cancel(env.ID, "server cancelled")
		default:
			log.Printf("link: unknown frame type %q", env.Type)
		}
	}
}

func (c *Client) handleExec(ctx context.Context, id string, req proto.ExecRequest) {
	res, err := c.handler.Exec(ctx, req)
	if err != nil {
		code := proto.ErrInternal
		if strings.Contains(err.Error(), "timeout") {
			code = proto.ErrTimeout
		}
		c.sendError(id, code, err.Error())
		return
	}
	_ = c.sendFrame(map[string]any{"v": 1, "id": id, "type": "result", "ts": time.Now().Unix(), "payload": res})
}

func (c *Client) handlePull(ctx context.Context, id string, req proto.PullRequest) {
	res, err := c.handler.Pull(ctx, req)
	if err != nil {
		c.sendError(id, proto.ErrInternal, err.Error())
		return
	}
	_ = c.sendFrame(map[string]any{"v": 1, "id": id, "type": "result", "ts": time.Now().Unix(), "payload": res})
}

func (c *Client) sendError(id, code, message string) {
	_ = c.sendFrame(map[string]any{
		"v": 1, "id": id, "type": "error", "ts": time.Now().Unix(),
		"payload": map[string]any{"code": code, "message": message},
	})
}

// sendFrame 统一出口。写必须串行：WebSocket 并发写会破坏帧边界。
func (c *Client) sendFrame(frame map[string]any) error {
	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	if c.conn == nil {
		return errors.New("not connected")
	}
	// 服务端的信封约定：id/type 在顶层，其余进 payload
	out := map[string]any{
		"v":    frame["v"],
		"type": frame["type"],
		"ts":   frame["ts"],
	}
	if id, ok := frame["id"]; ok {
		out["id"] = id
	}
	if p, ok := frame["payload"]; ok {
		out["payload"] = p
	}
	buf, err := json.Marshal(out)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	return c.conn.Write(ctx, websocket.MessageText, buf)
}

// Bye 主动下线，告诉服务端这是操作员关的，不是网络故障。
func (c *Client) Bye(reason string) error {
	return c.sendFrame(map[string]any{
		"v": 1, "type": "bye", "ts": time.Now().Unix(),
		"payload": map[string]any{"reason": reason},
	})
}

// Connected 当前是否有活的 WebSocket 连接。给菜单栏显示状态用。
func (c *Client) Connected() bool {
	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	return c.conn != nil
}

// SignalOff 由开关调用：先告诉服务端这是操作员关的，再断开连接。
//
// 只改本地标志是不够的——连接还开着，服务端会一直显示 online，而且在途
// 请求仍会被执行。这一步是"关"这个动作的真正落地。
func (c *Client) SignalOff(reason string) {
	_ = c.Bye(reason)
	c.writeMu.Lock()
	conn := c.conn
	c.conn = nil
	c.writeMu.Unlock()
	if conn != nil {
		_ = conn.Close(websocket.StatusNormalClosure, reason)
	}
}
