// axiom-mac-bridge 是 AXIOM 在 macOS 上的执行端。
//
// 它本身不含任何"智能"：收到一条命令就跑，跑完回结果。判断什么该做、
// 结果算不算成功，全在 AXIOM 那一侧的模型里。这里唯一的人工授权点是
// 菜单栏那个开关——关了就断开连接，指令到不了这台机器。
//
//	 axiom-mac-bridge                 菜单栏常驻（默认）
//	 axiom-mac-bridge selftest        自检，不碰网络，不碰 AXIOM
//	 axiom-mac-bridge ctl on|off|status   无 GUI 时的开关
//	 axiom-mac-bridge install-agent   装 LaunchAgent
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"time"

	"axiom/mac-bridge/internal/config"
	"axiom/mac-bridge/internal/control"
	"axiom/mac-bridge/internal/core"
	"axiom/mac-bridge/internal/envresolve"
	"axiom/mac-bridge/internal/link"
	"axiom/mac-bridge/internal/ui"
)

var version = "dev"

func main() {
	args := os.Args[1:]
	sub := "gui"
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		sub = args[0]
		args = args[1:]
	}

	switch sub {
	case "selftest":
		os.Exit(cmdSelftest(args))
	case "ctl":
		os.Exit(cmdCtl(args))
	case "install-agent":
		os.Exit(cmdInstallAgent(args))
	case "gui":
		os.Exit(cmdGUI(args))
	case "version", "--version", "-v":
		fmt.Printf("axiom-mac-bridge %s (%s/%s)\n", version, runtime.GOOS, runtime.GOARCH)
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand: %s\n", sub)
		fmt.Fprintf(os.Stderr, "usage: axiom-mac-bridge [selftest|ctl|install-agent|gui|version]\n")
		os.Exit(2)
	}
}

// ---------------------------------------------------------------------------
// selftest
// ---------------------------------------------------------------------------

type probe struct {
	Name   string `json:"name"`
	OK     bool   `json:"ok"`
	Detail string `json:"detail"`
}

type selftestReport struct {
	OK        bool    `json:"ok"`
	Version   string  `json:"version"`
	OS        string  `json:"os"`
	Arch      string  `json:"arch"`
	Hostname  string  `json:"hostname"`
	Target    string  `json:"target,omitempty"`
	Probes    []probe `json:"probes"`
	DurationMS int64  `json:"duration_ms"`
}

