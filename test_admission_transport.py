"""Temporary control fixtures only; no market or model acceptance claims."""
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import admission_transport as transport
import auto_capture_market_intel as intel
import collector as c
from prepare_frozen_inputs import prepare
from sealed_database import sha256


class QuoteTimeTest(unittest.TestCase):
    def line(self):
        fields = [''] * 55
        for i, value in {1:'fixture', 2:'000001', 3:'6', 4:'6', 30:'20261016150000',
                         33:'6.1', 34:'5.9', 44:'5.8', 45:'6.3', 47:'6.6', 48:'5.4'}.items():
            fields[i] = value
        return 'v_sz000001="' + '~'.join(fields) + '";'

    def test_weekend_retains_source_session_and_unknown_status(self):
        with patch('collector.now', return_value=dt.datetime(2026,10,17,16,tzinfo=c.TZ)):
            row = intel.parse_tx_line(self.line())
        self.assertEqual(row['trade_date'], '2026-10-16')
        self.assertEqual(row['trading_status'], 'unknown')
        self.assertEqual((row['limit_up'], row['limit_down']), (6.6,5.4))

    def test_missing_limits_never_use_market_cap_or_formula(self):
        row = intel.parse_tx_line(self.line().replace('~6.6~5.4~', '~~~'))
        self.assertIsNone(row['limit_up'])
        self.assertIsNone(row['limit_down'])

    def test_invalid_or_future_source_timestamp_rejected(self):
        self.assertIsNone(intel.parse_tx_line(self.line().replace('20261016150000','unknown')))
        self.assertIsNone(intel.parse_tx_line(self.line().replace('20261016150000','20991016150000')))


class TransportTest(unittest.TestCase):
    def make_db(self, path, status=False):
        db = sqlite3.connect(path)
        try:
            db.executescript(c.SCHEMA)
            db.execute(f'PRAGMA application_id={c.APP_ID}')
            db.execute("INSERT INTO daily_quotes(symbol,trade_date,fetched_at,source,finality,provenance) VALUES('000001.SZ','2026-10-16',?,'fixture','fixture','{}')", (c.stamp(),))
            if status:
                db.execute("INSERT INTO security_status_daily VALUES('000001.SZ','2026-10-16',?,0,NULL,'unknown',0,'https://qt.gtimg.cn/',NULL,'{}')", (c.stamp(),))
            db.commit()
        finally:
            db.close()

    def capture(self, root):
        source = root / 'captured'; source.mkdir(); (source/'raw').mkdir()
        db = root / 'source.sqlite3'; self.make_db(db, True)
        body = b'actual-response-control-fixture'
        (source/'raw/000000.body').write_bytes(body)
        evidence = [{'path':'raw/000000.body','url':'https://qt.gtimg.cn/',
                     'fetched_at':c.stamp(),'status':200,'bytes':len(body),'sha256':transport.digest(body)}]
        packet = transport.export_rows(db, '2026-10-16T00:00:00+08:00', source/'admission_rows.json', {}, evidence)
        transport.write(source/'auto_intel_report.json', {'formal_data_ready':False})
        return source, packet

    def test_roundtrip_is_idempotent_and_preserves_prices_and_observation_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source, packet=self.capture(root)
            database=root/'canonical.sqlite3'; self.make_db(database)
            transport.publish(source, root/'transport', '123')
            result=transport.import_pending(database, root/'transport', '2026-10-16')
            self.assertEqual(result[0]['counts']['security_status_daily'],1)
            self.assertEqual(transport.import_pending(database, root/'transport', '2026-10-16'),[])
            db=sqlite3.connect(database)
            try:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM daily_quotes').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT observed_at,verified,delisting FROM security_status_daily').fetchone(),
                                 (packet['tables']['security_status_daily'][0]['observed_at'],0,None))
            finally: db.close()

    def test_corrupt_transport_rejected_before_any_rows_are_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source,_=self.capture(root)
            database=root/'canonical.sqlite3'; self.make_db(database)
            transport.publish(source, root/'transport','124')
            block=next((root/'transport/admission').glob('*/*.b64'))
            block.write_bytes(b'Y29ycnVwdA==\n')
            with self.assertRaisesRegex(ValueError,'block verification'):
                transport.import_pending(database,root/'transport','2026-10-16')
            db=sqlite3.connect(database)
            try: self.assertEqual(db.execute('SELECT COUNT(*) FROM security_status_daily').fetchone()[0],0)
            finally: db.close()

    def test_vendor_packet_cannot_claim_official_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            _, packet=self.capture(Path(directory))
            packet['tables']['security_status_daily'][0]['verified']=1
            with self.assertRaisesRegex(ValueError,'cannot become official'): transport.validate(packet)

    def test_future_or_retimed_observation_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            _, packet=self.capture(Path(directory))
            packet['tables']['security_status_daily'][0]['observed_at']='2026-09-25T12:00:00+08:00'
            with self.assertRaisesRegex(ValueError,'outside actual collection'): transport.validate(packet)

    def test_supplement_from_later_base_waits_for_matching_price_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source,_=self.capture(root)
            database=root/'canonical.sqlite3'; self.make_db(database)
            transport.publish(source, root/'transport','125')
            self.assertEqual(transport.import_pending(database,root/'transport','2026-10-15'),[])

    def test_amount_import_receipt_removes_stale_pending_claim_but_keeps_real_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); database=root/'canonical.sqlite3'; self.make_db(database)
            db=sqlite3.connect(database)
            try:
                db.execute("INSERT INTO security_master VALUES('000001.SZ','000001','SZ','fixture',NULL,NULL,?,?,'fixture',0)", (c.stamp(),c.stamp()))
                db.execute('UPDATE daily_quotes SET open=6,high=6.1,low=5.9,close=6,volume=100,amount=600')
                db.commit()
            finally: db.close()
            prepare(database,'2026-10-16','2026-10-19',sha256(database),root/'before')
            before=json.loads((root/'before/quality.json').read_text(encoding='utf-8'))
            self.assertIn('historical_amount_batch_pending_replay_and_import',before['blockers'])
            db=sqlite3.connect(database)
            try:
                db.execute("INSERT INTO source_log(source,operation,fetched_at,status) VALUES('fixture','verified_amount_evidence_import',?,'success')", (c.stamp(),))
                db.commit()
            finally: db.close()
            prepare(database,'2026-10-16','2026-10-19',sha256(database),root/'after')
            after=json.loads((root/'after/quality.json').read_text(encoding='utf-8'))
            self.assertTrue(after['historical_amount_import_receipt_present'])
            self.assertNotIn('historical_amount_batch_pending_replay_and_import',after['blockers'])
            self.assertIn('1_base_symbols_without_full_amount20',after['blockers'])
            self.assertFalse(after['formal_prediction_allowed'])


if __name__ == '__main__': unittest.main()
