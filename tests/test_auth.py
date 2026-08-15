"""Optional sign-in, from both sides of the gate.

The cases here are the ones where being wrong is expensive. Two of them are about letting
people *in*: the login page has to be reachable while signed out, or the feature bricks the
deployment, and switching sign-in on without an account has to be refused rather than
obeyed. The rest are about keeping people out -- of ``/api``, of ``/media``, of ``/stream``,
and of a word list against the login endpoint.

The client fixture talks to the module-level ``app.main:app`` through the real middleware
stack, which is the only way to test a gate at all: it is not a dependency any handler
declares, and calling the handlers directly would sail straight past it.
"""

from __future__ import annotations

import pytest

from app.auth import ratelimit
from app.auth.service import COOKIE_NAME

PASSWORD = "correct-horse-battery"


async def _configure_account(client, *, username: str = "joshua", password: str = PASSWORD):
    response = await client.put(
        "/api/auth/credential", json={"username": username, "password": password}
    )
    assert response.status_code == 204, response.text


async def _require_sign_in(client) -> None:
    response = await client.put("/api/settings", json={"values": {"security.require_login": True}})
    assert response.status_code == 200, response.text


async def _sign_in(client, *, password: str = PASSWORD, remember: bool = False):
    return await client.post(
        "/api/auth/login",
        json={"username": "joshua", "password": password, "remember": remember},
    )


async def _secure_and_sign_out(client) -> None:
    """Set up an account, require sign-in, and leave the client signed out.

    The sign-out is not decoration. Setting the password issues the caller a fresh session
    so that changing it does not bounce them to the login page, and simply dropping the
    cookie would leave that row behind to be counted by the tests about sessions.
    """
    await _configure_account(client)
    await _require_sign_in(client)
    assert (await client.post("/api/auth/logout")).status_code == 204
    client.cookies.clear()


class TestItIsOffUntilAskedFor:
    """The default deployment is a trusted LAN and must stay zero-configuration."""

    async def test_nothing_is_challenged_by_default(self, client):
        assert (await client.get("/api/status")).status_code == 200
        assert (await client.get("/api/settings")).status_code == 200

    async def test_state_reports_it_is_off(self, client):
        body = (await client.get("/api/auth/state")).json()
        assert body == {
            "required": False,
            "authenticated": False,
            "username": None,
            "configured": False,
            "misconfigured": False,
            "can_claim_account": True,
            "remember_days": 30,
        }

    async def test_it_cannot_be_switched_on_without_an_account(self, client):
        """The one setting that could lock the operator out of their own footage."""
        response = await client.put(
            "/api/settings", json={"values": {"security.require_login": True}}
        )
        assert response.status_code == 400
        assert "username and password" in response.json()["detail"]

        # And it did not take effect anyway.
        assert (await client.get("/api/status")).status_code == 200


class TestTheGate:
    @pytest.fixture
    async def secured(self, client):
        await _secure_and_sign_out(client)
        return client

    async def test_data_routes_are_refused(self, secured):
        for path in ("/api/status", "/api/settings", "/api/recordings", "/media/x.jpg"):
            response = await secured.get(path)
            assert response.status_code == 401, f"{path} was not gated"
            assert response.json()["detail"] == "Sign in to continue."

    async def test_streaming_and_docs_are_refused(self, secured):
        assert (await secured.get("/stream/1")).status_code == 401
        assert (await secured.get("/api/openapi.json")).status_code == 401

    async def test_the_login_page_can_still_load(self, secured):
        """Everything the browser fetches before it can offer a password.

        If any of these were gated the app would answer 401 to a visitor who has no way to
        stop being one, which is the failure mode this whole feature has to avoid.
        """
        assert (await secured.get("/")).status_code == 200
        assert (await secured.get("/login")).status_code == 200
        assert (await secured.get("/api/auth/state")).status_code == 200

    async def test_the_healthcheck_still_answers(self, secured):
        """Docker restarts the container on a failing healthcheck, forever."""
        assert (await secured.get("/health")).status_code in {200, 503}

    async def test_state_tells_a_stranger_only_that_a_password_is_wanted(self, secured):
        assert (await secured.get("/api/auth/state")).json() == {
            "required": True,
            "authenticated": False,
            "username": None,
            "configured": False,
            "misconfigured": False,
            "can_claim_account": False,
            "remember_days": None,
        }


