# Crawler Output Schema v1

This document defines the crawler output format for business modeling and traffic testing.

Goals:
- model the business pages and user actions, not just raw URLs
- inventory APIs together with parameter provenance and auth requirements
- map page load and user actions to observed API traffic
- support incremental crawler writes without requiring full knowledge upfront

Design rules:
- prefer arrays with stable `id` fields over top-level display-name keys
- separate machine identifiers from human-readable names
- allow partial records first, then enrich later
- preserve unknown or missing information explicitly
- keep the schema friendly to both humans and downstream tooling

## Files

The crawler produces three YAML files:

1. `site_map.yaml`
2. `api_inventory.yaml`
3. `page_api_map.yaml`

## site_map.yaml

Purpose:
- describe the discovered business pages
- record how pages are reached from other pages
- record the main user actions available on each page
- expose key page-level data useful for later modeling

Suggested schema:

```yaml
version: 1
target:
  url: https://example.com/
  domain: example.com
  started_at: "2026-06-17T17:00:00Z"

pages:
  - id: page_home
    name: 首页
    url: https://example.com/
    path: /
    page_type: landing
    parent_id: null
    discovered_via:
      type: entry
      source_page_id: null
      trigger_label: null
    requires_auth: false
    business_role: guest
    preconditions: []
    actions:
      - id: action_home_search
        name: 搜索
        type: input_submit
        target_hint: 顶部搜索框
        status: discovered
      - id: action_home_login
        name: 登录
        type: click
        target_hint: 顶部登录按钮
        status: explored
      - id: action_home_register
        name: 注册
        type: click
        target_hint: 顶部注册按钮
        status: discovered
    key_data:
      - name: featured_goods_id
        source: dom
        note: 首页商品卡片 data-id
    related_api_ids:
      - api_portal_subsystems
    notes: []
    exhausted: false
```

Field notes:
- `page_type`: page category such as `landing`, `auth`, `list`, `detail`, `form`, `dashboard`
- `parent_id`: the page from which this page was reached
- `discovered_via`: how the crawler found this page
- `actions`: main user-visible actions on this page
- `key_data`: page-level data useful for later flows or API correlation
- `related_api_ids`: strong page-to-API references
- `exhausted`: whether the page has no meaningful unexplored actions left

## api_inventory.yaml

Purpose:
- record all observed APIs
- record authentication, parameters, parameter provenance, and dependencies
- support replay, fuzzing, and gap analysis for later traffic testing

Suggested schema:

```yaml
version: 1
target:
  url: https://example.com/
  domain: example.com
  started_at: "2026-06-17T17:00:00Z"

apis:
  - id: api_goods_detail
    method: GET
    path: /api/goods/detail
    full_url_example: https://example.com/api/goods/detail?goods_id=456346
    purpose: 获取商品详情
    protocol: https
    content_type: application/json
    status_codes_seen: [200]
    auth:
      required: false
      type: none
      example: null
    seen_on_pages:
      - page_id: page_goods_detail
        page_url: https://example.com/goods/456346
    params:
      - name: goods_id
        location: query
        required: true
        value_example: "456346"
        source:
          type: dom
          from: 商品卡片 data-id
        data_type: string
        notes: []
    response_keys:
      - goods_id
      - stock
      - price
      - shop_id
    produces_data:
      - name: goods_id
        path: response.goods_id
      - name: shop_id
        path: response.shop_id
    depends_on: []
    missing_inputs: []
    notes: []
```

Field notes:
- `purpose`: business meaning of the API
- `status_codes_seen`: all observed response status codes
- `auth`: auth requirement and a representative example
- `params[].source`: the most important field for business-chain reconstruction
- `produces_data`: response fields that can feed later APIs or business steps
- `depends_on`: upstream APIs or prerequisite data
- `missing_inputs`: unresolved values the crawler could not derive

## page_api_map.yaml

Purpose:
- map pages to the APIs they trigger
- separate page-load APIs from user-action APIs
- preserve the action context that caused each API request

Suggested schema:

```yaml
version: 1
target:
  url: https://example.com/
  domain: example.com
  started_at: "2026-06-17T17:00:00Z"

mappings:
  - id: map_goods_detail
    page_id: page_goods_detail
    page_name: 商品详情页
    page_url: https://example.com/goods/456346

    on_load:
      - api_id: api_goods_detail
        method: GET
        path: /api/goods/detail
        note: 商品信息
      - api_id: api_goods_reviews
        method: GET
        path: /api/goods/reviews
        note: 商品评价

    actions:
      - action_id: action_goods_add_cart
        action_name: 点击加购
        trigger_type: click
        trigger_label: 加购按钮
        target_hint: 商品详情页主按钮
        result:
          navigation: false
          status: success
        apis:
          - api_id: api_cart_add
            method: POST
            path: /api/cart/add
        notes: []

      - action_id: action_goods_quantity_change
        action_name: 修改数量
        trigger_type: input
        trigger_label: 数量输入框
        target_hint: 数量选择器
        result:
          navigation: false
          status: success
        apis:
          - api_id: api_goods_stock
            method: GET
            path: /api/goods/stock
        notes:
          - 可能为轮询
```

Field notes:
- `on_load`: APIs triggered automatically when the page loads
- `actions`: APIs triggered by explicit user actions
- `result.status`: one of `success`, `failure`, `noop`, `denied`
- `navigation`: whether the action caused a page transition
- `target_hint`: human-readable identity for the element or control

## Suggested enums

`page_type` values:
- `landing`
- `auth`
- `list`
- `detail`
- `form`
- `dashboard`
- `checkout`
- `cart`
- `support`
- `unknown`

`action.type` and `trigger_type` values:
- `click`
- `input`
- `input_submit`
- `form_submit`
- `tab_click`
- `toggle`
- `navigation`
- `select`
- `unknown`

`action.status` values:
- `discovered`
- `explored`
- `skipped`
- `failed`

`result.status` values:
- `success`
- `failure`
- `noop`
- `denied`

`source.type` values:
- `entry`
- `dom`
- `dom_text`
- `dom_attribute`
- `user_input`
- `user_choice`
- `api_response`
- `cookie`
- `local_storage`
- `session_storage`
- `unknown`

## Incremental write strategy

The crawler should write incrementally using this order:

1. create the three skeleton files
2. immediately write the entry page into `site_map.yaml`
3. when a page reveals actions, update that page's `actions`
4. after every action, immediately update `page_api_map.yaml`
5. when a new API appears, immediately update `api_inventory.yaml`
6. when information is incomplete, write `unknown` or partial fields first and enrich later

Do not wait for a page, branch, or full crawl to finish before recording discovered structure.

## Minimum first-write requirements

The crawler does not need full fidelity in the first record, but it should try to write these fields immediately:

- for pages: `id`, `name`, `url`, `path`
- for actions: `id`, `name`, `type`, `status`
- for APIs: `method`, `path`
- for params when known: `value_example`, `source`
- for page-to-API relations: page, action, API link

Fields that can be backfilled later include:

- `purpose`
- `business_role`
- `preconditions`
- `response_keys`
- `produces_data`
- `missing_inputs`
- `notes`
