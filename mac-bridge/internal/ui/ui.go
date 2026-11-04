// Package ui 提供菜单栏与设置窗口。
//
// ── 主线程铁律（改这块代码前先读）─────────────────────────────────
//
//	macOS 的界面只能在主线程画。全进程**只允许一个** `app.Run()`，
//	且**只准在 main goroutine 调用**。任何 goroutine 改 UI 一律
//	`fyne.Do(func(){...})`。
//
//	不要写 `go ui.Run(...)`。那样会把 NSApp 的事件循环丢进 goroutine，
//	AppKit 死锁，菜单栏永远建不起来——这正是上一版"装完什么都没发生"
//	的原因。
//
//	也不要再引第二个 GUI 库（含 webview）。NSApplication 是进程级单例，
//	两个库抢事件循环必然出问题。
//
// ────────────────────────────────────────────────────────────────
package ui

import (
	"bytes"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"log"
	"net/url"
	"strings"
	"time"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/app"
	"fyne.io/fyne/v2/canvas"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/driver/desktop"
	"fyne.io/fyne/v2/widget"

	"axiom/mac-bridge/internal/config"
)

// State 是 UI 渲染所需的全部信息。由主程序提供，UI 不去猜。
type State struct {
	Configured bool
	Enabled    bool
	Connected  bool
	LastErr    string
}

// Kind 五态。
type Kind int

const (
	KindUnconfigured Kind = iota
	KindOff
	KindConnecting
	KindConnected
	KindFailed
)

func (s State) Kind() Kind {
	switch {
	case !s.Configured:
		return KindUnconfigured
	case !s.Enabled:
		return KindOff
	case s.Connected:
		return KindConnected
	case s.LastErr != "":
		return KindFailed
	default:
		return KindConnecting
	}
}

func (k Kind) Label() string {
	switch k {
	case KindUnconfigured:
		return "未配置"
	case KindOff:
		return "已关闭"
	case KindConnecting:
		return "连接中…"
	case KindConnected:
		return "已连接"
	case KindFailed:
		return "连接失败"
	}
	return ""
}

// Hooks 是 UI 与主程序的接口。UI 只负责"问"和"通知"，不含业务逻辑。
type Hooks struct {
	// State 返回当前状态快照
	State func() State
	// Config 返回当前配置（用于填表）
	Config func() *config.Config
	// SaveConfig 保存并热生效。校验失败返回 error，UI 显示红字。
	SaveConfig func(*config.Config) error
	// SetEnabled 改开关
	SetEnabled func(bool) error
	// Quit 退出进程
	Quit func()
	// RegisterShow 由 ui.Run 回调，把"把设置窗口唤到前台"的能力交给主程序。
	// `ctl show` 和第二个实例启动时会用到。
	RegisterShow func(show func())
	// Version 显示在窗口底部
	Version string
}

