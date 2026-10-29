## Credential extraction (axiom fork only)

This command does not exist upstream, and upstream's website debugging
deliberately redacts `cookie` / `set-cookie`. It exists here because session
credentials are a legitimate output of authorized penetration testing.

```sh
bsk credentials --domain example.com --session <id> [--json]
```

Returns every cookie the browser holds for that domain — **including `HttpOnly`
values, which page JavaScript can never read** — plus the localStorage and
sessionStorage of every already-open tab on that domain, plus an assembled
`Cookie` request header.

Scope rules:

- `--domain` must cover a target the operator is authorized to assess. Pulling
  cookies for unrelated domains is out of scope even though the command allows it.
- Matching is suffix-exact and does **not** include parent domains:
  `chrome.cookies.getAll({ domain })` returns cookies whose domain equals the
  value or is a subdomain of it. A session cookie set on `.example.com` is *not*
  returned for `--domain wedata.example.com`. Pass the registrable parent domain
  and filter by cookie name instead.
- Storage is read from **already-open tabs**, so navigate to the target before
  calling. Tabs are visited in ascending id order; same-origin sessionStorage
  collisions keep the first tab's value.
- An empty result after retrying with the parent domain means the session is not
  in a cookie — look in localStorage, or accept that it belongs to another domain.

Write results to the engagement's evidence directory; do not paste credential
material into report or finding bodies.
