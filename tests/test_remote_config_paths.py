"""A remote session cannot choose which host folder the application serves from.

Found by reading how Settings and the producer asset route fit together: /api/config
accepted a new `logos_path` (or `imagestream_path`, `firmware_path`) from any
administrator and checked only that it was a folder, and /api/producer2/asset/<kind>/
<file> then served any file from it. A signed-in remote administrator could name the
folder LumaSuite keeps its own configuration in and read the device password out of it.

Two independent closes, tested separately: a remote session cannot change the folders,
and the asset route serves pictures only. The config read also stops handing a remote
caller an unsalted hash of the device password.

Real application, isolated state, network fenced.
"""
import os
import tempfile
import unittest

import _support
from _support import ADMIN_PASSWORD, RealApp, RealRemote

import luma_users

SECRET_NAME = "producer_config.json"
SECRET_BODY = "synthetic-device-password-4471"
PICTURE = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


class Base(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.app.seed()
        self.remote = RealRemote(self, self.app)
        self.remote.store.create("root", ADMIN_PASSWORD, luma_users.ROLE_ADMIN)
        self.local = self.m.APP.test_client()
        self.admin = self.remote.sign_in("root", ADMIN_PASSWORD)
        self.app.patch("_save_persisted_config", lambda: None)

        self.folder = tempfile.mkdtemp(prefix="luma-assets-")
        self.elsewhere = tempfile.mkdtemp(prefix="luma-elsewhere-")
        with open(os.path.join(self.folder, SECRET_NAME), "w") as handle:
            handle.write(SECRET_BODY)
        with open(os.path.join(self.folder, "logo.png"), "wb") as handle:
            handle.write(PICTURE)
        self.m.CONFIG["logos_path"] = self.folder
        self.m.CONFIG["imagestream_path"] = self.folder
        self.m.CONFIG["firmware_path"] = self.folder


class FolderChoiceTests(Base):
    KEYS = ("logos_path", "imagestream_path", "firmware_path")

    def test_a_remote_administrator_cannot_choose_a_folder(self):
        for key in self.KEYS:
            with self.subTest(key=key):
                response = self.remote.post(self.admin, "/api/config", {key: self.elsewhere})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.get_json()["reason"], "local_only")
                self.assertEqual(self.m.CONFIG[key], self.folder)

    def test_a_refused_save_applies_nothing_else_in_the_same_request(self):
        before = self.m.CONFIG.get("ws_port")
        self.remote.post(self.admin, "/api/config", {"ws_port": 4242, "logos_path": self.elsewhere})
        self.assertEqual(self.m.CONFIG.get("ws_port"), before)

    def test_repeating_the_current_folders_is_an_ordinary_save(self):
        body = {key: self.folder for key in self.KEYS}
        body["ws_port"] = 81
        response = self.remote.post(self.admin, "/api/config", body)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.m.CONFIG["ws_port"], 81)

    def test_the_folder_shown_back_with_different_spelling_is_still_the_same_folder(self):
        spelled = self.folder + os.sep + "." + os.sep
        self.assertEqual(self.remote.post(self.admin, "/api/config", {"logos_path": spelled}).status_code, 200)

    def test_the_local_operator_still_chooses_folders(self):
        response = self.local.post("/api/config", json={"logos_path": self.elsewhere})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.m.CONFIG["logos_path"], os.path.abspath(self.elsewhere))

    def test_a_non_object_body_is_not_a_crash(self):
        # A body that is not an object is refused, not ignored (it used to be read as an empty request).
        self.assertEqual(self.remote.post(self.admin, "/api/config", ["a", "list"]).status_code, 400)


class AssetRouteTests(Base):
    def get(self, client, kind, name):
        return client.get("/api/producer2/asset/%s/%s" % (kind, name))

    def test_pictures_are_still_served(self):
        for kind in ("logos", "imagestream"):
            with self.subTest(kind=kind):
                response = self.get(self.admin, kind, "logo.png")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_data(), PICTURE)

    def test_a_file_that_is_not_a_picture_is_not_served(self):
        for kind in ("logos", "imagestream"):
            for client in (self.admin, self.local):
                with self.subTest(kind=kind):
                    response = self.get(client, kind, SECRET_NAME)
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn(SECRET_BODY, response.get_data(as_text=True))

    def test_the_extension_check_is_not_fooled_by_case(self):
        with open(os.path.join(self.folder, "notes.TXT"), "w") as handle:
            handle.write(SECRET_BODY)
        self.assertEqual(self.get(self.admin, "logos", "notes.TXT").status_code, 404)

    def test_the_folder_cannot_be_escaped(self):
        for name in ("../" + SECRET_NAME, "..%2f" + SECRET_NAME, "sub/../../x.png"):
            with self.subTest(name=name):
                self.assertEqual(self.get(self.admin, "logos", name).status_code, 404)


class ConfigReadTests(Base):
    def test_a_remote_caller_gets_no_hash_of_the_device_password(self):
        self.m.CONFIG["password"] = "Synthetic1!"
        body = self.admin.get("/api/config").get_json()
        self.assertEqual(body["password_hash"], "")

    def test_the_local_operator_is_unchanged(self):
        self.m.CONFIG["password"] = "Synthetic1!"
        self.assertTrue(self.local.get("/api/config").get_json()["password_hash"])


if __name__ == "__main__":
    unittest.main()
