//go:build darwin

// Package tray 提供菜单栏上的那个"人工开关"。
//
// 这是整套东西里唯一的人工授权点：开关一关，连接就断，指令到不了这台机器。
// 不能只有配置文件——那样"关"这个动作没有立刻生效的语义。
//
// 图标用文字标题而不是 PNG：省掉一个资源文件，也不会因为图标尺寸在
// 不同 macOS 版本上表现不一致。
package tray

import (
	"log"
	"time"

	"fyne.io/systray"
)

// Callbacks 是菜单栏与主程序之间的接口。菜单栏只负责"问"和"通知"。
type Callbacks struct {
	// OnToggle 用户点击开关时调用
	OnToggle func(on bool)
	// OnQuit 用户点退出
	OnQuit func()
	// Enabled 当前开关状态
	Enabled func() bool
	// Status 一行状态描述，显示在菜单里
	Status func() string
}

// Run 启动菜单栏。**阻塞**，调用方要放 goroutine。
func Run(cb Callbacks) {
	systray.Run(func() {
		systray.SetTitle("AXIOM")
		systray.SetTooltip("AXIOM Mac Bridge")

		mToggle := systray.AddMenuItem("连接 AXIOM", "打开后 AXIOM 才能在这台 Mac 上执行命令")
		systray.AddSeparator()
		mStatus := systray.AddMenuItem("状态：…", "")
		mStatus.Disable()
		systray.AddSeparator()
		mQuit := systray.AddMenuItem("退出", "退出 AXIOM Mac Bridge")

		if cb.Enabled() {
			mToggle.Check()
		}

		go func() {
			for {
				select {
				case <-mToggle.ClickedCh:
					next := !cb.Enabled()
					if next {
						mToggle.Check()
					} else {
						mToggle.Uncheck()
					}
					// 先让 UI 反馈，再做实际动作（后者可能阻塞）
					go cb.OnToggle(next)
				case <-mQuit.ClickedCh:
					go cb.OnQuit()
					systray.Quit()
					return
				}
			}
		}()

		// 状态行定时刷新，让"已连接/已断开"在菜单里是可见的。
		// 只是 SetTitle，很便宜；不需要退出信号——进程整体退出时它一起消失。
		go func() {
			ticker := time.NewTicker(2 * time.Second)
			defer ticker.Stop()
			for range ticker.C {
				if s := cb.Status(); s != "" {
					mStatus.SetTitle("状态：" + s)
				}
			}
		}()
	}, func() {
		log.Println("tray: exited")
	})
}
