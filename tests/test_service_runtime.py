import socket
import socketserver
import threading
import unittest

import service_runtime


class _HealthHandler(socketserver.BaseRequestHandler):
    def handle(self):
        _ = self.request.recv(4096)
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{\"ok\":true}")


class _RawHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.recv(1024)
        except Exception:
            pass


class _ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class ServiceRuntimeTests(unittest.TestCase):
    def test_probe_local_service_state_free(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.assertEqual(service_runtime.probe_local_service_state("127.0.0.1", port), "free")

    def test_probe_local_service_state_service(self):
        server = _ThreadedTCPServer(("127.0.0.1", 0), _HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            self.assertEqual(service_runtime.probe_local_service_state("127.0.0.1", port), "service")
        finally:
            server.shutdown()
            server.server_close()

    def test_probe_local_service_state_occupied(self):
        server = _ThreadedTCPServer(("127.0.0.1", 0), _RawHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            self.assertEqual(service_runtime.probe_local_service_state("127.0.0.1", port), "occupied")
        finally:
            server.shutdown()
            server.server_close()

    def test_service_instance_guard_blocks_second_owner(self):
        g1 = service_runtime.ServiceInstanceGuard("127.0.0.1", 5678, config_path="a.json")
        g2 = service_runtime.ServiceInstanceGuard("127.0.0.1", 5678, config_path="b.json")
        self.assertTrue(g1.acquire())
        try:
            self.assertFalse(g2.acquire())
        finally:
            g2.release()
            g1.release()


if __name__ == "__main__":
    unittest.main()
