"""Frozen probability evidence is optional and cannot change mail timing or claims."""
import base64
import copy
import json
import subprocess
import unittest
from unittest.mock import patch

import market_outlook as outlook
import research_context as context
from test_market_outlook import fixture


def study():
    return {'schema':'calibrated-tplus1-probability-v1','base_trade_date':'2026-10-16',
            'generated_at':'2026-10-18T11:00:00+08:00','input_database_sha256':'a'*64,
            'formal_trade_ready':False,'execution_win_rate_80_validated':False,
            'actual_win_rate':None,'policy':{'active':False},'diagnostic':{}}


class ProbabilityIntegrationTests(unittest.TestCase):
    def test_matching_prior_probability_evidence_accepted(self):
        value,source=fixture();value['probability_research']=study()
        self.assertIs(outlook.validate(value,source),value)

    def test_future_mismatch_and_trade_claims_refused(self):
        for key,change in [('generated_at','2026-10-19T09:00:00+08:00'),
                           ('generated_at','2026-10-18T11:00:00'),
                           ('base_trade_date','2026-10-15'),('input_database_sha256','b'*64),
                           ('actual_win_rate',.81),('formal_trade_ready',True),
                           ('execution_win_rate_80_validated',True)]:
            value,source=fixture();value['probability_research']=study()
            value['probability_research'][key]=change
            with self.subTest(key=key),self.assertRaises(ValueError):outlook.validate(value,source)

    def test_published_context_uses_single_bounded_read_and_prior_cutoff(self):
        value,source=fixture();value['probability_research']=study()
        manifest={'snapshot_release':source['release_tag'],'input_database_sha256':'a'*64,
                  'base_trade_date':'2026-10-16','assets_readback_verified_at':'verified','outlook':value}
        envelope=json.dumps({'content':base64.b64encode(json.dumps(manifest).encode()).decode()})
        snapshot={'base_trade_date':'2026-10-16','release_tag':source['release_tag'],'database':source['database']}
        with patch.object(context.subprocess,'run',return_value=subprocess.CompletedProcess([],0,envelope,'')) as read:
            result=context.published_context(snapshot,'2026-10-18T12:00:00+08:00')
            self.assertEqual(read.call_count,1);self.assertEqual(read.call_args.kwargs['timeout'],10)
            self.assertEqual(result['probability_research']['schema'],study()['schema'])
            early=context.published_context(snapshot,'2026-10-18T10:00:00+08:00')
            self.assertEqual(early['probability_research']['status'],'unavailable')

    def test_pointer_archives_large_probability_details_preserving_report(self):
        from market_outlook_report import render
        value,_=fixture();value['probability_research']=study()
        value['probability_research']['model_metadata']={'large':'x'*900_000}
        value['probability_research']['protocol']={'large':'x'*900_000}
        pointer=outlook.compact_pointer({'outlook':value})
        self.assertLess(len(json.dumps(pointer)),40_000)
        self.assertNotIn('model_metadata',pointer['outlook']['probability_research'])
        self.assertEqual(render(value),render(pointer['outlook']))


if __name__=='__main__':unittest.main()
