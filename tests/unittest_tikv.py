import unittest

from synapse.synapse_rust import tikv_engine


class TestNativeTiKVEngine(unittest.TestCase):
    def test_import_and_registration(self) -> None:
        """Test that the tikv_engine module is registered and has all functions."""
        self.assertTrue(hasattr(tikv_engine, "open_client"))
        self.assertTrue(hasattr(tikv_engine, "put"))
        self.assertTrue(hasattr(tikv_engine, "get"))
        self.assertTrue(hasattr(tikv_engine, "batch_get"))
        self.assertTrue(hasattr(tikv_engine, "batch_put"))
        self.assertTrue(hasattr(tikv_engine, "delete"))
        self.assertTrue(hasattr(tikv_engine, "scan_prefix"))

    def test_uninitialized_calls_raise_runtime_error(self) -> None:
        """Test that calling operations before open_client raises RuntimeError."""
        with self.assertRaises(RuntimeError):
            tikv_engine.put(b"test_key", b"test_val")

        with self.assertRaises(RuntimeError):
            tikv_engine.get(b"test_key")

        with self.assertRaises(RuntimeError):
            tikv_engine.batch_get([b"test_key"])

        with self.assertRaises(RuntimeError):
            tikv_engine.batch_put([(b"test_key", b"test_val")])

        with self.assertRaises(RuntimeError):
            tikv_engine.delete(b"test_key")

        with self.assertRaises(RuntimeError):
            tikv_engine.scan_prefix(b"test_prefix", 10)

    def test_open_client_fallback_or_connection(self) -> None:
        """Test connecting to pd_endpoints. Since we might not have a local TiKV, we handle connection error."""
        try:
            tikv_engine.open_client(["127.0.0.1:2379"])
            # If we successfully connected (e.g. if local TiKV is running)
            print("Successfully connected to local TiKV cluster, running KV tests...")

            # Put and Get
            tikv_engine.put(b"tikv_test_key", b"tikv_test_val")
            self.assertEqual(tikv_engine.get(b"tikv_test_key"), b"tikv_test_val")

            # Batch Put and Batch Get
            tikv_engine.batch_put([(b"tk1", b"v1"), (b"tk2", b"v2")])
            batch_res = dict(tikv_engine.batch_get([b"tk1", b"tk2"]))
            self.assertEqual(batch_res.get(b"tk1"), b"v1")
            self.assertEqual(batch_res.get(b"tk2"), b"v2")

            # Scan Prefix
            scan_res = tikv_engine.scan_prefix(b"tk", 10)
            self.assertEqual(len(scan_res), 2)

            # Delete
            tikv_engine.delete(b"tikv_test_key")
            self.assertIsNone(tikv_engine.get(b"tikv_test_key"))

        except RuntimeError as e:
            # Expected if TiKV cluster is not running/available locally
            print(f"Skipping live TiKV operations (TiKV cluster is offline): {e}")


if __name__ == "__main__":
    unittest.main()
