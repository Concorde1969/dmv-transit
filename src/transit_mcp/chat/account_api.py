"""Accounts behind the map's "My places" panel (see :mod:`transit_mcp.accounts`).

    POST   /api/account/register     {username, password}          -> new account, signed in,
                                                                     and its recovery code
    POST   /api/account/login        {username, password}          -> signs in (session cookie)
    POST   /api/account/recover      {username, recovery_code, new_password}
                                                                   -> new password and code
    POST   /api/account/password     {current_password, new_password}
    POST   /api/account/code         {email}        -> emails a 6-digit sign-in code
    POST   /api/account/verify       {email, code}  -> signs in (session cookie)
                                     (these two only with ACCOUNTS_EMAIL_CODES)
    GET    /api/account                             -> who is signed in, their places
    POST   /api/account/signout
    DELETE /api/account                             -> deletes the account and its places
    POST   /api/account/places        {place}       -> saves a place
    PUT    /api/account/places/{id}   {place}
    DELETE /api/account/places/{id}

The session cookie is HttpOnly, Secure on the public site and SameSite=Lax; requests
that change anything must also come from this site's own pages (Origin check).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from transit_mcp.accounts import (
    KINDS,
    AccountStore,
    AuthError,
    PlaceError,
    PlaceInput,
    normalize_email,
)
from transit_mcp.chat.guard import DailyCap, RateLimiter
from transit_mcp.geocode import in_region
from transit_mcp.mailer import Mailer, MailError, code_email

log = logging.getLogger("transit_mcp.accounts")
router = APIRouter()

COOKIE = "dmv_session"
COOKIE_MAX_AGE = 90 * 86_400
UNAVAILABLE = "Accounts aren't available right now. Please try again later."
SLOW_DOWN = "That's several tries in a short time. Please wait a few minutes and try again."


class AccountService:
    """The account store, mailer and limits shared by requests."""

    def __init__(self, settings):
        self.settings = settings
        self.store = AccountStore.from_settings(settings)
        self.mailer = Mailer.from_settings(settings)
        self.login_limiter = RateLimiter(10, 100)    # password and recovery tries per visitor
        self.register_limiter = RateLimiter(3, 20)   # new accounts per visitor
        self.ip_limiter = RateLimiter(5, 30)        # code emails per visitor
        self.email_limiter = RateLimiter(2, 8)      # code emails per address
        self.verify_limiter = RateLimiter(10, 60)   # code checks per visitor
        self.write_limiter = RateLimiter(30, 500)   # place changes per visitor
        self.daily_emails = DailyCap(settings.accounts_daily_emails, settings.timezone)

    @property
    def email_codes(self) -> bool:
        """Email codes stay built but off until SES (or another sender) is ready."""
        return self.settings.accounts_email_codes and self.mailer.available

    def close(self) -> None:
        self.store.close()


def start_accounts(settings) -> AccountService | None:
    if not settings.accounts_enabled:
        return None
    try:
        svc = AccountService(settings)
        svc.store.prune(settings.accounts_inactive_days)
        return svc
    except Exception:  # noqa: BLE001 - the site works without accounts
        log.exception("accounts unavailable")
        return None


class EmailBody(BaseModel):
    email: str = Field(max_length=254)


class CredentialsBody(BaseModel):
    username: str = Field(max_length=60)
    password: str = Field(max_length=200)


class RecoverBody(BaseModel):
    username: str = Field(max_length=60)
    recovery_code: str = Field(max_length=40)
    new_password: str = Field(max_length=200)


class PasswordBody(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


class VerifyBody(BaseModel):
    email: str = Field(max_length=254)
    code: str = Field(max_length=12)


class PlaceBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: str = Field(max_length=20)
    lat: float
    lon: float
    address: str | None = Field(default=None, max_length=200)
    start_date: date | None = None
    end_date: date | None = None


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _svc(request: Request) -> AccountService | None:
    return getattr(request.app.state, "accounts", None)


def _same_site(request: Request) -> bool:
    """Changes must come from this site's own pages (blocks cross-site form posts)."""
    origin = request.headers.get("origin")
    if not origin:
        return True     # same-origin fetches from older browsers; SameSite still applies
    return urlsplit(origin).netloc == request.headers.get("host", "")


def _user(request: Request, svc: AccountService) -> dict | None:
    return svc.store.user_for_token(request.cookies.get(COOKIE))


def _set_cookie(response: JSONResponse, token: str, request: Request) -> None:
    response.set_cookie(COOKIE, token, max_age=COOKIE_MAX_AGE, httponly=True,
                        samesite="lax", path="/",
                        secure=request.app.state.settings.is_production)


def _message(text: str, status: int) -> JSONResponse:
    return JSONResponse({"ok": False, "message": text}, status_code=status)


