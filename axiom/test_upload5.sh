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

echo "====== 更多上传测试 ======"

# 测试1: typeMap用不同的key
echo ""
echo "--- 测试1: typeMap用 list 格式 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoodsAttachment/batchUploadFile" -X POST \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -F "file=@/tmp/test.txt" \
  -F 'bizUid=01M11DF8GW4GMWVTG4WGQA826N' \
  -F 'typeMap={}' 2>&1

# 测试2: 创建附件 - 改用POST JSON
echo ""
echo "--- 测试2: 附件创建 - POST JSON ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoodsAttachment/batchUploadFile" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"bizUid":"01M11DF8GW4GMWVTG4WGQA826N","typeMap":{}}' 2>&1

# 测试3: 获取企业关系 - GET
echo ""
echo "--- 测试3: 获取企业关系 GET ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/pms/api/v1/pmsUser/getRelationCorp" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" 2>&1 | python3 -c "
import sys,json; d=json.load(sys.stdin)
if d.get('data'):
    print(json.dumps(d['data'], indent=2, ensure_ascii=False)[:3000])
else:
    print(json.dumps(d, ensure_ascii=False)[:500])
"

# 测试4: 获取用户信息
echo ""
echo "--- 测试4: 获取用户信息 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/pms/api/v1/pmsUser/getUserByAccount" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"account":"19129430218"}' 2>&1 | python3 -c "
import sys,json; d=json.load(sys.stdin)
if d.get('data'):
    print(json.dumps(d['data'], indent=2, ensure_ascii=False)[:2000])
else:
    print(json.dumps(d, ensure_ascii=False)[:500])
"