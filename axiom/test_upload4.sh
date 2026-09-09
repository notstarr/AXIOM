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

echo "====== 上传测试 - 更多typeMap格式 ======"

# 测试1: typeMap作为JSON数组字符串
echo ""
echo "--- 测试1: typeMap=[1,2] ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoodsAttachment/batchUploadFile" -X POST \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -F "file=@/tmp/test.txt" \
  -F 'bizUid=01M11DF8GW4GMWVTG4WGQA826N' \
  -F 'typeMap=[1,2]' 2>&1

# 测试2: 用JSON格式typeMap
echo ""
echo "--- 测试2: typeMap={\"attachmentType\":\"1\"} ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoodsAttachment/batchUploadFile" -X POST \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -F "file=@/tmp/test.txt" \
  -F 'bizUid=01M11DF8GW4GMWVTG4WGQA826N' \
  -F 'typeMap={"attachmentType":"1"}' 2>&1

# 测试3: 获取relationCorp信息 - 了解企业关系
echo ""
echo "--- 测试3: 获取企业关系 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/pms/api/v1/pmsUser/getRelationCorp" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{}' 2>&1 | python3 -c "
import sys,json; d=json.load(sys.stdin)
if d.get('data'):
    print(json.dumps(d['data'], indent=2, ensure_ascii=False)[:2000])
else:
    print(json.dumps(d, ensure_ascii=False)[:500])
"