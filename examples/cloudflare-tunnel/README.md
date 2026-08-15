# Putting the app on a public hostname

This is the setup behind `dashcam.joshualeaper.dev`: a Cloudflare Tunnel, so nothing is
port-forwarded and the router keeps every port shut. `cloudflared` makes an outbound
connection to Cloudflare and traffic comes back down it.

**Turn sign-in on before you create the public hostname.** Not after. Sign-in is off by
default, and in the window between the hostname resolving and the password existing, the
app is open to whoever finds it — and the first account can be claimed by whoever gets
there first. The app refuses a first claim that arrives through a proxy for exactly this
reason, but the order below is what avoids relying on that.

---

## 1. Set the password first

Open the app on your LAN, go to **Settings → Access**, set a username and password, then
tick **Require sign-in** and save. Confirm it works: open a private window, load the app,
and check you get the login page.

## 2. Create the tunnel

In the Cloudflare dashboard, **Zero Trust → Networks → Tunnels → Create a tunnel**, pick
*Cloudflared*, name it, and copy the token it gives you.

Add a public hostname to the tunnel:

| Field | Value |
| --- | --- |
| Subdomain | `dashcam` |
| Domain | `joshualeaper.dev` |
| Service type | `HTTP` |
| URL | `dashcam:8080` |

`dashcam` is the compose *service* name, which is what Docker's DNS resolves on the shared
network. The port is the one inside the container (8080), not the one published on the host.

## 3. Add cloudflared to the stack

Copy `docker-compose.tunnel.yml` next to your `docker-compose.yml` and merge the
`cloudflared` service into it, or run both:

```bash
docker compose -f docker-compose.yml -f docker-compose.tunnel.yml up -d
```

Put the token in a `.env` file beside the compose file rather than in the compose file
itself:

```
TUNNEL_TOKEN=eyJhIjoi...
```

## 4. Turn off caching for the hostname

**This one is not optional.** Cloudflare caches `.jpg` and `.mp4` by default, and its cache
key does not include your session cookie. Thumbnails and licence-plate crops are served at
sequential paths like `/media/thumbnails/00000123.jpg`, so a cached response is an object
anyone can enumerate without ever reaching the container — where the sign-in check lives.

The app marks all of it `Cache-Control: private`, which Cloudflare honours, so this is
belt and braces rather than the only defence. Add it anyway:

**Caching → Cache Rules → Create rule**

- If: `Hostname equals dashcam.joshualeaper.dev`
- Then: **Bypass cache**

If the hostname was live before you did this, run **Caching → Configuration → Purge
Everything** once.

## 5. Worth doing while you are in there

**Rate limit the login endpoint.** The app throttles by address, but doing it at the edge
means the attempts never reach your house.

**Security → WAF → Rate limiting rules**

- If: `URI Path equals /api/auth/login` and `Request Method equals POST`
- Then: block for 10 minutes after 10 requests per minute from the same IP

**Consider Cloudflare Access.** Zero Trust → Access → Applications, self-hosted, pointed at
the hostname, with a policy allowing only your own email. That puts a second, independent
sign-in in front of the app's own — belt and braces if the footage matters to you. Add a
bypass policy for `/health` if you monitor it externally.

**Block `/health` at the edge** if you do not monitor it externally. It stays
unauthenticated on purpose — the Docker healthcheck calls it with no credentials and a 401
there would restart the container forever — and while sign-in is on the app strips its
detail down to a bare status, but there is no reason to publish it at all.

---

## Things to know

**Video through the CDN.** Cloudflare's terms restrict serving a large amount of non-HTML
content — video especially — through the CDN on the free and Pro plans. Watching your own
clips occasionally is not what that is aimed at; using this as a video host for other
people is. If you plan to stream a lot, that is a conversation with Cloudflare rather than
something this app can arrange.

**HTTPS detection.** Cloudflare terminates TLS and reaches the container over plain HTTP,
so the app reads `X-Forwarded-Proto` to decide whether to mark the session cookie `Secure`
— and, over HTTPS, to name it `__Host-dashcam_session`, which locks it to this exact
hostname so no other host under `joshualeaper.dev` can write one. That header is only
trusted from a private address, which `cloudflared` on the Docker network is.

**Keeping LAN access.** The bundled compose file still publishes `8098:8080`, so
`http://SERVER-IP:8098` keeps working alongside the tunnel. If you would rather the tunnel
be the only way in, delete the `ports:` block — sessions created over plain LAN HTTP are
issued without the `Secure` flag, and dropping the port removes that path entirely.

**Uploads.** Cloudflare caps request bodies at 100 MB on the free plan. That only affects
restoring a database backup through the tunnel; do that on the LAN if your database is
larger.

**Forgotten password.** On the host:

```bash
docker compose exec dashcam entrypoint.sh recover-login set-password
docker compose exec dashcam entrypoint.sh recover-login disable    # or turn it off entirely
```

`disable` reopens a running container within about thirty seconds. No restart needed.
