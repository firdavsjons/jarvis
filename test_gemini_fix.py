from core.ai import GeminiClient
import sys

def test_gemini():
    print("Initializing GeminiClient...")
    client = GeminiClient()
    if not client.model:
        print("Failed to initialize model")
        sys.exit(1)
    
    print("Sending request 'Hello'...")
    response = client.ask("Hello")
    print(f"Response: {response}")

    if "Не удалось" in response or "офлайн" in response:
        print("Test FAILED")
        sys.exit(1)
    else:
        print("Test PASSED")

if __name__ == "__main__":
    test_gemini()
