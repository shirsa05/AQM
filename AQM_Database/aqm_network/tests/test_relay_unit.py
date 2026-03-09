from AQM_Database.aqm_network.relay_server import RelayServer


class TestRelayServerUnit:
    def test_store_parcel_creates_mailbox(self):
        server = RelayServer("localhost", 0)
        server.store_parcel("bob", '{"msg_type":"PARCEL","data":"hello"}')
        assert "bob" in server.mailbox
        assert len(server.mailbox["bob"]) == 1

    def test_store_parcel_appends(self):
        server = RelayServer("localhost", 0)
        server.store_parcel("bob", "msg1")
        server.store_parcel("bob", "msg2")
        assert len(server.mailbox["bob"]) == 2

    def test_mailbox_starts_empty(self):
        server = RelayServer("localhost", 0)
        assert server.mailbox == {}
        assert server.connected_clients == {}
