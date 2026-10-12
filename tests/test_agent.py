import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from umbral import agent, i18n

KEY = "3103964883244563063"
# Respuesta real del Agent a las 13:05:19 del 2026-10-12, mientras actualizaba WoW Forever
UPDATING = {"state": 1003, "progress": 0.459, "download_complete": False, "download_rate": 18305249,
            "download_remaining": 226521747, "estimated_time_in_sec": 25}


def serve(handler) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def fake_agent(update: dict):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.headers.get("Authorization") != KEY:           # como el Agent: sin clave, 401 vacío
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps({"authorization": KEY, "session": "1"} if self.path == "/agent" else update).encode()
            self.send_response(200 if self.path in ("/agent", "/update/wow_classic_beta") else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return serve(H)


def fake_battlenet():
    class H(BaseHTTPRequestHandler):
        seen: list = []

        def log_message(self, *a):
            pass

        def do_GET(self):
            H.seen.append(self.headers.get("Authorization"))
            self.send_response(404)
            self.send_header("Server", "Battle.net/2.53")
            self.send_header("Content-Length", "9")
            self.end_headers()
            self.wfile.write(b"Not Found")
    srv = serve(H)
    srv.handler = H
    return srv


class TestParse(unittest.TestCase):
    def test_only_an_ongoing_download_counts(self):
        p = agent.parse_update(UPDATING)
        self.assertEqual((p.percent, p.rate, p.eta), (45, 18305249, 25))
        # el Agent repite estados con la descarga completa: no es una actualización
        for state in (1000, 1003, 1004, 1008):
            self.assertIsNone(agent.parse_update({**UPDATING, "state": state, "progress": 1.0,
                                                  "download_complete": True}))
        self.assertIsNone(agent.parse_update({**UPDATING, "state": 1004}))
        self.assertIsNone(agent.parse_update({}))
        self.assertIsNone(agent.parse_update([]))

    def test_eta_is_ignored_without_speed(self):
        # al aplicar el parche la velocidad es 0 y el tiempo estimado no significa nada
        p = agent.parse_update({**UPDATING, "download_rate": 0, "estimated_time_in_sec": 768079})
        self.assertEqual(p.eta, 0)

    @mock.patch.object(i18n, "LANG", "es")
    def test_text_under_the_bar(self):
        self.assertEqual(agent.describe(agent.Progress(0.0)), "Preparando la actualización…")
        self.assertEqual(agent.describe(agent.Progress(0.96, 0, 0, 0)), "Aplicando la actualización… 96 %")
        self.assertEqual(agent.describe(agent.Progress(0.42, 12_000_000, 1, 480)), "Actualizando 42 % · 12 MB/s · 8 min")
        self.assertEqual(agent.describe(agent.Progress(0.5, 18_305_249, 1, 25)),
                         "Actualizando 50 % · 18.3 MB/s · menos de 1 min")


class TestKeyFromLog(unittest.TestCase):
    def test_newest_log_wins_and_missing_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(agent.read_auth(d), "")
            old = Path(d) / "drive_c/ProgramData/Battle.net/Agent/Agent.9775/Logs/Agent-1.log"
            new = Path(d) / "drive_c/ProgramData/Battle.net/Agent/Agent.9824/Logs/Agent-2.log"
            for f, key in ((old, "1111111111"), (new, KEY)):
                f.parent.mkdir(parents=True)
                f.write_text(f'Response 200: {{\n  "authorization": "{key}",\n  "session": "9"\n}}')
            import os
            os.utime(old, (1, 1))
            self.assertEqual(agent.read_auth(d), KEY)


def fake_agent_posts(accept: bool, seen: list):
    """Agent que recibe POST /update: guarda lo recibido y acepta o rechaza."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, code, body=b"{}"):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get("Authorization") != KEY:
                return self._reply(401, b"")
            self._reply(200, json.dumps({"authorization": KEY}).encode() if self.path == "/agent" else b"{}")

        def do_POST(self):
            payload = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.headers.get("Authorization") != KEY:
                return self._reply(401, b"")
            seen.append((self.path, json.loads(payload), self.headers.get("Content-Type")))
            if accept:    # lo que respondió el Agent a Battle.net el 2026-10-12
                self._reply(200, json.dumps({"response_uri": "/update/wow_classic_beta",
                                             "result_uri": "/game/wow_classic_beta"}).encode())
            else:
                self._reply(404, b'{"error":1}')
    return serve(H)


class TestRequestUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        log = Path(self.tmp.name) / "drive_c/ProgramData/Battle.net/Agent/Agent.1/Logs/Agent-1.log"
        log.parent.mkdir(parents=True)
        log.write_text(f'{{\n  "authorization": "{KEY}"\n}}')

    def tearDown(self):
        self.tmp.cleanup()

    def test_sends_the_same_request_battlenet_does(self):
        seen: list = []
        srv = fake_agent_posts(True, seen)
        client = agent.AgentClient(self.tmp.name, ports=lambda: [srv.server_address[1]])
        self.assertTrue(client.request_update("wow_classic_beta"))
        self.assertEqual(seen, [("/update", {"uid": "wow_classic_beta", "priority": {"insert_at_head": True, "value": 699}},
                                 "application/json")])
        srv.shutdown()

    def test_rejected_and_not_ready(self):
        srv = fake_agent_posts(False, [])
        client = agent.AgentClient(self.tmp.name, ports=lambda: [srv.server_address[1]])
        self.assertFalse(client.request_update("w3"))                       # el Agent lo rechaza
        srv.shutdown()
        self.assertIsNone(agent.AgentClient(self.tmp.name, ports=lambda: []).request_update("w3"))   # aún no escucha

    def test_only_the_current_session_key_is_used(self):
        # un Agent recién arrancado aún no ha escrito su clave: no vale la de la sesión anterior
        new = Path(self.tmp.name) / "drive_c/ProgramData/Battle.net/Agent/Agent.1/Logs/Agent-2.log"
        new.write_text("[I] Agent started as Administrator\n")
        self.assertEqual(agent.read_auth(self.tmp.name), "")


class TestClient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        log = Path(self.tmp.name) / "drive_c/ProgramData/Battle.net/Agent/Agent.1/Logs/Agent-1.log"
        log.parent.mkdir(parents=True)
        log.write_text(f'{{\n  "authorization": "{KEY}"\n}}')
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
        self.tmp.cleanup()

    def start(self, srv):
        self.servers.append(srv)
        return srv.server_address[1]

    def test_finds_the_agent_among_ports_and_never_sends_the_key_to_battlenet(self):
        bnet, ag = fake_battlenet(), fake_agent(UPDATING)
        ports = [self.start(bnet), self.start(ag)]
        client = agent.AgentClient(self.tmp.name, ports=lambda: ports)
        p = client.progress("wow_classic_beta")
        self.assertEqual(p.percent, 45)
        self.assertEqual(client._port, ports[1])
        self.assertNotIn(KEY, bnet.handler.seen)                 # a Battle.net solo se le pregunta sin clave

    def test_no_update_in_progress_or_unknown_product(self):
        ag = fake_agent({**UPDATING, "state": 1008, "download_complete": True, "progress": 1.0})
        client = agent.AgentClient(self.tmp.name, ports=lambda: [self.start(ag)])
        self.assertIsNone(client.progress("wow_classic_beta"))
        self.assertIsNone(client.progress("w3"))                 # 404

    def test_agent_gone_or_restarted(self):
        ag = fake_agent(UPDATING)
        port = self.start(ag)
        client = agent.AgentClient(self.tmp.name, ports=lambda: [port])
        self.assertIsNotNone(client.progress("wow_classic_beta"))
        ag.shutdown()
        ag.server_close()                                        # se cierra Battle.net
        self.assertIsNone(client.progress("wow_classic_beta"))
        self.assertIsNone(client._port)                          # y se volverá a buscar
        self.assertIsNone(agent.AgentClient(self.tmp.name, ports=lambda: []).progress("w3"))   # nada en escucha

    def test_without_a_key_nothing_is_probed(self):
        with tempfile.TemporaryDirectory() as empty:
            called = []
            client = agent.AgentClient(empty, ports=lambda: called.append(1) or [])
            self.assertIsNone(client.progress("w3"))
            self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