func cmdSelftest(args []string) int {
	target := ""
	asJSON := false
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--target":
			if i+1 < len(args) {
				target = args[i+1]
				i++
			}
		case "--json":
			asJSON = true
		}
	}

	start := time.Now()
	env := envresolve.Resolve()
	rep := selftestReport{Version: version, OS: runtime.GOOS, Arch: runtime.GOARCH, Target: target}
	if h, err := os.Hostname(); err == nil {
		rep.Hostname = h
	}

	// 统一走 /bin/zsh -lc：exec.Command("node") 找可执行文件用的是**本进程**
	// 的 PATH，cmd.Env 改不了查找结果。只有经 shell 展开，测的才是
	// "重建出来的 PATH 能不能找到 node"。
	run := func(name, command string) probe {
		p := probe{Name: name}
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		res, err := core.Run(ctx, core.ExecRequest{Command: command, Timeout: 25 * time.Second}, env)
		text := strings.TrimSpace(res.Stdout + res.Stderr)
		if len(text) > 400 {
			text = text[:400] + "…"
		}
		if err != nil || !res.OK {
			p.OK = false
			p.Detail = fmt.Sprintf("%v | %s", err, text)
			return p
		}
		p.OK = true
		p.Detail = text
		return p
	}

	// 1) 环境重建 —— 这是最容易坏、坏了最难查的一项
	probeEnv := probe{Name: "env_path"}
	paths := ""
	for _, kv := range env {
		if strings.HasPrefix(kv, "PATH=") {
			paths = strings.TrimPrefix(kv, "PATH=")
		}
	}
	need := []string{"~/.local/bin", "/Library/Developer/CommandLineTools/usr/bin"}
	missing := []string{}
	for _, n := range need {
		expanded := strings.ReplaceAll(n, "~", os.Getenv("HOME"))
		if !strings.Contains(paths, expanded) {
			missing = append(missing, n)
		}
	}
	probeEnv.OK = len(missing) == 0
	probeEnv.Detail = fmt.Sprintf("PATH has %d entries; missing: %v", strings.Count(paths, ":")+1, missing)
	rep.Probes = append(rep.Probes, probeEnv)

	// 2) JAVA_HOME / GHIDRA_INSTALL_DIR
	for _, key := range []string{"JAVA_HOME", "GHIDRA_INSTALL_DIR"} {
		p := probe{Name: strings.ToLower(key)}
		found := ""
		for _, kv := range env {
			if strings.HasPrefix(kv, key+"=") {
				found = strings.TrimPrefix(kv, key+"=")
			}
		}
		if found == "" {
			p.OK = false
			p.Detail = "not set"
		} else if _, err := os.Stat(found); err != nil {
			p.OK = false
			p.Detail = "set but path missing: " + found
		} else {
			p.OK = true
			p.Detail = found
		}
		rep.Probes = append(rep.Probes, p)
	}

	// 3) node / rea —— 专门验证 Finder 启动时 node 能不能被找到。
	// 用 `command -v` 顺带把"找到了哪个"打出来，避免版本对了但二进制是错的。
	rep.Probes = append(rep.Probes, run("node_version",
		`echo "$(command -v node)" && node --version`))
	rep.Probes = append(rep.Probes, run("rea_version",
		`echo "$(command -v rea)" && rea --version`))

	// 4) 系统工具。注意 codesign 没有 --version，`codesign -v` 是"verify"
	// 且必须带路径，不带路径会打 usage 退出 2 —— 别把它当探测命令。
	rep.Probes = append(rep.Probes, run("codesign_display", `/usr/bin/codesign -dvvv /bin/ls`))
	rep.Probes = append(rep.Probes, run("llvm_objdump_version",
		`/Library/Developer/CommandLineTools/usr/bin/llvm-objdump --version`))

	// 5) Ghidra headless
	ghidra := ""
	for _, kv := range env {
		if strings.HasPrefix(kv, "GHIDRA_INSTALL_DIR=") {
			ghidra = strings.TrimPrefix(kv, "GHIDRA_INSTALL_DIR=")
		}
	}
	headless := filepath.Join(ghidra, "support", "analyzeHeadless")
	pGh := probe{Name: "ghidra_headless"}
	if ghidra == "" {
		pGh.OK = false
		pGh.Detail = "GHIDRA_INSTALL_DIR not set"
	} else if st, err := os.Stat(headless); err != nil || st.IsDir() {
		pGh.OK = false
		pGh.Detail = "analyzeHeadless not found at " + headless
	} else {
		pGh.OK = true
		pGh.Detail = headless
	}
	rep.Probes = append(rep.Probes, pGh)

	// 6) 目标应用（可选）
	if target != "" {
		pT := probe{Name: "target_exists"}
		if _, err := os.Stat(target); err != nil {
			pT.OK = false
			pT.Detail = err.Error()
		} else {
			pT.OK = true
			pT.Detail = "present"
		}
		rep.Probes = append(rep.Probes, pT)

		// 含空格路径的坑：otool 会被空格截断，这里显式验一下
		pO := probe{Name: "target_with_spaces"}
		if strings.Contains(target, " ") {
			pO.OK = true
			pO.Detail = "target path contains spaces — use llvm-objdump, not otool"
		} else {
			pO.OK = true
			pO.Detail = "no spaces in target path"
		}
		rep.Probes = append(rep.Probes, pO)

		// 签名取证
		rep.Probes = append(rep.Probes, run("target_codesign",
			fmt.Sprintf(`/usr/bin/codesign -dvvv %q`, target)))
	}

	// 7) 命令闸门自测（保证核心路径真的能跑一条命令）
	res, err := core.Run(context.Background(), core.ExecRequest{
		Command: "echo axiom-mac-bridge-ok && /usr/bin/uname -s",
		Timeout: 5 * time.Second,
	}, env)
	pExec := probe{Name: "exec_smoke"}
	if err != nil || !strings.Contains(res.Stdout, "axiom-mac-bridge-ok") {
		pExec.OK = false
		pExec.Detail = fmt.Sprintf("err=%v stdout=%q", err, res.Stdout)
	} else {
		pExec.OK = true
		pExec.Detail = strings.TrimSpace(res.Stdout)
	}
	rep.Probes = append(rep.Probes, pExec)

	// 汇总
	all := true
	for _, p := range rep.Probes {
		if !p.OK {
			all = false
			break
		}
	}
	rep.OK = all
	rep.DurationMS = time.Since(start).Milliseconds()

	if asJSON {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(rep)
	} else {
		fmt.Printf("axiom-mac-bridge selftest %s  %s/%s  ok=%v\n\n", version, runtime.GOOS, runtime.GOARCH, rep.OK)
		for _, p := range rep.Probes {
			mark := "OK "
			if !p.OK {
				mark = "FAIL"
			}
			fmt.Printf("  [%s] %-22s %s\n", mark, p.Name, p.Detail)
		}
	}
	if rep.OK {
		return 0
	}
	return 1
}

