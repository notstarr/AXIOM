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

echo "====== 文件上传测试 - 带typeMap ======"

# 测试1: 用JSON格式typeMap
echo ""
echo "--- 测试1: typeMap=1 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoodsAttachment/batchUploadFile" -X POST \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -F "file=@/tmp/test.txt" \
  -F 'bizUid=01M11DF8GW4GMWVTG4WGQA826N' \
  -F 'typeMap=1' 2>&1

# 测试2: typeMap JSON字符串
echo ""
echo "--- 测试2: typeMap={\"type\":\"1\"} ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoodsAttachment/batchUploadFile" -X POST \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -F "file=@/tmp/test.txt" \
  -F 'bizUid=01M11DF8GW4GMWVTG4WGQA826N' \
  -F 'typeMap={"type":"1"}' 2>&1

# 测试3: 不带file参数，只传bizUid和typeMap
echo ""
echo "--- 测试3: 不带file ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoodsAttachment/batchUploadFile" -X POST \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -F 'bizUid=01M11DF8GW4GMWVTG4WGQA826N' \
  -F 'typeMap=1' 2>&1

# 测试4: 上传PHP文件 带typeMap
echo ""
echo "--- 测试4: 上传PHP文件 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoodsAttachment/batchUploadFile" -X POST \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -F "file=@/tmp/test.php;filename=test.php;type=text/plain" \
  -F 'bizUid=01M11DF8GW4GMWVTG4WGQA826N' \
  -F 'typeMap=1' 2>&1