import os
import asyncio
import socket
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import telebot

load_dotenv("/root/bot_config.env")
BOT_TOKEN = os.getenv("BOT_TOKEN")
USER_ID = int(os.getenv("USER_ID"))
bot = telebot.TeleBot(BOT_TOKEN)

async def wait_for_file(path, timeout=300):
    """Wait for file to be created and have content"""
    for _ in range(timeout):
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path) as f:
                content = f.read().strip()
            # Clear file after reading to prevent re-use
            open(path, 'w').close()
            return content
        await asyncio.sleep(1)
    raise TimeoutError(f"Timeout waiting for {path}")

async def wait_for_port(host: str, port: int, timeout: int = 180):
    """Wait for port to become available"""
    for _ in range(timeout):
        try:
            with socket.create_connection((host, port), timeout=5):
                return True
        except Exception:
            await asyncio.sleep(1)
    return False

async def send_async_message(text):
    """Send message without blocking event loop"""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, bot.send_message, USER_ID, text)

async def send_async_photo(photo_path):
    """Send photo without blocking event loop"""
    loop = asyncio.get_event_loop()
    with open(photo_path, 'rb') as photo:
        await loop.run_in_executor(None, bot.send_photo, USER_ID, photo)

async def main():
    # Clear any previous state
    for path in ["/root/email.txt", "/root/otp.txt"]:
        if os.path.exists(path):
            open(path, 'w').close()

    # Don't send waiting message - bot already notified user
    if not await wait_for_port("localhost", 3000, 180):
        await send_async_message("❌ Timeout waiting for localhost:3000 - Check if Gensyn is running")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            await page.goto("http://localhost:3000", timeout=120000)
            
            # Click Login button
            login_button = await page.wait_for_selector("button:has-text('Login')", timeout=30000)
            await login_button.click()
            
            # Wait for email input
            email = await wait_for_file("/root/email.txt", 300)
            email_input = await page.wait_for_selector("input[type=email]", timeout=30000)
            await email_input.fill(email)
            
            # Click Continue button
            continue_button = await page.wait_for_selector("button:has-text('Continue')", timeout=30000)
            await continue_button.click()
            
            # Wait for OTP input
            await page.wait_for_selector("text=Enter verification code", timeout=90000)
            
            # Get OTP from file
            otp = await wait_for_file("/root/otp.txt", 300)
            
            # Find OTP input field
            otp_input = await page.wait_for_selector("input[inputmode=numeric]", timeout=60000)
            await otp_input.type(otp, delay=50)
            await page.keyboard.press("Enter")

            # Wait for either success or failure
            try:
                await asyncio.wait_for(
                    page.wait_for_selector("text=/successfully logged in|dashboard/i", timeout=120000),
                    timeout=120
                )
                await page.screenshot(path="/root/final_login_success.png", full_page=True)
                await send_async_message("✅ Login successful!")
                await send_async_photo("/root/final_login_success.png")
            except Exception as e:
                await page.screenshot(path="/root/login_failed.png", full_page=True)
                await send_async_message(f"❌ Login failed: {str(e)}")
                await send_async_photo("/root/login_failed.png")
                raise

        except Exception as e:
            await send_async_message(f"❌ Critical error in login process: {str(e)}")
            await page.screenshot(path="/root/login_error.png", full_page=True)
            await send_async_photo("/root/login_error.png")
        finally:
            await context.close()
            await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
if __name__ == "__main__":
    asyncio.run(main())

