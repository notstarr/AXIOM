// Package control 提供本地控制通道：命令行开关 + 单实例锁。
//
// 为什么需要 unix socket 而不是直接改配置文件：菜单栏关掉之后要让运行中
// 的进程立刻断开，光改文件它下次才读到。socket 让 `ctl off` 能立刻生效。
package control

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
)

// SocketPath 控制通道路径。放在配置目录里，跟着配置一起被 deny 列表挡住。
func SocketPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".axiom-mac-bridge", "control.sock")
}

// LockPath 单实例锁文件。
func LockPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".axiom-mac-bridge", "instance.lock")
}

// Controller 是被 ctl 命令操作的那一侧。
type Controller interface {
	SetEnabled(on bool) error
	Status() (map[string]any, error)
	Quit() error
	// Show 把设置窗口唤到前台。第二个实例启动时会调它，
	// 免得用户点了没反应。headless 模式下可以返回错误。
	Show() error
}

// Server 本地控制服务器。
type Server struct {
	ctrl   Controller
	ln     net.Listener
	mu     sync.Mutex
	closed bool
}

func NewServer(ctrl Controller) *Server {
	return &Server{ctrl: ctrl}
}

// Start 在后台监听。失败通常是上一次残留的 socket 文件。
func (s *Server) Start() error {
	path := SocketPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	// 残留的 socket 文件必须先删，否则 bind 会报 address in use
	_ = os.Remove(path)

	ln, err := net.Listen("unix", path)
	if err != nil {
		return err
	}
	if err := os.Chmod(path, 0o600); err != nil {
		_ = ln.Close()
		return err
	}
	s.ln = ln
	go s.acceptLoop()
	return nil
}

func (s *Server) acceptLoop() {
	for {
		conn, err := s.ln.Accept()
		if err != nil {
			s.mu.Lock()
			closed := s.closed
			s.mu.Unlock()
			if closed {
				return
			}
			continue
		}
		go s.serve(conn)
	}
}

func (s *Server) serve(conn net.Conn) {
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	line, err := bufio.NewReader(conn).ReadString('\n')
	if err != nil {
		return
	}
	cmd := strings.TrimSpace(line)
	resp := map[string]any{"ok": false}

	switch {
	case cmd == "on":
		resp = s.wrap(s.ctrl.SetEnabled(true))
	case cmd == "off":
		resp = s.wrap(s.ctrl.SetEnabled(false))
	case cmd == "status":
		if st, err := s.ctrl.Status(); err == nil {
			resp = map[string]any{"ok": true, "status": st}
		} else {
			resp = map[string]any{"ok": false, "error": err.Error()}
		}
	case cmd == "quit":
		resp = s.wrap(s.ctrl.Quit())
	case cmd == "show":
		resp = s.wrap(s.ctrl.Show())
	default:
		resp = map[string]any{"ok": false, "error": "unknown command: " + cmd}
	}
	buf, _ := json.Marshal(resp)
	_, _ = conn.Write(append(buf, '\n'))
}

func (s *Server) wrap(err error) map[string]any {
	if err != nil {
		return map[string]any{"ok": false, "error": err.Error()}
	}
	return map[string]any{"ok": true}
}

// Close 停止监听并清理 socket。
func (s *Server) Close() {
	s.mu.Lock()
	s.closed = true
	s.mu.Unlock()
	if s.ln != nil {
		_ = s.ln.Close()
	}
	_ = os.Remove(SocketPath())
}

// ---------------------------------------------------------------------------
// 客户端侧（ctl 子命令用）
// ---------------------------------------------------------------------------

// Send 给运行中的实例发一条命令。
func Send(ctx context.Context, command string) (map[string]any, error) {
	d := net.Dialer{}
	conn, err := d.DialContext(ctx, "unix", SocketPath())
	if err != nil {
		return nil, fmt.Errorf("没有正在运行的实例（%v）", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	if _, err := conn.Write([]byte(command + "\n")); err != nil {
		return nil, err
	}
	var raw []byte
	buf := make([]byte, 4096)
	for {
		n, rerr := conn.Read(buf)
		raw = append(raw, buf[:n]...)
		if rerr != nil || n == 0 {
			break
		}
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("bad response: %s", strings.TrimSpace(string(raw)))
	}
	return out, nil
}

// ---------------------------------------------------------------------------
// 单实例锁
// ---------------------------------------------------------------------------

// AcquireInstanceLock 拿到就返回 fd；拿不到返回错误（已有实例在跑）。
//
// 用 flock 而不是 pid 文件：进程崩溃时 flock 由内核自动释放，pid 文件会
// 留下一个孤儿让所有后续启动都失败。
func AcquireInstanceLock() (*os.File, error) {
	path := LockPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = f.Close()
		return nil, errors.New("已有另一个 axiom-mac-bridge 在运行")
	}
	return f, nil
}

// ReleaseInstanceLock 释放锁。
func ReleaseInstanceLock(f *os.File) {
	if f == nil {
		return
	}
	_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
	_ = f.Close()
}
