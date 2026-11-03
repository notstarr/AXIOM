package pull

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"axiom/mac-bridge/internal/proto"
)

// 为什么需要 pull 而不是 `cat`：二进制文件走 shell 会被按字节塞进
// stdout，一路上要过 zsh 的管道、JSON 编码、再到模型的上下文，字节必然
// 被破坏（NUL、非 UTF-8）。文件必须走独立通道，用 base64 分片。
const (
	// MaxPullBytes 单次 pull 上限，超出拒绝而不是截断——截断会让调用方
	// 误以为拿到了完整文件。
	MaxPullBytes = proto.MaxPullBytes
)

// CheckPath 做最小的路径校验。说清楚：这防的是手滑，不是恶意。
func CheckPath(path string) (string, error) {
	if strings.TrimSpace(path) == "" {
		return "", fmt.Errorf("path is empty")
	}
	if !filepath.IsAbs(path) {
		return "", fmt.Errorf("%s: %s", proto.ErrPathNotAbsolute, path)
	}
	// 在 Clean 之前查 ..，Clean 会把它消掉、事后查不出来。
	for _, seg := range strings.Split(path, "/") {
		if seg == ".." {
			return "", fmt.Errorf("%s: %s", proto.ErrPathTraversal, path)
		}
	}
	clean := filepath.Clean(path)
	if denied(clean) {
		return "", fmt.Errorf("%s: %s", proto.ErrPathDenied, clean)
	}
	// 符号链接要解析到真实路径再判一次，否则 deny 可以用一个软链绕过
	if real, err := filepath.EvalSymlinks(clean); err == nil {
		if denied(real) {
			return "", fmt.Errorf("%s: %s", proto.ErrPathDenied, real)
		}
		return real, nil
	}
	return clean, nil
}

func denied(path string) bool {
	prefixes := []string{
		"/System",
		"/var/db",
		"/Library/Keychains",
	}
	home, _ := os.UserHomeDir()
	if home != "" {
		prefixes = append(prefixes,
			filepath.Join(home, ".ssh"),
			filepath.Join(home, ".aws"),
			filepath.Join(home, ".gnupg"),
			filepath.Join(home, "Library", "Keychains"),
			filepath.Join(home, ".axiom-mac-bridge"), // 自身配置里有 device token
		)
	}
	for _, p := range prefixes {
		if path == p || strings.HasPrefix(path, p+string(os.PathSeparator)) {
			return true
		}
	}
	return false
}

// Chunk 表示一片数据。Data 是原始字节，落到 proto.Chunk 时由
// encoding/json 自动做 base64（[]byte 的默认行为），不要手工再编码一次。
type Chunk struct {
	Seq   int
	Data  []byte
	Last  bool
	Total int64
}

// Stream 按 offset/maxBytes 读文件，逐片回调。回调拿到的 Data 是原始字节。
func Stream(path string, offset, maxBytes int64, emit func(Chunk) error) (size int64, sha string, err error) {
	checked, err := CheckPath(path)
	if err != nil {
		return 0, "", err
	}

	st, err := os.Stat(checked)
	if err != nil {
		return 0, "", fmt.Errorf("%s: %s", proto.ErrPathNotExist, checked)
	}
	if !st.Mode().IsRegular() {
		return 0, "", fmt.Errorf("%s: not a regular file", proto.ErrNotFile)
	}
	if st.Size() > MaxPullBytes && maxBytes <= 0 {
		return 0, "", fmt.Errorf("%s: file is %d bytes, over the %d limit; pass max_bytes to read a slice",
			proto.ErrTooLarge, st.Size(), MaxPullBytes)
	}

	f, err := os.Open(checked)
	if err != nil {
		return 0, "", err
	}
	defer f.Close()

	if offset > 0 {
		if _, err := f.Seek(offset, io.SeekStart); err != nil {
			return 0, "", err
		}
	}

	limit := maxBytes
	if limit <= 0 {
		limit = MaxPullBytes
	}
	if limit > MaxPullBytes {
		limit = MaxPullBytes
	}

	h := sha256.New()
	buf := make([]byte, proto.MaxChunkBytes)
	var read int64
	seq := 0

	for read < limit {
		want := int64(len(buf))
		if remaining := limit - read; remaining < want {
			want = remaining
		}
		n, rerr := f.Read(buf[:want])
		if n > 0 {
			chunk := make([]byte, n)
			copy(chunk, buf[:n])
			read += int64(n)
			// 全文 sha 只在一次性读完整个文件时有意义
			if offset == 0 && maxBytes <= 0 {
				h.Write(chunk)
			}
			last := read >= limit || rerr == io.EOF
			if err := emit(Chunk{Seq: seq, Data: chunk, Last: last, Total: st.Size()}); err != nil {
				return 0, "", err
			}
			seq++
		}
		if rerr == io.EOF {
			break
		}
		if rerr != nil {
			return 0, "", rerr
		}
	}

	if offset == 0 && maxBytes <= 0 {
		sha = hex.EncodeToString(h.Sum(nil))
	}
	return st.Size(), sha, nil
}