// ---------------------------------------------------------------------------
// gui / ctl
// ---------------------------------------------------------------------------

func cmdCtl(args []string) int {
	if len(args) < 1 {
		fmt.Fprintln(os.Stderr, "usage: axiom-mac-bridge ctl on|off|status|quit")
		return 2
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	resp, err := control.Send(ctx, args[0])
	if err != nil {
		fmt.Fprintf(os.Stderr, "%v\n", err)
		return 1
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	_ = enc.Encode(resp)
	if ok, _ := resp["ok"].(bool); !ok {
		return 1
	}
	return 0
}

func cmdGUI(args []string) int {
	headless := false
	autoShow := true
	for _, a := range args {
		switch a {
		case "--headless", "--no-tray":
			headless = true
		case "--background":
			// LaunchAgent 用：只出托盘，不自动弹设置窗
			autoShow = false
		}
	}

	lock, err := control.AcquireInstanceLock()
	if err != nil {
		// Bug 2 修复：拿不到锁**不能静默退出**。先试着唤起已有实例的设置窗，
		// 唤不起来就弹一个系统可见的提示框——stderr 在 Finder 双击下是看不见的。
		notifyAlreadyRunning()
		return 0
	}
	defer control.ReleaseInstanceLock(lock)

	cfg, err := config.LoadOrCreate()
	if err != nil {
		fmt.Fprintf(os.Stderr, "读配置失败: %v\n", err)
		notifyAlert("读取配置失败", err.Error())
		return 1
	}

	hostname, _ := os.Hostname()
	disp := core.NewDispatcher(cfg.DeviceID, version, runtime.GOOS, runtime.GOARCH, hostname)
	disp.SetEnabled(cfg.Enabled)

	rootCtx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	ctrl := newController(disp, cfg, rootCtx)
	srv := control.NewServer(ctrl)
	if err := srv.Start(); err != nil {
		fmt.Fprintf(os.Stderr, "本地控制通道启动失败: %v\n", err)
		return 1
	}
	defer srv.Close()

	fmt.Printf("axiom-mac-bridge %s  device=%s  server=%s  enabled=%v\n",
		version, cfg.DeviceID, cfg.ServerURL, cfg.Enabled)

	if headless {
		// 无 UI 模式：LaunchAgent / 调试用。保留旧的阻塞等待。
		<-rootCtx.Done()
		if disp.Enabled() {
			ctrl.client.SignalOff("operator_off")
		}
		fmt.Println("axiom-mac-bridge 已退出")
		return 0
	}

	// ── GUI 模式 ─────────────────────────────────────────────────
	// 链路在后台跑；UI 在主 goroutine 阻塞。
	go ctrl.runLink()

	uiHooks := ui.Hooks{
		State:        ctrl.uiState,
		Config:       ctrl.uiConfig,
		SaveConfig:   ctrl.SaveConfig,
		SetEnabled:   ctrl.SetEnabled,
		Quit:         func() { stop() },
		RegisterShow: ctrl.setShowFn,
		Version:      version,
	}

	// ★ 铁律：ui.Run 必须在 main goroutine 阻塞调用 ★
	// 之前写成 `go ui.Run(...)`，把 NSApp 的事件循环丢进 goroutine，
	// AppKit 死锁，菜单栏永远建不起来——那就是"装完什么都没发生"的原因。
	ui.Run(uiHooks, autoShow)

	// 事件循环返回（用户点了退出）
	if disp.Enabled() && ctrl.client != nil {
		ctrl.client.SignalOff("operator_off")
	}
	fmt.Println("axiom-mac-bridge 已退出")
	return 0
}

// notifyAlreadyRunning 第二个实例启动时的可见反馈。
func notifyAlreadyRunning() {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	resp, err := control.Send(ctx, "show")
	if err == nil {
		if ok, _ := resp["ok"].(bool); ok {
			return // 已有实例的设置窗被唤起来了
		}
	}
	// 旧版本实例不认识 show，或者 socket 不通：弹系统提示框
	notifyAlert("AXIOM Mac Bridge 已在运行",
		"菜单栏里应该已经有一个 AXIOM 图标。\n如果没看到，先执行：axiom-mac-bridge ctl quit")
}

// notifyAlert 弹一个系统可见的提示框。
//
// 用 osascript 而不是自己画：此时要么进程里没有 NSApp，要么事件循环没跑，
// 自己画是画不出来的。osascript 是独立进程，无此问题。
func notifyAlert(title, msg string) {
	script := fmt.Sprintf(`display alert %q message %q as warning`, title, msg)
	cmd := exec.Command("/usr/bin/osascript", "-e", script)
	_ = cmd.Run()
}

// controller 把控制通道 / UI 的命令落到 Dispatcher 与 link 上。
//
// 之所以要加锁：控制通道的 goroutine 和 UI 的 goroutine 都会调
// SetEnabled / SaveConfig，而 client 会被热替换（改配置后重建连接）。
type controller struct {
	mu           sync.Mutex
	disp         *core.Dispatcher
	cfg          *config.Config
	client       *link.Client
	clientCancel context.CancelFunc
	rootCtx      context.Context
	showFn       func()
}

func newController(disp *core.Dispatcher, cfg *config.Config, rootCtx context.Context) *controller {
	c := &controller{disp: disp, cfg: cfg, rootCtx: rootCtx}
	c.startLink()
	return c
}

// startLink 按当前配置起一条连接。调用方需持锁。
func (c *controller) startLink() {
	if !c.cfg.Configured() {
		return
	}
	ctx, cancel := context.WithCancel(c.rootCtx)
	c.clientCancel = cancel
	c.client = link.New(c.cfg.ServerURL, c.cfg.Token, c.cfg.InsecureSkipVerify, c.disp)
	go func() { _ = c.client.Run(ctx) }()
}

func (c *controller) runLink() {
	// 连接在 startLink 里就起了，这里不需要额外动作
}

// setShowFn 由 ui.Run 初始化时回调，注册"唤起设置窗口"的能力。
func (c *controller) setShowFn(fn func()) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.showFn = fn
}

func (c *controller) SetEnabled(on bool) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	was := c.disp.Enabled()
	c.disp.SetEnabled(on)
	c.cfg.Enabled = on
	if err := config.Save(c.cfg); err != nil {
		return err
	}
	// 从开到关：必须真的断开连接并告诉服务端，否则它一直显示 online
	if was && !on && c.client != nil {
		c.client.SignalOff("operator_off")
	}
	fmt.Printf("开关 -> %v\n", on)
	return nil
}