class TestSigningIn:
    @pytest.fixture
    async def secured(self, client):
        await _secure_and_sign_out(client)
        return client

    async def test_the_wrong_password_is_refused(self, secured):
        response = await _sign_in(secured, password="not-it")
        assert response.status_code == 401
        assert response.json()["detail"] == "Incorrect username or password."
        assert COOKIE_NAME not in secured.cookies

    async def test_an_unknown_username_says_exactly_the_same_thing(self, secured):
        """Otherwise the endpoint hands out a list of valid account names."""
        wrong_name = await secured.post(
            "/api/auth/login", json={"username": "nobody", "password": PASSWORD}
        )
        wrong_password = await _sign_in(secured, password="not-it")
        assert wrong_name.status_code == wrong_password.status_code == 401
        assert wrong_name.json() == wrong_password.json()

    async def test_the_right_password_opens_everything(self, secured):
        response = await _sign_in(secured)
        assert response.status_code == 200, response.text
        assert response.json()["authenticated"] is True
        assert COOKIE_NAME in secured.cookies
        assert (await secured.get("/api/status")).status_code == 200

    async def test_without_remember_me_the_cookie_dies_with_the_browser(self, secured):
        response = await _sign_in(secured, remember=False)
        cookie = response.headers["set-cookie"]
        assert "Max-Age" not in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie.replace("SameSite=Lax", "SameSite=lax")

    async def test_remember_me_lasts_the_configured_number_of_days(self, secured):
        response = await _sign_in(secured, remember=True)
        cookie = response.headers["set-cookie"]
        max_age = int(cookie.split("Max-Age=")[1].split(";")[0])
        assert max_age == pytest.approx(30 * 86400, abs=120)

    async def test_remember_days_is_honoured_when_changed(self, secured):
        await _sign_in(secured)
        assert (
            await secured.put("/api/settings", json={"values": {"security.remember_days": 7}})
        ).status_code == 200
        secured.cookies.clear()
        response = await _sign_in(secured, remember=True)
        max_age = int(response.headers["set-cookie"].split("Max-Age=")[1].split(";")[0])
        assert max_age == pytest.approx(7 * 86400, abs=120)

    async def test_the_cookie_is_marked_secure_behind_a_tunnel(self, secured):
        """Cloudflare terminates TLS and reaches the container over plain HTTP."""
        plain = await _sign_in(secured)
        secured.cookies.clear()
        forwarded = await secured.post(
            "/api/auth/login",
            json={"username": "joshua", "password": PASSWORD},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert "Secure" not in plain.headers["set-cookie"]
        assert "Secure" in forwarded.headers["set-cookie"]

    async def test_signing_out_revokes_the_session_rather_than_just_the_cookie(self, secured):
        await _sign_in(secured)
        token = secured.cookies[COOKIE_NAME]
        assert (await secured.post("/api/auth/logout")).status_code == 204
        assert (await secured.get("/api/status")).status_code == 401

        # Replaying the cookie the browser was told to drop must not get back in.
        replayed = await secured.get("/api/status", headers={"Cookie": f"{COOKIE_NAME}={token}"})
        assert replayed.status_code == 401

    async def test_an_invented_cookie_is_refused(self, secured):
        response = await secured.get(
            "/api/status", headers={"Cookie": f"{COOKIE_NAME}=not-a-real-token"}
        )
        assert response.status_code == 401


class TestApiClients:
    """Home Assistant polls the ingest sensor and cannot hold a cookie."""

    @pytest.fixture
    async def secured(self, client):
        await _secure_and_sign_out(client)
        return client

    async def test_basic_credentials_are_accepted(self, secured):
        response = await secured.get("/api/status", auth=("joshua", PASSWORD))
        assert response.status_code == 200

    async def test_wrong_basic_credentials_are_refused_with_a_challenge(self, secured):
        response = await secured.get("/api/status", auth=("joshua", "not-it"))
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Basic")

    async def test_a_browser_is_never_sent_a_native_password_box(self, secured):
        """A `WWW-Authenticate` header here would put Chrome's own dialog in front of the
        login page, and there is no way to sign out of one."""
        response = await secured.get("/api/status")
        assert response.status_code == 401
        assert "www-authenticate" not in response.headers


class TestBruteForce:
    @pytest.fixture
    async def secured(self, client):
        await _secure_and_sign_out(client)
        return client

    async def test_repeated_failures_are_throttled(self, secured):
        for _ in range(ratelimit.KEY_LIMIT):
            assert (await _sign_in(secured, password="not-it")).status_code == 401
        blocked = await _sign_in(secured, password="not-it")
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) > 0

        # The throttle is not something the correct password talks its way past.
        assert (await _sign_in(secured)).status_code == 429

    async def test_a_successful_sign_in_forgives_earlier_typos(self, secured):
        for _ in range(ratelimit.KEY_LIMIT - 1):
            await _sign_in(secured, password="not-it")
        assert (await _sign_in(secured)).status_code == 200
        secured.cookies.clear()
        for _ in range(ratelimit.KEY_LIMIT - 1):
            assert (await _sign_in(secured, password="not-it")).status_code == 401


