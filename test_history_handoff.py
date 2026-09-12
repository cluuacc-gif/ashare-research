import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import collector as c
from short_term import digest, write_json
import prepare_history_handoff as h
import import_full_history as importer

class HandoffSafety(unittest.TestCase):
    """Temporary metadata-only cases; never market samples or production records."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.original={"latest_successful_dataset":{"sentinel":"preserve-old-success"},
           "latest_attempt":{"kind":"old"},"previous_attempts":[],
           "resources":{"latest_market_database":{"library_file_id":"synthetic-metadata-only","version":4,"sha256":"a"*64},
              "market_database":{},"current_source_repository":{},"rules":{}}}
        self.quality={"kind":"full_history_replay_acceptance","production_database_written":False,
           "generated_at":c.stamp(),"target_date":"2026-09-11","verified_history_symbols":3,
           "history_250_symbols":3,"history_250_latest_at_target":3,"total_real_bars":1700,
           "vendor_universe":5562,"github_run_id":"unit-metadata","commit_sha":"unit-only"}
        self.handoff=self.root/"original.json";self.q=self.root/"quality.json";self.t=self.root/"tasks.json"
        write_json(self.handoff,self.original);write_json(self.q,self.quality)
        write_json(self.t,{"verified_at":c.stamp(),"evening":{"enabled":True},"morning":{"enabled":True}})
        self.original_hash=digest(self.handoff)
    def prepare(self):
        h.run(self.handoff,self.original_hash,self.q,None,self.t,self.root/"prepared.json")
        return json.loads((self.root/"prepared.json").read_text())
    def test_original_and_success_identity_preserved(self):
        result=self.prepare()
        self.assertEqual(digest(self.handoff),self.original_hash)
        self.assertEqual(result["latest_successful_dataset"],self.original["latest_successful_dataset"])
        self.assertEqual(result["latest_attempt"]["market_db"],self.original["resources"]["latest_market_database"])
    def test_unimported_cloud_evidence_cannot_promote(self):
        result=self.prepare()
        self.assertEqual(result["status"],"DATA NOT READY")
        self.assertFalse(result["model_ready"]);self.assertFalse(result["bootstrap_complete"])
        self.assertTrue(result["latest_attempt"]["cloud_evidence_not_yet_imported"])
        self.assertEqual(result["latest_attempt"]["formal_predictions_created"],0)
    def test_verified_next_session_and_current_schedule(self):
        result=self.prepare()
        self.assertEqual(result["next_trade_date"],"2026-09-14")
        self.assertTrue(result["evening_task_state"]["enabled"])
        self.assertTrue(result["morning_task_state"]["enabled"])
    def test_hash_conflict_rejected(self):
        write_json(self.handoff,{"changed":True})
        with self.assertRaises(ValueError):self.prepare()
    def test_unknown_evidence_semantics_rejected(self):
        self.quality["production_database_written"]=True;write_json(self.q,self.quality)
        with self.assertRaises(ValueError):self.prepare()
    def test_import_never_creates_missing_database(self):
        path=self.root/"missing.sqlite3"
        with self.assertRaises(ValueError):importer.existing_database(path,"0"*64)
        self.assertFalse(path.exists())
    def test_import_rejects_prediction_filename(self):
        path=self.root/"预测数据库.sqlite3";path.write_bytes(b"metadata-only")
        with self.assertRaises(ValueError):importer.existing_database(path,digest(path))
        self.assertEqual(path.read_bytes(),b"metadata-only")

if __name__=="__main__":unittest.main()
