import httpx
import pytest

from canvas_connector.cloudflare_mail import CloudflareMailer, AccountMailUnavailable, MAIL_URL


@pytest.mark.parametrize("status", [200, 301, 302, 303, 307, 308, 401, 429, 500, 503])
def test_mail_uses_fixed_private_route_without_redirect_or_retry(status):
    requests = []

    def handle(request):
        requests.append(request)
        if status == 200:
            return httpx.Response(200, json={"sent": True})
        return httpx.Response(status, headers={"Location": "https://other.example/steal"},
                              text="private response with a synthetic secret")

    mailer = CloudflareMailer(client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    if status == 200:
        mailer("student@example.test", "verify", "one_time_code_123")
    else:
        with pytest.raises(AccountMailUnavailable, match="^Account email is unavailable$"):
            mailer("student@example.test", "verify", "one_time_code_123")
    assert len(requests) == 1
    assert str(requests[0].url) == MAIL_URL
    assert requests[0].headers.get("Authorization") is None


@pytest.mark.parametrize("body", [b'', b'not-json', b'{}', b'{"sent":false}', b'{"sent":"true"}', b'[]', b'x' * 4097])
def test_success_status_without_bounded_delivery_acknowledgement_is_not_success(body):
    attempts = []
    def handle(request):
        attempts.append(request)
        return httpx.Response(200, content=body)
    mailer = CloudflareMailer(client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(AccountMailUnavailable):
        mailer("student@example.test", "verify", "one_time_code_123")
    assert len(attempts) == 1


def test_lost_mail_response_has_one_attempt_and_redacts_errors():
    requests = []

    def handle(request):
        requests.append(request)
        raise httpx.ReadError("private token email and code", request=request)

    mailer = CloudflareMailer(client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(AccountMailUnavailable) as error:
        mailer("student@example.test", "reset", "one_time_code_123")
    assert str(error.value) == "Account email is unavailable"
    assert len(requests) == 1


@pytest.mark.parametrize("email,purpose,code", [
    ("a@b.test\r\nInjected: x", "verify", "valid_code"),
    ("not-an-email", "verify", "valid_code"),
    ("student@example.test", "arbitrary", "valid_code"),
    ("student@example.test", "reset", "short"),
    ("student@example.test", "verify", "code\r\nheader"),
])
def test_invalid_mail_payload_never_opens_transport(email, purpose, code):
    def forbidden():
        pytest.fail("No transport should open")
    with pytest.raises(AccountMailUnavailable):
        CloudflareMailer(client_factory=forbidden)(email, purpose, code)
