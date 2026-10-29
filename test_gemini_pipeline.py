import sys
import os
import time
import asyncio
import socket
import json
import numpy as np

def run_tests():
    print("--- 1. Authentication & Secret Store Test ---")
    try:
        from kakarot_daemon.credentials import get_gemini_api_key
        api_key = get_gemini_api_key()
        if not api_key:
            print("[FAIL] API key is empty or missing.")
            return False
        if not api_key.startswith("AIza"):
            print("[WARN] API key does not look like a standard Gemini key.")
        else:
            print("[PASS] API key located and validated.")
    except Exception as e:
        print(f"[FAIL] Auth test exception: {e}")
        return False
        
    print("\n--- 2. Audio Hardware & Subsystem Check ---")
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        default_in = sd.default.device[0]
        default_out = sd.default.device[1]
        print(f"Default Input Device: {default_in} - {devices[default_in]['name']}")
        print(f"Default Output Device: {default_out} - {devices[default_out]['name']}")
        
        print("Testing Mic (2s capture at 16kHz)...")
        # 2 seconds of 16 kHz mono signed 16-bit PCM
        mic_data = sd.rec(int(2 * 16000), samplerate=16000, channels=1, dtype='int16')
        sd.wait()
        rms = np.sqrt(np.mean(mic_data.astype(np.float32)**2))
        if rms > 0:
            print(f"[PASS] Mic active. RMS level: {rms:.2f}")
        else:
            print(f"[FAIL] Mic dead/muted. RMS: 0.0")
            
        print("Testing Speaker (440Hz sine wave for 0.5s at 24kHz)...")
        samplerate = 24000
        t = np.linspace(0, 0.5, int(samplerate * 0.5), endpoint=False)
        wave = (np.sin(2 * np.pi * 440 * t) * 32767 * 0.1).astype(np.int16)
        sd.play(wave, samplerate=samplerate)
        sd.wait()
        print("[PASS] Synthetic sine wave played.")
    except Exception as e:
        print(f"[FAIL] Audio subsystem check exception: {e}")
        return False

    return True

async def async_tests():
    print("\n--- 3. Unix Domain Socket & IPC Subscription Check ---")
    try:
        import socket
        import json
        sock_path = os.path.expanduser("~/.local/share/kakarot/kakarot.sock")
        if not os.path.exists(sock_path):
            print("[FAIL] Socket does not exist. Is the daemon running?")
        else:
            # Connect to socket
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(sock_path)
            client.setblocking(False)
            print("[PASS] Connected to Unix Domain Socket.")
            
            # Read initial state
            initial_state = ""
            timeout = time.time() + 5.0
            while time.time() < timeout:
                try:
                    data = client.recv(4096)
                    if data:
                        initial_state += data.decode()
                        if "\n" in initial_state:
                            break
                except BlockingIOError:
                    await asyncio.sleep(0.1)
                    
            if initial_state:
                state_json = json.loads(initial_state.strip().split("\n")[0])
                print(f"[PASS] Initial State Snapshot received: {state_json}")
                
                # Send toggle_mic
                print("Sending toggle_mic command...")
                client.sendall(b"toggle_mic\n")
                
                # Wait for state change to LISTENING
                state_changed = False
                timeout = time.time() + 5.0
                buffer = ""
                while time.time() < timeout:
                    try:
                        data = client.recv(4096)
                        if data:
                            buffer += data.decode()
                            while "\n" in buffer:
                                line, buffer = buffer.split("\n", 1)
                                if line:
                                    update = json.loads(line)
                                    # Look for LISTENING, which corresponds to muted=False in the NDJSON
                                    if update.get("muted") is False:
                                        state_changed = True
                                        print("[PASS] Daemon transitioned from IDLE_MUTED to LISTENING.")
                                        break
                            if state_changed:
                                break
                    except BlockingIOError:
                        await asyncio.sleep(0.1)
                
                if not state_changed:
                    print("[FAIL] Did not see state transition over NDJSON stream.")
                else:
                    # toggle mic back
                    client.sendall(b"toggle_mic\n")
            else:
                print("[FAIL] Failed to read initial state from socket.")
    except Exception as e:
        print(f"[FAIL] Socket test exception: {e}")

    print("\n--- 4. Live WebSocket End-to-End Talk-Back Test ---")
    try:
        from google import genai
        from google.genai import types
        import sounddevice as sd
        from kakarot_daemon.credentials import get_gemini_api_key
        
        api_key = get_gemini_api_key()
        client = genai.Client(api_key=api_key)
        
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction="You are a test script. Just say: 'I can hear you' in english and nothing else."
        )
        print("Connecting to Gemini Live WebSocket...")
        async with client.aio.live.connect(model="gemini-2.0-flash-exp", config=config) as session:
            print("[PASS] WebSocket Connected.")
            
            # Send a pre-recorded test audio frame or text
            print("Sending voice test via WebSocket...")
            await session.send_realtime_input(text="Hello, confirm you can hear me by saying 'I can hear you'.")
            
            print("Listening for incoming chunks...")
            audio_received = False
            audio_data = bytearray()
            
            async for message in session.receive():
                server_content = getattr(message, "server_content", None)
                if server_content is not None:
                    model_turn = getattr(server_content, "model_turn", None)
                    if model_turn:
                        for part in getattr(model_turn, "parts", []):
                            inline_data = getattr(part, "inline_data", None)
                            if inline_data and getattr(inline_data, "data", None):
                                audio_received = True
                                audio_data.extend(inline_data.data)
                
                # if we received audio and the turn is complete, break
                if getattr(server_content, "turn_complete", False):
                    break
                    
            if audio_received:
                print(f"[PASS] server_content.model_turn.parts contains native 24 kHz audio bytes ({len(audio_data)} bytes).")
                print("Piping bytes into sounddevice.RawOutputStream...")
                
                # play it
                with sd.RawOutputStream(samplerate=24000, channels=1, dtype='int16') as stream:
                    stream.write(bytes(audio_data))
                print("[PASS] Audio played through speakers.")
            else:
                print("[FAIL] No audio data received from Gemini.")
                
    except Exception as e:
        print(f"[FAIL] E2E WebSocket test exception: {e}")
        import traceback
        traceback.print_exc()

async def main():
    if not run_tests():
        return
    await async_tests()

if __name__ == "__main__":
    asyncio.run(main())