class TestManagingTheAccount:
    @pytest.fixture
    async def secured(self, client):
        await _secure_and_sign_out(client)
        await _sign_in(client, remember=True)
        return client

    async def test_sessions_are_listed_with_the_current_one_marked(self, secured):
        sessions = (await secured.get("/api/auth/sessions")).json()
        assert len(sessions) == 1
        assert sessions[0]["current"] is True
        assert sessions[0]["remembered"] is True

    async def test_changing_the_password_needs_the_old_one(self, secured):
        refused = await secured.put(
            "/api/auth/credential", json={"username": "joshua", "password": "a-new-password"}
        )
        assert refused.status_code == 403
        # And the old password still works, so nothing was half-applied.
        assert (await secured.get("/api/status")).status_code == 200

    async def test_changing_the_password_signs_other_browsers_out(self, secured, client):
        import httpx
        from httpx import ASGITransport

        from app.main import app

        # A second browser, holding its own thirty-day cookie.
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as other:
            assert (await _sign_in(other, remember=True)).status_code == 200
            assert (await other.get("/api/status")).status_code == 200

            changed = await secured.put(
                "/api/auth/credential",
                json={
                    "username": "joshua",
                    "password": "a-new-password",
                    "current_password": PASSWORD,
                },
            )
            assert changed.status_code == 204, changed.text

            assert (await other.get("/api/status")).status_code == 401
        # The browser that made the change keeps working.
        assert (await secured.get("/api/status")).status_code == 200

    async def test_revoke_all_spares_the_browser_asking(self, secured):
        import httpx
        from httpx import ASGITransport

        from app.main import app

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as other:
            await _sign_in(other)
            result = (await secured.post("/api/auth/sessions/revoke-all")).json()
            assert result["revoked"] == 1
            assert (await other.get("/api/status")).status_code == 401
        assert (await secured.get("/api/status")).status_code == 200

    async def test_clearing_the_account_switches_sign_in_off_with_it(self, secured):
        """The pair that must move together. Leaving the switch on with nothing to sign in
        against is the only state that locks the operator out of their own recordings."""
        response = await secured.post(
            "/api/auth/credential/clear", json={"current_password": PASSWORD}
        )
        assert response.status_code == 204, response.text

        secured.cookies.clear()
        assert (await secured.get("/api/status")).status_code == 200
        state = (await secured.get("/api/auth/state")).json()
        assert state["required"] is False
        assert state["configured"] is False


class TestTheEdgeCannotAnswerForUs:
    """A CDN caches by URL, and its cache key does not include the session cookie.

    Thumbnails and plate crops are `.jpg` at sequential zero-padded ids, which Cloudflare
    caches by default. Served `public`, one signed-in look at the recordings grid would
    have filled the edge with a week of footage stills and licence plates that anyone could
    then enumerate — and the gate cannot refuse a request that never reaches it.
    """

    async def test_media_is_never_cached_publicly(self, client, app_config):
        image = app_config.media_dir / "thumbnails" / "00000001.jpg"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"\xff\xd8\xff\xd9")

        response = await client.get("/media/thumbnails/00000001.jpg")
        assert response.status_code == 200, response.text
        cache = response.headers["cache-control"]
        assert "private" in cache
        assert "public" not in cache


