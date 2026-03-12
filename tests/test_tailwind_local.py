"""Tests that Tailwind is served locally, not from CDN."""
import inspect
import os

import pytest


def test_csp_does_not_allow_cdn():
    """CSP must not reference cdn.tailwindcss.com."""
    from api import middleware
    source = inspect.getsource(middleware.configure_middleware)
    assert "cdn.tailwindcss.com" not in source, (
        "CSP must not allow cdn.tailwindcss.com — bundle Tailwind locally"
    )


def test_base_template_no_cdn_reference():
    """Base template must not reference CDN Tailwind."""
    base_path = os.path.join(
        os.path.dirname(__file__), "..", "api", "admin_ui", "templates", "base.html"
    )
    with open(base_path) as f:
        content = f.read()
    assert "cdn.tailwindcss.com" not in content, (
        "Base template must use local Tailwind CSS, not CDN"
    )
