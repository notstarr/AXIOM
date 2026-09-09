#!/bin/bash

TOKEN=$(curl -sk "http://longnows-gts.t.kkgroup.cn/pms/api/v1/pmsUser/login" -X POST \
  -H "Content-Type: application/json" \
  -d '{"account":"19129430218","password":"any"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['accessToken'])")

echo "Token获取成功: ${TOKEN:0:30}..."

sign_request() {
  REQUEST_ID=$(uuidgen | tr '[:upper:]' '[:lower:]')
  TIMESTAMP=$(date +%s000)
  NONCE=$(echo -n "${REQUEST_ID}${TIMESTAMP}" | md5)
  echo "$REQUEST_ID|$TIMESTAMP|$NONCE"
}

# === 获取完整商品列表数据 ===
echo ""
echo "====== 完整商品列表 ======"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{}' 2>&1 | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(f'code={d.get(\"code\")}')
if d.get('data'):
    items = d['data']
    if isinstance(items, list):
        print(f'count={len(items)}')
        for item in items:
            print(json.dumps(item, indent=2, ensure_ascii=False))
    elif isinstance(items, dict):
        print(json.dumps(items, indent=2, ensure_ascii=False)[:2000])
"

echo ""
echo "====== 商品详情查询 ======"
# 尝试查询商品详情
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/get" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"uid":"01M119DDEB87DFG3M6AX32YG7P"}' 2>&1 | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(f'code={d.get(\"code\")}')
if d.get('data'):
    print(json.dumps(d['data'], indent=2, ensure_ascii=False)[:2000])
else:
    print(f'message={d.get(\"message\")}')
"