"""Step 1: Send code request and save the session state for step 2."""
import pickle
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

client = TelegramClient(StringSession(), 22642627, '10285055a1cec8f76fb66f2af1960f3e')
client.connect()
result = client.send_code_request('+4407434714953')
# Save session + phone_code_hash for step 2
ss = StringSession.save(client.session)
with open('/tmp/tg_auth_state.txt', 'w') as f:
    f.write(ss + '\n')
    f.write(result.phone_code_hash + '\n')
print("CODE SENT to +4407434714953")
print("Session saved. Run auth_step2.py <code> immediately.")
client.disconnect()
