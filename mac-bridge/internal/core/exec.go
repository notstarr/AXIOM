package core

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"
)

const (
	// DefaultTimeout 是模型没指定时的默认超时。给 60s 而不是 10s，
	// 因为 rea 的一次结构化分析很容易超过 30s。
	DefaultTimeout = 60 * time.Second
	// MaxTimeout 是上限。Ghidra 反编译一个大二进制要几分钟，所以不能太短。
	MaxTimeout = 600 * time.Second
	// killGrace 是 SIGTERM 之后等多久上 SIGKILL。
	killGrace = 3 * time.Second

	// 默认 head/tail 各 128KiB。
	defaultHeadTail = 128 << 10
)

// ExecRequest 是一次命令执行的入参。
type ExecRequest struct {
	Command        string
	CWD            string
	Timeout        time.Duration
	MaxOutputBytes int // 用于推导 head/tail 尺寸，<=0 用默认
}

// ExecResult 是一次命令执行的出参。
//
// 注意 OK 的语义：非零退出码**不算失败**。退出码本身就是信息量
// （比如 codesign 找不到签名会返回 1 但 stderr 里有结论），把它翻译成
// 错误只会让模型少看到一半输出。只有"根本没跑起来/被杀"才 OK=false。
type ExecResult struct {
	OK             bool
	Stdout         string
	Stderr         string
	ExitCode       int
	TruncatedBytes int64
	DurationMS     int64
	Rewritten      string
}

// Run 执行一条命令，跑在自己的进程组里，超时后整个进程组一起收。
//
// 为什么必须 Setpgid：`rea` 这类命令会再 fork node 和 java。不打进程组的
// 话，超时杀掉的是外层 zsh，node/java 变成孤儿继续跑，下一条命令会撞上
// 锁文件或端口，现象是"莫名其妙的第二次失败"。
func Run(ctx context.Context, req ExecRequest, env []string) (ExecResult, error) {
	if err := CheckCommand(req.Command); err != nil {
		return ExecResult{OK: false, ExitCode: -1}, err
	}

	timeout := req.Timeout
	if timeout <= 0 {
		timeout = DefaultTimeout
	}
	if timeout > MaxTimeout {
		timeout = MaxTimeout
	}

	cwd := req.CWD
	if cwd == "" {
		home, err := osUserHome()
		if err != nil {
			return ExecResult{OK: false, ExitCode: -1}, err
		}
		cwd = home
	}
	if abs, err := filepath.Abs(cwd); err == nil {
		cwd = abs
	}
	if !isDir(cwd) {
		return ExecResult{OK: false, ExitCode: -1}, fmt.Errorf("cwd does not exist: %s", cwd)
	}

	head, tail := headTailSizes(req.MaxOutputBytes)
	out := NewHeadTail(head, tail)
	errb := NewHeadTail(head, tail)

	// 走 login shell，让 ~/.zshrc 里的 PATH / JAVA_HOME / GHIDRA_INSTALL_DIR 生效。
	// 这一步是冗余保护：envresolve 已经把变量重建过一次，但 zsh -lc 会再
	// 读一遍用户的 rc，任何手工改动都自动跟上。
	cmd := exec.Command("/bin/zsh", "-lc", req.Command)
	cmd.Dir = cwd
	cmd.Env = env
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Stdout = out
	cmd.Stderr = errb

	start := time.Now()
	if err := cmd.Start(); err != nil {
		return ExecResult{OK: false, ExitCode: -1, DurationMS: time.Since(start).Milliseconds()}, err
	}

	waitCh := make(chan error, 1)
	go func() { waitCh <- cmd.Wait() }()

	var (
		waitErr  error
		timedOut bool
	)

	select {
	case waitErr = <-waitCh:
	case <-time.After(timeout):
		timedOut = true
		killGroup(cmd, syscall.SIGTERM)
		select {
		case waitErr = <-waitCh:
		case <-time.After(killGrace):
			killGroup(cmd, syscall.SIGKILL)
			waitErr = <-waitCh
		}
	case <-ctx.Done():
		killGroup(cmd, syscall.SIGTERM)
		waitErr = <-waitCh
	}

	res := ExecResult{
		OK:         true,
		DurationMS: time.Since(start).Milliseconds(),
	}

	stdoutText, dropO := out.Render()
	stderrText, dropE := errb.Render()
	res.Stdout = stdoutText
	res.Stderr = stderrText
	res.TruncatedBytes = dropO + dropE

	if timedOut {
		res.OK = false
		res.ExitCode = -1
		res.Stderr += fmt.Sprintf("\n[axiom-mac-bridge] command timed out after %s, process group killed\n", timeout)
		return res, fmt.Errorf("timeout after %s", timeout)
	}

	var exitErr *exec.ExitError
	switch {
	case waitErr == nil:
		res.ExitCode = 0
	case errors.As(waitErr, &exitErr):
		res.ExitCode = exitErr.ExitCode()
	default:
		res.OK = false
		res.ExitCode = -1
		return res, waitErr
	}
	return res, nil
}

func headTailSizes(maxOutputBytes int) (head, tail int) {
	if maxOutputBytes <= 0 {
		return defaultHeadTail, defaultHeadTail
	}
	// max_output_bytes 约束的是"返回给模型的总量"，head/tail 各一半。
	half := maxOutputBytes / 2
	if half < 4<<10 {
		half = 4 << 10
	}
	return half, half
}

// killGroup 向整个进程组发信号。
func killGroup(cmd *exec.Cmd, sig syscall.Signal) {
	if cmd.Process == nil {
		return
	}
	// 负 pid = 进程组
	_ = syscall.Kill(-cmd.Process.Pid, sig)
}
