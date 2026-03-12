"""Test admin CSRF in logout form and stale cookie clearing."""
import pytest
from unittest.mock import patch, MagicMock


def test_overview_page_has_csrf_in_logout_form(client):
    """The overview page's logout form must include a csrf_token hidden input."""
    # First login to get a session cookie
    with patch("api.admin_ui.views._verify_admin_key", return_value=True), \
         patch("api.admin_ui.views._session_store") as mock_store:
        mock_store.create_csrf_token.return_value = "test-csrf-token"
        mock_store.verify_csrf_token.return_value = True
        mock_store.create_session.return_value = "test-session-id"
        mock_store.get_session.return_value = {"created_at": "2026-03-12T00:00:00"}

        # Login
        response = client.post(
            "/admin/login",
            data={"admin_key": "test-key", "csrf_token": "test-csrf-token"},
        )

        # Get overview with session
        response = client.get(
            "/admin/",
            cookies={"admin_session": "test-session-id"},
        )
        assert response.status_code == 200
        html = response.text
        assert 'name="csrf_token"' in html, "Logout form is missing CSRF token"
        assert 'action="/admin/logout"' in html


def test_auth_redirect_clears_cookie(client):
    """When session is expired, redirect must delete the admin_session cookie."""
    response = client.get(
        "/admin/",
        cookies={"admin_session": "expired-bogus-token"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    set_cookie = response.headers.get("set-cookie", "")
    assert "admin_session" in set_cookie


def test_login_page_uses_local_tailwind(client):
    """Login page must use local Tailwind, not CDN."""
    response = client.get("/admin/login")
    assert response.status_code == 200
    html = response.text
    assert "cdn.tailwindcss.com" not in html
    assert "/admin/static/tailwind.css" in html
