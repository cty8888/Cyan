from dotenv import load_dotenv
import os

load_dotenv()

key = os.getenv("DEEPSEEK_API_KEY")

print(key[:8])