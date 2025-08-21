import os
import socket
import time
import threading
from typing import Optional, Dict

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import requests


_login_thread: Optional[threading.Thread] = None
_login_running: bool = False
_login_status: str = "idle"
_login_error: Optional[str] = None
_login_screenshot: Optional[str] = None

_email_event = threading.Event()
_otp_event = threading.Event()
_email_value: Optional[str] = None
_otp_value: Optional[str] = None
_email_submitted: bool = False
_otp_submitted: bool = False


def _set_status(status: str, error: Optional[str] = None, screenshot: Optional[str] = None) -> None:
    global _login_status, _login_error, _login_screenshot
    _login_status = status
    _login_error = error
    _login_screenshot = screenshot


def _wait_for_port(host: str, port: int, timeout_seconds: int) -> bool:
    for _ in range(timeout_seconds):
        # Try raw socket first
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except Exception:
            pass
        # Try HTTP probe (any status indicates server is up)
        try:
            r = requests.get(f"http://{host}:{port}", timeout=2)
            if r is not None:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _wait_for_event(evt: threading.Event, timeout_seconds: int) -> bool:
    return evt.wait(timeout=timeout_seconds)


def _run_login_flow() -> None:
    global _login_running, _email_value, _otp_value
    try:
        _set_status("waiting_port")
        if not _wait_for_port("localhost", 3000, 180):
            _set_status("error", error="Timeout waiting for localhost:3000")
            return

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            try:
                _set_status("opening")
                page.goto("http://localhost:3000", timeout=120_000)

                # Try multiple possible login button labels/selectors
                _set_status("click_login")
                selectors = [
                    "button:has-text('Login')",
                    "button:has-text('Sign in')",
                    "button:has-text('Sign In')",
                    "text=Login",
                ]
                clicked = False
                for sel in selectors:
                    try:
                        btn = page.wait_for_selector(sel, timeout=5_000)
                        btn.click()
                        clicked = True
                        break
                    except Exception:
                        continue
                if not clicked:
                    raise RuntimeError("Login button not found - UI changed?")

                _set_status("waiting_email")
                if not _wait_for_event(_email_event, 300):
                    _set_status("error", error="Email not provided in time")
                    return

                _set_status("fill_email")
                # Try multiple email input selectors
                email_selectors = [
                    "input[type=email]",
                    "input[name=email]",
                    "input[autocomplete=email]",
                ]
                email_input = None
                for sel in email_selectors:
                    try:
                        email_input = page.wait_for_selector(sel, timeout=5_000)
                        break
                    except Exception:
                        continue
                if not email_input:
                    raise RuntimeError("Email input not found - UI changed?")
                email_input.fill((_email_value or "").strip())

                _set_status("click_continue")
                cont_selectors = [
                    "button:has-text('Continue')",
                    "button:has-text('Next')",
                    "text=Continue",
                ]
                cont_clicked = False
                for sel in cont_selectors:
                    try:
                        cont_btn = page.wait_for_selector(sel, timeout=5_000)
                        cont_btn.click()
                        cont_clicked = True
                        break
                    except Exception:
                        continue
                if not cont_clicked:
                    raise RuntimeError("Continue button not found - UI changed?")

                # Capture state after continue
                try:
                    screenshot = "/root/login_after_continue_web.png"
                    page.screenshot(path=screenshot, full_page=True)
                    _set_status("after_continue", screenshot=screenshot)
                except Exception:
                    pass

                # Some UIs require explicit 'Send code' / 'Send email' action
                try:
                    _set_status("maybe_click_send_code")
                    send_selectors = [
                        "button:has-text('Send code')",
                        "button:has-text('Send Code')",
                        "button:has-text('Send email')",
                        "button:has-text('Send Email')",
                        "text=Send code",
                        "text=Send Email",
                        "button:has-text('Resend code')",
                        "button:has-text('Resend Code')",
                    ]
                    for sel in send_selectors:
                        btn = page.query_selector(sel)
                        if btn:
                            btn.click()
                            break
                except Exception:
                    pass

                _set_status("waiting_for_code_screen")
                try:
                    page.wait_for_selector("text=Enter verification code", timeout=90_000)
                except PlaywrightTimeoutError:
                    # Some flows might send a magic link instead of code
                    # Capture screenshot and report
                    screenshot = "/root/login_magic_link_web.png"
                    page.screenshot(path=screenshot, full_page=True)
                    _set_status("magic_link", screenshot=screenshot)
                    return

                _set_status("waiting_otp")
                if not _wait_for_event(_otp_event, 300):
                    _set_status("error", error="OTP not provided in time")
                    return

                _set_status("fill_otp")
                otp_selectors = [
                    "input[inputmode=numeric]",
                    "input[autocomplete='one-time-code']",
                    "input[type=tel]",
                ]
                otp_input = None
                for sel in otp_selectors:
                    try:
                        otp_input = page.wait_for_selector(sel, timeout=5_000)
                        break
                    except Exception:
                        continue
                if not otp_input:
                    raise RuntimeError("OTP input not found - UI changed?")
                otp_input.type((_otp_value or "").strip(), delay=50)
                page.keyboard.press("Enter")

                try:
                    _set_status("waiting_success")
                    page.wait_for_selector("text=/successfully logged in|dashboard|Logout/i", timeout=120_000)
                    screenshot = "/root/final_login_success_web.png"
                    page.screenshot(path=screenshot, full_page=True)
                    _set_status("success", screenshot=screenshot)
                except PlaywrightTimeoutError as e:
                    screenshot = "/root/login_failed_web.png"
                    page.screenshot(path=screenshot, full_page=True)
                    _set_status("failed", error=str(e), screenshot=screenshot)
            except Exception as e:
                screenshot = "/root/login_error_web.png"
                try:
                    page.screenshot(path=screenshot, full_page=True)
                except Exception:
                    pass
                _set_status("error", error=str(e), screenshot=screenshot)
            finally:
                context.close()
                browser.close()
    finally:
        _login_running = False


def start_login() -> bool:
    global _login_thread, _login_running, _email_value, _otp_value
    if _login_running:
        return False
    _login_running = True
    _set_status("starting")
    _email_value = None
    _otp_value = None
    global _email_submitted, _otp_submitted
    _email_submitted = False
    _otp_submitted = False
    if _email_event.is_set():
        _email_event.clear()
    if _otp_event.is_set():
        _otp_event.clear()
    _login_thread = threading.Thread(target=_run_login_flow, daemon=True)
    _login_thread.start()
    return True


def submit_email(email: str) -> None:
    global _email_value
    _email_value = email
    _email_event.set()
    global _email_submitted
    _email_submitted = True
    _set_status("email_submitted")


def submit_otp(otp: str) -> None:
    global _otp_value
    _otp_value = otp
    _otp_event.set()
    global _otp_submitted
    _otp_submitted = True
    _set_status("otp_submitted")


def get_status() -> Dict[str, Optional[str]]:
    return {
        "running": _login_running,
        "status": _login_status,
        "error": _login_error,
        "screenshot": _login_screenshot,
        "email": _email_value,
        "email_submitted": _email_submitted,
        "otp_submitted": _otp_submitted,
    }


