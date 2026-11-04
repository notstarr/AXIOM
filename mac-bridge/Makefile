APP_NAME := AXIOMMacBridge
BINARY   := axiom-mac-bridge
VERSION  ?= 1.0.0
BUILD    := build/$(APP_NAME).app
DIST     := dist

.PHONY: all build test selftest app dmg clean

all: build

build:
	# fyne 的 darwin 驱动是 cgo，CGO_ENABLED=0 会直接编不过
	CGO_ENABLED=1 MACOSX_DEPLOYMENT_TARGET=15.0 \
	CGO_CFLAGS="-I$$(xcrun --show-sdk-path)/usr/include" \
	CGO_LDFLAGS="-F$$(xcrun --show-sdk-path)/System/Library/Frameworks" \
	go build -trimpath \
		-ldflags "-s -w -X main.version=$(VERSION)" \
		-o bin/$(BINARY) ./cmd/axiom-mac-bridge

# 第 0 步验收：不碰网络、不碰 AXIOM
selftest: build
	./bin/$(BINARY) selftest --target "/Applications/Magic (Test).app" --json

test:
	go vet ./...
	go test ./...

# 组装 .app bundle
app: build
	./scripts/make-app.sh $(VERSION)

# 组装 DMG
dmg: app
	./scripts/make-dmg.sh $(VERSION)

clean:
	rm -rf bin $(DIST)/stage $(DIST)/rw.dmg
