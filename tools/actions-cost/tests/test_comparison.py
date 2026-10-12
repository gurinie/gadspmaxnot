import copy
import unittest
from compare_ci_runs import comparison,FULL

def evidence(minutes):
    summary={'schema_version':1,'result':'passed','selected_jobs':sorted(FULL),'source_digest':'same',
             'frontend_inventory_digest':'same','run_id':'123','commit':'abc','run_attempt':1,
             'physical_jobs':sorted(FULL)}
    names=[name for name in FULL if name!='frontend-tests']+['ci-required']+[f'frontend-tests ({i})' for i in range(1,5)]
    jobs={'jobs':[{'name':name,'conclusion':'success','run_id':123,'head_sha':'abc','run_attempt':1,
                   'labels':['ubuntu-latest'],'started_at':'2026-10-08T10:00:00Z',
                   'completed_at':f'2026-10-08T10:0{minutes}:00Z'} for name in names]}
    return summary,jobs

class ComparisonTest(unittest.TestCase):
    def test_equivalent_full_coverage_reduces_minutes(self):
        before,bj=evidence(2);after,aj=evidence(1)
        self.assertTrue(comparison(before,after,bj,aj)['accepted'])
    def test_insufficient_savings_or_slow_execution_not_accepted(self):
        before,bj=evidence(2);after,aj=evidence(2)
        self.assertFalse(comparison(before,after,bj,aj)['accepted'])
        before,bj=evidence(9);after,aj=evidence(7)
        self.assertFalse(comparison(before,after,bj,aj)['accepted'])
    def test_partial_changed_wrong_commit_and_duplicate_evidence_rejected(self):
        for change in ['coverage','source','commit','duplicate','missing','attempt']:
            before,bj=evidence(2);after,aj=evidence(1)
            if change=='coverage':after['selected_jobs'].pop()
            elif change=='source':after['source_digest']='changed'
            elif change=='commit':aj['jobs'][0]['head_sha']='wrong'
            elif change=='duplicate':aj['jobs'].append(aj['jobs'][0])
            elif change=='missing':aj['jobs'].pop()
            else:after['run_attempt']=2
            with self.subTest(change=change),self.assertRaises(ValueError):comparison(before,after,bj,aj)

if __name__=='__main__':unittest.main()
