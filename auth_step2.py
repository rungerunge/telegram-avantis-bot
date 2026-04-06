"""Step 2: Sign in with the code. Usage: python3 auth_step2.py <code>"""
import sys
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

code = sys.argv[1]

with open('/tmp/tg_auth_state.txt', 'r') as f:
    lines = f.read().strip().split('\n')
    saved_session = lines[0]
    phone_code_hash = lines[1]

client = TelegramClient(StringSession(saved_session), 22642627, '10285055a1cec8f76fb66f2af1960f3e')
client.connect()
client.sign_in('+4407434714953', code, phone_code_hash=phone_code_hash)
final_session = StringSession.save(client.session)
print("SESSION:" + final_session)
client.disconnect()
