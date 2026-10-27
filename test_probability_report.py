import json
import hashlib
import unittest
import probability_report as p


def fixture():
    return {'schema':p.VERSION,'formal_trade_ready':False,'policy':{'active':False},
            'coverage':{'causal_live_candidates':3},'model_metadata':{'large':'x'*900000},
            'diagnostic':{'same_universe_price_action_baseline':{'n_attempts':100,'n_unresolved':2,
                          'win_rate':.55,'ge3_rate':.3,'validation':{'selection_gate':False}}},
            'selection':[{'policy':{'id':'hgb_balanced'},'eligible_research':False,
                          'metrics':{'n_closed':20,'independent_entry_dates':10,
                                     'validation':{'familywise_lower_mean_return':-.01,'selection_blockers':['negative']}}}]}


class ProbabilityReportTests(unittest.TestCase):
    def test_legacy_html_remains_byte_for_byte_unchanged(self):
        self.assertEqual(hashlib.sha256(p.render(fixture()).encode()).hexdigest(),
                         '72f9d32fd92f8e3198e582a84fd18dc170c5132827da59c511a02fdfde85b1b8')

    def test_no_current_inputs_visible_in_full_and_compact_without_stale_predictions(self):
        study=fixture()
        study.update(status=p.NO_CURRENT_INPUTS,base_trade_date='2026-10-27',target_trade_date='2026-10-28',
                     training_performed=False,selection_performed=False,
                     current_probability_status='unknown_no_qualifying_current_inputs',
                     policy={'active':True,'chosen':{'id':'hgb_balanced'}},
                     current_watchlist=[{'symbol':'000001.SZ','name':'STALE_SHOULD_NOT_SHOW','p_win3':.99}],
                     endpoint_availability={'hgb':{'p_win3':True}},
                     coverage={'wide_universe_live_rows':100,'causal_live_candidates':0},
                     input_funnel={'current':{'input_rows':100,'qualifying_rows':0,
                         'steps':[{'condition':'market_breadth_ge_0.30','individual_pass':0,
                                   'individual_fail':100,'cumulative_pass':0,'rejected_at_step':80}],
                         'market_context':{'market_breadth':{'min':.2,'max':.2,'finite_rows':100}}}})
        compact=p.compact(study);rendered=p.render(study)
        self.assertEqual(rendered,p.render(compact));self.assertEqual(compact,p.compact(compact))
        self.assertFalse(compact['policy']['active']);self.assertIsNone(compact['policy']['chosen'])
        self.assertEqual(compact['status_message'],'��ģ�͵��ڲ�����')
        self.assertEqual(compact['current_watchlist'],[])
        self.assertFalse(compact['endpoint_availability']['hgb']['p_win3'])
        self.assertEqual(compact['input_funnel'],study['input_funnel'])
        for expected in ('��ģ�͵��ڲ�����','���ڸ���δ֪','2026-10-27','2026-10-28',
                         '��������ϵͳ','����δ��','�����ۼ�ͨ��','�г����Ǳ���','20.00%'):
            self.assertIn(expected,rendered)
        self.assertNotIn('STALE_SHOULD_NOT_SHOW',rendered);self.assertNotIn('99.00%',rendered)
        self.assertNotIn('��һ����ѧģ�ͼ���',rendered)

    def test_full_and_compact_same_html_and_compaction_idempotent(self):
        study=fixture();compact=p.compact(study)
        self.assertLess(len(json.dumps(compact)),20000)
        self.assertEqual(p.render(study),p.render(compact))
        self.assertEqual(compact,p.compact(compact))
        self.assertIn('model_metadata',study)

    def test_inactive_rule_does_not_suggest_diagnostic_winner(self):
        study=fixture();study['current_watchlist']=[{'name':'SHOULD_NOT_SHOW'}]
        rendered=p.render(study)
        self.assertNotIn('SHOULD_NOT_SHOW',rendered)
        self.assertIn('û������',rendered)
        self.assertIn('80%����ʵ��ʤ����δ��֤',rendered)

    def test_active_watchlist_escaped_and_six_percent_distinct(self):
        study=fixture();study['policy']={'active':True,'chosen':{'id':'hgb_balanced'}}
        study['current_watchlist']=[{'symbol':'000001.SZ','name':'<script>x</script>','p_win3':.8}]
        rendered=p.render(study)
        self.assertNotIn('<script>',rendered)
        self.assertIn('&lt;script&gt;',rendered)
        self.assertIn('6%�˳�����',rendered)

    def test_missing_study_does_not_change_old_report(self):
        self.assertEqual(p.render({}), '')


if __name__=='__main__':unittest.main()
