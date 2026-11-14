# Telephone calls

Set up **System → Voice → Telephone** with an account allowed to manage settings. Calls use
Twilio for the phone connection and your existing Voice profiles for recognition, replies and
speech. Several Twilio numbers may share a gateway and account, or use different accounts and
profiles. Check **Voice status** before a test; status reads do not wake compute.

## Costs and prerequisites

Budget for Twilio number rental, incoming minutes, verification SMS messages and any carrier or
registration fees. Model/API and compute costs still apply. Rates depend on country, number type
and your Twilio plan; check [Twilio Voice pricing](https://www.twilio.com/en-us/voice/pricing) and
[Messaging pricing](https://www.twilio.com/en-us/messaging/pricing) for your destination. A
simulation does not dial or send SMS, but does use compute and configured model services.

You need a Twilio account with an incoming voice number, working Voice profiles, and a public
HTTPS address that forwards WebSockets to the gateway's public listener (default port 8081).
User number registration also requires an SMS-capable Twilio number connected here. Trial
accounts and local messaging rules can restrict SMS destinations.

## Set up in the panel

1. Enter the Twilio **Account SID** and public HTTPS address. Enable telephone and save setup.
   Port changes remain pending until the voice gateway restarts; the page names the in-force port.
2. Save the **auth token**. Optionally provide an API key SID and secret (recommended for API
   operations). The auth token remains required to verify Twilio signatures. Fields stay empty
   after saving; empty submissions retain saved values. Check credentials to verify API access.
3. Create a phone account, then open its **Users** page to choose models, tools and limits.
   Creating an account does not grant unrestricted model access. Several numbers may share it.
4. Choose and configure a public route below. Set its resulting HTTPS address in setup and use
   **Check from outside**. This signed HEAD probe validates TLS and forwarding without a call or
   compute reservation. It requires a reasonably synchronized system clock.
5. **Find my numbers**, **Add** each number, and **Connect** it. Connect sets its Voice URL and
   POST method in Twilio. The Manual disclosure shows the exact values for console setup.
6. Choose each number's account, mode, profile, optional SoundRoom voice, greeting and rate limit.
   New numbers start disabled with Allowlist access. Save caller access before enabling calls.
7. **Simulate a call**, using a caller admitted by that number's policy. The test verifies a signed
   webhook, signed media stream, spoken reply and playback marks. It never dials a phone.

The bounded [Twilio API client](https://www.twilio.com/docs/usage/api) uses the saved API key when
present, otherwise the auth token. It only manages the account check,
[incoming number configuration](https://www.twilio.com/docs/phone-numbers/api/incomingphonenumber-resource)
and [verification messages](https://www.twilio.com/docs/messaging/api/message-resource).
Credentials are atomic owner-only files, read when needed; rotation needs no restart. Removing
the auth token stops new phone admissions. Rotating a phone account key replaces its existing
file; calls keep their normal account permissions and usage attribution.

## Public routes

Choose one route. The panel detects installed tools and shows examples using your current public
port. Commands are for you to run with your normal network administration workflow. They are
never run with elevated rights by the panel. Use a domain you control and a valid TLS certificate.
Forward to the **public** listener only: private admin, status and registration routes must stay
on the private network. No forwarded host or protocol headers override the signed public URLs.

### Own reverse proxy

For Caddy on the gateway host, replace the example domain. WebSocket upgrades are automatic;
`stream_timeout` bounds long connections. See [Caddy reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).

```caddyfile
voice.example.com {
    reverse_proxy 127.0.0.1:8081 {
        stream_timeout 1h
    }
}
```

For nginx, put this inside your HTTPS server. Keep normal certificate configuration. nginx
requires explicit upgrade headers and a timeout suitable for calls; see its
[WebSocket proxy documentation](https://nginx.org/en/docs/http/websocket.html).

```nginx
location / {
    proxy_pass http://127.0.0.1:8081;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 1h;
    proxy_send_timeout 1h;
}
```

If the proxy runs on a VPS, replace `127.0.0.1` with the gateway's reachable private address and
restrict that path to the proxy. Do not point the public proxy at the admin listener.

### Tailscale Funnel

Install and sign in to Tailscale, and enable Funnel for this node in the tailnet policy. Run:

```sh
tailscale funnel --bg http://127.0.0.1:8081
```

Use the resulting `https://node.your-tailnet.ts.net` address as the public address. The panel
shows your node's DNS name when available. This is public Funnel, rather than private Serve;
Twilio must reach it without joining your tailnet. Consult the
[Funnel command reference](https://tailscale.com/docs/reference/tailscale-cli/funnel) for policy
requirements and stopping or changing forwarding.

### Cloudflare Tunnel

Install `cloudflared`, create a named tunnel and configure DNS for your domain. Set ingress:

```yaml
tunnel: YOUR_TUNNEL_ID
credentials-file: /path/to/YOUR_TUNNEL_ID.json
ingress:
  - hostname: voice.example.com
    service: http://127.0.0.1:8081
  - service: http_status:404
```

Run your named tunnel through your usual service management. Use `https://voice.example.com`
in setup. Do not put an interactive Cloudflare Access login in front of Twilio. Follow the
[Cloudflare configuration guide](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/configuration-file/),
including its required final catch-all rule.

## Caller access and identity

**Anyone** admits public callers as the number's phone account. It exposes your calling and
model budget to anyone who has the number. **Allowlist** accepts configured callers and, when
enabled, verified users' registered numbers. An administrator chooses the account for each
manual entry. **PIN** permits a known caller to skip the challenge and asks others for a saved
4–12 digit keypad PIN. Three incorrect attempts end the call. PINs are salted hashes and never
shown again. Per-number and per-caller hourly limits are checked before compute is contacted.

Caller ID can be spoofed. Full Twilio attestation (`TN-Validation-Passed-A`) is required by
default for matches that select a personal account. Networks do not always supply it. Without
full attestation, a saved PIN can admit the caller as the shared phone account; it never permits
impersonating the matched personal account. With no usable PIN the call is refused. Review the
attestation setting deliberately before allowing a number to select someone else's account.

## Register your own phone

An account with **Call in by telephone as yourself** (`front-phone`) sees **My account → Phone
numbers**. Enter an E.164 number, request an SMS, then enter its six-digit code. Up to three
numbers may be registered. Codes expire in ten minutes, allow five attempts, and can be resent
after sixty seconds, at most three times an hour per account and number. Another user's
registered number cannot be claimed. SMS delivery failures do not reveal who owns a number.

The section names the SMS sender or explains why none is available. Administrators can connect
an SMS-capable number and check its credentials. A verified number is admitted only by
telephone numbers accepting registered users and subject to caller-ID attestation. Calls get
short-lived limited sessions with the user's own models, tools, limits and usage attribution.
Removing the number, disabling the user or removing `front-phone` revokes telephone access.
The owner sees their full numbers; administrators see masked registration rows and can revoke
them. Account deletion removes registration and challenge records.

## Existing machine-file installations

An existing `[twilio]` table continues to work unchanged, including custom token/account key
paths and exact signed webhook/stream URLs. The panel labels it **Set in the machine file**.
Use **Import to site settings**, enter the actual Twilio destination number and confirm. Import
keeps Anyone access and the original account/profile; it records references to existing secret
files without copying their contents. It does not edit your machine file. Only the specified
number is accepted after import, replacing legacy catch-all destination routing.

Custom signed URLs (including supported alias paths and query strings) remain in force while
the imported public address and Account SID are unchanged. Changing either selects the standard
derived URLs; reconnect the number in that case. Existing site settings are never overwritten
by import. Back up settings, the accounts store and the complete keys directory together. The
normal uninstall policy for `keys/` also applies to telephone credentials.

## Troubleshooting

| Verdict or result | Action |
|---|---|
| Gateway not answering | Inspect voice-gateway.service and System logs on the control node. |
| Off | Enable telephone in setup, then enable the intended number. |
| Save auth token / credentials unavailable | Save the correct auth token; blank fields retain it. Check API key pairing and Account SID. |
| Public listener not bound | Check pending port/host changes and service logs; restart through the established OWG workflow when authorized. |
| No enabled numbers | Add a number, configure its account and caller access, then enable it. |
| Account or key unavailable / missing permission | Open the phone account in Users; check enabled state, voice access, models and key file. |
| Missing model / compute asleep / synthesis unavailable | Inspect the named profile and Components. Status checks do not warm models or wake compute. |
| Public probe DNS/private-address error | Use public DNS, HTTPS and a public route; loopback/private URLs are refused. |
| Public probe HTTP 301/302 | Set the final HTTPS address directly; probes do not follow redirects. |
| Public probe HTTP 403 | Check the auth token, exact signed URL and system clock; do not rewrite query strings. |
| Public probe HTTP 404 or no connection | Check route destination, enabled telephone, TLS/firewall and in-force public port. |
| Webhook passed but stream/reply failed | Check WebSocket upgrade/timeout, caller policy, profile readiness and model capacity. |
| not_listed / unattested / bad_pin | Review caller access and attestation; a shared PIN does not select a personal account. |
| rate_limited | Wait for the hourly window or adjust the number's limit within your budget. |
| SMS unavailable / failed | Connect an SMS-capable number here; check Twilio messaging permissions, trial restrictions and destination rules. |

Voice status shows active calls with **End call**, reasons for each entry/profile, latency in ms
against targets, and masked recent phone attempts. Phone status and events contain no utterance
text or audio. Activity is bounded in memory and clears on gateway restart. Use the linked Event
log and System logs for operational details; never paste credentials or PINs into reports.

Software deployment remains **`owg update; owg restart`**, only when requested by the owner.
After deployment, verify the effective running checkout/branch, status commit and uptime, then a
signed simulation or real call. A successful Git update or restart alone does not prove the
updated source is running. This guide does not authorize a deployment.
