import sys
import os

print(f"Python executable: {sys.executable}")
print(f"Sys path: {sys.path}")

try:
    import openai
    print(f"OpenAI imported successfully: {openai.__file__}")
except ImportError as e:
    print(f"Failed to import openai: {e}")
