"""Most basic Gemini test - just text in, text out."""

import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="Say hello in one sentence.",
)
print(f"Response: {response.text}")
