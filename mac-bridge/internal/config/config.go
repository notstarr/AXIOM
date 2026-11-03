// Package config 管理设备侧配置。位置固定为 ~/.axiom-mac-bridge/config.json，
// 权限 0600 —— 里面有 device token，不该被同机其他用户读到。
package config

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// Config 是设备侧的全部配置。刻意保持极简：只有三样东西需要人填。
type Config struct {
	// DeviceID 首次生成后永久使用，服务端靠它识别这台机器。
	DeviceID string `json:"device_id"`

	// ServerURL 形如 ws://127.0.0.1:8888/api/v1/mac-bridge/device
	// 或 wss://124.220.165.118:8888/api/v1/mac-bridge/device
	ServerURL string `json:"server_url"`

	// Token 是设备侧握手用的 token，由 AXIOM 网关生成。
	Token string `json:"token"`

	// Enabled 是那个"人工开关"的持久化形态。程序退出再进来应该保持上次选择。
	Enabled bool `json:"enabled"`

	// InsecureSkipVerify 只在连自签证书的 wss:// 时用。
	InsecureSkipVerify bool `json:"insecure_skip_verify,omitempty"`
}

// Dir 配置目录。
func Dir() string {
	if h, err := os.UserHomeDir(); err == nil && h != "" {
		return filepath.Join(h, ".axiom-mac-bridge")
	}
	return filepath.Join(os.TempDir(), "axiom-mac-bridge")
}

// Path 配置文件完整路径。
func Path() string { return filepath.Join(Dir(), "config.json") }

// Load 读配置。文件不存在返回 (nil, ErrNotFound)。
func Load() (*Config, error) {
	b, err := os.ReadFile(Path())
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	var c Config
	if err := json.Unmarshal(b, &c); err != nil {
		return nil, fmt.Errorf("config.json is corrupted: %w", err)
	}
	return &c, nil
}

// Save 写配置，目录不存在则创建，权限锁死 0600。
func Save(c *Config) error {
	if err := os.MkdirAll(Dir(), 0o700); err != nil {
		return err
	}
	b, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	// 先写临时文件再 rename，避免写一半崩溃留下半截 config
	tmp := Path() + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	if err := os.Chmod(tmp, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, Path())
}

// ErrNotFound 配置文件不存在。
var ErrNotFound = errors.New("config not found")

// LoadOrCreate 读配置；不存在则生成一份带新 DeviceID 的默认配置。
func LoadOrCreate() (*Config, error) {
	c, err := Load()
	if err == nil {
		if c.DeviceID == "" {
			c.DeviceID = newDeviceID()
			if err := Save(c); err != nil {
				return c, err
			}
		}
		return c, nil
	}
	if !errors.Is(err, ErrNotFound) {
		return nil, err
	}
	c = &Config{
		DeviceID: newDeviceID(),
		Enabled:  false, // 默认关着：装完不会立刻开始连
	}
	if err := Save(c); err != nil {
		return nil, err
	}
	return c, nil
}

func newDeviceID() string {
	return fmt.Sprintf("mac-%s", randHex(12))
}

func randHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		// 退化成时间戳，保证永远有 id（这个 id 只用于识别设备，不是安全凭据）
		return fmt.Sprintf("%x", os.Getpid())
	}
	return hex.EncodeToString(b)
}
