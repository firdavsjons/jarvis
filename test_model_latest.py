from core.ai import GeminiClient
import sys
import google.generativeai as genai
import os
from dotenv import load_dotenv

# Override the class behavior locally for testing first, or just instantiate if I update the file?
# I'll update the file first, it's cleaner. But wait, I want to verify before changing the file again.
# So I'll write a script that does the setup manually.

def test_gemini_latest():
    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY")
    genai.configure(api_key=api_key)
    
    print("Testing gemini-flash-latest...")
    try:
        model = genai.GenerativeModel("gemini-flash-latest")
        response = model.generate_content("Hello")
        print(f"Response: {response.text}")
        print("Success!")
    except Exception as e:
        print(f"Failed: {e}")

if __name__ == "__main__":
    test_gemini_latest()
