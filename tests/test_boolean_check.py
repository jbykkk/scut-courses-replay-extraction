import unittest
from assistant_service.study_agent.boolean_check import verify_boolean_design as check

class BooleanCheckTests(unittest.TestCase):
    def test_source_exercise_and_mismatched_answer_give_real_counterexample(self):
        r=check(['x','y','z'],"x'*y*z+x*y'*z+x*y'*z+x*y*z",'x*y+x*z+y*z')
        self.assertFalse(r['equivalent']);self.assertEqual(r['counterexample']['inputs'],{'x':1,'y':1,'z':0})
        self.assertTrue(check(['x','y','z'],"x'*y*z+x*y'*z+x*y'*z+x*y*z",'z*(x+y)')['equivalent'])
    def test_consensus_does_not_change_boolean_function(self):
        r=check(['a','b','c'],"a'*b+a*c","a'*b+a*c+b*c")
        self.assertTrue(r['equivalent']);self.assertEqual(r['checked_rows'],8)
        self.assertIn('不证明',r['scope'])
    def test_original_five_variable_kmap(self):
        r=check(['v','w','x','y','z'],"v'*w'*x*z+w'*x'*y+v*w'*y+v*w*z","v'*w'*x*z+w'*x'*y+v*w'*y+v*w*z")
        self.assertEqual(r['original_on_set'],[2,3,5,7,18,19,22,23,25,27,29,31])
    def test_no_eval_files_or_implicit_variable_products(self):
        for e in ["__import__('os')",'A; delete x','AB','open(x)','A/0']:
            with self.assertRaises(ValueError):check(['A','B'],e,'A')
    def test_bounded_nesting_and_unique_variables(self):
        with self.assertRaises(ValueError):check(['a','A'],'a','a')
        with self.assertRaises(ValueError):check(['a'],'('*40+'a'+')'*40,'a')
    def test_operator_precedence_and_postfix_complement(self):
        self.assertTrue(check(['A','B'],'not(A and B)',"A'+B'")['equivalent'])
        self.assertTrue(check(['A','B'],'A xor B',"A'*B+A*B'")['equivalent'])

if __name__=='__main__':unittest.main()