def _event(request: Request, event: str, **detail) -> None:
    """The anonymous usage log (never the email address or the place)."""
    chatlog = getattr(request.app.state, "chatlog", None)
    if chatlog is not None:
        chatlog.safe_record_event(chatlog.visitor_id(_ip(request)), event, detail=detail)


def _account(svc: AccountService, user: dict) -> dict:
    return {"signed_in": True, "enabled": True, "email_codes": svc.email_codes,
            "username": user.get("username"), "email": user.get("email"),
            "places": svc.store.places(user["id"]), "plan": svc.store.plan(user["id"]),
            "kinds": list(KINDS),
            "max_places": svc.store.max_places}


def _signed_in(request: Request, svc: AccountService, user: dict, token: str,
               **extra) -> JSONResponse:
    response = JSONResponse({**_account(svc, user), **extra})
    _set_cookie(response, token, request)
    return response


def _password_request(request: Request, limiter_name: str):
    svc = _svc(request)
    if svc is None:
        return None, _message(UNAVAILABLE, 503)
    if not _same_site(request):
        return svc, _message("Please sign in from the DMV Transit page.", 403)
    if getattr(svc, limiter_name).check(_ip(request)):
        return svc, _message(SLOW_DOWN, 429)
    return svc, None


@router.post("/api/account/register")
async def register(request: Request, body: CredentialsBody) -> JSONResponse:
    svc, refused = _password_request(request, "register_limiter")
    if refused:
        return refused
    try:
        user, token, code = await asyncio.to_thread(svc.store.register, body.username,
                                                    body.password)
    except AuthError as exc:
        return _message(str(exc), exc.status)
    _event(request, "account_registered")
    return _signed_in(request, svc, user, token, created=True, recovery_code=code)


@router.post("/api/account/login")
async def login(request: Request, body: CredentialsBody) -> JSONResponse:
    svc, refused = _password_request(request, "login_limiter")
    if refused:
        return refused
    try:
        user, token = await asyncio.to_thread(svc.store.password_sign_in, body.username,
                                              body.password)
    except AuthError as exc:
        return _message(str(exc), exc.status)
    _event(request, "account_signed_in", method="password")
    return _signed_in(request, svc, user, token)


@router.post("/api/account/recover")
async def recover(request: Request, body: RecoverBody) -> JSONResponse:
    svc, refused = _password_request(request, "login_limiter")
    if refused:
        return refused
    try:
        user, token, code = await asyncio.to_thread(
            svc.store.recover, body.username, body.recovery_code, body.new_password)
    except AuthError as exc:
        return _message(str(exc), exc.status)
    _event(request, "account_recovered")
    return _signed_in(request, svc, user, token, recovery_code=code)


@router.post("/api/account/password")
async def change_password(request: Request, body: PasswordBody) -> JSONResponse:
    svc, refused = _password_request(request, "login_limiter")
    if refused:
        return refused
    token = request.cookies.get(COOKIE)
    user = svc.store.user_for_token(token)
    if user is None:
        return _message("Please sign in first.", 401)
    try:
        await asyncio.to_thread(svc.store.change_password, user["id"], body.current_password,
                                body.new_password, token)
    except AuthError as exc:
        return _message(str(exc), exc.status)
    return JSONResponse({"ok": True, "message": "Your password is changed. Any other "
                         "devices signed in to this account are signed out."})


@router.post("/api/account/code")
async def send_code(request: Request, body: EmailBody) -> JSONResponse:
    svc = _svc(request)
    if svc is None or not svc.email_codes:
        return _message("Email sign-in isn't available. Please sign in with your username "
                        "and password.", 503)
    if not _same_site(request):
        return _message("Please sign in from the DMV Transit page.", 403)
    email = normalize_email(body.email)
    if email is None:
        return _message("That email address doesn't look right. Please check it.", 422)
    if svc.ip_limiter.check(_ip(request)) or svc.email_limiter.check(email):
        return _message(SLOW_DOWN, 429)
    if not svc.daily_emails.try_take():
        log.warning("site-wide daily sign-in email limit reached (%s)",
                    svc.daily_emails.limit)
        return _message("We've sent a lot of sign-in emails today. Please try again "
                        "tomorrow.", 429)
    code = await asyncio.to_thread(svc.store.issue_code, email)
    subject, text, html = code_email(code, request.headers.get("host") or "dmvtransit.com")
    try:
        await asyncio.to_thread(svc.mailer.send, email, subject, text, html)
    except MailError as exc:
        log.error("sign-in email failed: %s", exc)
        return _message("Sorry, we couldn't send the email just now. Please try again in "
                        "a few minutes.", 502)
    _event(request, "account_code_sent")
    return JSONResponse({"ok": True, "message": f"We sent a 6-digit code to {email}. "
                         "It works for 10 minutes. Check your spam folder if it isn't "
                         "there in a minute."})


