import os
import smtplib
import ssl
from dotenv import load_dotenv

load_dotenv()

username = os.getenv("SMTP_USERNAME", "").strip()
password = os.getenv("SMTP_PASSWORD", "").replace(" ", "")

print("Username configured:", bool(username))
print("Password configured:", bool(password))

try:
    context = ssl.create_default_context()

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        timeout=20,
        context=context
    ) as server:
        server.login(username, password)

    print("SUCCESS: Gmail SMTP authentication worked.")

except Exception as error:
    print("SMTP TEST FAILED:", type(error).__name__, str(error))