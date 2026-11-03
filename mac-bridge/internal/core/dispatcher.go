package core

import (
	"context"
	"encoding/base64"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"axiom/mac-bridge/internal/envresolve"
	"axiom/mac-bridge/internal/proto"
	"axiom/mac-bridge/internal/pull"
)

// Dispatcher 把链路上的请求落到本机执行。它实现 link.Handler。
//
// 刻意不含任何"智能"：不判断命令该不该跑、不解析命令含义。唯一的判断是
// 那个人工开关（Enabled）和一条廉价的防手滑黑名单。
type Dispatcher struct {
	enabled   atomic.Bool
	deviceID  string
	appVer    string
	goos      string
	arch      string
	hostname  string

	envOnce sync.Once
	env     []string

	// 在途请求，供 cancel 定位
	inflight sync.Map // id -> context.CancelFunc
}

func NewDispatcher(deviceID, appVer, goos, arch, hostname string) *Dispatcher {
	d := &Dispatcher{
		deviceID: deviceID,
		appVer:   appVer,
		goos:     goos,
		arch:     arch,
		hostname: hostname,
	}
	return d
}

// SetEnabled 是那个"人工开关"。关掉之后新请求一律 E_DISABLED，
// 且 link 层不再重连。
func (d *Dispatcher) SetEnabled(on bool) { d.enabled.Store(on) }
func (d *Dispatcher) Enabled() bool      { return d.enabled.Load() }

func (d *Dispatcher) Identity() (string, string, string, string, string) {
	return d.deviceID, d.appVer, d.goos, d.arch, d.hostname
}

func (d *Dispatcher) environment() []string {
	d.envOnce.Do(func() {
		d.env = envresolve.Resolve()
	})
	return d.env
}

// Exec 跑一条命令。
func (d *Dispatcher) Exec(ctx context.Context, req proto.ExecRequest) (proto.Result, error) {
	if !d.Enabled() {
		return proto.Result{}, errDisabled
	}
	timeout := DefaultTimeout
	if req.TimeoutMS > 0 {
		timeout = time.Duration(req.TimeoutMS) * time.Millisecond
	}

	runCtx, cancel := context.WithCancel(ctx)
	d.inflight.Store(req.SessionID, cancel)
	defer d.inflight.Delete(req.SessionID)

	res, err := Run(runCtx, ExecRequest{
		Command:        req.Command,
		CWD:            req.CWD,
		Timeout:        timeout,
		MaxOutputBytes: req.MaxOutputBytes,
	}, d.environment())
	protoRes := proto.Result{
		OK:             res.OK,
		Stdout:         res.Stdout,
		Stderr:         res.Stderr,
		ExitCode:       res.ExitCode,
		TruncatedBytes: res.TruncatedBytes,
		DurationMS:     res.DurationMS,
		Rewritten:      res.Rewritten,
	}
	if err != nil && res.ExitCode == -1 && !res.OK {
		// Run 已经把超时/被杀写进 stderr 了，这里只管把 err 翻译上层用
		return protoRes, err
	}
	return protoRes, nil
}

// Pull 拉一段文件内容，一次性回传。
//
// v1 不做流式：真实用途是把 REA/Ghidra 的结论 JSON 拿回来，几 MB 以内，
// 一个 result 帧装得下。真要拉大文件（asar、pcap）时再上分片。
func (d *Dispatcher) Pull(ctx context.Context, req proto.PullRequest) (proto.Result, error) {
	if !d.Enabled() {
		return proto.Result{}, errDisabled
	}
	// 单帧上限 1MiB，base64 后约 750KB 原始；留点余量给信封。
	const maxInline = 700 << 10
	maxBytes := req.MaxBytes
	if maxBytes <= 0 || maxBytes > maxInline {
		maxBytes = maxInline
	}

	var buf []byte
	size, sha, err := pull.Stream(req.Path, req.Offset, maxBytes, func(c pull.Chunk) error {
		buf = append(buf, c.Data...)
		return nil
	})
	if err != nil {
		return proto.Result{}, err
	}
	return proto.Result{
		OK:         true,
		ContentB64: base64.StdEncoding.EncodeToString(buf),
		Size:       size,
		SHA256:     sha,
		Stdout:     fmt.Sprintf("read %d bytes from %s (file size %d)", len(buf), req.Path, size),
	}, nil
}

// Cancel 中止在途请求。当前每条命令独立进程，取消就是取消那个进程组的上下文。
func (d *Dispatcher) Cancel(id string, reason string) {
	if v, ok := d.inflight.Load(id); ok {
		if cancel, ok := v.(context.CancelFunc); ok {
			cancel()
		}
	}
}

// errDisabled 开关关闭。
var errDisabled = &disabledError{}

type disabledError struct{}

func (*disabledError) Error() string { return "switch is off" }
