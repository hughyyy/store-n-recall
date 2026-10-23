"""Config tests: env parsing, path defaults (XDG), project resolution precedence."""

import os
import tempfile
import unittest
from unittest import mock

from store_n_recall.config import FactError, load_config

CLEAN_ENV = {
    "STORE_N_RECALL_DB": "",
    "STORE_N_RECALL_PROJECT": "",
    "STORE_N_RECALL_AGENT": "",
    "XDG_DATA_HOME": "",
    "HOME": "/home/testuser",
}


class LoadConfigTests(unittest.TestCase):
    """DB path resolution: STORE_N_RECALL_DB > XDG_DATA_HOME > ~/.local/share."""

    def test_default_path_uses_home_local_share(self):
        with mock.patch.dict(os.environ, CLEAN_ENV, clear=True):
            cfg = load_config()
        self.assertEqual(
            cfg.db_path, "/home/testuser/.local/share/store-n-recall/facts.db"
        )

    def test_xdg_data_home_wins_over_home(self):
        env = dict(CLEAN_ENV, XDG_DATA_HOME="/opt/data")
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertEqual(cfg.db_path, "/opt/data/store-n-recall/facts.db")

    def test_empty_xdg_data_home_falls_back_to_home(self):
        env = dict(CLEAN_ENV, XDG_DATA_HOME="   ")
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertTrue(cfg.db_path.endswith(".local/share/store-n-recall/facts.db"))

    def test_db_override_beats_everything(self):
        env = dict(CLEAN_ENV, XDG_DATA_HOME="/opt/data", STORE_N_RECALL_DB="/srv/facts.db")
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertEqual(cfg.db_path, "/srv/facts.db")

    def test_db_override_expands_tilde(self):
        env = dict(CLEAN_ENV, STORE_N_RECALL_DB="~/custom/facts.db")
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertEqual(cfg.db_path, "/home/testuser/custom/facts.db")

    def test_db_override_whitespace_treated_as_unset(self):
        env = dict(CLEAN_ENV, STORE_N_RECALL_DB="  ")
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertTrue(cfg.db_path.endswith("store-n-recall/facts.db"))

    def test_no_data_directory_raises_clear_error(self):
        env = dict(CLEAN_ENV, STORE_N_RECALL_DB="", XDG_DATA_HOME="")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("os.path.expanduser", return_value=""):
            with self.assertRaises(FactError) as cm:
                load_config()
        self.assertIn("STORE_N_RECALL_DB", str(cm.exception))

    def test_project_env_default(self):
        env = dict(CLEAN_ENV, STORE_N_RECALL_PROJECT="  my-proj  ")
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertEqual(cfg.default_project, "my-proj")

    def test_project_env_unset_and_blank(self):
        for value in ("", "   "):
            env = dict(CLEAN_ENV, STORE_N_RECALL_PROJECT=value)
            with mock.patch.dict(os.environ, env, clear=True):
                cfg = load_config()
            self.assertIsNone(cfg.default_project)

    def test_agent_env_default_and_override(self):
        with mock.patch.dict(os.environ, CLEAN_ENV, clear=True):
            self.assertEqual(load_config().agent, "unknown")
        env = dict(CLEAN_ENV, STORE_N_RECALL_AGENT="builder")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(load_config().agent, "builder")

    def test_agent_env_whitespace_falls_back(self):
        env = dict(CLEAN_ENV, STORE_N_RECALL_AGENT="   ")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(load_config().agent, "unknown")

    def test_xdg_data_home_with_trailing_slash(self):
        env = dict(CLEAN_ENV, XDG_DATA_HOME="/opt/data/")
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertEqual(cfg.db_path, "/opt/data/store-n-recall/facts.db")

    def test_default_project_and_agent_coexist(self):
        env = dict(CLEAN_ENV, STORE_N_RECALL_PROJECT="p1", STORE_N_RECALL_AGENT="a1")
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertEqual((cfg.default_project, cfg.agent), ("p1", "a1"))


class ResolveProjectTests(unittest.TestCase):
    """Param wins > env fallback > clear error. No cwd-derived default exists
    at all: the resolution input is (param, configured default), never cwd."""

    def _cfg(self, default=None):
        from store_n_recall.config import Config
        return Config(db_path="unused.db", default_project=default)

    def test_param_wins_over_default(self):
        cfg = self._cfg(default="env-project")
        self.assertEqual(cfg.resolve_project("param-project"), "param-project")

    def test_param_stripped(self):
        cfg = self._cfg(default="env-project")
        self.assertEqual(cfg.resolve_project("  param-project "), "param-project")

    def test_blank_param_falls_back_to_default(self):
        cfg = self._cfg(default="env-project")
        self.assertEqual(cfg.resolve_project("   "), "env-project")

    def test_default_used_when_param_absent(self):
        cfg = self._cfg(default="env-project")
        self.assertEqual(cfg.resolve_project(None), "env-project")

    def test_missing_project_raises_clear_error(self):
        cfg = self._cfg(default=None)
        with self.assertRaises(FactError) as cm:
            cfg.resolve_project(None)
        self.assertIn("STORE_N_RECALL_PROJECT", str(cm.exception))

    def test_missing_project_with_blank_env_raises(self):
        cfg = self._cfg(default=None)
        with self.assertRaises(FactError):
            cfg.resolve_project("  ")

    def test_non_string_param_is_rejected_even_with_a_default(self):
        cfg = self._cfg(default="env-project")
        for bad in (5, 0, True, [], {"project": "x"}):
            with self.assertRaises(FactError, msg=repr(bad)):
                cfg.resolve_project(bad)

    def test_non_string_param_is_rejected_without_a_default(self):
        cfg = self._cfg(default=None)
        with self.assertRaises(FactError):
            cfg.resolve_project([])



if __name__ == "__main__":
    unittest.main()
