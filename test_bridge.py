import asyncio
import os
import sys
import io
from openclaw_acp_bridge import OpenClaw

# Fix Windows console encoding for emojis
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Load configuration
import json
config_path = os.path.join(os.path.dirname(__file__), "config.json")
config = {
    "host": "localhost",
    "port": 18781,
    "token": None,
    "download_dir": "test_downloads"
}

if os.path.exists(config_path):
    with open(config_path, "r") as f:
        config.update(json.load(f))

# Token now comes from token.txt by default
token_path = os.path.join(os.path.dirname(__file__), "token.txt")
if os.path.exists(token_path):
    with open(token_path, "r") as f:
        config["token"] = f.read().strip()

if not config["token"]:
    print(f"❌ Error: No authentication token found!")
    print(f"Please provide a token in 'token.txt' or 'config.json'.")
    print(f"TIP: Copy token.txt.sample to token.txt and enter your hex token.")
    sys.exit(1)

import argparse

async def run_test(tests_to_run=None):
    if not tests_to_run:
        tests_to_run = [1, 2, 3, 4]
        
    print(f"--- Starting ACP Bridge Test on {config['host']} (Tests: {tests_to_run}) ---")
    
    try:
        async with OpenClaw(
            host=config['host'], 
            port=config['port'], 
            token=config['token'], 
            download_dir=config['download_dir']
        ) as client:
            # 1. Test Normal Chat
            if 1 in tests_to_run:
                print("\n1. Testing normal chat...")
                resp = await client.chat("Hello! Who are you?")
                print(f"Response: {resp}")
            
            # 2. Test File Request (Special Mode)
            if 2 in tests_to_run:
                print("\n2. Testing file request (/filerequest)...")
                resp = await client.chat("/filerequest /path/to/anyfile.txt")
                print(f"Response: {resp}")
                if resp.files:
                    print(f"Success! File downloaded to: {resp.files[0]}")
                else:
                    print("Failed: No files received for /filerequest.")

            # 3. Test Image Generation (if agent supports it)
            if 3 in tests_to_run:
                print("\n3. Testing image generation...")
                resp = await client.chat("get me a picture of a dancing dog. return path of generated image file in format [FILEPATH: /path/to/file].")
                print(f"Response: {resp}")
                if resp.files:
                    print(f"Success! Agent sent {len(resp.files)} files/images.")
                    for f in resp.files:
                        print(f" - {f}")
                else:
                    print("No images received (Agent might not have image tools enabled).")

            # 4. Test Large File (HTTP Side-Channel)
            if 4 in tests_to_run:
                print("\n4. Testing 100MB file (HTTP Streaming)...")
                resp = await client.chat("/filerequest big_file.bin")
                print(f"Response: {resp}")
                if resp.files:
                    f_path = resp.files[0]
                    print(f"Success! Large file received at: {f_path}")
                    if os.path.exists(f_path):
                        print(f"Size: {os.path.getsize(f_path)} bytes")
                else:
                    print("Failed: No files received for large file request.")

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Test failed with error: {e}")
        print("\nTIP: Make sure the server is running with 'python -m openclaw_acp_bridge.server --debug'")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenClaw ACP Bridge Test Suite")
    parser.add_argument("--tests", type=str, default="1,2,3,4", help="Comma-separated list of tests to run (1-4)")
    args = parser.parse_args()
    
    selected_tests = [int(t.strip()) for t in args.tests.split(",")]
    asyncio.run(run_test(selected_tests))
