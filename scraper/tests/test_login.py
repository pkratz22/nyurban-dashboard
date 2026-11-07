from unittest.mock import Mock, call, patch

import login


def test_request_retries_server_errors_then_returns_success():
    session = Mock()
    session.get.side_effect = [Mock(status_code=503), Mock(status_code=200, text="ok")]
    with patch.object(login.time, "sleep") as sleep:
        response = login.get(session, "https://example.test", timeout=10)
    assert response.status_code == 200
    assert session.get.call_args_list == [call("https://example.test", timeout=10)] * 2
    sleep.assert_called_once_with(5)


def test_request_returns_final_server_error_after_all_retries():
    session = Mock()
    final = Mock(status_code=503)
    session.get.side_effect = [Mock(status_code=503) for _ in range(4)] + [final]
    with patch.object(login.time, "sleep"):
        assert login.get(session, "https://example.test") is final
    assert session.get.call_count == 5


def test_request_honors_retry_after_for_rate_limits():
    session = Mock()
    session.get.side_effect = [Mock(status_code=429, headers={"Retry-After": "7"}), Mock(status_code=200)]
    with patch.object(login.time, "sleep") as sleep:
        assert login.get(session, "https://example.test").status_code == 200
    sleep.assert_called_once_with(7)


def test_post_uses_the_shared_request_wrapper():
    session = Mock()
    session.post.return_value = Mock(status_code=200)
    login.post(session, "https://example.test", data={"key": "value"})
    session.post.assert_called_once_with("https://example.test", data={"key": "value"})


def test_login_posts_credentials_and_detects_success(monkeypatch, fixture_html):
    monkeypatch.setenv("NYURBAN_USERNAME", "test-user")
    monkeypatch.setenv("NYURBAN_PASSWORD", "test-password")
    session = Mock()
    with patch.object(login, "post", return_value=Mock(text="Welcome back")) as post:
        assert login.login(session) is True
    post.assert_called_once_with(
        session, login.LOGIN_URL,
        data={"ny_username": "test-user", "ny_password": "test-password", "submit": "login"},
        allow_redirects=True,
    )


def test_login_returns_false_without_success_marker(monkeypatch):
    monkeypatch.setenv("NYURBAN_USERNAME", "u")
    monkeypatch.setenv("NYURBAN_PASSWORD", "p")
    with patch.object(login, "post", return_value=Mock(text="Invalid credentials")):
        assert login.login(Mock()) is False
