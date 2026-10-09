from fastapi.testclient import TestClient


def _client(monkeypatch, password):
    from plume import auth

    monkeypatch.setattr(auth, "PASSWORD", password)
    monkeypatch.setattr(auth, "_attempts", {})
    from plume.app import app

    return TestClient(app)


def test_open_when_no_password(monkeypatch):
    client = _client(monkeypatch, "")
    assert client.get("/api/session").json() == {"required": False, "authenticated": True}
    assert client.get("/api/fonts").status_code == 200


def test_password_protects_api(monkeypatch):
    client = _client(monkeypatch, "s3cret!")
    assert client.get("/api/session").json() == {"required": True, "authenticated": False}
    assert client.get("/api/fonts").status_code == 401
    assert client.get("/api/documents/whatever").status_code == 401
    assert client.get("/api/health").status_code == 200
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    r = client.post("/api/login", json={"password": "s3cret!"})
    assert r.status_code == 200 and "plume_session" in r.cookies
    assert client.get("/api/fonts").status_code == 200
    assert client.get("/api/session").json()["authenticated"] is True
    client.post("/api/logout")
    client.cookies.clear()
    assert client.get("/api/fonts").status_code == 401


def test_login_attempts_are_limited(monkeypatch):
    client = _client(monkeypatch, "s3cret!")
    codes = [client.post("/api/login", json={"password": "bad"}).status_code for _ in range(9)]
    assert codes[-1] == 429
    # Even the right password is refused while blocked.
    assert client.post("/api/login", json={"password": "s3cret!"}).status_code == 429