class TestCrossSiteRequests:
    """`dashcam.joshualeaper.dev` shares a registrable domain with every other host under
    `joshualeaper.dev`, so `SameSite=Lax` considers them all same-site and lets the session
    cookie ride along on their POSTs. Nothing here is theoretical: the reachable targets
    include running retention, restoring a database and switching sign-in back off."""

    @pytest.fixture
    async def secured(self, client):
        await _secure_and_sign_out(client)
        await _sign_in(client)
        return client

    async def test_a_post_from_a_sibling_subdomain_is_refused(self, secured):
        response = await secured.post(
            "/api/retention/plan", headers={"Origin": "https://evil.joshualeaper.dev"}
        )
        assert response.status_code == 403
        assert "another site" in response.json()["detail"]

    async def test_the_app_s_own_origin_is_allowed(self, secured):
        response = await secured.post(
            "/api/retention/plan", headers={"Origin": "http://test", "Host": "test"}
        )
        assert response.status_code == 200

    async def test_reads_are_never_refused_on_origin(self, secured):
        """A GET changes nothing, and refusing one would break following an inbound link."""
        response = await secured.get(
            "/api/status", headers={"Origin": "https://evil.joshualeaper.dev"}
        )
        assert response.status_code == 200

    async def test_a_script_sending_no_origin_still_works(self, secured):
        assert (await secured.post("/api/retention/plan")).status_code == 200


class TestClaimingTheFirstAccount:
    """Sign-in is off by default, so there is a window in which this endpoint is ungated by
    design. On a public hostname that window is an invitation, and the README's advice to
    set the password first is not a control."""

    async def test_a_remote_caller_cannot_claim_it(self, client):
        response = await client.put(
            "/api/auth/credential",
            json={"username": "attacker", "password": PASSWORD},
            # What Cloudflare puts on a request that came in off the internet.
            # Not a TEST-NET address: Python's `ipaddress` reports 203.0.113.0/24 as
            # private, so the documentation range cannot stand in for the internet here.
            headers={"CF-Connecting-IP": "8.8.8.8"},
        )
        assert response.status_code == 403
        assert "your own network" in response.json()["detail"]

    async def test_the_local_network_can(self, client):
        response = await client.put(
            "/api/auth/credential",
            json={"username": "joshua", "password": PASSWORD},
            headers={"CF-Connecting-IP": "192.168.1.40"},
        )
        assert response.status_code == 204

    async def test_an_existing_account_can_be_changed_from_anywhere(self, client):
        await _configure_account(client)
        await _require_sign_in(client)
        response = await client.put(
            "/api/auth/credential",
            json={
                "username": "joshua",
                "password": "another-good-password",
                "current_password": PASSWORD,
            },
            headers={"CF-Connecting-IP": "8.8.8.8"},
        )
        assert response.status_code == 204, response.text


class TestPasswordRules:
    async def test_a_short_password_is_refused(self, client):
        response = await client.put(
            "/api/auth/credential", json={"username": "joshua", "password": "short"}
        )
        assert response.status_code == 422

    async def test_a_non_ascii_username_works_rather_than_500ing(self, client):
        """`hmac.compare_digest` raises TypeError on non-ASCII `str`, which would have made
        one accented character in a username a 500 on every attempt — a lockout."""
        assert (
            await client.put(
                "/api/auth/credential", json={"username": "josé", "password": PASSWORD}
            )
        ).status_code == 204
        await _require_sign_in(client)
        client.cookies.clear()

        response = await client.post(
            "/api/auth/login", json={"username": "josé", "password": PASSWORD}
        )
        assert response.status_code == 200, response.text
        assert (await client.get("/api/status")).status_code == 200

    async def test_an_over_long_password_is_refused_without_echoing_it(self, client):
        await _secure_and_sign_out(client)
        secret = "z" * 2000
        response = await client.post(
            "/api/auth/login", json={"username": "joshua", "password": secret}
        )
        assert response.status_code == 400
        assert secret not in response.text


class TestTheMisconfiguredState:
    async def test_required_with_no_account_serves_rather_than_locking_everyone_out(
        self, client, db_session
    ):
        """Only reachable by hand-editing the database, and it fails open on purpose.

        A footage library nobody can open is a worse outcome than one that is briefly
        unguarded, and the log line this writes is what sends the operator to the recovery
        command.
        """
        from app.auth.service import reset_auth_state
        from app.core.settings_service import get_settings_service

        await _configure_account(client)
        await _require_sign_in(client)
        assert (await client.get("/api/status")).status_code == 200

        # The row goes without the switch going with it, which the API refuses to do.
        from sqlalchemy import delete

        from app.db.models import AuthCredential

        await db_session.execute(delete(AuthCredential))
        await db_session.commit()
        reset_auth_state()
        assert get_settings_service().get_nowait("security.require_login") is True

        client.cookies.clear()
        assert (await client.get("/api/status")).status_code == 200
