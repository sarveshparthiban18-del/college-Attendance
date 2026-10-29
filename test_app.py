
import pytest
import app as attendance_app


@pytest.fixture
def client():
    attendance_app.app.config["TESTING"] = True

    with attendance_app.app.test_client() as client:
        yield client


def test_app_starts():
    assert attendance_app.app is not None


def test_login_page_response(client):
    response = client.get("/login", follow_redirects=False)

    # A login page should respond successfully, or redirect
    # according to the application's session rules.
    assert response.status_code in (200, 302, 303)
