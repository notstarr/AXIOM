// Package envresolve 负责在程序启动时重建子进程环境。
//
// 这不是锦上添花，是必需品。实测：从 Finder 双击启动的进程 PATH 只有
// /usr/bin:/bin:/usr/sbin:/sbin，`rea` 的 shebang 是 `#!/usr/bin/env node`，
// 于是 `env: node: No such file or directory` —— 整条 REA 链路直接废掉。
//
// 三步：从登录 shell 抓、补兜底目录、补两个已知的关键变量。
package envresolve

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// FallbackDirs 是 PATH 里缺失时要补的目录。顺序有意义：越靠前优先级越高。
var FallbackDirs = []string{
	"/Library/Developer/CommandLineTools/usr/bin", // llvm-objdump 在这里，不在 PATH 上
	filepath.Join(homeDir(), ".local", "bin"),     // node24 / rea / frida
	filepath.Join(homeDir(), ".local", "go", "bin"),
	"/usr/local/bin",
	"/opt/local/bin",
	"/usr/bin",
	"/bin",
	"/usr/sbin",
	"/sbin",
}

// FallbackVars 是必须显式补的两个变量。
//
// 注意 JAVA_HOME 只在系统里（Temurin 装在 ~/.local/opt/jdk21），
// GHIDRA_INSTALL_DIR 只写在 ~/.zshrc 里、不在 ~/.config/rea/env.sh ——
// 两处都要覆盖到，漏一处 REA 的 ghidra provider 就是灰的。
var FallbackVars = map[string]string{
	"JAVA_HOME":          filepath.Join(homeDir(), ".local", "opt", "jdk21", "Contents", "Home"),
	"GHIDRA_INSTALL_DIR": filepath.Join(homeDir(), ".local", "ghidra", "ghidra_12.1.4_PUBLIC"),
}

// Whitelist 是允许透传给子进程的变量前缀。不在名单里的一律不传，
// 尤其是 AXIOM_* 和任何 token —— 模型跑一条 `env` 就能看见它们。
var Whitelist = []string{
	"PATH", "HOME", "TMPDIR", "TEMP", "TMP",
	"LANG", "LC_",
	"JAVA_HOME", "GHIDRA_INSTALL_DIR", "REA_",
	"SHELL", "TERM", "USER", "LOGNAME",
	"SDKROOT", "DEVELOPER_DIR",
}

// Resolve 返回一份给子进程用的环境切片。
func Resolve() []string {
	base := map[string]string{}

	// 1) 先从当前进程继承一份（启动方式决定了它的 PATH 可能不全）
	for _, kv := range os.Environ() {
		if k, v, ok := strings.Cut(kv, "="); ok {
			if allowed(k) {
				base[k] = v
			}
		}
	}

	// 2) 从登录 shell 抓一次，拿到用户 rc 里定义的东西
	for k, v := range loginShellVars() {
		base[k] = v
	}

	// 3) 显式兜底
	for k, v := range FallbackVars {
		if base[k] == "" {
			if _, err := os.Stat(v); err == nil {
				base[k] = v
			}
		}
	}

	// 4) PATH 补齐
	base["PATH"] = mergePath(base["PATH"])

	// 5) HOME/TMPDIR 兜底
	if base["HOME"] == "" {
		base["HOME"] = homeDir()
	}
	if base["TMPDIR"] == "" {
		base["TMPDIR"] = os.TempDir()
	}

	out := make([]string, 0, len(base))
	for k, v := range base {
		out = append(out, k+"="+v)
	}
	return out
}

// loginShellVars 跑一次 `zsh -lic`，只问三四个变量，避免把整个 env 打回来
// 再被 rc 里的怪东西污染。
func loginShellVars() map[string]string {
	out := map[string]string{}
	cmd := exec.Command("/bin/zsh", "-lic",
		`printf '%s\n' "PATH=$PATH" "JAVA_HOME=$JAVA_HOME" "GHIDRA_INSTALL_DIR=$GHIDRA_INSTALL_DIR" "HOME=$HOME"`)
	// 给 4s：登录 shell 可能跑一堆 init 脚本
	done := make(chan []byte, 1)
	go func() {
		b, _ := cmd.Output()
		done <- b
	}()
	var raw []byte
	select {
	case raw = <-done:
	case <-time.After(4 * time.Second):
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
		return out
	}

	for _, line := range strings.Split(string(raw), "\n") {
		k, v, ok := strings.Cut(strings.TrimSpace(line), "=")
		if !ok || k == "" || v == "" {
			continue
		}
		if allowed(k) {
			out[k] = v
		}
	}
	return out
}

// mergePath 把给定的 path 和 FallbackDirs 合并去重，已有的一律保序在前。
func mergePath(existing string) string {
	seen := map[string]bool{}
	var parts []string
	for _, d := range strings.Split(existing, ":") {
		d = strings.TrimSpace(d)
		if d == "" || seen[d] {
			continue
		}
		seen[d] = true
		parts = append(parts, d)
	}
	for _, d := range FallbackDirs {
		if d == "" || seen[d] {
			continue
		}
		if _, err := os.Stat(d); err != nil {
			continue
		}
		seen[d] = true
		parts = append(parts, d)
	}
	return strings.Join(parts, ":")
}

func allowed(key string) bool {
	for _, p := range Whitelist {
		if strings.HasPrefix(key, p) {
			return true
		}
	}
	return false
}

func homeDir() string {
	if h, err := os.UserHomeDir(); err == nil && h != "" {
		return h
	}
	return os.Getenv("HOME")
}