@router.post("/api/account/verify")
async def verify(request: Request, body: VerifyBody) -> JSONResponse:
    svc = _svc(request)
    if svc is None or not svc.email_codes:
        return _message(UNAVAILABLE, 503)
    if not _same_site(request):
        return _message("Please sign in from the DMV Transit page.", 403)
    email = normalize_email(body.email)
    if svc.verify_limiter.check(_ip(request)):
        return _message(SLOW_DOWN, 429)
    if email is None or not await asyncio.to_thread(svc.store.check_code, email, body.code):
        return _message("That code isn't right, or it has expired. Check the email, or "
                        "ask for a new code.", 400)
    user, token, created = await asyncio.to_thread(svc.store.sign_in, email)
    _event(request, "account_signed_in", method="email", new=created)
    return _signed_in(request, svc, user, token, created=created)


@router.get("/api/account")
def account(request: Request) -> JSONResponse:
    svc = _svc(request)
    if svc is None:
        return JSONResponse({"signed_in": False, "enabled": False})
    user = _user(request, svc)
    if user is None:
        response = JSONResponse({"signed_in": False, "enabled": True,
                                 "email_codes": svc.email_codes})
        if COOKIE in request.cookies:
            response.delete_cookie(COOKIE, path="/")
        return response
    return JSONResponse(_account(svc, user), headers={"Cache-Control": "no-store"})


@router.post("/api/account/signout")
def sign_out(request: Request) -> JSONResponse:
    svc = _svc(request)
    if svc is not None and _same_site(request):
        svc.store.sign_out(request.cookies.get(COOKIE))
    response = JSONResponse({"ok": True, "signed_in": False})
    response.delete_cookie(COOKIE, path="/")
    return response


@router.delete("/api/account")
def delete_account(request: Request) -> JSONResponse:
    svc = _svc(request)
    if svc is None:
        return _message(UNAVAILABLE, 503)
    user = _user(request, svc)
    if user is None or not _same_site(request):
        return _message("Please sign in first.", 401)
    svc.store.delete_user(user["id"])
    _event(request, "account_deleted")
    response = JSONResponse({"ok": True, "signed_in": False,
                             "message": "Your account and saved places are deleted."})
    response.delete_cookie(COOKIE, path="/")
    return response


def _place_request(request: Request) -> tuple[AccountService | None, dict | None,
                                                 JSONResponse | None]:
    svc = _svc(request)
    if svc is None:
        return None, None, _message(UNAVAILABLE, 503)
    user = _user(request, svc)
    if user is None or not _same_site(request):
        return svc, None, _message("Please sign in to save places.", 401)
    if svc.write_limiter.check(_ip(request)):
        return svc, user, _message(SLOW_DOWN, 429)
    return svc, user, None


def _place_input(body: PlaceBody) -> PlaceInput:
    if not in_region(body.lat, body.lon):
        raise PlaceError("That place is outside the DC, Maryland and Northern Virginia "
                         "area this planner covers.")
    return PlaceInput(name=body.name, kind=body.kind, lat=body.lat, lon=body.lon,
                      address=body.address, start_date=body.start_date,
                      end_date=body.end_date)


@router.post("/api/account/places")
def add_place(request: Request, body: PlaceBody) -> JSONResponse:
    svc, user, refused = _place_request(request)
    if refused:
        return refused
    try:
        place = svc.store.add_place(user["id"], _place_input(body))
    except PlaceError as exc:
        return _message(str(exc), 422)
    _event(request, "place_saved", kind=place["kind"], dated=bool(place["start_date"]))
    return JSONResponse({"ok": True, "place": place, "places": svc.store.places(user["id"])},
                        status_code=201)


@router.put("/api/account/places/{place_id}")
def update_place(request: Request, place_id: int, body: PlaceBody) -> JSONResponse:
    svc, user, refused = _place_request(request)
    if refused:
        return refused
    try:
        place = svc.store.update_place(user["id"], place_id, _place_input(body))
    except PlaceError as exc:
        return _message(str(exc), 422)
    if place is None:
        return _message("That place isn't in your list any more.", 404)
    return JSONResponse({"ok": True, "place": place, "places": svc.store.places(user["id"])})


@router.delete("/api/account/plan/{stop_id}")
def delete_plan_stop(request: Request, stop_id: int) -> JSONResponse:
    svc, user, refused = _place_request(request)
    if refused:
        return refused
    svc.store.delete_stop(user["id"], stop_id)
    return JSONResponse({"ok": True, "plan": svc.store.plan(user["id"])})


@router.delete("/api/account/places/{place_id}")
def delete_place(request: Request, place_id: int) -> JSONResponse:
    svc, user, refused = _place_request(request)
    if refused:
        return refused
    svc.store.delete_place(user["id"], place_id)
    return JSONResponse({"ok": True, "places": svc.store.places(user["id"])})
