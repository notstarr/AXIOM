#!/bin/bash
set -e

# 获取Token
TOKEN=$(curl -sk "http://longnows-gts.t.kkgroup.cn/pms/api/v1/pmsUser/login" -X POST \
  -H "Content-Type: application/json" \
  -d '{"account":"19129430218","password":"any"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['accessToken'])")

echo "Token获取成功: ${TOKEN:0:30}..."

# 签名函数
sign_request() {
  REQUEST_ID=$(uuidgen | tr '[:upper:]' '[:lower:]')
  TIMESTAMP=$(date +%s000)
  NONCE=$(echo -n "${REQUEST_ID}${TIMESTAMP}" | md5)
  echo "$REQUEST_ID|$TIMESTAMP|$NONCE"
}

# === 测试1: 获取企业商品列表，了解数据结构 ===
echo ""
echo "====== 1. 获取企业商品列表 ======"
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
    if isinstance(items, dict):
        for k,v in items.items():
            if isinstance(v, list):
                print(f'  {k}: count={len(v)}')
                for item in v[:2]:
                    print(f'    {json.dumps(item, ensure_ascii=False)[:300]}')
            else:
                print(f'  {k}: {str(v)[:100]}')
    elif isinstance(items, list):
        print(f'  count={len(items)}')
        for item in items[:2]:
            print(f'    {json.dumps(item, ensure_ascii=False)[:300]}')
"