// SaveConfig 保存配置并热生效，不重启。
//
// 顺序很重要：先断旧连接并等它退出，再建新的。反过来会两条连接并存，
// 在途命令的结果会串到新会话上。
func (c *controller) SaveConfig(next *config.Config) error {
	c.mu.Lock()
	defer c.mu.Unlock()

	if err := next.Validate(); err != nil {
		return err
	}
	next.DeviceID = c.cfg.DeviceID // 设备身份不随配置变
	if err := config.Save(next); err != nil {
		return err
	}

	if c.clientCancel != nil {
		if c.client != nil {
			c.client.SignalOff("config_changed")
		}
		c.clientCancel()
	}
	*c.cfg = *next
	c.startLink()
	fmt.Println("配置已保存，连接已重建")
	return nil
}

// Show 把设置窗口唤到前台（ctl show / 第二实例用）。
func (c *controller) Show() error {
	c.mu.Lock()
	fn := c.showFn
	headless := fn == nil
	c.mu.Unlock()
	if headless {
		return errors.New("GUI 未启用（headless 模式）")
	}
	fn()
	return nil
}

func (c *controller) Status() (map[string]any, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	connected, lastErr := false, ""
	if c.client != nil {
		connected, lastErr = c.client.Snapshot()
	}
	s := ui.State{Configured: c.cfg.Configured(), Enabled: c.disp.Enabled(), Connected: connected, LastErr: lastErr}
	return map[string]any{
		"device_id":  c.cfg.DeviceID,
		"server":     c.cfg.ServerURL,
		"enabled":    c.disp.Enabled(),
		"version":    version,
		"state":      s.Kind().Label(),
		"connected":  connected,
		"last_error": lastErr,
	}, nil
}

