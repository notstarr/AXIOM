package core

import (
	"fmt"
	"strings"
)

// 命令文本的廉价防手滑黑名单。
//
// 说清楚边界：这不是安全边界。给模型一个 shell 就等于给了它这台机器的
// 用户权限，任何文本匹配都能被绕过（拼接、base64、变量）。它的作用只有一
// 个——挡住模型顺手 `cat ~/.ssh/id_rsa` 这种明显越界、但显然不是攻击意图
// 的动作。真正的边界靠菜单栏开关。
var commandDenySubstrings = []string{
	".ssh",
	"id_rsa",
	"id_ed25519",
	"Keychains",
	"login.keychain",
	".aws/credentials",
	".gnupg",
	"config.json", // 自身配置里有 device token
}

// CheckCommand 返回 nil 表示放行；否则返回可直接回给调用方的错误。
func CheckCommand(command string) error {
	if strings.TrimSpace(command) == "" {
		return fmt.Errorf("command is empty")
	}
	lower := strings.ToLower(command)
	for _, bad := range commandDenySubstrings {
		if strings.Contains(lower, strings.ToLower(bad)) {
			return fmt.Errorf("command rejected: contains %q", bad)
		}
	}
	return nil
}
