import asyncio
import websockets
from AQM_Database.aqm_network.protocol import parse_message , frame_message

class RelayServer:
    def __init__(self , host , port):
        self.host = host
        self.port = port
        self.connected_clients = {}
        self.mailbox = {}

    async def start(self):
        async with websockets.serve(self.handle_connection , self.host , self.port):
            await asyncio.Future()

    async def handle_connection(self , websocket) -> None:
        message = await websocket.recv()
        msg_type, payload = parse_message(message)
        if not payload.get("user_id") or msg_type != 'AUTH':
            await websocket.send(frame_message("ERROR", {"reason": "auth required"}))
            await websocket.close()
            return

        user_id = payload["user_id"]
        self.connected_clients[user_id] = websocket
        await self.deliver_pending(user_id , websocket)

        try:
            async for message in websocket:
                msg_type, payload = parse_message(message)
                if msg_type == 'PARCEL':
                    await self.route_parcel(user_id, payload["recipient_id"] , message)
        finally:
            if user_id in self.connected_clients:
                del self.connected_clients[user_id]

    async def route_parcel(self, sender_id, recipient_id, raw_frame):
        if recipient_id in self.connected_clients:
            await self.connected_clients[recipient_id].send(raw_frame)
        else:
            self.store_parcel(recipient_id , raw_frame)

    def store_parcel(self, recipient_id, raw_frame):
        self.mailbox.setdefault(recipient_id, []).append(raw_frame)

    async def deliver_pending(self, user_id, websocket):
        if user_id not in self.mailbox:
            return
        pending = self.mailbox.pop(user_id)
        for parcel in pending:
            await websocket.send(parcel)