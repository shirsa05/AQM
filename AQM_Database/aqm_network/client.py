import websockets
import asyncio
from AQM_Database.aqm_network.protocol import frame_message , parse_message
from websockets.exceptions import ConnectionClosed

class Client:
    def __init__(self , server_url , user_id):
        self.server_url = server_url
        self.user_id = user_id
        self._ws = None
        self._on_message = None
        self._listen_task = None

    async def connect(self):
        self._ws = await websockets.connect(self.server_url)
        frame = frame_message("AUTH" , {"user_id" : self.user_id})
        await self._ws.send(frame)
        self._listen_task = asyncio.create_task(self._listen_loop())

    async def _listen_loop(self):
        try:
            async for message in self._ws:
                msg_type , payload = parse_message(message)
                if msg_type == "PARCEL" and self._on_message:
                    await self._on_message(payload)
                elif msg_type == "ERROR":
                    print(f"Server error: {payload.get('reason', 'unknown')}")
        except ConnectionClosed:
            pass

    async def send_parcel(self , recipient_id , encrypted_blob):
        if not self._ws:
            raise RuntimeError("Not connected — call connect() first")
        payload_dict = {"sender_id" : self.user_id , "recipient_id" : recipient_id , "data" : encrypted_blob}
        frame = frame_message("PARCEL" , payload_dict)
        await self._ws.send(frame)

    def on_message(self, callback):
        self._on_message = callback

    async def disconnect(self):
        if self._listen_task:
            self._listen_task.cancel()
        if self._ws:
            await self._ws.close()
        self._ws = None
        self._listen_task = None

