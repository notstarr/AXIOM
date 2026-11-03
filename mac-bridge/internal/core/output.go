package core

import "fmt"

// HeadTail 保留一条流的开头和结尾，中间按字节数标注丢掉了多少。
//
// 为什么不直接全收：一条 `rea analyze` 或 Ghidra 的输出可以轻松上百兆，
// 全塞进 ToolResult 会直接把模型的上下文打爆。而真正有信息量的通常是
// 开头的配置/版本和结尾的结论/报错，中间那一大段是进度条。
//
// 为什么不截断成"前 N 字节"：那样会把结论（在结尾）整段丢掉。
type HeadTail struct {
	headLimit int
	tailLimit int
	head      []byte
	tail      []byte
	total     int64
}

func NewHeadTail(headLimit, tailLimit int) *HeadTail {
	if headLimit <= 0 {
		headLimit = 128 << 10
	}
	if tailLimit <= 0 {
		tailLimit = 128 << 10
	}
	return &HeadTail{headLimit: headLimit, tailLimit: tailLimit}
}

// Write 实现 io.Writer。
func (b *HeadTail) Write(p []byte) (int, error) {
	n := len(p)
	b.total += int64(n)

	if len(b.head) < b.headLimit {
		room := b.headLimit - len(b.head)
		if n <= room {
			b.head = append(b.head, p...)
		} else {
			b.head = append(b.head, p[:room]...)
		}
	}

	if n > 0 {
		b.tail = append(b.tail, p...)
		if over := len(b.tail) - b.tailLimit; over > 0 {
			// 只丢最前面的，保留尾巴
			b.tail = append(b.tail[:0], b.tail[over:]...)
		}
	}
	return n, nil
}

// TotalBytes 真实流过的总字节数。
func (b *HeadTail) TotalBytes() int64 { return b.total }

// Render 拼出最终文本，并返回中间被丢掉的字节数。
func (b *HeadTail) Render() (text string, dropped int64) {
	total := b.total
	headLen := int64(len(b.head))
	if total <= headLen {
		return string(b.head), 0
	}

	tl := int64(len(b.tail))
	// head 覆盖 [0, headLen)，tail 覆盖 [total-tl, total)
	gap := total - headLen - tl
	if gap <= 0 {
		// 两段有重叠，取 tail 中超出 head 的部分
		extra := b.tail[tl-(total-headLen):]
		return string(b.head) + string(extra), 0
	}
	marker := fmt.Sprintf("\n…[truncated %d bytes]…\n", gap)
	return string(b.head) + marker + string(b.tail), gap
}