func (c *controller) Quit() error {
	p, _ := os.FindProcess(os.Getpid())
	return p.Signal(syscall.SIGTERM)
}

// ── 给 UI 的三个取数方法 ────────────────────────────────────────

func (c *controller) uiState() ui.State {
	c.mu.Lock()
	defer c.mu.Unlock()
	connected, lastErr := false, ""
	if c.client != nil {
		connected, lastErr = c.client.Snapshot()
	}
	return ui.State{
		Configured: c.cfg.Configured(),
		Enabled:    c.disp.Enabled(),
		Connected:  connected,
		LastErr:    lastErr,
	}
}

func (c *controller) uiConfig() *config.Config {
	c.mu.Lock()
	defer c.mu.Unlock()
	cp := *c.cfg
	return &cp
}

func cmdInstallAgent(args []string) int {
	uninstall := false
	for _, a := range args {
		if a == "--uninstall" {
			uninstall = true
		}
	}
	home, _ := os.UserHomeDir()
	plist := filepath.Join(home, "Library", "LaunchAgents", "com.hexin.axiom.macbridge.plist")

	if uninstall {
		_ = exec.Command("launchctl", "unload", plist).Run()
		if err := os.Remove(plist); err != nil && !os.IsNotExist(err) {
			fmt.Fprintf(os.Stderr, "删除失败: %v\n", err)
			return 1
		}
		fmt.Println("已卸载开机自启")
		return 0
	}

	exe, err := os.Executable()
	if err != nil {
		fmt.Fprintf(os.Stderr, "找不到自身可执行文件: %v\n", err)
		return 1
	}
	exe, _ = filepath.EvalSymlinks(exe)

	logDir := filepath.Join(home, "Library", "Logs")
	_ = os.MkdirAll(logDir, 0o755)
	_ = os.MkdirAll(filepath.Dir(plist), 0o755)

	// KeepAlive 必须是 false：为真的话用户从菜单栏点退出会被 launchd 立刻
	// 拉起来，等于"退出"这个动作不存在，跟开关语义直接冲突。
	content := fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>com.hexin.axiom.macbridge</string>
	<key>ProgramArguments</key>
	<array>
		<string>%s</string>
		<string>gui</string>
		<string>--background</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<false/>
	<key>LimitLoadToSessionType</key>
	<string>Aqua</string>
	<key>StandardOutPath</key>
	<string>%s/axiom-mac-bridge.log</string>
	<key>StandardErrorPath</key>
	<string>%s/axiom-mac-bridge.log</string>
</dict>
</plist>
`, exe, logDir, logDir)

	if err := os.WriteFile(plist, []byte(content), 0o644); err != nil {
		fmt.Fprintf(os.Stderr, "写 plist 失败: %v\n", err)
		return 1
	}
	if out, err := exec.Command("launchctl", "load", plist).CombinedOutput(); err != nil {
		fmt.Fprintf(os.Stderr, "launchctl load 失败: %v\n%s", err, out)
		return 1
	}
	fmt.Printf("已安装开机自启: %s\n", plist)
	fmt.Printf("可执行文件: %s\n", exe)
	fmt.Println("卸载: axiom-mac-bridge install-agent --uninstall")
	return 0
}

// 避免 unused import：config 在 ctl/gui 里用
var _ = config.Path
