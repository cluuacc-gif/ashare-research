import json
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
        self.assertIn('没有启用',rendered)
        self.assertIn('80%以上实际胜率尚未验证',rendered)

    def test_active_watchlist_escaped_and_six_percent_distinct(self):
        study=fixture();study['policy']={'active':True,'chosen':{'id':'hgb_balanced'}}
        study['current_watchlist']=[{'symbol':'000001.SZ','name':'<script>x</script>','p_win3':.8}]
        rendered=p.render(study)
        self.assertNotIn('<script>',rendered)
        self.assertIn('&lt;script&gt;',rendered)
        self.assertIn('6%退出另列',rendered)

    def test_missing_study_does_not_change_old_report(self):
        self.assertEqual(p.render({}), '')


if __name__=='__main__':unittest.main()
