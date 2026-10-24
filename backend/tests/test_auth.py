from app.security import login_throttle


def test_health_is_public(anon):
    r = anon.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "controlli": {"database": True, "storage": True}}


def test_security_headers(anon):
    r = anon.get("/api/v1/health")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


def test_content_endpoints_require_auth(anon):
    for method, path in [("get", "/api/v1/recordings"), ("get", "/api/v1/jobs"),
                         ("post", "/api/v1/uploads"), ("get", "/api/v1/status"),
                         ("get", "/api/v1/auth/me")]:
        r = getattr(anon, method)(path)
        assert r.status_code == 401, (method, path, r.status_code)


def test_login_wrong_password(anon, users):
    r = anon.post("/api/v1/auth/login", json={"username": "alice", "password": "sbagliata"})
    assert r.status_code == 401
    assert "ddb_session" not in r.cookies
    login_throttle.reset("u:alice")
    login_throttle.reset("ip:testclient")


def test_login_unknown_user(anon, users):
    r = anon.post("/api/v1/auth/login", json={"username": "nessuno", "password": "x"})
    assert r.status_code == 401
    login_throttle.reset("u:nessuno")
    login_throttle.reset("ip:testclient")


def test_session_cookie_flags(anon, users):
    r = anon.post("/api/v1/auth/login", json={"username": "alice", "password": users["alice"]})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_password_not_stored_in_clear(admin_conn, users):
    h = admin_conn.execute("SELECT password_hash FROM users WHERE username = 'alice'").fetchone()[0]
    assert h.startswith("$argon2id$") and users["alice"] not in h


def test_session_token_stored_hashed(alice, admin_conn):
    token = alice.cookies.get("ddb_session")
    rows = admin_conn.execute("SELECT token_hash FROM sessions").fetchall()
    assert all(token.encode() not in bytes(r[0]) for r in rows)


def test_csrf_required_for_cookie_mutations(alice):
    csrf = alice.headers.pop("X-CSRF-Token")
    r = alice.post("/api/v1/uploads", headers={"Idempotency-Key": "csrf-test-0001"},
                   json={"mime_type": "audio/webm", "total_size": 10})
    assert r.status_code == 403
    alice.headers["X-CSRF-Token"] = "sbagliato"
    r = alice.post("/api/v1/uploads", headers={"Idempotency-Key": "csrf-test-0001"},
                   json={"mime_type": "audio/webm", "total_size": 10})
    assert r.status_code == 403
    alice.headers["X-CSRF-Token"] = csrf


def test_logout_revokes_session(app, users):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        r = c.post("/api/v1/auth/login", json={"username": "bruno", "password": users["bruno"]})
        csrf = r.json()["csrf_token"]
        assert c.get("/api/v1/auth/me").status_code == 200
        token = c.cookies.get("ddb_session")
        assert c.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
        c.cookies.set("ddb_session", token)  # anche riusando il vecchio cookie
        assert c.get("/api/v1/auth/me").status_code == 401


def test_login_throttling(anon, users):
    try:
        for _ in range(8):
            anon.post("/api/v1/auth/login", json={"username": "bruno", "password": "no"})
        r = anon.post("/api/v1/auth/login", json={"username": "bruno", "password": users["bruno"]})
        assert r.status_code == 429  # bloccato anche con la password giusta
    finally:
        login_throttle.reset("u:bruno")
        login_throttle.reset("ip:testclient")


def test_api_token(anon, users):
    from io import StringIO
    import contextlib
    from app.cli import main
    out = StringIO()
    with contextlib.redirect_stdout(out):
        main(["create-token", "alice", "test"])
    token = out.getvalue().strip().splitlines()[-1]
    r = anon.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["autenticazione"] == "api_token"
    # Il token API non richiede CSRF (non è un cookie inviato automaticamente)
    r = anon.put("/api/v1/entries/00000000-0000-4000-8000-000000000000/voci",
                 headers={"Authorization": f"Bearer {token}"}, json={"nomi": {}})
    assert r.status_code == 404
    r = anon.get("/api/v1/auth/me", headers={"Authorization": "Bearer ddbt_falso"})
    assert r.status_code == 401


def test_pages_are_always_revalidated(anon):
    """Le pagine si ricontrollano sempre: dopo un aggiornamento il browser non deve tenersi
    l'HTML vecchio, che punta a file che non esistono più. Le API non si salvano affatto."""
    assert anon.get("/").headers["cache-control"] == "no-cache"
    assert anon.get("/api/v1/health").headers["cache-control"] == "no-store"
