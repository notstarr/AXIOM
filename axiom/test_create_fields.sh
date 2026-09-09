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

# === 用有效字段测试create ===
echo "====== 尝试用不同字段名调用create ======"

# 尝试1: 使用list响应中的字段名
echo ""
echo "--- 尝试1: 使用codeTs作为商品编码 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/create" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"materialNo":"TEST002","goodsName":"test商品","firstUnit":"035","dclUnit":"035","codeTs":"0101210010","goodsModel":"0:品牌类型|1:出口享惠情况","regulatoryCondition":"AFEB","tradeBan":"出口","ciqType":"P/Q","operationType":"1","currency":"142"}' 2>&1

# 尝试2: 使用goodsCode字段
echo ""
echo "--- 尝试2: 使用goodsCode字段 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/create" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"materialNo":"TEST002","goodsName":"test商品","firstUnit":"035","dclUnit":"035","goodsCode":"0101210010"}' 2>&1

# 尝试3: 尝试修改现有记录
echo ""
echo "--- 尝试3: 使用update端点 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/update" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"uid":"01M119DDEB87DFG3M6AX32YG7P","goodsName":"test商品更新"}' 2>&1