// Run 启动 UI。**阻塞**，必须在 main goroutine 调用。
//
// autoShowSettings 为 true 且配置不完整时，启动后自动弹出设置窗口。
func Run(h Hooks, autoShowSettings bool) {
	a := app.NewWithID("com.hexin.axiom.macbridge")

	win := a.NewWindow("AXIOM Mac Bridge")
	win.Resize(fyne.NewSize(420, 300))
	win.CenterOnScreen()
	// 关闭 = 隐藏，不是退出。退出只经菜单栏「退出」或 ctl quit。
	win.SetCloseIntercept(func() { win.Hide() })

	if h.RegisterShow != nil {
		h.RegisterShow(func() {
			fyne.Do(func() {
				win.Show()
				win.RequestFocus()
			})
		})
	}

	// 状态标签（红字用 canvas.Text 才能上色）
	statusText := canvas.NewText("", color.NRGBA{120, 120, 130, 255})
	statusText.TextSize = 13

	// ── 设置表单 ────────────────────────────────────────────────
	cfg := h.Config()
	entryURL := widget.NewEntry()
	entryURL.SetPlaceHolder("ws://127.0.0.1:8888/api/v1/mac-bridge/device")
	entryURL.SetText(cfg.ServerURL)

	entryToken := widget.NewPasswordEntry()
	entryToken.SetPlaceHolder("从 AXIOM 的 mac_bridge_token 复制")
	entryToken.SetText(cfg.Token)

	checkInsecure := widget.NewCheck("跳过 TLS 证书校验（自签 wss 用）", nil)
	checkInsecure.SetChecked(cfg.InsecureSkipVerify)

	errText := canvas.NewText("", color.NRGBA{200, 70, 70, 255})
	errText.TextSize = 12

	checkEnabled := widget.NewCheck("连接 AXIOM（允许远端执行命令）", func(on bool) {
		if err := h.SetEnabled(on); err != nil {
			showErr(errText, err.Error())
		}
	})
	checkEnabled.SetChecked(h.State().Enabled)

	saveBtn := widget.NewButton("保存", func() {
		next := &config.Config{
			DeviceID:           cfg.DeviceID,
			ServerURL:          strings.TrimSpace(entryURL.Text),
			Token:              strings.TrimSpace(entryToken.Text),
			Enabled:            cfg.Enabled,
			InsecureSkipVerify: checkInsecure.Checked,
		}
		if err := next.Validate(); err != nil {
			showErr(errText, err.Error())
			return
		}
		wasConfigured := cfg.Configured()
		if err := h.SaveConfig(next); err != nil {
			showErr(errText, err.Error())
			return
		}
		*cfg = *next
		showErr(errText, "")
		// 首次配置成功就把开关打开，一步到位能用；
		// 之前已配置但开关是关的，尊重用户的关闭状态。
		if !wasConfigured {
			_ = h.SetEnabled(true)
		}
	})
	saveBtn.Importance = widget.HighImportance

	form := container.NewVBox(
		widget.NewLabel("服务器地址"),
		entryURL,
		widget.NewLabel("Token"),
		entryToken,
		checkInsecure,
		widget.NewSeparator(),
		statusText,
		checkEnabled,
		errText,
		saveBtn,
	)
	win.SetContent(container.NewPadded(container.NewVBox(
		widget.NewLabelWithStyle("AXIOM Mac Bridge", fyne.TextAlignLeading, fyne.TextStyle{Bold: true}),
		form,
		widget.NewLabel("v"+h.Version+"　关闭窗口不退出，程序留在菜单栏"),
	)))

	// ── 菜单栏 ──────────────────────────────────────────────────
	menuStatus := fyne.NewMenuItem("状态：…", nil)
	menuStatus.Disabled = true
	menuToggle := fyne.NewMenuItem("连接 AXIOM", func() {
		next := !h.State().Enabled
		if err := h.SetEnabled(next); err != nil {
			log.Printf("ui: toggle: %v", err)
		}
	})
	menuSettings := fyne.NewMenuItem("设置…", func() {
		fyne.Do(func() {
			win.Show()
			win.RequestFocus()
		})
	})
	menuQuit := fyne.NewMenuItem("退出", func() {
		h.Quit()
	})
	menu := fyne.NewMenu("AXIOM Mac Bridge", menuSettings, menuToggle, menuStatus, fyne.NewMenuItemSeparator(), menuQuit)

	if da, ok := a.(desktop.App); ok {
		da.SetSystemTrayIcon(trayIcon())
		da.SetSystemTrayMenu(menu)
		// 左键点托盘图标直接开设置窗
		da.SetSystemTrayWindow(win)
	}

	// ── 状态刷新 ────────────────────────────────────────────────
	// 1s 一次足够：连接状态本来就是秒级变化的。
	go func() {
		t := time.NewTicker(time.Second)
		defer t.Stop()
		for range t.C {
			s := h.State()
			k := s.Kind()
			label := k.Label()
			if k == KindFailed && s.LastErr != "" {
				label = "连接失败：" + truncate(s.LastErr, 40)
			}
			fyne.Do(func() {
				menuStatus.Label = "状态：" + label
				menuToggle.Checked = s.Enabled
				menu.Refresh()
				statusText.Text = "状态：" + label
				if k == KindUnconfigured || k == KindFailed {
					statusText.Color = color.NRGBA{200, 70, 70, 255}
				} else if k == KindConnected {
					statusText.Color = color.NRGBA{60, 150, 90, 255}
				} else {
					statusText.Color = color.NRGBA{120, 120, 130, 255}
				}
				statusText.Refresh()
				checkEnabled.SetChecked(s.Enabled)
			})
		}
	}()

	// 首启：配置不完整就直接把设置窗口推到脸上
	if autoShowSettings && !h.State().Configured {
		win.Show()
		win.RequestFocus()
	}

	log.Println("ui: entering event loop (this call must stay on the main goroutine)")
	a.Run()
	log.Println("ui: event loop returned")
}

func showErr(t *canvas.Text, msg string) {
	t.Text = msg
	fyne.Do(t.Refresh)
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

// trayIcon 生成一个 22×22 的 PNG 托盘图标。
//
// 为什么不放资源文件：fyne 的托盘只吃图片，纯文字标题没有对应 API。
// 运行时画一个深色圆角方块 + "A" 字样，省掉一个 .png 资源，也不用担心
// 尺寸在不同 macOS 版本上不一致。
func trayIcon() fyne.Resource {
	const size = 22
	img := image.NewNRGBA(image.Rect(0, 0, size, size))
	bg := color.NRGBA{40, 44, 52, 255}
	fg := color.NRGBA{230, 232, 236, 255}
	for y := 0; y < size; y++ {
		for x := 0; x < size; x++ {
			// 圆角：四角 4px 半径外才画背景
			cx, cy := x-4, y-4
			cx2, cy2 := size-5-x, y-4
			cx3, cy3 := x-4, size-5-y
			cx4, cy4 := size-5-x, size-5-y
			corner := func(dx, dy int) bool { return dx*dx+dy*dy > 16 }
			if (x < 4 && y < 4 && corner(cx, cy)) ||
				(x >= size-4 && y < 4 && corner(cx2, cy2)) ||
				(x < 4 && y >= size-4 && corner(cx3, cy3)) ||
				(x >= size-4 && y >= size-4 && corner(cx4, cy4)) {
				continue
			}
			img.SetNRGBA(x, y, bg)
		}
	}
	// 一个粗一点的竖笔画，读起来像 "A"
	for y := 6; y <= 15; y++ {
		for x := 9; x <= 12; x++ {
			img.SetNRGBA(x, y, fg)
		}
	}
	for x := 7; x <= 14; x++ {
		img.SetNRGBA(x, 15, fg)
	}

	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		return nil
	}
	return fyne.NewStaticResource("tray.png", buf.Bytes())
}

// 留一个引用，避免 goimports 把 net/url 当未使用删掉——
// Validate 在 config 包里，但这里保留校验错误的 URL 语义说明。
var _ = url.Parse
var _ = fmt.Sprintf
